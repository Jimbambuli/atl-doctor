# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import shutil
from pathlib import Path

import pytest

from atl_doctor import android_input as ai
from atl_doctor import recorded_input as bd
from atl_doctor.capture import Capture
from atl_doctor.cli import main
from atl_doctor.sources.exceptions import parse_agent_lines
from atl_doctor.sources.tree import Node, load_tree
from atl_doctor.steps import StepError, parse_steps, run_steps

DATA = Path(__file__).parent / "data" / "trees"
T_LINE = ("T\t1000\t7\ttap\torg.example.demo.MainActivity\tandroidx.appcompat.view.menu.ActionMenuItemView\t"
          "org.example.demo:id/save\t\tSave\t10\t20\t110\t70\t0/1/2")


def node(rid="", text="", desc="", rect=(0, 0, 100, 50), parent=None, clickable=False, full="", cls="android.widget.Button",
         visible=True):
    return Node(rid, cls, text, desc, rect, visible, parent=parent, clickable=clickable,
                full_id=full or (f"org.example.demo:{rid}" if rid else ""))


def tree():
    """content > [bar > [up, save], list > [row0 > [label], row1 > [label]]]"""
    return [node(rid="id/content", full="android:id/content", rect=(0, 0, 800, 600)),
            node(rid="id/bar", parent=0, rect=(0, 0, 800, 60)),
            node(rid="id/up", desc="Up", parent=1, rect=(0, 0, 60, 60), clickable=True),
            node(rid="id/save", text="Save", parent=1, rect=(700, 0, 800, 60), clickable=True),
            node(rid="id/list", parent=0, rect=(0, 60, 800, 600)),
            node(parent=4, rect=(0, 60, 800, 160), clickable=True),
            node(text="Alpha", parent=5, rect=(10, 70, 200, 100), cls="android.widget.TextView"),
            node(parent=4, rect=(0, 160, 800, 260), clickable=True),
            node(text="Beta", parent=7, rect=(10, 170, 200, 200), cls="android.widget.TextView")]


# ------------------------------------------------------------------ format
def test_roundtrip_header_events_sorted_and_numbered(tmp_path):
    h = bd.make_header("atl", "org.example.demo", "agent")
    ev = [bd.make_event("back", 20, "A"), bd.make_event("tap", 10, "A", bd.make_target("p:id/x", "B", "T", "", "0/1", (1, 2, 3, 4)), (2, 3))]
    p = tmp_path / "input.jsonl"
    bd.write_input(p, h, ev)
    head, rows = bd.read_input(p)
    assert head == h and [r["seq"] for r in rows] == [1, 2] and [r["kind"] for r in rows] == ["tap", "back"]
    assert rows[0]["target"]["rect"] == [1.0, 2.0, 3.0, 4.0] and rows[0]["point"] == [2.0, 3.0]
    assert bd.read_input(tmp_path)[1] == rows   # the capture directory itself


def test_text_is_only_a_length_unless_full():
    plain = bd.make_event("text", 1, text="secret")
    full = bd.make_event("text", 1, text="secret", full=True)
    assert plain["text"] == {"len": 6} and "secret" not in json.dumps(plain)
    assert full["text"] == {"len": 6, "value": "secret"}


@pytest.mark.parametrize("cls", ["android.widget.EditText", "androidx.appcompat.widget.AppCompatAutoCompleteTextView",
                                 "androidx.appcompat.widget.SearchView$SearchAutoComplete"])
def test_editable_target_text_is_dropped_unless_full(cls):
    assert bd.make_target("", cls, "typed", "")["text"] == ""
    assert bd.make_target("", cls, "typed", "", full=True)["text"] == "typed"
    assert bd.make_target("", "android.widget.TextView", "label", "")["text"] == "label"


