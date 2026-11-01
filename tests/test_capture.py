# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from atl_doctor.capture import Capture, Mark, clean_mark_text, now_us, phase_of
from atl_doctor.errors import DoctorError


def test_capture_roundtrip(tmp_path):
    c = Capture.create(tmp_path / "cap", "atl")
    m = c.read_manifest()
    assert m.format == 1 and m.side == "atl" and m.started and m.ended is None
    m.package, m.exit_status, m.command = "org.example.demo", 0, ["atl", "x.apk"]
    c.write_manifest(m)
    assert not (tmp_path / "cap/manifest.json.tmp").exists()
    again = Capture.open(tmp_path / "cap").read_manifest()
    assert (again.package, again.exit_status, again.command) == ("org.example.demo", 0, ["atl", "x.apk"])
    assert [x.name for x in c.read_marks()] == ["start"] and c.path("marks.tsv").name == "marks.tsv"


def test_create_refuses_nonempty_dir(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x/f").write_text("1")
    with pytest.raises(DoctorError):
        Capture.create(tmp_path / "x", "atl")


def test_capture_newer_format_rejected(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"format": 2}))
    with pytest.raises(DoctorError) as e:
        Capture.open(d)
    assert "format 2" in str(e.value) and "update atl-doctor" in str(e.value)


def test_open_errors_and_truncated_manifest(tmp_path):
    with pytest.raises(DoctorError, match="not a capture directory"):
        Capture.open(tmp_path)
    (tmp_path / "manifest.json").write_text('{"format": 1, "package"')   # cut off by SIGKILL
    with pytest.raises(DoctorError, match="not a capture directory"):
        Capture.open(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"format": 1, "package": "p", "future_key": 1}))
    assert Capture.open(tmp_path).read_manifest().ended is None      # no `ended`, unknown key ignored


def test_marks_ignore_partial_line(tmp_path):
    c = Capture.create(tmp_path / "c", "atl")
    c.append_mark("one")
    with open(c.path("marks.tsv"), "a") as f:
        f.write("2\t123\t456\thal")
    assert [m.name for m in c.read_marks()] == ["start", "one"]


def test_marks_indexes_and_clean_text(tmp_path):
    c = Capture.create(tmp_path / "c", "atl")
    a, b = c.append_mark("a\tb\nc"), c.append_mark(None)
    assert (a.index, a.name, b.index, b.name) == (1, "a b c", 2, "mark 2")
    assert [m.index for m in c.read_marks()] == [0, 1, 2]


def test_clean_mark_text():
    assert clean_mark_text("a\tb\nc", 1) == "a b c"
    assert clean_mark_text(None, 3) == "mark 3"
    assert clean_mark_text(" \t\n ", 4) == "mark 4"


def test_phase_of():
    marks = [Mark(0, 100, 1000, "start"), Mark(1, 200, 2000, "one"), Mark(2, 300, 3000, "two")]
    assert phase_of(250, marks).name == "one"
    assert phase_of(300, marks).name == "two"
    assert phase_of(50, marks).name == "start"
    assert phase_of(2500, marks, clock="wall").name == "one"
    boot, wall = now_us()
    assert boot > 0 and wall > 1_700_000_000_000_000
