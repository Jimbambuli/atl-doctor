# SPDX-License-Identifier: GPL-3.0-or-later
"""Rendering defects from view tree + screenshot. All data here is synthetic (small trees, drawn pictures)."""
from pathlib import Path

from atl_doctor.capture import Capture
from atl_doctor.findings import Klass
from atl_doctor.report import build_capture_report, render_html, render_text
from atl_doctor.sources import render
from atl_doctor.sources.render import android_ids, contrast_ratio, render_findings
from atl_doctor.sources.shots import png_write

W, H = 400, 300
WHITE, GREY, BLACK, ORANGE, BLUE = (255, 255, 255), (221, 221, 221), (0, 0, 0), (255, 152, 0), (30, 90, 200)


class Pic:
    def __init__(self, bg=WHITE):
        self.px = bytearray(bytes(bg + (255,)) * (W * H))

    def rect(self, r, colour):
        for y in range(r[1], r[3]):
            for x in range(r[0], r[2]):
                i = (y * W + x) * 4
                self.px[i:i + 3] = bytes(colour)

    def text(self, r, colour, height=8):
        """Stand-in for glyphs: short vertical strokes in the middle of r."""
        y0 = (r[1] + r[3]) // 2 - height // 2
        for x in range(r[0] + 4, r[2] - 4, 4):
            self.rect((x, y0, x + 2, y0 + height), colour)


def node(cls, rid, rect, text="", desc="", *, selected=False, clickable=False, children="", jrect=None, shown=True):
    r = ",".join(map(str, rect[:2])) + "][" + ",".join(map(str, rect[2:]))
    j = jrect or rect
    jr = ",".join(map(str, j[:2])) + "][" + ",".join(map(str, j[2:]))
    return (f'<node text="{text}" resource-id="{rid}" class="android.widget.{cls}" content-desc="{desc}" '
            f'clickable="{str(clickable).lower()}" enabled="true" selected="{str(selected).lower()}" bounds="[{r}]" '
            f'atl-class="android.widget.{cls}" atl-alpha="1.0" atl-shown="{str(shown).lower()}" atl-gtk-rect="true" '
            f'atl-jbounds="[{jr}]">{children}</node>')


def tree(children: str, extra_windows: str = "") -> str:
    root = node("FrameLayout", "android:id/content", (0, 0, W, H), children=children)
    return (f'<hierarchy><window atl-what="activity x.Main">{root}</window>{extra_windows}</hierarchy>')


def make(tmp_path: Path, xml: str, pic: Pic, side="atl", name="01-main") -> Capture:
    cap = Capture.create(tmp_path / side, side)
    cap.append_mark("main")
    m = cap.read_manifest()
    m.environment["density"] = "1.0"
    cap.write_manifest(m)
    (tmp_path / side / "trees").mkdir()
    (tmp_path / side / "shots").mkdir()
    (tmp_path / side / "trees" / f"{name}.xml").write_text(xml, encoding="utf-8")
    png_write(tmp_path / side / "shots" / f"{name}.png", W, H, bytes(pic.px))
    (tmp_path / side / "shots" / f"{name}.origin").write_text("0 0\n")
    return cap


def by_check(fs, check):
    return [f for f in fs if f.details["check"] == check]


def test_wcag_ratio_known_values():
    assert round(contrast_ratio(BLACK, WHITE), 1) == 21.0
    assert round(contrast_ratio(WHITE, (118, 118, 118)), 1) == 4.5


def test_white_text_on_light_grey_button_is_unreadable(tmp_path):
    btn = (20, 20, 220, 60)
    pic = Pic()
    pic.rect(btn, GREY)
    pic.text(btn, WHITE)
    cap = make(tmp_path, tree(node("Button", "x:id/download", btn, text="Download", clickable=True)), pic)
    [f] = render_findings(cap)
    assert f.kind == "render" and f.klass is Klass.RENDER and f.details["check"] == "contrast"
    assert f.details["ratio"] < 1.5 and f.details["needed"] == 4.5
    assert f.details["rect_px"] == list(btn) and f.details["view"] == "id/download"
    assert "FrameLayout/Button" in f.details["path"] and f.occurrences[0].phase == "main"
    assert (cap.root / f.details["crops"]["atl"]).is_file()


def test_readable_text_is_not_reported(tmp_path):
    btn = (20, 20, 220, 60)
    pic = Pic()
    pic.rect(btn, BLUE)
    pic.text(btn, WHITE)
    cap = make(tmp_path, tree(node("Button", "x:id/ok", btn, text="OK")), pic)
    assert render_findings(cap) == []


def test_large_text_needs_only_three_to_one(tmp_path):
    grey = (148, 148, 148)
    assert 3.0 < contrast_ratio(grey, WHITE) < 4.5
    small, large = (10, 10, 210, 40), (10, 100, 210, 160)
    pic = Pic()
    pic.text(small, grey, height=8)
    pic.text(large, grey, height=30)
    xml = tree(node("TextView", "x:id/small", small, text="small") + node("TextView", "x:id/large", large, text="large"))
    got = {f.details["view"] for f in render_findings(make(tmp_path, xml, pic))}
    assert got == {"id/small"}


