# SPDX-License-Identifier: GPL-3.0-or-later
"""`record --android`: the same app on an Android device or emulator over adb, as a reference capture."""
from __future__ import annotations

import os
import re
import select
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .capture import AGENT, ATL_LOG, CONTROL, MARKS, TREES, Capture, Mark
from .env import Probe, adb_device, find_helper, probe_all
from .errors import DoctorError, PrerequisiteMissing
from .record_atl import RecordOptions, _handle_control, _Stop, _warn, default_out_dir, tree_name

DEVICE_DIR = "/data/local/tmp/atl-doctor"
DEVICE_TREE = "/sdcard/atl-doctor.xml"
LOGCAT_RAW = "logcat.txt"
START_TIMEOUT_S = 60.0
ARM_AFTER_S = 5.0
POLL_S = 1.0
_LOGCAT_PID = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+(\d+)\s+\d+\s")


class Adb:
    def __init__(self, serial: str):
        self.serial = serial

    def run(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DoctorError(f"adb {' '.join(args)} timed out") from e

    def out(self, *args: str, timeout: float = 60) -> str:
        r = self.run(*args, timeout=timeout)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""

    def shell(self, *args: str, timeout: float = 60) -> str:
        return self.out("shell", *args, timeout=timeout)

    def boot_us(self) -> int | None:
        """The device's CLOCK_BOOTTIME (from /proc/uptime) in microseconds."""
        f = self.shell("cat", "/proc/uptime").split()
        try:
            return round(float(f[0]) * 1_000_000)
        except (IndexError, ValueError):
            return None

    def pidof(self, package: str) -> list[str]:
        return self.shell("pidof", package).split()


def density_of(wm_output: str) -> float | None:
    """`wm density`: the override if set, else the physical density, as a factor of 160 dpi."""
    vals = dict(re.findall(r"(Physical|Override) density:\s*(\d+)", wm_output))
    dpi = vals.get("Override") or vals.get("Physical")
    return int(dpi) / 160 if dpi else None


def installed_version_code(dumpsys: str) -> str | None:
    m = re.search(r"versionCode=(\d+)", dumpsys)
    return m.group(1) if m else None


def filter_logcat(text: str, pids: set[str]) -> str:
    """Only the lines of the app's processes (threadtime format); continuation lines follow their line."""
    out, keep = [], False
    for line in text.splitlines(keepends=True):
        m = _LOGCAT_PID.match(line)
        if m:
            keep = m.group(1) in pids
        if keep:
            out.append(line)
    return "".join(out)


def _android_agent(adb: Adb, prefix: Path | None, abi: str | None, rooted: bool) -> tuple[Path | None, Path | None, str]:
    """(agent library, wrap template, reason if unavailable)."""
    if not rooted:
        return None, None, "device is not rooted"
    name = f"libatl_doctor_agent-android-{abi}.so"
    lib = find_helper(name, None, prefix) if abi else None
    if lib is None:
        return None, None, f"{name} not found" if abi else "device ABI unknown"
    wrap = find_helper("android-wrap.sh", None, prefix) or Path(__file__).resolve().parent.parent / "agent/android-wrap.sh"
    if not wrap.is_file():
        return None, None, "android-wrap.sh not found"
    return lib, wrap, ""


def _install_agent(adb: Adb, package: str, lib: Path, wrap: Path) -> None:
    data = f"/data/data/{package}"
    opts = f",arm={data}/atl-doctor-armed,marks={data}/atl-doctor-marks.tsv"
    text = wrap.read_text(encoding="utf-8").replace("@OPTS@", opts).replace("@PACKAGE@", package)
    adb.shell("setenforce", "0")
    adb.shell("mkdir", "-p", DEVICE_DIR)
    adb.run("push", str(lib), f"{DEVICE_DIR}/agent.so")
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as f:
        f.write(text)
    try:
        adb.run("push", f.name, f"{DEVICE_DIR}/wrap.sh")
    finally:
        os.unlink(f.name)
    adb.shell("chmod", "755", DEVICE_DIR, f"{DEVICE_DIR}/wrap.sh")
    adb.shell("chmod", "644", f"{DEVICE_DIR}/agent.so")
    adb.shell("rm", "-f", f"{data}/atl-doctor-agent.tsv", f"{data}/atl-doctor-armed", f"{data}/atl-doctor-marks.tsv")
    adb.shell("setprop", f"wrap.{package}", f"{DEVICE_DIR}/wrap.sh")


def _tree(adb: Adb, cap: Capture, mark: Mark) -> None:
    out = cap.path(TREES) / f"{tree_name(mark)}.xml"
    adb.shell("uiautomator", "dump", DEVICE_TREE, timeout=30)
    if adb.run("pull", DEVICE_TREE, str(out)).returncode != 0 or not out.is_file():
        _warn(f"no view tree for mark {mark.index} ({mark.name}): uiautomator dump failed")


def record_android(opts: RecordOptions) -> int:
    from .sources.dex import read_apk

    serial = adb_device()
    if serial is None:
        raise PrerequisiteMissing("--android needs a device: no device attached (check `adb devices`)")
    adb = Adb(serial)
    info = read_apk(opts.apk)
    if not info.package:
        raise DoctorError(f"{opts.apk}: no package name in the manifest")
    pkg = info.package
    opts.apk = opts.apk.resolve()
    out = (opts.out or Path(f"{default_out_dir(datetime.now())}-android")).resolve()
    cap = Capture.create(out, "android")
    _warn(f"recording into {out}")

    adb.out("root")  # harmless where adbd cannot run as root
    adb.run("wait-for-device", timeout=30)
    rooted = adb.shell("id", "-u").strip() == "0"
    abi = adb.shell("getprop", "ro.product.cpu.abi").strip() or None
    sdk = adb.shell("getprop", "ro.build.version.sdk").strip()
    density = density_of(adb.shell("wm", "density"))
    probes = probe_all(opts.prefix, None, True, None)
    lib, wrap, why = _android_agent(adb, opts.prefix, abi, rooted)
    for i, p in enumerate(probes):
        if p.name == "android-agent":
            probes[i] = Probe("android-agent", True) if lib else Probe("android-agent", False, why, p.hint)
            if not lib:
                _warn(f"android-agent unavailable: {why}")

    if info.version_code is None or installed_version_code(adb.shell("dumpsys", "package", pkg)) != info.version_code:
        r = adb.run("install", "-r", str(opts.apk), timeout=300)
        if r.returncode != 0:
            raise DoctorError(f"adb install failed: {(r.stdout + r.stderr).decode('utf-8', 'replace').strip()}")

    m = cap.read_manifest()
    m.apk, m.package, m.version_name = str(opts.apk), pkg, info.version_name
    m.sdk_int = int(sdk) if sdk.isdigit() else None
    m.command = ["adb", "-s", serial, "shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"]
    m.probes = [asdict(p) for p in probes]
    m.environment = {"serial": serial, **({"abi": abi} if abi else {}),
                     **({"density": str(density)} if density else {})}
    boot = adb.boot_us()
    if boot is not None:  # phase 0 on the device's clock
        cap.path(MARKS).write_text("")
        cap.append_mark("start", boot)
    cap.write_manifest(m)
    cap.path(TREES).mkdir()

    adb.shell("am", "force-stop", pkg)
    if lib and wrap:
        _install_agent(adb, pkg, lib, wrap)
    os.mkfifo(cap.path(CONTROL))
    ctl = os.open(cap.path(CONTROL), os.O_RDONLY | os.O_NONBLOCK)
    keep = os.open(cap.path(CONTROL), os.O_WRONLY)
    adb.run("logcat", "-c")
    pids: set[str] = set()
    stop = _Stop()
    try:
        with stop, open(cap.path(LOGCAT_RAW), "wb") as raw:
            logcat = subprocess.Popen(["adb", "-s", serial, "logcat", "-v", "threadtime"], stdout=raw,
                                      stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            try:
                adb.run(*m.command[3:])
                started = time.monotonic()
                buf, last_poll, armed = bytearray(), 0.0, False
                while stop.by is None:
                    now = time.monotonic()
                    if now - last_poll >= POLL_S:
                        last_poll = now
                        running = adb.pidof(pkg)
                        pids |= set(running)
                        if not running and (pids or now - started > START_TIMEOUT_S):
                            break  # the app ended (or never started)
                        if lib and pids and not armed and now - started >= ARM_AFTER_S:
                            adb.shell("touch", f"/data/data/{pkg}/atl-doctor-armed")
                            armed = True
                    try:
                        ready, _, _ = select.select([ctl], [], [], 0.2)
                    except InterruptedError:
                        continue
                    if ready:
                        try:
                            data = os.read(ctl, 4096)
                        except BlockingIOError:
                            continue
                        for mk in _handle_control(cap, data, buf, lambda: adb.boot_us()):
                            if lib:
                                line = f"{mk.index}\t{mk.boot_us}\t{mk.wall_us}\t{mk.name}"
                                adb.shell(f"printf '%s\\n' {shlex.quote(line)} >> /data/data/{pkg}/atl-doctor-marks.tsv")
                            _tree(adb, cap, mk)
                if stop.by is not None:
                    _warn(f"{stop.by}: stopping the app")
                    adb.shell("am", "force-stop", pkg)
            finally:
                logcat.terminate()
                logcat.wait(10)
    finally:
        if lib:
            adb.shell("setprop", f"wrap.{pkg}", '""')
            adb.shell("setenforce", "1")
        os.close(keep)
        os.close(ctl)

    raw_text = cap.path(LOGCAT_RAW).read_bytes().decode("utf-8", "replace")
    cap.path(ATL_LOG).write_text(filter_logcat(raw_text, pids), encoding="utf-8")
    cap.path(LOGCAT_RAW).unlink()  # the whole device log grows fast; only the app's lines are kept
    if lib:
        r = adb.run("exec-out", "cat", f"/data/data/{pkg}/atl-doctor-agent.tsv")
        if r.returncode == 0:
            cap.path(AGENT).write_bytes(r.stdout)
    m.ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if stop.by is not None:
        m.environment["stopped_by"] = stop.by
    m.environment["pids"] = " ".join(sorted(pids))
    cap.write_manifest(m)
    _warn(f"done: {out}")
    return 0
