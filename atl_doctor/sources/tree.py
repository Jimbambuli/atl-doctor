# SPDX-License-Identifier: GPL-3.0-or-later
"""View trees: Android (`uiautomator dump`) against ATL (ATLViewDump), normalised to dp.

Both trees are put into dp relative to the origin of android:id/content (Android pixels / density,
ATL pixels 1:1). Visible on Android means: the node is in the dump (uiautomator only lists what the
user sees). On ATL this is rebuilt: isShown, total alpha > 0.05, GTK widget mapped, area > 0 after
clipping by all ancestors. Contents of WebViews (DOM nodes) and Compose views (semantics nodes) only
exist on Android; they are counted, never reported as missing.
"""
from __future__ import annotations

import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ..capture import Mark
from ..errors import DoctorError
from ..findings import Finding, Klass, Occurrence, normalize_text

OPAQUE = ("android.webkit.WebView", "androidx.compose.ui.platform.AndroidComposeView",
          "androidx.compose.ui.platform.ComposeView")
ENVIRONMENT_IDS = ("android:id/statusBarBackground", "android:id/navigationBarBackground")
SIZE_MIN_FRACTION = 0.2  # a size difference also has to exceed 20 % of the Android size
# request file name of the existing ATLViewDump patch; one place, so an upstream name is easy to follow
VIEWDUMP_REQUEST = "anfrage"
_REQUEST_LOCK = threading.Lock()

Rect = tuple[float, float, float, float]


@dataclass
class Node:
    rid: str          # short resource id "id/foo", "" without
    cls: str          # class as uiautomator reports it
    text: str
    desc: str
    rect: Rect        # dp, relative to android:id/content (ATL: Java layout, clipped by ancestors)
    visible: bool
    java_rect: Rect | None = None   # ATL only: Java layout bounds (pixels, window)
    gtk_rect: Rect | None = None    # ATL only: GTK widget bounds (pixels, window)
    window: str = ""
    real_cls: str = ""              # ATL: the app's own class (atl-class)
    clickable: bool = False
    parent: int | None = None       # index into the node list
    children: int = 0
    opaque: bool = False            # inside a WebView/Compose view
    hidden_reason: str = ""
    clip_v: bool = False            # clipped by an ancestor at the top or bottom edge
    clip_h: bool = False
    scrollable: bool = False
    full_id: str = field(default="", repr=False)


def _rect(s: str) -> Rect:
    a, b = s.strip("[]").split("][")
    left, top = map(float, a.split(","))
    right, bottom = map(float, b.split(","))
    return left, top, right, bottom


def _cut(a: Rect, b: Rect) -> Rect:
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


def _area(r: Rect) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def short_id(full: str) -> str:
    return "id/" + full.split(":id/", 1)[1] if ":id/" in full else full


def _norm(s: str) -> str:
    return " ".join(s.split())


