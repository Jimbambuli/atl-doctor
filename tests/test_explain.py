# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.cli import main

CAP = str(Path(__file__).parent / "data" / "capture-atl")


def test_explain_by_short_id_shows_stack(capsys):
    assert main(["explain", "-d", CAP, "286e960d"]) == 0
    out = capsys.readouterr().out
    assert "exc:java.lang.NullPointerException@android.graphics.drawable.Drawable.inflate" in out
    assert "stack:\n  android.graphics.drawable.Drawable.inflate\n  org.example.demo.Main.onCreate" in out
    assert "occurrences:\n  start [atl]: 1x" in out and '"catcher"' in out


def test_explain_from_json_report_and_prefix(tmp_path, capsys):
    j = tmp_path / "r.json"
    assert main(["report", "-f", "json", "-o", str(j), CAP]) == 0
    assert main(["explain", "-d", str(j), "286e"]) == 0
    assert "stack:" in capsys.readouterr().out


def test_explain_unknown_exit_1(capsys):
    assert main(["explain", "-d", CAP, "deadbeef"]) == 1
    assert "no finding matches ID" in capsys.readouterr().err


def test_explain_ambiguous_exit_2(tmp_path, capsys):
    from atl_doctor.findings import Finding, Klass
    from atl_doctor.report import Report, render_json
    fs = [Finding("exc", f"k{i}", Klass.OTHER, "t") for i in range(400)]
    pre = {}
    for f in fs:
        pre.setdefault(f.short_id[:4], []).append(f)
    hit = next(k for k, v in pre.items() if len(v) > 1)
    j = tmp_path / "r.json"
    j.write_text(render_json(Report("log", {}, [], ["p"], fs)), encoding="utf-8")
    assert main(["explain", "-d", str(j), hit]) == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err and pre[hit][0].short_id in err


def test_explain_needs_dir(capsys):
    assert main(["explain", "286e960d"]) == 2
