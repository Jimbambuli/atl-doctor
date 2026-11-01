# SPDX-License-Identifier: GPL-3.0-or-later
"""Time line of a recording: per phase the methods that kept a thread busy (self time from atl-doctor-tracecount's
OUTPREFIX.time, normally from ART's sampling profiler, see the agent's sample=). Waiting (Object.wait, sleep, park,
the looper's poll) is left out: it is where idle threads spend their time."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..capture import Mark

PREFIX = "tr"
TOP = 8  # methods per phase
WAITING = {"java.lang.Object.wait", "java.lang.Thread.sleep", "sun.misc.Unsafe.park", "jdk.internal.misc.Unsafe.park",
           "android.os.MessageQueue.nativePollOnce", "java.lang.Thread.parkFor$", "java.lang.VMThread.sleep",
           "dalvik.system.VMStack.getThreadStackTrace", "java.lang.Thread.join"}


def _rows(path: Path) -> list[list[str]]:
    try:
        return [ln.split("\t") for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln]
    except OSError:
        return []


def read_timeline(trace_dir: Path, marks: list[Mark], top: int = TOP) -> list[dict]:
    """[{"phase": name, "top": [{"thread", "method", "ms"}...]}...] in phase order; [] without a time file."""
    methods = {}
    for f in _rows(trace_dir / f"{PREFIX}.methods"):
        if len(f) >= 3:
            try:
                methods[int(f[0], 16)] = f"{f[1]}.{f[2]}"
            except ValueError:
                pass
    threads = {f[0]: f[1] for f in _rows(trace_dir / f"{PREFIX}.threads") if len(f) >= 2}
    per: dict[int, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    for f in _rows(trace_dir / f"{PREFIX}.time"):
        try:
            phase, mid, us = int(f[0]), int(f[2], 16), int(f[3])
        except (ValueError, IndexError):
            continue
        name = methods.get(mid)
        if name is None or name in WAITING:
            continue
        per[phase][(threads.get(f[1], f"thread {f[1]}"), name)] += us
    names = {m.index: m.name for m in marks}
    out = []
    for phase in sorted(per):
        ranked = sorted(per[phase].items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        out.append({"phase": names.get(phase, f"mark {phase}"),
                    "top": [{"thread": t, "method": m, "ms": round(us / 1000, 1)} for (t, m), us in ranked]})
    return out


def timeline_lines(timeline: list[dict]) -> list[str]:
    """Text form: the phase, then "ms thread method" per busy method."""
    out = []
    for p in timeline:
        out.append(p["phase"])
        out += [f"  {e['ms']:>8.1f} ms  {e['thread']}  {e['method']}" for e in p["top"]]
    return out
