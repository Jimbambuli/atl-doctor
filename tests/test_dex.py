# SPDX-License-Identifier: GPL-3.0-or-later
import zipfile

import pytest

from atl_doctor.errors import DoctorError
from atl_doctor.sources.dex import Ref, World, framework_refs, parse_axml, read_apk, result_used_refs
from dexgen import cls, make_apk, make_axml, make_dex, make_jar


def apk_with_calls(tmp_path, calls, extra_classes=(), attrs=None):
    app = cls("Lorg/example/Main;", calls=calls)
    return read_apk(make_apk(tmp_path / "a.apk", [make_dex([app, *extra_classes])], attrs))


def test_read_apk_manifest_and_abis(tmp_path):
    p = make_apk(tmp_path / "a.apk", [make_dex([cls("Lorg/example/A;")])],
                 {"package": "org.example.demo", "versionName": "1.2", "versionCode": "7",
                  "minSdkVersion": "21", "targetSdkVersion": "36"},
                 {"lib/arm64-v8a/libx.so": b"", "lib/x86_64/libx.so": b""})
    a = read_apk(p)
    assert (a.package, a.min_sdk, a.target_sdk) == ("org.example.demo", 21, 36)
    assert (a.version_name, a.version_code) == ("1.2", "7")
    assert a.abis == ["arm64-v8a", "x86_64"] and a.dex_names == ["classes.dex"]


def test_parse_axml_roundtrip():
    assert parse_axml(make_axml({"package": "p.q", "minSdkVersion": "9"})) == {"package": "p.q", "minSdkVersion": "9"}
    assert parse_axml(b"garbage") == {}


def test_framework_refs_exclude_app_declared(tmp_path):
    own = cls("Lorg/example/Own;", members=["x()V"])
    a = apk_with_calls(tmp_path, [("invoke-virtual", "Landroid/view/View;->setAlpha(F)V"),
                                  ("invoke-virtual", "Lorg/example/Own;->x()V")], [own])
    refs = framework_refs(a)
    assert Ref("M", "android.view.View", "setAlpha(F)V") in refs
    assert not any(r.cls == "org.example.Own" for r in refs)


def test_framework_ref_through_app_superclass_and_field_and_class(tmp_path):
    base = cls("Lorg/example/MyView;", super_="Landroid/view/View;")
    a = apk_with_calls(tmp_path, [("invoke-virtual", "Lorg/example/MyView;->setAlpha(F)V"),
                                  ("sget", "Landroid/os/Build;->MODEL:Ljava/lang/String;"),
                                  ("new-instance", "Landroid/widget/Toast;")], [base])
    refs = framework_refs(a)
    assert Ref("M", "android.view.View", "setAlpha(F)V") in refs
    assert Ref("F", "android.os.Build", "MODEL:Ljava/lang/String;") in refs
    assert Ref("C", "android.widget.Toast", "") in refs


def test_result_used_refs(tmp_path):
    drawable = "Landroid/graphics/drawable/Drawable;->parseBlendMode(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;"
    a = apk_with_calls(tmp_path, [("invoke-static", drawable), ("move-result-object", 0),
                                  ("invoke-static", "Landroid/util/Log;->d(Ljava/lang/String;)I"), ("nop",)])
    used = result_used_refs(a)
    assert ("android.graphics.drawable.Drawable", "parseBlendMode") in used
    assert ("android.util.Log", "d") not in used


def test_payload_pseudo_instruction_is_skipped(tmp_path):
    # packed-switch payload (0x0100, size 1 -> 4 + 2 units) must not be decoded as code
    a = apk_with_calls(tmp_path, [("raw", [0x0100, 1, 0, 0, 0, 0]),
                                  ("invoke-static", "Landroid/util/Log;->d(Ljava/lang/String;)I")])
    assert Ref("M", "android.util.Log", "d(Ljava/lang/String;)I") in framework_refs(a)


def test_bad_apk_raises(tmp_path):
    (tmp_path / "x.apk").write_bytes(b"not a zip")
    with pytest.raises(DoctorError, match="not an APK"):
        read_apk(tmp_path / "x.apk")
    make_apk(tmp_path / "nodex.apk", [])
    with pytest.raises(DoctorError, match="classes.dex"):
        read_apk(tmp_path / "nodex.apk")
    make_apk(tmp_path / "noman.apk", [make_dex([cls("Lo/A;")])], manifest=False)
    with pytest.raises(DoctorError, match="AndroidManifest"):
        read_apk(tmp_path / "noman.apk")
    with zipfile.ZipFile(tmp_path / "baddex.apk", "w") as z:
        z.writestr("AndroidManifest.xml", make_axml({"package": "p"}))
        z.writestr("classes.dex", b"junk")
    with pytest.raises(DoctorError):
        read_apk(tmp_path / "baddex.apk")


def test_world_resolves_inherited_member(tmp_path):
    jar = make_jar(tmp_path / "fw.jar", [make_dex([
        cls("Landroid/view/View;", members=["setAlpha(F)V", "FLAG:I"]),
        cls("Landroid/widget/TextView;", super_="Landroid/view/View;"),
        cls("Landroid/widget/Button;", super_="Landroid/widget/TextView;", interfaces=["Landroid/view/Cb;"]),
        cls("Landroid/view/Cb;", super_="Ljava/lang/Object;", members=["onClick()V"]),
    ])])
    w = World.from_jars([jar])
    assert w.has("android.widget.TextView", "setAlpha(F)V") and not w.has("android.widget.TextView", "nope()V")
    assert w.has("android.widget.Button", "onClick()V")          # via interface
    assert w.has("android.view.View", "FLAG:I")
    assert w.has_class("android.view.View") and not w.has_class("android.view.Nope")
    assert w.declaring_class("android.widget.TextView", "setAlpha(F)V") == "android.view.View"
    assert "android.widget.Button" in w.class_names()
