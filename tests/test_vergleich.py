# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from atl_doctor.capture import Capture
from atl_doctor.cli import main
from atl_doctor.sources.shots import png_write
from atl_doctor.vergleich import MAX_LINES, build_comparison, render_markdown

APP = "org.example.demo"


def _capture(root: Path, side: str, agent: list[str], log: str = "", stubs: str = "") -> Path:
    c = Capture.create(root, side)
    m = c.read_manifest()
    m.package, m.apk, m.ended = APP, str(root / "demo.apk"), "2026-11-02T22:00:00"
    c.write_manifest(m)
    (root / "marks.tsv").write_text("")
    c.append_mark("start", 1_000_000)
    c.append_mark("Suche", 2_000_000)
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
        _exc(1_500_000, "java.lang.NullPointerException", "x is null", f"android.view.View.performClick|{APP}.Main.onClick"),
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
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    md = render_markdown(d)
    assert len(md.splitlines()) <= MAX_LINES
    assert md.splitlines()[0].startswith("# atl-doctor vergleich")
    assert "## Likely causes" in md and md.index("## Likely causes") < md.index("## Stubs hit")


def test_shot_steps_are_compared(pair, tmp_path):
    atl, android = pair
    png_write(atl / "shots/suche.png", 100, 100, bytes((250, 250, 250, 255)) * 10000)
    png_write(android / "shots/suche.png", 100, 200,
              b"".join(bytes((0, 0, 0, 255) if (x // 10) % 2 else (255, 255, 255, 255)) for _ in range(200) for x in range(100)))
    d = build_comparison(*pair, tmp_path / "out", no_aosp_fetch=True)
    assert d["shots"][0]["step"] == "suche" and d["shots"][0]["finding"] == "ATL empty"
    assert any(c["kind"] == "shot" for c in d["causes"])
    assert (tmp_path / "out/shots/suche-diff.png").is_file()


def test_cli_with_existing_captures_writes_report(pair, tmp_path, capsys):
    out = tmp_path / "v"
    assert main(["vergleich", "--atl-dir", str(pair[0]), "--android-dir", str(pair[1]), "-o", str(out),
                 "--no-aosp-fetch", "unused.apk"]) == 0
    md = (out / "bericht.md").read_text()
    assert capsys.readouterr().out == md
    assert json.loads((out / "bericht.json").read_text())["causes"]


def test_cli_records_light_by_default_and_full_with_tief(tmp_path, monkeypatch, pair):
    import atl_doctor.vergleich as v

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
    assert main(["vergleich", "-o", str(tmp_path / "a"), "--prefix", str(tmp_path), "--szenario", str(steps),
                 "x.apk"]) == 0
    assert main(["vergleich", "-o", str(tmp_path / "b"), "--launcher", "flatpak run X", "--tief", "x.apk"]) == 0
    assert seen[0] == ("atl", True, True, steps, None) and seen[1] == ("android", steps)
    default = tmp_path / "b/szenario.steps"                     # without --szenario: wait, then one screenshot
    assert seen[2] == ("atl", False, True, default, "flatpak run X") and "screenshot" in default.read_text()
