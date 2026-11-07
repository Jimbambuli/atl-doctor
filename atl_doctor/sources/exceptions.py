# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent output (agent.tsv): exceptions and lifecycle callbacks."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..capture import Mark, phase_of
from ..findings import Finding, Klass, Occurrence, normalize_text
from .log import SUGGEST_CRASH, is_platform_frame

# columns per line tag, including tag and time; tags in WITH_TID have the thread id as third column
COLUMNS = {"E": 7, "L": 9, "I": 4, "P": 5, "G": 3, "H": 5, "M": 6, "C": 6, "X": 7, "R": 7}
WITH_TID = {"E", "L", "I", "H", "X", "R"}


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
    return parse_agent_lines(lines)


def parse_agent_lines(lines: list[str]) -> list[AgentEvent]:
    """Complete agent lines (without newline); malformed ones are counted."""
    out, bad = [], 0
    for ln in lines:
        f = ln.split("\t")
        tid = f[0] in WITH_TID
        if COLUMNS.get(f[0]) != len(f) or not f[1].isdigit() or (tid and not f[2].lstrip("-").isdigit()):
            bad += 1
            continue
        out.append(AgentEvent(f[0], int(f[1]), int(f[2]), f[3:]) if tid else AgentEvent(f[0], int(f[1]), 0, f[2:]))
    if bad:
        out.append(AgentEvent("I", 0, 0, [f"skipped {bad} malformed lines"]))
    return out


# I lines that mean a probe of the agent did not work (the others are progress notes)
AGENT_PROBLEMS = ("late: attach failed", "late: cannot load plugin", "no JVMTI environment", "AddCapabilities failed",
                  "SetBreakpoint failed", "exit hook: method not found", "unknown option", "frames unavailable", "hang: no thread named main",
                  "heap: cannot tag objects", "heap: class counting failed", "gc pauses unavailable", "skipped ")


def agent_problems(events: list[AgentEvent]) -> list[str]:
    """The agent's runtime problems, each text once, in order."""
    return list(dict.fromkeys(e.fields[0] for e in events if e.tag == "I" and e.fields[0].startswith(AGENT_PROBLEMS)))


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


EXIT_NONE = "Ende ohne System.exit/Runtime.exit erfasst"
EXIT_HOOKS = 3  # breakpoints the agent sets: System.exit, Runtime.exit, Runtime.halt


def _exit_hooks(events: list[AgentEvent]) -> int | None:
    """Number of exit breakpoints the agent says it set (I line "exit hooks N"), None if it said nothing."""
    n = [int(e.fields[0].split()[2]) for e in events
         if e.tag == "I" and e.fields[0].startswith("exit hooks ") and e.fields[0].split()[2:3][:1] and e.fields[0].split()[2].isdigit()]
    return n[-1] if n else None


def exit_findings(events: list[AgentEvent], status: int | None, signal: str | None, marks: list[Mark], side: str) -> list[Finding]:
    """Why did the process end: the first System.exit/Runtime.exit/Runtime.halt call the agent saw (X lines, with
    the last exceptions before it, R lines), else the statement that none was seen."""
    hooks = _exit_hooks(events)
    last = marks[-1] if marks else None
    where = [Occurrence(last.name, last.index, side, 1, "agent")] if last else []
    xs = [i for i, e in enumerate(events) if e.tag == "X"]
    end: dict = {"process_exit_status": status, "process_signal": signal, "exit_hooks": hooks}
    if not xs:
        title = EXIT_NONE
        if hooks != EXIT_HOOKS:
            title += f" (Exit-Hooks des Agenten: {'ohne Angabe' if hooks is None else f'{hooks} von {EXIT_HOOKS}'})"
        klass = Klass.CRASH if (signal or status) else Klass.OTHER
        return [Finding("exit", "cause:none", klass, title,
                        "The process ended in another way (native exit, signal, crash of the runtime); see the crash findings "
                        "and the log." if klass is Klass.CRASH else "", where, end)]
    first = events[xs[0]]
    kind, st, thread, frames = first.fields
    stack = [_frame(f) for f in frames.split("|") if f]
    app_frame = next((f for f in stack if not is_platform_frame(f)), "")
    chain = [events[i].fields[0] for i in xs]
    recent = []
    for e in events[xs[0] + 1:]:
        if e.tag != "R":
            break
        etype, ethread, catcher, app = e.fields
        recent.append({"type": etype, "thread": ethread, "tid": e.tid, "t_us": e.t_us,
                       "catcher": "" if catcher == "UNCAUGHT" else _frame(catcher), "uncaught": catcher == "UNCAUGHT",
                       "app_frame": "" if app == "-" else _frame(app)})
    end.update({"call": kind, "status": st, "thread": thread, "tid": first.tid, "app_frame": app_frame, "stack": stack,
                "chain": chain, "recent_exceptions": recent})
    nonzero = st != "0" and (st != "?" or bool(status or signal))
    title = f"{kind}({st}) called" + (f" from {app_frame}" if app_frame else " (no app frame in the stack)") + f" on thread {thread}"
    return [Finding("exit", f"cause:{kind}:{st}@{app_frame}", Klass.CRASH if nonzero else Klass.OTHER, title,
                    "The app ended itself here; start at the app frame and the exceptions before it." if nonzero else "",
                    where, end)]


