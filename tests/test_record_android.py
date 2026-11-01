# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from atl_doctor.capture import Capture, Manifest
from atl_doctor.cli import main
from atl_doctor.findings import Finding, Klass, Occurrence
from atl_doctor.sources.exceptions import compare_sides, lifecycle_findings
from dexgen import make_apk, make_dex

HERE = Path(__file__).parent
TREES = HERE / "data" / "trees"
PKG = "org.example.demo"


@pytest.fixture
def apk(tmp_path):
    return make_apk(tmp_path / "demo.apk", [make_dex([])])


@pytest.fixture
def adb(tmp_path, monkeypatch):
    """A fake adb on PATH; returns its state directory."""
    d = tmp_path / "adb"
    (d / "device").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "adb"
    exe.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv[0] = 'adb'\n"
                   f"runpy.run_path({str(HERE / 'fake_adb.py')!r}, run_name='__main__')\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ADB_DIR", str(d))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    return d


def table(d: Path, root: bool = True) -> None:
    (d / "table.json").write_text(json.dumps({
        "devices": ["List of devices attached\nemulator-5554\tdevice\n\n", 0],
        "shell id -u": ["0\n" if root else "2000\n", 0],
        "shell getprop ro.product.cpu.abi": ["x86_64\n", 0],
        "shell getprop ro.build.version.sdk": ["34\n", 0],
        "shell dumpsys package org.example.demo": ["Unable to find package\n", 0],
        "shell wm density": ["Physical density: 320\n", 0],
        "shell cat /proc/uptime": ["1000.50 900.00\n", 0],
    }))


def calls(d: Path) -> list[str]:
    return (d / "calls.log").read_text().splitlines()


def wait_for(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_record_android_without_device_exit_3(adb, apk, tmp_path, capsys):
    (adb / "table.json").write_text(json.dumps({"devices": ["List of devices attached\n\n", 0]}))
    assert main(["record", "--android", "-o", str(tmp_path / "c"), str(apk)]) == 3
    assert "no device" in capsys.readouterr().err


def _run_with_mark(adb, apk, out):
    (adb / "device" / "pidof").write_text("1234\n")
    shutil.copy(TREES / "android-main.xml", adb / "device" / "atl-doctor.xml")
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("rc", main(["record", "--android", "-o", str(out), str(apk)])))
    t.start()
    try:
        assert wait_for(lambda: (out / "control").exists())
        assert wait_for(lambda: any("monkey" in c for c in calls(adb)))
        assert main(["mark", "-d", str(out), "Main"]) == 0
        assert wait_for(lambda: (out / "trees" / "01-Main.xml").is_file())
    finally:
        (adb / "device" / "pidof").unlink()   # the app ends
        t.join(30)
    assert not t.is_alive()
    return res["rc"]


def test_record_android_flow_with_fake_adb(adb, apk, tmp_path, capsys):
    table(adb)
    agent_dir = tmp_path / "home/.local/lib/atl-doctor"
    agent_dir.mkdir(parents=True)
    (agent_dir / "libatl_doctor_agent-android-x86_64.so").write_bytes(b"\x7fELF")
    (adb / "device" / "atl-doctor-agent.tsv").write_text("I\t5\t1\tagent started\n")
    out = tmp_path / "c"
    assert _run_with_mark(adb, apk, out) == 0
    c = calls(adb)
    assert any(x.startswith("install -r ") for x in c)
    assert "shell uiautomator dump /sdcard/atl-doctor.xml" in c
    assert "shell dumpsys gfxinfo org.example.demo reset" in c
    assert "shell dumpsys meminfo org.example.demo" in c
    assert "shell dumpsys gfxinfo org.example.demo framestats" in c and (out / "perf/gfxinfo-01.txt").is_file()
    assert "shell setprop wrap.org.example.demo /data/local/tmp/atl-doctor/wrap.sh" in c
    assert [x for x in c if x.startswith("shell setprop wrap.")][-1] == 'shell setprop wrap.org.example.demo ""'
    m = Capture.open(out).read_manifest()
    assert m.side == "android" and m.ended and m.package == PKG and m.sdk_int == 34
    assert m.environment["density"] == "2.0" and m.environment["serial"] == "emulator-5554"
    marks = Capture(out).read_marks()
    assert [k.name for k in marks] == ["start", "Main"] and marks[1].boot_us == 1000500000
    assert (out / "agent.tsv").read_text().startswith("I\t5\t1\t")
    log = (out / "atl.log").read_text()
    assert "org.example.demo" in log and "not the app" not in log   # other processes filtered out
    assert "android-agent unavailable" not in capsys.readouterr().err


