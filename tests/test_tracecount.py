# SPDX-License-Identifier: GPL-3.0-or-later
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tracegen import ENTER, EXIT, encode_records, header, make_trace, method_op, thread_op

pytestmark = pytest.mark.skipif(not shutil.which("meson"), reason="meson missing")

START = 1_000_000_000  # CLOCK_MONOTONIC us, like ART's trace header


def boot_offset() -> int:
    return (time.clock_gettime_ns(time.CLOCK_BOOTTIME) - time.clock_gettime_ns(time.CLOCK_MONOTONIC)) // 1000
STUB = (1, "android.graphics.drawable.Drawable", "parseBlendMode",
        "(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;")
APP = (2, "org.example.demo.ImportTask", "run", "()V")
LIB = (3, "com.lib.Other", "doIt", "()V")
FW = (4, "android.app.Activity", "onCreate", "(Landroid/os/Bundle;)V")
METHODS = [STUB, APP, LIB, FW]


def marks(path: Path, *mono: int) -> Path:
    """marks.tsv with CLOCK_BOOTTIME times that correspond to the given CLOCK_MONOTONIC times"""
    off = boot_offset()
    path.write_text("".join(f"{i}\t{t + off}\t{i}\t{'start' if i == 0 else f'm{i}'}\n" for i, t in enumerate(mono)))
    return path


def watch(path: Path, *lines: str) -> Path:
    path.write_text("".join(ln + "\n" for ln in lines))
    return path


def call(tid: int, outer: tuple, inner: tuple, t: int) -> list[tuple[int, int, int, int]]:
    return [(tid, outer[0], ENTER, t), (tid, inner[0], ENTER, t + 1), (tid, inner[0], EXIT, t + 2),
            (tid, outer[0], EXIT, t + 3)]


def table(path: str) -> set[str]:
    return set(Path(path).read_text().splitlines())


def run_counter(built, trace, mk, out, wt, app="org.example.", stop=True, timeout=10):
    if stop:
        Path(str(out) + ".stop").touch()
    return subprocess.run([str(built / "atl-doctor-tracecount"), str(trace), str(mk), str(out), str(wt), app],
                          capture_output=True, text=True, timeout=timeout)


def ids(out) -> dict[str, int]:
    return {ln.split("\t")[2]: int(ln.split("\t")[0], 16) for ln in Path(str(out) + ".methods").read_text().splitlines()}


def test_tracecount_counts_per_phase_and_app_caller(built, tmp_path):
    recs = call(1, FW, STUB, 10) + call(1, APP, STUB, 1000) + call(1, APP, STUB, 1010) + call(1, LIB, STUB, 1020)
    make_trace(tmp_path / "t.bin", 3, START, METHODS, recs)
    mk = marks(tmp_path / "marks.tsv", START, START + 1000)
    wt = watch(tmp_path / "w.txt", "\t".join(STUB[1:]))
    out = tmp_path / "tr"
    r = run_counter(built, tmp_path / "t.bin", mk, out, wt)
    assert r.returncode == 0, r.stderr
    s, a, o = 1 << 2, 2 << 2, 3 << 2
    counts = table(f"{out}.counts")
    assert f"0\t0x{s:x}\t1" in counts and f"1\t0x{s:x}\t3" in counts and f"1\t0x{a:x}\t2" in counts
    callers = table(f"{out}.callers")
    assert f"1\t0x{s:x}\t0x{a:x}\ta\t2" in callers
    assert f"1\t0x{s:x}\t0x{o:x}\to\t1" in callers
    assert f"0\t0x{s:x}\t-\t-\t1" in callers  # only a platform frame above it
    assert "1\tmain" in table(f"{out}.threads")
    assert ids(out)["parseBlendMode"] == s


def test_tracecount_watch_without_sig_matches_any_overload(built, tmp_path):
    make_trace(tmp_path / "t.bin", 3, START, METHODS, call(1, APP, STUB, 10))
    out = tmp_path / "tr"
    r = run_counter(built, tmp_path / "t.bin", marks(tmp_path / "m", START),
                    out, watch(tmp_path / "w", f"{STUB[1]}\t{STUB[2]}\t"))
    assert r.returncode == 0, r.stderr
    assert f"0\t0x4\t0x8\ta\t1" in table(f"{out}.callers")


def test_tracecount_follows_growing_file_and_stops(built, tmp_path):
    trace, out = tmp_path / "t.bin", tmp_path / "tr"
    first = header(3, START) + method_op(METHODS) + thread_op(1, "main") + encode_records(3, call(1, APP, STUB, 10))
    trace.write_bytes(first[:-7])  # cut inside a record
    p = subprocess.Popen([str(built / "atl-doctor-tracecount"), str(trace), str(marks(tmp_path / "m", START)),
                          str(out), str(watch(tmp_path / "w", "\t".join(STUB[1:]))), "org.example."],
                         stderr=subprocess.PIPE, text=True)
    time.sleep(0.8)
    with open(trace, "ab") as f:
        f.write(first[-7:] + encode_records(3, call(1, APP, STUB, 20)))
    time.sleep(0.8)
    assert p.poll() is None  # still following
    t = time.monotonic()
    Path(f"{out}.stop").touch()
    p.wait(timeout=5)
    assert time.monotonic() - t < 5 and p.returncode == 0
    assert "0\t0x4\t2" in table(f"{out}.counts")


def test_tracecount_v2_single_clock(built, tmp_path):
    make_trace(tmp_path / "t.bin", 2, START, METHODS, call(1, APP, STUB, 10) + call(1, APP, STUB, 2000))
    out = tmp_path / "tr"
    r = run_counter(built, tmp_path / "t.bin", marks(tmp_path / "m", START, START + 1000), out,
                    watch(tmp_path / "w", "\t".join(STUB[1:])))
    assert r.returncode == 0, r.stderr
    assert {"0\t0x4\t1", "1\t0x4\t1"} <= table(f"{out}.counts")


def test_tracecount_streaming_versions(built, tmp_path):
    for ver in (0xF2, 0xF3):
        make_trace(tmp_path / "t.bin", ver, START, METHODS, call(1, APP, STUB, 10) + call(1, APP, STUB, 2000))
        out = tmp_path / f"tr{ver}"
        r = run_counter(built, tmp_path / "t.bin", marks(tmp_path / "m", START, START + 1000), out,
                        watch(tmp_path / "w", "\t".join(STUB[1:])))
        assert r.returncode == 0, r.stderr
        assert {"0\t0x4\t1", "1\t0x4\t1"} <= table(f"{out}.counts"), ver


def test_tracecount_phase_limit(built, tmp_path):
    make_trace(tmp_path / "t.bin", 3, START, METHODS, call(1, APP, STUB, 400))
    out = tmp_path / "tr"
    r = run_counter(built, tmp_path / "t.bin", marks(tmp_path / "m", *[START + i for i in range(300)]), out,
                    watch(tmp_path / "w", "\t".join(STUB[1:])))
    assert r.returncode == 0, r.stderr
    assert "255\t0x4\t1" in table(f"{out}.counts")
    assert r.stderr.count("phase limit 256 reached") == 1
