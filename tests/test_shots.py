# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from atl_doctor.cli import main
from atl_doctor.errors import DoctorError
from atl_doctor.findings import Finding, Klass, Occurrence
from atl_doctor.report import Report, render_explain, render_html
from atl_doctor.sources.shots import CROP_MARGIN_DP, crop_for_finding, png_read, png_write

TREES = Path(__file__).parent / "data" / "trees"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(w: int, h: int, color_type: int, rows: list[bytes], bit_depth: int = 8, interlace: int = 0) -> bytes:
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, interlace))
            + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))


def _gradient(w: int, h: int) -> bytes:
    return b"".join(bytes((x % 256, y % 256, 7, 255)) for y in range(h) for x in range(w))


def test_png_roundtrip(tmp_path):
    rgba = bytes(range(24))
    png_write(tmp_path / "a.png", 3, 2, rgba)
    assert png_read(tmp_path / "a.png") == (3, 2, rgba)


def test_png_rgb_reads_as_rgba(tmp_path):
    p = tmp_path / "rgb.png"
    p.write_bytes(_png(2, 1, 2, [bytes([1, 2, 3, 4, 5, 6])]))
    assert png_read(p) == (2, 1, bytes([1, 2, 3, 255, 4, 5, 6, 255]))


def test_png_filters_are_undone(tmp_path):
    # sub filter (1) on one RGBA row: each byte adds the byte four to the left
    raw = b"\x01" + bytes([10, 20, 30, 40, 1, 1, 1, 1])
    p = tmp_path / "f.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
                  + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))
    assert png_read(p)[2] == bytes([10, 20, 30, 40, 11, 21, 31, 41])


@pytest.mark.parametrize("data", [b"not a png", _png(1, 1, 3, [b"\x00"]), _png(1, 1, 2, [b"\0\0\0"], interlace=1),
                                  _png(1, 1, 0, [b"\x00"], bit_depth=16)])
def test_png_unsupported_raises(tmp_path, data):
    p = tmp_path / "x.png"
    p.write_bytes(data)
    with pytest.raises(DoctorError, match="unsupported PNG|cannot read"):
        png_read(p)


def _finding(**details):
    return Finding("tree", "id/x:height", Klass.TREE_VISIBLE, "x", occurrences=[Occurrence("Main", 1, "atl")],
                   details=details)


def test_crop_rect_with_margin_and_clamp(tmp_path):
    png_write(tmp_path / "a.png", 200, 200, _gradient(200, 200))
    png_write(tmp_path / "b.png", 200, 200, _gradient(200, 200))
    f = _finding(atl=[10, 10, 50, 30], android=[10, 10, 50, 30])
    a, b = crop_for_finding(f, tmp_path / "a.png", tmp_path / "b.png", 2.0, 2.0, tmp_path / "crops")
    assert CROP_MARGIN_DP == 16
    assert a.name == f"{f.short_id}-atl.png" and b.name == f"{f.short_id}-android.png"
    w, h, px = png_read(a)
    assert (w, h) == (132, 92) and px[:4] == bytes((0, 0, 7, 255))  # x 0..132, y 0..92 of the source
    assert f.details["crops"] == {"atl": f"crops/{a.name}", "android": f"crops/{b.name}"}


def test_crop_origin_shifts_the_rectangle(tmp_path):
    png_write(tmp_path / "a.png", 200, 200, _gradient(200, 200))
    f = _finding(atl=[0, 0, 10, 10], android=[0, 0, 10, 10])
    a, _ = crop_for_finding(f, tmp_path / "a.png", tmp_path / "a.png", 1.0, 1.0, tmp_path / "c",
                            atl_origin=(50, 60), android_origin=(0, 0))
    w, h, px = png_read(a)  # dp 0..10 + margin 16 -> pixels 50-0..50+26 (x), 60..60+26 (y): the margin is clamped only at the image
    assert (w, h) == (42, 42) and px[:4] == bytes((34, 44, 7, 255))