def load_tree(path: Path, side: str, density: float | None = None, normalize: bool = True) -> list[Node]:
    """Nodes of one dump in document order, in dp relative to android:id/content (normalize=False: raw pixels)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        raise DoctorError(f"cannot read view tree {path}: {e}") from e
    atl = side == "atl"
    scale = 1.0 if atl or not density else density
    nodes: list[Node] = []
    package = None
    if not atl:  # keep the app's nodes only (uiautomator also dumps the system UI)
        content = next((e for e in root.iter("node") if e.get("resource-id") == "android:id/content"), None)
        package = content.get("package") if content is not None else None

    def walk(elem: ET.Element, parent: int | None, window: str, clip: Rect | None, alpha: float, opaque: bool) -> None:
        for k in elem:
            if k.tag == "window":
                walk(k, None, k.get("atl-what", ""), None, 1.0, False)
                continue
            if k.tag != "node" or (package and k.get("package", package) != package):
                continue
            a = k.attrib
            full = "" if a.get("resource-id", "") == "0x0" else a.get("resource-id", "")
            n = Node(short_id(full), a.get("class", ""), _norm(a.get("text", "")), _norm(a.get("content-desc", "")),
                     _rect(a.get("bounds", "[0,0][0,0]")), True, window=window, real_cls=a.get("atl-class", ""),
                     clickable=a.get("clickable") == "true", parent=parent, opaque=opaque, full_id=full)
            n.real_cls = n.real_cls or n.cls
            new_clip, new_alpha = None, 1.0
            if atl:
                new_alpha = alpha * float(a.get("atl-alpha", "1"))
                n.gtk_rect = n.rect
                whole = _rect(a.get("atl-jbounds", a.get("bounds", "[0,0][0,0]")))
                n.java_rect = whole
                c = whole if clip is None else _cut(whole, clip)
                if a.get("atl-shown") != "true":
                    n.visible, n.hidden_reason = False, "not shown (" + a.get("atl-hidden-by", "?") + ")"
                elif new_alpha < 0.05:
                    n.visible, n.hidden_reason = False, f"alpha {new_alpha:.2f}"
                elif a.get("atl-gtk-rect") != "true":
                    n.visible, n.hidden_reason = False, "GTK widget not mapped"
                elif _area(whole) == 0:
                    n.visible, n.hidden_reason = False, f"size 0 ({whole[2] - whole[0]:.0f}x{whole[3] - whole[1]:.0f})"
                elif _area(c) == 0:
                    n.visible, n.hidden_reason = False, "outside its parents (clipped)"
                if not n.visible and a.get("atl-animation"):
                    n.hidden_reason += ", pending animation " + a["atl-animation"].split(".")[-1]
                n.rect = c if _area(c) else whole
                n.clip_v = c[1] != whole[1] or c[3] != whole[3]
                n.clip_h = c[0] != whole[0] or c[2] != whole[2]
                new_clip = c
            else:
                # uiautomator clips bounds to the visible part: an edge on the edge of a scrollable
                # ancestor means the node is probably cut off there
                p = parent
                while p is not None:
                    e = nodes[p]
                    if e.scrollable:
                        n.clip_v |= abs(n.rect[1] - e.rect[1]) <= 2 or abs(n.rect[3] - e.rect[3]) <= 2
                        n.clip_h |= abs(n.rect[0] - e.rect[0]) <= 2 or abs(n.rect[2] - e.rect[2]) <= 2
                    p = e.parent
                n.scrollable = a.get("scrollable") == "true"
            if parent is not None:
                nodes[parent].children += 1
            nodes.append(n)
            walk(k, len(nodes) - 1, window, new_clip, new_alpha,
                 opaque or n.cls in OPAQUE or n.real_cls in OPAQUE)

    walk(root, None, "", None, 1.0, False)
    if normalize:
        _normalize(nodes, scale)
    return nodes


def _origin(nodes: list[Node]) -> tuple[float, float]:
    content = next((n for n in nodes if n.full_id == "android:id/content"), None)
    ox, oy = (content.rect[0], content.rect[1]) if content else (0.0, 0.0)
    # edge-to-edge Android: the status bar lies over the content, the app is inset by its height
    sb = next((n for n in nodes if n.full_id == "android:id/statusBarBackground"), None)
    if sb and sb.rect[3] > oy:
        oy = sb.rect[3]
    return ox, oy


def content_origin(path: Path, side: str) -> tuple[float, float]:
    """Where dp 0,0 lies in the dump's own pixels (Android: screen, ATL: GTK window surface)."""
    return _origin(load_tree(path, side, normalize=False))


def _normalize(nodes: list[Node], scale: float) -> None:
    ox, oy = _origin(nodes)
    for n in nodes:
        r = n.rect
        n.rect = ((r[0] - ox) / scale, (r[1] - oy) / scale, (r[2] - ox) / scale, (r[3] - oy) / scale)


