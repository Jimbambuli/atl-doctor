# SPDX-License-Identifier: GPL-3.0-or-later
"""Step files (`record --steps FILE`): parse and run them against a driver (ATL or Android).

One command per line or separated by ';', '#' starts a comment outside quotes:
    wait-window "Title" [TIMEOUT]   click "Text" | click id/NAME | click X,Y (dp)   key NAME[+NAME]
    type "text"   scroll N   scroll-smooth DY   wait SECONDS   wait-idle [TIMEOUT]   mark ["name"]   screenshot "name"
    repeat N { ... }   (also over several lines, nested)
A scenario may begin with `start_zustand: NAME`: the app's state saved with `atl-doctor zustand sichern` is loaded
before the app starts.
"""
from __future__ import annotations

import errno
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TextIO

from .errors import DoctorError

if TYPE_CHECKING:
    from .capture import Capture
    from .headless import HeadlessSession

WAIT_IDLE_S = 10.0
WAIT_WINDOW_S = 60.0
TEXT_CHUNK = 400  # bytes of text per line to the input helper (it reads lines of up to 511 bytes)
UIAUTOMATOR_LOCK = threading.Lock()  # two uiautomator dumps at once kill one of them (marks and steps both dump)


@dataclass(frozen=True)
class Step:
    line: int
    cmd: str
    args: tuple[str | int | float, ...]


@dataclass(frozen=True)
class Repeat:
    line: int
    count: int
    body: tuple["Step | Repeat", ...]


class StepError(DoctorError):
    def __init__(self, filename: str, line: int, message: str):
        super().__init__(f"{filename}:{line}: {message}")
        self.line = line


# ------------------------------------------------------------------ parser
@dataclass
class _Tok:
    kind: str  # "word" | "str" | "sep" | "{" | "}"
    value: str
    line: int


def _tokens(text: str, filename: str) -> list[_Tok]:
    out: list[_Tok] = []
    i, line, n = 0, 1, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            out.append(_Tok("sep", "\n", line))
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c == ";":
            out.append(_Tok("sep", ";", line))
            i += 1
        elif c in "{}":
            out.append(_Tok(c, c, line))
            i += 1
        elif c == '"':
            start, i, buf = line, i + 1, []
            while i < n and text[i] not in '"\n':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\':
                    i += 1
                buf.append(text[i])
                i += 1
            if i >= n or text[i] != '"':
                raise StepError(filename, start, "unterminated string")
            out.append(_Tok("str", "".join(buf), start))
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n;{}#"':
                j += 1
            out.append(_Tok("word", text[i:j], line))
            i = j
    return out


_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_XY = re.compile(r"^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")


def _number(tok: _Tok | None) -> float | None:
    return float(tok.value) if tok and tok.kind == "word" and _NUM.match(tok.value) else None


def _command(name: _Tok, args: list[_Tok], filename: str) -> Step:
    cmd, line = name.value, name.line

    def err(msg: str) -> StepError:
        return StepError(filename, line, msg)

    def text_arg(i: int) -> str | None:
        return args[i].value if len(args) > i and args[i].kind in ("str", "word") else None

    def limit(n: int) -> None:
        if len(args) > n:
            raise err(f"{cmd}: too many arguments")

    if cmd == "click":
        limit(2)
        a = args[0] if args else None
        if a and a.kind == "str":
            return Step(line, cmd, ("text", a.value))
        if a and a.value.startswith("id/") and len(a.value) > 3:
            return Step(line, cmd, ("id", a.value))
        m = _XY.match(a.value) if a else None
        o = _XY.match(args[1].value[8:]) if len(args) > 1 and args[1].value.startswith("android=") else None
        if m and (len(args) == 1 or o):  # android=X,Y: the same target where Android lays it out elsewhere
            other = (float(o.group(1)), float(o.group(2))) if o else ()
            return Step(line, cmd, ("xy", float(m.group(1)), float(m.group(2)), *other))
        if len(args) > 1:
            raise err("click: only X,Y takes a second argument (android=X,Y)")
        raise err('click expects "Text", id/NAME or X,Y')
    if cmd == "wait-window":
        limit(2)
        match = text_arg(0)
        t = _number(args[1]) if len(args) > 1 else WAIT_WINDOW_S
        if not match or t is None:
            raise err("wait-window expects a title or package and an optional timeout")
        return Step(line, cmd, (match, t))
    if cmd == "wait-idle":
        limit(1)
        t = _number(args[0]) if args else WAIT_IDLE_S
        if t is None:
            raise err("wait-idle expects an optional timeout in seconds")
        return Step(line, cmd, (t,))
    if cmd == "wait":
        limit(1)
        t = _number(args[0]) if args else None
        if t is None:
            raise err("wait expects a number of seconds")
        return Step(line, cmd, (t,))
    if cmd in ("scroll", "scroll-smooth"):
        limit(1)
        if not args or not re.match(r"^-?\d+$", args[0].value) or args[0].kind != "word":
            raise err(f"{cmd} expects an integer")
        return Step(line, cmd, (int(args[0].value),))
    if cmd in ("key", "type", "screenshot"):
        limit(1)
        v = text_arg(0)
        if not v:
            raise err(f"{cmd} expects {'a key name' if cmd == 'key' else 'a text' if cmd == 'type' else 'a name'}")
        return Step(line, cmd, (v,))
    if cmd == "mark":
        limit(1)
        return Step(line, cmd, (text_arg(0) or "",))
    raise err(f"unknown command '{cmd}'")


