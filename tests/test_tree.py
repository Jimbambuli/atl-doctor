# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import time
from pathlib import Path

from atl_doctor.capture import Mark
from atl_doctor.findings import Klass
from atl_doctor.sources.tree import (VIEWDUMP_REQUEST, find_node, load_tree, pair_nodes, request_atl_tree,
                                     tree_findings)

DATA = Path(__file__).parent / "data" / "trees"
PHASE = Mark(1, 0, 0, "Main")


def _trees():
    return load_tree(DATA / "android-main.xml", "android", 2.0), load_tree(DATA / "atl-main.xml", "atl")


def _by_id(fs):
    return {f.id: f for f in fs}


def _node(nodes, rid):
    return next(n for n in nodes if n.rid == rid)


def test_normalize_android_density():
    android, _ = _trees()
    assert _node(android, "id/name_field").rect == (100.0, 200.0, 300.0, 248.0)
    assert not any(n.rid == "id/clock" for n in android)  # other packages (system UI) are dropped


def test_normalize_atl_relative_to_content():
    _, atl = _trees()
    n = _node(atl, "id/save")
    assert n.rect == (100.0, 300.0, 300.0, 350.0)  # Java bounds, relative to android:id/content
    assert n.gtk_rect == (110.0, 320.0, 310.0, 340.0) and n.java_rect == (110.0, 320.0, 310.0, 370.0)
    assert n.window == "activity org.example.demo.MainActivity"


def test_pairing_by_id_then_class_text():
    android, atl = _trees()
    pairs = pair_nodes(android, atl)
    named = {(a.rid or a.desc if a else None, b.rid or b.desc if b else None) for a, b in pairs}
    assert ("id/name_field", "id/name_field") in named
    assert ("Open menu", "Open menu") in named        # no ID: class + description
    assert ("id/help", None) in named and (None, "id/debug_banner") in named


def test_height_difference_finding():
    android, atl = _trees()
    f = _by_id(tree_findings(android, atl, PHASE))["tree:id/name_field:height"]
    assert f.klass is Klass.TREE_VISIBLE
    assert f.details["delta_dp"] == 28
    assert f.details["android"] == [100.0, 200.0, 300.0, 248.0] and f.details["atl"] == [100.0, 200.0, 300.0, 220.0]
    assert [(o.phase, o.side) for o in f.occurrences] == [("Main", "atl")]


def test_missing_and_extra():
    android, atl = _trees()
    fs = _by_id(tree_findings(android, atl, PHASE))
    assert fs["tree:id/help:missing"].klass is Klass.TREE_VISIBLE
    assert fs["tree:id/debug_banner:extra"].klass is Klass.TREE_VISIBLE
    assert not any(":x" in k or ":y" in k for k in fs)  # positions match
    assert "tree:id/title:height" not in fs and "tree:ImageButton[Open menu]:x" not in fs


def test_invisible_atl_node_is_other():
    android, atl = _trees()
    fs = _by_id(tree_findings(android, atl, PHASE))
    hidden = fs["tree:id/status:missing"]   # shown on Android, hidden on ATL: the user sees it missing
    assert hidden.klass is Klass.TREE_VISIBLE and "status_box=gone" in hidden.details["atl_hidden"]
    assert fs["tree:id/status:height"].klass is Klass.OTHER   # geometry of an invisible node
    assert "tree:id/status_box:extra" not in fs


def test_webview_children_not_missing():
    android, atl = _trees()
    fs = tree_findings(android, atl, PHASE)
    assert not any("Article" in f.id for f in fs)
    assert sum(1 for n in android if n.opaque) == 2


def test_java_gtk_size_mismatch():
    android, atl = _trees()
    f = _by_id(tree_findings(android, atl, PHASE))["tree:id/save:java-gtk-size"]
    assert f.klass is Klass.TREE_VISIBLE
    assert f.details["java"] == [200.0, 50.0] and f.details["gtk"] == [200.0, 20.0]


def test_cls_text_key_without_id():
    android, atl = _trees()
    a = [n for n in android if n.desc != "Open menu"]
    fs = _by_id(tree_findings(a, atl, PHASE))
    assert "tree:ImageButton[Open menu]:extra" in fs


def test_find_node_by_text_and_id():
    _, atl = _trees()
    assert find_node(atl, "id/save").text == "Save"
    assert find_node(atl, "  PLAYLIST ").rid == "id/title"
    assert find_node(atl, "Status") is None          # hidden on ATL
    assert find_node(atl, "Open   menu").desc == "Open menu"


def test_request_atl_tree_timeout(tmp_path):
    t = time.monotonic()
    assert request_atl_tree(tmp_path, "01-Main", timeout_s=0.5) is None
    assert time.monotonic() - t < 2
    assert not (tmp_path / VIEWDUMP_REQUEST).exists()


def test_request_atl_tree_answered(tmp_path):
    def answer():  # what the ATLViewDump hook does
        req = tmp_path / VIEWDUMP_REQUEST
        while not req.exists():
            time.sleep(0.02)
        name = req.read_text().strip()
        (tmp_path / f"{name}.xml").write_text((DATA / "atl-main.xml").read_text())
        req.unlink()

    th = threading.Thread(target=answer)
    th.start()
    p = request_atl_tree(tmp_path, "01-Main", timeout_s=5)
    th.join()
    assert p == tmp_path / "01-Main.xml"


def test_request_atl_tree_serialised(tmp_path):
    """Two requests at once (a mark and a step) must both be answered."""
    stop = threading.Event()

    def hook():
        req = tmp_path / VIEWDUMP_REQUEST
        while not stop.is_set():
            if req.exists():
                time.sleep(0.1)  # the hook polls; a second writer would replace the name meanwhile
                name = req.read_text().strip()
                (tmp_path / f"{name}.xml").write_text("<hierarchy/>")
                req.unlink()
            time.sleep(0.01)

    h = threading.Thread(target=hook)
    h.start()
    res = {}
    ts = [threading.Thread(target=lambda n=n: res.setdefault(n, request_atl_tree(tmp_path, n, timeout_s=5)))
          for n in ("01-Main", "step-001")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    h.join()
    assert res["01-Main"] and res["step-001"]


def test_node_key_without_id_ignores_numbers_in_the_text():
    from atl_doctor.sources.tree import Node, _key
    a = Node("", "android.widget.TextView", "3 new messages", "", (0, 0, 1, 1), True)
    b = Node("", "android.widget.TextView", "12 new messages", "", (0, 0, 1, 1), True)
    assert _key(a) == _key(b) == "TextView[N new messages]"
