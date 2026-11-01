# SPDX-License-Identifier: GPL-3.0-or-later
"""ART method trace: the watch list for atl-doctor-tracecount and its output turned into stub findings."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..capture import Mark
from ..findings import Finding, Occurrence
from .stubs import StubEntry, stub_finding, stub_index


@dataclass
class TraceHit:
    phase_index: int
    cls: str
    method: str
    sig: str
    caller: str  # "pkg.Class.method" or "" (no non-platform frame on the stack)
    caller_kind: str  # "a" app package, "o" other non-platform code, "-" none
    count: int


def write_watch_file(entries: list[StubEntry], path: Path) -> None:
    """Lines "class\\tmethod\\tsig" (an empty sig matches every overload in the counter)."""
    lines = sorted({f"{e.cls}\t{e.method}\t{e.sig}" for e in entries})
    Path(path).write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")


def _rows(path: Path) -> list[list[str]]:
    try:
        text = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    return [ln.split("\t") for ln in text.split("\n") if ln]


def read_counter_output(prefix: Path) -> list[TraceHit]:
    """Hits of watched methods from OUTPREFIX.methods + OUTPREFIX.callers; missing files give no hits."""
    prefix = Path(prefix)
    methods: dict[int, tuple[str, str, str]] = {}
    for f in _rows(prefix.with_name(prefix.name + ".methods")):
        if len(f) >= 4:
            try:
                methods[int(f[0], 16)] = (f[1], f[2], f[3])
            except ValueError:
                pass
    hits = []
    for f in _rows(prefix.with_name(prefix.name + ".callers")):
        try:
            phase, stub, count = int(f[0]), int(f[1], 16), int(f[4])
            caller = "" if f[2] == "-" else ".".join(methods[int(f[2], 16)][:2])
        except (ValueError, IndexError, KeyError):
            continue
        if stub in methods:
            cls, name, sig = methods[stub]
            hits.append(TraceHit(phase, cls, name, sig, caller, f[3], count))
    return hits


def stub_findings(hits: list[TraceHit], entries: list[StubEntry], marks: list[Mark],
                  result_used: set[tuple[str, str]]) -> list[Finding]:
    """One finding per called stub: count per phase, callers (app package first, then by count)."""
    names = {m.index: m.name for m in marks}
    known = stub_index(entries)
    per_phase: dict[tuple[str, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    callers: dict[tuple[str, str], dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    for h in hits:
        key = (h.cls, h.method)
        if key not in known:
            continue
        per_phase[key][h.phase_index] += h.count
        if h.caller:
            callers[key][(h.caller_kind, h.caller)] += h.count
    out = []
    for key, phases in per_phase.items():
        occ = [Occurrence(names.get(i, f"mark {i}"), i, "atl", n) for i, n in sorted(phases.items())]
        ranked = sorted(callers[key].items(), key=lambda kv: (kv[0][0] != "a", -kv[1], kv[0][1]))
        out.append(stub_finding(key[0], key[1], entries, key in result_used, occ, [c for (_, c), _ in ranked]))
    return out
