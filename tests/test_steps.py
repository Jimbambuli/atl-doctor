# SPDX-License-Identifier: GPL-3.0-or-later
import io

import pytest

from atl_doctor.steps import Repeat, Step, StepError, parse_steps, run_steps

SPEC_EXAMPLE = '''wait-window "My App"       # until the window is there (title or app package)
click "Playlist"           # text, resource ID (id/foo) or dp coordinates (x,y)
key Escape                 # keys as xkb names, combinations ctrl+a
type "Hallo"
scroll 5                   # notches, negative = up
wait 2                     # seconds
wait-idle                  # until no frame/layout change any more (time limit)
mark "Import"              # phase mark (+ view tree + screenshot)
repeat 10 { click "Weiter"; key Escape }   # repetition, e.g. for memory growth
screenshot "name"
'''


def test_parse_all_commands():
    s = parse_steps(SPEC_EXAMPLE, "s.txt")
    assert [x.cmd for x in s if isinstance(x, Step)][:3] == ["wait-window", "click", "key"]
    rep = next(x for x in s if isinstance(x, Repeat))
    assert rep.count == 10 and [b.cmd for b in rep.body] == ["click", "key"]
    by = {x.cmd: x for x in s if isinstance(x, Step)}
    assert by["wait-window"].args == ("My App", 60.0)
    assert by["key"].args == ("Escape",) and by["type"].args == ("Hallo",)
    assert by["scroll"].args == (5,) and by["wait"].args == (2.0,) and by["wait-idle"].args == (10.0,)
    assert by["mark"].args == ("Import",) and by["screenshot"].args == ("name",)
    assert rep.line == 9 and by["screenshot"].line == 10


def test_click_forms():
    (a, b, c) = parse_steps('click "Playlist"\nclick id/foo\nclick 120,48', "s")
    assert a.args == ("text", "Playlist") and b.args == ("id", "id/foo") and c.args == ("xy", 120.0, 48.0)


def test_timeouts_and_combos():
    s = parse_steps('wait-idle 3\nwait-window org.example.demo 5\nkey ctrl+a\nscroll -2', "s")
    assert [x.args for x in s] == [(3.0,), ("org.example.demo", 5.0), ("ctrl+a",), (-2,)]


def test_multiline_and_nested_repeat():
    s = parse_steps('repeat 2 {\n  key Tab\n  repeat 3 { click 1,2 }\n}\nkey Return', "s")
    assert len(s) == 2 and isinstance(s[0], Repeat) and s[1].line == 5
    inner = s[0].body[1]
    assert isinstance(inner, Repeat) and inner.count == 3 and inner.line == 3 and inner.body[0].args == ("xy", 1.0, 2.0)


def test_comment_hash_inside_quotes_kept():
    assert parse_steps('type "a#b"', "s")[0].args == ("a#b",)


def test_escaped_quote():
    assert parse_steps(r'type "say \"hi\""', "s")[0].args == ('say "hi"',)


@pytest.mark.parametrize("src,line,msg", [
    ("wait 2\nfrobnicate", 2, "unknown command 'frobnicate'"),
    ('click "unterminated', 1, "unterminated string"),
    ("repeat 3 { key Escape", 1, "missing '}'"),
    ("scroll x", 1, "scroll expects an integer"),
    ("scroll-smooth", 1, "scroll-smooth expects an integer"),
    ("scroll-smooth 1.5", 1, "scroll-smooth expects an integer"),
    ("wait", 1, "wait expects a number of seconds"),
    ("click", 1, 'click expects "Text", id/NAME or X,Y'),
    ("key a b", 1, "key: too many arguments"),
    ("}", 1, "unexpected '}'"),
    ("repeat x { key a }", 1, "repeat expects a count and '{'"),
])
def test_errors_with_line_numbers(src, line, msg):
    with pytest.raises(StepError) as e:
        parse_steps(src, "s.txt")
    assert str(e.value) == f"s.txt:{line}: {msg}"


class FakeDriver:
    def __init__(self, has_tree=True, nodes=None):
        self.calls = []
        self.has_tree = has_tree
        self.nodes = nodes or {"Weiter": (10.0, 20.0)}

    def click(self, x, y): self.calls.append(("click", x, y))
    def locate(self, sel): self.calls.append(("locate", sel)); return self.nodes.get(sel)
    def key(self, combo): self.calls.append(("key", combo))
    def type_text(self, s): self.calls.append(("type", s))
    def scroll(self, n): self.calls.append(("scroll", n))
    def scroll_smooth(self, dy): self.calls.append(("scroll-smooth", dy))
    def screenshot(self, name): self.calls.append(("screenshot", name))
    def wait_idle(self, t): self.calls.append(("wait-idle", t)); return True
    def wait_window(self, m, t): self.calls.append(("wait-window", m, t)); return m != "never"
    def mark(self, name): self.calls.append(("mark", name))


