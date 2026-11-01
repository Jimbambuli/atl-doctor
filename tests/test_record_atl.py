# SPDX-License-Identifier: GPL-3.0-or-later
import json
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
from dexgen import cls, make_apk, make_dex, make_jar

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
    assert "Fix first" not in out          # the app does not use the result itself


def test_stub_result_used_by_atl_itself(built, tmp_prefix, apk, tmp_path, capsys):
    """A placeholder result consumed by ATL's own code (ImageView -> parseBlendMode) ranks like an app use."""
    helpers = tmp_prefix / "lib/atl-doctor"
    helpers.mkdir(parents=True)
    shutil.copy(built / "atl-doctor-tracecount", helpers)
    drawable = "Landroid/graphics/drawable/Drawable;->parseBlendMode(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;"
    dex_dir = tmp_prefix / "lib64/java/dex/android_translation_layer"
    dex_dir.mkdir(parents=True)
    make_jar(dex_dir / "api-impl.jar", [make_dex([cls("Landroid/widget/ImageView;",
                                                       calls=[("invoke-static", drawable), ("move-result-object", 0)])])])
    c = tmp_path / "c"
    assert main(["record", "-o", str(c), "--prefix", str(tmp_prefix), "--atl-src", str(HERE / "data/atl-src"),
                 str(apk), "--", "--trace"]) == 0
    capsys.readouterr()
    assert main(["report", "-f", "json", str(c)]) == 0
    doc = json.loads(capsys.readouterr().out)
    f = next(x for x in doc["findings"] if x["id"] == "stub:android.graphics.drawable.Drawable#parseBlendMode")
    assert f["class"] == "stub-result-used"


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

    @staticmethod
    def missing():
        return None

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
    rows = [ln.split("\t") for ln in (c / "steps.tsv").read_text().splitlines()]
    assert [r[2] for r in rows] == ["wait-window", "mark", "click", "key"] and all(len(r) == 9 for r in rows)
    assert _FakeSession.started and _FakeSession.stopped
    assert "-w" in m.command and "1333" in m.command and "WAYLAND_DISPLAY" not in m.environment


def test_mark_in_last_step_still_gets_its_tree(tmp_prefix, apk, tmp_path, monkeypatch):
    import atl_doctor.record_atl as ra
    import atl_doctor.sources.shots as shots
    _fake_steps(monkeypatch)
    monkeypatch.setattr(ra, "has_viewdump", lambda prefix: True)
    monkeypatch.setattr(shots, "take_atl_shot", lambda session, path: True)
    steps = tmp_path / "s.steps"
    steps.write_text('wait-window "Demo"\nmark "Last"\n')
    c = tmp_path / "c"
    assert main(["record", "--steps", str(steps), "-o", str(c), "--prefix", str(tmp_prefix), str(apk), "--", "--wait",
                 "--viewdump", "--dump-delay", "1"]) == 0
    assert (c / "trees/01-Last.xml").is_file()      # ATL is ended only after the pending tree request


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


def test_mark_screenshot_with_surface_origin(tmp_path, monkeypatch):
    import atl_doctor.record_atl as ra
    from atl_doctor.capture import Mark
    from atl_doctor.sources.shots import read_origin

    class Session:
        def windows(self):
            return [{"kind": "normal", "rect": (0.0, 0.0, 100.0, 100.0), "surface": (-14.0, -12.0)}]

        def screenshot(self, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"png")
            return True

    cap = Capture.create(tmp_path / "c", "atl")
    monkeypatch.setattr("atl_doctor.sources.tree.request_atl_tree", lambda d, name: d / f"{name}.xml")
    threads = []
    monkeypatch.setattr(ra.threading, "Thread", lambda target, daemon: threads.append(target) or type("T", (), {"start": lambda s: target()})())
    ra._request_tree(cap, Mark(1, 0, 0, "Main view"), Session())
    assert read_origin(tmp_path / "c/shots/01-Main-view.png") == (-14.0, -12.0)


