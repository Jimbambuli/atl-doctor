# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from atl_doctor.cli import main
from atl_doctor.findings import Klass, rank
from atl_doctor.sources.dex import read_apk
from atl_doctor.sources.scan import load_state, scan_apk, scan_many
from dexgen import cls, make_apk, make_dex, make_jar

SRC = Path(__file__).parent / "data" / "atl-src"
REFS = {
    "View.setAlpha": "Landroid/view/View;->setAlpha(F)V",
    "Drawable.parseBlendMode":
        "Landroid/graphics/drawable/Drawable;->parseBlendMode(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;",
    "ThumbnailUtils.createImageThumbnail": "Landroid/media/ThumbnailUtils;->createImageThumbnail(Ljava/io/File;)Landroid/graphics/Bitmap;",
}


def fw_dex():
    return make_dex([
        cls("Landroid/view/View;", members=["setAlpha(F)V"]),
        cls("Landroid/graphics/drawable/Drawable;", members=["parseBlendMode(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;"]),
        cls("Landroid/graphics/BlendMode;"),
        cls("Landroid/media/ThumbnailUtils;", members=["realMethod(I)I"]),
    ])


@pytest.fixture
def prefix(tmp_path):
    p = tmp_path / "prefix"
    (p / "lib64/java/dex/android_translation_layer").mkdir(parents=True)
    (p / "lib64/java/dex/art").mkdir(parents=True)
    make_jar(p / "lib64/java/dex/android_translation_layer/api-impl.jar", [fw_dex()])
    make_jar(p / "lib64/java/dex/art/core-hostdex.jar", [make_dex([cls("Ljava/lang/Object;"), cls("Ljava/lang/String;")])])
    return p


@pytest.fixture
def state(prefix):
    from atl_doctor.env import atl_jars, libcore_jars
    return load_state("main", SRC, atl_jars(prefix), libcore_jars(prefix))


def apk(tmp_path, name, calls, attrs=None, extra_classes=(), extra_files=None):
    app = cls("Lorg/example/Main;", calls=[("invoke-virtual", REFS.get(c, c)) for c in calls])
    return make_apk(tmp_path / name, [make_dex([app, *extra_classes])],
                    attrs or {"package": "org.example." + name.split(".")[0], "minSdkVersion": "21", "targetSdkVersion": "36"},
                    extra_files)


def test_coverage_counts(state, tmp_path):
    p = apk(tmp_path, "a.apk", ["View.setAlpha", "Drawable.parseBlendMode", "ThumbnailUtils.createImageThumbnail"])
    cov, fs = scan_apk(read_apk(p), state, None)
    assert (cov.implemented, cov.stub, cov.missing_member) == (1, 1, 1)
    assert cov.coverage_pct == pytest.approx(33.3, abs=0.1) and cov.coverage_incl_stub_pct == pytest.approx(66.7, abs=0.1)
    assert {f.id for f in fs} >= {"missing:android.media.ThumbnailUtils#createImageThumbnail",
                                  "stub:android.graphics.drawable.Drawable#parseBlendMode"}
    assert all(f.occurrences[0].side == "static" and f.occurrences[0].where == "org.example.a" for f in fs)


def test_result_used_makes_stub_rank_higher(state, tmp_path):
    app = cls("Lorg/example/Main;", calls=[("invoke-static", REFS["Drawable.parseBlendMode"]), ("move-result-object", 0)])
    p = make_apk(tmp_path / "r.apk", [make_dex([app])], {"package": "org.example.r"})
    _, fs = scan_apk(read_apk(p), state, None)
    (stub,) = [f for f in fs if f.kind == "stub"]
    assert stub.klass is Klass.STUB_RESULT_USED


def test_missing_class(state, tmp_path):
    p = apk(tmp_path, "m.apk", ["Landroid/nope/Gone;->x()V"])
    cov, fs = scan_apk(read_apk(p), state, None)
    assert cov.missing_class == 1 and "missing:android.nope.Gone" in {f.id for f in fs}


def test_sdk_int_and_target(state, tmp_path):
    p = apk(tmp_path, "s.apk", ["View.setAlpha"], {"package": "p", "minSdkVersion": "21", "targetSdkVersion": "36"})
    cov, _ = scan_apk(read_apk(p), state, None)
    assert cov.sdk_int == 21 and cov.target_above_sdk_int
    p = apk(tmp_path, "t.apk", ["View.setAlpha"], {"package": "p", "minSdkVersion": "21", "targetSdkVersion": "34"})
    cov, _ = scan_apk(read_apk(p), state, 34)
    assert cov.sdk_int == 34 and not cov.target_above_sdk_int
    p = apk(tmp_path, "u.apk", ["View.setAlpha"], {"package": "p", "minSdkVersion": "3", "targetSdkVersion": "10"})
    assert scan_apk(read_apk(p), state, None)[0].sdk_int == 9