_START_STATE = re.compile(r"^\s*start_zustand:\s*(\S+)\s*$")


def _split_start_state(text: str, filename: str) -> tuple[str | None, str]:
    """The name of a leading `start_zustand: NAME` line and the text with that line blanked (line numbers stay)."""
    lines = text.split("\n")
    first = next((i for i, x in enumerate(lines) if x.split("#", 1)[0].strip()), None)
    name = None
    for i, line in enumerate(lines):
        m = _START_STATE.match(line)
        if not m:
            continue
        if i != first:
            raise StepError(filename, i + 1, "start_zustand only once, before the first step")
        name, lines[i] = m.group(1), ""
    return name, "\n".join(lines)


def start_state(text: str, filename: str = "-") -> str | None:
    """Name of the state a scenario starts from (`start_zustand: NAME`), or None."""
    return _split_start_state(text, filename)[0]


def parse_steps(text: str, filename: str) -> list[Step | Repeat]:
    toks = _tokens(_split_start_state(text, filename)[1], filename)
    pos = 0

    def block(opened: _Tok | None) -> list[Step | Repeat]:
        nonlocal pos
        out: list[Step | Repeat] = []
        while pos < len(toks):
            t = toks[pos]
            if t.kind == "sep":
                pos += 1
                continue
            if t.kind == "}":
                if opened is None:
                    raise StepError(filename, t.line, "unexpected '}'")
                pos += 1
                return out
            if t.kind != "word":
                raise StepError(filename, t.line, f"expected a command, got {t.value!r}")
            if t.value == "repeat":
                cnt = toks[pos + 1] if pos + 1 < len(toks) else None
                brace = toks[pos + 2] if pos + 2 < len(toks) else None
                if not (cnt and cnt.kind == "word" and cnt.value.isdigit() and brace and brace.kind == "{"):
                    raise StepError(filename, t.line, "repeat expects a count and '{'")
                pos += 3
                out.append(Repeat(t.line, int(cnt.value), tuple(block(t))))
                continue
            pos += 1
            args = []
            while pos < len(toks) and toks[pos].kind in ("word", "str"):
                args.append(toks[pos])
                pos += 1
            if pos < len(toks) and toks[pos].kind == "{":
                raise StepError(filename, toks[pos].line, "unexpected '{'")
            out.append(_command(t, args, filename))
        if opened is not None:
            raise StepError(filename, opened.line, "missing '}'")
        return out

    return block(None)


# ------------------------------------------------------------------ runner
class Driver(Protocol):
    has_tree: bool

    def click(self, x_dp: float, y_dp: float) -> None: ...
    def locate(self, selector: str) -> tuple[float, float] | None: ...
    def key(self, combo: str) -> None: ...
    def type_text(self, s: str) -> None: ...
    def scroll(self, n: int) -> None: ...
    def scroll_smooth(self, dy: int) -> None: ...
    def screenshot(self, name: str) -> None: ...
    def wait_idle(self, timeout_s: float) -> bool: ...
    def wait_window(self, match: str, timeout_s: float) -> bool: ...  # may set event_us: when the window appeared
    def mark(self, name: str) -> None: ...


