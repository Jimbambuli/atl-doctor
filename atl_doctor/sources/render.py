# SPDX-License-Identifier: GPL-3.0-or-later
"""Rendering defects found in one capture: view tree plus screenshot of the same mark (trees/NAME.xml, shots/NAME.png).

Checks (kind `render`, class `render`):
  contrast   text whose colour against the background measured in the screenshot is under WCAG 4.5:1 (3:1 for large
             text; ink height >= LARGE_INK_DP). The colours are not in the tree: background = dominant colour of the
             view's rectangle (needs >= BG_SHARE of it), text colour = the pixel farthest from it, edge band left out.
  flat       a view that should show content is one single colour: text that is not painted at all, an ImageView with a
             description or click handler, a selected view (tab) that has text below it.
  overflow   (ATL tree only) a text/clickable view lies partly outside its window or outside a non-scrolling parent.
Not determinable from tree and screenshot: truncated text (the tree has neither text size nor measured text width);
it is not reported. A view inside a dialog/popup window is only checked by `overflow`: the screenshot origin file only
describes the main window. While such a window is open the main window is dimmed, so `contrast` is skipped for it.
"""
from __future__ import annotations

from pathlib import Path

from ..capture import SHOTS, TREES, Capture
from ..errors import DoctorError
from ..findings import Finding, Klass, Occurrence
from .shots import CROPS, _crop, png_read, read_origin
from .tree import Node, _key, load_tree

WCAG_TEXT = 4.5
WCAG_LARGE = 3.0
LARGE_INK_DP = 18.0     # ink height of text from about 18 pt on counts as large (assumption, not measured size)
BG_SHARE = 0.4
INK_DIFF = 8            # channel difference from the background from which a pixel counts as ink
FLAT_DIFF = 3
EDGE_PX = 2
MIN_SIDE_PX = 6
BLANK_MIN = 4           # this many unpainted text views, and BLANK_SHARE of all, count as one blank area
BLANK_SHARE = 0.6
BLANK_RATIO = 1.5       # text this close to its background counts as not painted for the blank area
CUT_MIN_PX = 4.0
CUT_MIN_SHARE = 0.05
SCROLL_VERTICAL = ("ScrollView", "ListView", "GridView")   # also matches HorizontalScrollView, see SCROLL_BOTH
SCROLL_BOTH = ("Horizontal", "Recycler", "Pager", "Gallery", "Drawer", "Coordinator", "Behavior", "Carousel", "Swipe",
               "WebView", "Compose")
NOT_DETERMINABLE = ("render: truncated text is not determinable from view tree and screenshot (no text size or "
                    "measured text width) and is not checked")

Rect = tuple[float, float, float, float]
Img = tuple[int, int, bytes]


def luminance(rgb: tuple[int, int, int]) -> float:
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _samples(img: Img, rect: Rect) -> tuple[list[tuple[int, int, int, int, int]], int, int] | None:
    """(x, y, r, g, b) samples inside rect (clamped to the image), plus the clamped width and height."""
    w, h, px = img
    x0, y0 = max(0, round(rect[0])), max(0, round(rect[1]))
    x1, y1 = min(w, round(rect[2])), min(h, round(rect[3]))
    if x1 - x0 < MIN_SIDE_PX or y1 - y0 < MIN_SIDE_PX:
        return None
    step = max(1, int(((x1 - x0) * (y1 - y0) / 6000) ** 0.5))
    out = []
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            i = (y * w + x) * 4
            out.append((x, y, px[i], px[i + 1], px[i + 2]))
    return out, x1 - x0, y1 - y0


