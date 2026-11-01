# SPDX-License-Identifier: GPL-3.0-or-later
"""Test double for adb.

State lives in $FAKE_ADB_DIR:
  calls.log     one line per call (arguments after an optional `-s SERIAL`, joined by spaces)
  table.json    {"PREFIX": [stdout, rc], ...}: the longest matching prefix of the joined arguments answers
  device/       files of the "device": `pull SRC DST` and `exec-out cat SRC` read device/<basename of SRC>;
                `shell pidof PKG` prints device/pidof if it exists
`logcat` without -c prints two lines and runs until terminated; unmatched calls succeed silently.
"""
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path


def main() -> int:
    d = Path(os.environ["FAKE_ADB_DIR"])
    args = sys.argv[1:]
    if args[:1] == ["-s"]:
        args = args[2:]
    line = " ".join(args)
    with open(d / "calls.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    dev = d / "device"
    if args[:1] == ["logcat"] and "-c" not in args:
        print("11-01 07:00:00.000  1234  1234 I ActivityManager: Start proc 1234:org.example.demo/u0a1", flush=True)
        print("11-01 07:00:00.100  999  999 E Other: java.lang.IllegalStateException: not the app", flush=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            time.sleep(0.05)
    if args[:2] == ["shell", "pidof"]:
        p = dev / "pidof"
        if p.exists():
            print(p.read_text().strip())
            return 0
        return 1
    if args[:1] == ["pull"]:
        src = dev / Path(args[1]).name
        if not src.exists():
            print(f"adb: error: remote object '{args[1]}' does not exist", file=sys.stderr)
            return 1
        shutil.copy(src, args[2])
        return 0
    if args[:2] == ["exec-out", "cat"]:
        src = dev / Path(args[2]).name
        if src.exists():
            sys.stdout.buffer.write(src.read_bytes())
            return 0
        return 1
    table = json.loads((d / "table.json").read_text()) if (d / "table.json").exists() else {}
    best = max((k for k in table if line.startswith(k)), key=len, default=None)
    if best is None:
        return 0
    out, rc = table[best]
    sys.stdout.write(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
