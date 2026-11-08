# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from atl_doctor.capture import Capture
from atl_doctor.cli import main
from atl_doctor.sources.shots import png_write
from atl_doctor.compare import MAX_LINES, build_comparison, render_markdown

APP = "org.example.demo"


def _capture(root: Path, side: str, agent: list[str], log: str = "", stubs: str = "") -> Path:
    c = Capture.create(root, side)
    m = c.read_manifest()
    m.package, m.apk, m.ended = APP, str(root / "demo.apk"), "2026-11-02T22:00:00"
    c.write_manifest(m)
    (root / "marks.tsv").write_text("")
    c.append_mark("start", 1_000_000)
    c.append_mark("Search", 2_000_000)
    (root / "agent.tsv").write_text("".join(x + "\n" for x in agent))
    (root / "atl.log").write_text(log)
    if stubs:
        (root / "stubhits.tsv").write_text(stubs)
    return root


def _exc(t, typ, msg, frames):
    return f"E\t{t}\t1\t{typ}\t{msg}\t{APP}.Main.onClick\t{frames}"


def _lc(t, method):
    return f"L\t{t}\t1\tA\t{method}\t{APP}.Main\t-\t-\t-"


STUBS = ("N\t1\tandroid.view.inputmethod.InputMethodManager\tshowSoftInput\t(Landroid/view/View;I)Z\tconst\n"
         "N\t2\tandroid.view.View\tsetAutofillHints\t([Ljava/lang/String;)V\tempty\n"
         "S\t2500000\t1\t7\nS\t2500000\t2\t2\n"
         f"C\t1\t{APP}.SearchField.focus\n")


@pytest.fixture
def pair(tmp_path):
    atl = _capture(tmp_path / "atl", "atl", [
        _exc(2_500_000, "java.lang.NullPointerException", "x is null", f"android.view.View.performClick|{APP}.Main.onClick"),
        _exc(1_600_000, "java.io.IOException", "both", "java.io.File.open|" + f"{APP}.Io.read"),
        _lc(1_100_000, "onCreate"), _lc(1_200_000, "onStart"),
    ], 'E/linker: dlopen failed: library "libsqlcipher.so" not found\nALSA lib pcm.c: Unknown PCM default\n', STUBS)
    android = _capture(tmp_path / "android", "android", [
        _exc(1_600_000, "java.io.IOException", "both", "java.io.File.open|" + f"{APP}.Io.read"),
        _lc(1_100_000, "onCreate"), _lc(1_150_000, "onPostCreate"), _lc(1_200_000, "onStart"),
    ], "ALSA lib pcm.c: Unknown PCM default\n")
    return atl, android


def test_sections_compare_atl_with_android(pair, tmp_path):
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    assert [s["title"] for s in d["stubs"]][:1] == ["android.view.inputmethod.InputMethodManager.showSoftInput"]
    assert d["stubs"][0]["count"] == 7 and d["stubs"][0]["caller"] == f"{APP}.SearchField.focus"
    assert [e["title"] for e in d["exceptions"]] == ["java.lang.NullPointerException: x is null"]
    assert d["exceptions"][0]["where"] == f"{APP}.Main.onClick"
    assert [n["key"] for n in d["native"]] == ["dlopen:libsqlcipher.so"]       # ALSA is on Android too
    assert len(d["lifecycle"]) == 1 and "onPostCreate" in d["lifecycle"][0]["title"]


def test_causes_ranked_exception_first(pair, tmp_path):
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    kinds = [c["kind"] for c in d["causes"]]
    assert kinds[:3] == ["exception", "native", "lifecycle"]
    assert d["causes"][0]["score"] > d["causes"][-1]["score"]


def test_markdown_is_short_with_many_findings(pair, tmp_path):
    atl, _ = pair
    many = "".join(f"N\t{i}\tandroid.x.C{i}\tm\t()V\tempty\nS\t2500000\t{i}\t{i}\n" for i in range(10, 200))
    (atl / "stubhits.tsv").write_text(STUBS + many)
    with open(atl / "agent.tsv", "a") as f:
        f.write(_exc(1_800_000, "javax.net.ssl.SSLPeerUnverifiedException", "Hostname not verified: " + "x" * 400,
                     f"{APP}.Net.get") + "\n")
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    md = render_markdown(d)
    assert len(md.splitlines()) <= MAX_LINES
    assert max(len(x) for x in md.splitlines()) <= 240
    assert md.splitlines()[0].startswith("# atl-doctor compare")
    assert "## Likely causes" in md and md.index("## Likely causes") < md.index("## Stubs hit")