def test_unknown_kind_and_bad_files(tmp_path):
    with pytest.raises(bd.InputError):
        bd.make_event("swipe", 1)
    p = tmp_path / "x.jsonl"
    for body, msg in (("", "header"), ('{"kind": "tap", "t_us": 1}\n', "header"), ("not json\n", "not JSON"),
                      ('{"format": 99, "kind": "header"}\n', "newer"),
                      ('{"format": 1, "kind": "header"}\n{"kind": "tap"}\n', "not an input event")):
        p.write_text(body)
        with pytest.raises(bd.InputError, match=msg):
            bd.read_input(p)
    with pytest.raises(bd.InputError, match="cannot read"):
        bd.read_input(tmp_path / "missing.jsonl")


# ------------------------------------------------------------------ ATL: agent lines
def test_events_from_agent_lines():
    ev = parse_agent_lines([T_LINE, T_LINE.replace("tap", "long-tap", 1).replace("\t10\t20\t110\t70\t0/1/2", "\t\t\t\t\t-"),
                            "L\t2000\t7\tA\tonBackPressed\torg.example.demo.MainActivity\tx\t01\ty",
                            "L\t2100\t7\tA\tonResume\torg.example.demo.MainActivity\tx\t01\ty", "T\t1\t2\tbroken"])
    out = bd.events_from_agent(ev)
    assert [e["kind"] for e in out] == ["tap", "long-tap", "back"]
    assert out[0]["target"] == {"id": "org.example.demo:id/save", "cls": "androidx.appcompat.view.menu.ActionMenuItemView",
                                "text": "", "desc": "Save", "path": "0/1/2", "rect": [10.0, 20.0, 110.0, 70.0]}
    assert out[0]["point"] == [60.0, 45.0] and out[0]["activity"] == "org.example.demo.MainActivity"
    assert out[1]["target"]["rect"] is None and "point" not in out[1]   # no rectangle: no point either


def test_agent_never_reports_edit_text_content_and_writer_skips_without_clicks(tmp_path):
    cap = Capture.create(tmp_path / "c", "atl")
    assert bd.write_atl_input(cap) == 0 and not cap.path(bd.INPUT_FILE).exists()
    line = T_LINE.replace("androidx.appcompat.view.menu.ActionMenuItemView", "android.widget.EditText").replace("\t\tSave", "\tsecret\tSave")
    cap.path("agent.tsv").write_text(line + "\n")
    assert bd.write_atl_input(cap, "org.example.demo") == 1
    head, rows = bd.read_input(cap.root)
    assert head["side"] == "atl" and head["source"] == "agent" and head["package"] == "org.example.demo"
    assert rows[0]["target"]["text"] == ""   # editable: dropped on this side as well


# ------------------------------------------------------------------ trees: path, target, resolution
def test_node_path_counts_children_below_content():
    n = tree()
    assert bd.node_path(n, 3) == "0/1" and bd.node_path(n, 8) == "1/1/0" and bd.node_path(n, 0) == "-"
    assert bd.node_path([node(), node(parent=0)], 1) == "-"   # no content view above


def test_target_at_prefers_the_smallest_clickable_and_labels_from_below():
    n = tree()
    t = bd.target_at(n, 750, 30)
    assert t["id"] == "org.example.demo:id/save" and t["text"] == "Save" and t["path"] == "0/1" and t["rect"] == [700.0, 0.0, 800.0, 60.0]
    row = bd.target_at(n, 50, 80)   # the clickable row without text of its own: label of the first text below it
    assert row["id"] == "" and row["text"] == "Alpha" and row["path"] == "1/0"
    assert bd.target_at(n, 5000, 5000) is None
    assert bd.target_at(n, 30, 30)["desc"] == "Up"


def test_resolve_order_id_text_path_rect_none():
    n = tree()
    rect = [700.0, 0.0, 800.0, 60.0]
    assert bd.resolve_target(n, "org.example.demo:id/save", "other", "9/9", rect)[1] == "id"
    node_, how = bd.resolve_target(n, "p:id/gone", "Save", "9/9", rect)
    assert how == "text" and node_.rid == "id/save"
    assert bd.resolve_target(n, "", "Up", "", None)[0].rid == "id/up"   # description counts as text
    assert bd.resolve_target(n, "", "alpha", "", None)[0].clickable   # label below, clickable row wins, case ignored
    node_, how = bd.resolve_target(n, "", "gone", "0/1", rect)
    assert how == "path" and node_.rid == "id/save"
    assert bd.resolve_target(n, "", "gone", "0/0", rect) == (None, "rect")   # path leads to a view far from the rectangle
    assert bd.resolve_target(n, "", "gone", "7/7", rect) == (None, "rect")   # path that does not exist
    assert bd.resolve_target(n, "", "gone", "", None) == (None, "none")


