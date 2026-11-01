# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
from pathlib import Path

import pytest

from atl_doctor.cli import main
from atl_doctor.errors import DoctorError
from atl_doctor.findings import Finding, Klass
from atl_doctor.sources.aosp import attach_aosp, class_index, match, open_aosp
from atl_doctor.sources.dex import World, descriptor
from atl_doctor.sources.stubs import StubEntry, build_stub_list, save_stub_tsv, stub_index

DATA = Path(__file__).parent / "data"
TU = "android.media.ThumbnailUtils"
DR = "android.graphics.drawable.Drawable"


@pytest.fixture
def world():
    w = World()
    for cls, members in {
        DR: {"parseBlendMode(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;"},
        TU: {"other()I"},
        "android.graphics.BitmapFactory": {"decodeFile(Ljava/lang/String;)Landroid/graphics/Bitmap;"},
        "android.util.Size": {"<init>(II)V"},
    }.items():
        w.classes[descriptor(cls)] = ("Ljava/lang/Object;", [], members)
    return w


@pytest.fixture
def stubs():
    return stub_index([StubEntry("empty0", "android.graphics.BitmapFactory", "decodeFile", "", "x:1", "", "")])


def _m(tree, cls, method, world, stubs, sig=""):
    t = open_aosp(DATA / tree)
    return match(t, class_index(t), cls, method, world, stubs, sig)


@pytest.mark.parametrize("tree,sdk", [("aosp-14", 34), ("aosp-16", 36)])
def test_detect_sdk(tree, sdk):
    assert open_aosp(DATA / tree).sdk == sdk


def test_open_accepts_framework_base_dir():
    t = open_aosp(DATA / "aosp-16/frameworks/base")
    assert t.sdk == 36 and t.root == (DATA / "aosp-16").resolve()


def test_sdk_from_build_java_ignores_cur_development(tmp_path):
    fb = tmp_path / "fb"
    shutil.copytree(DATA / "aosp-16/frameworks/base", fb)   # no build/ next to it: Build.java decides
    assert open_aosp(fb).sdk == 36


def test_not_an_aosp_tree(tmp_path):
    with pytest.raises(DoctorError, match="not an AOSP tree"):
        open_aosp(tmp_path)


@pytest.mark.parametrize("tree", ["aosp-14", "aosp-16"])
def test_match_portable_and_lines(tree, world, stubs):
    m = _m(tree, DR, "parseBlendMode", world, stubs)
    assert m.verdict == "portable" and m.file.endswith("Drawable.java")
    assert m.size_lines == m.lines[1] - m.lines[0] + 1
    assert "@hide" in m.markers and "@UnsupportedAppUsage" in m.markers
    assert m.deps_missing == [] and m.deps_stub == []


def test_lines_follow_the_version(world, stubs):
    a = _m("aosp-14", DR, "parseBlendMode", world, stubs)
    b = _m("aosp-16", DR, "parseBlendMode", world, stubs)
    assert a.lines == (13, 19) and b.lines == (24, 31)   # annotation line up to the closing brace


def test_match_differs_between_versions(world, stubs):
    assert _m("aosp-14", TU, "createImageThumbnail", world, stubs) is None
    m = _m("aosp-16", TU, "createImageThumbnail", world, stubs)
    assert m.verdict == "needs-deps" and m.overloads == 2
    assert m.lines == (19, 25)          # without signature: the largest overload
    assert "android.media.MediaFile$Info#<init>" in m.deps_missing and "android.media.MediaFile#Info" not in m.deps_missing
    assert "android.media.MediaFile#getMimeTypeForFile" in m.deps_missing
    assert "android.media.ThumbnailUtils$Resizer#<init>" in m.deps_missing
    assert m.deps_stub == ["android.graphics.BitmapFactory#decodeFile"]
    assert not any(d.startswith("java.") for d in m.deps_missing)


def test_signature_selects_overload(world, stubs):
    m = _m("aosp-16", TU, "createImageThumbnail", world, stubs, "(Ljava/lang/String;I)Landroid/graphics/Bitmap;")
    assert m.lines == (15, 17)
    assert m.deps_missing == [] and m.verdict == "portable"   # same-name call and java.io.File are no dependencies


def test_native_or_hidden(world, stubs):
    assert _m("aosp-14", DR, "nCreate", world, stubs).verdict == "native-or-hidden"   # native itself
    m = _m("aosp-14", DR, "create", world, stubs)                                    # calls a native of the file
    assert m.verdict == "native-or-hidden" and "native" not in m.markers
    assert _m("aosp-14", DR, "service", world, stubs).verdict == "native-or-hidden"  # ServiceManager


def test_aconfig_flags_are_noted_not_dependencies(world, stubs):
    m = _m("aosp-16", DR, "tintLevel", world, stubs)
    assert m.verdict == "portable"
    assert "@hide" in m.markers and "@FlaggedApi(Flags.FLAG_NEW_TINT)" in m.markers
    assert "flag android.graphics.flags.Flags.newTint()" in m.markers


def test_without_world_dependencies_are_unchecked(stubs):
    m = _m("aosp-16", TU, "createImageThumbnail", None, stubs)
    assert m.deps_checked is False and m.deps_missing == []
    assert m.deps_stub == ["android.graphics.BitmapFactory#decodeFile"] and m.verdict == "needs-deps"


