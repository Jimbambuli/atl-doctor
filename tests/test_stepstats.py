# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json

from atl_doctor.capture import STEPS_DATA, Capture
from atl_doctor.cli import main
from atl_doctor.sources.stepstats import read_steps, scroll_delta
from atl_doctor.sources.tree import Node
from atl_doctor.steps import parse_steps, run_steps


def node(rid, top, text="", left=0.0, clip=False, visible=True):
    n = Node(rid, "android.widget.TextView", text, "", (left, top, left + 100, top + 20), visible)
    n.clip_v = clip
    return n


def test_scroll_delta_is_content_shift_ignoring_fixed_and_clipped_nodes():
    before = [node("id/bar", 0), node("", 50, "a"), node("", 80, "b"), node("", 110, "c", clip=True)]
    after = [node("id/bar", 0), node("", 20, "a"), node("", 50, "b"), node("", 200, "c", clip=True)]
    assert scroll_delta(before, after) == (0.0, 30.0)  # content moved up 30 dp = scrolled 30 dp further


def test_scroll_delta_pairs_repeated_items_in_order_and_signs_direction():
    before = [node("id/row", 0, "x"), node("id/row", 40, "x")]
    after = [node("id/row", 10, "x"), node("id/row", 50, "x")]
    assert scroll_delta(before, after) == (0.0, -10.0)


def test_scroll_delta_zero_when_nothing_moved_and_none_without_match():
    same = [node("id/bar", 0), node("", 50, "a")]
    assert scroll_delta(same, list(same)) == (0.0, 0.0)
    assert scroll_delta(same, [node("", 50, "other")]) is None
    assert scroll_delta([], []) is None
    assert scroll_delta([node("", 5, "h", visible=False)], [node("", 9, "h", visible=False)]) is None


class ProbeDriver:
    has_tree = True

    def __init__(self, trees, frames):
        self.trees, self.frames, self.calls = list(trees), list(frames), []

    def click(self, x, y): self.calls.append("click")
    def locate(self, sel): return (1.0, 1.0)
    def key(self, combo): self.calls.append("key")
    def type_text(self, s): pass
    def scroll(self, n): self.calls.append("scroll")
    def screenshot(self, name): pass
    def wait_idle(self, t): self.calls.append("idle"); return True
    def wait_window(self, m, t): return True
    def mark(self, name): pass
    def frame_count(self): return self.frames.pop(0)
    def scroll_tree(self): return self.trees.pop(0)


def test_run_steps_records_frames_and_scroll_per_step(tmp_path):
    t_a = [node("", 50, "a")]
    t_b = [node("", 20, "a")]
    d = ProbeDriver([t_a, t_b, t_b, t_a], [0, 5, 5, 9, 9, 9])
    data = io.StringIO()
    run_steps(parse_steps('scroll 3\nclick 1,2\nscroll -3', "s"), d, "s", io.StringIO(), data=data)
    rows = [ln.split("\t") for ln in data.getvalue().splitlines()]
    assert [r[0] for r in rows] == ["1", "2", "3"] and [r[2] for r in rows] == ["scroll", "click", "scroll"]
    assert [r[6] for r in rows] == ["5", "0", "4"]               # frames
    assert [(r[7], r[8]) for r in rows] == [("0.0", "30.0"), ("", ""), ("0.0", "-30.0")]  # click: no scroll probe
    assert int(rows[0][5]) >= int(rows[0][4])


def test_run_steps_without_probes_writes_empty_columns():
    class Plain(ProbeDriver):
        frame_count = scroll_tree = None

    data = io.StringIO()
    run_steps(parse_steps("scroll 1", "s"), Plain([], []), "s", io.StringIO(), data=data)
    r = data.getvalue().splitlines()[0].split("\t")
    assert r[6:] == ["", "", ""]


