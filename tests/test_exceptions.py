# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.capture import Capture, Mark
from atl_doctor.findings import Klass, lookup
from atl_doctor.sources import load_capture_findings
from atl_doctor.sources.exceptions import exception_findings, lifecycle_sequence, parse_agent
from atl_doctor.sources.stubs import StubEntry

DATA = Path(__file__).parent / "data"
MARKS = [Mark(0, 0, 0, "start"), Mark(1, 50, 0, "Later")]
ENTRIES = [StubEntry("const", "android.graphics.drawable.Drawable", "parseBlendMode",
                     "(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;", "x:6", "const", "")]


def test_parse_agent_drops_partial_and_malformed_lines():
    ev = parse_agent(DATA / "capture-atl/agent.tsv")
    assert [e.tag for e in ev] == ["I", "L", "L", "E", "L", "E", "I"]
    assert ev[-1].fields == ["skipped 1 malformed lines"]
    assert ev[3].t_us == 1000400 and ev[3].tid == 42 and ev[3].fields[0] == "java.lang.NullPointerException"


def test_agent_exception_ids_stable_across_runs():
    a = exception_findings(parse_agent(DATA / "agent-run1.tsv"), MARKS, "atl")
    b = exception_findings(parse_agent(DATA / "agent-run2.tsv"), MARKS, "atl")
    assert {f.id for f in a} == {f.id for f in b}
    assert "exc:java.lang.NullPointerException@android.widget.TextView.onDraw" in {f.id for f in a}


def test_exception_details_and_phase():
    fs = exception_findings(parse_agent(DATA / "agent-run1.tsv"), MARKS, "atl")
    f = lookup(fs, "exc:java.lang.NullPointerException@android.widget.TextView.onDraw")
    assert f.klass is Klass.OTHER and f.title == "java.lang.NullPointerException: null at X"
    assert f.details["app_frame"] == "org.example.demo.A$N.b"
    assert f.details["catcher"] == "org.example.demo.A.b"
    assert f.details["stack"] == ["android.widget.TextView.onDraw", "org.example.demo.A$N.b"]
    assert f.details["message"] == "null at 0xdeadbeef"
    assert [(o.phase, o.side) for o in f.occurrences] == [("start", "atl")]


def test_uncaught_is_crash():
    fs = exception_findings(parse_agent(DATA / "agent-run1.tsv"), MARKS, "atl")
    f = lookup(fs, "crash:uncaught:java.lang.IllegalArgumentException@android.os.Looper.loop")
    assert f.klass is Klass.CRASH and f.occurrences[0].phase == "start"


def test_same_exception_counts_per_phase():
    ev = parse_agent(DATA / "agent-run1.tsv") + parse_agent(DATA / "agent-run2.tsv")
    fs = exception_findings(ev, MARKS, "atl")
    f = lookup(fs, "exc:java.lang.NullPointerException@android.widget.TextView.onDraw")
    assert [(o.phase, o.count) for o in f.occurrences] == [("start", 1), ("Later", 1)]


def test_lifecycle_sequence():
    cap = Capture.open(DATA / "capture-atl")
    seq = lifecycle_sequence(parse_agent(cap.path("agent.tsv")), cap.read_marks())
    assert seq == {0: ["A:onCreate:org.example.demo.Main", "A:onResume:org.example.demo.Main"],
                   1: ["W:onWindowFocusChanged:org.example.demo.Main"]}


def test_partial_last_line_and_crash():
    phases, fs = load_capture_findings(Capture.open(DATA / "capture-atl"), ENTRIES, set())
    ids = {f.id for f in fs}
    assert "crash:signal:SIGSEGV" in ids  # manifest exit signal (and the log line, merged)
    assert "crash:uncaught:java.lang.IllegalStateException@android.os.Handler.dispatchMessage" in ids
    assert "stub:android.graphics.drawable.Drawable#parseBlendMode" in ids
    assert phases == ["start", "Import"]


def test_agent_and_log_duplicate_merged():
    _, fs = load_capture_findings(Capture.open(DATA / "capture-atl"), None, set())
    npe = [f for f in fs if f.id == "exc:java.lang.NullPointerException@android.graphics.drawable.Drawable.inflate"]
    assert len(npe) == 1 and npe[0].details["catcher"] == "org.example.demo.Main.onCreate"
    assert npe[0].total_count == 1
    assert not any(f.kind == "stub" for f in fs)  # no stub list -> no stub findings


def test_aborted_recording_and_exit_code(tmp_path):
    c = Capture.create(tmp_path / "c", "atl")
    _, fs = load_capture_findings(c, None, set())
    f = lookup(fs, "crash:aborted-recording")
    assert f.klass is Klass.OTHER and "recording did not finish cleanly" in f.title
    m = c.read_manifest()
    m.ended, m.exit_status = "2026-11-01T10:00:00+00:00", 3
    c.write_manifest(m)
    _, fs = load_capture_findings(c, None, set())
    assert {f.id for f in fs} == {"crash:exit:3"}
    m.exit_status = 0
    c.write_manifest(m)
    assert load_capture_findings(c, None, set()) == (["start"], [])


def test_agent_runtime_problems_reach_the_environment(tmp_path, capsys):
    """A plugin that did not load (or no JVMTI) must not look like "no exceptions": the report says so."""
    from atl_doctor.cli import main

    cap = Capture.create(tmp_path / "c", "atl")
    (cap.root / "agent.tsv").write_text(
        "I\t1\t7\tloaded, plugin later x\n"
        "I\t2\t7\tlate: cannot load plugin: libopenjdkjvmti.so: no such file\n"
        "I\t3\t7\tlate: cannot load plugin: libopenjdkjvmti.so: no such file\n"
        "I\t4\t7\tframes unavailable: no GTK in the process\n"
        "garbage\n")
    assert main(["report", str(cap.root)]) == 0
    out = capsys.readouterr().out
    agent_lines = [ln for ln in out.splitlines() if "agent" in ln and "late: cannot load plugin" in ln]
    assert len(agent_lines) == 1 and out.count("late: cannot load plugin") == 1
    assert "frames unavailable" in agent_lines[0] and "skipped 1 malformed lines" in agent_lines[0]
    assert "loaded, plugin later" not in out
