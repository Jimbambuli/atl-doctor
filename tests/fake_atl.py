# SPDX-License-Identifier: GPL-3.0-or-later
"""Test double for android-translation-layer.

fake_atl.py APK [--exit-code N] [--signal SIG] [--wait] [--trace] [--viewdump] [-X OPT ...]
Prints a few log lines; --wait runs until SIGTERM; --trace writes a small method trace to the
-Xmethod-trace-file: path (one app call of Drawable.parseBlendMode); --viewdump answers ATLViewDump requests
in $ATL_VIEWDUMP while waiting; ends with exit code N or dies by SIG.
"""
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    args = sys.argv[1:]
    print("hello from fake atl", flush=True)
    print("args: " + " ".join(args), flush=True)
    opt = {a for a in args if a.startswith("--")}
    trace = next((a.split(":", 1)[1] for a in args if a.startswith("-Xmethod-trace-file:")), None)
    if "--trace" in opt and trace:
        from tracegen import ENTER, EXIT, make_trace
        stub = (1, "android.graphics.drawable.Drawable", "parseBlendMode",
                "(ILandroid/graphics/BlendMode;)Landroid/graphics/BlendMode;")
        app = (2, "org.example.demo.ImportTask", "run", "()V")
        make_trace(Path(trace), 0xF3, time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000, [stub, app],
                   [(1, 2, ENTER, 0), (1, 1, ENTER, 1), (1, 1, EXIT, 2), (1, 2, EXIT, 3)])
    if "--wait" in opt:
        signal.signal(signal.SIGTERM, lambda *_: (print("fake atl: SIGTERM", flush=True), sys.exit(0)))
        dump = os.environ.get("ATL_VIEWDUMP") if "--viewdump" in opt else None
        while True:
            if dump and os.path.exists(os.path.join(dump, "anfrage")):
                req = os.path.join(dump, "anfrage")
                name = open(req, encoding="utf-8").read().strip()
                with open(os.path.join(dump, name + ".xml"), "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0"?>\n<hierarchy rotation="0" atl="1"></hierarchy>\n')
                os.unlink(req)
            time.sleep(0.05)
    if "--signal" in opt:
        print("Fatal signal 11 (SIGSEGV), code 1", flush=True)
        os.kill(os.getpid(), getattr(signal, args[args.index("--signal") + 1]))
    code = int(args[args.index("--exit-code") + 1]) if "--exit-code" in opt else 0
    sys.exit(code)


if __name__ == "__main__":
    main()