def test_shot_steps_are_compared(pair, tmp_path):
    atl, android = pair
    png_write(atl / "shots/search.png", 100, 100, bytes((250, 250, 250, 255)) * 10000)
    png_write(android / "shots/search.png", 100, 200,
              b"".join(bytes((0, 0, 0, 255) if (x // 10) % 2 else (255, 255, 255, 255)) for _ in range(200) for x in range(100)))
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    assert d["shots"][0]["step"] == "search" and d["shots"][0]["finding"] == "ATL empty"
    assert any(c["kind"] == "shot" for c in d["causes"])
    assert (tmp_path / "out/shots/search-diff.png").is_file()


def test_cli_with_existing_captures_writes_report(pair, tmp_path, capsys):
    out = tmp_path / "v"
    assert main(["compare", "--atl-dir", str(pair[0]), "--android-dir", str(pair[1]), "-o", str(out),
                 "--no-aosp-fetch", "unused.apk"]) == 0
    md = (out / "report.md").read_text()
    assert capsys.readouterr().out == md
    assert json.loads((out / "report.json").read_text())["causes"]


def test_cli_records_light_by_default_and_full_with_deep(tmp_path, monkeypatch, pair):
    import atl_doctor.compare as v

    seen = []

    def fake_atl(opts):
        seen.append(("atl", opts.light, opts.trace, opts.steps, opts.launcher))
        return 0

    def fake_android(opts):
        seen.append(("android", opts.steps))
        return 0

    monkeypatch.setattr(v, "record_atl", fake_atl)
    monkeypatch.setattr(v, "record_android", fake_android)
    monkeypatch.setattr(v, "build_comparison", lambda *a, **k: {"causes": [], "app": APP})
    steps = tmp_path / "s.steps"
    steps.write_text("wait 0\n")
    assert main(["compare", "-o", str(tmp_path / "a"), "--prefix", str(tmp_path), "--scenario", str(steps),
                 "x.apk"]) == 0
    assert main(["compare", "-o", str(tmp_path / "b"), "--launcher", "flatpak run X", "--deep", "x.apk"]) == 0
    assert seen[0] == ("atl", True, True, steps, None) and seen[1] == ("android", steps)
    default = tmp_path / "b/scenario.steps"                     # without --scenario: wait, then one screenshot
    assert seen[2] == ("atl", False, True, default, "flatpak run X") and "screenshot" in default.read_text()


def test_exception_inside_the_platform_ranks_below_one_through_the_app(pair, tmp_path):
    atl, _ = pair
    with open(atl / "agent.tsv", "a") as f:
        for t in (1_700_000, 1_710_000, 1_720_000):
            f.write(_exc(t, "java.nio.file.FileAlreadyExistsException", "/data/lib", "java.nio.file.Files.createDirectory|"
                         "android.atl.Natives.extract").replace(f"{APP}.Main.onClick", "android.atl.Natives.extract") + "\n")
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    titles = [c["title"] for c in d["causes"]]
    assert titles.index("java.lang.NullPointerException: x is null") < titles.index("native library does not load: libsqlcipher.so")
    assert titles.index("native library does not load: libsqlcipher.so") < \
        titles.index("java.nio.file.FileAlreadyExistsException: /data/lib")
    assert d["causes"][0]["phase"] == "Search"


def test_scenario_errors_are_in_the_report(tmp_path, monkeypatch, pair):
    import atl_doctor.compare as v
    from atl_doctor.errors import DoctorError

    def fail(opts):
        raise DoctorError("s.steps:13: no visible view matches id/suw_navbar_done")

    monkeypatch.setattr(v, "record_atl", fail)
    monkeypatch.setattr(v, "record_android", lambda opts: 0)
    import shutil
    shutil.copytree(pair[0], tmp_path / "v/atl")
    shutil.copytree(pair[1], tmp_path / "v/android")
    assert main(["compare", "-o", str(tmp_path / "v"), "--prefix", str(tmp_path), "--no-aosp-fetch", "x.apk"]) == 0
    md = (tmp_path / "v/report.md").read_text()
    assert "Scenario stopped on ATL: s.steps:13: no visible view matches id/suw_navbar_done" in md.splitlines()[2]


def test_side_whose_app_ended_before_the_scenario_is_named(pair, tmp_path):
    atl, android = pair
    for side, stopped in ((atl, "steps"), (android, None)):
        c = Capture.open(side)
        m = c.read_manifest()
        m.environment = {"stopped_by": stopped} if stopped else {}
        c.write_manifest(m)
    (android / "steps.log").write_text("22:40:01 s:3: wait 20.0\n")
    (atl / "steps.log").write_text("22:40:01 s:3: wait 20.0\n")
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    assert d["unfinished"] == ["Android"]
    assert "Scenario not finished on Android: the app ended" in render_markdown(d)


def test_what_happens_during_the_scenario_ranks_above_the_start(pair, tmp_path):
    """Startup findings (phase 0) are often noise next to what a step triggers: a lifecycle difference after a
    mark ranks above an exception through the app at startup."""
    atl, android = pair
    with open(atl / "agent.tsv", "a") as f:
        f.write(_exc(1_300_000, "java.lang.IllegalStateException", "early", f"android.app.Activity.attach|{APP}.App.init") + "\n")
        f.write(_lc(2_100_000, "onResume") + "\n")
    with open(android / "agent.tsv", "a") as f:
        f.write(_lc(2_100_000, "onResume") + "\n")
        f.write(_lc(2_200_000, "onWindowFocusChanged") + "\n")
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    titles = [c["title"] for c in d["causes"]]
    focus = next(t for t in titles if "onWindowFocusChanged" in t)
    assert titles.index(focus) < titles.index("java.lang.IllegalStateException: early")