def _pair(tmp_path):
    a = Capture.create(tmp_path / "atl", "atl")
    t = a.read_marks()[0].boot_us
    (tmp_path / "atl/agent.tsv").write_text("".join(f"P\t{t + 1000 + 100 * i}\t{i}\t5000\t0\n" for i in range(7)) +
                                            f"P\t{t + 90000}\t9\t5000\t0\n")
    (tmp_path / "atl" / STEPS_DATA).write_text(
        f"1\t3\tscroll\tscroll 5\t{t}\t{t + 2000}\t\t0.0\t-312.0\n"
        f"2\t4\tclick\tclick 1,2\t{t + 2000}\t{t + 100000}\t\t\t\n")
    b = Capture.create(tmp_path / "android", "android")
    (tmp_path / "android" / STEPS_DATA).write_text(
        "1\t3\tscroll\tscroll 5\t0\t0\t11\t0.0\t-300.0\n2\t4\tclick\tclick 1,2\t0\t0\t2\t\t\n")
    for c in (a, b):
        m = c.read_manifest()
        m.ended = "2026-11-01T07:00:00+00:00"
        c.write_manifest(m)


def test_read_steps_roundtrip_and_malformed_rows_skipped(tmp_path):
    c = Capture.create(tmp_path / "c", "atl")
    (tmp_path / "c" / STEPS_DATA).write_text("1\t3\tscroll\tscroll 5\t10\t20\t4\t\t-2.5\nbroken\n")
    rows = read_steps(c)
    assert len(rows) == 1 and rows[0].frames == 4 and rows[0].scroll_x is None and rows[0].scroll_y == -2.5


def test_pair_report_json_has_steps_side_by_side(tmp_path, capsys):
    _pair(tmp_path)
    assert main(["report", "-f", "json", str(tmp_path / "atl"), str(tmp_path / "android")]) == 0
    steps = json.loads(capsys.readouterr().out)["extras"]["steps"]
    assert steps[0] == {"n": 1, "line": 3, "step": "scroll 5",
                        "atl": {"frames": 7, "scroll_x_dp": 0.0, "scroll_y_dp": -312.0},
                        "android": {"frames": 11, "scroll_x_dp": 0.0, "scroll_y_dp": -300.0}}
    assert steps[1]["atl"] == {"frames": 1, "scroll_x_dp": None, "scroll_y_dp": None}
    assert steps[1]["android"]["frames"] == 2


def test_pair_report_text_and_html_show_steps(tmp_path, capsys):
    _pair(tmp_path)
    args = [str(tmp_path / "atl"), str(tmp_path / "android")]
    assert main(["report", *args]) == 0
    text = capsys.readouterr().out
    assert "Steps" in text and "scroll 5" in text and "n/a" in text
    line = next(ln for ln in text.splitlines() if "scroll 5" in ln)
    assert "7" in line and "11" in line and "-312" in line and "-300" in line
    assert main(["report", "-f", "html", "-o", str(tmp_path / "r.html"), *args]) == 0
    h = (tmp_path / "r.html").read_text()
    assert "<h2>Steps</h2>" in h and "<table" in h and "-312" in h


def test_single_atl_report_without_steps_has_no_section(tmp_path, capsys):
    Capture.create(tmp_path / "atl", "atl")
    m = Capture(tmp_path / "atl")
    mm = m.read_manifest()
    mm.ended = "2026-11-01T07:00:00+00:00"
    m.write_manifest(mm)
    assert main(["report", str(tmp_path / "atl")]) == 0
    assert "Steps" not in capsys.readouterr().out


def test_atl_without_frame_lines_shows_na(tmp_path, capsys):
    c = Capture.create(tmp_path / "atl", "atl")
    (tmp_path / "atl" / STEPS_DATA).write_text("1\t3\tscroll\tscroll 5\t10\t20\t\t\t\n")
    m = c.read_manifest()
    m.ended = "x"
    c.write_manifest(m)
    assert main(["report", "-f", "json", str(tmp_path / "atl")]) == 0
    s = json.loads(capsys.readouterr().out)["extras"]["steps"][0]
    assert s["atl"]["frames"] is None and "android" not in s
