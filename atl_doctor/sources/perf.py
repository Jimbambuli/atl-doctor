# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame times, main-thread hangs and GC pauses per phase (agent lines P/H/G, Android `dumpsys gfxinfo`).

A frame time is the duration of one frame: under ATL from the start of the GDK frame clock paint cycle (before-paint) to its
after-paint signal, under Android from IntendedVsync to FrameCompleted - the same span, so both sides
compare. Android values are only shown for comparison; findings come from ATL.
"""
from __future__ import annotations

import math
from collections import defaultdict

from ..capture import Mark, phase_of
from ..findings import Finding, Klass, Occurrence
from .exceptions import AgentEvent
from .log import is_platform_frame

JANK_MS = 16.7
HANG_MS = 100
IDLE_GAP_MS = 1000     # a "frame" this long is a hang or idle time, not a frame
JANK_MIN_FRAMES = 30   # fewer frames in a phase say too little: no jank finding (option --jank-min-frames)
JANK_MIN_PCT = 10.0    # a phase with at least this share of slow frames is a finding
JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "libcore.", "dalvik.", "kotlin.", "kotlinx.")
PROBE_FRAMES = ("android.atl.ATLViewDump.",)  # atl-doctor's own view tree dump on the main thread: not the app
SUGGEST_JANK = "Compare the phase with Android (report DIR ANDROID-DIR) and look at what the main thread draws."
SUGGEST_HANG = "The main thread waits or works here instead of drawing; move the work off the main thread or find why it blocks."


def _rank(values: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(values)
    return s[max(0, math.ceil(q / 100 * len(s)) - 1)]


def frame_stats(intervals_ms: list[float]) -> dict:
    n = len(intervals_ms)
    if not n:
        return {"frames": 0, "median_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "over_16_7_pct": 0.0}
    return {"frames": n, "median_ms": _rank(intervals_ms, 50), "p95_ms": _rank(intervals_ms, 95),
            "p99_ms": _rank(intervals_ms, 99),
            "over_16_7_pct": round(100.0 * sum(1 for v in intervals_ms if v > JANK_MS) / n, 1)}


def atl_frame_intervals(events: list[AgentEvent], marks: list[Mark]) -> dict[int, list[float]]:
    """Frame times (ms) per phase index from the agent's P lines (frame counter, duration us, presentation us)."""
    out: dict[int, list[float]] = defaultdict(list)
    for e in events:
        if e.tag != "P" or not e.fields[1].isdigit():
            continue
        ms = int(e.fields[1]) / 1000
        if ms < IDLE_GAP_MS:
            out[phase_of(e.t_us, marks).index].append(ms)
    return dict(out)


def android_frame_intervals(gfxinfo_text: str) -> list[float]:
    """Frame times (ms) from `dumpsys gfxinfo PKG framestats`; frames with Flags != 0 (not normal) are dropped."""
    out: list[float] = []
    head: list[str] | None = None
    inside = False
    for line in gfxinfo_text.splitlines():
        if line.startswith("---PROFILEDATA---"):
            inside, head = not inside, None
            continue
        if not inside:
            continue
        cols = line.rstrip(",").split(",")
        if head is None:
            head = cols
            continue
        try:
            row = dict(zip(head, cols))
            if int(row["Flags"]) != 0:
                continue
            ms = (int(row["FrameCompleted"]) - int(row["IntendedVsync"])) / 1e6
        except (KeyError, ValueError):
            continue
        if 0 <= ms < IDLE_GAP_MS:
            out.append(ms)
    return out


def _hang_frame(stack: list[str]) -> tuple[str, str]:
    """(topmost app or platform frame, topmost app frame)."""
    top = next((f for f in stack if not f.startswith(JDK_PREFIXES)), stack[0] if stack else "?")
    app = next((f for f in stack if not is_platform_frame(f) and not f.startswith(JDK_PREFIXES)), "")
    return top, app


