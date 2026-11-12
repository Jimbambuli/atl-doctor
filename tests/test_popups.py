# SPDX-License-Identifier: GPL-3.0-or-later
"""Popups, menus and dialogs under ATL: windows of the tree, clicks into them, wait-popup, window images."""
import io
from pathlib import Path

import pytest

from atl_doctor.sources.shots import compose_toplevels, move_atl_window_shot, png_read, png_write
from atl_doctor.sources.tree import find_node, load_tree
from atl_doctor.steps import parse_steps, run_steps

_NODE = ('<node index="0" text="{text}" resource-id="{rid}" class="android.widget.TextView" content-desc="" '
         'clickable="true" enabled="true" bounds="{b}" atl-class="x" atl-visibility="visible" atl-alpha="1.0" '
         'atl-shown="true" atl-gtk-rect="true" atl-jbounds="{b}" />')
_CONTENT = ('<node index="0" text="" resource-id="android:id/content" class="android.widget.FrameLayout" '
            'content-desc="" bounds="[14,59][1347,812]" atl-class="x" atl-visibility="visible" atl-alpha="1.0" '
            'atl-shown="true" atl-gtk-rect="true" atl-jbounds="[14,59][1347,812]">{}</node>')
TREE = ("<hierarchy rotation=\"0\" atl=\"1\">"
        "<window atl-what=\"activity org.example.Main\" atl-toplevel=\"0\" atl-surface-size=\"1361x824\" "
        "atl-surface-offset=\"14,12\">" + _CONTENT.format(_NODE.format(text="Settings", rid="", b="[20,70][120,90]"))
        + "</window>"
        "<window atl-what=\"gtk GtkPopoverMenu\" atl-toplevel=\"0\" atl-surface-size=\"1361x824\" "
        "atl-surface-offset=\"14,12\">" + _NODE.format(text="Settings", rid="", b="[1200,100][1300,120]") + "</window>"
        "<window atl-what=\"dialog android.app.AlertDialog\" atl-toplevel=\"1\" atl-surface-size=\"400x200\" "
        "atl-surface-offset=\"0,0\">" + _NODE.format(text="OK", rid="android:id/button1", b="[300,150][380,190]")
        + "</window></hierarchy>")


class _Session:
    def windows(self):
        return [{"caption": "Main", "resource_class": "x", "kind": "normal", "rect": (0.0, 0.0, 1333.0, 800.0),
                 "surface": (-14.0, -12.0), "size": (1361, 824)},
                {"caption": "", "resource_class": "x", "kind": "dialog", "rect": (466.0, 312.0, 400.0, 200.0),
                 "surface": (466.0, 312.0), "size": (400, 200)}]


def _tree(tmp_path) -> Path:
    p = tmp_path / "t.xml"
    p.write_text(TREE, encoding="utf-8")
    return p


def test_tree_tells_window_order_and_toplevel(tmp_path):
    nodes = load_tree(_tree(tmp_path), "atl")
    menu = next(n for n in nodes if n.window.startswith("gtk"))
    ok = next(n for n in nodes if n.rid == "id/button1")
    assert (menu.window_index, menu.toplevel, menu.surface_size) == (1, 0, (1361, 824))
    assert (ok.window_index, ok.toplevel, ok.surface_size) == (2, 1, (400, 200))


def test_find_node_takes_the_topmost_window(tmp_path):
    """The menu item, not the activity's view with the same text below the menu."""
    assert find_node(load_tree(_tree(tmp_path), "atl"), "Settings").window == "gtk GtkPopoverMenu"


def _driver(tmp_path, monkeypatch, tree_path, cap_dir="c"):
    import atl_doctor.sources.tree as tree
    from atl_doctor.capture import Capture
    from atl_doctor.steps import AtlDriver

    cap = Capture.create(tmp_path / cap_dir, "atl")
    (tmp_path / cap_dir / "trees").mkdir()
    monkeypatch.setattr(tree, "request_atl_tree", lambda d, name, timeout_s=10.0: tree_path)
    d = AtlDriver(cap, _Session(), tmp_path / "fifo", tmp_path / "status", has_tree=True, frames=False)
    sent = []
    monkeypatch.setattr(d, "_send", sent.append)
    return d, sent