def _content_size(nodes: list[Node]) -> tuple[float, float]:
    c = next((n for n in nodes if n.full_id == "android:id/content"), None)
    return (c.rect[2] - c.rect[0], c.rect[3] - c.rect[1]) if c else (0.0, 0.0)


def _norm_class(c: str) -> str:
    c = c.split(".")[-1]
    return {"AppCompatTextView": "TextView", "MaterialTextView": "TextView", "AppCompatImageView": "ImageView",
            "AppCompatImageButton": "ImageButton", "MaterialButton": "Button", "AppCompatButton": "Button",
            "FloatingActionButton": "ImageButton", "AppCompatEditText": "EditText",
            "TextInputEditText": "EditText"}.get(c, c)


def _meaningful(n: Node) -> bool:
    return bool(n.rid or n.text or n.desc) and n.full_id not in ENVIRONMENT_IDS


def _centre(n: Node) -> tuple[float, float]:
    return (n.rect[0] + n.rect[2]) / 2, (n.rect[1] + n.rect[3]) / 2


_STAGES = [
    lambda n: (n.rid, _norm_class(n.cls), n.text, n.desc) if n.rid else None,
    lambda n: (n.rid, _norm_class(n.cls)) if n.rid else None,
    lambda n: (n.rid,) if n.rid else None,
    lambda n: ("", _norm_class(n.cls), n.text, n.desc) if not n.rid and (n.text or n.desc) else None,
    lambda n: ("", n.text or n.desc) if not n.rid and (n.text or n.desc) else None,
    lambda n: ("", _norm_class(n.cls), (n.text or n.desc)[:25]) if not n.rid and (n.text or n.desc) else None,
    # last: clickable nodes without ID of the same class at the same place (at most 24 dp apart)
    lambda n: ("", _norm_class(n.cls), "click") if not n.rid and n.clickable else None,
]


def _partners(android: list[Node], atl: list[Node]) -> dict[int, int]:
    """Android index -> ATL index."""
    pa: dict[int, int] = {}
    taken: set[int] = set()
    open_a = [i for i, n in enumerate(android) if (_meaningful(n) or n.clickable) and not n.opaque]
    for only_visible in (True, False):
        for stage in _STAGES:
            index: dict[tuple, list[int]] = {}
            for j, m in enumerate(atl):
                if j in taken or m.opaque or (only_visible and not m.visible):
                    continue
                k = stage(m)
                if k is not None:
                    index.setdefault(k, []).append(j)
            for i in open_a:
                if i in pa:
                    continue
                k = stage(android[i])
                cand = [j for j in index.get(k, []) if j not in taken] if k is not None else []
                if not cand:
                    continue
                cx, cy = _centre(android[i])
                j = min(cand, key=lambda j: (_centre(atl[j])[0] - cx) ** 2 + (_centre(atl[j])[1] - cy) ** 2)
                if stage is _STAGES[-1] and (_centre(atl[j])[0] - cx) ** 2 + (_centre(atl[j])[1] - cy) ** 2 > 24 ** 2:
                    continue
                pa[i] = j
                taken.add(j)
    return pa


def pair_nodes(android: list[Node], atl: list[Node]) -> list[tuple[Node | None, Node | None]]:
    """Pairs (Android, ATL) of the meaningful nodes; unpaired ones with None on the other side."""
    pa = _partners(android, atl)
    paired = set(pa.values())
    out: list[tuple[Node | None, Node | None]] = [
        (n, atl[pa[i]] if i in pa else None) for i, n in enumerate(android)
        if (_meaningful(n) or n.clickable) and not n.opaque]
    out += [(None, m) for j, m in enumerate(atl) if j not in paired and _meaningful(m) and not m.opaque]
    return out


def _key(n: Node) -> str:
    if n.rid:
        return n.rid
    return f"{_norm_class(n.real_cls or n.cls)}[{normalize_text(n.text or n.desc)[:40]}]"  # "3 new" = "5 new"


def _r(n: Node) -> list[float]:
    return [round(v, 1) for v in n.rect]


