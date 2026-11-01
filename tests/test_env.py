# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from pathlib import Path

import atl_doctor
from atl_doctor import env
from atl_doctor.cli import main

EXPECTED = ["atl", "api-impl.jar", "atl-src", "jvmti-plugin", "agent", "tracecount", "input-helper", "kwin",
            "spectacle", "viewdump", "aosp"]


def test_resolve_option_beats_env(monkeypatch):
    monkeypatch.setenv("ATL_DOCTOR_PREFIX", "/from/env")
    assert env.resolve("/from/option", "ATL_DOCTOR_PREFIX") == "/from/option"
    assert env.resolve(None, "ATL_DOCTOR_PREFIX") == "/from/env"
    monkeypatch.delenv("ATL_DOCTOR_PREFIX")
    assert env.resolve(None, "ATL_DOCTOR_PREFIX") is None


def test_helper_dirs_order(monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/h")))
    assert env.HELPER_DIRS(Path("/p")) == [
        Path(atl_doctor.__file__).parent / "data", Path("/p/lib/atl-doctor"), Path("/h/.local/lib/atl-doctor"),
        Path("/usr/local/lib/atl-doctor"), Path("/usr/lib/atl-doctor"), Path(sys.prefix) / "lib/atl-doctor"]
    assert env.HELPER_DIRS(None)[1] == Path("/h/.local/lib/atl-doctor")


def test_find_helper_order(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "atl-doctor-input").write_text("x")
        (d / "libatl_doctor_agent.so").write_text("x")
    monkeypatch.setattr(env, "HELPER_DIRS", lambda prefix: [a, b])
    assert env.find_helper("atl-doctor-input", None, None) == a / "atl-doctor-input"
    assert env.find_helper("nope", None, None) is None
    # agent: --agent beats ATL_DOCTOR_AGENT beats the search list
    monkeypatch.setenv("ATL_DOCTOR_AGENT", str(b / "libatl_doctor_agent.so"))
    assert env.find_helper("libatl_doctor_agent.so", None, None) == b / "libatl_doctor_agent.so"
    assert env.find_helper("libatl_doctor_agent.so", a / "libatl_doctor_agent.so", None) == a / "libatl_doctor_agent.so"
    assert env.find_helper("libatl_doctor_agent.so", tmp_path / "missing.so", None) is None


def test_probe_names_and_order(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "HELPER_DIRS", lambda prefix: [])
    monkeypatch.setenv("PATH", str(tmp_path))
    ps = env.probe_all(None, None, False, None)
    assert [p.name for p in ps] == EXPECTED
    assert all(not p.available and p.hint for p in ps)
    ps = env.probe_all(None, None, True, None)
    assert [p.name for p in ps] == EXPECTED + ["adb", "android-device", "android-root", "android-agent"]


def test_probes_with_prefix(tmp_path, monkeypatch):
    from dexgen import cls, make_dex, make_jar
    monkeypatch.setattr(env, "HELPER_DIRS", lambda prefix: [])
    pre = tmp_path / "pre"
    (pre / "bin").mkdir(parents=True)
    (pre / "bin/android-translation-layer").write_text("")
    (pre / "lib64/art").mkdir(parents=True)
    (pre / "lib64/art/libopenjdkjvmti.so").write_text("")
    jar = pre / "lib64/java/dex/android_translation_layer"
    jar.mkdir(parents=True)
    make_jar(jar / "api-impl.jar", [make_dex([cls("Landroid/atl/ATLViewDump;")])])
    src = tmp_path / "src"
    (src / "src/api-impl").mkdir(parents=True)
    got = {p.name: p.available for p in env.probe_all(pre, src, False, None)}
    assert got["atl"] and got["api-impl.jar"] and got["jvmti-plugin"] and got["viewdump"] and got["atl-src"]
    assert not got["agent"]


def test_check_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(env, "HELPER_DIRS", lambda prefix: [])
    monkeypatch.delenv("ATL_DOCTOR_PREFIX", raising=False)
    assert main(["check", "--prefix", str(tmp_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("atl              missing: ") and "; fix: " in out[0]
    assert len(out) == len(EXPECTED)


def test_check_android_without_adb_exit_3(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["check", "--android"]) == 3
    cap = capsys.readouterr()
    assert "adb" in cap.out and "atl-doctor: " in cap.err
