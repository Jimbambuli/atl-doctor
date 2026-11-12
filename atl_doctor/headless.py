# SPDX-License-Identifier: GPL-3.0-or-later
"""A private, invisible KWin session (`kwin_wayland --virtual`) for --headless and --steps.

Own Wayland socket, own XDG_RUNTIME_DIR inside the capture (run/), own D-Bus session; the user's
desktop is not touched. Windows are queried and moved with KWin scripts, screenshots come from spectacle.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .errors import DoctorError

APP_WIDTH, APP_HEIGHT = 1333, 800
START_TIMEOUT_S = 20.0
SCRIPT_WAIT_S = 0.5
SOCKET_PATH_MAX = 100
_TAG = "ATLDOC"
# the distribution's session bus configuration; the private bus takes it and listens in the runtime directory too
SESSION_CONFS = (Path("/usr/share/dbus-1/session.conf"), Path("/etc/dbus-1/session.conf"))
_WATCH_TAG = "ATLWATCH"
# windows of the app: normal windows, dialogs and popups (KWin's own windows are none of these)
_APP_WINDOW = "(w.normalWindow || w.dialog || w.popupWindow)"


def _own_address(socket: Path) -> str:
    """D-Bus address of a socket path; bytes outside the spec's optionally-escaped set become %XX."""
    ok = set(b"-_/.*" b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return "unix:path=" + "".join(chr(b) if b in ok else f"%{b:02x}" for b in os.fsencode(socket))


def _bus_config(base: Path, socket: Path) -> str:
    from xml.sax.saxutils import escape

    return ('<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"\n'
            ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
            f"<busconfig>\n  <include>{escape(str(base))}</include>\n"
            f"  <listen>{escape(_own_address(socket))}</listen>\n</busconfig>\n")


class HeadlessSession:
    def __init__(self, run_dir: Path):
        self._link = None
        self.run_dir = Path(run_dir)
        self.socket = f"wayland-atl-doctor-{os.getpid()}"
        if len(str(self.run_dir.resolve() / self.socket)) > SOCKET_PATH_MAX:
            # a Unix socket path has at most 107 bytes: run the session in a short temporary directory
            # and leave a link to it in the capture
            self._link = self.run_dir
            self.run_dir = Path(tempfile.mkdtemp(prefix="atl-doctor-"))
            self._link.symlink_to(self.run_dir, target_is_directory=True)
        self.proc: subprocess.Popen | None = None
        self.bus = ""
        self._n = 0
        self._script_lock = threading.Lock()  # mark screenshots run beside the step driver
        self._watching = False
        self._events: list[dict] = []
        self._events_pos = 0

    @staticmethod
    def missing() -> str | None:
        """Why no session can be started here, or None."""
        return None if shutil.which("kwin_wayland") else "--headless needs kwin_wayland (install KWin)"

    @property
    def log(self) -> Path:
        return self.run_dir / "kwin.log"

    def start(self, width: int = 1400, height: int = 900) -> None:
        cfg = self.run_dir / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        (cfg / "kxkbrc").write_text("[Layout]\nLayoutList=us\nUse=true\n")  # fixed layout; the input helper reads it
        # the bus socket lies at XDG_RUNTIME_DIR/bus, where programs look without DBUS_SESSION_BUS_ADDRESS
        # (glycin's `env -i XDG_RUNTIME_DIR=... flatpak-spawn` in a Flatpak); the address file is named apart
        bus_file = self.run_dir / "bus.address"
        dbus = ["dbus-run-session"]
        conf = next((c for c in SESSION_CONFS if c.is_file()), None)
        if conf is not None:
            (self.run_dir / "dbus.conf").write_text(_bus_config(conf, self.run_dir / "bus"), encoding="utf-8")
            dbus.append(f"--config-file={self.run_dir / 'dbus.conf'}")
        env = {**os.environ, "XDG_RUNTIME_DIR": str(self.run_dir), "XDG_CONFIG_HOME": str(cfg),
               "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1", "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
               # script print() is debug output of category js, read back from kwin.log: some distributions
               # switch debug off (qtlogging.ini) and Qt may log to journald instead of stderr
               "QT_LOGGING_RULES": ";".join(filter(None, [os.environ.get("QT_LOGGING_RULES"), "js.debug=true"])),
               "QT_FORCE_STDERR_LOGGING": "1"}
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        # paths go in as arguments ($1, $2), never into the script text
        script = ('echo "$DBUS_SESSION_BUS_ADDRESS" > "$1"; exec kwin_wayland --virtual --no-lockscreen '
                  f'--no-global-shortcuts --width {width} --height {height} --socket "$2"')
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen([*dbus, "--", "sh", "-c", script, "sh", str(bus_file), self.socket],
                                         env=env, stdout=log,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < end:
            if (self.run_dir / self.socket).exists() and bus_file.is_file() and bus_file.read_text().strip():
                # only the socket in the runtime directory: it is what a sandbox sharing that directory reaches
                self.bus = _own_address(self.run_dir / "bus") if conf is not None else bus_file.read_text().strip()
                time.sleep(1.0)  # KWin finishes its own setup after creating the socket
                self._start_watch()
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        tail = self.log.read_text(errors="replace").strip().splitlines()[-3:]
        self.stop()
        raise DoctorError("kwin_wayland --virtual did not start: " + (" / ".join(tail) or "no output"))

    def env(self) -> dict[str, str]:
        """Environment for programs that run inside the session (ATL, the input helper, spectacle)."""
        return {"XDG_RUNTIME_DIR": str(self.run_dir), "WAYLAND_DISPLAY": self.socket, "GDK_BACKEND": "wayland",
                "DBUS_SESSION_BUS_ADDRESS": self.bus, "QT_QPA_PLATFORM": "wayland", "XKB_DEFAULT_LAYOUT": "us"}

    def _script(self, js: str) -> list[str]:
        """Run a KWin script; its print() lines starting with ATLDOC (without the tag)."""
        with self._script_lock:
            return self._script_locked(js)

    def _script_locked(self, js: str) -> list[str]:
        self._n += 1
        path = self.run_dir / f"script-{self._n}.js"
        path.write_text(js, encoding="utf-8")
        name = f"atldoc{self._n}"
        start = self.log.stat().st_size
        env = {**os.environ, **self.env()}

        def gdbus(method: str, *args: str) -> None:
            subprocess.run(["gdbus", "call", "--session", "-d", "org.kde.KWin", "-o", "/Scripting", "-m",
                            f"org.kde.kwin.Scripting.{method}", *args], env=env, capture_output=True, timeout=10)

        try:
            gdbus("loadScript", str(path), name)
            gdbus("start")
            time.sleep(SCRIPT_WAIT_S)
            gdbus("unloadScript", name)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DoctorError(f"KWin script failed: {e}") from e
        finally:
            path.unlink(missing_ok=True)
        with open(self.log, "rb") as f:
            f.seek(start)
            new = f.read().decode("utf-8", "replace")
        out = []
        for line in new.splitlines():
            i = line.find(f"js: {_TAG}\t")
            if i >= 0:
                out.append(line[i + len(_TAG) + 5:])
        return out

    def _start_watch(self) -> None:
        """Load a KWin script that stays: it prints a line, stamped with KWin's own clock, whenever an app window
        appears or changes its title or class. window_events() reads them, so a window is dated by when it came,
        not by when a query noticed it (a query is a script run of SCRIPT_WAIT_S)."""
        js = (f"function say(w) {{ if (!{_APP_WINDOW}) return; "
              f"print('{_WATCH_TAG}\\t' + [Date.now(), w.dialog ? 'dialog' : w.popupWindow ? 'popup' : 'normal', "
              f"String(w.caption).replace(/[\\t\\r\\n]+/g, ' '), String(w.resourceClass)].join('\\t')); }} "
              f"function hook(w) {{ say(w); w.captionChanged.connect(() => say(w)); "
              f"w.windowClassChanged.connect(() => say(w)); }} "
              f"for (const w of workspace.stackingOrder) hook(w); workspace.windowAdded.connect(hook);")
        path = self.run_dir / "script-watch.js"
        path.write_text(js, encoding="utf-8")
        env = {**os.environ, **self.env()}
        try:
            r = subprocess.run(["gdbus", "call", "--session", "-d", "org.kde.KWin", "-o", "/Scripting", "-m",
                                "org.kde.kwin.Scripting.loadScript", str(path), "atldocwatch"],
                               env=env, capture_output=True, timeout=10)
            subprocess.run(["gdbus", "call", "--session", "-d", "org.kde.KWin", "-o", "/Scripting", "-m",
                            "org.kde.kwin.Scripting.start"], env=env, capture_output=True, timeout=10)
            self._watching = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            self._watching = False  # window_events() says None, the driver queries as before

    def window_events(self) -> list[dict] | None:
        """Window appearances and title/class changes so far, oldest first: t_us (CLOCK_BOOTTIME, as in
        capture.now_us), caption, resource_class, kind. None without a watch script (then only windows())."""
        if not self._watching:
            return None
        try:
            with open(self.log, "rb") as f:
                f.seek(self._events_pos)
                new = f.read()
        except OSError:
            return self._events
        nl = new.rfind(b"\n") + 1   # only whole lines
        self._events_pos += nl
        from .capture import now_us
        boot, epoch = now_us()
        for line in new[:nl].decode("utf-8", "replace").splitlines():
            i = line.find(f"js: {_WATCH_TAG}\t")
            f = line[i + len(_WATCH_TAG) + 5:].split("\t") if i >= 0 else []
            if len(f) == 4:
                try:
                    t_us = int(float(f[0]) * 1000) - epoch + boot
                except ValueError:
                    continue
                self._events.append({"t_us": t_us, "kind": f[1], "caption": f[2], "resource_class": f[3]})
        return self._events

    def windows(self) -> list[dict]:
        """App windows bottom to top: caption, resource class, kind, frame geometry (x, y, w, h) and the origin
        of the surface; GTK widget coordinates are relative to the surface, which includes the CSD shadow."""
        js = (f"for (const w of workspace.stackingOrder) if {_APP_WINDOW} {{ const g = w.frameGeometry; "
              f"const b = w.bufferGeometry; "
              f"print('{_TAG}\\t' + [w.caption, w.resourceClass, w.dialog ? 'dialog' : w.popupWindow ? 'popup' : "
              f"'normal', g.x, g.y, g.width, g.height, b.x, b.y].join('\\t')); }}")
        out = []
        for line in self._script(js):
            f = line.split("\t")
            if len(f) == 9:
                try:
                    out.append({"caption": f[0], "resource_class": f[1], "kind": f[2],
                                "rect": tuple(float(v) for v in f[3:7]),
                                "surface": (float(f[7]), float(f[8]))})  # client-side shadow included
                except ValueError:
                    continue
        return out

    def window_list(self) -> list[tuple[str, str]]:
        return [(w["caption"], w["resource_class"]) for w in self.windows()]

    def place_window_at_origin(self) -> None:
        """KWin places a window anew when it is mapped: move the main window to 0,0 until it stays there."""
        js = (f"for (const w of workspace.stackingOrder) if (w.normalWindow) {{ const g = w.frameGeometry; "
              f"w.frameGeometry = {{x: 0, y: 0, width: g.width, height: g.height}}; break; }}")
        for _ in range(30):
            self._script(js)
            ws = [w for w in self.windows() if w["kind"] == "normal"]
            if ws and ws[0]["rect"][:2] == (0.0, 0.0):
                return

    def screenshot(self, path: Path) -> bool:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **self.env(), "XDG_CONFIG_HOME": str(self.run_dir / "cfg")}
        try:
            subprocess.run(["spectacle", "-b", "-n", "-f", "-o", str(path)], env=env, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return path.is_file() and path.stat().st_size > 0

    def stop(self) -> None:
        if self.proc is None:
            return
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                self.proc.wait(wait)
                break
            except subprocess.TimeoutExpired:
                continue
        self.proc = None
        if self._link is not None:  # bring the logs back into the capture, drop the temporary directory
            self._link.unlink()
            self._link.mkdir()
            for f in self.run_dir.glob("*.log"):
                shutil.copy(f, self._link / f.name)
            shutil.rmtree(self.run_dir, ignore_errors=True)
            self.run_dir, self._link = self._link, None
