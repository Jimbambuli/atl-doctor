# SPDX-License-Identifier: GPL-3.0-or-later
"""Figures per step of a step file: frames drawn and distance scrolled (steps.tsv, written while the steps run).

steps.tsv columns: n, line, cmd, step text, t0_us, t1_us (CLOCK_BOOTTIME of the recording host), frames,
scroll_x_dp, scroll_y_dp. An empty column means "not measured". Under ATL the frames are counted from the
agent's P lines inside [t0, t1]; on Android the driver counts them (gfxinfo) while the step runs. The scroll distance
comes from the view trees before and after the step: the shift of the nodes that are in both and moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from ..capture import AGENT, STEPS_DATA, Capture
from .tree import Node

MOVED_DP = 0.5   # a node that moved less than this stays where it is


@dataclass
class StepRow:
    n: int
    line: int
    cmd: str
    text: str
    t0_us: int
    t1_us: int
    frames: int | None
    scroll_x: float | None
    scroll_y: float | None


def _key(n: Node) -> tuple[str, str, str, str]:
    return n.rid, n.real_cls, n.text, n.desc


def scroll_delta(before: list[Node], after: list[Node]) -> tuple[float, float] | None:
    """(x, y) in dp the content moved, positive = further right/down (scrollX/scrollY grew); None without a common node.

    Nodes are paired by (id, class, text, description) in document order, so repeated list items pair one to one.
    Invisible and clipped nodes (their visible rectangle changes with the clip, not with the content) are skipped;
    the shift is the median over the nodes that moved, so fixed bars do not dilute it.
    """
    groups: dict[tuple, list[Node]] = {}
    for n in before:
        if n.visible and not (n.clip_v or n.clip_h) and (n.rid or n.text or n.desc):
            groups.setdefault(_key(n), []).append(n)
    seen: dict[tuple, int] = {}
    dx: list[float] = []
    dy: list[float] = []
    matched = 0
    for n in after:
        if not n.visible or n.clip_v or n.clip_h:
            continue
        k = _key(n)
        i = seen.get(k, 0)
        seen[k] = i + 1
        if i >= len(groups.get(k, ())):
            continue
        b = groups[k][i]
        matched += 1
        dx.append(n.rect[0] - b.rect[0])
        dy.append(n.rect[1] - b.rect[1])
    if not matched:
        return None

    def shift(v: list[float]) -> float:
        moved = [x for x in v if abs(x) > MOVED_DP]
        return round(-median(moved), 1) + 0.0 if moved else 0.0

    return shift(dx), shift(dy)


def _num(s: str, cast):
    try:
        return cast(s) if s != "" else None
    except ValueError:
        return None


def read_steps(cap: Capture) -> list[StepRow]:
    try:
        text = Path(cap.path(STEPS_DATA)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for ln in text.split("\n"):
        f = ln.split("\t")
        if len(f) != 9 or not (f[0].isdigit() and f[1].isdigit() and f[4].isdigit() and f[5].isdigit()):
            continue
        out.append(StepRow(int(f[0]), int(f[1]), f[2], f[3], int(f[4]), int(f[5]), _num(f[6], int),
                           _num(f[7], float), _num(f[8], float)))
    return out


def _side(cap: Capture, rows: list[StepRow]) -> dict[int, dict]:
    atl = cap.read_manifest().side == "atl"
    times: list[int] = []
    if atl and cap.path(AGENT).is_file():
        from .exceptions import parse_agent

        times = [e.t_us for e in parse_agent(cap.path(AGENT)) if e.tag == "P"]
    out = {}
    for r in rows:
        frames = r.frames
        if atl:
            frames = sum(1 for t in times if r.t0_us <= t <= r.t1_us) if times else None
        out[r.n] = {"frames": frames, "scroll_x_dp": r.scroll_x, "scroll_y_dp": r.scroll_y}
    return out


def step_table(atl: Capture, android: Capture | None = None) -> list[dict]:
    """One row per executed step: {"n", "line", "step", "atl": {...}, "android": {...}}; empty without steps.tsv.

    A single capture fills only its own side; a pair is matched by step number and text (the same step file)."""
    own = read_steps(atl)
    a = _side(atl, own)
    key = "atl" if android is not None else atl.read_manifest().side
    other = read_steps(android) if android is not None else []
    d = _side(android, other) if android is not None else {}
    text = {r.n: r.text for r in other}
    out = []
    for r in own:
        row: dict = {"n": r.n, "line": r.line, "step": r.text, key: a[r.n]}
        if android is not None and text.get(r.n) == r.text:
            row["android"] = d[r.n]
        out.append(row)
    return out