def _describe(s: Step) -> str:
    return " ".join([s.cmd] + [repr(a) if isinstance(a, str) and " " in a else str(a) for a in s.args])


PROBED_CMDS = ("scroll", "scroll-smooth", "key")   # steps that can move content: view trees before and after
SETTLE_S = 2.0                    # a probed step is over when the drawing has settled (kinetic scrolling)


class _Probe:
    """Per-step figures from the optional driver methods frame_count() and scroll_tree()."""

    def __init__(self, driver: Driver):
        self.d = driver
        self.frames: int | None = None
        self.tree: list | None = None   # tree after the previous step, valid while nothing else ran in between

    def _frames(self) -> int | None:
        fn = getattr(self.d, "frame_count", None)
        try:
            return fn() if fn else None
        except DoctorError:
            return None

    def _tree(self) -> list | None:
        fn = getattr(self.d, "scroll_tree", None)
        try:
            return fn() if fn else None
        except DoctorError:
            return None

    def _probed(self, s: Step) -> bool:
        return s.cmd in PROBED_CMDS and getattr(self.d, "scroll_tree", None) is not None

    def before(self, s: Step) -> list | None:
        if self.frames is None:
            self.frames = self._frames()
        if self._probed(s):
            return self.tree if self.tree is not None else self._tree()
        return None

    def after(self, s: Step, tree_before: list | None) -> tuple[int | None, tuple[float, float] | None]:
        from .sources.stepstats import scroll_delta

        probed = self._probed(s)
        if probed:
            try:
                self.d.wait_idle(SETTLE_S)
            except DoctorError:
                pass
        f1 = self._frames()
        frames = None
        if f1 is not None and self.frames is not None:
            frames = f1 - self.frames if f1 >= self.frames else f1   # a counter that restarted (new phase) counts from 0
        self.frames = f1
        scroll = None
        self.tree = None
        if probed:
            self.tree = self._tree()
            if tree_before is not None and self.tree is not None:
                scroll = scroll_delta(tree_before, self.tree)
        return frames, scroll


def run_steps(steps: list[Step | Repeat], driver: Driver, filename: str, log: TextIO,
              data: TextIO | None = None, _state: dict | None = None) -> None:
    """Run the steps in order; one line per step in log; failures raise StepError with the line number.

    data (steps.tsv, see sources/stepstats.py) gets one row per finished step with its times and, if the driver offers
    frame_count()/scroll_tree(), the frames drawn and the distance scrolled."""
    from .capture import now_us

    if _state is None:
        _state = {"n": 0, "probe": _Probe(driver)}
    probe: _Probe = _state["probe"]
    for s in steps:
        if isinstance(s, Repeat):
            for _ in range(s.count):
                run_steps(list(s.body), driver, filename, log, data, _state)
            continue
        log.write(f"{time.strftime('%H:%M:%S')} {filename}:{s.line}: {_describe(s)}\n")
        log.flush()
        _state["n"] += 1
        t0 = now_us()[0]
        tree = probe.before(s) if data is not None else None
        try:
            _run_one(s, driver, filename)
        except StepError:
            raise
        except DoctorError as e:  # a driver failure (input helper, adb ...) belongs to this line
            raise StepError(filename, s.line, str(e)) from e
        if data is not None:
            frames, scroll = probe.after(s, tree)
            t1 = now_us()[0]
            if s.cmd == "wait-window" and getattr(driver, "event_us", None) is not None:
                t1 = max(t0, driver.event_us)  # the window's time, not the time the query returned
            desc = re.sub(r"[\t\r\n]", " ", _describe(s))
            data.write("\t".join([str(_state["n"]), str(s.line), s.cmd, desc, str(t0), str(t1),
                                  "" if frames is None else str(frames),
                                  *(("", "") if scroll is None else (str(scroll[0]), str(scroll[1])))]) + "\n")
            data.flush()


def _fmt_s(t: float) -> str:
    return f"{t:g}"