def test_run_steps_with_fake_driver_repeat_and_marks():
    d = FakeDriver()
    log = io.StringIO()
    run_steps(parse_steps('repeat 3 { click "Weiter"; mark "Weiter" }\nkey ctrl+a', "s"), d, "s", log)
    assert [c for c in d.calls if c[0] == "mark"] == [("mark", "Weiter")] * 3
    assert d.calls[:2] == [("locate", "Weiter"), ("click", 10.0, 20.0)]
    assert d.calls[-1] == ("key", "ctrl+a")
    assert len(log.getvalue().splitlines()) == 7


def test_click_text_without_tree_fails_with_line():
    with pytest.raises(StepError) as e:
        run_steps(parse_steps('click 1,2\nclick "Weiter"', "s.txt"), FakeDriver(has_tree=False), "s.txt", io.StringIO())
    assert str(e.value) == "s.txt:2: click by text needs a view tree (ATLViewDump/uiautomator)"


def test_click_target_not_found():
    with pytest.raises(StepError) as e:
        run_steps(parse_steps('click id/nope', "s"), FakeDriver(), "s", io.StringIO())
    assert str(e.value) == "s:1: no visible view matches id/nope"


def test_wait_window_timeout_fails():
    with pytest.raises(StepError) as e:
        run_steps(parse_steps('wait-window never 1', "s"), FakeDriver(), "s", io.StringIO())
    assert str(e.value) == "s:1: no window matching 'never' within 1 s"


class _Session:
    def windows(self):
        return [{"caption": "Demo", "resource_class": "org.example.demo", "kind": "normal",
                 "rect": (0.0, 0.0, 1333.0, 800.0), "surface": (-14.0, -12.0)}]


def test_atl_driver_maps_tree_to_screen(tmp_path, monkeypatch):
    from pathlib import Path

    import atl_doctor.sources.tree as tree
    from atl_doctor.capture import Capture
    from atl_doctor.steps import AtlDriver

    cap = Capture.create(tmp_path / "c", "atl")
    (tmp_path / "c/trees").mkdir()
    data = Path(__file__).parent / "data/trees/atl-main.xml"
    monkeypatch.setattr(tree, "request_atl_tree", lambda d, name, timeout_s=10.0: data)
    d = AtlDriver(cap, _Session(), tmp_path / "fifo", tmp_path / "status", has_tree=True, frames=False)
    sent = []
    monkeypatch.setattr(d, "_send", sent.append)
    d.click(5, 5)                       # no tree yet: relative to the window frame
    pos = d.locate("id/save")           # GTK rect [110,320][310,340], content at 10,20
    assert pos == (200.0, 310.0)
    d.click(*pos)                       # surface origin -14,-12 + GTK centre 210,330
    assert sent == ["click 5 5", "click 196 318"]


def test_example_step_files_parse():
    from pathlib import Path

    files = sorted((Path(__file__).parent.parent / "examples").glob("*.steps"))
    assert files
    for f in files:
        assert parse_steps(f.read_text(encoding="utf-8"), f.name)


def test_scroll_smooth_parses_and_runs():
    steps = parse_steps("scroll-smooth -120\nscroll 3", "s")
    assert steps[0].cmd == "scroll-smooth" and steps[0].args == (-120,)
    d = FakeDriver()
    run_steps(steps, d, "s", io.StringIO())
    assert d.calls == [("scroll-smooth", -120), ("scroll", 3)]


def test_atl_driver_wheel_notches_vs_smooth(tmp_path, monkeypatch):
    from atl_doctor.capture import Capture
    from atl_doctor.steps import AtlDriver

    cap = Capture.create(tmp_path / "c", "atl")
    d = AtlDriver(cap, _Session(), tmp_path / "fifo", tmp_path / "status", has_tree=False, frames=False)
    sent = []
    monkeypatch.setattr(d, "_send", sent.append)
    d.scroll(5)            # real wheel notches (the preload shim turns them into source wheel + value120)
    d.scroll_smooth(-90)   # the former touchpad-like scrolling
    assert sent == ["wheel 666 400 5", "wheel-smooth 666 400 -90"]
