# SPDX-License-Identifier: GPL-3.0-or-later
import io
import time

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


def test_atl_driver_type_splits_long_text_and_refuses_4_byte_characters(tmp_path, monkeypatch):
    """The helper reads lines of at most 511 bytes and types UTF-8 of up to 3 bytes per character."""
    from atl_doctor.capture import Capture
    from atl_doctor.errors import DoctorError
    from atl_doctor.steps import AtlDriver

    cap = Capture.create(tmp_path / "c", "atl")
    d = AtlDriver(cap, _Session(), tmp_path / "fifo", tmp_path / "status", has_tree=False, frames=False)
    sent = []
    monkeypatch.setattr(d, "_send", sent.append)
    text = "a" * 399 + "ü" * 300
    d.type_text(text)
    assert all(len(s.encode()) <= 405 for s in sent) and "".join(s[5:] for s in sent) == text
    with pytest.raises(DoctorError, match="U\\+1F600"):
        d.type_text("ok \U0001F600")


class _WatchSession(_Session):
    """Session that saw the window appear 1.9 s ago (window_events as the KWin watch script delivers them)."""

    def __init__(self, t_us):
        self.t_us, self.placed = t_us, 0

    def window_events(self):
        return [{"t_us": self.t_us, "caption": "Demo", "resource_class": "org.example.demo", "kind": "normal"}]

    def place_window_at_origin(self):
        self.placed += 1


def test_wait_window_reports_the_event_time_not_the_time_of_the_query(tmp_path):
    from atl_doctor.capture import Capture, now_us
    from atl_doctor.steps import AtlDriver

    seen = now_us()[0] + 5_000_000      # after the step began
    d = AtlDriver(Capture.create(tmp_path / "c", "atl"), _WatchSession(seen), tmp_path / "fifo", tmp_path / "st",
                  has_tree=False, frames=False)
    assert d.wait_window("org.example.demo", 5) and d.event_us == seen
    assert d.session.placed == 1
    assert d.wait_window("nothing", 0.1) is False and d.event_us is None


def test_wait_window_ignores_events_from_before_the_step(tmp_path):
    """A dialog that came and went (repeat N { ... wait-window ... }) must not count as there."""
    from atl_doctor.capture import Capture, now_us
    from atl_doctor.steps import AtlDriver

    class S(_WatchSession):
        def windows(self):
            return []                   # the dialog is closed again

    d = AtlDriver(Capture.create(tmp_path / "c", "atl"), S(now_us()[0] - 60_000_000), tmp_path / "fifo",
                  tmp_path / "st", has_tree=False, frames=False)
    assert d.wait_window("org.example.demo", 0.3) is False and d.event_us is None and d.session.placed == 0


def test_wait_window_takes_a_window_that_is_already_open(tmp_path):
    from atl_doctor.capture import Capture, now_us
    from atl_doctor.steps import AtlDriver

    d = AtlDriver(Capture.create(tmp_path / "c", "atl"), _WatchSession(now_us()[0] - 60_000_000),
                  tmp_path / "fifo", tmp_path / "st", has_tree=False, frames=False)
    t = time.monotonic()
    assert d.wait_window("Demo", 5) and d.event_us is None and time.monotonic() - t < 1


def test_wait_window_step_ends_at_the_event_time():
    from atl_doctor.capture import now_us

    class D(FakeDriver):
        def wait_window(self, m, t):
            self.event_us = now_us()[0] + 100_000 if m == "later" else now_us()[0] - 1_900_000
            time.sleep(0.3)             # the query returns well after the window appeared
            return True

    for m in ("later", "earlier"):
        d, out = D(), io.StringIO()
        run_steps(parse_steps(f"wait-window {m} 5", "s"), d, "s", io.StringIO(), out)
        row = out.getvalue().split("\t")
        t0, t1 = int(row[4]), int(row[5])
        # step end = event time; an event from before the step began ends the step where it began
        assert t1 == (d.event_us if m == "later" else t0)


def test_wait_window_without_a_watch_polls_the_windows(tmp_path):
    from atl_doctor.capture import Capture
    from atl_doctor.steps import AtlDriver

    class S(_WatchSession):
        def window_events(self):
            return None

    d = AtlDriver(Capture.create(tmp_path / "c", "atl"), S(0), tmp_path / "fifo", tmp_path / "st",
                  has_tree=False, frames=False)
    assert d.wait_window("Demo", 5) and d.event_us is None and d.session.placed == 1


# ------------------------------------------------------------------ start_zustand
def test_start_state_read_from_first_line():
    from atl_doctor.steps import start_state

    assert start_state("# Aurora\nstart_zustand: eingerichtet\nclick \"Suche\"\n") == "eingerichtet"
    assert start_state("click \"Suche\"\n") is None


def test_start_state_line_is_no_step_and_keeps_line_numbers():
    steps = parse_steps("start_zustand: x\nwait 1\n", "s")
    assert steps == [Step(2, "wait", (1.0,))]


def test_start_state_only_before_the_first_step():
    with pytest.raises(StepError, match="s:2: start_zustand"):
        parse_steps("wait 1\nstart_zustand: x\n", "s")


def test_click_with_android_coordinates():
    from atl_doctor.steps import AndroidDriver, parse_steps, run_steps

    (s,) = parse_steps("click 1289,676 android=1289,612\n", "t")
    assert s.args == ("xy", 1289.0, 676.0, 1289.0, 612.0)
    with pytest.raises(StepError):
        parse_steps("click 1,2 3,4\n", "t")

    class A(AndroidDriver):
        def __init__(self):
            self.has_tree, self.clicks = False, []

        def click(self, x, y):
            self.clicks.append((x, y))

    a = A()
    run_steps([s], a, "t", io.StringIO())
    assert a.clicks == [(1289.0, 612.0)]
