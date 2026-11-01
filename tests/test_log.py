# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.findings import Klass
from atl_doctor.sources.log import is_platform_frame, parse_logs

DATA = Path(__file__).parent / "data" / "logs"


def by_id(r):
    return {f.id: f for f in r.findings}


def test_missing_method_from_nosuchmethoderror():
    r = parse_logs([DATA / "plain.log"])
    f = next(f for f in r.findings if f.id == "missing:android.media.ThumbnailUtils#createImageThumbnail")
    assert f.klass is Klass.MISSING and f.occurrences[0].phase == "plain"
    assert not any(x.id.startswith("exc:java.lang.NoSuchMethodError") for x in r.findings)


def test_missing_class_field_native():
    ids = by_id(parse_logs([DATA / "plain.log"]))
    assert ids["missing:androidx.missing.Thing"].klass is Klass.MISSING
    assert "missing:android.view.View#FLAG_X" in ids
    assert ids["missing:android.graphics.Foo#nativeBar"].details["native"] is True


def test_stub_message_and_crash_signal():
    ids = {f.id for f in parse_logs([DATA / "plain.log"]).findings}
    assert "stublog:AssetManager:AssetManager.destroy(): STUB" in ids
    assert "stublog:Bar:Method foo is unimplemented" in ids
    assert "crash:signal:SIGSEGV" in ids
    assert "crash:exit:3" in ids


def test_uncaught_exception_is_crash():
    ids = by_id(parse_logs([DATA / "plain.log"]))
    f = ids["crash:uncaught:java.lang.IllegalStateException@com.example.Foo.bar"]
    assert f.klass is Klass.CRASH


def test_plain_exception_normalized_frame():
    ids = by_id(parse_logs([DATA / "plain.log"]))
    f = ids["exc:java.lang.ArrayIndexOutOfBoundsException@com.example.Lists.get$$ExternalSyntheticN.run"]
    assert f.klass is Klass.OTHER


def test_apk_starter_header():
    r = parse_logs([DATA / "apk-starter.log"])
    assert r.header["package"] == "org.example.demo" and r.header["atl_commit"] == "abc123"
    assert "crash:exit:0" in by_id(r) and by_id(r)["crash:exit:0"].klass is Klass.OTHER


def test_messy_log_does_not_break():
    r = parse_logs([DATA / "messy.log"])
    f = [f for f in r.findings if f.kind == "stublog"]
    assert len(f) == 1 and f[0].total_count == 2


def test_multiple_logs_are_phases():
    r = parse_logs([DATA / "plain.log", DATA / "apk-starter.log"])
    assert r.phases == ["plain", "apk-starter"]


def test_platform_frames():
    assert is_platform_frame("android.app.Activity.onCreate")
    assert not is_platform_frame("androidx.core.Foo.bar")
    assert not is_platform_frame("com.example.Foo")


def test_apk_starter_signal_trailer_is_one_crash():
    r = parse_logs([DATA / "apk-starter-signal.log"])
    assert r.header["signal"] == "SIGSEGV" and r.header["exit"] == "139" and r.header["duration"] == "3.4"
    ids = {f.id for f in r.findings}
    assert "crash:signal:SIGSEGV" in ids and "crash:exit:139" not in ids
