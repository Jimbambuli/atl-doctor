# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare two JSON reports by finding ID (`diff` subcommand)."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import EXIT_FOUND, DoctorError, UsageError
from .findings import Finding, Klass
from .report import Report, load_json


@dataclass
class DiffEntry:
    status: str  # "new" | "gone" | "changed"
    id: str
    short_id: str
    title: str
    old_count: int
    new_count: int
    old_weight: int | None
    new_weight: int | None


ORDER = {"new": 0, "gone": 1, "changed": 2}


def _phases(f: Finding) -> set[str]:
    return {o.phase for o in f.occurrences}


def diff_reports(old: Report, new: Report) -> list[DiffEntry]:
    a = {f.id: f for f in old.findings}
    b = {f.id: f for f in new.findings}
    out: list[DiffEntry] = []
    for fid in a.keys() | b.keys():
        o, n = a.get(fid), b.get(fid)
        if o and n:
            if (o.total_count, o.weight, _phases(o)) == (n.total_count, n.weight, _phases(n)):
                continue
            out.append(DiffEntry("changed", fid, n.short_id, n.title, o.total_count, n.total_count, o.weight, n.weight))
        elif n:
            out.append(DiffEntry("new", fid, n.short_id, n.title, 0, n.total_count, None, n.weight))
        else:
            assert o
            out.append(DiffEntry("gone", fid, o.short_id, o.title, o.total_count, 0, o.weight, None))
    out.sort(key=lambda e: (ORDER[e.status], -max(w for w in (e.old_weight, e.new_weight) if w is not None), e.id))
    return out


def render_diff_text(entries: list[DiffEntry]) -> str:
    return "".join(f"{e.status:<8}{e.short_id}  {e.title}  {e.old_count} -> {e.new_count}\n" for e in entries)


def render_diff_json(entries: list[DiffEntry]) -> str:
    return json.dumps([asdict(e) for e in entries], indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load(path: str) -> Report:
    try:
        return load_json(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise DoctorError(f"cannot read {path}: {e.strerror}") from e


def _run_diff(args: argparse.Namespace) -> int:
    wanted = [v for v in (args.fail_on or "").split(",") if v]
    classes: set[Klass] = set()
    for v in wanted:
        if v != "new":
            try:
                classes.add(Klass(v))
            except ValueError as e:
                raise UsageError(f"--fail-on: {e}; use new or a class: {', '.join(k.value for k in Klass)}") from e
    old, new = _load(args.old), _load(args.new)
    entries = diff_reports(old, new)
    text = render_diff_json(entries) if args.format == "json" else render_diff_text(entries)
    sys.stdout.write(text)
    klass = {f.id: f.klass for f in new.findings}
    fresh = [e for e in entries if e.status == "new"]
    if ("new" in wanted and fresh) or any(klass[e.id] in classes for e in fresh):
        return EXIT_FOUND
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import common_parser

    p = sub.add_parser("diff", help="compare two JSON reports", parents=[common_parser()])
    p.add_argument("-f", "--format", choices=["text", "json"], default="text")
    p.add_argument("--fail-on", metavar="new|CLASS[,CLASS]",
                   help="exit 4 if there are new findings (new) or new findings of these classes")
    p.add_argument("old", metavar="OLD.json")
    p.add_argument("new", metavar="NEW.json")
    p.set_defaults(func=_run_diff)
