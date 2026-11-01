# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
from pathlib import Path

from atl_doctor.cli import main
from atl_doctor.errors import DoctorError
from atl_doctor.findings import Finding, Klass, Occurrence, rank
from atl_doctor.report import Report, load_json, render_html, render_json
import pytest

DATA = Path(__file__).parent / "data"
CAP = str(DATA / "capture-atl")


def _json_out(capsys, *extra):
    assert main(["report", "-f", "json", *extra, CAP]) == 0
    return capsys.readouterr().out


def test_text_golden_capture(capsys, assert_golden):
    assert main(["report", CAP]) == 0
    assert_golden(capsys.readouterr().out, "capture-atl.txt")


def test_json_golden_capture(capsys, assert_golden):
    d = json.loads(_json_out(capsys))
    d["tool_version"] = "X"
    assert_golden(json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "capture-atl.json")


def test_json_keys_and_ranking(capsys):
    d = json.loads(_json_out(capsys))
    assert set(d) == {"format", "tool", "tool_version", "mode", "environment", "probes", "phases", "ranking",
                      "findings", "extras"}
    assert d["ranking"]["rule"].startswith("class rank")
    weights = [f["weight"] for f in d["findings"]]
    assert weights == sorted(weights, reverse=True)


def test_json_roundtrip_load(capsys):
    r = load_json(_json_out(capsys))
    again = load_json(render_json(r))
    assert [f.id for f in again.findings] == [f.id for f in rank(r.findings)]
    assert again.findings == rank(r.findings) and again.phases == ["start", "Import"]


def test_newer_report_format_rejected():
    with pytest.raises(DoctorError, match=r"report format 2 is newer than supported \(1\)"):
        load_json('{"format": 2}')


def test_not_a_report_rejected():
    with pytest.raises(DoctorError, match="not an atl-doctor JSON report"):
        load_json("[1]")


def test_html_is_self_contained(tmp_path):
    out = tmp_path / "r.html"
    assert main(["report", "-f", "html", "-o", str(out), CAP]) == 0
    h = out.read_text(encoding="utf-8")
    assert h.startswith("<!doctype html>") and "<details id=" in h
    assert 'src="http' not in h and "<script" not in h and 'href="http' not in h
    assert "prefers-color-scheme:dark" in h


def test_html_requires_output(capsys):
    assert main(["report", "-f", "html", CAP]) == 2


def test_html_escapes_and_embeds_images(tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG")
    f = Finding("tree", "<b>", Klass.TREE_VISIBLE, "x <script>alert(1)</script>",
                occurrences=[Occurrence("p", 0, "atl")], details={"images": ["a.png", "missing.png"]})
    h = render_html(Report("record", {}, [], ["p"], [f]), tmp_path)
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert h.count("data:image/png;base64,") == 1 and h.count('id="') == 1


def test_fail_on_exit_4(capsys):
    assert main(["report", "--fail-on", "crash", CAP]) == 4
    assert "Fix first" in capsys.readouterr().out
    assert main(["report", "--fail-on", "jank", CAP]) == 0
    assert main(["report", "--fail-on", "jank,crash", CAP]) == 4
    assert main(["report", "--fail-on", "bogus", CAP]) == 2


def test_truncated_capture_reports(tmp_path, capsys):
    cap = tmp_path / "c"
    shutil.copytree(DATA / "capture-atl", cap)
    m = json.loads((cap / "manifest.json").read_text())
    m["ended"] = None
    m["signal"] = None
    (cap / "manifest.json").write_text(json.dumps(m))
    assert main(["report", str(cap)]) == 0
    assert "recording did not finish cleanly" in capsys.readouterr().out
    assert main(["report", "-f", "json", str(cap)]) == 0
    assert "crash:aborted-recording" in capsys.readouterr().out


def test_output_into_missing_directory_is_one_line(tmp_path, capsys):
    assert main(["report", "-o", str(tmp_path / "nope/x.txt"), CAP]) == 1
    err = capsys.readouterr().err
    assert err.startswith("atl-doctor: cannot write ") and "Traceback" not in err


def test_log_and_capture_directory_exclude_each_other(tmp_path, capsys):
    log = tmp_path / "a.log"
    log.write_text("")
    assert main(["report", "--log", str(log), CAP]) == 2
    assert "--log and a capture directory exclude each other" in capsys.readouterr().err
