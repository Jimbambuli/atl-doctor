# SPDX-License-Identifier: GPL-3.0-or-later
"""Screenshots of ATL and Android compared step by step (screenshots with the same name in shots/).

Taken over from the experiment tool bildvergleich.py, without PIL: both pictures are cut to the app content in dp
(ATL: window surface origin plus content origin from the view tree; Android: content origin from the view tree, else
below a 24 dp status bar, and without a 48 dp navigation bar), reduced to grey cells of CELL_DP and compared over
the common area. Similarity is 1 - mean grey difference / 255, `different` the share of cells that differ by more
than STRONG; "ATL empty" means the ATL content is almost one colour while Android's is not.
"""
from __future__ import annotations

from pathlib import Path

from ..capture import SHOTS, TREES
from ..errors import DoctorError
from .shots import png_read, png_write, read_origin

CELL_DP = 4
STRONG = 48
EMPTY_STDDEV = 4.0
STATUS_BAR_DP = 24
NAV_BAR_DP = 48


def _origin(cap: Path, name: str, side: str) -> tuple[float, float] | None:
    from .tree import content_origin

    xml = cap / TREES / f"{name}.xml"
    try:
        return content_origin(xml, side) if xml.is_file() else None
    except DoctorError:
        return None


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
        tree = _origin(android_dir, n, "android")
        bx, by = tree or (0.0, STATUS_BAR_DP * density)
        w_dp = min(a[0] - ax, (b[0] - bx) / density)
        h_dp = min(a[1] - ay, (b[1] - by) / density - (0 if tree else NAV_BAR_DP))
        cols, nrows = int(w_dp // CELL_DP), int(h_dp // CELL_DP)
        if cols < 1 or nrows < 1:
            continue
        ca, cb = _cells(a, ax, ay, 1.0, cols, nrows), _cells(b, bx, by, density, cols, nrows)
        d = [abs(x - y) for x, y in zip(ca, cb)]
        empty = _stddev(ca) < EMPTY_STDDEV <= _stddev(cb)
        rows.append({"step": n, "similarity": round(1 - sum(d) / len(d) / 255, 3),
                     "different": round(sum(1 for x in d if x > STRONG) / len(d), 3),
                     "finding": "ATL empty" if empty else ""})
        _diff_png(Path(out_dir) / f"{n}-diff.png", ca, cb, cols, nrows)
    rows.sort(key=lambda r: (r["similarity"], r["step"]))
    return rows
