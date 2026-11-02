# SPDX-License-Identifier: GPL-3.0-or-later
"""App state snapshots (`atl-doctor zustand sichern|laden APK NAME`).

Under ATL the app's data directory is copied (ATL keeps it in $ANDROID_APP_DATA_DIR, else
$XDG_DATA_HOME/android_translation_layer, inside a Flatpak below ~/.var/app/ID/data, as `<apk name>_`); the copies
live in $XDG_DATA_HOME/atl-doctor/zustand/<apk name>/NAME. On Android the emulator saves and loads a whole AVD
snapshot named atl-doctor-PACKAGE-NAME (adb emu), so this needs an emulator, not a phone.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
from pathlib import Path

from .errors import DoctorError, PrerequisiteMissing, UsageError


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _flatpak_app(launcher: str | None) -> str | None:
    """The application ID of a `flatpak run ... ID` launcher."""
    args = shlex.split(launcher or "")
    if args[:2] != ["flatpak", "run"]:
        return None
    return next((a for a in args[2:] if not a.startswith("-")), None)


def atl_data_dir(apk: Path, launcher: str | None) -> Path:
    """ATL's data directory for this APK (see the module docstring)."""
    base = os.environ.get("ANDROID_APP_DATA_DIR")
    app = _flatpak_app(launcher)
    if base:
        root = Path(base)
    elif app:
        root = Path.home() / ".var/app" / app / "data/android_translation_layer"
    else:
        root = _data_home() / "android_translation_layer"
    return root / f"{Path(apk).name}_"


def _store(apk: Path, name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        raise UsageError(f"bad state name {name!r}")
    return _data_home() / "atl-doctor/zustand" / Path(apk).name / name


def save_atl(apk: Path, name: str, launcher: str | None = None) -> Path:
    data, store = atl_data_dir(apk, launcher), _store(apk, name)
    if not data.is_dir():
        raise DoctorError(f"no app data in {data} (start the app once under ATL first)")
    if store.exists():
        shutil.rmtree(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(data, store, symlinks=True)
    return store


def load_atl(apk: Path, name: str, launcher: str | None = None) -> Path:
    data, store = atl_data_dir(apk, launcher), _store(apk, name)
    if not store.is_dir():
        raise DoctorError(f"no saved state {name!r} for {Path(apk).name} (atl-doctor zustand sichern first)")
    if data.exists():
        shutil.rmtree(data)
    data.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(store, data, symlinks=True)
    return data


def _android_snapshot(action: str, package: str, name: str) -> str:
    from .env import adb_device
    from .record_android import Adb

    _store(Path(package), name)  # same name rules
    serial = adb_device()
    if serial is None:
        raise PrerequisiteMissing("zustand --android needs an emulator: no device attached (check `adb devices`)")
    adb = Adb(serial)
    snap = f"atl-doctor-{package}-{name}"
    out = adb.out("emu", "avd", "snapshot", action, snap, timeout=300).strip()
    if not out.startswith("OK"):
        raise DoctorError(f"emulator snapshot {action} {snap} failed: {out or 'no answer (not an emulator?)'}")
    if action == "load":
        adb.run("wait-for-device", timeout=120)
    return snap


def load_start_state(name: str, apk: Path, android: bool, launcher: str | None = None) -> str:
    """Load the state a scenario starts from (`start_zustand: NAME`); returns what was loaded."""
    if android:
        from .sources.dex import read_apk

        return _android_snapshot("load", read_apk(apk).package, name)
    return str(load_atl(apk, name, launcher))


def _run(args: argparse.Namespace) -> int:
    apk = Path(args.apk)
    if args.android:
        from .sources.dex import read_apk

        snap = _android_snapshot("save" if args.action == "sichern" else "load", read_apk(apk).package, args.name)
        print(snap)
    elif args.action == "sichern":
        print(save_atl(apk, args.name, args.launcher))
    else:
        print(load_atl(apk, args.name, args.launcher))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import common_parser

    p = sub.add_parser("zustand", parents=[common_parser()], help="save or load an app's state (data directory / AVD snapshot)")
    p.add_argument("action", choices=["sichern", "laden"])
    p.add_argument("--android", action="store_true", help="on the Android emulator (AVD snapshot over adb)")
    p.add_argument("--launcher", metavar="CMD", help="the ATL launcher (a Flatpak keeps its data below ~/.var/app)")
    p.add_argument("apk", metavar="APK")
    p.add_argument("name", metavar="NAME")
    p.set_defaults(func=_run)