def _far(a, b) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def analyze(img: Img, rect: Rect, scale: float) -> dict | None:
    """Colours in one rectangle: {"flat": bool, "bg": rgb, "ink": rgb|None, "ratio": float|None, "ink_dp": float}.
    None when the rectangle is outside the picture or no background colour dominates it."""
    got = _samples(img, rect)
    if got is None:
        return None
    s, w, h = got
    first = s[0][2:]
    if all(_far(p[2:], first) <= FLAT_DIFF for p in s):
        return {"flat": True, "bg": first, "ink": None, "ratio": 1.0, "ink_dp": 0.0}
    hist: dict[tuple[int, int, int], list[int]] = {}
    for p in s:
        hist.setdefault((p[2] >> 4, p[3] >> 4, p[4] >> 4), []).append(p[2] * 65536 + p[3] * 256 + p[4])
    top = max(hist.values(), key=len)
    if len(top) / len(s) < BG_SHARE:
        return None
    m = sorted(top)[len(top) // 2]
    bg = (m >> 16, (m >> 8) & 255, m & 255)
    x0, y0 = min(p[0] for p in s), min(p[1] for p in s)
    inner = [p for p in s if w <= 12 or h <= 12 or (EDGE_PX <= p[0] - x0 < w - EDGE_PX and EDGE_PX <= p[1] - y0 < h - EDGE_PX)]
    ink_px = [p for p in inner if _far(p[2:], bg) >= INK_DIFF]
    if not ink_px:
        return {"flat": False, "bg": bg, "ink": None, "ratio": 1.0, "ink_dp": 0.0}
    far = max(ink_px, key=lambda p: (_far(p[2:], bg), -p[0]))
    ink = far[2:]
    ys = [p[1] for p in ink_px if _far(p[2:], bg) >= max(INK_DIFF, _far(ink, bg) // 2)]
    return {"flat": False, "bg": bg, "ink": ink, "ratio": contrast_ratio(ink, bg),
            "ink_dp": ((max(ys) - min(ys) + 1) if ys else 0) / scale}


def _path(nodes: list[Node], i: int) -> str:
    parts = []
    k: int | None = i
    while k is not None:
        n = nodes[k]
        sib = [j for j, o in enumerate(nodes) if o.parent == n.parent and o.cls == n.cls]
        parts.append(n.cls.split(".")[-1] + (f"[{sib.index(k)}]" if len(sib) > 1 else ""))
        k = n.parent
    return "/".join(reversed(parts))


def _has_text_below(nodes: list[Node], i: int) -> bool:
    return any(n.text and _under(nodes, k, i) for k, n in enumerate(nodes) if k != i)


def _under(nodes: list[Node], k: int, anc: int) -> bool:
    p = nodes[k].parent
    while p is not None:
        if p == anc:
            return True
        p = nodes[p].parent
    return False


def _chain(nodes: list[Node], i: int) -> list[int]:
    out, p = [], nodes[i].parent
    while p is not None:
        out.append(p)
        p = nodes[p].parent
    return out


def _outside(r: Rect, box: Rect) -> tuple[float, float, float, float]:
    """How far r reaches beyond box on the left, top, right, bottom (0 if inside)."""
    return max(0.0, box[0] - r[0]), max(0.0, box[1] - r[1]), max(0.0, r[2] - box[2]), max(0.0, r[3] - box[3])


def overflow_hits(nodes: list[Node]) -> list[dict]:
    """ATL only: content views that stick out of their window or of an ancestor (Java layout bounds). Inside a
    vertical scroller only the horizontal axis counts, inside any other scroller/pager/drawer nothing does."""
    hits = []
    for i, n in enumerate(nodes):
        if not n.visible or n.opaque or n.java_rect is None or not (n.text or n.desc or n.clickable):
            continue
        r = n.java_rect
        w, h = r[2] - r[0], r[3] - r[1]
        chain = _chain(nodes, i)
        if w <= 0 or h <= 0 or not chain:
            continue
        names = [nodes[a].real_cls or nodes[a].cls for a in chain]
        if any(any(t in c for t in SCROLL_BOTH) for c in names) or any(nodes[a].scrollable for a in chain):
            continue
        vertical_free = any(any(t in c for t in SCROLL_VERTICAL) for c in names)
        for a in chain:
            box = nodes[a].java_rect
            if box is None or box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
                continue
            cut = [(o, name) for o, d, name in zip(_outside(r, box), (w, h, w, h), ("left", "top", "right", "bottom"))
                   if o >= CUT_MIN_PX and o >= CUT_MIN_SHARE * d and not (vertical_free and name in ("top", "bottom"))]
            if cut:
                hits.append({"check": "overflow", "i": i, "of": "window" if a == chain[-1] else "parent", "ancestor": a,
                             "box": box, "sides": {name: round(o) for o, name in cut}, "rect_px": r})
                break
    return hits


def _is_image(n: Node) -> bool:
    c = n.real_cls or n.cls
    return "ImageView" in c or "ImageButton" in c


def _covered(nodes: list[Node], i: int) -> bool:
    """A visible view drawn later (document order), not an ancestor or descendant, covers the whole rectangle: what
    the picture shows there belongs to that view (e.g. a full-screen page over the map's buttons)."""
    r = nodes[i].rect
    return any(j > i and o.visible and not o.opaque and o.rect[0] <= r[0] and o.rect[1] <= r[1]
               and o.rect[2] >= r[2] and o.rect[3] >= r[3] and (o.rect[2] - o.rect[0]) * (o.rect[3] - o.rect[1]) > 0
               and not _under(nodes, j, i) and not _under(nodes, i, j) and o.window == nodes[i].window
               and (o.children > 0 or o.text or o.desc or _is_image(o))
               for j, o in enumerate(nodes))


def pixel_hits(nodes: list[Node], img: Img, side: str, origin: tuple[float, float], density: float) -> list[dict]:
    """contrast and flat hits of all views of the main window that carry text or an image."""
    hits: list[dict] = []
    flagged: list[int] = []
    texts = 0
    dimmed = any(n.window and not n.window.startswith("activity") for n in nodes)   # dialog scrim dims the activity
    for i, n in enumerate(nodes):
        if not n.visible or n.opaque or not n.enabled or n.full_id in ("android:id/statusBarBackground",
                                                                         "android:id/navigationBarBackground"):
            continue
        if side == "atl":
            if not n.window.startswith("activity") or n.gtk_rect is None:
                continue
            g = n.gtk_rect
            rect, scale = (g[0] + origin[0], g[1] + origin[1], g[2] + origin[0], g[3] + origin[1]), 1.0
        else:
            rect, scale = n.rect, density
        is_text = bool(n.text.strip())
        sel_text = n.selected and not is_text and _has_text_below(nodes, i)
        image = _is_image(n) and bool(n.desc or n.clickable)
        if not (is_text or sel_text or image):
            continue
        if any(_under(nodes, i, a) for a in flagged) or _covered(nodes, i):
            continue
        a = analyze(img, rect, scale)
        if a is None:
            continue
        texts += is_text
        if a["flat"] or (is_text and a["ink"] is None):
            what = "text" if is_text else "image" if image else "selected"
            hits.append({"check": "flat", "i": i, "what": what, "colour": a["bg"], "rect_px": rect})
            flagged.append(i)
        elif is_text and not dimmed:
            need = WCAG_LARGE if a["ink_dp"] >= LARGE_INK_DP else WCAG_TEXT
            if a["ratio"] < need:
                hits.append({"check": "contrast", "i": i, "ratio": a["ratio"], "need": need, "bg": a["bg"],
                             "ink": a["ink"], "rect_px": rect})
    blank = [h for h in hits if (h["check"] == "flat" and h["what"] == "text")
             or (h["check"] == "contrast" and h["ratio"] < BLANK_RATIO)]
    if len(blank) >= BLANK_MIN and len(blank) >= BLANK_SHARE * texts:   # the picture shows another state than the tree
        rs = [h["rect_px"] for h in blank]
        hits = [h for h in hits if h["check"] == "overflow"] + [
            {"check": "flat", "what": "blank", "i": blank[0]["i"], "colour": blank[0].get("colour") or blank[0]["bg"], "count": len(blank),
             "of": texts, "rect_px": (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs),
                                      max(r[3] for r in rs))}]
    return hits


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(c)


def _finding(h: dict, nodes: list[Node], name: str, phase: str, index: int, side: str, scale: float,
             origin: tuple[float, float]) -> Finding:
    n = nodes[h["i"]]
    rp = h["rect_px"]
    key = f"{h['check']}:{name}:" + ("blank" if h.get("what") == "blank" else _key(n))
    d: dict = {"check": h["check"], "view": n.rid or n.cls, "class": n.real_cls or n.cls, "path": _path(nodes, h["i"]),
               "text": n.text, "rect_px": [round(v) for v in rp], "tree": name}
    where = f"{name}: {_path(nodes, h['i'])}"
    if h["check"] == "contrast":
        d.update(ratio=round(h["ratio"], 2), needed=h["need"], background=_hex(h["bg"]), text_colour=_hex(h["ink"]))
        title = (f"{'Unreadable' if h['ratio'] < WCAG_LARGE else 'Low-contrast'} text, contrast {h['ratio']:.1f}:1 "
                 f"(needs {h['need']:g}:1): {_key(n)} "
                 f"{_hex(h['ink'])} on {_hex(h['bg'])}")
        sugg = "The text colour is hardly distinguishable from the background it is painted on; check the theme/style colours."
    elif h["check"] == "flat":
        d.update(colour=_hex(h["colour"]), what=h["what"])
        if h["what"] == "blank":
            d.update(unpainted_text_views=h["count"], text_views=h["of"], view="(several)", path="(several)", text="")
        title = {"blank": f"{h.get('count')} of {h.get('of')} text views show no text: the area is blank ({_hex(h['colour'])}); "
                          "the screenshot may predate the drawing or the content is missing",
                 "text": f"Text is not painted: {_key(n)} is one colour ({_hex(h['colour'])})",
                 "image": f"Image is one colour ({_hex(h['colour'])}) instead of a symbol: {_key(n)}",
                 "selected": f"Selected view without visible content: {_key(n)} is one colour ({_hex(h['colour'])})"
                 }[h["what"]]
        sugg = "The view has content in the tree but draws nothing or a flat surface; check drawable/tint/text colour."
    else:
        d.update(of=h["of"], outside_px=h["sides"], box_px=[round(v) for v in h["box"]])
        title = (f"{_key(n)} sticks out of its {h['of']} by "
                 + ", ".join(f"{v}px {k}" for k, v in h["sides"].items()))
        sugg = "The view is laid out partly outside its window/parent; check width/measure of the window or layout."
    rect_dp = [round(v / scale, 1) for v in rp] if h["check"] != "overflow" else [round(v, 1) for v in rp]
    d["rect_dp"] = rect_dp
    return Finding("render", key, Klass.RENDER, title, sugg, [Occurrence(phase, index, side, 1, where)], d)


def _crop_to(f: Finding, shot: Path, rect_px: list[float], out_dir: Path, img: Img) -> None:
    out = out_dir / f"{f.short_id}-atl.png"
    _crop(shot, [float(v) for v in rect_px], 1.0, (0.0, 0.0), out, lambda p: img)
    f.details["crops"] = {"atl": f"{out_dir.name}/{out.name}"}


def capture_hits(cap: Capture) -> list[tuple[Finding, Img | None, Path | None, list[float] | None]]:
    """All render findings of one capture, with what is needed to cut the picture out of its screenshot."""
    m = cap.read_manifest()
    side = m.side
    density = float(m.environment.get("density") or 1.0) if side != "atl" else 1.0
    marks = {}
    for k in cap.read_marks():
        from ..record_atl import tree_name
        marks[tree_name(k)] = k
    out = []
    for xml in sorted(cap.path(TREES).glob("*.xml")):
        name = xml.stem
        shot = cap.path(SHOTS) / f"{name}.png"
        try:
            nodes = load_tree(xml, side, normalize=False)
        except DoctorError:
            continue
        k = marks.get(name)
        phase, index = (k.name, k.index) if k else (name, 0)
        hits = overflow_hits(nodes) if side == "atl" else []
        img = None
        origin = read_origin(shot) if side == "atl" else (0.0, 0.0)
        if shot.is_file():
            try:
                img = png_read(shot)
                hits += pixel_hits(nodes, img, side, origin, density)
            except DoctorError:
                img = None
        for h in hits:
            f = _finding(h, nodes, name, phase, index, side, 1.0 if side == "atl" else density, origin)
            n = nodes[h["i"]]
            if h["check"] != "overflow":
                rect = list(h["rect_px"])
            elif n.window.startswith("activity") and n.gtk_rect is not None:
                g = n.gtk_rect
                rect = [g[0] + origin[0], g[1] + origin[1], g[2] + origin[0], g[3] + origin[1]]
            else:
                rect = None   # dialog/popup: its position in the screenshot is not recorded
            out.append((f, img, shot if shot.is_file() else None, rect))
    return out


def render_findings(cap: Capture) -> list[Finding]:
    """Findings of an ATL capture (crops go to crops/ of the capture); none for an Android capture."""
    if cap.read_manifest().side != "atl":
        return []
    out: dict[str, Finding] = {}
    for f, img, shot, rect in capture_hits(cap):
        if f.id in out:
            out[f.id].occurrences += f.occurrences
            continue
        out[f.id] = f
        if shot is None:
            continue
        if rect is None:   # the whole screenshot instead of a cut-out
            f.details["crops"] = {"atl": f"{SHOTS}/{shot.name}"}
            f.details["crop_note"] = "whole screenshot: the position of the dialog window in it is not recorded"
        elif img is not None:
            try:
                _crop_to(f, shot, rect, cap.path(CROPS), img)
            except (DoctorError, OSError):
                pass
    return list(out.values())


def android_ids(cap: Capture) -> set[str]:
    """IDs of the same checks on an Android capture: a defect that Android shows as well is not an ATL defect."""
    ids = set()
    for f, *_ in capture_hits(cap):
        ids.add(f.id)
    return ids