def test_spectacle_probe_needs_headless(tmp_prefix, apk, tmp_path):
    assert main(["record", "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), str(apk)]) == 0
    p = next(p for p in Capture.open(tmp_path / "c").read_manifest().probes if p["name"] == "spectacle")
    assert not p["available"] and p["reason"] == "screenshots need --headless"


def test_build_command_preloads_wheel_shim(tmp_path, monkeypatch):
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    cap = Capture.create(tmp_path / "c", "atl")
    opts = RecordOptions(apk=Path("/x/app.apk"), out=None, prefix=Path("/p"), atl_src=None, agent=None, atl_args=[])
    _, env = build_command(opts, cap, None, None, trace=False, wheel=Path("/h/libatl_doctor_wheel.so"))
    assert env["LD_PRELOAD"] == "/h/libatl_doctor_wheel.so"
    assert env["ATL_DOCTOR_WHEEL_LOG"] == f"{cap.root}/run/wheel.log"
    monkeypatch.setenv("LD_PRELOAD", "/x/other.so")
    _, env = build_command(opts, cap, None, None, trace=False, wheel=Path("/h/libatl_doctor_wheel.so"))
    assert env["LD_PRELOAD"] == "/h/libatl_doctor_wheel.so:/x/other.so"
    _, env = build_command(opts, cap, None, None, trace=False)
    assert "LD_PRELOAD" not in env


def test_ctrl_c_to_process_group_keeps_stub_counts(built, tmp_prefix, apk, tmp_path):
    """Ctrl-C in a terminal hits the whole foreground group: the counter must survive it and still write its counts."""
    helpers = tmp_prefix / "lib/atl-doctor"
    helpers.mkdir(parents=True)
    shutil.copy(built / "atl-doctor-tracecount", helpers)
    c = tmp_path / "c"
    code = ("import sys; from atl_doctor.cli import main; "
            f"sys.exit(main(['record', '-o', {str(c)!r}, '--prefix', {str(tmp_prefix)!r}, "
            f"'--atl-src', {str(HERE / 'data/atl-src')!r}, {str(apk)!r}, '--', '--trace', '--wait']))")
    p = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(HERE.parent)},
                         start_new_session=True)  # own group, like a shell job
    assert wait_for(lambda: (c / "control").exists() and "fake atl" in (c / "atl.log").read_text())
    time.sleep(0.5)
    os.killpg(p.pid, signal.SIGINT)
    assert p.wait(timeout=60) == 0
    assert (c / "trace/tr.counts").is_file() and (c / "trace/tr.callers").is_file()
    assert "tracecount" not in Capture.open(c).read_manifest().environment


def test_failed_counter_is_reported(tmp_prefix, apk, tmp_path, capsys):
    """A counter that dies without its counts: warning and an environment note, not a silent empty stub list."""
    helpers = tmp_prefix / "lib/atl-doctor"
    helpers.mkdir(parents=True)
    (helpers / "atl-doctor-tracecount").write_text("#!/bin/sh\nexit 3\n")
    (helpers / "atl-doctor-tracecount").chmod(0o755)
    c = tmp_path / "c"
    assert main(["record", "-o", str(c), "--prefix", str(tmp_prefix), "--atl-src", str(HERE / "data/atl-src"),
                 str(apk)]) == 0
    assert "tracecount failed" in capsys.readouterr().err
    note = Capture.open(c).read_manifest().environment["tracecount"]
    assert note.startswith("failed")
    assert main(["report", str(c)]) == 0
    assert "tracecount: failed" in capsys.readouterr().out


def test_headless_session_passes_paths_as_arguments_not_shell_text(monkeypatch, request):
    """A capture path with shell characters must reach sh as an argument, never inside the script."""
    from atl_doctor import headless
    from atl_doctor.errors import DoctorError

    import tempfile
    base = Path(tempfile.mkdtemp(prefix="h"))  # short: the session runs right there, not in a temporary directory
    request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    run = base / '$(x)`y`"' / "run"
    run.parent.mkdir()
    seen = {}

    class Dead:
        def poll(self):
            return 1

    def popen(args, **kw):
        seen["args"] = args
        return Dead()

    monkeypatch.setattr(headless.subprocess, "Popen", popen)
    s = headless.HeadlessSession(run)
    assert s.run_dir == run
    monkeypatch.setattr(s, "stop", lambda: None)  # the fake process must not be signalled
    with pytest.raises(DoctorError):
        s.start()
    script = seen["args"][seen["args"].index("-c") + 1]
    assert str(s.run_dir) not in script and "$(" not in script.replace("$DBUS_SESSION_BUS_ADDRESS", "")
    assert str(s.run_dir / "bus") in seen["args"]


def test_explicit_agent_that_does_not_exist_exit_3(tmp_prefix, apk, tmp_path, capsys):
    c = tmp_path / "c"
    assert main(["record", "-o", str(c), "--prefix", str(tmp_prefix), "--agent", str(tmp_path / "nope.so"), str(apk)]) == 3
    assert "nope.so not found" in capsys.readouterr().err and not c.exists()


def test_headless_without_kwin_exit_3_before_the_capture(tmp_prefix, apk, tmp_path, monkeypatch, capsys):
    import atl_doctor.headless as headless
    monkeypatch.setattr(headless.shutil, "which", lambda name: None)
    c = tmp_path / "c"
    assert main(["record", "--headless", "-o", str(c), "--prefix", str(tmp_prefix), str(apk)]) == 3
    assert "kwin_wayland" in capsys.readouterr().err and not c.exists()


def test_verbose_shows_helpers_and_atl_command(tmp_prefix, apk, tmp_path, capsys):
    assert main(["record", "-o", str(tmp_path / "a"), "--prefix", str(tmp_prefix), str(apk)]) == 0
    assert "command:" not in capsys.readouterr().err
    assert main(["record", "-v", "-o", str(tmp_path / "b"), "--prefix", str(tmp_prefix), str(apk)]) == 0
    err = capsys.readouterr().err
    assert f"command: {tmp_prefix}/bin/android-translation-layer " in err and "helpers: agent " in err


