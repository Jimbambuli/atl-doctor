# SPDX-License-Identifier: GPL-3.0-or-later
"""Record the input on an Android device or emulator into input.jsonl (see bedienung.py): `getevent -lt` for touches
and keys, `uiautomator dump` in a loop for the targets.

A touch is resolved against the last dump that was complete before the finger went down (the screen as the user saw
it); the dump loop runs for as long as the recording, which costs the app some speed. Text typed on a soft keyboard
is not visible to getevent: only keys of a hardware keyboard become key events, no text events.
"""
from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .bedienung import INPUT_FILE, make_event, make_header, target_at, write_input
from .capture import TREES, Capture, now_us
from .errors import DoctorError

SLOP_DP = 12.0       # a finger that moved further than this scrolls
LONG_S = 0.5         # a finger that stayed down this long long-taps
DUMPS_KEPT = 4
TEXT_GAP_S = 2.0      # typed characters this close together are one text event

_EVENT = re.compile(r"^\[\s*(\d+\.\d+)\]\s+(/dev/input/event\d+):\s+(\w+)\s+(\w+)\s+(\w+)\s*$")
_DEVICE = re.compile(r"^add device \d+: (/dev/input/event\d+)")
_RANGE = re.compile(r"(ABS_MT_POSITION_[XY])\s*:.*?max\s+(\d+)")
_FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^ }/]+)/([^ }]+)")

KEY_NAMES = {"KEY_BACK": "back", "KEY_ENTER": "Return", "KEY_KPENTER": "KP_Enter", "KEY_BACKSPACE": "BackSpace",
             "KEY_DELETE": "Delete", "KEY_TAB": "Tab", "KEY_ESC": "Escape", "KEY_SPACE": "space", "KEY_UP": "Up",
             "KEY_DOWN": "Down", "KEY_LEFT": "Left", "KEY_RIGHT": "Right", "KEY_HOME": "Home", "KEY_END": "End",
             "KEY_PAGEUP": "Page_Up", "KEY_PAGEDOWN": "Page_Down", "KEY_MENU": "Menu"}


def touch_ranges(getevent_p: str) -> tuple[int, int] | None:
    """(max X, max Y) of the first device in `getevent -pl` that reports multi-touch positions."""
    dev: dict[str, int] = {}
    for ln in getevent_p.splitlines() + ["add device 0: /dev/input/event999"]:
        if _DEVICE.match(ln):
            if "ABS_MT_POSITION_X" in dev and "ABS_MT_POSITION_Y" in dev:
                return dev["ABS_MT_POSITION_X"], dev["ABS_MT_POSITION_Y"]
            dev = {}
            continue
        m = _RANGE.search(ln)
        if m:
            dev[m.group(1)] = int(m.group(2))
    return None