def test_exclusions(state, tmp_path):
    compose = [cls(f"Landroidx/compose/ui/C{i};") for i in range(100)]
    p = apk(tmp_path, "x.apk", ["Lcom/google/android/gms/maps/Map;->x()V"], extra_classes=compose,
            extra_files={"lib/arm64-v8a/libx.so": b""})
    cov, _ = scan_apk(read_apk(p), state, None)
    assert cov.exclusions == ["Google Play Services/Firebase", "Jetpack Compose", "native libraries only for ARM"]
    assert cov.gms_classes == 1 and cov.compose_classes == 100


def test_corpus_ranking_and_histogram(state, tmp_path):
    t = "ThumbnailUtils.createImageThumbnail"
    a = apk(tmp_path, "a.apk", [t, "View.setAlpha"])
    b = apk(tmp_path, "b.apk", [t, t])      # two refs in one app still count as one app
    c = apk(tmp_path, "c.apk", ["View.setAlpha"], {"package": "c", "minSdkVersion": "21", "targetSdkVersion": "34"})
    r = scan_many([a, b, c], [state], None)
    top = rank(r.findings)[0]
    assert top.id == "missing:android.media.ThumbnailUtils#createImageThumbnail" and top.total_count == 2
    assert r.extras["target_sdk_histogram"] == {"34": 1, "36": 2}
    assert len(r.extras["apps"]["main"]) == 3


def test_bad_apk_is_skipped_and_exit_1(prefix, tmp_path, capsys):
    good = apk(tmp_path, "good.apk", ["View.setAlpha"])
    (tmp_path / "broken.apk").write_bytes(b"nope")
    nodex = make_apk(tmp_path / "nodex.apk", [])
    rc = main(["scan", "--atl-src", str(SRC), "--prefix", str(prefix), str(good), str(tmp_path / "broken.apk"), str(nodex)])
    out = capsys.readouterr().out
    assert rc == 1 and "skipped: broken.apk" in out and "skipped: nodex.apk" in out and "org.example.good" in out
    assert "Reflection is only detected through string constants." in out


def test_cli_text_and_missing_prerequisites(prefix, tmp_path, capsys, monkeypatch):
    good = apk(tmp_path, "good.apk", ["View.setAlpha", "ThumbnailUtils.createImageThumbnail"])
    assert main(["scan", "--atl-src", str(SRC), "--prefix", str(prefix), str(good)]) == 0
    out = capsys.readouterr().out
    assert "Coverage" in out and "50.0%" in out and "target SDK: 36: 1" in out
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    monkeypatch.delenv("ATL_DOCTOR_ATL_SRC", raising=False)
    assert main(["scan", str(good)]) == 3
    assert main(["scan", "--prefix", str(prefix), "--atl-src", str(SRC), "--compare-src", str(tmp_path), str(good)]) == 3
    assert "build the compare tree first" in capsys.readouterr().err


def test_compare_src_second_state(prefix, tmp_path, capsys):
    cmp_src = tmp_path / "cmp"
    (cmp_src / "build").mkdir(parents=True)
    make_jar(cmp_src / "build/api-impl.jar", [make_dex([cls("Landroid/view/View;", members=["setAlpha(F)V"])])])
    (cmp_src / "src/api-impl").mkdir(parents=True)
    good = apk(tmp_path, "good.apk", ["View.setAlpha", "ThumbnailUtils.createImageThumbnail"])
    from atl_doctor.env import atl_jars, libcore_jars
    states = [load_state("main", SRC, atl_jars(prefix), libcore_jars(prefix)),
              load_state("compare", cmp_src, [cmp_src / "build/api-impl.jar"], libcore_jars(prefix))]
    r = scan_many([good], states, None)
    assert set(r.extras["apps"]) == {"main", "compare"}
    assert r.extras["apps"]["main"][0]["missing_member"] == 1 and r.extras["apps"]["compare"][0]["missing_class"] == 1
    rc = main(["scan", "--atl-src", str(SRC), "--prefix", str(prefix), "--compare-src", str(cmp_src), str(good)])
    assert rc == 0 and "Coverage (compare)" in capsys.readouterr().out
