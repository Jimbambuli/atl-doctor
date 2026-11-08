# SPDX-License-Identifier: GPL-3.0-or-later
"""App state snapshots (`atl-doctor state save|load APK NAME`).

Under ATL the app's data directory is copied (ATL keeps it in $ANDROID_APP_DATA_DIR, else
$XDG_DATA_HOME/android_translation_layer, inside a Flatpak below ~/.var/app/ID/data, as `<apk name>_`); the copies
live in $XDG_DATA_HOME/atl-doctor/state/<apk name>/NAME. On Android the emulator saves and loads a whole AVD
snapshot named atl-doctor-PACKAGE-NAME (adb emu), so this needs an emulator, not a phone. Where the emulator skips
the snapshot (an app used Vulkan), the app's /data/data directory is copied as root instead
($XDG_DATA_HOME/atl-doctor/state/android/PACKAGE/NAME.tar) and loaded from there.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
import time
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
    return _data_home() / "atl-doctor/state" / Path(apk).name / name


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
        raise DoctorError(f"no saved state {name!r} for {Path(apk).name} (atl-doctor state save first)")
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
        raise PrerequisiteMissing("state --android needs an emulator: no device attached (check `adb devices`)")
    adb = Adb(serial)
    snap = f"atl-doctor-{package}-{name}"
    tar = _data_home() / "atl-doctor/state/android" / package / f"{name}.tar"
    if action == "load" and tar.is_file():
        return _android_data("load", adb, package, tar)
    before = _apps_kept(adb, package) if action == "load" else {}
    out = adb.out("emu", "avd", "snapshot", action, snap, timeout=300).strip()
    if action == "save" and not out.startswith("OK") and "skipped" in out:
        return _android_data("save", adb, package, tar)  # e.g. "Reason: UNSUPPORTED_VK_APP" (an app used Vulkan)
    if not out.startswith("OK"):
        raise DoctorError(f"emulator snapshot {action} {snap} failed: {out or 'no answer (not an emulator?)'}")
    tar.unlink(missing_ok=True)  # the snapshot is newer than an app-data copy of the same name
    if action == "load":
        adb.run("wait-for-device", timeout=120)
        _reinstall_lost(adb, before)
    return snap


def _third_party(adb) -> dict[str, int] | None:
    """Installed third-party packages and their version codes; None while the package manager does not answer."""
    r = adb.run("shell", "pm", "list", "packages", "-3", "--show-versioncode")
    if r.returncode != 0:
        return None
    apps = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        f = dict(x.split(":", 1) for x in line.split() if ":" in x)
        if f.get("package") and f.get("versionCode", "").isdigit():
            apps[f["package"]] = int(f["versionCode"])
    return apps


def _apk_dir(package: str, version: int) -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "atl-doctor/apks" / f"{package}-{version}"


def _apps_kept(adb, package: str) -> dict[str, int]:
    """Before an AVD snapshot load: copy the APKs of the other installed apps (once per version) so that the apps
    the snapshot does not have can be installed again; a snapshot puts back the whole device of its time."""
    kept = {}
    for pkg, vc in (_third_party(adb) or {}).items():
        d = _apk_dir(pkg, vc)
        if pkg != package and not any(d.glob("*.apk")):
            paths = [x[8:] for x in adb.shell("pm", "path", pkg).split() if x.startswith("package:")]
            tmp = d.with_name(d.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            if paths and all(adb.run("pull", p, str(tmp / f"{i}-{Path(p).name}"), timeout=300).returncode == 0
                             for i, p in enumerate(paths)):
                shutil.rmtree(d, ignore_errors=True)
                tmp.rename(d)
            else:
                shutil.rmtree(tmp, ignore_errors=True)
        if pkg != package and any(d.glob("*.apk")):
            kept[pkg] = vc
    return kept


def _reinstall_lost(adb, before: dict[str, int]) -> None:
    """Install the apps again that the loaded snapshot does not have (or only in an older version)."""
    after = None
    for _ in range(60):
        after = _third_party(adb)
        if after is not None:
            break
        time.sleep(1)
    for pkg, vc in before.items():
        if after is not None and after.get(pkg, -1) >= vc:
            continue
        apks = sorted(str(a) for a in _apk_dir(pkg, vc).glob("*.apk"))
        r = adb.run("install-multiple" if len(apks) > 1 else "install", "-r", *apks, timeout=300)
        ok = r.returncode == 0
        print(f"atl-doctor: state: {pkg} {'installed again' if ok else 'NOT installed again'} "
              f"(not in the loaded snapshot)", file=sys.stderr)


DEVICE_TAR = "/data/local/tmp/atl-doctor-state.tar"


def _android_data(action: str, adb, package: str, tar: Path) -> str:
    """Fallback when the emulator will not take a snapshot: the app's /data/data directory as root (adb root)."""
    adb.out("root")
    adb.run("wait-for-device", timeout=60)
    adb.shell("am", "force-stop", package)
    if action == "save":
        r = adb.run("exec-out", "tar", "-C", "/data/data", "-cf", "-", package, timeout=300)
        if r.returncode != 0 or not r.stdout:
            raise DoctorError(f"cannot copy /data/data/{package} (needs adb root)")
        tar.parent.mkdir(parents=True, exist_ok=True)
        tar.write_bytes(r.stdout)
        return str(tar)
    if adb.run("push", str(tar), DEVICE_TAR, timeout=300).returncode != 0:
        raise DoctorError(f"adb push {tar} failed")
    d = f"/data/data/{package}"
    script = (f'o=$(stat -c %u:%g {d}) && rm -rf {d}/* && tar -C /data/data -xf {DEVICE_TAR} && chown -R $o {d} '
              f'&& restorecon -R {d}; rc=$?; rm -f {DEVICE_TAR}; exit $rc')
    if adb.run("shell", script, timeout=300).returncode != 0:
        raise DoctorError(f"cannot restore {d} (needs adb root)")
    return str(tar)


def load_start_state(name: str, apk: Path, android: bool, launcher: str | None = None) -> str:
    """Load the state a scenario starts from (`start_state: NAME`); returns what was loaded."""
    if android:
        from .sources.dex import read_apk

        return _android_snapshot("load", read_apk(apk).package, name)
    return str(load_atl(apk, name, launcher))


def _run(args: argparse.Namespace) -> int:
    apk = Path(args.apk)
    if args.android:
        from .sources.dex import read_apk

        snap = _android_snapshot("save" if args.action == "save" else "load", read_apk(apk).package, args.name)
        print(snap)
    elif args.action == "save":
        print(save_atl(apk, args.name, args.launcher))
    else:
        print(load_atl(apk, args.name, args.launcher))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import command, done

    p, g = command(sub, "state", "save or load an app's state (data dir / AVD snapshot)",
                   "Save the state of an app under a name and put it back later: its data directory under\n"
                   "ATL, an AVD snapshot with --android. A step file can begin with start_state: NAME.")
    g.args.add_argument("action", choices=["save", "load"], metavar="{save,load}",
                        help="save the state, or load a saved one")
    g.args.add_argument("apk", metavar="APK", help="the app")
    g.args.add_argument("name", metavar="NAME", help="name of the saved state")
    g.input.add_argument("--android", action="store_true", help="on the Android emulator (AVD snapshot over adb)")
    g.atl.add_argument("--launcher", metavar="CMD",
                       help="the ATL launcher (a Flatpak keeps its data below ~/.var/app)")
    done(p, g, _run)