def _fmt(n: Node) -> str:
    r = n.rect
    return f"{r[0]:.0f},{r[1]:.0f} {r[2] - r[0]:.0f}x{r[3] - r[1]:.0f} dp"


def _median(xs: list[float], default: float) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else default


def _is_ancestor(nodes: list[Node], anc: int, k: int) -> bool:
    p = nodes[k].parent
    while p is not None:
        if p == anc:
            return True
        p = nodes[p].parent
    return False


def _topmost(nodes: list[Node], idx: list[int]) -> list[int]:
    s = set(idx)
    return [i for i in idx if nodes[i].parent not in s]


def tree_findings(android: list[Node], atl: list[Node], phase: Mark, threshold_dp: float = 8.0) -> list[Finding]:
    """Differences between the Android and the ATL tree of one mark."""
    pa = _partners(android, atl)
    ga, gt = _content_size(android), _content_size(atl)
    occ = [Occurrence(phase.name, phase.index, "atl", 1, "")]
    out: list[Finding] = []

    def add(n: Node, attr: str, klass: Klass, title: str, **details: object) -> None:
        f = Finding("tree", f"{_key(n)}:{attr}", klass, title, occurrences=list(occ), details=dict(details))
        out.append(f)

    def opaque_children(i: int) -> dict:
        n = sum(1 for k in range(len(android)) if android[k].opaque and _is_ancestor(android, i, k))
        return {"opaque_children": n} if n else {}

    leaf_pairs = [(i, j) for i, j in pa.items() if atl[j].visible and android[i].children == 0]
    mdx = _median([atl[j].rect[0] - android[i].rect[0] for i, j in leaf_pairs], 0.0)
    mdy = _median([atl[j].rect[1] - android[i].rect[1] for i, j in leaf_pairs if android[i].rect[1] < 0.5 * ga[1]], 0.0)
    # bottom-anchored elements (bottom bars, video controls) follow the bottom edge
    mdb = _median([atl[j].rect[3] - android[i].rect[3] for i, j in leaf_pairs if android[i].rect[3] > 0.75 * ga[1]], mdy)

    missing = []
    for i, n in enumerate(android):
        if n.opaque or not _meaningful(n):
            continue
        if i not in pa:
            missing.append(i)
            continue
        m = atl[pa[i]]
        if not m.visible:
            add(n, "missing", Klass.TREE_VISIBLE, f"{_key(n)} is hidden on ATL: {m.hidden_reason}",
                atl=_r(m), android=_r(n), atl_hidden=m.hidden_reason, **opaque_children(i))
        # position relative to the nearest paired visible ancestor (removes status bar, task bar and window
        # size); without one relative to the common offset (top or bottom anchored)
        va = n.parent
        while va is not None and not (va in pa and atl[pa[va]].visible and _is_ancestor(atl, pa[va], pa[i])):
            va = android[va].parent
        if va is not None:
            a0, t0 = android[va].rect, atl[pa[va]].rect
            dx = min(m.rect[0] - t0[0] - (n.rect[0] - a0[0]), m.rect[2] - t0[2] - (n.rect[2] - a0[2]), key=abs)
            dy = min(m.rect[1] - t0[1] - (n.rect[1] - a0[1]), m.rect[3] - t0[3] - (n.rect[3] - a0[3]), key=abs)
        else:
            dx = m.rect[0] - n.rect[0] - mdx
            dy = min(m.rect[1] - n.rect[1] - mdy, m.rect[3] - n.rect[3] - mdb, key=abs)
        wa, ha = n.rect[2] - n.rect[0], n.rect[3] - n.rect[1]
        wt, ht = m.rect[2] - m.rect[0], m.rect[3] - m.rect[1]
        if n.rect[1] < -1 or (ht >= 0.85 * gt[1] and ha >= 0.85 * ga[1]):
            continue  # window containers follow the window size
        horiz, vert = not (n.clip_h or m.clip_h), not (n.clip_v or m.clip_v)
        klass = Klass.TREE_VISIBLE if m.visible else Klass.OTHER
        checks = [("x", horiz and abs(dx) > threshold_dp, dx, "position x"),
                  ("y", vert and abs(dy) > threshold_dp, dy, "position y"),
                  ("width", horiz and abs(wt - wa) > threshold_dp and abs(wt - wa) > SIZE_MIN_FRACTION * max(wa, 1),
                   wt - wa, "width"),
                  ("height", vert and abs(ht - ha) > threshold_dp and abs(ht - ha) > SIZE_MIN_FRACTION * max(ha, 1),
                   ht - ha, "height")]
        for attr, hit, delta, what in checks:
            if hit:
                add(n, attr, klass, f"{_key(n)}: {what} differs by {delta:+.0f} dp (ATL {_fmt(m)}, Android {_fmt(n)})"
                    + ("" if m.visible else " [hidden on ATL]"),
                    atl=_r(m), android=_r(n), delta_dp=round(abs(delta), 1), **opaque_children(i))
    for i in _topmost(android, missing):
        n = android[i]
        add(n, "missing", Klass.TREE_VISIBLE, f"{_key(n)} is missing on ATL (Android {_fmt(n)})",
            android=_r(n), **opaque_children(i))

    paired = set(pa.values())
    extra = [j for j, m in enumerate(atl) if j not in paired and m.visible and not m.opaque and _meaningful(m)
             and m.cls not in OPAQUE and m.window.startswith(("activity", "dialog", "wm"))]
    for j in _topmost(atl, extra):
        m = atl[j]
        add(m, "extra", Klass.TREE_VISIBLE, f"{_key(m)} is only on ATL ({_fmt(m)})", atl=_r(m))

    # ATL only: the GTK allocation differs from the Java layout (widget drawn smaller or larger than measured)
    for m in atl:
        if not (m.visible and not m.opaque and _meaningful(m) and m.java_rect and m.gtk_rect):
            continue
        jw, jh = m.java_rect[2] - m.java_rect[0], m.java_rect[3] - m.java_rect[1]
        gw, gh = m.gtk_rect[2] - m.gtk_rect[0], m.gtk_rect[3] - m.gtk_rect[1]
        if max(abs(gw - jw), abs(gh - jh)) > threshold_dp / 2:
            add(m, "java-gtk-size", Klass.TREE_VISIBLE,
                f"{_key(m)}: GTK size {gw:.0f}x{gh:.0f} differs from Java layout {jw:.0f}x{jh:.0f}",
                java=[jw, jh], gtk=[gw, gh])
    return out