def lifecycle_sequence(events: list[AgentEvent], marks: list[Mark]) -> dict[int, list[str]]:
    """Per phase index the order of Activity/Window.Callback/Fragment callbacks as "group:method:receiver"."""
    seq: dict[int, list[str]] = defaultdict(list)
    for e in events:
        if e.tag == "L" and set(e.fields[0]) & set("AWF"):
            group = "".join(c for c in e.fields[0] if c in "AWF")
            seq[phase_of(e.t_us, marks).index].append(f"{group}:{e.fields[1]}:{e.fields[2]}")
    return dict(seq)


ATL_ONLY_FACTOR = 3  # an exception this many times as often on ATL counts as ATL-only


def compare_sides(atl: list[Finding], android: list[Finding]) -> list[Finding]:
    """ATL findings with the Android capture's findings: exceptions only on ATL (or >= 3x as often) become
    EXC_ATL_ONLY, findings on both sides are marked on_android, Android-only ones stay as OTHER."""
    by_id = {f.id: f for f in android}
    out = []
    for f in atl:
        a = by_id.pop(f.id, None)
        if a is not None:
            f.on_android = True
            f.occurrences = f.occurrences + a.occurrences
            n_atl = sum(o.count for o in f.occurrences if o.side == "atl")
            n_and = sum(o.count for o in f.occurrences if o.side == "android")
            if f.kind == "exc" and n_atl >= ATL_ONLY_FACTOR * n_and:
                f.klass = Klass.EXC_ATL_ONLY
        elif f.kind == "exc":
            f.klass = Klass.EXC_ATL_ONLY
        out.append(f)
    for f in by_id.values():
        f.klass = Klass.OTHER
        out.append(f)
    return out


def phases_by_name(phases: list[str]) -> dict[tuple[str, int], int]:
    """(name, n-th time this name occurs) -> phase index; repeated phases pair in order."""
    seen: dict[str, int] = defaultdict(int)
    out = {}
    for i, name in enumerate(phases):
        out[(name, seen[name])] = i
        seen[name] += 1
    return out


def lifecycle_findings(atl_seq: dict[int, list[str]], android_seq: dict[int, list[str]], atl_phases: list[str],
                       android_phases: list[str]) -> list[Finding]:
    """Per phase (paired by name) the first lifecycle entry where ATL and Android differ."""
    andr = phases_by_name(android_phases)
    out = []
    for (name, nth), i in phases_by_name(atl_phases).items():
        j = andr.get((name, nth))
        if j is None:
            continue
        a, b = atl_seq.get(i, []), android_seq.get(j, [])
        k = next((k for k in range(max(len(a), len(b))) if k >= len(a) or k >= len(b) or a[k] != b[k]), None)
        if k is None:
            continue
        entry = a[k] if k < len(a) else b[k]
        what = (f"ATL {a[k]}, Android {b[k]}" if k < len(a) and k < len(b)
                else f"only on ATL: {a[k]}" if k < len(a) else f"missing on ATL: {b[k]}")
        out.append(Finding("lc", f"{name}:{entry}", Klass.OTHER, f"Phase {name}: lifecycle differs at step {k + 1} ({what})",
                           occurrences=[Occurrence(name, i, "atl", 1, "")], details={"atl": a, "android": b}))
    return out
