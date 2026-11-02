# SPDX-License-Identifier: GPL-3.0-or-later
"""AOSP source is fetched on demand (sparse, shallow) into the cache; no network in the tests: a local git mirror."""
import shutil
import subprocess
from pathlib import Path

import pytest

from atl_doctor.sources import aosp_fetch as af

DATA = Path(__file__).parent / "data"


def _git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def mirror(tmp_path):
    """A frameworks/base mirror with tags android-14.0.0_r1 and _r10, a java and a non-java directory."""
    src = tmp_path / "src"
    shutil.copytree(DATA / "like-aosp/frameworks/base", src)
    (src / "tests").mkdir()
    (src / "tests/Skip.java").write_text("class Skip {}\n")
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "x")
    for t in ("android-14.0.0_r1", "android-14.0.0_r10", "android-13.0.0_r3", "android-15.0.0_r2"):
        _git(src, "tag", t)
    _git(src, "config", "uploadpack.allowFilter", "true")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    return src


def test_tag_table_and_fallback():
    assert af.tag_series(34) == "android-14.0.0_r"
    assert af.tag_series(33) == "android-13.0.0_r"
    assert af.tag_series(36) == "android-16.0.0_r"
    assert af.tag_series(35) == "android-15.0.0_r"
    assert af.tag_series(99) == "android-16.0.0_r"          # newer than the table: the newest known
    assert af.tag_series(20) is None                        # older than the table
    assert af.tag_series(28) is None


def test_latest_tag_is_numeric(mirror):
    assert af.latest_tag(str(mirror), 34) == "android-14.0.0_r10"


def test_fetch_creates_cache_and_reuses_it(mirror, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(mirror))
    r = af.ensure_aosp(34)
    assert r.path == tmp_path / "cache/atl-doctor/aosp/android-14.0.0_r10"
    assert (r.path / "frameworks/base/core/java/android/view/View.java").is_file()
    assert not (r.path / "frameworks/base/tests/Skip.java").exists()
    assert r.tag == "android-14.0.0_r10" and r.fetched
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(tmp_path / "gone"))   # offline now
    again = af.ensure_aosp(34)
    assert again.path == r.path and not again.fetched


def test_fallback_to_next_lower_series(mirror, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(mirror))
    assert af.ensure_aosp(36).tag == "android-15.0.0_r2"   # the mirror has no 16: next lower series


def test_no_network_no_cache_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(tmp_path / "gone"))
    with pytest.raises(af.FetchError) as e:
        af.ensure_aosp(34)
    assert "gone" in str(e.value) or "android-14" in str(e.value)
    assert not (tmp_path / "cache/atl-doctor/aosp").exists() or not any((tmp_path / "cache/atl-doctor/aosp").iterdir())


def test_apply_aosp_fetches_and_marks(mirror, tmp_path, monkeypatch):
    from atl_doctor.report import Report, apply_aosp
    from test_like_aosp import _entries, _findings

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(mirror))
    from atl_doctor.sources.stubs import stub_index
    fs = _findings("isInEditMode", "isShown")
    r = Report("record", {}, [], ["p"], fs)
    apply_aosp(r, None, None, stub_index(_entries()))
    assert [f.details.get("like_aosp", False) for f in fs] == [True, False]
    assert r.environment["aosp"].endswith("android-14.0.0_r10")


def test_apply_aosp_says_so_when_impossible(tmp_path, monkeypatch):
    from atl_doctor.report import Report, apply_aosp
    from atl_doctor.sources.stubs import stub_index
    from test_like_aosp import _entries, _findings

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(tmp_path / "gone"))
    fs = _findings("isInEditMode")
    r = Report("record", {}, [], ["p"], fs)
    apply_aosp(r, None, None, stub_index(_entries()))
    assert "AOSP comparison not possible" in r.environment["aosp"] and "unfiltered" in r.environment["aosp"]
    assert not fs[0].details.get("like_aosp")


def test_no_fetch_switch(tmp_path, monkeypatch):
    from atl_doctor.report import Report, apply_aosp
    from atl_doctor.sources.stubs import stub_index
    from test_like_aosp import _entries, _findings

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    r = Report("record", {}, [], ["p"], _findings("isInEditMode"))
    apply_aosp(r, None, None, stub_index(_entries()), no_fetch=True)
    assert "AOSP comparison not possible" in r.environment["aosp"] and "--no-aosp-fetch" in r.environment["aosp"]


def test_apply_capture_aosp_public_api(mirror, tmp_path, monkeypatch):
    """What a program that shows reports (apk-starter) calls after build_capture_report: like `report DIR`."""
    from atl_doctor.capture import TRACE_DIR, Capture
    from atl_doctor.report import Report, apply_capture_aosp, like_aosp
    from atl_doctor.sources.stubs import save_stub_tsv
    from test_like_aosp import _entries, _findings

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", str(mirror))
    cap = Capture.create(tmp_path / "rec", "atl")
    cap.path(TRACE_DIR).mkdir()
    save_stub_tsv(_entries(), cap.path(TRACE_DIR) / "stubs.tsv")
    fs = _findings("isInEditMode", "isShown")
    r = Report("record", {}, [], ["p"], fs)
    apply_capture_aosp(r, cap.root)
    assert [like_aosp(f) for f in fs] == [True, False]
    assert r.environment["aosp"].endswith("android-14.0.0_r10")