def test_resolve_ignores_invisible_nodes_and_takes_the_nearest_of_several():
    n = tree() + [node(rid="id/save", text="Save", rect=(0, 300, 100, 360), parent=0, visible=False)]
    assert bd.resolve_target(n, "p:id/save", "", "", None)[0].rect == (700, 0, 800, 60)
    twice = tree() + [node(rid="id/save", text="Save", rect=(0, 300, 100, 360), parent=0, clickable=True)]
    assert bd.resolve_target(twice, "p:id/save", "", "", [0, 290, 100, 350])[0].rect == (0, 300, 100, 360)
    assert bd.resolve_target(twice, "p:id/save", "", "", [690, 0, 800, 60])[0].rect == (700, 0, 800, 60)


def test_target_at_and_resolve_on_a_real_uiautomator_dump():
    android = load_tree(DATA / "android-main.xml", "android", 2.0)
    t = bd.target_at(android, 150, 220)   # inside id/name_field (an EditText)
    assert t["id"].endswith(":id/name_field") and t["text"] == "" and t["cls"] == "android.widget.EditText"
    assert bd.resolve_target(android, t["id"], t["text"], t["path"], t["rect"])[1] == "id"


# ------------------------------------------------------------------ scenario from a recording
def _recording():
    tg = bd.make_target("org.example.demo:id/save", "android.widget.Button", "Say \"hi\"", "", "0/1", (700, 0, 800, 60))
    return bd.make_header("android", "org.example.demo", "getevent"), [
        bd.make_event("tap", 1_000_000, "org.example.demo.MainActivity", tg, (750, 30)),
        bd.make_event("long-tap", 1_100_000, "org.example.demo.MainActivity", bd.make_target(desc="Up", rect=(0, 0, 60, 60))),
        bd.make_event("text", 3_000_000, "org.example.demo.MainActivity", text="secret"),
        bd.make_event("text", 4_000_000, "org.example.demo.MainActivity", text="open", full=True),
        bd.make_event("key", 9_000_000, key="Return"),
        bd.make_event("back", 60_000_000, "org.example.demo.MainActivity"),
        bd.make_event("scroll", 61_000_000, scroll=(0, 312.4)),
        bd.make_event("tap", 62_000_000, point=(5, 6)),
        bd.make_event("tap", 63_000_000)]


def test_scenario_from_golden_and_parses(assert_golden):
    text = bd.scenario_from(*_recording(), name="android-run/input.jsonl")
    assert_golden(text, "scenario-from.steps")
    assert "secret" not in text and 'type "xxxxxx"' in text and 'type "open"' in text
    steps = parse_steps(text, "s")
    assert [s.cmd for s in steps].count("tap") == 2 and any(s.cmd == "long-tap" for s in steps)
    assert not any("wait 6" in ln for ln in text.splitlines() if "wait 60" in ln)   # a long pause is cut to 10 s
    assert "wait 10.0" in text


def test_scenario_has_no_waits_for_close_events_and_no_package_means_no_window_wait():
    h, ev = bd.make_header("atl", "", "agent"), [bd.make_event("back", 1_000_000), bd.make_event("back", 1_100_000)]
    text = bd.scenario_from(h, ev)
    assert "wait" not in text and text.count("key Escape") == 2


