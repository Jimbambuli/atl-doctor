# SPDX-License-Identifier: GPL-3.0-or-later
"""Report model, text rendering and the `report` subcommand."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import DoctorError, UsageError
from .findings import FIX_FIRST_MAX, Finding, Klass, rank


@dataclass
class Report:
    mode: str  # "record" | "log" | "scan"
    environment: dict[str, str]
    probes: list[Any]  # list[Probe] from Task 7
    phases: list[str]
    findings: list[Finding]
    extras: dict[str, Any] = field(default_factory=dict)


def _kind_in(*kinds: str) -> Callable[[Finding], bool]:
    return lambda f: f.kind in kinds


SECTIONS: list[tuple[str, Callable[[Finding], bool]]] = [
    ("Environment", lambda f: False),
    ("Fix first", lambda f: False),
    ("Hit stubs", _kind_in("stub", "stublog")),
    ("Missing methods/classes", _kind_in("missing")),
    ("Exceptions", _kind_in("exc")),
    ("Crashes", _kind_in("crash")),
    ("View tree differences", _kind_in("tree")),
    ("Lifecycle differences", _kind_in("lc")),
    ("Jank and hangs", _kind_in("jank", "hang", "gc")),
    ("Memory growth", _kind_in("mem")),
]


def finding_lines(f: Finding) -> list[str]:
    first = min(f.occurrences, key=lambda o: o.phase_index).phase if f.occurrences else "-"
    lines = [f"{f.short_id}  {f.weight:>3}  {f.title}  [{f.total_count}x, first: {first}]"]
    if f.suggestion:
        lines.append(f"    -> {f.suggestion}")
    return lines


def _exception_side(f: Finding) -> str:
    sides = {o.side for o in f.occurrences}
    if sides == {"android"}:
        return "only Android"
    if "android" in sides or f.on_android:
        return "both"
    return "only ATL"


def _environment_lines(r: Report) -> list[str]:
    lines = [f"mode: {r.mode}"]
    if r.phases:
        lines.append("phases: " + ", ".join(r.phases))
    lines += [f"{k}: {v}" for k, v in r.environment.items()]
    for p in r.probes:
        lines.append(f"{p.name}: available" if p.available else f"{p.name}: unavailable ({p.reason})")
    return lines


def render_text(r: Report) -> str:
    ranked = rank(r.findings)
    out: list[str] = []

    def section(title: str, lines: list[str]) -> None:
        if lines:
            out.append(title)
            out.extend("  " + line for line in lines)
            out.append("")

    for title, pred in SECTIONS:
        if title == "Environment":
            section(title, _environment_lines(r))
        elif title == "Fix first":
            top = [f for f in ranked if f.klass is not Klass.OTHER][:FIX_FIRST_MAX]
            section(title, [ln for f in top for ln in finding_lines(f)])
        elif title == "Exceptions":
            lines: list[str] = []
            for side in ("only ATL", "both", "only Android"):
                group = [f for f in ranked if f.kind == "exc" and _exception_side(f) == side]
                if group:
                    lines.append(side)
                    lines += ["  " + ln for f in group for ln in finding_lines(f)]
            section(title, lines)
        else:
            section(title, [ln for f in ranked if pred(f) for ln in finding_lines(f)])
    return "\n".join(out)


ENV_KEYS = ("package", "version_name", "version_code", "atl_commit", "art_commit", "apk", "started", "args",
            "exit", "signal", "duration")


def _run_report(args: argparse.Namespace) -> int:
    if not args.log and not args.dirs:
        raise UsageError("report needs --log FILE or a capture directory")
    if not args.log:
        raise DoctorError("reports from capture directories are not available yet")
    from .sources.log import parse_logs

    res = parse_logs([Path(p) for p in args.log])
    env = {k: res.header[k] for k in ENV_KEYS if k in res.header}
    text = render_text(Report("log", env, [], res.phases, res.findings))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import common_parser

    p = sub.add_parser("report", help="report from a capture directory or a log", parents=[common_parser()])
    p.add_argument("-f", "--format", choices=["text"], default="text")
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of stdout")
    p.add_argument("--log", action="append", metavar="FILE", help="log mode: one log file per phase (repeatable)")
    p.add_argument("dirs", nargs="*", metavar="DIR", help="capture directory [and Android capture directory]")
    p.set_defaults(func=_run_report)