@dataclass
class Tracker:
    """Turns `getevent -lt` lines into gestures: {"kind": "tap"|"long-tap"|"scroll", ...} and key presses.

    Positions are in device pixels (raw / range * display size); only the first finger counts."""
    max_x: int
    max_y: int
    width: int
    height: int
    density: float
    _x: int | None = None    # unknown until the device has reported the axis: it reports only values that changed, so
    _y: int | None = None    # a first touch at the old position of an earlier touch lacks that axis
    _down: dict | None = None
    _pending_down: bool = False
    _down_host: int = 0
    _down_t: float = 0.0
    out: list[dict] = field(default_factory=list)

    def _px(self) -> tuple[float, float] | None:
        if self._x is None or self._y is None:
            return None
        return self._x * self.width / max(1, self.max_x), self._y * self.height / max(1, self.max_y)

    def feed(self, line: str, host_us: int) -> None:
        m = _EVENT.match(line.strip())
        if not m:
            return
        t, _dev, typ, code, val = float(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
        if typ == "EV_ABS":
            if code == "ABS_MT_POSITION_X":
                self._x = int(val, 16)
            elif code == "ABS_MT_POSITION_Y":
                self._y = int(val, 16)
            elif code == "ABS_MT_TRACKING_ID":
                if val.lower() == "ffffffff":
                    self._finish(t)
                elif self._down is None:
                    self._pending_down = True
                    self._down_host = host_us
                    self._down_t = t
        elif typ == "EV_SYN" and code == "SYN_REPORT":
            if self._pending_down and self._down is None:   # the position of the first report after the finger came down
                p = self._px()
                self._down = {"x0": p[0] if p else None, "y0": p[1] if p else None, "t0": self._down_t,
                              "host_us": self._down_host}
                self._pending_down = False
        elif typ == "EV_KEY" and code.startswith("KEY_") and val == "DOWN":
            name = KEY_NAMES.get(code)
            if re.fullmatch(r"KEY_[A-Z0-9]", code) or code == "KEY_SPACE":
                # a typed character: the watcher joins them into text events (never into key events with letters)
                self.out.append({"kind": "char", "ch": " " if code == "KEY_SPACE" else code[4:].lower(), "host_us": host_us})
            elif name:
                self.out.append({"kind": "back" if name == "back" else "key", "key": name, "host_us": host_us})

    def _finish(self, t: float) -> None:
        d, self._down, self._pending_down = self._down, None, False
        if d is None:
            return
        p = self._px()
        x1, y1 = p if p else (None, None)
        known = d["x0"] is not None and x1 is not None
        dist_dp = math.hypot(x1 - d["x0"], y1 - d["y0"]) / self.density if known else 0.0
        if dist_dp > SLOP_DP:
            kind = "scroll"
        else:
            kind = "long-tap" if t - d["t0"] >= LONG_S else "tap"
        self.out.append({"kind": kind, "x0": d["x0"], "y0": d["y0"], "x1": x1, "y1": y1, "host_us": d["host_us"]})

    def take(self) -> list[dict]:
        out, self.out = self.out, []
        return out


class Watcher:
    """getevent and the dump loop in threads; stop() returns the events and writes nothing."""

    def __init__(self, serial: str, cap: Capture, package: str, density: float, adb_run, full: bool = False):
        self.serial, self.cap, self.package, self.density, self.full = serial, cap, package, density, full
        self.adb = adb_run   # callable(*args) -> str, raises DoctorError
        self.events: list[dict] = []
        self._dumps: list[tuple[int, str]] = []   # (host boot us when complete, xml)
        self._activity = ""
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._proc: subprocess.Popen | None = None
        self._n = 0
        self._chars: list[tuple[int, str]] = []   # [(time of the first character, ""), (time, char) ...]
        self._chars_act = ""
        self.offset_us = 0

    def start(self, boot_us_device: int | None = None) -> None:
        size = re.search(r"(\d+)x(\d+)", self.adb("shell", "wm", "size"))
        w, h = (int(size.group(1)), int(size.group(2))) if size else (1080, 1920)
        rng = touch_ranges(self.adb("shell", "getevent", "-pl"))
        if rng is None:
            raise DoctorError("no touch screen found in `getevent -pl`: cannot record the input")
        self.tracker = Tracker(rng[0], rng[1], w, h, self.density)
        self.offset_us = 0 if boot_us_device is None else boot_us_device - now_us()[0]
        self._proc = subprocess.Popen(["adb", "-s", self.serial, "shell", "getevent", "-lt"], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
        for fn in (self._read, self._dump_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def _dump_loop(self) -> None:
        from .steps import UIAUTOMATOR_LOCK

        while not self._stop.is_set():
            try:
                with UIAUTOMATOR_LOCK:
                    self.adb("shell", "uiautomator", "dump", "/sdcard/atl-doctor-input.xml")
                    xml = self.adb("shell", "cat", "/sdcard/atl-doctor-input.xml")
                done = now_us()[0]
                self._dumps = (self._dumps + [(done, xml)])[-DUMPS_KEPT:]
                m = _FOCUS.search(self.adb("shell", "dumpsys", "window"))
                if m:
                    self._activity = m.group(2)
            except DoctorError:
                time.sleep(0.5)

    def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            self.tracker.feed(raw.decode("utf-8", "replace"), now_us()[0])
            for g in self.tracker.take():
                self._resolve(g)

    def _tree_before(self, host_us: int) -> str | None:
        older = [x for t, x in self._dumps if t <= host_us]
        return older[-1] if older else None

    def _flush_text(self) -> None:
        if self._chars:
            t_us, text = self._chars[0][0], "".join(c for _, c in self._chars[1:])
            self.events.append(make_event("text", t_us + self.offset_us, self._chars_act, text=text, full=self.full))
            self._chars = []

    def _resolve(self, g: dict) -> None:
        t_us = g["host_us"] + self.offset_us
        act = self._activity
        if g["kind"] == "char":
            if self._chars and g["host_us"] - self._chars[-1][0] > TEXT_GAP_S * 1_000_000:
                self._flush_text()
            if not self._chars:
                self._chars, self._chars_act = [(g["host_us"], "")], act
            self._chars.append((g["host_us"], g["ch"]))
            return
        self._flush_text()
        if g["kind"] in ("key", "back"):
            self.events.append(make_event(g["kind"], t_us, act, key=g["key"] if g["kind"] == "key" else None))
            return
        if g["kind"] == "scroll" and g["x1"] is not None:
            dx, dy = (g["x0"] - g["x1"]) / self.density, (g["y0"] - g["y1"]) / self.density
            self.events.append(make_event("scroll", t_us, act, scroll=(dx, dy)))
            return
        xml = self._tree_before(g["host_us"]) if g["x0"] is not None else None
        target, point = None, None
        if xml:
            from .sources.tree import content_origin, load_tree

            self._n += 1
            self.cap.path(TREES).mkdir(exist_ok=True)
            p = self.cap.path(TREES) / f"input-{self._n:03d}.xml"
            p.write_text(xml, encoding="utf-8")
            try:
                ox, oy = content_origin(p, "android")
                point = ((g["x0"] - ox) / self.density, (g["y0"] - oy) / self.density)
                target = target_at(load_tree(p, "android", self.density), point[0], point[1], self.full)
            except DoctorError:
                pass
        self.events.append(make_event(g["kind"], t_us, act, target, point))

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for t in self._threads:
            t.join(10)
        self._flush_text()
        return sorted(self.events, key=lambda e: e["t_us"])

    def write(self, side: str = "android") -> int:
        ev = self.stop()
        write_input(self.cap.path(INPUT_FILE), make_header(side, self.package, "getevent", self.full, self.density), ev)
        return len(ev)
