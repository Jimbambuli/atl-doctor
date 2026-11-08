# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import atl_doctor.record_atl as ra
from atl_doctor.capture import Capture
from atl_doctor.cli import main
from atl_doctor.findings import Klass, RANK
from atl_doctor.sources.windowclose import KEY, NOT_CHECKED, summary, window_close_findings
from test_record_atl import _fake_steps, apk, tmp_prefix  # noqa: F401  (fixtures)

DATA = Path(__file__).parent / "data"
YES = {"limit_s": 5.0, "ended": True, "seconds": 1.2, "exit": 0, "signal": None, "remaining": []}
NO = {"limit_s": 5.0, "ended": False, "seconds": None, "exit": None, "signal": None,
      "remaining": [{"pid": 4711, "name": "java"}, {"pid": 4720, "name": "dbus-launch"}]}


def _measure(code: str, wait: float):
    """_measure_window_close against a real child that runs CODE; returns (result, child)."""
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    ended: dict = {}
    threading.Thread(target=lambda: (proc.wait(), ended.update(t=time.monotonic())), daemon=True).start()
    old, ra.WINDOW_CLOSE_WAIT_S = ra.WINDOW_CLOSE_WAIT_S, wait
    try:
        return ra._measure_window_close(proc, time.monotonic(), ended), proc
    finally:
        ra.WINDOW_CLOSE_WAIT_S = old


def test_process_ends_in_time_is_yes_with_exit_code():
    r, _ = _measure("import time,sys; time.sleep(0.3); sys.exit(3)", 5.0)
    assert r["ended"] and 0.2 <= r["seconds"] <= 1.5 and r["exit"] == 3 and r["signal"] is None
    assert r["remaining"] == []


def test_process_still_running_is_no_with_its_own_pids_only():
    r, p = _measure("import time; time.sleep(30)", 0.5)
    try:
        assert not r["ended"] and r["seconds"] is None
        assert [e["pid"] for e in r["remaining"]] == [p.pid] and r["remaining"][0]["name"].startswith("python")
    finally:
        p.kill()  # only this PID
        p.wait()


def test_summary_lines():
    assert summary(YES) == "yes (1.2 s)"
    assert summary(NO) == "no (still running after 5 s)"
    assert summary(None) == NOT_CHECKED and NOT_CHECKED.startswith("not checked")


def test_no_is_high_weight_finding_yes_and_missing_are_not():
    (f,) = window_close_findings({"window_close": json.dumps(NO)}, "start", 0, "atl")
    assert f.klass is Klass.LIFECYCLE and f.weight == RANK[Klass.CRASH] and f.details["remaining"][0]["pid"] == 4711
    assert window_close_findings({"window_close": json.dumps(YES)}, "start", 0, "atl") == []
    assert window_close_findings({}, "start", 0, "atl") == []


def _cap(tmp_path, wc):
    dst = tmp_path / "cap"
    shutil.copytree(DATA / "capture-atl", dst)
    mf = json.loads((dst / "manifest.json").read_text())
    mf["signal"] = None
    if wc is not None:
        mf["environment"]["window_close"] = json.dumps(wc)
    (dst / "manifest.json").write_text(json.dumps(mf))
    return dst


def test_report_text_json_html_golden(tmp_path, capsys, assert_golden):
    for name, wc in (("window-close-no", NO), ("window-close-yes", YES)):
        cap = _cap(tmp_path / name, wc) if (tmp_path / name).mkdir() is None else None
        assert main(["report", str(cap)]) == 0
        text = capsys.readouterr().out
        assert_golden(text, name + ".txt")
        assert main(["report", "-f", "json", str(cap)]) == 0
        d = json.loads(capsys.readouterr().out)
        assert d["environment"][KEY] == summary(wc)
        assert any(f["kind"] == "lifecycle" for f in d["findings"]) == (wc is NO)
        out = tmp_path / (name + ".html")
        assert main(["report", "-f", "html", "-o", str(out), str(cap)]) == 0
        assert summary(wc) in out.read_text()
    assert "no (still running after 5 s)" in (tmp_path / "window-close-no.html").read_text()
    assert main(["report", str(_cap(tmp_path / "x", None) if (tmp_path / "x").mkdir() is None else None)]) == 0
    assert f"{KEY}: {NOT_CHECKED}" in capsys.readouterr().out


def test_record_with_close_window_measures_and_stores(tmp_prefix, apk, tmp_path, monkeypatch):
    from test_steps import FakeDriver

    _fake_steps(monkeypatch)
    monkeypatch.setattr(ra, "WINDOW_CLOSE_WAIT_S", 0.5)
    monkeypatch.setattr(FakeDriver, "close_window", lambda self: setattr(self, "window_closed_at", time.monotonic()),
                        raising=False)  # what AtlDriver.close_window additionally does
    steps = tmp_path / "s.steps"
    steps.write_text('wait-window "Demo"\nclose-window\n')
    c = tmp_path / "c"
    assert main(["record", "--steps", str(steps), "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait"]) == 0
    m = Capture.open(c).read_manifest()
    wc = json.loads(m.environment["window_close"])
    assert wc["ended"] is False and wc["limit_s"] == 0.5 and wc["remaining"]
    assert m.environment["stopped_by"] == "steps"


def test_record_without_close_window_stores_nothing(tmp_prefix, apk, tmp_path, monkeypatch):
    _fake_steps(monkeypatch)
    steps = tmp_path / "s.steps"
    steps.write_text('wait-window "Demo"\n')
    c = tmp_path / "c"
    assert main(["record", "--steps", str(steps), "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait"]) == 0
    assert "window_close" not in Capture.open(c).read_manifest().environment
