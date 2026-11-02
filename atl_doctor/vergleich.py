# SPDX-License-Identifier: GPL-3.0-or-later
"""`atl-doctor vergleich APK`: the same scenario under ATL and on Android, and a short report of the differences.

Records both sides (record and record --android), pairs them like `report DIR ANDROID-DIR` and writes OUT/bericht.md
(at most MAX_LINES lines, likely causes first) and OUT/bericht.json. Under ATL the recording is light by default
(no debuggable runtime, the app keeps its speed); --tief records with the full method trace and the agent from
the start, which forces the interpreter and changes the timing.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .errors import DoctorError, UsageError
from .findings import Klass
from .record_android import record_android
from .record_atl import RecordOptions, record_atl

MAX_LINES = 60
DEFAULT_SCENARIO = 'wait 15\nscreenshot "start"\n'
SHOT_BAD = 0.8  # a step whose screenshots are less similar than this is a cause candidate

# score of a cause candidate by kind; within a score the larger count comes first
SCORE = {"crash": 100, "exception": 90, "lifecycle": 88, "native": 85, "missing": 80, "exception-platform": 75,
         "stub-result": 60, "native-other": 55, "stub": 40, "shot": 30}
START_PENALTY = 10  # a candidate first seen while the app starts (phase 0, before the first mark) is often noise
CAPS = {"causes": 5, "stubs": 10, "exceptions": 6, "lifecycle": 4, "native": 5, "shots": 6}


def _atl_count(f) -> int:
    return sum(o.count for o in f.occurrences if o.side == "atl")


def _first_phase(f) -> str:
    atl = [o for o in f.occurrences if o.side == "atl"]
    return min(atl, key=lambda o: o.phase_index).phase if atl else ""


def _starts_early(f) -> bool:
    """True if the finding is first seen in phase 0 (the app starting, before the first mark)."""
    return min((o.phase_index for o in f.occurrences if o.side == "atl"), default=1) == 0


def build_comparison(atl_dir: Path, android_dir: Path, out_dir: Path, density: float | None = None,
                     no_aosp_fetch: bool = False) -> dict:
    """All sections of the comparison as plain data (also the JSON report)."""
    from .capture import ATL_LOG, STEPS_LOG, Capture
    from .report import apply_capture_aosp, build_pair_report, like_aosp
    from .sources.native import native_findings
    from .sources.shotdiff import compare_shots

    r, root = build_pair_report(Path(atl_dir), Path(android_dir), density)
    try:
        apply_capture_aosp(r, root, None, no_fetch=no_aosp_fetch)
    except DoctorError as e:
        r.environment["aosp"] = f"not compared: {e}"
    fs = r.findings

    stubs = sorted((f for f in fs if f.kind in ("stub", "stublog") and not like_aosp(f) and _atl_count(f)),
                   key=lambda f: (-_atl_count(f), f.key))
    stub_rows = [{"title": f.key.replace("#", "."), "count": _atl_count(f), "kinds": f.details.get("kinds", []),
                  "phase": _first_phase(f), "early": _starts_early(f),
                  "caller": (f.details.get("callers") or [""])[0], "result_used": f.klass == Klass.STUB_RESULT_USED,
                  "id": f.short_id} for f in stubs]
    exc_rows = [{"title": f.title, "count": _atl_count(f), "phase": _first_phase(f), "early": _starts_early(f),
                 "where": f.details.get("app_frame") or next((o.where for o in f.occurrences if o.where), ""),
                 "app": bool(f.details.get("app_frame")), "id": f.short_id}
                for f in sorted((f for f in fs if f.kind == "exc" and f.klass == Klass.EXC_ATL_ONLY),
                                key=lambda f: (-_atl_count(f), f.key))]
    crash_rows = [{"title": f.title, "count": _atl_count(f), "id": f.short_id}
                  for f in fs if f.kind == "crash" and not f.on_android and f.klass == Klass.CRASH and _atl_count(f)]
    lc_rows = [{"title": f.title, "phase": _first_phase(f), "early": _starts_early(f), "id": f.short_id}
               for f in fs if f.kind == "lc"]
    missing_rows = [{"title": f.title, "count": _atl_count(f), "id": f.short_id}
                    for f in fs if f.kind == "missing" and not f.on_android and _atl_count(f)]

    def log_of(d: Path) -> str:
        p = Path(d) / ATL_LOG
        return p.read_bytes().decode("utf-8", "replace") if p.is_file() else ""

    on_android = {f.key for f in native_findings(log_of(android_dir), "android")}
    native_rows = [{"key": f.key, "title": f.title, "count": f.total_count, "line": f.details["line"]}
                   for f in sorted(native_findings(log_of(atl_dir), "atl"), key=lambda f: (-f.total_count, f.key))
                   if f.key not in on_android]

    if density is None:
        density = float(Capture.open(android_dir).read_manifest().environment.get("density") or 1.0)
    shots = compare_shots(Path(atl_dir), Path(android_dir), density, Path(out_dir) / "shots")

    causes = [{"kind": "crash", "title": c["title"], "count": c["count"], "where": ""} for c in crash_rows]
    causes += [{"kind": "exception" if e["app"] else "exception-platform", "title": e["title"], "count": e["count"],
                "where": e["where"], "phase": e["phase"], "early": e["early"]} for e in exc_rows]
    causes += [{"kind": "native" if n["key"].split(":")[0] in ("dlopen", "symbol") else "native-other",
                "title": n["title"], "count": n["count"], "where": ""} for n in native_rows]
    causes += [{"kind": "missing", "title": m["title"], "count": m["count"], "where": ""} for m in missing_rows]
    causes += [{"kind": "lifecycle", "title": x["title"], "count": 1, "where": "", "early": x["early"]}
               for x in lc_rows]
    causes += [{"kind": "stub-result" if s["result_used"] else "stub", "title": s["title"], "count": s["count"],
                "where": s["caller"], "phase": s["phase"], "early": s["early"]} for s in stub_rows]
    causes += [{"kind": "shot", "title": f"step {s['step']}: similarity {s['similarity']}"
                + (f", {s['finding']}" if s["finding"] else ""), "count": 1, "where": ""}
               for s in shots if s["similarity"] < SHOT_BAD or s["finding"]]
    for c in causes:
        c.setdefault("phase", "")
        c["score"] = SCORE[c["kind"]] - (START_PENALTY if c.pop("early", False) else 0)
        c["kind"] = c["kind"].split("-")[0]
    causes.sort(key=lambda c: (-c["score"], -c["count"], c["title"]))

    unfinished = [side for side, d in (("ATL", atl_dir), ("Android", android_dir))
                  if (Path(d) / STEPS_LOG).is_file()
                  and Capture.open(d).read_manifest().environment.get("stopped_by") != "steps"]
    m = Capture.open(atl_dir).read_manifest()
    return {"unfinished": unfinished, "app": m.package or "", "atl": str(atl_dir), "android": str(android_dir),
            "mode": m.environment.get("trace", ""), "start_state": m.environment.get("start_state", ""),
            "causes": causes, "crashes": crash_rows, "stubs": stub_rows, "exceptions": exc_rows,
            "lifecycle": lc_rows, "native": native_rows, "missing": missing_rows, "shots": shots,
            "environment": r.environment}


def _t(title: str, n: int = 120) -> str:
    return title if len(title) <= n else title[:n - 3] + "..."


def render_markdown(d: dict) -> str:
    out = [f"# atl-doctor vergleich: {d.get('app') or '?'}",
           f"ATL `{d.get('atl')}` ({d.get('mode') or 'full trace'}) / Android `{d.get('android')}`"
           + (f", start state `{d['start_state']}`" if d.get("start_state") else "")]
    out += [f"Scenario stopped on {side}: {msg}" for side, msg in (d.get("errors") or {}).items()]
    out += [f"Scenario not finished on {side}: the app ended (or the recording was stopped)"
            for side in d.get("unfinished") or [] if side not in (d.get("errors") or {})]

    def section(title: str, key: str, line) -> None:
        rows = d.get(key) or []
        out.extend(["", f"## {title}"])
        out.extend(line(i, x) for i, x in enumerate(rows[:CAPS[key]], 1))
        if not rows:
            out.append("none")
        elif len(rows) > CAPS[key]:
            out.append(f"... {len(rows) - CAPS[key]} more in bericht.json")

    section("Likely causes", "causes", lambda i, c: f"{i}. [{c['kind']}] {_t(c['title'])}"
            + (f" ({c['count']}x)" if c["count"] > 1 else "") + (f" <- {c['where']}" if c["where"] else "")
            + (f" [{c['phase']}]" if c["phase"] else ""))
    section("Stubs hit", "stubs", lambda i, s: f"- {s['count']}x {_t(s['title'])}"
            + (" (result used)" if s["result_used"] else "") + (f" <- {s['caller']}" if s["caller"] else ""))
    section("Exceptions only on ATL", "exceptions", lambda i, e: f"- {e['count']}x {_t(e['title'])}"
            + (f" <- {e['where']}" if e["where"] else "") + (f" [{e['phase']}]" if e["phase"] else ""))
    section("Lifecycle differences", "lifecycle", lambda i, x: f"- {_t(x['title'])}")
    section("Missing native pieces", "native", lambda i, n: f"- {n['count']}x {_t(n['title'])}")
    section("Screenshots (worst first)", "shots", lambda i, s: f"- {s['step']}: similarity {s['similarity']}, "
            f"{round(s['different'] * 100)} % different" + (f", {s['finding']}" if s["finding"] else ""))
    return "\n".join(out[:MAX_LINES]) + "\n"


def _record(args: argparse.Namespace, out: Path, errors: dict[str, str]) -> tuple[Path, Path]:
    from .env import resolve
    from .cli import log

    if args.launcher and args.prefix:
        raise UsageError("--launcher and --prefix exclude each other")
    prefix = None if args.launcher else resolve(args.prefix, "ATL_DOCTOR_PREFIX")
    atl_src = resolve(args.atl_src, "ATL_DOCTOR_ATL_SRC")
    steps = Path(args.szenario) if args.szenario else None
    if steps is None:
        out.mkdir(parents=True, exist_ok=True)
        steps = out / "szenario.steps"
        steps.write_text(DEFAULT_SCENARIO, encoding="utf-8")
    atl, android = out / "atl", out / "android"
    for side, run in (("ATL", lambda: record_atl(RecordOptions(
            apk=Path(args.apk), out=atl, prefix=Path(prefix) if prefix else None, launcher=args.launcher,
            atl_src=Path(atl_src) if atl_src else None, agent=None, steps=steps, light=not args.tief,
            atl_args=list(args.atl_args)))),
                      ("Android", lambda: record_android(RecordOptions(
            apk=Path(args.apk), out=android, prefix=Path(prefix) if prefix else None, atl_src=None, agent=None,
            steps=steps, android=True)))):
        try:
            run()
        except DoctorError as e:  # a step that failed still leaves a capture worth comparing
            log(0, f"{side} recording: {e}")
            errors[side] = str(e)
    return atl, android


def _run(args: argparse.Namespace) -> int:
    out = Path(args.output or f"atl-doctor-vergleich-{datetime.now():%Y%m%d-%H%M%S}")
    if bool(args.atl_dir) != bool(args.android_dir):
        raise UsageError("--atl-dir and --android-dir go together")
    errors: dict[str, str] = {}
    if args.atl_dir:
        atl, android = Path(args.atl_dir), Path(args.android_dir)
    else:
        atl, android = _record(args, out, errors)
    d = build_comparison(atl, android, out, args.density, args.no_aosp_fetch)
    d["errors"] = errors
    out.mkdir(parents=True, exist_ok=True)
    (out / "bericht.json").write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    md = render_markdown(d)
    (out / "bericht.md").write_text(md, encoding="utf-8")
    print(md, end="")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import common_parser

    p = sub.add_parser("vergleich", parents=[common_parser()],
                       help="record the same scenario under ATL and on Android and report the differences")
    p.add_argument("-o", "--output", metavar="DIR", help="output directory (default: ./atl-doctor-vergleich-TIME)")
    p.add_argument("--szenario", metavar="FILE", help=f"step file for both sides (default: {DEFAULT_SCENARIO!r})")
    p.add_argument("--prefix", metavar="DIR", help="ATL installation prefix (default: $ATL_DOCTOR_PREFIX)")
    p.add_argument("--launcher", metavar="CMD", help="start ATL with CMD instead (e.g. a Flatpak), see record")
    p.add_argument("--atl-src", metavar="DIR", help="ATL source tree for the stub list (default: $ATL_DOCTOR_ATL_SRC)")
    p.add_argument("--tief", action="store_true",
                   help="full method trace and agent from the start (debuggable: interpreter, timing changes)")
    p.add_argument("--atl-dir", metavar="DIR", help="compare this ATL capture instead of recording")
    p.add_argument("--android-dir", metavar="DIR", help="compare this Android capture instead of recording")
    p.add_argument("--density", type=float, metavar="D", help="Android screen density (default: from the capture)")
    p.add_argument("--no-aosp-fetch", action="store_true", help="do not download AOSP source (see report)")
    p.add_argument("apk", metavar="APK")
    p.add_argument("atl_args", nargs="*", metavar="ATL-ARGS", help="after --: passed to android-translation-layer")
    p.set_defaults(func=_run)
