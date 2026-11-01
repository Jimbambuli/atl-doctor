# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.capture import Mark
from atl_doctor.findings import Klass
from atl_doctor.sources.exceptions import AgentEvent, parse_agent
from atl_doctor.sources.perf import (android_frame_intervals, atl_frame_intervals, frame_stats, perf_findings)

MARKS = [Mark(0, 0, 0, "start"), Mark(1, 10_000_000, 0, "Import")]

GFXINFO = """Applications Graphics Acceleration Info:
Uptime: 1000 Realtime: 1000

** Graphics info for pid 1234 [org.example.demo] **

Stats since: 1000ns
Total frames rendered: 4

---PROFILEDATA---
Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,FrameDeadline,FrameStartTime,FrameInterval,WorkloadTarget,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,DequeueBufferDuration,QueueBufferDuration,GpuCompleted,SwapBuffersCompleted,DisplayPresentTime,CommandSubmissionCompleted,
0,1,1000000000,1000000000,0,1,1,1,1,1,1,16666666,1,1,1,1,1,1010000000,0,0,0,0,0,0,
0,2,1016000000,1016000000,0,1,1,1,1,1,1,16666666,1,1,1,1,1,1046000000,0,0,0,0,0,0,
1,3,1032000000,1032000000,0,1,1,1,1,1,1,16666666,1,1,1,1,1,1532000000,0,0,0,0,0,0,
0,4,1048000000,1048000000,0,1,1,1,1,1,1,16666666,1,1,1,1,1,1056000000,0,0,0,0,0,0,
---PROFILEDATA---

View hierarchy:
"""


def test_frame_stats_exact():
    assert frame_stats([10, 10, 10, 20, 40]) == {"frames": 5, "median_ms": 10, "p95_ms": 40, "p99_ms": 40,
                                                 "over_16_7_pct": 40.0}


def test_frame_stats_empty():
    assert frame_stats([])["frames"] == 0


def _p(t_us, n, dur_us):
    return AgentEvent("P", t_us, 0, [str(n), str(dur_us), "0"])


def test_idle_gap_excluded():
    ev = [_p(1_000_000, 1, 10_000), _p(1_100_000, 2, 1_500_000), _p(1_200_000, 3, 20_000)]
    assert atl_frame_intervals(ev, MARKS) == {0: [10.0, 20.0]}


def test_atl_intervals_per_phase():
    ev = [_p(1_000_000, 1, 8_000), _p(12_000_000, 2, 30_000), _p(12_016_000, 3, 12_000),
          AgentEvent("G", 12_100_000, 0, ["5000"])]
    assert atl_frame_intervals(ev, MARKS) == {0: [8.0], 1: [30.0, 12.0]}


def test_android_gfxinfo_parse():
    assert android_frame_intervals(GFXINFO) == [10.0, 30.0, 8.0]
    assert android_frame_intervals("no profile data") == []


def test_jank_finding_threshold():
    nine = {1: [10.0] * 91 + [20.0] * 9}
    assert not [f for f in perf_findings(nine, None, [], [], MARKS) if f.kind == "jank"]
    many = {1: [10.0] * 77 + [20.0] * 23}
    (f,) = [f for f in perf_findings(many, {1: [10.0] * 10}, [], [], MARKS) if f.kind == "jank"]
    assert f.id == "jank:Import" and f.klass is Klass.JANK
    assert f.title == "Phase Import: 23 % of frames > 16.7 ms"
    assert f.details["atl"]["frames"] == 100 and f.details["android"]["over_16_7_pct"] == 0.0
    assert [(o.phase, o.side, o.count) for o in f.occurrences] == [("Import", "atl", 23)]


def test_hang_finding_title():
    h = AgentEvent("H", 11_000_000, 42, ["1800", "B:java.lang.Object.wait|B:android.webkit.WebView.loadUrl|"
                                                 "org.example.demo.Main.onCreate"])
    (f,) = perf_findings({}, None, [h], [], MARKS)
    assert f.id == "hang:android.webkit.WebView.loadUrl" and f.klass is Klass.JANK
    assert f.title == "Main thread blocked 1.8 s in android.webkit.WebView.loadUrl"
    assert f.details["stack"][0] == "java.lang.Object.wait" and f.details["max_ms"] == 1800
    assert f.occurrences[0].phase == "Import" and f.occurrences[0].where == "org.example.demo.Main.onCreate"


def test_view_tree_dump_is_not_an_app_hang():
    # atl-doctor's own probe: the ATLViewDump hook runs on the main thread while it writes a tree
    h = AgentEvent("H", 11_000_000, 42, ["1000", "android.atl.ATLViewDump.indent|android.atl.ATLViewDump.node|"
                                                 "android.atl.ATLViewDump.dump|android.os.Handler.dispatchMessage"])
    assert perf_findings({}, None, [h], [], MARKS) == []


def test_longest_hang_titles_finding():
    hs = [AgentEvent("H", 1_000_000, 1, [ms, "org.example.demo.Slow.run"]) for ms in ("300", "2500", "120")]
    (f,) = perf_findings({}, None, hs, [], MARKS)
    assert f.title == "Main thread blocked 2.5 s in org.example.demo.Slow.run" and f.total_count == 3


