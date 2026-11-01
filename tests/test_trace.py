# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.capture import Capture
from atl_doctor.findings import Klass, lookup
from atl_doctor.sources.stubs import StubEntry
from atl_doctor.sources.trace import read_counter_output, stub_findings, write_watch_file

DATA = Path(__file__).parent / "data"
SIG = "(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;"
ENTRIES = [StubEntry("const", "android.graphics.drawable.Drawable", "parseBlendMode", SIG,
                     "src/api-impl/android/graphics/drawable/Drawable.java:6", "const", "")]


def test_stub_hits_by_phase():
    cap = Capture.open(DATA / "capture-atl")
    fs = stub_findings(read_counter_output(cap.path("trace") / "tr"), ENTRIES, cap.read_marks(),
                       {("android.graphics.drawable.Drawable", "parseBlendMode")})
    f = lookup(fs, "stub:android.graphics.drawable.Drawable#parseBlendMode")
    assert f.klass is Klass.STUB_RESULT_USED
    assert [(o.phase, o.count) for o in f.occurrences] == [("start", 1), ("Import", 4)]
    assert f.details["callers"][0] == "org.example.demo.ImportTask.run"
    assert f.details["callers"] == ["org.example.demo.ImportTask.run", "com.lib.Other.doIt"]


def test_stub_hit_without_result_use_is_other():
    cap = Capture.open(DATA / "capture-atl")
    fs = stub_findings(read_counter_output(cap.path("trace") / "tr"), ENTRIES, cap.read_marks(), set())
    assert fs[0].klass is Klass.OTHER


def test_read_counter_output_missing_files_is_empty(tmp_path):
    assert read_counter_output(tmp_path / "tr") == []


def test_write_watch_file_sorted_unique(tmp_path):
    e2 = StubEntry("empty", "a.B", "c", "()V", "x:1", "", "")
    write_watch_file(ENTRIES + [e2, e2], tmp_path / "w")
    assert (tmp_path / "w").read_text() == f"a.B\tc\t()V\nandroid.graphics.drawable.Drawable\tparseBlendMode\t{SIG}\n"
