# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from atl_doctor.capture import Capture
from atl_doctor.findings import Klass
from atl_doctor.sources.exceptions import parse_agent
from atl_doctor.sources.memory import (growth, memory_findings, parse_meminfo, read_memory, smaps_snapshot,
                                       write_memory_rows)

MEMINFO = """Applications Memory Usage (in Kilobytes):
Uptime: 1000 Realtime: 1000

** MEMINFO in pid 1234 [org.example.demo] **
                   Pss  Private  Private  SwapPss      Rss     Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free
 Native Heap     2936     2900        0       20     4044     8192     5000     3000

 App Summary
                       Pss(KB)                        Rss(KB)
                        ------                         ------
           Java Heap:     5180                          16916
         Native Heap:     2936                           4044
                Code:     4256                          29552

           TOTAL PSS:    20533            TOTAL RSS:    57044       TOTAL SWAP PSS:       47
"""

SMAPS_ROLLUP = """55d0c0000000-7ffd00000000 ---p 00000000 00:00 0                          [rollup]
Rss:              204800 kB
Pss:              102400 kB
Anonymous:         51200 kB
"""
SMAPS = """55d0c0000000-55d0c1000000 rw-p 00000000 00:00 0                          [heap]
Size:              16384 kB
Rss:               10240 kB
Pss:               10240 kB
7f0000000000-7f0000100000 r--p 00000000 08:01 1234                       /usr/lib/libc.so.6
Size:               1024 kB
Pss:                 512 kB
"""


def _capture(tmp_path, names):
    cap = Capture.create(tmp_path / "c", "atl")
    for n in names:
        cap.append_mark(n)
    return cap


def test_growth_linear():
    assert growth([100, 105, 110, 115]) == pytest.approx(15.0)


def test_growth_flat_noise():
    assert abs(growth([100, 101, 99, 100])) < 1


def test_memory_finding_repeated_phase(tmp_path):
    cap = _capture(tmp_path, ["Open"] * 10)
    write_memory_rows(cap, [(i, "atl", "pss", 100_000 + 2048 * (i - 1)) for i in range(1, 11)]
                      + [(i, "atl", "anon", 50_000) for i in range(1, 11)])
    fs = {f.id: f for f in memory_findings(cap, [])}
    f = fs["mem:Open:pss"]
    assert f.klass is Klass.MEMORY_GROWTH
    assert f.title == "pss grows by 18.0 MB over 10 repetitions of phase Open"
    assert "mem:Open:anon" not in fs
    assert f.details["unmeasured"] == ["jni_weak_globals", "gtk_gsk_objects"] and f.details["unmeasured_reason"]


def test_no_finding_below_repeats(tmp_path):
    cap = _capture(tmp_path, ["Open", "Open"])
    write_memory_rows(cap, [(1, "atl", "pss", 100_000), (2, "atl", "pss", 200_000)])
    assert memory_findings(cap, []) == []


def test_java_heap_from_agent_lines(tmp_path):
    cap = _capture(tmp_path, ["Open"] * 4)
    (tmp_path / "c/agent.tsv").write_text("".join(
        f"M\t{i}\t{i}\t{(20 + 3 * i) << 20}\t{64 << 20}\t{100 + i}\n" for i in range(5)))
    fs = {f.id: f for f in memory_findings(cap, parse_agent(tmp_path / "c/agent.tsv"))}
    assert fs["mem:Open:java_heap"].title == "java_heap grows by 9.0 MB over 4 repetitions of phase Open"
    assert fs["mem:Open:java_heap"].details["jni_globals"] == [101, 102, 103, 104]


def test_top_classes_from_agent_lines(tmp_path):
    cap = _capture(tmp_path, ["Open"] * 3)
    lines = [f"M\t{i}\t{i}\t{(20 + 4 * i) << 20}\t{64 << 20}\t-1\n" for i in range(4)]
    for i in (1, 2, 3):
        lines.append(f"C\t{i}\t{i}\torg.example.demo.Leak\t{i * 1000}\t{i * 1_000_000}\n")
        lines.append(f"C\t{i}\t{i}\tjava.lang.String\t500\t40000\n")
    (tmp_path / "c/agent.tsv").write_text("".join(lines))
    f = {f.id: f for f in memory_findings(cap, parse_agent(tmp_path / "c/agent.tsv"))}["mem:Open:java_heap"]
    assert f.details["top_classes"][0] == {"class": "org.example.demo.Leak", "instances": 2000, "bytes": 2_000_000}
    assert f.details["top_classes"][1]["class"] == "java.lang.String" and f.details["top_classes"][1]["bytes"] == 0


def test_android_meminfo_parse():
    assert parse_meminfo(MEMINFO) == {"pss": 20533, "java_heap": 5180, "native_heap": 2936}
    assert parse_meminfo("TOTAL:  1000  \n Java Heap:  10  \n") == {"pss": 1000, "java_heap": 10}


def test_smaps_snapshot(tmp_path):
    (tmp_path / "smaps_rollup").write_text(SMAPS_ROLLUP)
    (tmp_path / "smaps").write_text(SMAPS)
    assert smaps_snapshot(tmp_path) == {"pss": 102400, "anon": 51200, "native_heap": 10240}


def test_read_memory_end_row(tmp_path):
    cap = _capture(tmp_path, ["Open"])
    write_memory_rows(cap, [(1, "atl", "pss", 1024), ("end", "atl", "pss", 2048)])
    assert read_memory(cap) == {"pss": {"Open": [(1, 1.0)], "end": [(-1, 2.0)]}}


def test_pair_report_adds_android_memory(tmp_path, capsys):
    import json

    from atl_doctor.cli import main

    a = Capture.create(tmp_path / "atl", "atl")
    b = Capture.create(tmp_path / "android", "android")
    for c in (a, b):
        for _ in range(3):
            c.append_mark("Open")
        m = c.read_manifest()
        m.ended = "2026-11-01T07:00:00+00:00"
        c.write_manifest(m)
    write_memory_rows(a, [(i, "atl", "pss", 100_000 + 4096 * i) for i in (1, 2, 3)])
    write_memory_rows(b, [(i, "android", "pss", 50_000) for i in (1, 2, 3)])
    assert main(["report", "-f", "json", str(tmp_path / "atl"), str(tmp_path / "android")]) == 0
    f = next(f for f in json.loads(capsys.readouterr().out)["findings"] if f["kind"] == "mem")
    assert f["key"] == "Open:pss" and f["details"]["android"] == {"values_mb": [48.8, 48.8, 48.8], "growth_mb": 0.0}
