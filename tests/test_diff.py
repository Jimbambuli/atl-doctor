# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.capture import Capture
from atl_doctor.cli import main
from atl_doctor.diff import diff_reports, render_diff_json
from atl_doctor.findings import Finding, Klass, Occurrence
from atl_doctor.report import Report, render_json
import json


def _f(kind, key, klass, count=1, phase="start", title=None):
    return Finding(kind, key, klass, title or key, occurrences=[Occurrence(phase, 0, "atl", count)])


def _write(tmp_path, name, findings):
    p = tmp_path / name
    p.write_text(render_json(Report("record", {}, [], ["start"], findings)), encoding="utf-8")
    return str(p)


def _pair(tmp_path):
    old = _write(tmp_path, "old.json", [_f("crash", "a", Klass.CRASH), _f("stub", "b", Klass.STUB_RESULT_USED, 3),
                                         _f("exc", "same", Klass.EXC_ATL_ONLY), _f("exc", "gone2", Klass.EXC_ATL_ONLY)])
    new = _write(tmp_path, "new.json", [_f("crash", "a", Klass.CRASH), _f("stub", "b", Klass.STUB_RESULT_USED, 5),
                                         _f("exc", "same", Klass.EXC_ATL_ONLY), _f("missing", "n#m", Klass.MISSING)])
    return old, new


def test_diff_new_gone_changed_golden(tmp_path, capsys, assert_golden):
    old, new = _pair(tmp_path)
    assert main(["diff", old, new]) == 0
    assert_golden(capsys.readouterr().out, "diff.txt")


def test_diff_entries_fields(tmp_path):
    from atl_doctor.report import load_json
    old, new = _pair(tmp_path)
    es = diff_reports(load_json(Path(old).read_text()), load_json(Path(new).read_text()))
    assert [(e.status, e.id) for e in es] == [("new", "missing:n#m"), ("gone", "exc:gone2"), ("changed", "stub:b")]
    assert (es[2].old_count, es[2].new_count, es[2].old_weight, es[2].new_weight) == (3, 5, 40, 40)
    assert es[0].old_weight is None and es[1].new_weight is None
    assert json.loads(render_diff_json(es))[0]["status"] == "new"


def test_diff_phase_set_change_is_changed(tmp_path):
    a = _write(tmp_path, "a.json", [_f("exc", "k", Klass.OTHER, phase="start")])
    b = _write(tmp_path, "b.json", [_f("exc", "k", Klass.OTHER, phase="Import")])
    assert main(["diff", a, b]) == 0


def _capture(path, n, addr, line):
    c = Capture.create(path, "atl")
    (path / "agent.tsv").write_text(
        f"E\t1000\t1\tjava.lang.IllegalStateException\tbad state {n} at 0x{addr}\tapp.Main.run\t"
        f"B:android.os.Handler.dispatchMessage|app.Main$1.run:{line}\n", encoding="utf-8")
    m = c.read_manifest()
    m.ended, m.exit_status = "2026-11-01T10:00:00+00:00", 0
    c.write_manifest(m)


def test_diff_same_cause_two_captures_is_unchanged(tmp_path, capsys):
    _capture(tmp_path / "c1", 7, "7f00aa", 10)
    _capture(tmp_path / "c2", 9, "7f13bb", 99)
    out = []
    for n in ("c1", "c2"):
        assert main(["report", "-f", "json", "-o", str(tmp_path / f"{n}.json"), str(tmp_path / n)]) == 0
        out.append(str(tmp_path / f"{n}.json"))
    assert "exc:java.lang.IllegalStateException@android.os.Handler.dispatchMessage" in Path(out[0]).read_text()
    assert main(["diff", *out]) == 0
    assert capsys.readouterr().out == ""


def test_diff_fail_on_new_exit_4(tmp_path, capsys):
    old, new = _pair(tmp_path)
    assert main(["diff", "--fail-on", "new", old, new]) == 4
    assert main(["diff", "--fail-on", "missing", old, new]) == 4
    assert main(["diff", "--fail-on", "crash", old, new]) == 0
    assert main(["diff", "--fail-on", "new", new, new]) == 0
    assert main(["diff", "--fail-on", "bogus", old, new]) == 2
    capsys.readouterr()
    assert main(["diff", "-f", "json", old, new]) == 0
    assert [e["status"] for e in json.loads(capsys.readouterr().out)] == ["new", "gone", "changed"]
