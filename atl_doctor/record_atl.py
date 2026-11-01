# SPDX-License-Identifier: GPL-3.0-or-later
"""`record` under ATL and `mark`: start ATL with method trace and agent, collect phases, write the capture."""
from __future__ import annotations

import argparse
import errno
import os
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .capture import AGENT, ATL_LOG, CONTROL, MARKS, SHOTS, STEPS_DATA, STEPS_LOG, TRACE_DIR, TREES, Capture, Mark
from .env import AGENT_LIB, INPUT_HELPER, TRACECOUNT, WHEEL_SHIM, Probe, find_helper, has_viewdump, jvmti_plugin, probe_all, resolve
from .errors import DoctorError, PrerequisiteMissing
from .headless import APP_HEIGHT, APP_WIDTH, HeadlessSession
from .sources.trace import write_watch_file

ARM_FILE = "armed"
HANG_MS = 100     # main thread blocked longer than this is a hang (Spec 6.4)
RUN_DIR = "run"  # private KWin session: socket, D-Bus address, logs, input helper FIFO
ARM_AFTER_S = 5.0
TREE_WAIT_S = 30.0  # tree request (10 s) + screenshot of a mark sent by the last step
STOP_GRACE_S = 10.0
TRACE_FILE = "trace.bin"
TRACE_PREFIX = "tr"
STUBS_TSV = "stubs.tsv"
WATCH = "watch.txt"
# optional probes `record` uses; a missing one gives one stderr line and the recording goes on
RECORD_PROBES = ("atl-src", "jvmti-plugin", "agent", "tracecount", "viewdump")


@dataclass
class RecordOptions:
    apk: Path
    out: Path | None
    prefix: Path | None
    atl_src: Path | None
    agent: Path | None
    headless: bool = False
    steps: Path | None = None
    heap: bool = False
    android: bool = False
    atl_args: list[str] = field(default_factory=list)


def default_out_dir(now: datetime) -> Path:
    return Path(f"atl-doctor-{now:%Y%m%d-%H%M%S}")


