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
    _png(a / "shots/search.png", 200, 160, _stripes)
    _png(b / "shots/search.png", 400, 320 + 2 * 24, lambda x, y: _stripes(x // 2, (y - 48) // 2))  # density 2, status bar
    rows = compare_shots(a, b, 2.0, tmp_path / "out")
    assert [r["step"] for r in rows] == ["search"]
    assert rows[0]["similarity"] > 0.97 and rows[0]["finding"] == ""
    assert (tmp_path / "out/search-diff.png").is_file()


def test_empty_atl_picture_is_flagged_and_sorted_first(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for n in ("good", "empty"):
        _png(b / f"shots/{n}.png", 200, 200, _stripes)
    _png(a / "shots/good.png", 200, 200, _stripes)
    _png(a / "shots/empty.png", 200, 200, _white)
    _png(a / "shots/only-atl.png", 200, 200, _white)
    rows = compare_shots(a, b, 1.0, tmp_path / "out")
    assert [r["step"] for r in rows] == ["empty", "good"]
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


def _android_tree(path, bars):
    node = '<node resource-id="%s" class="android.view.View" package="p" bounds="%s"/>'
    inner = "".join(node % b for b in bars)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<?xml version="1.0"?><hierarchy rotation="0"><node resource-id="android:id/content" '
                    'class="android.widget.FrameLayout" package="p" bounds="[0,0][200,200]">' + inner +
                    "</node></hierarchy>")


def _bars_png(path):
    """Android 200x200: red bars (24 px top, 40 px bottom) around the 200x136 stripe content."""
    _png(path, 200, 200, lambda x, y: (255, 0, 0, 255) if y < 24 or y >= 160 else _stripes(x, y - 24))


def test_system_bars_from_the_tree_are_not_compared(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _png(a / "shots/s.png", 200, 136, _stripes)
    _bars_png(b / "shots/s.png")
    _android_tree(b / "trees/s.xml", [("android:id/statusBarBackground", "[0,0][200,24]"),
                                      ("android:id/navigationBarBackground", "[0,160][200,200]")])
    r = compare_shots(a, b, 1.0, tmp_path / "out")[0]
    assert r["similarity"] > 0.97
    assert "24" in r["area"] and "40" in r["area"] and "tree" in r["area"]


def test_system_bars_from_the_recorded_insets(tmp_path):
    import json
    a, b = tmp_path / "a", tmp_path / "b"
    _png(a / "shots/s.png", 200, 136, _stripes)
    _bars_png(b / "shots/s.png")
    (b / "manifest.json").write_text(json.dumps({"format": 1, "environment": {"insets": "24,40"}}))
    r = compare_shots(a, b, 1.0, tmp_path / "out")[0]
    assert r["similarity"] > 0.97 and "recorded" in r["area"]


def test_without_any_bar_data_the_default_dp_is_used_and_said_so(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _png(a / "shots/s.png", 200, 136, _stripes)
    _png(b / "shots/s.png", 200, 200, _stripes)
    assert "default" in compare_shots(a, b, 1.0, tmp_path / "out")[0]["area"]


def test_parse_insets_from_dumpsys_window():
    from atl_doctor.record_android import parse_insets
    out = ("InsetsSource id=1 type=statusBars frame=[0,0][2000,36] visible=true\n"
           "InsetsSource id=2 type=navigationBars frame=[0,1110][2000,1200] visible=true\n"
           "InsetsSource id=3 type=ime frame=[0,0][0,0] visible=false\n")
    assert parse_insets(out) == (36, 90)
    assert parse_insets("nothing") is None