def test_cli_scenario_from(tmp_path, capsys):
    cap = tmp_path / "rec"
    cap.mkdir()
    bd.write_input(cap / "input.jsonl", *_recording())
    assert main(["scenario-from", str(cap)]) == 0
    assert capsys.readouterr().out.startswith("# made by `atl-doctor scenario-from`")
    out = tmp_path / "s.steps"
    assert main(["scenario-from", "-o", str(out), str(cap / "input.jsonl")]) == 0
    assert out.read_text().count("tap ") >= 2
    assert main(["scenario-from", str(tmp_path / "nothing")]) == 1
    assert "cannot read" in capsys.readouterr().err


# ------------------------------------------------------------------ the step commands
def test_tap_step_parses_all_target_forms():
    s = parse_steps('tap id/a "Some text" path:0/1/2 rect:1,2.5,3,4\nlong-tap "X"\ntap rect:0,0,1,1', "s")
    assert s[0].args == ("id/a", "Some text", "0/1/2", "1,2.5,3,4") and s[1].cmd == "long-tap" and s[1].args == ("", "X", "", "")
    assert s[2].args == ("", "", "", "0,0,1,1")


@pytest.mark.parametrize("line", ["tap", "tap foo", "tap path:a/b", "tap rect:1,2,3", "long-tap rect:1,2,3,x"])
def test_tap_step_errors_carry_the_line(line):
    with pytest.raises(StepError) as e:
        parse_steps("\n" + line, "f.steps")
    assert str(e.value).startswith("f.steps:2:")


class TapDriver:
    def __init__(self, nodes, has_tree=True):
        self.nodes, self.has_tree, self.calls = nodes, has_tree, []

    def tree_nodes(self): return self.nodes
    def node_pos(self, n): return ((n.rect[0] + n.rect[2]) / 2, (n.rect[1] + n.rect[3]) / 2)
    def click(self, x, y): self.calls.append(("click", x, y))
    def long_press(self, x, y): self.calls.append(("long", x, y))
    def key(self, c): self.calls.append(("key", c))
    def type_text(self, s): self.calls.append(("type", s))
    def wait_idle(self, t): return True
    def snapshot(self, name):
        p = self.root / "trees" / f"{name}.xml"
        p.parent.mkdir(exist_ok=True)
        p.write_text("<hierarchy/>")
        return p


def test_tap_runs_find_or_fall_back_and_write_replay_rows(tmp_path):
    d = TapDriver(tree())
    d.root = tmp_path
    steps = parse_steps('tap id/save rect:0,0,1,1\nlong-tap "Alpha"\ntap "Gone" rect:10,10,30,50\ntap "Gone"\nkey Return\ntype "x"', "s")
    rp = io.StringIO()
    run_steps(steps, d, "s", io.StringIO(), replay=rp)
    rows = [json.loads(ln) for ln in rp.getvalue().splitlines()]
    assert [(r["cmd"], r["found"], r["by"]) for r in rows] == [
        ("tap", True, "id"), ("long-tap", True, "text"), ("tap", False, "rect"), ("tap", False, "none"),
        ("key", None, ""), ("type", None, "")]
    assert [r["n"] for r in rows] == [1, 2, 3, 4, 5, 6] and rows[2]["tree"] == "trees/replay-003.xml"
    assert d.calls == [("click", 750.0, 30.0), ("long", 400.0, 110.0), ("click", 20.0, 30.0), ("key", "Return"), ("type", "x")]


def test_tap_without_a_tree_fails_the_step_with_its_line():
    d = TapDriver([], has_tree=False)
    with pytest.raises(StepError) as e:
        run_steps(parse_steps("\ntap id/x", "s"), d, "s", io.StringIO())
    assert str(e.value).startswith("s:2: tap needs a view tree")


# ------------------------------------------------------------------ Android: getevent
def _lines(*groups):
    out = []
    for t, dev, rows in groups:
        out += [f"[{t:12.6f}] /dev/input/event{dev}: {typ:<12} {code:<20} {val}" for typ, code, val in rows]
    return out