def perf_findings(atl: dict[int, list[float]], android: dict[int, list[float]] | None, hangs: list[AgentEvent],
                  gcs: list[AgentEvent], marks: list[Mark], min_frames: int = JANK_MIN_FRAMES,
                  notes: list[str] | None = None) -> list[Finding]:
    """jank:<phase> (>= 10 % of >= min_frames frames > 16.7 ms), hang:<frame> (main thread blocked), gc:<phase>.

    A phase that would be a jank finding but has too few frames adds a line to `notes` instead."""
    names = {m.index: m.name for m in marks}
    out: list[Finding] = []
    for idx in sorted(atl):
        st = frame_stats(atl[idx])
        if st["frames"] and st["over_16_7_pct"] >= JANK_MIN_PCT:
            phase = names.get(idx, f"mark {idx}")
            if st["frames"] < min_frames:
                if notes is not None:
                    notes.append(f"Phase {phase}: too few frames ({st['frames']} < {min_frames}) for a jank finding, "
                                 f"{st['over_16_7_pct']:.0f} % of them > 16.7 ms")
                continue
            slow = sum(1 for v in atl[idx] if v > JANK_MS)
            out.append(Finding("jank", phase, Klass.JANK, f"Phase {phase}: {st['over_16_7_pct']:.0f} % of frames > 16.7 ms",
                               SUGGEST_JANK, [Occurrence(phase, idx, "atl", slow)],
                               {"atl": st, "android": frame_stats(android[idx]) if android and idx in android else None}))

    by_frame: dict[str, Finding] = {}
    per_phase: dict[str, dict[tuple[int, str, str], int]] = defaultdict(lambda: defaultdict(int))
    for h in hangs:
        if h.tag != "H" or not h.fields[0].isdigit():
            continue
        stack = [f[2:] if f.startswith("B:") else f for f in h.fields[1].split("|") if f]
        if any(f.startswith(PROBE_FRAMES) for f in stack):
            continue
        top, app = _hang_frame(stack)
        ms = int(h.fields[0])
        f = by_frame.get(top)
        if f is None:
            f = by_frame[top] = Finding("hang", top, Klass.JANK, "", SUGGEST_HANG,
                                        details={"stack": stack, "max_ms": ms, "durations_ms": []})
        if ms > f.details["max_ms"]:
            f.details["max_ms"], f.details["stack"] = ms, stack
        f.details["durations_ms"].append(ms)
        m = phase_of(h.t_us, marks)
        per_phase[top][(m.index, m.name, app)] += 1
    for top, f in by_frame.items():
        f.title = f"Main thread blocked {f.details['max_ms'] / 1000:.1f} s in {top}"
        f.occurrences = [Occurrence(name, i, "atl", n, app) for (i, name, app), n in sorted(per_phase[top].items())]
        out.append(f)

    pauses: dict[int, list[float]] = defaultdict(list)
    for g in gcs:
        if g.tag == "G" and g.fields[0].isdigit():
            pauses[phase_of(g.t_us, marks).index].append(int(g.fields[0]) / 1000)
    for idx in sorted(pauses):
        p = pauses[idx]
        phase = names.get(idx, f"mark {idx}")
        out.append(Finding("gc", phase, Klass.OTHER,
                           f"Phase {phase}: {len(p)} GC pauses, {sum(p):.0f} ms in total (longest {max(p):.0f} ms)",
                           occurrences=[Occurrence(phase, idx, "atl", len(p))],
                           details={"pauses": len(p), "total_ms": round(sum(p), 1), "max_ms": round(max(p), 1)}))
    return out


def android_phase_frames(cap) -> dict[int, list[float]]:
    """Frame times per phase index of an Android capture: perf/gfxinfo-NN.txt was taken at mark NN and holds
    the frames of phase NN-1; perf/gfxinfo-end.txt those of the last phase."""
    from pathlib import Path

    marks = cap.read_marks()
    out: dict[int, list[float]] = {}
    for p in sorted(Path(cap.path("perf")).glob("gfxinfo-*.txt")):
        tag = p.stem.split("-", 1)[1]
        idx = (marks[-1].index if marks else 0) if tag == "end" else int(tag) - 1 if tag.isdigit() else None
        if idx is not None and idx >= 0:
            out.setdefault(idx, []).extend(android_frame_intervals(p.read_text(encoding="utf-8", errors="replace")))
    return out
