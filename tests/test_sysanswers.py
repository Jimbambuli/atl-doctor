# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from atl_doctor.capture import Capture, Mark
from atl_doctor.cli import main
from atl_doctor.findings import Klass
from atl_doctor.sources.exceptions import parse_agent, parse_agent_lines
from atl_doctor.sources.sysanswers import (annotate_broadcasts, answer_findings, answers, broadcast_findings, broadcasts,
                                           last_answers)

DATA = Path(__file__).parent / "data"
MARKS = [Mark(0, 0, 0, "start")]
CONNECTIVITY = "android.net.conn.CONNECTIVITY_CHANGE"


def test_parse_q_and_b_lines():
    ev = parse_agent(DATA / "agent-sys-atl.tsv")
    assert [e.tag for e in ev].count("Q") == 7 and [e.tag for e in ev].count("B") == 5
    b = next(e for e in ev if e.tag == "B")
    assert b.tid == 7 and b.fields == ["register", CONNECTIVITY, "org.videolan.vlc.util.NetworkMonitor$1", "0a1b2c3d"]
    assert answers(ev)["connectivity.activeNetwork"] == [(1, "null")]
    assert not [e for e in ev if e.tag == "I" and "skipped" in e.fields[0]]   # no malformed line


def test_registered_never_delivered_without_android():
    ev = parse_agent(DATA / "agent-sys-atl.tsv")
    reg, deliv, unreadable = broadcasts(ev)
    assert len(reg[CONNECTIVITY]) == 1 and unreadable == 1 and deliv["android.intent.action.SCREEN_ON"] == 1
    notes: list[str] = []
    fs = broadcast_findings(ev, MARKS, "atl", notes)
    assert [f.key for f in fs] == ["android.intent.action.SCREEN_OFF", CONNECTIVITY]   # SCREEN_OFF was in the same filter
    f = fs[1]
    assert f.title == f"Receiver registered for {CONNECTIVITY}, {CONNECTIVITY} never delivered"
    assert f.klass is Klass.SYSTEM and f.details["receivers"] == ["org.videolan.vlc.util.NetworkMonitor$1"]
    assert f.details["registrations"] == 1   # the overload that calls the other counts once
    assert "could not be read for 1" in notes[0]


def test_delivered_action_is_no_finding_and_dash_is_ignored():
    ev = parse_agent_lines(["B\t1\t1\tregister\tX\tA\t01", "B\t2\t1\tdeliver\tX\tA\t01", "B\t3\t1\tdeliver\t-\tA\t01"])
    assert broadcast_findings(ev, MARKS, "atl") == []


def test_answers_differ_only_where_they_differ():
    atl, andr = parse_agent(DATA / "agent-sys-atl.tsv"), parse_agent(DATA / "agent-sys-android.tsv")
    assert last_answers(atl)["connectivity.transports"] == "none"
    fs = {f.key: f for f in answer_findings(atl, andr, MARKS)}
    assert set(fs) == {"connectivity.activeNetwork", "connectivity.activeNetworkInfo", "connectivity.transports",
                       "pm.feature:android.hardware.touchscreen", "pm.package:com.google.android.gms", "display.metrics"}
    assert "accounts.count" not in fs
    assert fs["connectivity.transports"].title == "connectivity.transports: ATL none, Android WIFI"
    assert fs["connectivity.activeNetwork"].klass is Klass.SYSTEM and fs["display.metrics"].klass is Klass.OTHER


def test_no_q_lines_on_one_side_says_so():
    notes: list[str] = []
    assert answer_findings([], parse_agent(DATA / "agent-sys-android.tsv"), MARKS, notes) == []
    assert "no Q lines in the ATL agent trace" in notes[0]


def test_annotate_android_delivered():
    atl, andr = parse_agent(DATA / "agent-sys-atl.tsv"), parse_agent(DATA / "agent-sys-android.tsv")
    fs = broadcast_findings(atl, MARKS, "atl")
    annotate_broadcasts(fs, andr)
    by = {f.key: f for f in fs}
    assert by[CONNECTIVITY].details["android_delivered"] == 1 and "Android delivered it 1x" in by[CONNECTIVITY].suggestion
    off = by["android.intent.action.SCREEN_OFF"]
    assert off.details["android_registered"] is True and off.details["android_delivered"] == 0


def _pair(tmp_path, atl_tsv="agent-sys-atl.tsv", android_tsv="agent-sys-android.tsv"):
    a = Capture.create(tmp_path / "atl", "atl")
    b = Capture.create(tmp_path / "android", "android")
    for c, tsv in ((a, atl_tsv), (b, android_tsv)):
        c.append_mark("start")
        m = c.read_manifest()
        m.ended = "2026-11-08T07:00:00+00:00"
        m.package = "org.videolan.vlc"
        c.write_manifest(m)
        if tsv:
            shutil.copy(DATA / tsv, c.root / "agent.tsv")
    return a.root, b.root


def test_pair_report_section_json_and_golden(tmp_path, capsys, assert_golden):
    a, b = _pair(tmp_path)
    assert main(["report", str(a), str(b)]) == 0
    text = capsys.readouterr().out
    assert_golden("".join(ln for ln in text.splitlines(True) if not ln.startswith(("  started:", "  android_capture:"))),
                  "system-answers.txt")
    sec = text.split("System answers differ")[1].split("\n\n")[0]
    assert "connectivity.activeNetwork: ATL null, Android present" in sec and "accounts.count" not in sec
    assert "Broadcasts registered, never delivered" in text
    assert main(["report", "-f", "json", str(a), str(b)]) == 0
    d = json.loads(capsys.readouterr().out)
    sysf = next(x for x in d["findings"] if x["id"] == "sys:connectivity.transports")
    assert sysf["details"] == {"query": "connectivity.transports", "atl": "none", "android": "WIFI"}
    bc = next(x for x in d["findings"] if x["id"] == f"bcast:{CONNECTIVITY}")
    assert bc["class"] == "system-answer" and bc["details"]["android_delivered"] == 1 and not bc["on_android"]
    out = tmp_path / "r.html"
    assert main(["report", "-f", "html", "-o", str(out), str(a), str(b)]) == 0
    assert "System answers differ" in out.read_text()