def _touch(t0, t1, x0, y0, x1=None, y1=None, *, send_x=True, send_y=True):
    first = [("EV_ABS", "ABS_MT_TRACKING_ID", "00000000")]
    first += [("EV_ABS", "ABS_MT_POSITION_X", f"{x0:08x}")] * send_x + [("EV_ABS", "ABS_MT_POSITION_Y", f"{y0:08x}")] * send_y
    first += [("EV_SYN", "SYN_REPORT", "00000000")]
    move = []
    if x1 is not None:
        move = [(t0 + (t1 - t0) / 2, 2, [("EV_ABS", "ABS_MT_POSITION_X", f"{x1:08x}"), ("EV_ABS", "ABS_MT_POSITION_Y", f"{y1:08x}"),
                                         ("EV_SYN", "SYN_REPORT", "00000000")])]
    end = [("EV_ABS", "ABS_MT_TRACKING_ID", "ffffffff"), ("EV_SYN", "SYN_REPORT", "00000000")]
    return _lines((t0, 2, first), *move, (t1, 2, end))


def tracker():
    return ai.Tracker(32767, 32767, 2000, 1200, 1.5)  # the tracker test uses 1.5 dp per px


def feed(t, lines, base=1_000):
    for i, ln in enumerate(lines):
        t.feed(ln, base + i)
    return t.take()


def test_touch_ranges_from_getevent_pl():
    text = ("add device 1: /dev/input/event13\n  name: \"qwerty2\"\n"
            "add device 2: /dev/input/event12\n  name: \"touch\"\n  events:\n    ABS (0003): ABS_MT_SLOT : value 0, min 0, max 10\n"
            "                ABS_MT_POSITION_X     : value 0, min 0, max 32767, fuzz 0, flat 0\n"
            "                ABS_MT_POSITION_Y     : value 0, min 0, max 16383, fuzz 0, flat 0\n")
    assert ai.touch_ranges(text) == (32767, 16383)
    assert ai.touch_ranges("add device 1: /dev/input/event1\n") is None


def test_tracker_tap_long_tap_and_scroll():
    g = feed(tracker(), _touch(10.0, 10.1, 16384, 8192))
    assert [x["kind"] for x in g] == ["tap"] and round(g[0]["x0"]) == 1000 and round(g[0]["y0"]) == 300
    assert feed(tracker(), _touch(10.0, 10.8, 16384, 8192))[0]["kind"] == "long-tap"
    g = feed(tracker(), _touch(10.0, 10.3, 16384, 16384, 16384, 8192))[0]   # 300 px up
    assert g["kind"] == "scroll" and round(g["y0"] - g["y1"]) == 300
    assert feed(tracker(), _touch(10.0, 10.9, 16384, 8192, 16384 + 100, 8192))[0]["kind"] == "long-tap"   # 3 dp: still a stay


def test_tracker_reports_only_changed_axes_so_a_missing_one_means_unknown():
    t = tracker()
    g = feed(t, _touch(10.0, 10.1, 16384, 8192, send_y=False))[0]
    assert g["kind"] == "tap" and g["x0"] is None   # never saw Y: the position is unknown, not 0
    g = feed(t, _touch(11.0, 11.1, 16384, 8192, send_x=False))[0]   # now both are known from the first touch
    assert g["x0"] is not None and round(g["y0"]) == 300


def test_tracker_keys_chars_and_back():
    rows = [("EV_KEY", "KEY_A", "DOWN"), ("EV_KEY", "KEY_A", "UP"), ("EV_KEY", "KEY_SPACE", "DOWN"),
            ("EV_KEY", "KEY_ENTER", "DOWN"), ("EV_KEY", "KEY_BACK", "DOWN"), ("EV_KEY", "KEY_BACK", "UP"),
            ("EV_KEY", "KEY_F13", "DOWN"), ("EV_SYN", "SYN_REPORT", "00000000")]
    g = feed(tracker(), _lines((1.0, 13, rows)))
    assert [(x["kind"], x.get("ch") or x.get("key")) for x in g] == [("char", "a"), ("char", " "), ("key", "Return"), ("back", "back")]
    assert feed(tracker(), ["garbage", "[ 1.0] /dev/input/event2: EV_ABS"]) == []


