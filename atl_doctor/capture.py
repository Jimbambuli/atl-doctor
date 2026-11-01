# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture directory: versioned manifest.json plus raw data; phase marks."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .errors import DoctorError

CAPTURE_FORMAT = 1
MANIFEST = "manifest.json"
ATL_LOG = "atl.log"
AGENT = "agent.tsv"
STUB_HITS = "stubhits.tsv"  # stub calls counted by ATL itself (ATL_STUB_HITS)
MARKS = "marks.tsv"
TRACE_DIR = "trace"
TREES = "trees"
SHOTS = "shots"
MEMORY = "memory.tsv"
CONTROL = "control"
STEPS_LOG = "steps.log"
STEPS_DATA = "steps.tsv"


@dataclass
class Mark:
    index: int
    boot_us: int
    wall_us: int
    name: str


@dataclass
class Manifest:
    format: int = CAPTURE_FORMAT
    tool_version: str = __version__
    side: str = "atl"
    apk: str = ""
    package: str | None = None
    version_name: str | None = None
    sdk_int: int | None = None
    command: list[str] = field(default_factory=list)
    started: str = ""
    ended: str | None = None
    exit_status: int | None = None
    signal: str | None = None
    probes: list[dict] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)


def now_us() -> tuple[int, int]:
    """(CLOCK_BOOTTIME, epoch) in microseconds"""
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1000, time.time_ns() // 1000


def clean_mark_text(text: str | None, index: int) -> str:
    cleaned = re.sub(r"[\t\r\n]", " ", text or "").strip()
    return cleaned or f"mark {index}"


def phase_of(t_us: int, marks: list[Mark], clock: str = "boot") -> Mark:
    """The mark a time falls into: the last mark at or before it, else the first."""
    if not marks:
        return Mark(0, 0, 0, "start")
    key = (lambda m: m.boot_us) if clock == "boot" else (lambda m: m.wall_us)
    best = marks[0]
    for m in marks:
        if key(m) <= t_us:
            best = m
    return best


class Capture:
    def __init__(self, path: Path):
        self.root = Path(path)

    def path(self, name: str) -> Path:
        return self.root / name

    @classmethod
    def create(cls, path: Path, side: str) -> "Capture":
        root = Path(path)
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise DoctorError(f"{root} exists and is not an empty directory")
        root.mkdir(parents=True, exist_ok=True)
        c = cls(root)
        c.write_manifest(Manifest(side=side, started=datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.append_mark("start")
        return c

    @classmethod
    def open(cls, path: Path) -> "Capture":
        c = cls(Path(path))
        if not c.path(MANIFEST).is_file():
            raise DoctorError(f"not a capture directory: {path}")
        c.read_manifest()  # validates format
        return c

    def write_manifest(self, m: Manifest) -> None:
        tmp = self.path(MANIFEST + ".tmp")
        tmp.write_text(json.dumps(asdict(m), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path(MANIFEST))

    def read_manifest(self) -> Manifest:
        try:
            d = json.loads(self.path(MANIFEST).read_text(encoding="utf-8"))
            fmt = d["format"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise DoctorError(f"not a capture directory: {self.root} (unreadable manifest)") from e
        if not isinstance(fmt, int) or fmt > CAPTURE_FORMAT:
            raise DoctorError(f"capture format {fmt} is newer than supported ({CAPTURE_FORMAT}); update atl-doctor")
        known = {f.name for f in fields(Manifest)}
        return Manifest(**{k: v for k, v in d.items() if k in known})

    def append_mark(self, name: str | None, boot_us: int | None = None) -> Mark:
        """Next phase mark; boot_us overrides the local CLOCK_BOOTTIME (Android: the device's clock)."""
        index = len(self.read_marks())
        boot, wall = now_us()
        boot = boot if boot_us is None else boot_us
        m = Mark(index, boot, wall, clean_mark_text(name, index))
        with open(self.path(MARKS), "a", encoding="utf-8") as f:
            f.write(f"{m.index}\t{m.boot_us}\t{m.wall_us}\t{m.name}\n")
        return m

    def read_marks(self) -> list[Mark]:
        try:
            text = self.path(MARKS).read_bytes().decode("utf-8", "replace")
        except OSError:
            return []
        lines = text.split("\n")
        lines.pop()  # last element is "" or a partial line without newline
        out = []
        for ln in lines:
            f = ln.split("\t", 3)
            if len(f) == 4 and all(x.lstrip("-").isdigit() for x in f[:3]):
                out.append(Mark(int(f[0]), int(f[1]), int(f[2]), f[3]))
        return out
