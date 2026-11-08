# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from atl_doctor.cli import main
from atl_doctor.state import atl_data_dir
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
    (data / "shared_prefs/p.xml").write_text("ready")
    assert main(["state", "save", str(apk), "ready"]) == 0
    (data / "shared_prefs/p.xml").write_text("corrupt")
    (data / "new.txt").write_text("x")
    assert main(["state", "load", str(apk), "ready"]) == 0
    assert (data / "shared_prefs/p.xml").read_text() == "ready"
    assert not (data / "new.txt").exists()


def test_atl_load_unknown_state_is_an_error(home, apk, capsys):
    assert main(["state", "load", str(apk), "missing"]) == 1
    assert "no saved state 'missing'" in capsys.readouterr().err


def test_atl_save_without_data_dir_is_an_error(home, apk, capsys):
    assert main(["state", "save", str(apk), "x"]) == 1
    assert "no app data" in capsys.readouterr().err


def test_android_uses_avd_snapshot(adb, apk):  # noqa: F811
    table(adb)
    t = json.loads((adb / "table.json").read_text())
    t["emu avd snapshot"] = ["OK\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    assert main(["state", "save", "--android", str(apk), "ready"]) == 0
    assert main(["state", "load", "--android", str(apk), "ready"]) == 0
    c = calls(adb)
    assert f"emu avd snapshot save atl-doctor-{PKG}-ready" in c
    assert f"emu avd snapshot load atl-doctor-{PKG}-ready" in c
    assert c.index(f"emu avd snapshot load atl-doctor-{PKG}-ready") < len(c) - 1  # waits for the device after


def test_android_snapshot_failure_is_an_error(adb, apk, capsys):  # noqa: F811
    table(adb)
    t = json.loads((adb / "table.json").read_text())
    t["emu avd snapshot"] = ["KO: snapshot not found\n", 0]
    (adb / "table.json").write_text(json.dumps(t))
    assert main(["state", "load", "--android", str(apk), "x"]) == 1
    assert "snapshot not found" in capsys.readouterr().err


def test_android_falls_back_to_the_app_data_when_the_emulator_refuses_a_snapshot(adb, apk, home, capsys):  # noqa: F811
    table(adb)
    t = json.loads((adb / "table.json").read_text())
    t["emu avd snapshot save"] = ["KO: Snapshot save is skipped. Reason: UNSUPPORTED_VK_APP\n", 0]
    t["exec-out tar"] = ["TARDATA", 0]
    (adb / "table.json").write_text(json.dumps(t))
    assert main(["state", "save", "--android", str(apk), "signed_in"]) == 0
    saved = home / f".local/share/atl-doctor/state/android/{PKG}/signed_in.tar"
    assert saved.read_bytes() == b"TARDATA"
    assert main(["state", "load", "--android", str(apk), "signed_in"]) == 0
    c = calls(adb)
    assert f"exec-out tar -C /data/data -cf - {PKG}" in c
    assert not any(x.startswith("emu avd snapshot load") for x in c)
    assert any(x.startswith("push ") and x.endswith("/data/local/tmp/atl-doctor-state.tar") for x in c)
    assert any(x.startswith("shell") and "tar -C /data/data -xf /data/local/tmp/atl-doctor-state.tar" in x for x in c)


class _Dev:
    """A device whose installed apps change when a snapshot is loaded."""

    def __init__(self, apps):
        self.apps, self.calls = dict(apps), []

    def run(self, *args, timeout=60):
        import subprocess

        self.calls.append(" ".join(args))
        out = b""
        if args[:4] == ("shell", "pm", "list", "packages"):
            out = "".join(f"package:{p} versionCode:{v}\n" for p, v in self.apps.items()).encode()
        elif args[0] == "pull":
            Path(args[2]).write_bytes(b"APK")
        elif args[0] == "install":
            self.apps[args[-1].split("/")[-2].rsplit("-", 1)[0]] = int(args[-1].split("/")[-2].rsplit("-", 1)[1])
        return subprocess.CompletedProcess(args, 0, out, b"")

    def shell(self, *args, timeout=60):
        return "package:/data/app/x/base.apk\n" if args[:2] == ("pm", "path") else ""


def test_android_snapshot_load_keeps_the_other_apps(home, monkeypatch, capsys):
    from atl_doctor import state

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    dev = _Dev({PKG: 1, "com.aurora.store": 76, "org.other": 5})
    before = state._apps_kept(dev, PKG)
    assert before == {"com.aurora.store": 76, "org.other": 5}  # not the app the state belongs to
    dev.apps = {PKG: 1, "org.other": 5}  # the snapshot is older than the Aurora installation
    state._reinstall_lost(dev, before)
    assert dev.apps["com.aurora.store"] == 76
    assert not any(c.startswith("install") and "org.other" in c for c in dev.calls)
    assert "com.aurora.store installed again" in capsys.readouterr().err
    n = len([c for c in dev.calls if c.startswith("pull")])
    state._apps_kept(dev, PKG)  # the APKs are pulled once per version
    assert len([c for c in dev.calls if c.startswith("pull")]) == n