def test_capture_path_that_breaks_the_agent_options_exit_2(tmp_prefix, apk, tmp_path, capsys):
    """The capture path goes into the comma-separated agent options and 512-byte buffers of the agent."""
    for out in (tmp_path / "a,b", tmp_path / ("x" * 150) / ("y" * 150) / ("z" * 150)):
        assert main(["record", "-o", str(out), "--prefix", str(tmp_prefix), str(apk)]) == 2
        assert not out.exists()
    err = capsys.readouterr().err
    assert "must not contain ','" in err and "too long" in err


# ------------------------------------------------------------------ --launcher
def _launcher() -> str:
    """A launcher command: the fake ATL behind an interpreter, the way `flatpak run ID` stands for ATL."""
    import shlex
    return shlex.join([sys.executable, "-I", str(HERE / "fake_atl.py")])


def test_build_command_with_launcher_and_no_prefix(tmp_path):
    cap = Capture.create(tmp_path / "c", "atl")
    opts = RecordOptions(apk=Path("/x/app.apk"), out=None, prefix=None, atl_src=None, agent=None,
                         atl_args=["--sdk-int", "34"], launcher="flatpak run --user org.example.Starter")
    cmd, env = build_command(opts, cap, None, None, trace=False)
    assert cmd == ["flatpak", "run", "--user", "org.example.Starter", "/x/app.apk", "--sdk-int", "34"]
    assert "LD_LIBRARY_PATH" not in env


def test_record_with_launcher_needs_no_prefix(apk, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    c = tmp_path / "c"
    assert main(["record", "-o", str(c), "--launcher", _launcher(), str(apk), "--", "--exit-code", "0"]) == 0
    m = Capture.open(c).read_manifest()
    assert m.command[:3] == [sys.executable, "-I", str(HERE / "fake_atl.py")] and m.exit_status == 0
    assert m.environment["launcher"] == _launcher() and "prefix" not in m.environment
    assert "hello from fake atl" in (c / "atl.log").read_text()


def test_record_launcher_not_found_exit_3(apk, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    assert main(["record", "-o", str(tmp_path / "c"), "--launcher", "no-such-launcher-xyz run", str(apk)]) == 3
    assert "no-such-launcher-xyz" in capsys.readouterr().err


def test_launcher_and_prefix_exclude_each_other(apk, tmp_prefix, tmp_path):
    assert main(["record", "-o", str(tmp_path / "c"), "--prefix", str(tmp_prefix), "--launcher", _launcher(),
                 str(apk)]) == 2


def test_no_memory_rows_with_launcher(apk, tmp_path, monkeypatch):
    """The launcher's own process (flatpak run, a wrapper) is not the app: its memory would mislead."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    c = tmp_path / "c"
    t, res = _record_in_thread(["record", "-o", str(c), "--launcher", _launcher(), str(apk), "--", "--wait"])
    try:
        assert wait_for(lambda: (c / "control").exists())
        assert main(["mark", "-d", str(c), "Open"]) == 0
        assert wait_for(lambda: [k.name for k in Capture(c).read_marks()] == ["start", "Open"])
    finally:
        wait_for(lambda: "atl_pid" in Capture(c).read_manifest().environment)
        os.kill(int(Capture(c).read_manifest().environment["atl_pid"]), signal.SIGTERM)
        t.join(20)
    assert res["rc"] == 0 and not (c / "memory.tsv").exists()


def test_no_trace_starts_without_method_trace(built, tmp_prefix, apk, tmp_path):
    """--no-trace: full speed (timings, slow first starts), at the price of the stub hits."""
    helpers = tmp_prefix / "lib/atl-doctor"
    helpers.mkdir(parents=True)
    shutil.copy(built / "atl-doctor-tracecount", helpers)
    c = tmp_path / "c"
    assert main(["record", "--no-trace", "-o", str(c), "--prefix", str(tmp_prefix), str(apk)]) == 0
    m = Capture.open(c).read_manifest()
    assert not any(a.startswith("-Xmethod-trace") for a in m.command)
    assert not (c / "trace/tracecount.log").exists()


@pytest.mark.parametrize("rules, expected", [(None, "js.debug=true"), ("foo.info=false", "foo.info=false;js.debug=true")])
def test_headless_session_lets_kwin_script_output_reach_its_log(monkeypatch, tmp_path, rules, expected):
    """KWin scripts print at debug level (category js) to KWin's log, which the session reads. Distributions switch
    debug output off (Fedora: qtlogging.ini *.debug=false) and Qt sends it to journald instead of stderr."""
    from atl_doctor import headless
    from atl_doctor.errors import DoctorError

    seen = {}

    class Dead:
        def poll(self):
            return 1

    def popen(args, **kw):
        seen["env"] = kw["env"]
        return Dead()

    if rules is None:
        monkeypatch.delenv("QT_LOGGING_RULES", raising=False)
    else:
        monkeypatch.setenv("QT_LOGGING_RULES", rules)
    monkeypatch.setattr(headless.subprocess, "Popen", popen)
    s = headless.HeadlessSession(tmp_path / "r")
    monkeypatch.setattr(s, "stop", lambda: None)
    with pytest.raises(DoctorError):
        s.start()
    assert seen["env"]["QT_LOGGING_RULES"] == expected and seen["env"]["QT_FORCE_STDERR_LOGGING"] == "1"