def test_android_without_root_skips_agent(adb, apk, tmp_path, capsys):
    table(adb, root=False)
    assert _run_with_mark(adb, apk, tmp_path / "c") == 0
    assert "atl-doctor: android-agent unavailable: device is not rooted" in capsys.readouterr().err
    assert not any(x.startswith("shell setprop wrap.") for x in calls(adb))


def test_android_installed_same_version_not_reinstalled(adb, tmp_path):
    apk = make_apk(tmp_path / "v1.apk", [make_dex([])], {"package": PKG, "versionCode": "1"})
    table(adb, root=False)
    t = json.loads((adb / "table.json").read_text())
    t["shell dumpsys package org.example.demo"] = ["Packages:\n    versionCode=1 minSdk=21 targetSdk=34\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    _run_with_mark(adb, apk, tmp_path / "c")
    assert not any(x.startswith("install") for x in calls(adb))


def _exc(fid_key, side, count, phase="start", idx=0):
    return Finding("exc", fid_key, Klass.OTHER, fid_key, occurrences=[Occurrence(phase, idx, side, count)])


def test_compare_sides_classes():
    atl = [_exc("A@x", "atl", 1), _exc("B@x", "atl", 9), _exc("C@x", "atl", 2),
           Finding("stub", "S#m", Klass.STUB_RESULT_USED, "s", occurrences=[Occurrence("start", 0, "atl")])]
    android = [_exc("B@x", "android", 3), _exc("C@x", "android", 2), _exc("D@x", "android", 1)]
    fs = {f.id: f for f in compare_sides(atl, android)}
    assert fs["exc:A@x"].klass is Klass.EXC_ATL_ONLY and not fs["exc:A@x"].on_android
    assert fs["exc:B@x"].klass is Klass.EXC_ATL_ONLY and fs["exc:B@x"].on_android     # 9 >= 3 * 3
    assert fs["exc:C@x"].klass is Klass.OTHER and fs["exc:C@x"].on_android
    assert {o.side for o in fs["exc:C@x"].occurrences} == {"atl", "android"}
    assert fs["exc:D@x"].klass is Klass.OTHER and {o.side for o in fs["exc:D@x"].occurrences} == {"android"}
    assert fs["stub:S#m"].klass is Klass.STUB_RESULT_USED


def test_lifecycle_paired_by_phase_name():
    atl = {0: ["A:onCreate:org.example.demo.Main"], 1: ["A:onPause:org.example.demo.Main"],
           2: ["A:onResume:org.example.demo.Main", "W:onWindowFocusChanged:org.example.demo.Main"]}
    android = {0: ["A:onCreate:org.example.demo.Main"],
               1: ["A:onResume:org.example.demo.Main", "A:onStop:org.example.demo.Main"]}
    fs = lifecycle_findings(atl, android, ["start", "Extra", "Open"], ["start", "Open"])
    assert [f.id for f in fs] == ["lc:Open:W:onWindowFocusChanged:org.example.demo.Main"]
    f = fs[0]
    assert f.klass is Klass.OTHER and f.details["android"] == android[1] and f.details["atl"] == atl[2]


def _capture(path: Path, side: str, marks: list[str], tree: Path | None, density: str | None = None) -> Path:
    cap = Capture.create(path, side)
    for name in marks:
        cap.append_mark(name)
    m = cap.read_manifest()
    m.ended, m.package, m.exit_status = "2026-11-01T07:00:00+00:00", PKG, None
    if density:
        m.environment["density"] = density
    cap.write_manifest(m)
    if tree:
        (path / "trees").mkdir()
        shutil.copy(tree, path / "trees" / "01-Main.xml")
    return path


def test_report_two_dirs_tree_diff(tmp_path, capsys):
    a = _capture(tmp_path / "atl", "atl", ["Main"], TREES / "atl-main.xml")
    b = _capture(tmp_path / "android", "android", ["Main"], TREES / "android-main.xml", density="2.0")
    assert main(["report", "-f", "json", str(a), str(b)]) == 0
    r = json.loads(capsys.readouterr().out)
    ids = {f["kind"] + ":" + f["key"] for f in r["findings"]}
    assert "tree:id/name_field:height" in ids
    assert r["environment"]["android_capture"] == str(b)


def test_report_two_dirs_density_option(tmp_path, capsys):
    a = _capture(tmp_path / "atl", "atl", ["Main"], TREES / "atl-main.xml")
    b = _capture(tmp_path / "android", "android", ["Main"], TREES / "android-main.xml")
    assert main(["report", "--density", "2", "-f", "json", str(a), str(b)]) == 0
    r = json.loads(capsys.readouterr().out)
    assert any(f["key"] == "id/name_field:height" for f in r["findings"])


def test_report_second_dir_must_be_android(tmp_path):
    a = _capture(tmp_path / "atl", "atl", ["Main"], None)
    b = _capture(tmp_path / "atl2", "atl", ["Main"], None)
    assert main(["report", str(a), str(b)]) == 2


def test_android_steps(adb, apk, tmp_path):
    table(adb, root=False)
    t = json.loads((adb / "table.json").read_text())
    t["shell dumpsys window"] = ["  mCurrentFocus=Window{1 u0 org.example.demo/org.example.demo.Main}\n", 0]
    t["shell dumpsys gfxinfo org.example.demo"] = ["Total frames rendered: 12\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    (adb / "device" / "pidof").write_text("1234\n")
    shutil.copy(TREES / "android-main.xml", adb / "device" / "atl-doctor.xml")
    steps = tmp_path / "s.steps"
    steps.write_text('wait-window org.example.demo 5\nclick 10,20\nclick "Save"\nkey Escape\nkey ctrl+a\n'
                     'wait-idle 5\nmark "M"\n')
    out = tmp_path / "c"
    assert main(["record", "--android", "--steps", str(steps), "-o", str(out), str(apk)]) == 0
    c = calls(adb)
    assert "shell input tap 20 40" in c
    assert "shell input tap 400 650" in c            # centre of id/save: (200..600, 600..700) px
    assert "shell input keyevent KEYCODE_BACK" in c
    assert "shell input keycombination KEYCODE_CTRL_LEFT KEYCODE_A" in c
    assert [k.name for k in Capture(out).read_marks()] == ["start", "M"]
    assert Capture(out).read_manifest().environment["stopped_by"] == "steps"
    assert len((out / "steps.tsv").read_text().splitlines()) == 7
    assert "shell am force-stop org.example.demo" in c[-6:] or c.count("shell am force-stop org.example.demo") >= 2


def test_android_mark_screenshot(adb, apk, tmp_path, capsys):
    table(adb)
    (adb / "device" / "screencap.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    out = tmp_path / "c"
    assert _run_with_mark(adb, apk, out) == 0
    assert "exec-out screencap -p" in calls(adb)
    assert (out / "shots/01-Main.png").read_bytes().endswith(b"fake")
    capsys.readouterr()
    (adb / "device" / "screencap.png").unlink()
    assert _run_with_mark(adb, apk, tmp_path / "d") == 0
    assert "no screenshot for mark 1 (Main): screencap failed" in capsys.readouterr().err


def test_android_scroll_notches_and_smooth(adb, apk, tmp_path):
    table(adb, root=False)
    t = json.loads((adb / "table.json").read_text())
    t["shell dumpsys window"] = ["  mCurrentFocus=Window{1 u0 org.example.demo/org.example.demo.Main}\n", 0]
    t["shell dumpsys gfxinfo org.example.demo"] = ["Total frames rendered: 12\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    (adb / "device" / "pidof").write_text("1234\n")
    steps = tmp_path / "s.steps"
    steps.write_text('wait-window org.example.demo 5\nscroll 2\nscroll-smooth -128\n')
    assert main(["record", "--android", "--steps", str(steps), "-o", str(tmp_path / "c"), str(apk)]) == 0
    sw = [c.split() for c in calls(adb) if c.startswith("shell input swipe")]
    assert len(sw) == 3                        # one gesture per notch, one for the smooth scroll
    dist = [int(s[4]) - int(s[6]) for s in sw]  # y1 - y2
    assert dist[0] == dist[1] > 0 and dist[2] == -2 * dist[0]   # 64 dp per notch, -128 dp smooth


@pytest.mark.parametrize("before,restored", [("Enforcing", True), ("Permissive", False)])
def test_selinux_mode_is_restored_not_forced(adb, apk, tmp_path, before, restored):
    table(adb)
    t = json.loads((adb / "table.json").read_text())
    t["shell getenforce"] = [before + "\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    agent_dir = tmp_path / "home/.local/lib/atl-doctor"
    agent_dir.mkdir(parents=True)
    (agent_dir / "libatl_doctor_agent-android-x86_64.so").write_bytes(b"\x7fELF")
    assert _run_with_mark(adb, apk, tmp_path / "c") == 0
    c = calls(adb)
    assert "shell setenforce 0" in c
    assert ("shell setenforce 1" in c) == restored