def test_text_not_painted_is_flat(tmp_path):
    r = (10, 10, 210, 40)
    cap = make(tmp_path, tree(node("TextView", "x:id/t", r, text="Hello")), Pic())
    [f] = render_findings(cap)
    assert f.details["check"] == "flat" and f.details["what"] == "text" and "not painted" in f.title


def test_black_square_instead_of_icon_and_real_icon(tmp_path):
    bad, good = (10, 10, 70, 70), (100, 10, 160, 70)
    pic = Pic()
    pic.rect(bad, BLACK)
    pic.rect(good, BLUE)
    pic.rect((115, 25, 145, 55), WHITE)
    xml = tree(node("ImageView", "x:id/icon", bad, desc="Warning") + node("ImageView", "x:id/real", good, desc="Ok"))
    [f] = render_findings(make(tmp_path, xml, pic))
    assert f.details["what"] == "image" and f.details["colour"] == "#000000" and f.details["view"] == "id/icon"


def test_decorative_image_without_description_is_not_reported(tmp_path):
    r = (10, 10, 70, 70)
    pic = Pic()
    pic.rect(r, BLACK)
    assert render_findings(make(tmp_path, tree(node("ImageView", "x:id/swatch", r)), pic)) == []


def test_selected_tab_orange_without_text_is_one_finding(tmp_path):
    tab = (10, 10, 130, 50)
    inner = node("TextView", "x:id/tab_text", (20, 15, 120, 45), text="Playback")
    pic = Pic()
    pic.rect(tab, ORANGE)
    xml = tree(node("LinearLayout", "x:id/tab", tab, selected=True, clickable=True, children=inner))
    [f] = render_findings(make(tmp_path, xml, pic))
    assert f.details["what"] == "selected" and f.details["colour"] == "#ff9800" and f.details["view"] == "id/tab"


def test_many_unpainted_texts_become_one_blank_finding(tmp_path):
    kids = "".join(node("TextView", f"x:id/t{i}", (10, 10 + 30 * i, 200, 35 + 30 * i), text=f"Row {i}") for i in range(5))
    [f] = render_findings(make(tmp_path, tree(kids), Pic()))
    assert f.details["what"] == "blank" and f.details["unpainted_text_views"] == 5 and "blank" in f.title


def test_overflow_out_of_window_and_scroller_exceptions(tmp_path):
    out = node("TextView", "x:id/out", (250, 10, 390, 40), text="wide", jrect=(250, 10, 450, 40))
    inside = node("TextView", "x:id/in", (10, 60, 100, 90), text="ok")
    scroll = node("HorizontalScrollView", "x:id/hs", (0, 100, 400, 140), children=node(
        "TextView", "x:id/chip", (300, 100, 390, 140), text="chip", jrect=(300, 100, 600, 140)))
    vscroll = node("ScrollView", "x:id/vs", (0, 150, 400, 290), children=node(
        "TextView", "x:id/below", (10, 250, 100, 280), text="below", jrect=(10, 250, 100, 400)))
    pic = Pic()
    pic.text((250, 10, 390, 40), BLACK)
    got = render_findings(make(tmp_path, tree(out + inside + scroll + vscroll), pic))
    [f] = by_check(got, "overflow")
    assert f.details["view"] == "id/out" and f.details["outside_px"] == {"right": 50} and f.details["of"] == "window"
    assert (tmp_path / "atl" / f.details["crops"]["atl"]).is_file()
    assert len(by_check(got, "overflow")) == 1


def test_overflow_out_of_parent_panel_in_dialog_window(tmp_path):
    kid = node("TextView", "x:id/desc", (64, 40, 392, 100), text="langer Text")
    panel = node("LinearLayout", "x:id/panel", (0, 0, 360, 200), children=kid)
    dialog = (f'<window atl-what="dialog x.Dialog">{node("FrameLayout", "android:id/content", (0, 0, 360, 200), children=panel)}</window>')
    [f] = by_check(render_findings(make(tmp_path, tree("", dialog), Pic())), "overflow")
    assert f.details["outside_px"] == {"right": 32} and f.details["view"] == "id/desc"
    assert f.details["crops"] == {"atl": "shots/01-main.png"} and "whole screenshot" in f.details["crop_note"]


def test_no_contrast_check_while_a_dialog_dims_the_activity(tmp_path):
    btn = (20, 20, 220, 60)
    pic = Pic()
    pic.rect(btn, GREY)
    pic.text(btn, WHITE)
    dialog = f'<window atl-what="dialog x.D">{node("FrameLayout", "android:id/content", (0, 0, 100, 100))}</window>'
    got = render_findings(make(tmp_path, tree(node("Button", "x:id/b", btn, text="Hello"), dialog), pic))
    assert got == []


def test_hidden_and_disabled_views_are_ignored(tmp_path):
    r = (10, 10, 210, 40)
    pic = Pic()
    gone = tree(node("TextView", "x:id/h", r, text="gone", shown=False))
    assert render_findings(make(tmp_path, gone, pic)) == []
    off = tree(node("TextView", "x:id/d", r, text="off")).replace('enabled="true"', 'enabled="false"')
    assert render_findings(make(tmp_path / "b", off, pic)) == []


