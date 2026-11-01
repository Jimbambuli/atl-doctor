# SPDX-License-Identifier: GPL-3.0-or-later
from atl_doctor.capture import Mark
from atl_doctor.sources.timeline import read_timeline, timeline_lines

MARKS = [Mark(0, 0, 0, "start"), Mark(1, 5, 0, "running")]


def _trace(tmp_path):
    d = tmp_path / "trace"
    d.mkdir()
    (d / "tr.methods").write_text("0x4\torg.ex.App\tload\t()V\tApp.java\n0x8\tjava.lang.Object\twait\t()V\tObject.java\n"
                                  "0xc\tandroid.view.View\tdraw\t(Landroid/graphics/Canvas;)V\tView.java\n")
    (d / "tr.threads").write_text("1\tmain\n7\tDefaultDispatcher-worker-1\n")
    (d / "tr.time").write_text("0\t1\t0x4\t250000\n0\t7\t0x8\t900000\n0\t1\t0xc\t50000\n1\t7\t0x4\t1200\n"
                               "1\t7\t0x10\t5\n")
    return d


def test_read_timeline_busiest_methods_per_phase_without_waiting(tmp_path):
    t = read_timeline(_trace(tmp_path), MARKS)
    assert [(p["phase"], [(e["thread"], e["method"], e["ms"]) for e in p["top"]]) for p in t] == [
        ("start", [("main", "org.ex.App.load", 250.0), ("main", "android.view.View.draw", 50.0)]),
        ("running", [("DefaultDispatcher-worker-1", "org.ex.App.load", 1.2)])]


def test_timeline_lines(tmp_path):
    lines = timeline_lines(read_timeline(_trace(tmp_path), MARKS))
    assert lines[0] == "start"
    assert lines[1].split() == ["250.0", "ms", "main", "org.ex.App.load"]


def test_read_timeline_without_files(tmp_path):
    assert read_timeline(tmp_path / "trace", MARKS) == []
