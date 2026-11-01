# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.findings import Klass, Occurrence
from atl_doctor.sources.log import parse_logs
from atl_doctor.sources.stubs import (build_stub_list, load_stub_tsv, match_log_stubs, save_stub_tsv, stub_finding,
                                      stub_index)
from dexgen import make_dex, make_jar

DATA = Path(__file__).parent / "data"


def entries():
    return build_stub_list(DATA / "atl-src", [])


def test_stub_kinds_from_source():
    e = {(x.cls, x.method): x for x in entries()}
    assert e[("android.graphics.drawable.Drawable", "parseBlendMode")].kind == "empty0"
    assert e[("android.content.res.AssetManager", "destroy")].kind == "logs"
    assert e[("android.content.res.AssetManager$Inner", "isOk")].kind == "const"
    assert ("android.media.ThumbnailUtils", "createImageThumbnail") not in e   # not in the tree: no stub
    assert ("android.media.ThumbnailUtils", "realMethod") not in e
    assert ("android.content.res.AssetManager", "<init>") not in e             # empty constructor does not count
    assert e[("android.graphics.Foo", "nativeBar")].kind == "native-missing"
    assert e[("android.graphics.Foo", "nativeBaz")].kind == "native-empty0"
    assert ("android.graphics.Foo", "nativeReal") not in e
    assert e[("android.graphics.drawable.Drawable", "parseBlendMode")].location == \
        "src/api-impl/android/graphics/drawable/Drawable.java:6"


def test_comments_and_strings_do_not_confuse_scanner():
    e = {(x.cls, x.method): x for x in entries()}
    assert ("android.graphics.drawable.Drawable", "tricky") not in e       # "}" in string, /* { */ in comment
    assert e[("android.graphics.drawable.Drawable", "doNothing")].kind == "empty"


def test_returning_a_parameter_unchanged_is_a_stub():
    # placeholder like ATL's old Drawable.parseBlendMode: `return defaultMode;` ignores the value to parse
    e = {(x.cls, x.method): x for x in entries()}
    assert e[("android.graphics.drawable.Drawable", "pickMode")].kind == "returnsarg"
    assert ("android.graphics.drawable.Drawable", "getAlpha") not in e     # returning a field is real code
    assert "TODO" in e[("android.graphics.drawable.Drawable", "doNothing")].comment
    assert ("android.graphics.drawable.Drawable", "setAlpha") not in e


def test_jar_gives_signature_and_bytecode_kind(tmp_path):
    jar = make_jar(tmp_path / "api-impl.jar", [make_dex([{
            "name": "Landroid/graphics/drawable/Drawable;", "super": "Ljava/lang/Object;", "interfaces": [], "fields": [],
            "methods": [{"name": "parseBlendMode", "proto": "(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;",
                         "code": [("raw", [0x0012, 0x0011])], "exact": True}]}])])
    e = {(x.cls, x.method): x for x in build_stub_list(DATA / "atl-src", [jar])}
    x = e[("android.graphics.drawable.Drawable", "parseBlendMode")]
    assert x.sig == "(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;"
    assert x.bytecode_kind == "empty0"


def test_tsv_roundtrip(tmp_path):
    es = entries()
    save_stub_tsv(es, tmp_path / "s.tsv")
    assert load_stub_tsv(tmp_path / "s.tsv") == es
    assert ("android.content.res.AssetManager", "destroy") in stub_index(es)


def test_stub_finding_result_used_class():
    es = entries()
    occ = [Occurrence("start", 0, "atl", 3)]
    f = stub_finding("android.graphics.drawable.Drawable", "parseBlendMode", es, True, occ, ["org.example.A.b"])
    assert f.id == "stub:android.graphics.drawable.Drawable#parseBlendMode" and f.klass is Klass.STUB_RESULT_USED
    assert f.details["callers"] == ["org.example.A.b"] and f.details["kinds"] == ["empty0"]
    g = stub_finding("android.graphics.drawable.Drawable", "parseBlendMode", es, False, occ, [])
    assert g.klass is Klass.OTHER
    h = stub_finding("android.content.res.AssetManager", "destroy", es, True, occ, [])
    assert h.klass is Klass.OTHER      # logs does not return a placeholder value


def test_match_log_stubs():
    fs = parse_logs([DATA / "logs/plain.log"]).findings
    out = match_log_stubs(fs, entries())
    ids = {f.id for f in out}
    assert "stub:android.content.res.AssetManager#destroy" in ids
    assert not any(i.startswith("stublog:AssetManager") for i in ids)
    assert any(i.startswith("stublog:Bar:") for i in ids)      # not attributable: stays
