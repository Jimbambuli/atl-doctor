# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from atl_doctor.cli import main
from atl_doctor.zustand import atl_data_dir
from test_record_android import PKG, adb, apk, calls, table  # noqa: F401 (fixtures)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    for v in ("XDG_DATA_HOME", "ANDROID_APP_DATA_DIR"):
        monkeypatch.delenv(v, raising=False)
    return h


def test_atl_data_dir_default_and_overrides(home, monkeypatch):
    a = Path("/x/app.apk")
    assert atl_data_dir(a, None) == home / ".local/share/android_translation_layer/app.apk_"
    assert atl_data_dir(a, "flatpak run --user --filesystem=/tmp io.github.X.Starter") == \
        home / ".var/app/io.github.X.Starter/data/android_translation_layer/app.apk_"
    monkeypatch.setenv("ANDROID_APP_DATA_DIR", "/d")
    assert atl_data_dir(a, None) == Path("/d/app.apk_")


def test_atl_save_and_load_roundtrip(home, apk, capsys):
    data = atl_data_dir(apk, None)
    (data / "shared_prefs").mkdir(parents=True)
    (data / "shared_prefs/p.xml").write_text("eingerichtet")
    assert main(["zustand", "sichern", str(apk), "eingerichtet"]) == 0
    (data / "shared_prefs/p.xml").write_text("verdorben")
    (data / "neu.txt").write_text("x")
    assert main(["zustand", "laden", str(apk), "eingerichtet"]) == 0
    assert (data / "shared_prefs/p.xml").read_text() == "eingerichtet"
    assert not (data / "neu.txt").exists()


def test_atl_load_unknown_state_is_an_error(home, apk, capsys):
    assert main(["zustand", "laden", str(apk), "fehlt"]) == 1
    assert "no saved state 'fehlt'" in capsys.readouterr().err


def test_atl_save_without_data_dir_is_an_error(home, apk, capsys):
    assert main(["zustand", "sichern", str(apk), "x"]) == 1
    assert "no app data" in capsys.readouterr().err


def test_android_uses_avd_snapshot(adb, apk):  # noqa: F811
    table(adb)
    t = json.loads((adb / "table.json").read_text())
    t["emu avd snapshot"] = ["OK\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    assert main(["zustand", "sichern", "--android", str(apk), "eingerichtet"]) == 0
    assert main(["zustand", "laden", "--android", str(apk), "eingerichtet"]) == 0
    c = calls(adb)
    assert f"emu avd snapshot save atl-doctor-{PKG}-eingerichtet" in c
    assert f"emu avd snapshot load atl-doctor-{PKG}-eingerichtet" in c
    assert c.index(f"emu avd snapshot load atl-doctor-{PKG}-eingerichtet") < len(c) - 1  # waits for the device after


def test_android_snapshot_failure_is_an_error(adb, apk, capsys):  # noqa: F811
    table(adb)
    t = json.loads((adb / "table.json").read_text())
    t["emu avd snapshot"] = ["KO: snapshot not found\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    assert main(["zustand", "laden", "--android", str(apk), "x"]) == 1
    assert "snapshot not found" in capsys.readouterr().err
