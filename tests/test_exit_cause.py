# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
from pathlib import Path

from atl_doctor.cli import main
from atl_doctor.findings import Klass
from atl_doctor.sources.exceptions import EXIT_NONE, exit_findings, parse_agent, parse_agent_lines
from atl_doctor.capture import Capture, Mark

DATA = Path(__file__).parent / "data"
MARKS = [Mark(0, 0, 0, "start")]


def test_parse_x_and_r_lines():
    ev = parse_agent(DATA / "agent-exit.tsv")
    assert [e.tag for e in ev].count("X") == 3 and [e.tag for e in ev].count("R") == 2
    x = next(e for e in ev if e.tag == "X")
    assert x.tid == 44 and x.fields[:3] == ["System.exit", "10", "worker-1"]


def test_exit_finding_names_status_app_frame_stack_and_last_exceptions():
    (f,) = exit_findings(parse_agent(DATA / "agent-exit.tsv"), 10, None, MARKS, "atl")
    assert f.klass is Klass.CRASH and f.kind == "exit"
    assert f.title == "System.exit(10) called from org.example.demo.Quit.now on thread worker-1"
    d = f.details
    assert d["app_frame"] == "org.example.demo.Quit.now" and d["status"] == "10" and d["thread"] == "worker-1"
    assert d["stack"][0] == "java.lang.System.exit" and len(d["stack"]) == 4
    assert d["chain"] == ["System.exit", "Runtime.exit", "Runtime.halt"]
    assert [x["type"] for x in d["recent_exceptions"]] == ["java.lang.NullPointerException", "java.lang.IllegalStateException"]
    assert d["recent_exceptions"][1]["uncaught"] and d["recent_exceptions"][0]["app_frame"] == "org.example.demo.Main.onCreate"


def test_exit_zero_is_not_fix_first():
    ev = parse_agent_lines(["X\t5\t1\tSystem.exit\t0\tmain\tB:java.lang.System.exit|org.example.A.b"])
    (f,) = exit_findings(ev, 0, None, MARKS, "atl")
    assert f.klass is Klass.OTHER and "System.exit(0) called from org.example.A.b" in f.title


def test_no_exit_call_recorded_says_so():
    ev = parse_agent_lines(["I\t1\t1\texit hooks 3"])
    (f,) = exit_findings(ev, 10, None, MARKS, "atl")
    assert f.title == EXIT_NONE and f.klass is Klass.CRASH and f.details["process_exit_status"] == 10


def test_no_exit_call_and_hooks_not_reported():
    (f,) = exit_findings([], 10, None, MARKS, "atl")
    assert f.title.startswith(EXIT_NONE) and "ohne Angabe" in f.title
    (g,) = exit_findings(parse_agent_lines(["I\t1\t1\texit hooks 2"]), 0, None, MARKS, "atl")
    assert "2 von 3" in g.title and g.klass is Klass.OTHER


def _cap(tmp_path):
    dst = tmp_path / "cap"
    shutil.copytree(DATA / "capture-atl", dst)
    shutil.copy(DATA / "agent-exit.tsv", dst / "agent.tsv")
    mf = json.loads((dst / "manifest.json").read_text())
    mf["signal"], mf["exit_status"] = None, 10
    (dst / "manifest.json").write_text(json.dumps(mf))
    return dst


def test_report_text_json_html_golden(tmp_path, capsys, assert_golden):
    cap = _cap(tmp_path)
    assert main(["report", str(cap)]) == 0
    text = capsys.readouterr().out
    assert_golden(text, "exit-cause.txt")
    assert "System.exit(10) called from org.example.demo.Quit.now" in text.split("Fix first")[1].split("\n\n")[0]
    assert main(["report", "-f", "json", str(cap)]) == 0
    d = json.loads(capsys.readouterr().out)
    f = next(x for x in d["findings"] if x["kind"] == "exit")
    assert f["details"]["status"] == "10" and f["details"]["app_frame"] == "org.example.demo.Quit.now"
    out = tmp_path / "r.html"
    assert main(["report", "-f", "html", "-o", str(out), str(cap)]) == 0
    assert "System.exit(10) called from org.example.demo.Quit.now" in out.read_text()
