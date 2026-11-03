# SPDX-License-Identifier: GPL-3.0-or-later
"""Screenshots of ATL and Android compared step by step (screenshots with the same name in shots/).

Taken over from the experiment tool bildvergleich.py, without PIL: both pictures are cut to the app content in dp
(ATL: window surface origin plus content origin from the view tree of that name, else from any other tree;
Android: content origin from a view tree likewise, without the system bars: status and navigation bar heights from the
view tree (statusBarBackground, navigationBarBackground), else from the insets recorded at capture time
(manifest `insets`, `dumpsys window`), else default 24/48 dp; the row's `area` says which), reduced to grey cells of CELL_DP and compared over
the common area. Similarity is 1 - mean grey difference / 255, `different` the share of cells that differ by more
than STRONG; "ATL empty" means the ATL content is almost one colour while Android's is not.
"""
from __future__ import annotations

from pathlib import Path

from ..capture import SHOTS, TREES, Capture
from ..errors import DoctorError
from .shots import png_read, png_write, read_origin

CELL_DP = 4
STRONG = 48
EMPTY_STDDEV = 4.0
STATUS_BAR_DP = 24
NAV_BAR_DP = 48


def _content_origin(xml: Path, side: str) -> tuple[float, float]:
    """Android: the tree's content origin. ATL: where GTK put android:id/content in the window surface (`bounds`,
    with shadow and title bar), not the app's own rectangle (`atl-jbounds`)."""
    from .tree import content_origin, load_tree

    if side != "atl":
        return content_origin(xml, side)
    c = next((n for n in load_tree(xml, "atl", normalize=False) if n.full_id == "android:id/content"), None)
    return (c.gtk_rect[0], c.gtk_rect[1]) if c else (0.0, 0.0)


def _origin(cap: Path, name: str, side: str) -> tuple[float, float] | None:
    own = cap / TREES / f"{name}.xml"   # else another tree: the window's content origin does not move
    for xml in [own] * own.is_file() + sorted((cap / TREES).glob("*.xml")):
        try:
            return _content_origin(xml, side)
        except DoctorError:
            continue
    return None


def _tree_bars(cap: Path, name: str) -> tuple[int | None, int | None]:
    """(status bar, navigation bar) height in px from the first tree of the capture that has the bar."""
    from .tree import load_tree

    own = cap / TREES / f"{name}.xml"
    top = bottom = None
    for xml in [own] * own.is_file() + sorted((cap / TREES).glob("*.xml")):
        try:
            nodes = load_tree(xml, "android", normalize=False)
        except DoctorError:
            continue
        for n in nodes:
            if top is None and n.full_id == "android:id/statusBarBackground":
                top = round(n.rect[3] - n.rect[1])
            if bottom is None and n.full_id == "android:id/navigationBarBackground":
                bottom = round(n.rect[3] - n.rect[1])
        if top is not None and bottom is not None:
            break
    return top, bottom


def _bars(cap: Path, name: str, density: float) -> tuple[float, float, str]:
    """Height of the Android status and navigation bar in px, and where it comes from (`area` text)."""
    top, bottom = _tree_bars(cap, name)
    src = "tree"
    if top is None or bottom is None:
        try:
            rec = Capture.open(cap).read_manifest().environment.get("insets", "")
            t, b = (int(v) for v in rec.split(","))
        except (DoctorError, ValueError):
            t = b = None
        if t is not None:
            top, bottom, src = (t if top is None else top), (b if bottom is None else bottom), "recorded"
    if top is None or bottom is None:
        top = round(STATUS_BAR_DP * density) if top is None else top
        bottom = round(NAV_BAR_DP * density) if bottom is None else bottom
        src = "default dp"
    return float(top), float(bottom), src


def _cells(img: tuple[int, int, bytes], ox: float, oy: float, scale: float, cols: int, rows: int) -> list[float]:
    """Mean grey of each CELL_DP cell (2x2 samples per cell)."""
    w, h, px = img
    out = []
    for r in range(rows):
        for c in range(cols):
            g = 0.0
            for fx, fy in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)):
                x = min(w - 1, int(ox + (c + fx) * CELL_DP * scale))
                y = min(h - 1, int(oy + (r + fy) * CELL_DP * scale))
                i = (y * w + x) * 4
                g += (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) / 1000
            out.append(g / 4)
    return out


def _stddev(v: list[float]) -> float:
    m = sum(v) / len(v)
    return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5


def _diff_png(path: Path, a: list[float], b: list[float], cols: int, rows: int) -> None:
    """ATL | Android | difference, one pixel per cell."""
    rgba = bytearray()
    for r in range(rows):
        for row in (a, b, [abs(x - y) for x, y in zip(a, b)]):
            for c in range(cols):
                g = int(row[r * cols + c])
                rgba += bytes((g, g, g, 255))
    png_write(path, cols * 3, rows, bytes(rgba))


def compare_shots(atl_dir: Path, android_dir: Path, density: float, out_dir: Path) -> list[dict]:
    """One row per screenshot name present on both sides, worst first; writes out_dir/<name>-diff.png."""
    atl_dir, android_dir = Path(atl_dir), Path(android_dir)
    names = sorted(p.stem for p in (atl_dir / SHOTS).glob("*.png") if (android_dir / SHOTS / p.name).is_file())
    rows = []
    for n in names:
        a_png, b_png = atl_dir / SHOTS / f"{n}.png", android_dir / SHOTS / f"{n}.png"
        try:
            a, b = png_read(a_png), png_read(b_png)
        except DoctorError:
            continue
        sx, sy = read_origin(a_png)
        gx, gy = _origin(atl_dir, n, "atl") or (0.0, 0.0)
        ax, ay = sx + gx, sy + gy
        sb, nb, src = _bars(android_dir, n, density)
        bx, by = _origin(android_dir, n, "android") or (0.0, 0.0)
        if by < sb:   # edge-to-edge: the status bar overlays the content's top, leave that band out on both sides
            skip = -(-int((sb - by) / density * 1000) // (CELL_DP * 1000)) * CELL_DP
            ay, by = ay + skip, by + skip * density
        w_dp = min(a[0] - ax, (b[0] - bx) / density)
        h_dp = min(a[1] - ay, (b[1] - by - nb) / density)
        cols, nrows = int(w_dp // CELL_DP), int(h_dp // CELL_DP)
        if cols < 1 or nrows < 1:
            continue
        ca, cb = _cells(a, ax, ay, 1.0, cols, nrows), _cells(b, bx, by, density, cols, nrows)
        d = [abs(x - y) for x, y in zip(ca, cb)]
        empty = _stddev(ca) < EMPTY_STDDEV <= _stddev(cb)
        rows.append({"step": n, "similarity": round(1 - sum(d) / len(d) / 255, 3),
                     "different": round(sum(1 for x in d if x > STRONG) / len(d), 3),
                     "finding": "ATL empty" if empty else "",
                     "area": f"Android without status bar {sb:g} px and navigation bar {nb:g} px ({src})"})
        _diff_png(Path(out_dir) / f"{n}-diff.png", ca, cb, cols, nrows)
    rows.sort(key=lambda r: (r["similarity"], r["step"]))
    return rows