def _run_one(s: Step, d: Driver, filename: str) -> None:
    def fail(msg: str) -> StepError:
        return StepError(filename, s.line, msg)

    a = s.args
    if s.cmd == "click":
        if a[0] == "xy":
            k = 3 if len(a) > 3 and isinstance(d, AndroidDriver) else 1
            d.click(float(a[k]), float(a[k + 1]))
            return
        if not d.has_tree:
            raise fail(f"click by {'text' if a[0] == 'text' else 'id'} needs a view tree (ATLViewDump/uiautomator)")
        pos = d.locate(str(a[1]))
        if pos is None:
            raise fail(f"no visible view matches {a[1] if a[0] == 'id' else repr(a[1])}")
        d.click(*pos)
    elif s.cmd == "key":
        d.key(str(a[0]))
    elif s.cmd == "type":
        d.type_text(str(a[0]))
    elif s.cmd == "scroll":
        d.scroll(int(a[0]))
    elif s.cmd == "scroll-smooth":
        d.scroll_smooth(int(a[0]))
    elif s.cmd == "wait":
        time.sleep(float(a[0]))
    elif s.cmd == "wait-idle":
        if not d.wait_idle(float(a[0])):
            raise fail(f"not idle within {_fmt_s(float(a[0]))} s")
    elif s.cmd == "wait-window":
        if not d.wait_window(str(a[0]), float(a[1])):
            raise fail(f"no window matching '{a[0]}' within {_fmt_s(float(a[1]))} s")
    elif s.cmd == "mark":
        d.mark(str(a[0]))
    elif s.cmd == "screenshot":
        d.screenshot(str(a[0]))


# ------------------------------------------------------------------ drivers
IDLE_QUIET_S = 0.6        # no new frame for this long = idle
IDLE_FALLBACK_S = 2.0     # without frame times: fixed pause
INPUT_SYNC_S = 15.0
WINDOW_POLL_S = 0.02      # wait-window: how often the watch script's events are read
WINDOW_QUERY_S = 2.0      # wait-window: how often the windows are asked for as well


