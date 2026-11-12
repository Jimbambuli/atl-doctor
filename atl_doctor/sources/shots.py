# SPDX-License-Identifier: GPL-3.0-or-later
"""Screenshots per mark and crops around view-tree differences (Spec 6.7).

PNG handling uses only zlib/struct: spectacle and screencap write 8-bit RGB or RGBA without interlacing.
A screenshot of the ATL side has a sidecar `NN-name.origin` with the screen position of the GTK window
surface (KWin bufferGeometry, includes the client-side shadow); GTK coordinates of the tree are relative to it.
"""
from __future__ import annotations

import glob
import struct
import sys
import zlib
from pathlib import Path

from ..capture import SHOTS, TREES, Capture
from ..errors import DoctorError
from ..findings import Finding

CROP_MARGIN_DP = 16
CROPS = "crops"
_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {2: 3, 6: 4}


def png_read(path: Path) -> tuple[int, int, bytes]:
    """(width, height, RGBA bytes); 8 bit, colour type 2 or 6, no interlace."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise DoctorError(f"cannot read {path}: {e.strerror}") from e
    if not data.startswith(_SIG):
        raise DoctorError(f"unsupported PNG {path}: bad signature")
    pos, ihdr, idat = len(_SIG), None, []
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + n
    if ihdr is None:
        raise DoctorError(f"unsupported PNG {path}: no header")
    w, h, depth, ctype, _, _, interlace = ihdr
    if depth != 8 or ctype not in _CHANNELS or interlace:
        raise DoctorError(f"unsupported PNG {path}: bit depth {depth}, colour type {ctype}, interlace {interlace}")
    ch = _CHANNELS[ctype]
    stride = w * ch
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise DoctorError(f"unsupported PNG {path}: {e}") from e
    if len(raw) < h * (stride + 1):
        raise DoctorError(f"unsupported PNG {path}: truncated data")
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        ft, row = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if ft > 4:
            raise DoctorError(f"unsupported PNG {path}: filter {ft}")
        for i in range(stride):
            a = row[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if ft == 1:
                row[i] = (row[i] + a) & 255
            elif ft == 2:
                row[i] = (row[i] + b) & 255
            elif ft == 3:
                row[i] = (row[i] + (a + b) // 2) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = row
        if ch == 4:
            out += row
        else:
            for x in range(w):
                out += row[x * 3:x * 3 + 3] + b"\xff"
    return w, h, bytes(out)


def png_write(path: Path, width: int, height: int, rgba: bytes) -> None:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def origin_file(png: Path) -> Path:
    return Path(png).with_suffix(".origin")


def write_origin(png: Path, origin: tuple[float, float]) -> None:
    origin_file(png).write_text(f"{origin[0]:g} {origin[1]:g}\n", encoding="utf-8")


def read_origin(png: Path) -> tuple[float, float]:
    """Screen position of the GTK surface when the screenshot was taken (0, 0 without a sidecar)."""
    try:
        x, y = origin_file(png).read_text(encoding="utf-8").split()
        return float(x), float(y)
    except (OSError, ValueError):
        return 0.0, 0.0


def move_atl_window_shot(trees_dir: Path, name: str, png: Path, session=None) -> bool:
    """ATL's own image of the app window: ATLViewDump writes trees_dir/shots/NAME.png together with the tree
    (only the window with the popups shown on it, not the screen, no portal). Moves it to PNG; False if ATL did
    not write one. Further toplevels (dialogs) come as NAME.N.png and go to PNG's name .N.png; with a SESSION
    they are also laid over PNG where KWin shows them."""
    src = Path(trees_dir) / SHOTS / f"{name}.png"
    if not src.is_file() or src.stat().st_size == 0:
        return False
    png = Path(png)
    png.parent.mkdir(parents=True, exist_ok=True)
    src.replace(png)
    extra = {}
    for p in (Path(trees_dir) / SHOTS).glob(f"{glob.escape(name)}.*.png"):
        k = p.name[len(name) + 1:-4]
        if k.isdigit():
            extra[int(k)] = png.with_name(f"{png.stem}.{k}.png")
            p.replace(extra[int(k)])
    if extra and session is not None:
        try:
            compose_toplevels(png, extra, Path(trees_dir) / f"{name}.xml", session.windows())
        except (DoctorError, OSError) as e:
            print(f"atl-doctor: {png.name}: dialogs not laid over the window image: {e}", file=sys.stderr)
    return True


def compose_toplevels(png: Path, extra: dict[int, Path], tree: Path, windows: list[dict]) -> list[int]:
    """Lay the images of further toplevels (EXTRA: toplevel -> image) over the main window's image PNG at the
    place KWin shows them; the tree tells each toplevel's surface size and where its image begins in the
    surface. Returns the toplevels laid over; one whose KWin window is not found by its size stays apart."""
    import xml.etree.ElementTree as ET

    from ..headless import toplevel_window
    from .tree import surface_size

    try:
        wins = ET.parse(tree).getroot().iter("window")
    except (OSError, ET.ParseError) as e:
        raise DoctorError(f"cannot read view tree {tree}: {e}") from e
    surf: dict[int, tuple] = {}
    for e in wins:
        try:
            ox, oy = (float(v) for v in e.get("atl-surface-offset", "").split(","))
            surf.setdefault(int(e.get("atl-toplevel", "-1")), (surface_size(e), (ox, oy)))
        except ValueError:
            continue
    main = next((w for w in windows if w["kind"] == "normal"), None)
    if main is None or 0 not in surf:
        return []
    w, h, px = png_read(png)
    out = bytearray(px)
    done = []
    for k, path in sorted(extra.items()):
        win = toplevel_window(windows, surf[k][0]) if k in surf else None
        if win is None:
            print(f"atl-doctor: {png.name}: no KWin window for toplevel {k}, it stays apart ({path.name})",
                  file=sys.stderr)
            continue
        dx = round(win["surface"][0] + surf[k][1][0] - main["surface"][0] - surf[0][1][0])
        dy = round(win["surface"][1] + surf[k][1][1] - main["surface"][1] - surf[0][1][1])
        ew, eh, epx = png_read(path)
        for y in range(max(0, -dy), min(eh, h - dy)):
            for x in range(max(0, -dx), min(ew, w - dx)):
                i, j = ((y + dy) * w + x + dx) * 4, (y * ew + x) * 4
                a = epx[j + 3]
                if a == 255:
                    out[i:i + 4] = epx[j:j + 4]
                elif a:
                    for c in range(3):
                        out[i + c] = (epx[j + c] * a + out[i + c] * (255 - a)) // 255
                    out[i + 3] = max(out[i + 3], a)
        done.append(k)
    if done:
        png_write(png, w, h, bytes(out))
    return done


def atl_window_shot(trees_dir: Path, name: str, png: Path, session=None) -> bool:
    """Ask ATLViewDump for a tree and (explicitly) a window image under NAME and move the image to PNG."""
    from .tree import request_atl_tree

    return (request_atl_tree(trees_dir, name, shot=True) is not None
            and move_atl_window_shot(trees_dir, name, png, session))


def take_atl_shot(session, png: Path) -> bool:
    """Stand-in when ATL gave no window image: screenshot of the whole headless session screen plus the surface
    origin of the main window (the .origin file marks such an image). Said on stderr, the image looks different."""
    from ..cli import log

    ws = session.windows()
    main = next((w for w in ws if w["kind"] == "normal"), ws[0] if ws else None)
    ok = session.screenshot(png)
    if ok:
        write_origin(png, main["surface"] if main else (0.0, 0.0))
        log(0, f"{png.name}: ATL gave no window image, a screenshot of the whole session screen stands in")
    return ok


def screen_shots(shots_dir: Path) -> list[str]:
    """Names of the images in SHOTS_DIR that are whole-screen stand-ins (take_atl_shot), not window images."""
    return sorted(p.stem for p in Path(shots_dir).glob("*.png") if origin_file(p).is_file())


def _crop(path: Path, rect_dp: list[float], scale: float, origin: tuple[float, float], out: Path,
          read=None) -> None:
    w, h, px = (read or png_read)(path)
    m = CROP_MARGIN_DP
    x0 = max(0, min(w, round(origin[0] + (rect_dp[0] - m) * scale)))
    y0 = max(0, min(h, round(origin[1] + (rect_dp[1] - m) * scale)))
    x1 = max(x0, min(w, round(origin[0] + (rect_dp[2] + m) * scale)))
    y1 = max(y0, min(h, round(origin[1] + (rect_dp[3] + m) * scale)))
    rows = b"".join(px[(y * w + x0) * 4:(y * w + x1) * 4] for y in range(y0, y1))
    png_write(out, x1 - x0, y1 - y0, rows)


def crop_for_finding(f: Finding, atl_png: Path, android_png: Path, atl_scale: float, android_density: float,
                     out_dir: Path, atl_origin: tuple[float, float] = (0.0, 0.0),
                     android_origin: tuple[float, float] = (0.0, 0.0), read=None) -> tuple[Path, Path] | None:
    """Crop the view's rectangle (plus margin) from both screenshots. A side without a rectangle (the view is
    missing there) is cut at the other side's place. Origins are where dp 0,0 lies in each image, in pixels."""
    atl_r, and_r = f.details.get("atl"), f.details.get("android")
    if not (atl_r or and_r):
        return None
    out_dir = Path(out_dir)
    a = out_dir / f"{f.short_id}-atl.png"
    b = out_dir / f"{f.short_id}-android.png"
    _crop(Path(atl_png), atl_r or and_r, atl_scale, atl_origin, a, read)
    _crop(Path(android_png), and_r or atl_r, android_density, android_origin, b, read)
    f.details["crops"] = {"atl": f"{out_dir.name}/{a.name}", "android": f"{out_dir.name}/{b.name}"}
    return a, b