def test_crop_missing_rect_uses_the_other_side(tmp_path):
    png_write(tmp_path / "a.png", 100, 100, _gradient(100, 100))
    f = _finding(android=[10, 10, 20, 20])  # missing on ATL: only the Android rectangle exists
    a, b = crop_for_finding(f, tmp_path / "a.png", tmp_path / "a.png", 1.0, 1.0, tmp_path / "c")
    assert png_read(a)[:2] == png_read(b)[:2] == (36, 36)


def test_crop_without_any_rect_is_none(tmp_path):
    png_write(tmp_path / "a.png", 10, 10, _gradient(10, 10))
    assert crop_for_finding(_finding(), tmp_path / "a.png", tmp_path / "a.png", 1.0, 1.0, tmp_path / "c") is None


def test_html_embeds_crops(tmp_path):
    (tmp_path / "crops").mkdir()
    for n in ("a", "b"):
        png_write(tmp_path / "crops" / f"{n}.png", 1, 1, b"\0\0\0\xff")
    f = _finding(crops={"atl": "crops/a.png", "android": "crops/b.png"})
    h = render_html(Report("record", {}, [], ["Main"], [f]), tmp_path)
    assert h.count("data:image/png;base64,") == 2 and "ATL" in h and "Android" in h


def test_text_lists_crop_paths(tmp_path):
    f = _finding(crops={"atl": "crops/a.png", "android": "crops/b.png"})
    out = render_explain(f, Report("record", {}, [], ["Main"], [f]), tmp_path)
    assert f"  {tmp_path / 'crops/a.png'}" in out and f"  {tmp_path / 'crops/b.png'}" in out


def _pair(tmp_path, with_android_shot=True):
    from atl_doctor.capture import Capture

    a = Capture.create(tmp_path / "atl", "atl")
    b = Capture.create(tmp_path / "android", "android")
    for c, side, name in ((a, "atl", "atl-main.xml"), (b, "android", "android-main.xml")):
        c.append_mark("Main")
        (c.root / "trees").mkdir()
        shutil.copy(TREES / name, c.root / "trees/01-Main.xml")
        (c.root / "shots").mkdir()
        m = c.read_manifest()
        m.ended = "2026-11-01T07:00:00+00:00"
        if side == "android":
            m.environment["density"] = "2.0"
        c.write_manifest(m)
    png_write(a.root / "shots/01-Main.png", 700, 900, _gradient(700, 900))
    (a.root / "shots/01-Main.origin").write_text("5 7\n")  # screen position of the window surface
    if with_android_shot:
        png_write(b.root / "shots/01-Main.png", 1080, 1700, _gradient(1080, 1700))
    return a.root, b.root


def test_pair_report_attaches_crops(tmp_path, capsys):
    a, b = _pair(tmp_path)
    assert main(["report", "-f", "json", str(a), str(b)]) == 0
    f = next(f for f in json.loads(capsys.readouterr().out)["findings"] if f["id"] == "tree:id/name_field:height")
    assert set(f["details"]["crops"]) == {"atl", "android"}
    atl = png_read(a / f["details"]["crops"]["atl"])
    # name_field ATL dp 100,200..300,220 (content origin GTK 10,20 + surface 5,7), margin 16: x 15+84.., y 27+184.. (pixel 99,211)
    assert atl[:2] == (232, 52) and atl[2][:4] == bytes((99, 211, 7, 255))
    droid = png_read(a / f["details"]["crops"]["android"])  # Android: dp 100,200..300,248 +16 at density 2 -> 464x160
    assert droid[:2] == (464, 160)


def test_pair_report_without_shots_has_no_crops(tmp_path, capsys):
    a, b = _pair(tmp_path, with_android_shot=False)
    assert main(["report", "-f", "json", str(a), str(b)]) == 0
    fs = json.loads(capsys.readouterr().out)["findings"]
    assert any(f["kind"] == "tree" for f in fs) and not any("crops" in f["details"] for f in fs)
