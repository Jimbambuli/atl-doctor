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

from .capture import AGENT, ATL_LOG, CONTROL, MARKS, TRACE_DIR, TREES, Capture
from .env import AGENT_LIB, TRACECOUNT, Probe, find_helper, has_viewdump, jvmti_plugin, probe_all, resolve
from .errors import DoctorError, PrerequisiteMissing
from .sources.trace import write_watch_file

ARM_FILE = "armed"
ARM_AFTER_S = 5.0
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
    prefix: Path
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
                  trace: bool = True) -> tuple[list[str], dict[str, str]]:
    """ATL command line and the environment variables to add."""
    c = cap.root
    cmd = [str(opts.prefix / "bin/android-translation-layer"), str(opts.apk), *opts.atl_args]
    if trace:
        cmd += ["-X", "-Xmethod-trace", "-X", "-Xmethod-trace-stream",
                "-X", f"-Xmethod-trace-file:{c}/{TRACE_DIR}/{TRACE_FILE}"]
    if plugin and agent:
        cmd += ["-X", f"-agentpath:{agent}={c}/{AGENT},late={plugin},latesec=3,marks={c}/{MARKS},arm={c}/{ARM_FILE}"]
    env = {"LD_LIBRARY_PATH": f"{opts.prefix}/lib64:{opts.prefix}/lib64/art"}
    if has_viewdump(opts.prefix):
        env["ATL_VIEWDUMP"] = f"{c}/{TREES}"
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


def _handle_control(cap: Capture, data: bytes, buf: bytearray) -> None:
    buf += data
    while b"\n" in buf:
        line, _, rest = bytes(buf).partition(b"\n")
        buf[:] = rest
        text = line.decode("utf-8", "replace")
        if text == "mark" or text.startswith("mark "):
            cap.append_mark(text[5:] or None)


def record_atl(opts: RecordOptions) -> int:
    from .sources.dex import read_apk

    atl = opts.prefix / "bin/android-translation-layer"
    if not atl.is_file():
        raise PrerequisiteMissing(f"{atl} not found (pass the ATL installation prefix with --prefix)")
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

    # without the counter nobody would consume the streamed trace: no trace then (it would grow without bound)
    cmd, extra_env = build_command(opts, cap, plugin, agent, trace=counter is not None)
    m = cap.read_manifest()
    m.apk, m.package, m.version_name = str(opts.apk), info.package, info.version_name
    m.command = cmd
    m.probes = [asdict(p) for p in probes]
    m.environment = {"prefix": str(opts.prefix), **({"atl_src": str(opts.atl_src)} if opts.atl_src else {})}
    cap.write_manifest(m)

    os.mkfifo(cap.path(CONTROL))
    ctl = os.open(cap.path(CONTROL), os.O_RDONLY | os.O_NONBLOCK)
    keep = os.open(cap.path(CONTROL), os.O_WRONLY)  # own writer: no EOF while no `mark` is connected
    count_proc = None
    if counter:
        with open(trace_dir / "tracecount.log", "wb") as clog:
            count_proc = subprocess.Popen(
                [str(counter), str(trace_dir / TRACE_FILE), str(cap.path(MARKS)), str(trace_dir / TRACE_PREFIX),
                 str(watch), (info.package or "") + "."], stdout=subprocess.DEVNULL, stderr=clog)
    stop = _Stop()
    with stop, open(cap.path(ATL_LOG), "wb", buffering=0) as logf:
        proc = subprocess.Popen(cmd, env={**os.environ, **extra_env}, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True)
        m.environment["atl_pid"] = str(proc.pid)
        cap.write_manifest(m)
        started, buf = time.monotonic(), bytearray()
        while proc.poll() is None and stop.by is None:
            if not cap.path(ARM_FILE).exists() and time.monotonic() - started >= ARM_AFTER_S:
                cap.path(ARM_FILE).touch()
            try:
                ready, _, _ = select.select([ctl], [], [], 0.2)
            except InterruptedError:
                continue
            if ready:
                try:
                    _handle_control(cap, os.read(ctl, 4096), buf)
                except BlockingIOError:
                    pass
        if stop.by is not None:
            _warn(f"{stop.by}: stopping ATL")
            _stop_group(proc)
        rc = proc.wait()
    os.close(keep)
    os.close(ctl)

    if count_proc:
        (trace_dir / (TRACE_PREFIX + ".stop")).touch()
        try:
            count_proc.wait(120)
            (trace_dir / TRACE_FILE).unlink(missing_ok=True)
        except subprocess.TimeoutExpired:
            count_proc.kill()
            count_proc.wait()
            _warn("tracecount did not finish in time; trace counts may be incomplete")

    m.ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if stop.by is not None:
        m.environment["stopped_by"] = stop.by  # ended on request: not a crash
    elif rc < 0:
        m.signal = _signal_name(rc)
    else:
        m.exit_status = rc
    cap.write_manifest(m)
    _warn(f"done: {out}")
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
    if not prefix:
        raise PrerequisiteMissing("no ATL prefix (pass --prefix DIR or set ATL_DOCTOR_PREFIX)")
    atl_src = resolve(args.atl_src, "ATL_DOCTOR_ATL_SRC")
    opts = RecordOptions(apk=Path(args.apk), out=Path(args.output) if args.output else None, prefix=Path(prefix),
                         atl_src=Path(atl_src) if atl_src else None, agent=Path(args.agent) if args.agent else None,
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
    p.add_argument("apk", metavar="APK")
    p.add_argument("atl_args", nargs="*", metavar="ATL-ARGS", help="after --: passed to android-translation-layer")
    p.set_defaults(func=_run_record)

    p = sub.add_parser("mark", help="start a new phase in the running recording", parents=[common_parser()])
    p.add_argument("-d", "--dir", metavar="DIR", help="capture directory (default: the newest ./atl-doctor-*)")
    p.add_argument("text", nargs="?", metavar="TEXT", help="phase name (default: 'mark N')")
    p.set_defaults(func=_run_mark)
