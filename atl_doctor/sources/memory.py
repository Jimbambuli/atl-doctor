# SPDX-License-Identifier: GPL-3.0-or-later
"""Memory per phase and growth over repetitions of the same phase (Spec 6.6).

Sources: memory.tsv of the capture (`mark_index side metric value_kb`; ATL: /proc/PID/smaps_rollup and the
[heap] mapping, Android: `dumpsys meminfo` App Summary), and the agent's M lines (Java heap, JNI global
references) and C lines (instances and bytes per class, only with --heap). A value belongs to the mark at
which it was taken; phases with the same name are repetitions.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..capture import MEMORY, Capture
from ..findings import Finding, Klass, Occurrence
from .exceptions import AgentEvent

GROWTH_MIN_MB = 5.0
GROWTH_MIN_REPEATS = 3
TOP_CLASSES = 10
UNMEASURED = ["jni_weak_globals", "gtk_gsk_objects"]
UNMEASURED_REASON = ("JVMTI exposes no weak-global roots; GTK object counts are not reachable without a GTK "
                     "debug build")
SUGGEST_MEMORY = ("Something is kept alive on every repetition: compare top_classes (record --heap) and look for "
                  "listeners, caches or native objects that are never released.")


def write_memory_rows(cap: Capture, rows: list[tuple[int | str, str, str, int]]) -> None:
    """Append rows (mark index or "end", side, metric, value in kB) to memory.tsv."""
    with open(cap.path(MEMORY), "a", encoding="utf-8") as f:
        for idx, side, metric, kb in rows:
            f.write(f"{idx}\t{side}\t{metric}\t{kb}\n")


def smaps_snapshot(proc_dir: Path) -> dict[str, int]:
    """pss and anon from smaps_rollup, native_heap = Pss of the [heap] mapping (kB); {} if the process is gone."""
    out: dict[str, int] = {}
    try:
        roll = (Path(proc_dir) / "smaps_rollup").read_text()
        smaps = (Path(proc_dir) / "smaps").read_text()
    except OSError:
        return out
    for key, name in (("Pss", "pss"), ("Anonymous", "anon")):
        m = re.search(rf"^{key}:\s+(\d+) kB", roll, re.M)
        if m:
            out[name] = int(m.group(1))
    heap, inside = 0, False
    for line in smaps.splitlines():
        if re.match(r"^[0-9a-f]+-[0-9a-f]+ ", line):
            inside = line.rstrip().endswith("[heap]")
        elif inside and line.startswith("Pss:"):
            heap += int(line.split()[1])
    out["native_heap"] = heap
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    """pss, java_heap, native_heap (kB) from the App Summary of `dumpsys meminfo PKG`."""
    out: dict[str, int] = {}
    for pattern, name in ((r"TOTAL PSS:\s+(\d+)", "pss"), (r"^\s*TOTAL:\s+(\d+)", "pss"),
                          (r"^\s*Java Heap:\s+(\d+)", "java_heap"), (r"^\s*Native Heap:\s+(\d+)", "native_heap")):
        m = re.search(pattern, text, re.M)
        if m and name not in out:
            out[name] = int(m.group(1))
    return out


def read_memory(cap: Capture) -> dict[str, dict[str, list[tuple[int, float]]]]:
    """metric -> phase name -> [(mark index, MB)] in recording order; the final row is phase "end" (index -1)."""
    names = {m.index: m.name for m in cap.read_marks()}
    out: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    try:
        text = cap.path(MEMORY).read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) != 4 or not f[3].isdigit():
            continue
        idx = -1 if f[0] == "end" else int(f[0]) if f[0].isdigit() else None
        if idx is None:
            continue
        out[f[2]]["end" if idx < 0 else names.get(idx, f"mark {idx}")].append((idx, int(f[3]) / 1024))
    return {k: dict(v) for k, v in out.items()}


def growth(values_mb: list[float]) -> float:
    """Least-squares slope over the repetitions 0..n-1, times (n-1): growth from first to last."""
    n = len(values_mb)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(values_mb) / n
    slope = sum((i - mx) * (v - my) for i, v in enumerate(values_mb)) / sum((i - mx) ** 2 for i in range(n))
    return slope * (n - 1)


def _agent_series(events: list[AgentEvent], names: dict[int, str]):
    """java_heap (MB) and jni_globals per phase name from M lines; class stats per mark index from C lines."""
    heap: dict[str, list[tuple[int, float]]] = defaultdict(list)
    jni: dict[str, list[int]] = defaultdict(list)
    classes: dict[int, dict[str, tuple[int, int]]] = defaultdict(dict)
    for e in events:
        if e.tag == "M" and all(x.lstrip("-").isdigit() for x in e.fields):
            idx = int(e.fields[0])
            name = names.get(idx)
            if name is None:
                continue
            heap[name].append((idx, int(e.fields[1]) / (1 << 20)))
            if int(e.fields[3]) >= 0:
                jni[name].append(int(e.fields[3]))
        elif e.tag == "C" and e.fields[0].isdigit() and e.fields[2].isdigit() and e.fields[3].isdigit():
            classes[int(e.fields[0])][e.fields[1]] = (int(e.fields[2]), int(e.fields[3]))
    return heap, jni, classes


def _top_classes(first: dict[str, tuple[int, int]], last: dict[str, tuple[int, int]]) -> list[dict]:
    rows = [{"class": c, "instances": n - first.get(c, (0, 0))[0], "bytes": b - first.get(c, (0, 0))[1]}
            for c, (n, b) in last.items()]
    rows.sort(key=lambda r: (-r["bytes"], -r["instances"], r["class"]))
    return rows[:TOP_CLASSES]


def memory_findings(cap: Capture, events: list[AgentEvent]) -> list[Finding]:
    """mem:<phase>:<metric> when a phase repeated at least 3 times grows by at least 5 MB."""
    marks = cap.read_marks()
    names = {m.index: m.name for m in marks}
    series = read_memory(cap)
    heap, jni, classes = _agent_series(events, names)
    if heap:
        series["java_heap"] = dict(heap)
    out: list[Finding] = []
    for metric in sorted(series):
        for phase, vals in series[metric].items():
            n = len(vals)
            if phase == "end" or n < GROWTH_MIN_REPEATS:
                continue
            mb = growth([v for _, v in vals])
            if mb < GROWTH_MIN_MB:
                continue
            first, last = vals[0][0], vals[-1][0]
            details: dict = {"values_mb": [round(v, 1) for _, v in vals], "growth_mb": round(mb, 1),
                             "unmeasured": list(UNMEASURED), "unmeasured_reason": UNMEASURED_REASON}
            if phase in jni:
                details["jni_globals"] = jni[phase]
            if first in classes and last in classes:
                details["top_classes"] = _top_classes(classes[first], classes[last])
            out.append(Finding("mem", f"{phase}:{metric}", Klass.MEMORY_GROWTH,
                               f"{metric} grows by {mb:.1f} MB over {n} repetitions of phase {phase}", SUGGEST_MEMORY,
                               [Occurrence(phase, first, cap.read_manifest().side, n)], details))
    return out
