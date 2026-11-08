# SPDX-License-Identifier: GPL-3.0-or-later
"""Common finding model: stable IDs and the "fix first" ranking.

Every source turns its raw data into `Finding`s; report, diff and explain
only know this model.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class Klass(StrEnum):
    CRASH = "crash"
    MISSING = "missing"
    EXC_ATL_ONLY = "exception-atl-only"
    STUB_RESULT_USED = "stub-result-used"
    SYSTEM = "system-answer"
    TREE_VISIBLE = "tree-visible"
    MEMORY_GROWTH = "memory-growth"
    RENDER = "render"
    LIFECYCLE = "lifecycle"
    JANK = "jank"
    OTHER = "other"


RANK: dict[Klass, int] = {
    Klass.CRASH: 60, Klass.MISSING: 60, Klass.EXC_ATL_ONLY: 50, Klass.STUB_RESULT_USED: 40, Klass.SYSTEM: 40,
    Klass.TREE_VISIBLE: 30, Klass.RENDER: 30, Klass.LIFECYCLE: 60, Klass.MEMORY_GROWTH: 20, Klass.JANK: 10, Klass.OTHER: 0,
}
ANDROID_PENALTY = 5
FIX_FIRST_MAX = 10


@dataclass(frozen=True)
class Occurrence:
    phase: str
    phase_index: int
    side: str  # "atl" | "android" | "static"
    count: int = 1
    where: str = ""


@dataclass
class Finding:
    kind: str
    key: str
    klass: Klass
    title: str
    suggestion: str = ""
    occurrences: list[Occurrence] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    on_android: bool = False

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"

    @property
    def short_id(self) -> str:
        return hashlib.sha1(self.id.encode()).hexdigest()[:8]

    @property
    def weight(self) -> int:
        return max(0, RANK[self.klass] - (ANDROID_PENALTY if self.on_android else 0))

    @property
    def total_count(self) -> int:
        return sum(o.count for o in self.occurrences)

    @property
    def first_phase_index(self) -> int:
        return min((o.phase_index for o in self.occurrences), default=0)


_NORMALIZE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "X"),
    (re.compile(r"@[0-9a-fA-F]{4,}"), "@X"),
    (re.compile(r"\$\$ExternalSynthetic\w*?\d+"), "$$ExternalSyntheticN"),
    (re.compile(r"\$\d+"), "$N"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalize_text(s: str) -> str:
    """Replace numbers, addresses, hashes and synthetic suffixes so the same cause gives the same text."""
    for rx, rep in _NORMALIZE:
        s = rx.sub(rep, s)
    return s.strip()


def merge(findings: Iterable[Finding]) -> list[Finding]:
    """Combine findings with the same ID (order of first appearance, highest class wins)."""
    out: dict[str, Finding] = {}
    for f in findings:
        m = out.get(f.id)
        if m is None:
            out[f.id] = Finding(f.kind, f.key, f.klass, f.title, f.suggestion, list(f.occurrences),
                                dict(f.details), f.on_android)
            continue
        if RANK[f.klass] > RANK[m.klass]:
            m.klass = f.klass
        m.occurrences.extend(f.occurrences)
        for k, v in f.details.items():
            if k not in m.details:
                m.details[k] = v
            elif isinstance(m.details[k], list) and isinstance(v, list):
                m.details[k] = m.details[k] + v
        m.on_android = m.on_android or f.on_android
        if not m.suggestion:
            m.suggestion = f.suggestion
    return list(out.values())


def rank(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (-f.weight, -f.total_count, f.first_phase_index, f.id))


def to_dict(f: Finding) -> dict:
    return {
        "id": f.id, "short_id": f.short_id, "kind": f.kind, "key": f.key, "class": str(f.klass),
        "title": f.title, "suggestion": f.suggestion, "weight": f.weight,
        "weight_explained": {"class": str(f.klass), "rank": RANK[f.klass],
                             "android_penalty": ANDROID_PENALTY if f.on_android else 0},
        "count": f.total_count, "on_android": f.on_android,
        "occurrences": [{"phase": o.phase, "phase_index": o.phase_index, "side": o.side,
                         "count": o.count, "where": o.where} for o in f.occurrences],
        "details": f.details,
    }


def from_dict(d: dict) -> Finding:
    return Finding(d["kind"], d["key"], Klass(d["class"]), d["title"], d.get("suggestion", ""),
                   [Occurrence(o["phase"], o["phase_index"], o["side"], o.get("count", 1), o.get("where", ""))
                    for o in d.get("occurrences", [])],
                   dict(d.get("details", {})), bool(d.get("on_android", False)))


def lookup(findings: list[Finding], ref: str) -> Finding:
    """Find by full ID, by short ID, or by a unique short-ID prefix of at least 4 characters."""
    for f in findings:
        if f.id == ref:
            return f
    for f in findings:
        if f.short_id == ref:
            return f
    if len(ref) >= 4:
        hits = [f for f in findings if f.short_id.startswith(ref)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise LookupError("ambiguous: " + ", ".join(f"{h.short_id} {h.id}" for h in hits))
    raise LookupError(f"no finding matches {ref}")
