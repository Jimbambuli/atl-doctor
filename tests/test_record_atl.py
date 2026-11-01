# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from atl_doctor.capture import Capture
from atl_doctor.cli import main
from atl_doctor.record_atl import RecordOptions, build_command, default_out_dir, send_mark
from dexgen import make_apk, make_dex

HERE = Path(__file__).resolve().parent


@pytest.fixture
def tmp_prefix(tmp_path, monkeypatch):
    p = tmp_path / "prefix"
    (p / "bin").mkdir(parents=True)
    exe = p / "bin/android-translation-layer"
    exe.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv[0] = {str(HERE / 'fake_atl.py')!r}\n"
                   f"runpy.run_path({str(HERE / 'fake_atl.py')!r}, run_name='__main__')\n")
    exe.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no helpers from ~/.local/lib/atl-doctor
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    monkeypatch.delenv("ATL_DOCTOR_AGENT", raising=False)
    return p


@pytest.fixture
def apk(tmp_path):
    return make_apk(tmp_path / "demo.apk", [make_dex([])])


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_record_writes_manifest_log_and_exit(tmp_prefix, apk, tmp_path):
    assert main(["record", "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), str(apk), "--", "--exit-code", "0"]) == 0
    m = Capture.open(tmp_path / "c").read_manifest()
    assert m.ended and m.exit_status == 0 and m.signal is None and m.package == "org.example.demo"
    assert "hello from fake atl" in (tmp_path / "c/atl.log").read_text()
    assert m.command[0] == str(tmp_prefix / "bin/android-translation-layer")
    assert {p["name"] for p in m.probes} >= {"atl", "agent", "tracecount"}


def test_record_reports_missing_probes_once(tmp_prefix, apk, tmp_path, capsys):
    main(["record", "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), str(apk)])
    err = capsys.readouterr().err
    assert err.count("atl-doctor: jvmti-plugin unavailable: ") == 1
    assert err.count("atl-doctor: tracecount unavailable: ") == 1


def test_record_app_crash_is_still_valid(tmp_prefix, apk, tmp_path, capsys):
    assert main(["record", "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), str(apk), "--", "--signal", "SIGSEGV"]) == 0
    m = Capture.open(tmp_path / "c").read_manifest()
    assert m.signal == "SIGSEGV" and m.exit_status is None and m.ended
    capsys.readouterr()
    assert main(["report", str(tmp_path / "c")]) == 0
    out = capsys.readouterr().out
    assert "fatal signal SIGSEGV" in out and "Crashes" in out


def _record_in_thread(argv):
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("rc", main(argv)))
    t.start()
    return t, res


def test_mark_during_recording(tmp_prefix, apk, tmp_path):
    c = tmp_path / "c"
    t, res = _record_in_thread(["record", "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait"])
    assert wait_for(lambda: (c / "control").exists())
    assert main(["mark", "-d", str(c), "Import"]) == 0
    assert main(["mark", "-d", str(c)]) == 0
    assert wait_for(lambda: len(Capture(c).read_marks()) == 3)
    assert [m.name for m in Capture(c).read_marks()] == ["start", "Import", "mark 2"]
    os.kill(int(Capture(c).read_manifest().environment["atl_pid"]), signal.SIGTERM)
    t.join(20)
    assert not t.is_alive() and res["rc"] == 0


def test_mark_requests_view_tree(tmp_prefix, apk, tmp_path, monkeypatch):
    import atl_doctor.record_atl as ra
    monkeypatch.setattr(ra, "has_viewdump", lambda prefix: True)
    c = tmp_path / "c"
    t, res = _record_in_thread(["record", "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait",
                                "--viewdump"])
    try:
        assert wait_for(lambda: (c / "control").exists())
        assert main(["mark", "-d", str(c), "Main view"]) == 0
        assert wait_for(lambda: (c / "trees/01-Main-view.xml").is_file())
    finally:
        wait_for(lambda: "atl_pid" in Capture(c).read_manifest().environment)
        os.kill(int(Capture(c).read_manifest().environment["atl_pid"]), signal.SIGTERM)
        t.join(20)
    assert not t.is_alive() and res["rc"] == 0


def test_mark_without_recorder_times_out(tmp_path, capsys):
    Capture.create(tmp_path / "c", "atl")
    os.mkfifo(tmp_path / "c/control")
    t = time.monotonic()
    assert main(["mark", "-d", str(tmp_path / "c"), "x"]) == 1
    assert time.monotonic() - t < 7
    assert f"no recording is reading {tmp_path / 'c'}/control" in capsys.readouterr().err


def test_mark_text_sanitized(tmp_path):
    c = tmp_path / "c"
    Capture.create(c, "atl")
    os.mkfifo(c / "control")
    fd = os.open(c / "control", os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert send_mark(c, "a\tb\nc") == 0
        assert os.read(fd, 100) == b"mark a b c\n"
    finally:
        os.close(fd)


def test_mark_finds_newest_capture(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["mark", "x"]) == 1
    assert "no recording" in capsys.readouterr().err


def test_sigint_ends_recording_cleanly(tmp_prefix, apk, tmp_path):
    c = tmp_path / "c"
    code = ("import sys; from atl_doctor.cli import main; "
            f"sys.exit(main(['record', '-o', {str(c)!r}, '--prefix', {str(tmp_prefix)!r}, {str(apk)!r}, '--', '--wait']))")
    p = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(HERE.parent)})
    assert wait_for(lambda: (c / "control").exists() and "fake atl" in (c / "atl.log").read_text())
    p.send_signal(signal.SIGINT)
    assert p.wait(timeout=20) == 0
    m = Capture.open(c).read_manifest()
    assert m.ended and m.signal is None and m.exit_status is None and m.environment["stopped_by"] == "SIGINT"
    assert "fake atl: SIGTERM" in (c / "atl.log").read_text()


def test_build_command_contains_trace_and_agent(tmp_path):
    cap = Capture.create(tmp_path / "c", "atl")
    opts = RecordOptions(apk=Path("/x/app.apk"), out=None, prefix=Path("/p"), atl_src=None, agent=None,
                         atl_args=["--sdk-int", "34"])
    cmd, env = build_command(opts, cap, Path("/p/lib64/art/libopenjdkjvmti.so"), Path("/h/libatl_doctor_agent.so"))
    c = cap.root
    assert cmd == ["/p/bin/android-translation-layer", "/x/app.apk", "--sdk-int", "34",
                   "-X", "-Xmethod-trace", "-X", "-Xmethod-trace-stream", "-X", f"-Xmethod-trace-file:{c}/trace/trace.bin",
                   "-X", f"-agentpath:/h/libatl_doctor_agent.so={c}/agent.tsv,late=/p/lib64/art/libopenjdkjvmti.so,"
                         f"latesec=3,marks={c}/marks.tsv,arm={c}/armed,frames,hang=100"]
    assert env == {"LD_LIBRARY_PATH": "/p/lib64:/p/lib64/art"}
    cmd, _ = build_command(opts, cap, None, Path("/h/libatl_doctor_agent.so"))
    assert not any(a.startswith("-agentpath") for a in cmd)
    cmd, _ = build_command(opts, cap, None, None, trace=False)
    assert cmd == ["/p/bin/android-translation-layer", "/x/app.apk", "--sdk-int", "34"]


def test_default_out_dir():
    assert default_out_dir(datetime(2026, 11, 1, 9, 5, 7)) == Path("atl-doctor-20261101-090507")


def test_missing_prefix_exit_3(apk, tmp_path, monkeypatch):
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    assert main(["record", "-o", str(tmp_path / "c"), str(apk)]) == 3
    assert main(["record", "-o", str(tmp_path / "d"), "--prefix", str(tmp_path / "nope"), str(apk)]) == 3


def test_record_trace_to_stub_report(built, tmp_prefix, apk, tmp_path, capsys):
    """End to end with the real counter: stub list from the test ATL tree, trace from the fake ATL."""
    helpers = tmp_prefix / "lib/atl-doctor"
    helpers.mkdir(parents=True)
    shutil.copy(built / "atl-doctor-tracecount", helpers)
    c = tmp_path / "c"
    assert main(["record", "-o", str(c), "--prefix", str(tmp_prefix), "--atl-src", str(HERE / "data/atl-src"),
                 str(apk), "--", "--trace"]) == 0
    assert (c / "trace/stubs.tsv").is_file() and (c / "trace/tr.callers").is_file()
    assert not (c / "trace/trace.bin").exists() or (c / "trace/trace.bin").stat().st_blocks < 64
    capsys.readouterr()
    assert main(["report", str(c)]) == 0
    out = capsys.readouterr().out
    assert "stub android.graphics.drawable.Drawable#parseBlendMode" in out


def test_report_without_apk_notes_it(tmp_path, capsys):
    c = Capture.create(tmp_path / "c", "atl")
    (c.path("trace")).mkdir()
    (c.path("trace") / "stubs.tsv").write_text("kind\tclass\tmethod\tsig\tlocation\tbytecode_kind\tcomment\n")
    m = c.read_manifest()
    m.apk, m.ended, m.exit_status = str(tmp_path / "gone.apk"), "2026-11-01T10:00:00+00:00", 0
    c.write_manifest(m)
    assert main(["report", str(c.root)]) == 0
    assert "stub results not checked" in capsys.readouterr().out


class _FakeSession:
    started = stopped = False

    def __init__(self, run_dir):
        self.run_dir = run_dir

    def start(self, width=1400, height=900):
        _FakeSession.started = True

    def env(self):
        return {"WAYLAND_DISPLAY": "wayland-fake"}

    def stop(self):
        _FakeSession.stopped = True


def _fake_steps(monkeypatch):
    import atl_doctor.record_atl as ra
    from test_steps import FakeDriver

    drivers = []

    def make(cap, session, opts, package):
        d = FakeDriver(has_tree=False)
        d.mark = lambda name: ra.send_mark(cap.root, name)
        d.close = lambda: None
        drivers.append(d)
        return d

    _FakeSession.started = _FakeSession.stopped = False
    monkeypatch.setattr(ra, "HeadlessSession", _FakeSession)
    monkeypatch.setattr(ra, "make_atl_driver", make)
    return drivers


def test_steps_run_and_end_recording(tmp_prefix, apk, tmp_path, monkeypatch, capsys):
    drivers = _fake_steps(monkeypatch)
    steps = tmp_path / "s.steps"
    steps.write_text('wait-window "Demo"\nmark "Open"\nclick 10,20\nkey Escape\n')
    c = tmp_path / "c"
    assert main(["record", "--steps", str(steps), "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait"]) == 0
    assert "--steps implies --headless" in capsys.readouterr().err
    m = Capture.open(c).read_manifest()
    assert m.ended and m.environment["stopped_by"] == "steps"
    assert [k.name for k in Capture(c).read_marks()] == ["start", "Open"]
    assert ("click", 10.0, 20.0) in drivers[0].calls
    assert len((c / "steps.log").read_text().splitlines()) == 4
    assert _FakeSession.started and _FakeSession.stopped
    assert "-w" in m.command and "1333" in m.command and "WAYLAND_DISPLAY" not in m.environment


def test_step_failure_ends_recording_exit_1(tmp_prefix, apk, tmp_path, monkeypatch, capsys):
    _fake_steps(monkeypatch)
    steps = tmp_path / "s.steps"
    steps.write_text('wait 0\nwait-window never 0.2\nkey Escape\n')
    c = tmp_path / "c"
    assert main(["record", "--steps", str(steps), "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait"]) == 1
    assert f"{steps}:2: no window matching 'never' within 0.2 s" in capsys.readouterr().err
    m = Capture.open(c).read_manifest()
    assert m.ended and m.environment["stopped_by"] == "steps"


def test_steps_syntax_error_before_recording(tmp_prefix, apk, tmp_path, capsys):
    steps = tmp_path / "s.steps"
    steps.write_text('frobnicate\n')
    assert main(["record", "--steps", str(steps), "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), str(apk)]) == 1
    assert f"{steps}:1: unknown command 'frobnicate'" in capsys.readouterr().err
    assert not (tmp_path / "c").exists()


def test_steps_without_input_helper_exit_3(tmp_prefix, apk, tmp_path, monkeypatch):
    import atl_doctor.record_atl as ra
    monkeypatch.setattr(ra, "HeadlessSession", _FakeSession)
    steps = tmp_path / "s.steps"
    steps.write_text('key Escape\n')
    assert main(["record", "--steps", str(steps), "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), str(apk)]) == 3


def test_memory_rows_per_mark(tmp_prefix, apk, tmp_path):
    c = tmp_path / "c"
    t, res = _record_in_thread(["record", "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait"])
    try:
        assert wait_for(lambda: (c / "control").exists())
        assert main(["mark", "-d", str(c), "Open"]) == 0
        assert wait_for(lambda: (c / "memory.tsv").is_file())
    finally:
        wait_for(lambda: "atl_pid" in Capture(c).read_manifest().environment)
        os.kill(int(Capture(c).read_manifest().environment["atl_pid"]), signal.SIGTERM)
        t.join(20)
    rows = [ln.split("\t") for ln in (c / "memory.tsv").read_text().splitlines()]
    assert {(r[0], r[1], r[2]) for r in rows} >= {("1", "atl", "pss"), ("1", "atl", "anon"), ("1", "atl", "native_heap")}
    assert all(r[3].isdigit() for r in rows)


def test_build_command_heap():
    opts = RecordOptions(apk=Path("/x/app.apk"), out=None, prefix=Path("/p"), atl_src=None, agent=None, heap=True)
    cmd, _ = build_command(opts, Capture(Path("/c")), Path("/p/lib64/art/libopenjdkjvmti.so"), Path("/h/a.so"))
    assert cmd[-1].endswith(",frames,hang=100,heap")
