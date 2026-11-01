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
# windows of the app: normal windows, dialogs and popups (KWin's own windows are none of these)
_APP_WINDOW = "(w.normalWindow || w.dialog || w.popupWindow)"


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

    @property
    def log(self) -> Path:
        return self.run_dir / "kwin.log"

    def start(self, width: int = 1400, height: int = 900) -> None:
        cfg = self.run_dir / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        (cfg / "kxkbrc").write_text("[Layout]\nLayoutList=us\nUse=true\n")  # fixed layout; the input helper reads it
        bus_file = self.run_dir / "bus"
        env = {**os.environ, "XDG_RUNTIME_DIR": str(self.run_dir), "XDG_CONFIG_HOME": str(cfg),
               "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1", "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1"}
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        # paths go in as arguments ($1, $2), never into the script text
        script = ('echo "$DBUS_SESSION_BUS_ADDRESS" > "$1"; exec kwin_wayland --virtual --no-lockscreen '
                  f'--no-global-shortcuts --width {width} --height {height} --socket "$2"')
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(["dbus-run-session", "--", "sh", "-c", script, "sh", str(bus_file), self.socket],
                                         env=env, stdout=log,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < end:
            if (self.run_dir / self.socket).exists() and bus_file.is_file() and bus_file.read_text().strip():
                self.bus = bus_file.read_text().strip()
                time.sleep(1.0)  # KWin finishes its own setup after creating the socket
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