def test_attach_aosp_to_stub_and_missing(world, stubs):
    t = open_aosp(DATA / "aosp-16")
    fs = [Finding("stub", f"{DR}#parseBlendMode", Klass.OTHER, "t"),
          Finding("missing", f"{TU}#createImageThumbnail", Klass.MISSING, "t"),
          Finding("missing", "android.foo.Gone", Klass.MISSING, "t"),
          Finding("exc", "x", Klass.OTHER, "t")]
    attach_aosp(fs, t, world, stubs)
    assert fs[0].details["aosp"]["verdict"] == "portable" and fs[0].details["aosp_sdk"] == 36
    assert fs[1].details["aosp"]["verdict"] == "needs-deps"
    assert "aosp" not in fs[2].details and "aosp" not in fs[3].details   # class only: nothing to compare


def _capture_with_stubs(tmp_path):
    cap = tmp_path / "c"
    shutil.copytree(DATA / "capture-atl", cap)
    save_stub_tsv(build_stub_list(DATA / "atl-src", []), cap / "trace/stubs.tsv")
    return cap


def test_report_names_aosp_version(tmp_path, capsys):
    cap = _capture_with_stubs(tmp_path)
    assert main(["report", "-f", "json", "--aosp", str(DATA / "aosp-16"), str(cap)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["environment"]["aosp_sdk"] == "36"
    (f,) = [f for f in d["findings"] if f["id"] == f"stub:{DR}#parseBlendMode"]
    assert f["details"]["aosp"]["verdict"] == "portable" and f["details"]["aosp_sdk"] == 36
    assert main(["report", "-f", "json", str(cap)]) == 0
    assert json.loads(capsys.readouterr().out)["environment"]["aosp"] == "not given (use --aosp DIR)"


def test_report_text_and_env_variable(tmp_path, capsys, monkeypatch):
    cap = _capture_with_stubs(tmp_path)
    monkeypatch.setenv("ATL_DOCTOR_AOSP", str(DATA / "aosp-14"))
    assert main(["report", str(cap)]) == 0
    out = capsys.readouterr().out
    assert "aosp_sdk: 34" in out
    assert "AOSP 34: portable, frameworks/base/graphics/java/android/graphics/drawable/Drawable.java:13-19" in out


def test_report_bad_aosp_exit_1(tmp_path, capsys):
    assert main(["report", "--aosp", str(tmp_path), str(_capture_with_stubs(tmp_path))]) == 1
    assert "not an AOSP tree" in capsys.readouterr().err


def test_explain_shows_aosp_comparison(tmp_path, capsys):
    cap = _capture_with_stubs(tmp_path)
    assert main(["explain", "-d", str(cap), "--aosp", str(DATA / "aosp-14"), f"stub:{DR}#parseBlendMode"]) == 0
    out = capsys.readouterr().out
    assert "AOSP comparison (SDK 34):" in out and "verdict: portable" in out and "@UnsupportedAppUsage" in out


def test_scan_with_aosp(tmp_path, capsys):
    from test_scan import apk, fw_dex  # noqa: F401  (same synthetic ATL world as the scan tests)
    from dexgen import make_dex, make_jar
    p = tmp_path / "prefix"
    (p / "lib64/java/dex/android_translation_layer").mkdir(parents=True)
    (p / "lib64/java/dex/art").mkdir(parents=True)
    make_jar(p / "lib64/java/dex/android_translation_layer/api-impl.jar", [fw_dex()])
    make_jar(p / "lib64/java/dex/art/core-hostdex.jar", [make_dex([])])
    good = apk(tmp_path, "good.apk", ["Drawable.parseBlendMode", "ThumbnailUtils.createImageThumbnail"])
    assert main(["scan", "--atl-src", str(DATA / "atl-src"), "--prefix", str(p), "--aosp", str(DATA / "aosp-16"),
                 str(good)]) == 0
    out = capsys.readouterr().out
    assert "aosp_sdk: 36" in out
    assert "AOSP 36: portable, frameworks/base/graphics/java/android/graphics/drawable/Drawable.java:24-31" in out
    assert "AOSP 36: needs-deps, frameworks/base/media/java/android/media/ThumbnailUtils.java:19-25" in out


def _mini_tree(tmp_path):
    d = tmp_path / "frameworks/base/core/java/android/app"
    d.mkdir(parents=True)
    (d / "Act.java").write_text("""package android.app;
public class Act {
    public void onUserInteraction() {
        // hook for subclasses
    }
    public void onOther() { }
    public void onReal() { int x = 1; }
    public boolean dispatch() { onUserInteraction(); return true; }
    public boolean dispatch2() { onUserInteraction(); onReal(); return true; }
    public void dispatch3(Act a) { Act.helper(); }
    static void helper() { }
}
""")
    return open_aosp(tmp_path)


def test_empty_aosp_callbacks_are_no_dependency(tmp_path):
    t = _mini_tree(tmp_path)
    st = stub_index([StubEntry("e0", "android.app.Act", "onUserInteraction", "", "x:1", "", ""),
                     StubEntry("e1", "android.app.Act", "onReal", "", "x:2", "", ""),
                     StubEntry("e2", "android.app.Act", "helper", "", "x:3", "", "")])
    idx = class_index(t)
    m = match(t, idx, "android.app.Act", "dispatch", World(), st)
    assert m.verdict == "portable" and m.deps_stub == []              # onUserInteraction is empty in AOSP
    m2 = match(t, idx, "android.app.Act", "dispatch2", World(), st)
    assert m2.verdict == "needs-deps" and m2.deps_stub == ["android.app.Act#onReal"]  # a non-empty one still counts
    m3 = match(t, idx, "android.app.Act", "dispatch3", World(), st)
    assert m3.verdict == "portable"                                   # empty static helper: nothing to implement
