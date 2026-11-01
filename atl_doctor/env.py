# SPDX-License-Identifier: GPL-3.0-or-later
"""Environment: option/variable resolution, ATL installation layout, helper lookup, probes, `check`."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import atl_doctor

from .errors import PrerequisiteMissing

AGENT_LIB = "libatl_doctor_agent.so"
TRACECOUNT = "atl-doctor-tracecount"
INPUT_HELPER = "atl-doctor-input"
WHEEL_SHIM = "libatl_doctor_wheel.so"


@dataclass
class Probe:
    name: str
    available: bool
    reason: str = ""
    hint: str = ""


def resolve(cli_value: str | None, env_name: str) -> str | None:
    """Option before environment variable; empty values count as unset."""
    return cli_value or os.environ.get(env_name) or None


def HELPER_DIRS(prefix: Path | None) -> list[Path]:  # noqa: N802 (name fixed by the design)
    """Where compiled helpers are looked for, in this order."""
    dirs = [Path(atl_doctor.__file__).parent / "data"]
    if prefix:
        dirs.append(Path(prefix) / "lib/atl-doctor")
    dirs += [Path.home() / ".local/lib/atl-doctor", Path("/usr/local/lib/atl-doctor"), Path("/usr/lib/atl-doctor"),
             Path(sys.prefix) / "lib/atl-doctor"]
    return dirs


def find_helper(name: str, explicit: Path | None, prefix: Path | None) -> Path | None:
    """--agent / ATL_DOCTOR_AGENT (agent only) before the search list; None if not found."""
    if explicit is None and name == AGENT_LIB and os.environ.get("ATL_DOCTOR_AGENT"):
        explicit = Path(os.environ["ATL_DOCTOR_AGENT"])
    if explicit is not None:
        return Path(explicit) if Path(explicit).is_file() else None
    for d in HELPER_DIRS(prefix):
        if (d / name).is_file():
            return d / name
    return None


def lib_dir(prefix: Path) -> Path:
    """Library directory of an ATL prefix: lib64 (art_standalone's default), or lib where only that one has ART
    (art_standalone built with ____LIBDIR=lib, e.g. a Flatpak's /app)."""
    for d in ("lib64", "lib"):
        if (Path(prefix) / d / "art").is_dir():
            return Path(prefix) / d
    return Path(prefix) / "lib64"


def atl_jars(prefix: Path | None) -> list[Path]:
    """ATL's Java classes (api-impl, gstub, ghax ...) of an installation prefix."""
    if prefix is None:
        return []
    return sorted((lib_dir(prefix) / "java/dex/android_translation_layer").glob("*.jar"))


def libcore_jars(prefix: Path | None) -> list[Path]:
    """libcore host DEX jars shipped with art_standalone."""
    if prefix is None:
        return []
    return sorted((lib_dir(prefix) / "java/dex/art").glob("*-hostdex.jar"))


def jvmti_plugin(prefix: Path | None) -> Path | None:
    if prefix is None:
        return None
    for rel in ("art/libopenjdkjvmti.so", "libopenjdkjvmti.so"):
        if (lib_dir(prefix) / rel).is_file():
            return lib_dir(prefix) / rel
    return None


def has_viewdump(prefix: Path | None) -> bool:
    """True if an ATL jar contains the class android.atl.ATLViewDump."""
    import zipfile

    needle = b"Landroid/atl/ATLViewDump;"
    for j in atl_jars(prefix):
        try:
            with zipfile.ZipFile(j) as z:
                for n in z.namelist():
                    if n.startswith("classes") and n.endswith(".dex") and needle in z.read(n):
                        return True
        except (zipfile.BadZipFile, OSError):
            continue
    return False


def _adb(*args: str, timeout: float = 10) -> str | None:
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def adb_device() -> str | None:
    """Serial of the first attached device in state `device`, else None."""
    out = _adb("devices")
    for line in (out or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def adb_is_root() -> bool:
    return (_adb("shell", "id", "-u") or "").strip() == "0"


def adb_abi() -> str | None:
    out = (_adb("shell", "getprop", "ro.product.cpu.abi") or "").strip()
    return out or None


def _probe(name: str, ok: bool, reason: str, hint: str) -> Probe:
    return Probe(name, True) if ok else Probe(name, False, reason, hint)


def probe_all(prefix: Path | None, atl_src: Path | None, android: bool, aosp: Path | None) -> list[Probe]:
    """All measurement probes in a fixed order."""
    prefix = Path(prefix) if prefix else None
    no_prefix = "no ATL prefix given"
    prefix_hint = "pass --prefix DIR or set ATL_DOCTOR_PREFIX"
    ps: list[Probe] = []
    atl = prefix / "bin/android-translation-layer" if prefix else None
    ps.append(_probe("atl", bool(atl and atl.is_file()), no_prefix if not prefix else f"{atl} not found", prefix_hint))
    jars = [j for j in atl_jars(prefix) if j.name == "api-impl.jar"]
    ps.append(_probe("api-impl.jar", bool(jars),
                     no_prefix if not prefix else f"no api-impl.jar under {prefix}/lib64/java/dex/android_translation_layer",
                     "install ATL (ninja install) into the prefix"))
    has_src = bool(atl_src and any((Path(atl_src) / p).is_dir() for p in ("src/api-impl", "api-impl")))
    ps.append(_probe("atl-src", has_src, "no ATL source tree given" if not atl_src else f"{atl_src}/src/api-impl not found",
                     "pass --atl-src DIR or set ATL_DOCTOR_ATL_SRC"))
    ps.append(_probe("jvmti-plugin", jvmti_plugin(prefix) is not None, "libopenjdkjvmti.so not found in the prefix",
                     "build it with tools/build-jvmti-plugin.sh ART_TREE PREFIX"))
    for name, fname, hint in (("agent", AGENT_LIB, "build the C helpers (meson setup build agent && ninja -C build) or pass --agent"),
                              ("tracecount", TRACECOUNT, "build the C helpers (meson setup build agent && ninja -C build)"),
                              ("input-helper", INPUT_HELPER, "build the C helpers (meson setup build agent && ninja -C build)"),
                              ("wheel-shim", WHEEL_SHIM, "build the C helpers (meson setup build agent && ninja -C build)")):
        ps.append(_probe(name, find_helper(fname, None, prefix) is not None, f"{fname} not found", hint))
    ps.append(_probe("kwin", shutil.which("kwin_wayland") is not None, "kwin_wayland not in PATH",
                     "install KWin (needed for --headless)"))
    ps.append(_probe("spectacle", shutil.which("spectacle") is not None, "spectacle not in PATH",
                     "install Spectacle (needed for screenshots)"))
    ps.append(_probe("viewdump", has_viewdump(prefix), "ATLViewDump not found in the ATL jars",
                     "build ATL with the ATLViewDump patch (docs/atlviewdump.md)"))
    ps.append(_probe("aosp", bool(aosp and Path(aosp).is_dir() and any(Path(aosp).iterdir())),
                     "no AOSP tree given" if not aosp else f"{aosp} is not a directory with content",
                     "pass --aosp DIR or set ATL_DOCTOR_AOSP"))
    if android:
        have_adb = shutil.which("adb") is not None
        ps.append(_probe("adb", have_adb, "adb not in PATH", "install android-tools (adb)"))
        dev = adb_device() if have_adb else None
        ps.append(_probe("android-device", dev is not None, "adb not available" if not have_adb else "no device attached",
                         "connect a device or start an emulator and check `adb devices`"))
        root = bool(dev) and adb_is_root()
        ps.append(_probe("android-root", root, "no device" if not dev else "device is not rooted",
                         "use a rootable image (userdebug) and run `adb root`"))
        abi = adb_abi() if dev else None
        agent = find_helper(f"libatl_doctor_agent-android-{abi}.so", None, prefix) if abi else None
        ps.append(_probe("android-agent", agent is not None,
                         "no device" if not abi else f"libatl_doctor_agent-android-{abi}.so not found",
                         "build the Android agent with the NDK (see agent/meson.build)"))
    return ps


def format_probe(p: Probe) -> str:
    return f"{p.name:<16} available" if p.available else f"{p.name:<16} missing: {p.reason}; fix: {p.hint}"


def _run_check(args: argparse.Namespace) -> int:
    prefix = resolve(args.prefix, "ATL_DOCTOR_PREFIX")
    atl_src = resolve(args.atl_src, "ATL_DOCTOR_ATL_SRC")
    aosp = resolve(args.aosp, "ATL_DOCTOR_AOSP")
    probes = probe_all(Path(prefix) if prefix else None, Path(atl_src) if atl_src else None, args.android,
                       Path(aosp) if aosp else None)
    for p in probes:
        print(format_probe(p))
    if args.android:
        bad = [p for p in probes if p.name in ("adb", "android-device") and not p.available]
        if bad:
            raise PrerequisiteMissing(f"--android requested but {bad[0].name} is unavailable: {bad[0].reason}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import common_parser

    p = sub.add_parser("check", help="check the environment and say how to fix what is missing",
                       parents=[common_parser()])
    p.add_argument("--prefix", metavar="DIR", help="ATL installation prefix (default: $ATL_DOCTOR_PREFIX)")
    p.add_argument("--atl-src", metavar="DIR", help="ATL source tree (default: $ATL_DOCTOR_ATL_SRC)")
    p.add_argument("--aosp", metavar="DIR", help="AOSP source tree (default: $ATL_DOCTOR_AOSP)")
    p.add_argument("--android", action="store_true", help="also check adb and the Android device")
    p.set_defaults(func=_run_check)