def find_node(nodes: list[Node], selector: str) -> Node | None:
    """First visible node matching `id/foo` or a text/description (case and whitespace insensitive)."""
    if selector.startswith("id/"):
        return next((n for n in nodes if n.visible and n.rid == selector), None)
    want = _norm(selector).casefold()
    return next((n for n in nodes if n.visible and want in (n.text.casefold(), n.desc.casefold())), None)


def request_atl_tree(trees_dir: Path, name: str, timeout_s: float = 10.0) -> Path | None:
    """Ask the ATLViewDump hook for a dump into trees_dir/NAME.xml; None if none arrives in time."""
    with _REQUEST_LOCK:  # one request file: a mark's tree and a step's lookup must not overwrite each other
        return _request(Path(trees_dir), name, timeout_s)


def _request(trees_dir: Path, name: str, timeout_s: float) -> Path | None:
    out = trees_dir / f"{name}.xml"
    req = trees_dir / VIEWDUMP_REQUEST
    out.unlink(missing_ok=True)
    tmp = trees_dir / (VIEWDUMP_REQUEST + ".tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, req)  # the hook must never read a half-written name
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not req.exists() and out.is_file() and out.stat().st_size > 0:
            return out
        time.sleep(0.05)
    req.unlink(missing_ok=True)
    return None