def test_click_into_a_gtk_menu_and_into_a_dialog(tmp_path, monkeypatch):
    d, sent = _driver(tmp_path, monkeypatch, _tree(tmp_path))
    d.click(*d.locate("Settings"))   # the menu is on the main surface: -14 + 1250, -12 + 110
    d.click(*d.locate("id/button1"))  # the dialog's KWin window (found by its size) is at 466,312: + 340,170
    assert sent == ["click 1236 98", "click 806 482"]


def test_wait_popup_polls_until_a_window_besides_the_activity(tmp_path, monkeypatch):
    only = tmp_path / "only.xml"
    only.write_text(TREE.split("<window atl-what=\"gtk")[0] + "</hierarchy>", encoding="utf-8")
    trees = [only, only, _tree(tmp_path)]
    import atl_doctor.sources.tree as tree
    import atl_doctor.steps as steps

    d, _ = _driver(tmp_path, monkeypatch, only)
    monkeypatch.setattr(tree, "request_atl_tree", lambda dd, name, timeout_s=10.0: trees.pop(0))
    monkeypatch.setattr(steps, "POPUP_POLL_S", 0)
    run_steps(parse_steps("wait-popup 5", "s"), d, "s", io.StringIO())
    assert trees == []
    d2, _ = _driver(tmp_path, monkeypatch, only, "c2")
    with pytest.raises(steps.StepError, match="s:1: no popup, menu or dialog within 0.2 s"):
        run_steps(parse_steps("wait-popup 0.2", "s"), d2, "s", io.StringIO())


def test_wait_popup_parses_with_default_timeout():
    assert parse_steps("wait-popup", "s")[0].args == (10.0,)
    with pytest.raises(Exception, match="wait-popup expects an optional timeout"):
        parse_steps("wait-popup soon", "s")


def _solid(path, w, h, rgba):
    png_write(path, w, h, bytes(rgba) * (w * h))


def test_extra_toplevel_images_are_kept_and_laid_over(tmp_path):
    """shots/NAME.1.png goes next to the window image; the dialog lands where KWin shows it."""
    trees = tmp_path / "trees"
    (trees / "shots").mkdir(parents=True)
    (trees / "01-Menu.xml").write_text(TREE.replace("400x200", "2x2"), encoding="utf-8")
    _solid(trees / "shots/01-Menu.png", 4, 3, (0, 0, 0, 255))
    _solid(trees / "shots/01-Menu.1.png", 2, 2, (255, 0, 0, 255))

    class S:  # main surface -14,-12 with the image at 14,12 in it; the dialog at 1,1 on screen
        def windows(self):
            return [{"kind": "normal", "surface": (-14.0, -12.0), "size": (32, 27)},
                    {"kind": "dialog", "surface": (1.0, 1.0), "size": (2, 2)}]

    png = tmp_path / "shots/01-Menu.png"
    assert move_atl_window_shot(trees, "01-Menu", png, S())
    assert png_read(tmp_path / "shots/01-Menu.1.png")[:2] == (2, 2)
    w, h, px = png_read(png)
    red = [(x, y) for y in range(h) for x in range(w) if px[(y * w + x) * 4] == 255]
    assert red == [(1, 1), (2, 1), (1, 2), (2, 2)]


def test_dialog_without_its_kwin_window_stays_apart(tmp_path):
    tree = _tree(tmp_path)
    _solid(tmp_path / "m.png", 4, 3, (0, 0, 0, 255))
    _solid(tmp_path / "m.1.png", 2, 2, (255, 0, 0, 255))
    wins = [{"kind": "normal", "surface": (0.0, 0.0), "size": (1361, 824)}]
    assert compose_toplevels(tmp_path / "m.png", {1: tmp_path / "m.1.png"}, tree, wins) == []
    assert all(v == 0 for i, v in enumerate(png_read(tmp_path / "m.png")[2]) if i % 4 == 0)