def test_android_side_gives_no_findings_but_ids_for_comparison(tmp_path):
    btn = (20, 20, 220, 60)
    pic = Pic()
    pic.rect(btn, GREY)
    pic.text(btn, WHITE)
    xml = tree(node("Button", "x:id/b", btn, text="Download"))
    atl, android = make(tmp_path, xml, pic), make(tmp_path, xml, pic, side="android")
    assert render_findings(android) == []
    assert {f.id for f in render_findings(atl)} == android_ids(android) != set()


def test_report_text_html_and_note(tmp_path):
    btn = (20, 20, 220, 60)
    pic = Pic()
    pic.rect(btn, GREY)
    pic.text(btn, WHITE)
    cap = make(tmp_path, tree(node("Button", "x:id/b", btn, text="Download")), pic)
    r, root = build_capture_report(cap.root)
    assert [f.kind for f in r.findings if f.kind == "render"] == ["render"]
    html = render_html(r, root)
    assert "Rendering defects" in html and "data:image/png;base64" in html and "id/b" in html
    assert any("truncated text" in n for n in r.extras["limits"])
    assert "Unreadable text, contrast" in render_text(r)


def test_capture_without_screenshots_still_checks_overflow(tmp_path):
    cap = make(tmp_path, tree(node("TextView", "x:id/out", (250, 10, 390, 40), text="wide", jrect=(250, 10, 450, 40))), Pic())
    for p in (cap.root / "shots").iterdir():
        p.unlink()
    [f] = render_findings(cap)
    assert f.details["check"] == "overflow" and "crops" not in f.details


def test_module_names():
    assert render.WCAG_TEXT == 4.5 and render.WCAG_LARGE == 3.0


def test_blank_picture_swallows_other_flat_hits_and_mid_contrast_is_low_not_unreadable(tmp_path):
    kids = "".join(node("TextView", f"x:id/t{i}", (10, 10 + 30 * i, 200, 35 + 30 * i), text=f"Row {i}") for i in range(5))
    kids += node("ImageView", "x:id/i", (300, 10, 360, 70), desc="Picture")
    got = render_findings(make(tmp_path, tree(kids), Pic()))
    assert [f.details["what"] for f in got] == ["blank"]
    r = (10, 10, 210, 40)
    pic = Pic()
    pic.text(r, (148, 148, 148), height=8)
    [f] = render_findings(make(tmp_path / "b", tree(node("TextView", "x:id/m", r, text="medium")), pic))
    assert f.title.startswith("Low-contrast text, contrast 3.") or f.title.startswith("Low-contrast text, contrast 2.")


def test_view_covered_by_a_later_page_is_not_judged(tmp_path):
    hud = node("ImageButton", "x:id/hud", (10, 10, 60, 60), desc="Zoom")
    page = node("LinearLayout", "x:id/page", (0, 0, W, H), children=node("TextView", "x:id/t", (10, 100, 200, 130), text="Page"))
    pic = Pic()
    pic.text((10, 100, 200, 130), BLACK)
    assert render_findings(make(tmp_path, tree(hud + page), pic)) == []
    assert [f.details["what"] for f in render_findings(make(tmp_path / "b", tree(hud), pic))] == ["image"]


def test_pair_report_drops_defects_that_android_shows_too(tmp_path):
    from atl_doctor.report import build_pair_report

    btn = (20, 20, 220, 60)
    bad = Pic()
    bad.rect(btn, GREY)
    bad.text(btn, WHITE)
    good = Pic()
    good.rect(btn, BLUE)
    good.text(btn, WHITE)
    xml = tree(node("Button", "x:id/b", btn, text="Download"))
    atl = make(tmp_path / "t1", xml, bad)
    same = make(tmp_path / "t1", xml, bad, side="android")
    other = make(tmp_path / "t2", xml, good, side="android")
    r, _ = build_pair_report(atl.root, same.root, 1.0)
    assert [f for f in r.findings if f.kind == "render"] == []
    r, _ = build_pair_report(atl.root, other.root, 1.0)
    assert [f.details["check"] for f in r.findings if f.kind == "render"] == ["contrast"]


def test_report_names_whole_screen_stand_in_images(tmp_path):
    """A stand-in screenshot (.origin beside it) is marked, so a black-bordered image is not taken for the window."""
    btn = (20, 20, 220, 60)
    pic = Pic()
    pic.rect(btn, GREY)
    pic.text(btn, WHITE)
    cap = make(tmp_path, tree(node("Button", "x:id/b", btn, text="Download")), pic)
    for o in (cap.root / "shots").glob("*.origin"):  # make() writes one; a window image from ATL has none
        o.unlink()
    r, _ = build_capture_report(cap.root)
    assert "screen_shots" not in r.environment
    for png in (cap.root / "shots").glob("*.png"):
        png.with_suffix(".origin").write_text("0 0\n")
    r, _ = build_capture_report(cap.root)
    assert r.environment["screen_shots"].startswith("whole session screen, ATL gave no window image: ")