class FakeAdb:
    def __call__(self, *a):
        return ""


def _watcher(tmp_path, xml, full=False):
    cap = Capture.create(tmp_path / "c", "android")
    w = ai.Watcher("emulator-5554", cap, "org.example.demo", 2.0, FakeAdb(), full=full)
    w._activity = "org.example.demo.MainActivity"
    w._dumps = [(100, xml)] if xml else []
    return w, cap


def test_watcher_resolves_touches_against_the_dump_before_them_and_joins_characters(tmp_path):
    xml = (DATA / "android-main.xml").read_text()
    w, cap = _watcher(tmp_path, xml)
    from atl_doctor.sources.tree import content_origin
    p = tmp_path / "o.xml"
    p.write_text(xml)
    ox, oy = content_origin(p, "android")
    w._resolve({"kind": "tap", "x0": ox + 300, "y0": oy + 440, "x1": 0, "y1": 0, "host_us": 200})   # 150, 220 dp
    w._resolve({"kind": "tap", "x0": 5, "y0": 5, "x1": 0, "y1": 0, "host_us": 50})   # before any dump: no target
    for i, ch in enumerate("hi!"):
        w._resolve({"kind": "char", "ch": ch, "host_us": 300 + i})
    w._resolve({"kind": "char", "ch": "z", "host_us": 300 + 3_000_000})   # a pause: a new text event
    w._resolve({"kind": "back", "key": "back", "host_us": 9_000_000})
    w._resolve({"kind": "scroll", "x0": 300, "y0": 600, "x1": 300, "y1": 300, "host_us": 9_100_000})
    w._flush_text()
    assert sorted(e["kind"] for e in w.events) == ["back", "scroll", "tap", "tap", "text", "text"]
    kinds = {e["t_us"]: e for e in w.events}
    tap = kinds[200]
    assert tap["target"]["id"].endswith(":id/name_field") and tap["target"]["text"] == "" and tap["point"] == [150.0, 220.0]
    assert "target" not in kinds[50]
    texts = [e for e in sorted(w.events, key=lambda e: e["t_us"]) if e["kind"] == "text"]
    assert [e["text"] for e in texts] == [{"len": 3}, {"len": 1}]
    assert [e for e in w.events if e["kind"] == "scroll"][0]["scroll"]["dy"] == 150.0
    assert cap.path("trees/input-001.xml").is_file()
    assert "hi" not in json.dumps(w.events)


def test_watcher_full_keeps_the_typed_text(tmp_path):
    w, _ = _watcher(tmp_path, None, full=True)
    for i, ch in enumerate("ok"):
        w._resolve({"kind": "char", "ch": ch, "host_us": 10 + i})
    w._flush_text()
    assert w.events[0]["text"] == {"len": 2, "value": "ok"}