def build_command(opts: RecordOptions, cap: Capture, plugin: Path | None, agent: Path | None,
                  trace: bool = True, wheel: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """ATL command line and the environment variables to add."""
    c = cap.root
    cmd = [str(opts.prefix / "bin/android-translation-layer"), str(opts.apk), *opts.atl_args]
    if trace:
        cmd += ["-X", "-Xmethod-trace", "-X", "-Xmethod-trace-stream",
                "-X", f"-Xmethod-trace-file:{c}/{TRACE_DIR}/{TRACE_FILE}"]
    if plugin and agent:
        cmd += ["-X", f"-agentpath:{agent}={c}/{AGENT},late={plugin},latesec=3,marks={c}/{MARKS},arm={c}/{ARM_FILE}"
                      f",frames,hang={HANG_MS}" + (",heap" if opts.heap else "")]
    env = {"LD_LIBRARY_PATH": f"{opts.prefix}/lib64:{opts.prefix}/lib64/art"}
    if has_viewdump(opts.prefix):
        env["ATL_VIEWDUMP"] = f"{c}/{TREES}"
    if wheel:  # turns the helper's marked wheel steps into real notches (source wheel); log lets one count them
        env["LD_PRELOAD"] = ":".join([str(wheel), *filter(None, [os.environ.get("LD_PRELOAD")])])
        env["ATL_DOCTOR_WHEEL_LOG"] = f"{c}/{RUN_DIR}/wheel.log"
    return cmd, env


def _warn(msg: str) -> None:
    from .cli import log

    log(0, msg)


def _signal_name(rc: int) -> str:
    try:
        return signal.Signals(-rc).name
    except ValueError:
        return f"SIG{-rc}"


class _Stop:
    """SIGINT/SIGTERM of `record` itself (only installable in the main thread)."""

    def __init__(self) -> None:
        self.by: str | None = None
        self.old: dict[int, object] = {}

    def __enter__(self) -> "_Stop":
        if threading.current_thread() is threading.main_thread():
            for s in (signal.SIGINT, signal.SIGTERM):
                self.old[s] = signal.signal(s, lambda n, _f: setattr(self, "by", signal.Signals(n).name))
        return self

    def __exit__(self, *_: object) -> None:
        for s, h in self.old.items():
            signal.signal(s, h)


def _stop_group(proc: subprocess.Popen) -> None:
    """SIGTERM to ATL's process group, SIGKILL after STOP_GRACE_S."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _handle_control(cap: Capture, data: bytes, buf: bytearray,
                    clock: Callable[[], int | None] | None = None) -> list[Mark]:
    """Process complete control lines; the marks they created (clock: boot time source, default local)."""
    new = []
    buf += data
    while b"\n" in buf:
        line, _, rest = bytes(buf).partition(b"\n")
        buf[:] = rest
        text = line.decode("utf-8", "replace")
        if text == "mark" or text.startswith("mark "):
            new.append(cap.append_mark(text[5:] or None, clock() if clock else None))
    return new


def drain_control(cap: Capture, ctl: int, buf: bytearray, clock: Callable[[], int | None] | None = None) -> list[Mark]:
    """Whatever is still in the control FIFO."""
    new: list[Mark] = []
    while True:
        try:
            data = os.read(ctl, 4096)
        except BlockingIOError:
            return new
        if not data:
            return new
        new += _handle_control(cap, data, buf, clock)


def _memory(cap: Capture, pid: int, index: int | str) -> None:
    from .sources.memory import smaps_snapshot, write_memory_rows

    snap = smaps_snapshot(Path(f"/proc/{pid}"))
    write_memory_rows(cap, [(index, "atl", k, v) for k, v in snap.items()])


def tree_name(mark: Mark) -> str:
    """trees/NN-name.xml: mark index and a file-name-safe form of its name."""
    return f"{mark.index:02d}-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", mark.name).strip("-") or "mark")


def _request_tree(cap: Capture, mark: Mark, session: HeadlessSession | None = None) -> threading.Thread:
    """In the background: the control loop must not wait for the app. With a headless session the mark also
    gets a screenshot (shots/NN-name.png)."""
    from .sources.shots import take_atl_shot
    from .sources.tree import request_atl_tree

    def run() -> None:
        if request_atl_tree(cap.path(TREES), tree_name(mark)) is None:
            _warn(f"no view tree for mark {mark.index} ({mark.name}): ATLViewDump did not answer")
        if session is not None:
            try:
                if not take_atl_shot(session, cap.path(SHOTS) / f"{tree_name(mark)}.png"):
                    _warn(f"no screenshot for mark {mark.index} ({mark.name}): spectacle failed")
            except DoctorError as e:
                _warn(f"no screenshot for mark {mark.index} ({mark.name}): {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def make_atl_driver(cap: Capture, session: HeadlessSession, opts: RecordOptions, package: str | None):
    """Start the input helper inside the session and return the step driver for ATL."""
    from .steps import AtlDriver

    helper = find_helper(INPUT_HELPER, None, opts.prefix)
    run = cap.path(RUN_DIR)
    fifo, status = run / "input", run / "input.status"
    os.mkfifo(fifo)
    with open(run / "input.log", "wb") as log:
        proc = subprocess.Popen([str(helper), "serve", str(fifo), str(status)], env={**os.environ, **session.env()},
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    frames = jvmti_plugin(opts.prefix) is not None and find_helper(AGENT_LIB, opts.agent, opts.prefix) is not None
    d = AtlDriver(cap, session, fifo, status, has_viewdump(opts.prefix), frames=frames, package=package)
    close = d.close

    def close_all() -> None:
        close()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    d.close = close_all  # type: ignore[method-assign]
    return d


_make_atl_driver = make_atl_driver  # tests replace make_atl_driver; the helper check applies to the real one


def _start_steps(cap: Capture, driver, steps: list, filename: str) -> dict:
    """Run the steps in a thread; result["done"] when finished, result["error"] on failure."""
    from .steps import run_steps

    result: dict = {}

    def run() -> None:
        try:
            with open(cap.path(STEPS_LOG), "w", encoding="utf-8") as log, \
                    open(cap.path(STEPS_DATA), "w", encoding="utf-8") as data:
                run_steps(steps, driver, filename, log, data)
        except DoctorError as e:
            result["error"] = e
        except Exception as e:  # noqa: BLE001 - the recording must still end cleanly
            result["error"] = DoctorError(f"step runner failed: {e!r}")
        finally:
            result["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return result


def record_atl(opts: RecordOptions) -> int:
    from .sources.dex import read_apk
    from .steps import parse_steps

    atl = opts.prefix / "bin/android-translation-layer"
    if not atl.is_file():
        raise PrerequisiteMissing(f"{atl} not found (pass the ATL installation prefix with --prefix)")
    steps = None
    if opts.steps:
        try:
            steps = parse_steps(opts.steps.read_text(encoding="utf-8"), str(opts.steps))
        except OSError as e:
            raise DoctorError(f"cannot read {opts.steps}: {e.strerror}") from e
        if not opts.headless:
            _warn("--steps implies --headless")
            opts.headless = True
        if make_atl_driver is _make_atl_driver and find_helper(INPUT_HELPER, None, opts.prefix) is None:
            raise PrerequisiteMissing("--steps needs the input helper atl-doctor-input (build the C helpers)")
    info = read_apk(opts.apk)
    opts.apk = opts.apk.resolve()
    out = (opts.out or default_out_dir(datetime.now())).resolve()
    cap = Capture.create(out, "atl")
    _warn(f"recording into {out}")

    plugin = jvmti_plugin(opts.prefix)
    agent = find_helper(AGENT_LIB, opts.agent, opts.prefix)
    probes = probe_all(opts.prefix, opts.atl_src, False, None)
    for i, p in enumerate(probes):
        if p.name == "agent" and (agent is not None or opts.agent is not None):  # --agent / ATL_DOCTOR_AGENT
            probes[i] = p = Probe("agent", True) if agent else Probe("agent", False, f"{opts.agent} not found", p.hint)
        if p.name in RECORD_PROBES and not p.available:
            _warn(f"{p.name} unavailable: {p.reason}")
    counter = find_helper(TRACECOUNT, None, opts.prefix)

    trace_dir = cap.path(TRACE_DIR)
    trace_dir.mkdir()
    watch = trace_dir / WATCH
    entries = []
    if opts.atl_src:
        from .env import atl_jars
        from .sources.stubs import build_stub_list, save_stub_tsv

        entries = build_stub_list(opts.atl_src, atl_jars(opts.prefix))
        save_stub_tsv(entries, trace_dir / STUBS_TSV)
    write_watch_file(entries, watch)

    if opts.headless and not any(a in ("-w", "--width") for a in opts.atl_args):
        opts.atl_args = ["-w", str(APP_WIDTH), "-h", str(APP_HEIGHT), *opts.atl_args]
    # without the counter nobody would consume the streamed trace: no trace then (it would grow without bound)
    wheel = find_helper(WHEEL_SHIM, None, opts.prefix) if opts.headless else None
    cmd, extra_env = build_command(opts, cap, plugin, agent, trace=counter is not None, wheel=wheel)
    trees = "ATL_VIEWDUMP" in extra_env
    if trees:
        cap.path(TREES).mkdir()
    if not opts.headless:  # screenshots come from spectacle inside the headless session
        probes = [Probe("spectacle", False, "screenshots need --headless", p.hint) if p.name == "spectacle" else p
                  for p in probes]
    m = cap.read_manifest()
    m.apk, m.package, m.version_name = str(opts.apk), info.package, info.version_name
    m.command = cmd
    m.probes = [asdict(p) for p in probes]
    m.environment = {"prefix": str(opts.prefix), **({"atl_src": str(opts.atl_src)} if opts.atl_src else {})}
    cap.write_manifest(m)

    session = driver = None
    steps_state: dict = {}
    if opts.headless:
        session = HeadlessSession(cap.path(RUN_DIR))
        try:
            session.start()
        except DoctorError:
            m.ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
            m.environment["stopped_by"] = "headless session failed"
            cap.write_manifest(m)
            raise
    os.mkfifo(cap.path(CONTROL))
    ctl = os.open(cap.path(CONTROL), os.O_RDONLY | os.O_NONBLOCK)
    keep = os.open(cap.path(CONTROL), os.O_WRONLY)  # own writer: no EOF while no `mark` is connected
    count_proc = None
    if counter:
        with open(trace_dir / "tracecount.log", "wb") as clog:
            count_proc = subprocess.Popen(
                [str(counter), str(trace_dir / TRACE_FILE), str(cap.path(MARKS)), str(trace_dir / TRACE_PREFIX),
                 str(watch), (info.package or "") + "."], stdout=subprocess.DEVNULL, stderr=clog,
                start_new_session=True)  # Ctrl-C hits the terminal's group; the counter is stopped via tr.stop
    stop = _Stop()
    try:
        with stop, open(cap.path(ATL_LOG), "wb", buffering=0) as logf:
            run_env = {**os.environ, **(session.env() if session else {}), **extra_env}
            proc = subprocess.Popen(cmd, env=run_env, stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            m.environment["atl_pid"] = str(proc.pid)
            cap.write_manifest(m)
            if steps is not None:
                driver = make_atl_driver(cap, session, opts, info.package)
                steps_state = _start_steps(cap, driver, steps, str(opts.steps))
            rc = _record_loop(cap, proc, stop, ctl, trees, steps_state, session)
    finally:
        if driver is not None:
            driver.close()
        if session is not None:
            session.stop()
    os.close(keep)
    os.close(ctl)
    return _finish(cap, m, rc, stop, steps_state, count_proc, trace_dir, out)


def _record_loop(cap: Capture, proc: subprocess.Popen, stop: _Stop, ctl: int, trees: bool, steps_state: dict,
                 session: HeadlessSession | None = None) -> int:
    """Until ATL ends, SIGINT/SIGTERM or the steps are done; serves the control FIFO meanwhile."""
    started, buf = time.monotonic(), bytearray()
    pending: list[threading.Thread] = []  # tree/screenshot requests of the marks
    while proc.poll() is None and stop.by is None and not steps_state.get("done"):
        if not cap.path(ARM_FILE).exists() and time.monotonic() - started >= ARM_AFTER_S:
            cap.path(ARM_FILE).touch()
        try:
            ready, _, _ = select.select([ctl], [], [], 0.2)
        except InterruptedError:
            continue
        if ready:
            try:
                for mk in _handle_control(cap, os.read(ctl, 4096), buf):
                    _memory(cap, proc.pid, mk.index)
                    if trees:
                        pending.append(_request_tree(cap, mk, session))
            except BlockingIOError:
                pass
    for mk in drain_control(cap, ctl, buf):  # a mark sent by the last step
        _memory(cap, proc.pid, mk.index)
        if trees:
            pending.append(_request_tree(cap, mk, session))
    for t in pending:  # ATL must answer the tree (and pose for the screenshot) before it is ended
        if proc.poll() is None:
            t.join(TREE_WAIT_S)
    if proc.poll() is None:
        _memory(cap, proc.pid, "end")
    if stop.by is not None:
        _warn(f"{stop.by}: stopping ATL")
    if stop.by is not None or (steps_state.get("done") and proc.poll() is None):
        _stop_group(proc)
    return proc.wait()


def _finish(cap: Capture, m, rc: int, stop: _Stop, steps_state: dict, count_proc, trace_dir: Path, out: Path) -> int:
    if count_proc:
        (trace_dir / (TRACE_PREFIX + ".stop")).touch()
        try:
            count_proc.wait(120)
            (trace_dir / TRACE_FILE).unlink(missing_ok=True)
        except subprocess.TimeoutExpired:
            count_proc.kill()
            count_proc.wait()
            _warn("tracecount did not finish in time; trace counts may be incomplete")
        counts = trace_dir / (TRACE_PREFIX + ".counts")
        if count_proc.returncode != 0 or not counts.is_file():
            why = f"exit {count_proc.returncode}" if count_proc.returncode else "no counts written"
            m.environment["tracecount"] = f"failed ({why}, see trace/tracecount.log): stub hits missing"
            _warn(f"tracecount failed ({why}); the report has no stub hits, see {trace_dir / 'tracecount.log'}")

    m.ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if stop.by is not None:
        m.environment["stopped_by"] = stop.by  # ended on request: not a crash
    elif steps_state.get("done") and rc in (0, -signal.SIGTERM, -signal.SIGKILL):
        m.environment["stopped_by"] = "steps"  # ATL was ended after the last step
    elif rc < 0:
        m.signal = _signal_name(rc)
    else:
        m.exit_status = rc
    cap.write_manifest(m)
    _warn(f"done: {out}")
    if steps_state.get("error"):
        raise steps_state["error"]
    return 0


# ------------------------------------------------------------------ mark
def send_mark(cap_dir: Path, text: str | None, timeout_s: float = 5.0) -> int:
    """Write "mark TEXT" into the control FIFO of a running recording."""
    path = Path(cap_dir) / CONTROL
    line = ("mark " + re.sub(r"[\t\r\n]", " ", text) if text else "mark").rstrip() + "\n"
    end = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as e:
            if e.errno not in (errno.ENXIO, errno.ENOENT) or time.monotonic() >= end:
                raise DoctorError(f"no recording is reading {cap_dir}/control") from e
            time.sleep(0.1)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return 0


def _newest_capture() -> Path:
    found = [p for p in Path(".").glob("atl-doctor-*") if (p / CONTROL).exists()]
    if not found:
        raise DoctorError("no recording found in the current directory (use -d DIR)")
    return max(found, key=lambda p: p.stat().st_mtime)


# ------------------------------------------------------------------ CLI
def _run_record(args: argparse.Namespace) -> int:
    prefix = resolve(args.prefix, "ATL_DOCTOR_PREFIX")
    if args.android:
        from .record_android import record_android

        return record_android(RecordOptions(apk=Path(args.apk), out=Path(args.output) if args.output else None,
                                            prefix=Path(prefix) if prefix else None, atl_src=None, agent=None,
                                            steps=Path(args.steps) if args.steps else None, android=True))
    if not prefix:
        raise PrerequisiteMissing("no ATL prefix (pass --prefix DIR or set ATL_DOCTOR_PREFIX)")
    atl_src = resolve(args.atl_src, "ATL_DOCTOR_ATL_SRC")
    opts = RecordOptions(apk=Path(args.apk), out=Path(args.output) if args.output else None, prefix=Path(prefix),
                         atl_src=Path(atl_src) if atl_src else None, agent=Path(args.agent) if args.agent else None,
                         headless=args.headless, steps=Path(args.steps) if args.steps else None, heap=args.heap,
                         atl_args=list(args.atl_args))
    return record_atl(opts)


def _run_mark(args: argparse.Namespace) -> int:
    return send_mark(Path(args.dir) if args.dir else _newest_capture(), args.text)


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import common_parser

    p = sub.add_parser("record", help="record an app under ATL until it ends or SIGINT", parents=[common_parser()])
    p.add_argument("-o", "--output", metavar="DIR", help="capture directory (default: ./atl-doctor-YYYYmmdd-HHMMSS)")
    p.add_argument("--prefix", metavar="DIR", help="ATL installation prefix (default: $ATL_DOCTOR_PREFIX)")
    p.add_argument("--atl-src", metavar="DIR", help="ATL source tree for the stub list (default: $ATL_DOCTOR_ATL_SRC)")
    p.add_argument("--agent", metavar="PATH", help="JVMTI agent library (default: $ATL_DOCTOR_AGENT, then the helper dirs)")
    p.add_argument("--headless", action="store_true",
                   help="run ATL in a private invisible KWin session (kwin_wayland --virtual)")
    p.add_argument("--steps", metavar="FILE", help="drive the app with a step file; implies --headless under ATL")
    p.add_argument("--heap", action="store_true",
                   help="count instances and bytes per class at every mark (agent; slow, forces a GC per mark)")
    p.add_argument("--android", action="store_true",
                   help="record the same app on an Android device over adb (own capture directory)")
    p.add_argument("apk", metavar="APK")
    p.add_argument("atl_args", nargs="*", metavar="ATL-ARGS", help="after --: passed to android-translation-layer")
    p.set_defaults(func=_run_record)

    p = sub.add_parser("mark", help="start a new phase in the running recording", parents=[common_parser()])
    p.add_argument("-d", "--dir", metavar="DIR", help="capture directory (default: the newest ./atl-doctor-*)")
    p.add_argument("text", nargs="?", metavar="TEXT", help="phase name (default: 'mark N')")
    p.set_defaults(func=_run_mark)