def test_not_delivered_on_android_either_is_not_atl_blame(tmp_path, capsys):
    a, b = _pair(tmp_path, android_tsv="agent-sys-atl.tsv")
    assert main(["report", "-f", "json", str(a), str(b)]) == 0
    d = json.loads(capsys.readouterr().out)
    bc = next(x for x in d["findings"] if x["id"] == f"bcast:{CONNECTIVITY}")
    assert bc["on_android"] and bc["class"] == "other" and bc["weight"] == 0
    assert not [x for x in d["findings"] if x["kind"] == "sys"]


def test_single_capture_reports_never_delivered(tmp_path, capsys):
    a, _ = _pair(tmp_path)
    assert main(["report", str(a)]) == 0
    text = capsys.readouterr().out
    assert f"Receiver registered for {CONNECTIVITY}, {CONNECTIVITY} never delivered" in text.split("Fix first")[1].split("\n\n")[0]


@pytest.mark.skipif(not shutil.which("meson") or not shutil.which("javac"), reason="meson or JDK missing")
def test_agent_records_answers_and_broadcasts_on_host_jvm(built, tmp_path):
    """Stand-ins for the android.* classes on a host JVM: the agent asks them, sees register and deliver."""
    java = shutil.which("java")
    src = DATA / "sysjava"
    cls = tmp_path / "classes"
    cls.mkdir()
    subprocess.run(["javac", "-d", str(cls), *map(str, src.rglob("*.java"))], check=True, capture_output=True)
    out, marks = tmp_path / "agent.tsv", tmp_path / "marks.tsv"
    r = subprocess.run(["nice", "-n", "19", java, "-cp", str(cls),
                        f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},marks={marks},noex,noexit", "Main", str(marks)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    ev = parse_agent(out)
    assert not [e for e in ev if e.tag == "I" and e.fields[0].startswith("skipped")]
    last = last_answers(ev)
    assert last["connectivity.activeNetwork"] == "present" and last["connectivity.transports"] == "WIFI"
    assert last["connectivity.capabilities"] == "INTERNET,VALIDATED" and last["connectivity.activeNetworkInfo"] == "type=1 connected=true"
    assert last["pm.feature:android.hardware.touchscreen"] == "true" and last["pm.feature:android.hardware.camera"] == "false"
    assert last["pm.package:com.example.app"] == "found" and last["pm.package:com.android.chrome"] == "missing"
    assert last["display.metrics"] == "1280x720@160dpi" and last["accounts.count"] == "2" and last["accounts.count:com.google"] == "1"
    assert "someone@example.org" not in out.read_text()   # account names never leave the process
    reg, deliv, unreadable = broadcasts(ev)
    assert set(reg) == {CONNECTIVITY, "com.example.PING", "android.intent.action.SCREEN_ON"} and unreadable == 0
    assert deliv == {"com.example.PING": 1}
    assert [f.key for f in broadcast_findings(ev, MARKS, "atl")] == ["android.intent.action.SCREEN_ON", CONNECTIVITY]


@pytest.mark.skipif(not shutil.which("meson") or not shutil.which("javac"), reason="meson or JDK missing")
def test_nosys_switches_it_off(built, tmp_path):
    cls = tmp_path / "classes"
    cls.mkdir()
    src = DATA / "sysjava"
    subprocess.run(["javac", "-d", str(cls), *map(str, src.rglob("*.java"))], check=True, capture_output=True)
    out, marks = tmp_path / "agent.tsv", tmp_path / "marks.tsv"
    subprocess.run(["nice", "-n", "19", shutil.which("java"), "-cp", str(cls),
                    f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},marks={marks},noex,noexit,nosys", "Main", str(marks)],
                   check=True, capture_output=True, timeout=120)
    assert not [e for e in parse_agent(out) if e.tag in ("Q", "B")]


def test_early_registration_counts_but_says_deliveries_before_are_not_seen():
    ev = parse_agent_lines(["B\t1\t1\tearly\tA,B\tcom.x.R\t01", "B\t2\t1\tregister\tB\tcom.x.S\t02", "B\t3\t1\tdeliver\tB\tcom.x.S\t02"])
    (f,) = broadcast_findings(ev, MARKS, "atl")
    assert f.key == "A" and f.details["registered_before_agent"] is True and "before the agent was armed" in f.suggestion
    live = parse_agent_lines(["B\t1\t1\tregister\tA\tcom.x.R\t01"])
    (g,) = broadcast_findings(live, MARKS, "atl")
    assert g.details["registered_before_agent"] is False and "armed" not in g.suggestion


def test_app_own_action_is_not_blamed_on_atl():
    ev = parse_agent_lines(["B\t1\t1\tregister\taction_pause_scan,android.intent.action.TIME_TICK\tcom.x.R\t01"])
    fs = {f.key: f for f in broadcast_findings(ev, MARKS, "atl")}
    assert fs["action_pause_scan"].klass is Klass.OTHER and fs["action_pause_scan"].details["system_action"] is False
    assert fs["android.intent.action.TIME_TICK"].klass is Klass.SYSTEM