# ------------------------------------------------------------------ replay: pair report
def _pair(tmp_path, android_found=True):
    a = Capture.create(tmp_path / "atl", "atl")
    b = Capture.create(tmp_path / "android", "android")
    for c, found, by in ((a, True, "id"), (b, android_found, "text" if android_found else "rect")):
        c.append_mark("start")
        m = c.read_manifest()
        m.ended, m.package = "2026-11-08T07:00:00+00:00", "org.example.demo"
        m.environment = {"density": "2.0"} if c is b else {}
        c.write_manifest(m)
        (c.root / "trees").mkdir()
        shutil.copy(DATA / ("atl-main.xml" if c is a else "android-main.xml"), c.root / "trees" / "replay-001.xml")
        rows = [{"n": 1, "line": 3, "cmd": "tap", "step": "tap id/save Save 0/1 1,2,3,4", "found": found, "by": by,
                 "tree": "trees/replay-001.xml"},
                {"n": 2, "line": 4, "cmd": "key", "step": "key Escape", "found": None, "by": "", "tree": None}]
        (c.root / bd.REPLAY_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return a.root, b.root


def test_replay_table_found_per_side_and_tree_state(tmp_path):
    a, b = _pair(tmp_path, android_found=False)
    rows = bd.replay_table(Capture.open(a), Capture.open(b))
    assert [r["n"] for r in rows] == [1, 2]
    assert rows[0]["atl"]["found"] is True and rows[0]["android"] == {"found": False, "by": "rect", "error": None}
    assert rows[0]["tree"] == "differs" and rows[0]["tree_difference_count"] >= 1 and rows[0]["tree_differences"]
    assert rows[1]["tree"] == "n/a" and rows[1]["tree_differences"] == []


def test_tree_equal_when_no_differences(tmp_path, monkeypatch):
    a, b = _pair(tmp_path)
    monkeypatch.setattr("atl_doctor.sources.tree.tree_findings", lambda *x, **k: [])
    assert bd.replay_table(Capture.open(a), Capture.open(b))[0]["tree"] == "equal"


def test_rows_of_different_step_texts_are_not_paired(tmp_path):
    a, b = _pair(tmp_path)
    (Path(b) / bd.REPLAY_FILE).write_text(json.dumps({"n": 1, "step": "tap id/other", "found": True, "by": "id"}) + "\n")
    assert bd.replay_table(Capture.open(a), Capture.open(b)) == []
    assert bd.read_replay(Capture.create(tmp_path / "none", "atl")) == []


def test_pair_report_section_text_json_html_golden(tmp_path, capsys, assert_golden):
    a, b = _pair(tmp_path, android_found=False)
    assert main(["report", str(a), str(b), "--density", "2.0"]) == 0
    text = capsys.readouterr().out
    section = text[text.index("Replayed input"):]
    section = section[:section.index("\n\n")] + "\n"
    assert_golden(section, "replay-section.txt")
    assert main(["report", "-f", "json", str(a), str(b), "--density", "2.0"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["extras"]["replay"][0]["android"]["found"] is False
    out = tmp_path / "r.html"
    assert main(["report", "-f", "html", "-o", str(out), str(a), str(b), "--density", "2.0"]) == 0
    html = out.read_text()
    assert "Replayed input" in html and "no (rect)" in html and "yes (id)" in html


def test_report_without_replay_has_no_section(tmp_path, capsys):
    a, b = _pair(tmp_path)
    (Path(a) / bd.REPLAY_FILE).unlink()
    assert main(["report", str(a), str(b), "--density", "2.0"]) == 0
    assert "Replayed input" not in capsys.readouterr().out


# ------------------------------------------------------------------ the agent: clicks on a host JVM
@pytest.mark.skipif(not shutil.which("meson") or not shutil.which("javac"), reason="meson or JDK missing")
@pytest.mark.parametrize("opts, expect", [("", 4), ("noinput", 0)])
def test_agent_records_clicks_on_a_host_jvm(built, tmp_path, opts, expect):
    """Stand-ins for android.view.View and friends: T lines with id, text, rectangle, content path and activity."""
    import subprocess
    cls, out = tmp_path / "classes", tmp_path / "agent.tsv"
    cls.mkdir()
    src = Path(__file__).parent / "data" / "inputjava"
    subprocess.run(["javac", "-d", str(cls), *map(str, src.rglob("*.java"))], check=True, capture_output=True)
    agent = f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},noex,noexit,nosys" + (f",{opts}" if opts else "")
    r = subprocess.run(["nice", "-n", "19", shutil.which("java"), "-cp", str(cls), agent, "Main"], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    from atl_doctor.sources.exceptions import parse_agent
    ev = parse_agent(out)
    assert not [e for e in ev if e.tag == "I" and e.fields[0].startswith(("skipped", "SetBreakpoint"))]
    t = [e.fields for e in ev if e.tag == "T"]
    assert len(t) == expect
    if not expect:
        return
    act = "Main$MainActivity"
    assert t[0] == ["tap", act, "android.widget.TextView", "org.example:id/v7f01", "Save it", "Save button", "10", "20", "110", "70", "1/0"]
    assert t[1][:3] == ["tap", act, "android.widget.EditText"] and t[1][4] == "" and "secret" not in out.read_text()   # editable: no text
    assert t[1][6:] == ["0", "0", "30", "9", "1/2"]
    assert t[2][0] == "long-tap" and t[2][3] == "" and t[2][10] == "1/1"   # no resource name (NotFoundException): empty id
    assert t[3][1] == "" and t[3][10] == "-"   # no activity, no content view above: window-relative rectangle, no path


@pytest.mark.skipif(not shutil.which("meson") or not shutil.which("javac"), reason="meson or JDK missing")
def test_agent_follows_atl_input_trace_hooks_on_a_host_jvm(built, tmp_path):
    """With android.atl.InputTrace the agent reports what the user does (tap, long-tap, text length, scroll, keys) and no
    longer the performClick of the app itself."""
    import subprocess
    cls, out = tmp_path / "classes", tmp_path / "agent.tsv"
    cls.mkdir()
    data = Path(__file__).parent / "data"
    srcs = [*(data / "inputjava").rglob("*.java"), *(data / "inputtrace").rglob("*.java")]
    subprocess.run(["javac", "-d", str(cls), *map(str, srcs)], check=True, capture_output=True)
    agent = f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},noex,noexit,nosys"
    r = subprocess.run(["nice", "-n", "19", shutil.which("java"), "-cp", str(cls), agent, "TraceMain"], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    from atl_doctor.sources.exceptions import parse_agent
    ev = parse_agent(out)
    assert not [e for e in ev if e.tag == "I" and e.fields[0].startswith(("skipped", "SetBreakpoint"))]
    t = [e.fields for e in ev if e.tag == "T"]
    assert [x[0] for x in t] == ["tap", "text", "key", "key", "scroll", "long-tap"]
    assert t[0][3] == "org.example:id/v7f01" and t[0][4] == "Save"
    assert t[1][2] == "android.widget.EditText" and t[1][4] == "6" and "secret" not in out.read_text()
    assert t[2][4] == "4,0" and t[3][4] == "66,0" and t[4][4] == "0,300"
    out_ev = bd.events_from_agent(ev)
    assert [e["kind"] for e in out_ev] == ["tap", "text", "back", "key", "scroll", "long-tap"]
    assert out_ev[1]["text"] == {"len": 6} and out_ev[3]["key"] == "Return" and out_ev[4]["scroll"] == {"dx": 0.0, "dy": 300.0}


def test_events_from_agent_user_input_lines_are_merged():
    def t(us, kind, text="", cls="android.widget.EditText", i="org.x:id/q"):
        return f"T\t{us}\t7\t{kind}\tMainActivity\t{cls}\t{i}\t{text}\t\t0\t0\t10\t10\t0/1"
    k = lambda us, c, a=0: f"T\t{us}\t7\tkey\t\t\t\t{c},{a}\t\t\t\t\t\t-"
    lines = [t(1_000_000, "text", "1"), t(1_200_000, "text", "2"), t(1_400_000, "text", "3"),   # one text, 3 characters
             t(9_000_000, "scroll", "0,100", "android.view.View", ""), t(9_100_000, "scroll", "0,100", "android.view.View", ""),
             t(9_200_000, "scroll", "0,-100", "android.view.View", ""),                     # direction change: new event
             t(12_000_000, "tap", "", "android.view.View", "org.x:id/b"), t(12_010_000, "tap", "", "android.view.View", "org.x:id/b"),
             k(14_000_000, 111), k(14_010_000, 4), k(14_100_000, 4, 1), k(15_000_000, 57), k(16_000_000, 66),
             "L\t14010000\t7\tA\tonBackPressed\tMainActivity\tx\t01\ty"]
    out = bd.events_from_agent(parse_agent_lines(lines))
    assert [e["kind"] for e in out] == ["text", "scroll", "scroll", "tap", "back", "key"]
    assert out[0]["text"] == {"len": 3} and out[0]["target"]["text"] == ""
    assert [e["scroll"]["dy"] for e in out[1:3]] == [200.0, -100.0]
    assert out[5]["key"] == "Return"

