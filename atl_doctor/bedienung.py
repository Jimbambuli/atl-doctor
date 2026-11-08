# SPDX-License-Identifier: GPL-3.0-or-later
"""Recorded input ("Bedienung"): `input.jsonl` in a capture directory, and scenarios made from it.

Format 1, one JSON object per line, UTF-8. Line 1 is the header, every further line one input event:

    {"format": 1, "kind": "header", "side": "atl"|"android"|"starter", "package": "org.example",
     "full": false, "density": 1.5, "source": "agent"|"getevent"|"starter"}
    {"seq": 1, "t_us": 291437957025, "kind": "tap", "activity": "org.example.MainActivity",
     "target": {"id": "org.example:id/search", "cls": "...ActionMenuItemView", "text": "", "desc": "Search",
                "path": "0/0/1/0/3/0", "rect": [1277, 16, 1325, 64]},
     "point": [1301, 40]}
    {"seq": 2, "t_us": ..., "kind": "text", "activity": "...", "target": {...}, "text": {"len": 11}}
    {"seq": 3, "t_us": ..., "kind": "key", "key": "Return"}          key: xkb name as in the step files
    {"seq": 4, "t_us": ..., "kind": "scroll", "activity": "...", "scroll": {"dx": 0, "dy": 312}}
    {"seq": 5, "t_us": ..., "kind": "back", "activity": "..."}

kind: tap, long-tap, text, key, scroll, back. t_us is CLOCK_BOOTTIME in microseconds, the clock of the marks of the
same capture. rect and point are in dp relative to android:id/content (ATL: pixels 1:1), like the view trees. path is
the child index chain from android:id/content down ("0/2/1", "-" without a content view above the target). id is the
full resource name, cls the class the platform reports (ATL agent: the app's own class, uiautomator: the
accessibility class). dy is the change of the scroll position in dp (positive = content moves up).

Text is sanitised: a text event holds only the length ({"len": n}), and the text of an editable target (EditText,
AutoCompleteTextView) is left out of the target. Only with header "full": true a text event also holds
"value" and the target keeps its text. A scenario made from a sanitised recording types n placeholder characters.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from .errors import DoctorError

INPUT_FILE = "input.jsonl"
INPUT_FORMAT = 1
KINDS = ("tap", "long-tap", "text", "key", "scroll", "back")
TARGET_KINDS = ("tap", "long-tap", "text")
PLACEHOLDER = "x"
PATH_TOLERANCE_DP = 24.0   # a view found by path must lie this close (centre, |dx| + |dy|) to the recorded rectangle
MAX_WAIT_S = 10.0     # a pause between two events becomes a wait of at most this long
MIN_WAIT_S = 0.3
EDITABLE = ("EditText", "AutoCompleteTextView", "SearchAutoComplete", "MultiAutoCompleteTextView")


class InputError(DoctorError):
    pass


def is_editable(cls: str) -> bool:
    return cls.rsplit(".", 1)[-1].rsplit("$", 1)[-1].endswith(EDITABLE)


def make_header(side: str, package: str | None, source: str, full: bool = False, density: float | None = None) -> dict:
    h: dict = {"format": INPUT_FORMAT, "kind": "header", "side": side, "package": package or "", "full": full,
               "source": source}
    if density:
        h["density"] = density
    return h


def make_target(id_: str = "", cls: str = "", text: str = "", desc: str = "", path: str = "-",
                rect: Iterable[float] | None = None, full: bool = False) -> dict:
    """A target dict; the text of an editable view is dropped unless full."""
    if is_editable(cls) and not full:
        text = ""
    r = [round(float(x), 1) for x in rect] if rect is not None else None
    return {"id": id_, "cls": cls, "text": text, "desc": desc, "path": path or "-", "rect": r}


def make_event(kind: str, t_us: int, activity: str = "", target: dict | None = None, point: Iterable[float] | None = None,
               text: str | None = None, key: str | None = None, scroll: tuple[float, float] | None = None,
               full: bool = False) -> dict:
    if kind not in KINDS:
        raise InputError(f"unknown input kind {kind!r}")
    e: dict = {"t_us": int(t_us), "kind": kind}
    if activity:
        e["activity"] = activity
    if target is not None:
        e["target"] = target
    if point is not None:
        e["point"] = [round(float(x), 1) for x in point]
    if kind == "text":
        e["text"] = {"len": len(text or "")}
        if full and text is not None:
            e["text"]["value"] = text
    if key is not None:
        e["key"] = key
    if scroll is not None:
        e["scroll"] = {"dx": round(scroll[0], 1), "dy": round(scroll[1], 1)}
    return e


def write_input(path: Path, header: dict, events: list[dict]) -> None:
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    for i, e in enumerate(sorted(events, key=lambda x: x["t_us"]), 1):
        lines.append(json.dumps({"seq": i, **{k: v for k, v in e.items() if k != "seq"}}, ensure_ascii=False,
                                sort_keys=True))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_input(path: Path) -> tuple[dict, list[dict]]:
    """(header, events) of an input.jsonl, or of the input.jsonl in a capture directory."""
    p = Path(path)
    if p.is_dir():
        p = p / INPUT_FILE
    try:
        raw = p.read_text(encoding="utf-8").split("\n")
    except OSError as e:
        raise InputError(f"cannot read {p}: {e.strerror}") from e
    rows: list[dict] = []
    for n, ln in enumerate(raw, 1):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except ValueError as e:
            raise InputError(f"{p}:{n}: not JSON") from e
        if not isinstance(d, dict):
            raise InputError(f"{p}:{n}: not an object")
        rows.append(d)
    if not rows or rows[0].get("kind") != "header":
        raise InputError(f"{p}: first line is not the header")
    fmt = rows[0].get("format")
    if not isinstance(fmt, int) or fmt > INPUT_FORMAT:
        raise InputError(f"{p}: input format {fmt} is newer than supported ({INPUT_FORMAT}); update atl-doctor")
    for n, e in enumerate(rows[1:], 2):
        if e.get("kind") not in KINDS or not isinstance(e.get("t_us"), int):
            raise InputError(f"{p}:{n}: not an input event (kind/t_us)")
    return rows[0], rows[1:]


# ------------------------------------------------------------------ ATL: agent T and L lines
def events_from_agent(agent_events: list, full: bool = False) -> list[dict]:
    """Input events from the agent's lines: T (click on a view) and L onBackPressed of an Activity (back).

    The agent never reports the text of an EditText; `full` has no effect on this source."""
    out: list[dict] = []
    for a in agent_events:
        if a.tag == "T":
            kind, act, cls, id_, text, desc, l, t, r, b, path = a.fields
            rect = [float(x) for x in (l, t, r, b)] if all(re.fullmatch(r"-?\d+", x) for x in (l, t, r, b)) else None
            tg = make_target(id_, cls, text, desc, path, rect, full)
            point = [(rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2] if rect and rect[2] > rect[0] else None
            out.append(make_event(kind, a.t_us, act, tg, point))
        elif a.tag == "L" and a.fields[0] == "A" and a.fields[1] == "onBackPressed":
            out.append(make_event("back", a.t_us, a.fields[2]))
    return out


def write_atl_input(cap, package: str | None = None) -> int:
    """input.jsonl from the agent.tsv of an ATL capture; the number of events (0: no file written)."""
    from .capture import AGENT
    from .sources.exceptions import parse_agent

    if not cap.path(AGENT).is_file():
        return 0
    ev = events_from_agent(parse_agent(cap.path(AGENT)))
    if not ev:
        return 0
    write_input(cap.path(INPUT_FILE), make_header("atl", package or cap.read_manifest().package, "agent"), ev)
    return len(ev)


# ------------------------------------------------------------------ view trees: path, label, resolution
def node_path(nodes: list, i: int) -> str:
    """Child index chain from android:id/content down to nodes[i]; "-" without a content view above it."""
    chain: list[int] = []
    k = i
    while nodes[k].full_id != "android:id/content":
        p = nodes[k].parent
        if p is None:
            return "-"
        chain.append([j for j, n in enumerate(nodes) if n.parent == p].index(k))
        k = p
    return "/".join(str(x) for x in reversed(chain)) or "-"


def _descendants(nodes: list, i: int) -> list[int]:
    out, todo = [], [i]
    while todo:
        k = todo.pop()
        kids = [j for j, n in enumerate(nodes) if n.parent == k]
        out += kids
        todo = kids + todo
    return sorted(out)


def label_of(nodes: list, i: int, desc: bool = True) -> str:
    """The node's own text (with desc: or description), else the first text below it (a clickable row without text of
    its own)."""
    n = nodes[i]
    if n.text or (desc and n.desc):
        return n.text or n.desc
    return next((nodes[j].text or (nodes[j].desc if desc else "") for j in _descendants(nodes, i)
                 if nodes[j].text or (desc and nodes[j].desc)), "")


def target_at(nodes: list, x_dp: float, y_dp: float, full: bool = False) -> dict | None:
    """The view a tap at (x, y) dp reached: the smallest visible clickable node containing the point, else the smallest
    node with an id or a text."""
    def area(n) -> float:
        return (n.rect[2] - n.rect[0]) * (n.rect[3] - n.rect[1])

    inside = [i for i, n in enumerate(nodes) if n.visible and area(n) > 0
              and n.rect[0] <= x_dp < n.rect[2] and n.rect[1] <= y_dp < n.rect[3]]
    pick = [i for i in inside if nodes[i].clickable] or [i for i in inside if nodes[i].rid or nodes[i].text or nodes[i].desc]
    if not pick:
        return None
    i = min(pick, key=lambda k: (area(nodes[k]), -k))
    n = nodes[i]
    text = label_of(nodes, i, desc=False)
    return make_target(n.full_id, n.cls, text, n.desc, node_path(nodes, i), n.rect, full)


def _near(n, rect: list[float] | None) -> float:
    if not rect:
        return 0.0
    cx, cy = (n.rect[0] + n.rect[2]) / 2, (n.rect[1] + n.rect[3]) / 2
    return abs(cx - (rect[0] + rect[2]) / 2) + abs(cy - (rect[1] + rect[3]) / 2)


def resolve_target(nodes: list, id_: str = "", text: str = "", path: str = "", rect: list[float] | None = None):
    """(node, how): the node a recorded target means in this tree, in the order resource id, text, path; (None, "rect")
    when only the rectangle is left (None, "none" without one). Several matches: the one nearest to the rectangle."""
    from .sources.tree import short_id

    vis = [i for i, n in enumerate(nodes) if n.visible and n.rect[2] > n.rect[0] and n.rect[3] > n.rect[1]]
    want = short_id(id_) if id_ else ""
    if want:
        hit = [i for i in vis if nodes[i].rid == want]
        if hit:
            return nodes[min(hit, key=lambda k: _near(nodes[k], rect))], "id"
    if text:
        t = " ".join(text.split()).casefold()
        hit = [i for i in vis if t in (nodes[i].text.casefold(), nodes[i].desc.casefold(), label_of(nodes, i).casefold())]
        clickable = [i for i in hit if nodes[i].clickable]
        hit = clickable or hit
        if hit:
            return nodes[min(hit, key=lambda k: _near(nodes[k], rect))], "text"
    if path and path != "-":
        c = next((i for i, n in enumerate(nodes) if n.full_id == "android:id/content" and n.visible), None)
        k = c
        for part in path.split("/"):
            kids = [j for j, n in enumerate(nodes) if n.parent == k]
            if k is None or not part.isdigit() or int(part) >= len(kids):
                k = None
                break
            k = kids[int(part)]
        if k is not None and k in vis and _near(nodes[k], rect) <= PATH_TOLERANCE_DP:
            return nodes[k], "path"
    return None, ("rect" if rect else "none")


# ------------------------------------------------------------------ scenario from a recording
def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tap_line(kind: str, tg: dict, point: list[float] | None) -> str:
    parts = [kind]
    if tg.get("id") and ":id/" in tg["id"]:
        parts.append("id/" + tg["id"].split(":id/", 1)[1])
    label = (tg.get("text") or tg.get("desc") or "").strip()
    if label and "\n" not in label:
        parts.append(_q(" ".join(label.split())))
    if tg.get("path") and tg["path"] != "-":
        parts.append("path:" + tg["path"])
    r = tg.get("rect")
    if r:
        parts.append("rect:" + ",".join(f"{x:g}" for x in r))
    elif point:
        parts.append("rect:" + ",".join(f"{x:g}" for x in (point[0], point[1], point[0], point[1])))
    return " ".join(parts)


def scenario_from(header: dict, events: list[dict], name: str = "") -> str:
    """Step file text for a recording (`tap`/`long-tap` with element targets, see steps.py)."""
    out = [f"# made by `atl-doctor scenario-from` from {name or 'an input recording'} "
           f"(side {header.get('side', '?')}, source {header.get('source', '?')}, {len(events)} events)",
           "# a tap finds its view by resource id, then text, then path, then rectangle"]
    if header.get("package"):
        out += [f"wait-window {header['package']} 60", "wait 5"]
    prev: int | None = None
    for i, e in enumerate(events, 1):
        if prev is not None:
            gap = (e["t_us"] - prev) / 1e6
            if gap >= MIN_WAIT_S:
                out.append(f"wait {min(gap, MAX_WAIT_S):.1f}")
        prev = e["t_us"]
        k = e["kind"]
        act = e.get("activity", "")
        tg = e.get("target") or {}
        c = f"# {e.get('seq', i)}: {k}" + (f" in {act}" if act else "")
        if k in ("tap", "long-tap"):
            if tg or e.get("point"):
                out += [c, _tap_line(k, tg, e.get("point"))]
            else:
                out.append(f"{c} (no target, skipped)")
        elif k == "text":
            t = e.get("text", {})
            val = t.get("value")
            if val is None:
                val = PLACEHOLDER * min(int(t.get("len", 0)), 200)
                c += f" ({t.get('len', 0)} characters, placeholder: the recording holds no text)"
            if val:
                out += [c, f"type {_q(val)}"]
        elif k == "key":
            out += [c, f"key {e['key']}"]
        elif k == "back":
            out += [c, "key Escape"]
        elif k == "scroll":
            dy = round(e.get("scroll", {}).get("dy", 0))
            if dy:
                out += [c, f"scroll-smooth {dy}"]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ replay results (replay.jsonl) and the pair table
REPLAY_FILE = "replay.jsonl"


def read_replay(cap) -> list[dict]:
    try:
        text = Path(cap.path(REPLAY_FILE)).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in text.split("\n"):
        try:
            d = json.loads(ln) if ln.strip() else None
        except ValueError:
            d = None
        if isinstance(d, dict) and isinstance(d.get("n"), int):
            out.append(d)
    return out


def _tree_state(atl, android, a_row: dict, b_row: dict, density: float) -> tuple[str, list[str], int]:
    """("equal"|"differs"|"n/a", titles of the first differences, number of differences) of the trees taken after one
    step on both sides."""
    from .capture import Mark
    from .sources.tree import load_tree, tree_findings

    if not a_row.get("tree") or not b_row.get("tree"):
        return "n/a", [], 0
    try:
        ta = load_tree(atl.path(a_row["tree"]), "atl")
        tb = load_tree(android.path(b_row["tree"]), "android", density)
    except DoctorError:
        return "n/a", [], 0
    fs = tree_findings(tb, ta, Mark(int(a_row["n"]), 0, 0, f"step {a_row['n']}"))
    return ("differs" if fs else "equal"), [f.title for f in fs[:5]], len(fs)


def replay_table(atl, android, density: float | None = None) -> list[dict]:
    """One row per replayed step present on both sides: found yes/no per side and how, tree after the step equal or
    differs (with the first differences); empty without replay.jsonl on both sides."""
    a_rows, b_rows = read_replay(atl), {r["n"]: r for r in read_replay(android)}
    if density is None:
        density = float(android.read_manifest().environment.get("density") or 1.0)
    out = []
    for a in a_rows:
        b = b_rows.get(a["n"])
        if b is None or b.get("step") != a.get("step"):
            continue
        state, diffs, count = _tree_state(atl, android, a, b, density)
        out.append({"n": a["n"], "step": a["step"],
                    "atl": {k: a.get(k) for k in ("found", "by", "error")},
                    "android": {k: b.get(k) for k in ("found", "by", "error")},
                    "tree": state, "tree_differences": diffs, "tree_difference_count": count})
    return out


def _yn(side: dict) -> str:
    if side.get("found") is None:
        return "-"
    s = "yes" if side["found"] else "no"
    by = side.get("by")
    return f"{s} ({by})" if by and by not in ("none",) else s


def replay_lines(rows: list[dict]) -> list[str]:
    w = max(len(r["step"]) for r in rows)
    out = [f"{'#':>3}  {'step':<{w}}  target found ATL / Android   tree after the step"]
    for r in rows:
        tail = r["tree"] + (f" ({r['tree_difference_count']} differences, first: {r['tree_differences'][0]})"
                            if r["tree_differences"] else "")
        out.append(f"{r['n']:>3}  {r['step']:<{w}}  {_yn(r['atl']) + ' / ' + _yn(r['android']):<27}  {tail}")
    return out



def replay_html(rows: list[dict], esc) -> str:
    body = "".join(
        f"<tr><td>{r['n']}</td><td>{esc(r['step'])}</td><td>{esc(_yn(r['atl']))}</td><td>{esc(_yn(r['android']))}</td>"
        f"<td>{esc(r['tree'])}</td><td>{esc('; '.join(r['tree_differences']))}</td></tr>" for r in rows)
    return ("<table><thead><tr><th>#</th><th>step</th><th>target found ATL</th><th>target found Android</th>"
            f"<th>tree after the step</th><th>differences</th></tr></thead><tbody>{body}</tbody></table>")


# ------------------------------------------------------------------ command line
def _run_scenario_from(args) -> int:
    import sys

    p = Path(args.recording)
    header, events = read_input(p)
    text = scenario_from(header, events, p.name if not p.is_dir() else p.name + "/" + INPUT_FILE)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def register(sub) -> None:
    from .cli import command, done

    p, g = command(sub, "scenario-from", "make a step file from a recorded input",
                   "Turn the input recorded in input.jsonl (record --input) into a step file that can be\n"
                   "replayed under ATL and on Android.")
    g.args.add_argument("recording", metavar="RECORDING",
                        help="a capture directory with input.jsonl, or the file itself")
    g.output.add_argument("-o", "--output", metavar="FILE",
                          help="write the step file here (default: standard output)")
    done(p, g, _run_scenario_from)