def test_gc_finding_per_phase():
    gcs = [AgentEvent("G", 1_000_000, 0, ["4000"]), AgentEvent("G", 2_000_000, 0, ["16000"])]
    (f,) = perf_findings({}, None, [], gcs, MARKS)
    assert f.id == "gc:start" and f.klass is Klass.OTHER
    assert f.details == {"pauses": 2, "total_ms": 20.0, "max_ms": 16.0}


def test_agent_perf_lines_parse(tmp_path):
    p = tmp_path / "agent.tsv"
    p.write_text("P\t5\t1\t8000\t0\nG\t6\t3000\nH\t7\t42\t150\ta.B.c\nP\t8\t2\n")
    ev = parse_agent(p)
    assert [(e.tag, e.tid, e.fields) for e in ev] == [("P", 0, ["1", "8000", "0"]), ("G", 0, ["3000"]),
                                                     ("H", 42, ["150", "a.B.c"]),
                                                     ("I", 0, ["skipped 1 malformed lines"])]


def test_android_phase_frames(tmp_path):
    from atl_doctor.capture import Capture
    from atl_doctor.sources.perf import android_phase_frames

    cap = Capture.create(tmp_path / "a", "android")
    cap.append_mark("Import")
    (tmp_path / "a/perf").mkdir()
    (tmp_path / "a/perf/gfxinfo-01.txt").write_text(GFXINFO)       # taken at mark 1: frames of phase 0
    (tmp_path / "a/perf/gfxinfo-end.txt").write_text(GFXINFO.replace("1010000000", "1001000000"))
    assert android_phase_frames(cap) == {0: [10.0, 30.0, 8.0], 1: [1.0, 30.0, 8.0]}


def test_pair_report_adds_android_frames(tmp_path, capsys):
    import json

    from atl_doctor.capture import Capture
    from atl_doctor.cli import main

    a = Capture.create(tmp_path / "atl", "atl")
    a.append_mark("Import")
    t1 = a.read_marks()[1].boot_us
    (tmp_path / "atl/agent.tsv").write_text("".join(f"P\t{t1 + 1000 * i}\t{i}\t{30000 if i % 3 == 0 else 8000}\t0\n"
                                                    for i in range(30)))
    b = Capture.create(tmp_path / "android", "android")
    b.append_mark("Import")
    (tmp_path / "android/perf").mkdir()
    (tmp_path / "android/perf/gfxinfo-end.txt").write_text(GFXINFO)
    for c in (a, b):
        m = c.read_manifest()
        m.ended = "2026-11-01T07:00:00+00:00"
        c.write_manifest(m)
    assert main(["report", "-f", "json", str(tmp_path / "atl"), str(tmp_path / "android")]) == 0
    f = next(f for f in json.loads(capsys.readouterr().out)["findings"] if f["kind"] == "jank")
    assert f["key"] == "Import" and f["details"]["atl"]["frames"] == 30
    assert f["details"]["android"]["frames"] == 3


def test_jank_needs_a_minimum_number_of_frames():
    three = {1: [30.0, 30.0, 10.0]}                     # 67 % slow, but only 3 frames
    notes: list[str] = []
    assert not [f for f in perf_findings(three, None, [], [], MARKS, notes=notes) if f.kind == "jank"]
    assert notes == ["Phase Import: too few frames (3 < 30) for a jank finding, 67 % of them > 16.7 ms"]
    # exactly the minimum counts; a lowered minimum lets the small phase through
    thirty = {1: [30.0] * 30}
    assert [f.id for f in perf_findings(thirty, None, [], [], MARKS) if f.kind == "jank"] == ["jank:Import"]
    assert [f.id for f in perf_findings(three, None, [], [], MARKS, min_frames=3) if f.kind == "jank"] == ["jank:Import"]


def test_few_frames_without_slow_ones_leave_no_note():
    notes: list[str] = []
    perf_findings({1: [5.0, 5.0]}, None, [], [], MARKS, notes=notes)
    assert notes == []


def test_report_option_jank_min_frames(tmp_path, capsys):
    import json

    from atl_doctor.capture import Capture
    from atl_doctor.cli import main

    a = Capture.create(tmp_path / "atl", "atl")
    a.append_mark("Import")
    t1 = a.read_marks()[1].boot_us
    (tmp_path / "atl/agent.tsv").write_text("".join(f"P\t{t1 + 1000 * i}\t{i}\t30000\t0\n" for i in range(3)))
    m = a.read_manifest()
    m.ended = "2026-11-01T07:00:00+00:00"
    a.write_manifest(m)

    def run(*extra):
        assert main(["report", "-f", "json", *extra, str(tmp_path / "atl")]) == 0
        return json.loads(capsys.readouterr().out)

    r = run()
    assert not [f for f in r["findings"] if f["kind"] == "jank"]
    assert any("too few frames (3 < 30)" in n for n in r["extras"]["limits"])
    r = run("--jank-min-frames", "3")
    assert [f["id"] for f in r["findings"] if f["kind"] == "jank"] == ["jank:Import"]
    assert main(["report", "--jank-min-frames", "0", str(tmp_path / "atl")]) == 2