class AtlDriver:
    """ATL in a headless KWin: input through atl-doctor-input, targets from ATLViewDump trees.

    dp coordinates are relative to android:id/content of the activity window, as in the trees; the
    content origin comes from the latest tree (without one: the window frame's top left corner).
    """

    def __init__(self, cap: "Capture", session: "HeadlessSession", input_fifo: Path, input_status: Path,
                 has_tree: bool, frames: bool, package: str | None = None):
        self.cap, self.session, self.has_tree, self.frames, self.package = cap, session, has_tree, frames, package
        self.input_fifo, self.input_status = Path(input_fifo), Path(input_status)
        self.content = (0.0, 0.0)  # content origin in window pixels
        self._fd: int | None = None
        self._ping = 0
        self._status_pos = 0
        self._n_tree = 0
        self._warned_idle = False
        self.event_us: int | None = None  # when the window of the last wait-window appeared (CLOCK_BOOTTIME)

    # --- input helper
    def _send(self, line: str) -> None:
        if self._fd is None:
            end = time.monotonic() + 5
            while True:
                try:
                    self._fd = os.open(self.input_fifo, os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError as e:
                    if e.errno not in (errno.ENXIO, errno.ENOENT) or time.monotonic() > end:
                        raise DoctorError("input helper is not reading its FIFO") from e
                    time.sleep(0.1)
            os.set_blocking(self._fd, True)
        os.write(self._fd, (line.replace("\n", " ") + "\n").encode("utf-8"))
        self._sync()

    def _sync(self) -> None:
        """ping/pong: wait until the helper processed everything; its ERROR lines become exceptions."""
        self._ping += 1
        os.write(self._fd, f"ping {self._ping}\n".encode())
        end = time.monotonic() + INPUT_SYNC_S
        while time.monotonic() < end:
            try:
                with open(self.input_status, "rb") as f:
                    f.seek(self._status_pos)
                    new = f.read().decode("utf-8", "replace")
            except OSError:
                new = ""
            if f"pong {self._ping}\n" in new:
                self._status_pos += len(new.encode("utf-8"))
                errors = [ln for ln in new.splitlines() if ln.startswith("ERROR")]
                if errors:
                    raise DoctorError("input helper: " + errors[0][6:])
                return
            time.sleep(0.05)
        raise DoctorError("input helper does not answer")

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.write(self._fd, b"quit\n")
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

    # --- windows
    def _main_window(self) -> dict | None:
        ws = self.session.windows()
        return next((w for w in ws if w["kind"] == "normal"), ws[0] if ws else None)

    def _origin(self) -> tuple[float, float]:
        """Screen position of dp 0,0: surface origin + content origin of the latest tree, without a tree
        the window frame's top left corner."""
        w = self._main_window()
        if w is None:
            return self.content
        if self.content == (0.0, 0.0):
            return w["rect"][0], w["rect"][1]
        return w["surface"][0] + self.content[0], w["surface"][1] + self.content[1]

    def click(self, x_dp: float, y_dp: float) -> None:
        ox, oy = self._origin()
        self._send(f"click {round(ox + x_dp)} {round(oy + y_dp)}")

    def locate(self, selector: str) -> tuple[float, float] | None:
        from .capture import TREES
        from .sources.tree import find_node, load_tree, request_atl_tree

        self._n_tree += 1
        path = request_atl_tree(self.cap.path(TREES), f"step-{self._n_tree:03d}")
        if path is None:
            raise DoctorError("ATLViewDump did not answer")
        nodes = load_tree(path, "atl")
        content = next((n for n in nodes if n.full_id == "android:id/content" and n.window.startswith("activity")), None)
        if content and content.gtk_rect:
            self.content = (content.gtk_rect[0], content.gtk_rect[1])
        n = find_node(nodes, selector)
        if n is None or n.gtk_rect is None:
            return None
        cx, cy = (n.gtk_rect[0] + n.gtk_rect[2]) / 2, (n.gtk_rect[1] + n.gtk_rect[3]) / 2
        if not n.window.startswith("activity"):  # dialog or popup: its own window, the topmost one
            ws = self.session.windows()
            main = self._main_window()
            top = ws[-1] if ws else None
            if top and main and top is not main:
                cx += top["surface"][0] - main["surface"][0]
                cy += top["surface"][1] - main["surface"][1]
        return cx - self.content[0], cy - self.content[1]

    def scroll_tree(self) -> list | None:
        """Nodes of a fresh view tree for the scroll distance of a step; None without ATLViewDump."""
        from .capture import TREES
        from .sources.tree import load_tree, request_atl_tree

        if not self.has_tree:
            return None
        self._n_tree += 1
        path = request_atl_tree(self.cap.path(TREES), f"scroll-{self._n_tree:03d}")
        return load_tree(path, "atl") if path else None

    def key(self, combo: str) -> None:
        self._send(f"key {combo}")

    def type_text(self, s: str) -> None:
        # the helper reads lines of up to 511 bytes and decodes UTF-8 of up to 3 bytes (no emoji)
        wide = next((c for c in s if ord(c) > 0xFFFF), None)
        if wide:
            raise DoctorError(f"type: character U+{ord(wide):04X} cannot be typed (outside the BMP)")
        chunk = ""
        for c in s:
            if len((chunk + c).encode()) > TEXT_CHUNK:
                self._send(f"text {chunk}")
                chunk = ""
            chunk += c
        if chunk:
            self._send(f"text {chunk}")

    def _window_centre(self) -> tuple[int, int]:
        w = self._main_window()
        x, y, ww, hh = w["rect"] if w else (0.0, 0.0, 0.0, 0.0)
        return round(x + ww / 2), round(y + hh / 2)

    def scroll(self, n: int) -> None:
        """N real wheel notches (the helper marks them; the preload shim in the app makes them source wheel)."""
        self._send("wheel %d %d %d" % (*self._window_centre(), n))

    def scroll_smooth(self, dy: int) -> None:
        """Touchpad-like smooth scrolling by DY surface units (what `scroll` sent before wheel notches existed)."""
        self._send("wheel-smooth %d %d %d" % (*self._window_centre(), dy))

    def screenshot(self, name: str) -> None:
        from .capture import SHOTS
        from .sources.shots import take_atl_shot

        if not take_atl_shot(self.session, self.cap.path(SHOTS) / f"{_safe(name)}.png"):
            raise DoctorError("screenshot failed (spectacle)")

    def wait_idle(self, timeout_s: float) -> bool:
        from .capture import AGENT

        if not self.frames:
            if not self._warned_idle:
                from .cli import log
                log(0, f"wait-idle: no frame times (agent frames probe), waiting {IDLE_FALLBACK_S:g} s instead")
                self._warned_idle = True
            time.sleep(min(IDLE_FALLBACK_S, timeout_s))
            return True
        end = time.monotonic() + timeout_s
        last, since = -1, time.monotonic()
        while time.monotonic() < end:
            try:
                n = self.cap.path(AGENT).read_bytes().count(b"\nP\t")
            except OSError:
                n = 0
            now = time.monotonic()
            if n != last:
                last, since = n, now
            elif now - since >= IDLE_QUIET_S:
                return True
            time.sleep(0.1)
        return False

    def _is_match(self, match: str, w: dict) -> bool:
        return match == self.package or match in w["caption"] or match in w["resource_class"]

    def wait_window(self, match: str, timeout_s: float) -> bool:
        """Wait for a window; event_us is the time it appeared (the KWin watch script's stamp), not the time
        of the query that found it. Without a watch the windows are polled and event_us stays None."""
        from .capture import now_us

        self.event_us = None
        began_us = now_us()[0]          # events from before this step are history: that window may be gone
        end = time.monotonic() + timeout_s
        first = True
        next_query = time.monotonic() + WINDOW_QUERY_S
        while time.monotonic() < end:
            events = self.session.window_events()
            hit = next((e for e in events or [] if e["t_us"] >= began_us and self._is_match(match, e)), None)
            if hit is not None:
                self.event_us = hit["t_us"]
            elif first or events is None or time.monotonic() >= next_query:
                # a window already there (no event to come), no watch, or a watch that saw nothing for a while
                next_query = time.monotonic() + WINDOW_QUERY_S
                hit = next((w for w in self.session.windows() if self._is_match(match, w)), None)
            first = False
            if hit is not None:
                self.session.place_window_at_origin()
                return True
            time.sleep(WINDOW_POLL_S)
        return False

    def mark(self, name: str) -> None:
        from .record_atl import send_mark

        send_mark(self.cap.root, name or None)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "shot"


# xkb key names -> Android key codes (Escape is Back, as ATL maps it)
ANDROID_KEYS = {"Escape": "KEYCODE_BACK", "Return": "KEYCODE_ENTER", "KP_Enter": "KEYCODE_ENTER",
                "BackSpace": "KEYCODE_DEL", "Delete": "KEYCODE_FORWARD_DEL", "Tab": "KEYCODE_TAB",
                "space": "KEYCODE_SPACE", "Up": "KEYCODE_DPAD_UP", "Down": "KEYCODE_DPAD_DOWN",
                "Left": "KEYCODE_DPAD_LEFT", "Right": "KEYCODE_DPAD_RIGHT", "Home": "KEYCODE_MOVE_HOME",
                "End": "KEYCODE_MOVE_END", "Page_Up": "KEYCODE_PAGE_UP", "Page_Down": "KEYCODE_PAGE_DOWN",
                "ctrl": "KEYCODE_CTRL_LEFT", "shift": "KEYCODE_SHIFT_LEFT", "alt": "KEYCODE_ALT_LEFT",
                "super": "KEYCODE_META_LEFT", "Menu": "KEYCODE_MENU"}
SCROLL_STEP_DP = 64.0     # one wheel notch as a swipe distance


def android_keycode(name: str) -> str:
    if name in ANDROID_KEYS:
        return ANDROID_KEYS[name]
    if re.fullmatch(r"[A-Za-z0-9]", name):
        return "KEYCODE_" + name.upper()
    if re.fullmatch(r"F\d{1,2}", name):
        return "KEYCODE_" + name
    raise DoctorError(f"key '{name}' has no Android key code")


class AndroidDriver:
    """The same steps on a device: `adb shell input`, targets from uiautomator, idle from gfxinfo."""

    def __init__(self, serial: str, cap: "Capture", package: str, density: float):
        self.serial, self.cap, self.package, self.density = serial, cap, package, density
        self.has_tree = True
        self.content = (0.0, 0.0)  # content origin in device pixels
        self._n_tree = 0

    def _adb(self, *args: str, timeout: float = 30) -> str:
        try:
            r = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DoctorError(f"adb {' '.join(args)} timed out") from e
        if r.returncode != 0:
            raise DoctorError(f"adb {' '.join(args[:3])} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
        return r.stdout.decode("utf-8", "replace")

    def _px(self, x_dp: float, y_dp: float) -> tuple[int, int]:
        return round(self.content[0] + x_dp * self.density), round(self.content[1] + y_dp * self.density)

    def click(self, x_dp: float, y_dp: float) -> None:
        self._adb("shell", "input", "tap", *map(str, self._px(x_dp, y_dp)))

    def locate(self, selector: str) -> tuple[float, float] | None:
        from .capture import TREES
        from .sources.tree import _rect, find_node, load_tree

        self._n_tree += 1
        out = self.cap.path(TREES) / f"step-{self._n_tree:03d}.xml"
        with UIAUTOMATOR_LOCK:
            self._adb("shell", "uiautomator", "dump", "/sdcard/atl-doctor.xml")
            self._adb("pull", "/sdcard/atl-doctor.xml", str(out))
        import xml.etree.ElementTree as ET
        root = ET.parse(out).getroot()
        content = next((e for e in root.iter("node") if e.get("resource-id") == "android:id/content"), None)
        if content is not None:
            r = _rect(content.get("bounds", "[0,0][0,0]"))
            self.content = (r[0], r[1])
        n = find_node(load_tree(out, "android", self.density), selector)
        if n is None:
            return None
        return (n.rect[0] + n.rect[2]) / 2, (n.rect[1] + n.rect[3]) / 2

    def scroll_tree(self) -> list | None:
        from .capture import TREES
        from .sources.tree import load_tree

        self._n_tree += 1
        out = self.cap.path(TREES) / f"scroll-{self._n_tree:03d}.xml"
        with UIAUTOMATOR_LOCK:
            self._adb("shell", "uiautomator", "dump", "/sdcard/atl-doctor.xml")
            self._adb("pull", "/sdcard/atl-doctor.xml", str(out))
        return load_tree(out, "android", self.density)

    def frame_count(self) -> int | None:
        """Frames the app has drawn since the last gfxinfo reset (marks reset it)."""
        return self._frames()

    def key(self, combo: str) -> None:
        codes = [android_keycode(k) for k in combo.split("+")]
        if len(codes) == 1:
            self._adb("shell", "input", "keyevent", codes[0])
        else:
            self._adb("shell", "input", "keycombination", *codes)

    def type_text(self, s: str) -> None:
        self._adb("shell", "input", "text", shlex.quote(s.replace(" ", "%s")))

    def _swipe(self, dp: float) -> None:
        """One vertical gesture of DP (positive = content moves up), at most 80 % of the screen height."""
        size = re.search(r"(\d+)x(\d+)", self._adb("shell", "wm", "size"))
        w, h = (int(size.group(1)), int(size.group(2))) if size else (1080, 1920)
        dist = max(-h * 0.4, min(h * 0.4, dp * self.density))
        x, y = w // 2, h // 2
        self._adb("shell", "input", "swipe", str(x), str(round(y + dist / 2)), str(x), str(round(y - dist / 2)), "300")

    def scroll(self, n: int) -> None:
        """One gesture of SCROLL_STEP_DP per notch (adb has no wheel source; each notch stays a separate event)."""
        for _ in range(abs(n)):
            self._swipe(SCROLL_STEP_DP if n > 0 else -SCROLL_STEP_DP)

    def scroll_smooth(self, dy: int) -> None:
        self._swipe(float(dy))

    def screenshot(self, name: str) -> None:
        from .capture import SHOTS

        r = subprocess.run(["adb", "-s", self.serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=30)
        if r.returncode != 0 or not r.stdout:
            raise DoctorError("screenshot failed (screencap)")
        self.cap.path(SHOTS).mkdir(exist_ok=True)
        (self.cap.path(SHOTS) / f"{_safe(name)}.png").write_bytes(r.stdout)

    def _frames(self) -> int | None:
        m = re.search(r"Total frames rendered:\s*(\d+)", self._adb("shell", "dumpsys", "gfxinfo", self.package))
        return int(m.group(1)) if m else None

    def wait_idle(self, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        last, since = None, time.monotonic()
        while time.monotonic() < end:
            n = self._frames()
            now = time.monotonic()
            if n != last:
                last, since = n, now
            elif now - since >= IDLE_QUIET_S:
                return True
            time.sleep(0.2)
        return False

    def wait_window(self, match: str, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            focus = re.search(r"mCurrentFocus=(.*)", self._adb("shell", "dumpsys", "window"))
            if focus and match in focus.group(1):
                return True
            time.sleep(0.5)
        return False

    def mark(self, name: str) -> None:
        from .record_atl import send_mark

        send_mark(self.cap.root, name or None)