def attach_crops(findings: list[Finding], atl_cap: Capture, android_cap: Capture, density: float | None = None) -> None:
    """Crops for every tree difference whose mark has a screenshot on both sides (paired by mark name).
    Files go to crops/ of the ATL capture; trouble with one finding never stops the report."""
    from ..record_atl import tree_name
    from .exceptions import phases_by_name
    from .tree import content_origin

    atl_marks, and_marks = atl_cap.read_marks(), android_cap.read_marks()
    pairs = phases_by_name([m.name for m in and_marks])
    atl_idx = {i: pairs[k] for k, i in phases_by_name([m.name for m in atl_marks]).items() if k in pairs}
    if density is None:
        density = float(android_cap.read_manifest().environment.get("density") or 1.0)
    images: dict[Path, tuple[int, int, bytes]] = {}

    def read(path: Path) -> tuple[int, int, bytes]:  # decode each screenshot once, not once per finding
        if path not in images:
            images[path] = png_read(path)
        return images[path]

    for f in findings:
        if f.kind != "tree" or not f.occurrences:
            continue
        i = f.occurrences[0].phase_index
        if i not in atl_idx:
            continue
        a_name, b_name = tree_name(atl_marks[i]), tree_name(and_marks[atl_idx[i]])
        a_png, b_png = atl_cap.path(SHOTS) / f"{a_name}.png", android_cap.path(SHOTS) / f"{b_name}.png"
        a_xml, b_xml = atl_cap.path(TREES) / f"{a_name}.xml", android_cap.path(TREES) / f"{b_name}.xml"
        if not all(p.is_file() for p in (a_png, b_png, a_xml, b_xml)):
            continue
        try:
            gx, gy = content_origin(a_xml, "atl")
            sx, sy = read_origin(a_png)
            crop_for_finding(f, a_png, b_png, 1.0, density, atl_cap.root / CROPS, (sx + gx, sy + gy),
                             content_origin(b_xml, "android"), read)
        except (DoctorError, OSError) as e:
            print(f"atl-doctor: no crop for {f.short_id}: {e}", file=sys.stderr)
