# SPDX-License-Identifier: GPL-3.0-or-later
from atl_doctor.sources.shotdiff import compare_shots
from atl_doctor.sources.shots import png_write


def _png(path, w, h, pixel):
    png_write(path, w, h, b"".join(bytes(pixel(x, y)) for y in range(h) for x in range(w)))


def _stripes(x, y):
    return (255, 255, 255, 255) if (x // 20) % 2 else (0, 0, 0, 255)


def _white(x, y):
    return (250, 250, 250, 255)


def test_same_picture_is_similar_and_density_is_honoured(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _png(a / "shots/suche.png", 200, 160, _stripes)
    _png(b / "shots/suche.png", 400, 320 + 2 * 24, lambda x, y: _stripes(x // 2, (y - 48) // 2))  # density 2, status bar
    rows = compare_shots(a, b, 2.0, tmp_path / "out")
    assert [r["step"] for r in rows] == ["suche"]
    assert rows[0]["similarity"] > 0.97 and rows[0]["finding"] == ""
    assert (tmp_path / "out/suche-diff.png").is_file()


def test_empty_atl_picture_is_flagged_and_sorted_first(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for n in ("gut", "leer"):
        _png(b / f"shots/{n}.png", 200, 200, _stripes)
    _png(a / "shots/gut.png", 200, 200, _stripes)
    _png(a / "shots/leer.png", 200, 200, _white)
    _png(a / "shots/nur-atl.png", 200, 200, _white)
    rows = compare_shots(a, b, 1.0, tmp_path / "out")
    assert [r["step"] for r in rows] == ["leer", "gut"]
    assert rows[0]["finding"] == "ATL empty" and rows[0]["similarity"] < 0.7 and rows[0]["different"] > 0.3


def test_no_shots_no_rows(tmp_path):
    assert compare_shots(tmp_path / "a", tmp_path / "b", 1.0, tmp_path / "out") == []
