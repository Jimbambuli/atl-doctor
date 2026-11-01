# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent output (agent.tsv): exceptions and lifecycle callbacks."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..capture import Mark, phase_of
from ..findings import Finding, Klass, Occurrence, normalize_text
from .log import SUGGEST_CRASH, is_platform_frame

# columns per line tag, including tag, time and thread id
COLUMNS = {"E": 7, "L": 9, "I": 4}


@dataclass
class AgentEvent:
    tag: str
    t_us: int
    tid: int
    fields: list[str]


def parse_agent(path: Path) -> list[AgentEvent]:
    """Read agent.tsv; a partial last line (no newline yet) is dropped, malformed lines are counted."""
    text = Path(path).read_bytes().decode("utf-8", "replace")
    lines = text.split("\n")
    lines.pop()  # "" or the partial last line
    out, bad = [], 0
    for ln in lines:
        f = ln.split("\t")
        if COLUMNS.get(f[0]) != len(f) or not f[1].isdigit() or not f[2].lstrip("-").isdigit():
            bad += 1
            continue
        out.append(AgentEvent(f[0], int(f[1]), int(f[2]), f[3:]))
    if bad:
        out.append(AgentEvent("I", 0, 0, [f"skipped {bad} malformed lines"]))
    return out


def _frame(f: str) -> str:
    return normalize_text(f[2:] if f.startswith("B:") else f)


def exception_findings(events: list[AgentEvent], marks: list[Mark], side: str) -> list[Finding]:
    """One finding per exception type and first platform frame; uncaught ones are crashes."""
    found: dict[str, Finding] = {}
    counts: dict[str, dict[tuple[int, str], int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        if e.tag != "E":
            continue
        typ, msg, catcher, frames = e.fields
        stack = [_frame(f) for f in frames.split("|") if f]
        where = next((f for f in stack if is_platform_frame(f)), stack[0] if stack else "?")
        app_frame = next((f for f in stack if not is_platform_frame(f)), "")
        title = f"{typ}: {normalize_text(msg)}".rstrip(": ")
        if catcher == "UNCAUGHT":
            f = Finding("crash", f"uncaught:{typ}@{where}", Klass.CRASH, f"uncaught {title}", SUGGEST_CRASH)
        else:
            f = Finding("exc", f"{typ}@{where}", Klass.OTHER, title)
        found.setdefault(f.id, f)
        found[f.id].details.setdefault("message", msg)
        found[f.id].details.setdefault("catcher", "" if catcher == "UNCAUGHT" else _frame(catcher))
        found[f.id].details.setdefault("stack", stack)
        found[f.id].details.setdefault("app_frame", app_frame)
        m = phase_of(e.t_us, marks)
        counts[f.id][(m.index, m.name)] += 1
    for fid, f in found.items():
        f.occurrences = [Occurrence(name, i, side, n, f.details["app_frame"])
                         for (i, name), n in sorted(counts[fid].items())]
    return list(found.values())


def lifecycle_sequence(events: list[AgentEvent], marks: list[Mark]) -> dict[int, list[str]]:
    """Per phase index the order of Activity/Window.Callback/Fragment callbacks as "group:method:receiver"."""
    seq: dict[int, list[str]] = defaultdict(list)
    for e in events:
        if e.tag == "L" and set(e.fields[0]) & set("AWF"):
            group = "".join(c for c in e.fields[0] if c in "AWF")
            seq[phase_of(e.t_us, marks).index].append(f"{group}:{e.fields[1]}:{e.fields[2]}")
    return dict(seq)
