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


def test_a_shot_without_its_own_tree_uses_another_tree_of_the_capture(tmp_path):
    """screenshot "x" has no tree of its own; the content origin of the window is the same as at the marks."""
    import shutil
    from pathlib import Path

    trees = Path(__file__).parent / "data/trees"
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "trees").mkdir(parents=True)
    shutil.copy(trees / "atl-main.xml", a / "trees/01-Start.xml")       # content at 10,20 in the ATL picture
    _png(a / "shots/x.png", 220, 220, lambda x, y: (255, 0, 0, 255) if y < 20 else _stripes(x - 10, y))
    _png(b / "shots/x.png", 200, 248, lambda x, y: _stripes(x, y))
    rows = compare_shots(a, b, 1.0, tmp_path / "out")
    assert rows[0]["similarity"] > 0.97


def test_atl_content_origin_is_where_gtk_put_it_not_the_java_rectangle(tmp_path):
    """bounds= is the GTK surface position (shadow and title bar included), atl-jbounds= the app's own dp."""
    from pathlib import Path

    src = (Path(__file__).parent / "data/trees/atl-main.xml").read_text()
    src = src.replace('bounds="[10,20][550,820]" atl-class="android.widget.FrameLayout"',
                      'bounds="[23,51][573,851]" atl-class="android.widget.FrameLayout"')
    src = src.replace('atl-jbounds="[10,20][550,820]"', 'atl-jbounds="[0,0][550,800]"')
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "trees").mkdir(parents=True)
    (a / "trees/01-Start.xml").write_text(src)
    _png(a / "shots/x.png", 260, 260, lambda x, y: (255, 0, 0, 255) if y < 28 else _stripes(x, y))
    (a / "shots/x.origin").write_text("-23 -23\n")                     # the surface starts 23 px off screen
    _png(b / "shots/x.png", 200, 248, lambda x, y: _stripes(x, y))
    assert compare_shots(a, b, 1.0, tmp_path / "out")[0]["similarity"] > 0.97
