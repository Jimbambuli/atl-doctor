# SPDX-License-Identifier: GPL-3.0-or-later
"""Report model, text rendering and the `report` subcommand."""
from __future__ import annotations

import argparse
import base64
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .env import Probe, resolve
from .errors import EXIT_FOUND, DoctorError, UsageError
from .sources.harmless import is_harmless, mark_harmless
from .sources.perf import JANK_MIN_FRAMES
from .findings import FIX_FIRST_MAX, RANK, Finding, Klass, from_dict, rank, to_dict

REPORT_FORMAT = 1
RANKING_RULE = "class rank, minus 5 if also on Android; then count desc, earliest phase, id"


@dataclass
class Report:
    mode: str  # "record" | "log" | "scan"
    environment: dict[str, str]
    probes: list[Probe]
    phases: list[str]
    findings: list[Finding]
    extras: dict[str, Any] = field(default_factory=dict)


def _kind_in(*kinds: str) -> Callable[[Finding], bool]:
    return lambda f: f.kind in kinds


SECTIONS: list[tuple[str, Callable[[Finding], bool]]] = [
    ("Environment", lambda f: False),
    ("Fix first", lambda f: False),
    ("Hit stubs", lambda f: f.kind in ("stub", "stublog") and not like_aosp(f)),
    ("Missing methods/classes", _kind_in("missing")),
    ("Exceptions", _kind_in("exc")),
    ("Crashes", _kind_in("crash")),
    ("Exit cause", _kind_in("exit")),
    ("System answers differ", _kind_in("sys")),
    ("Broadcasts registered, never delivered", _kind_in("bcast")),
    ("View tree differences", _kind_in("tree")),
    ("Rendering defects", _kind_in("render")),
    ("Lifecycle differences", _kind_in("lc")),
    ("Jank and hangs", _kind_in("jank", "hang", "gc")),
    ("Memory growth", _kind_in("mem")),
]


def like_aosp(f: Finding) -> bool:
    """True for a stub hit that is as trivial in AOSP (after apply_aosp): not counted as an ATL gap."""
    return bool(f.details.get("like_aosp"))


def finding_lines(f: Finding) -> list[str]:
    first = min(f.occurrences, key=lambda o: o.phase_index).phase if f.occurrences else "-"
    lines = [f"{f.short_id}  {f.weight:>3}  {f.title}  [{f.total_count}x, first: {first}]"]
    if f.suggestion:
        lines.append(f"    -> {f.suggestion}")
    if f.kind == "exit":
        d = f.details
        if d.get("stack"):
            lines += ["    stack:"] + [f"      {fr}" for fr in d["stack"]]
        if d.get("recent_exceptions"):
            lines.append("    last exceptions before it:")
            lines += [f"      {x['type']} on {x['thread']}, {'uncaught' if x['uncaught'] else 'caught'}"
                      f"{', first app frame ' + x['app_frame'] if x['app_frame'] else ''}" for x in d["recent_exceptions"]]
        if d.get("process_exit_status") is not None or d.get("process_signal"):
            lines.append("    process: " + ", ".join(
                x for x in (f"exit {d['process_exit_status']}" if d.get("process_exit_status") is not None else "",
                            f"signal {d['process_signal']}" if d.get("process_signal") else "") if x))
    also = f.details.get("also_missing_in_class")
    if also:
        lines.append("    also missing in this class: " + ", ".join(also))
    from .sources.aosp import aosp_line

    a = aosp_line(f)
    if a:
        lines.append(f"    {a}")
    return lines


def _exception_side(f: Finding) -> str:
    sides = {o.side for o in f.occurrences}
    if sides == {"android"}:
        return "only Android"
    if "android" in sides or f.on_android:
        return "both"
    return "only ATL"


def _side_count(f: Finding, side: str) -> int:
    return sum(o.count for o in f.occurrences if o.side == side)


def exception_groups(ranked: list[Finding]) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Caught exceptions as (shown in full, also on Android with comparable frequency, known harmless)."""
    exc = [f for f in ranked if f.kind == "exc"]
    harmless = [f for f in exc if is_harmless(f)]
    rest = [f for f in exc if not is_harmless(f)]
    android = [f for f in rest if _exception_side(f) == "both" and f.klass is Klass.OTHER]
    return [f for f in rest if f not in android], android, harmless


def _harmless_rules(harmless: list[Finding]) -> list[tuple[dict, int, int]]:
    """Per rule: (entry, findings, ATL events), most events first."""
    by: dict[str, list[Finding]] = {}
    for f in harmless:
        by.setdefault(f.details["known_harmless"]["id"], []).append(f)
    out = [(fs[0].details["known_harmless"], len(fs), sum(_side_count(f, "atl") or f.total_count for f in fs)) for fs in by.values()]
    return sorted(out, key=lambda x: (-x[2], x[0]["id"]))


def _noise_lines(android: list[Finding], harmless: list[Finding]) -> list[str]:
    lines: list[str] = []
    if android:
        n = sum(_side_count(f, "atl") for f in android)
        lines.append(f"also on Android (comparable frequency, weight 0): {len(android)} findings, {n} events on ATL")
        lines += [f"  {f.short_id}  {f.title}  [ATL {_side_count(f, 'atl')}x / Android {_side_count(f, 'android')}x]"
                  for f in android]
    if harmless:
        rules = _harmless_rules(harmless)
        lines.append(f"known harmless: {sum(c for _, _, c in rules)} events in {len(harmless)} findings ("
                     + ", ".join(f"{k['id']} {c}x" for k, _, c in rules) + "); reasons and sources: HTML report, JSON details.known_harmless")
    return lines


def _environment_lines(r: Report) -> list[str]:
    lines = [f"mode: {r.mode}"]
    if r.phases:
        lines.append("phases: " + ", ".join(r.phases))
    lines += [f"{k}: {v}" for k, v in r.environment.items()]
    for p in r.probes:
        lines.append(f"{p.name}: available" if p.available else f"{p.name}: unavailable ({p.reason})")
    return lines


def _coverage_lines(r: Report, state: str) -> list[str]:
    lines = []
    for a in r.extras.get("apps", {}).get(state, []):
        lines.append(
            f"{a['package']}  {a['coverage_pct']:.1f}%  {a['coverage_incl_stub_pct']:.1f}%  stubs {a['stub']}  "
            f"missing {a['missing_member'] + a['missing_class']}  target {a['target_sdk'] or '-'}/sdk_int {a['sdk_int']}"
            + (f"  [{'; '.join(a['exclusions'])}]" if a["exclusions"] else ""))
    return lines


def _coverage_sections(r: Report) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    states = list(r.extras.get("apps", {}))
    head = "package  coverage  incl. stubs  stubs  missing  target/sdk_int  exclusions"
    for st in states:
        lines = _coverage_lines(r, st)
        if lines:
            out.append((f"Coverage ({st})" if len(states) > 1 else "Coverage", [head] + lines))
    return out


def _notes(r: Report) -> list[str]:
    extra = [f"skipped: {s['apk']} ({s['reason']})" for s in r.extras.get("skipped", [])]
    hist = r.extras.get("target_sdk_histogram", {})
    if hist:
        extra.append("target SDK: " + ", ".join(f"{k}: {v}" for k, v in hist.items()))
    like = [f for f in rank(r.findings) if like_aosp(f)]
    if like:
        names = ", ".join(f.key for f in like[:20]) + (" ..." if len(like) > 20 else "")
        extra.append(f"{len(like)} stub hit{'s' if len(like) > 1 else ''} behave like AOSP (trivial there as well, "
                     f"not counted as stubs; details.like_aosp in json): {names}")
    return extra + r.extras.get("limits", [])


def _na(v: object, unit: str = "") -> str:
    return "n/a" if v is None else f"{v:g}{unit}"


def _cell(row: dict, field: str, unit: str = "") -> str:
    """ATL value, or ATL / Android for a pair."""
    sides = [_na(row[s][field], unit) for s in ("atl", "android") if row.get(s) is not None]
    return " / ".join(sides) if sides else "n/a"


def _steps_header(steps: list[dict]) -> str:
    sides = "ATL / Android" if any("android" in x and "atl" in x for x in steps) else "ATL" if any(
        "atl" in x for x in steps) else "Android"
    return sides


def _step_lines(steps: list[dict]) -> list[str]:
    head = _steps_header(steps)
    w = max(len(x["step"]) for x in steps)
    out = [f"{'#':>3} {'line':>4}  {'step':<{w}}  frames ({head})  scroll y ({head}, dp)  scroll x ({head}, dp)"]
    for x in steps:
        out.append(f"{x['n']:>3} {x['line']:>4}  {x['step']:<{w}}  {_cell(x, 'frames')}  {_cell(x, 'scroll_y_dp')}"
                   f"  {_cell(x, 'scroll_x_dp')}")
    return out


def render_text(r: Report) -> str:
    ranked = rank(r.findings)
    out: list[str] = []

    def section(title: str, lines: list[str]) -> None:
        if lines:
            out.append(title)
            out.extend("  " + line for line in lines)
            out.append("")

    for title, pred in SECTIONS:
        if title == "Environment":
            section(title, _environment_lines(r))
            if r.mode == "scan":
                for t, lines in _coverage_sections(r):
                    section(t, lines)
        elif title == "Fix first":
            top = [f for f in ranked if f.klass is not Klass.OTHER][:FIX_FIRST_MAX]
            section(title, [ln for f in top for ln in finding_lines(f)])
        elif title == "Exceptions":
            lines: list[str] = []
            shown, android, harmless = exception_groups(ranked)
            for side in ("only ATL", "both", "only Android"):
                group = [f for f in shown if _exception_side(f) == side]
                if group:
                    lines.append(side)
                    lines += ["  " + ln for f in group for ln in finding_lines(f)]
            section(title, lines + _noise_lines(android, harmless))
        else:
            section(title, [ln for f in ranked if pred(f) for ln in finding_lines(f)])
    if r.extras.get("steps"):
        section("Steps", _step_lines(r.extras["steps"]))
    if r.extras.get("replay"):
        from .bedienung import replay_lines

        section("Replayed input (ATL / Android)", replay_lines(r.extras["replay"]))
    if r.extras.get("timeline"):
        from .sources.timeline import timeline_lines

        section("Time line (busiest methods per phase, self time)", timeline_lines(r.extras["timeline"]))
    section("Notes", _notes(r))
    return "\n".join(out)


def render_json(r: Report) -> str:
    doc = {
        "format": REPORT_FORMAT, "tool": "atl-doctor", "tool_version": __version__, "mode": r.mode,
        "environment": r.environment,
        "probes": [{"name": p.name, "available": p.available, "reason": p.reason, "hint": p.hint} for p in r.probes],
        "phases": r.phases,
        "ranking": {"rule": RANKING_RULE, "ranks": {str(k): v for k, v in RANK.items()}},
        "findings": [to_dict(f) for f in rank(r.findings)],
        "extras": r.extras,
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_json(text: str) -> Report:
    try:
        d = json.loads(text)
        fmt = d["format"]
    except (ValueError, KeyError, TypeError) as e:
        raise DoctorError("not an atl-doctor JSON report") from e
    if not isinstance(fmt, int) or fmt > REPORT_FORMAT:
        raise DoctorError(f"report format {fmt} is newer than supported ({REPORT_FORMAT})")
    return Report(d["mode"], d.get("environment", {}),
                  [Probe(p["name"], p["available"], p.get("reason", ""), p.get("hint", "")) for p in d.get("probes", [])],
                  d.get("phases", []), [from_dict(x) for x in d.get("findings", [])], d.get("extras", {}))


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d2127;--mute:#5f6b7a;--line:#d5dae1;--card:#f5f7fa;--accent:#0b5cad}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e4e8ed;--mute:#9aa6b5;--line:#2c333b;--card:#1c2127;--accent:#6cb2ff}}
body{margin:0 auto;max-width:60rem;padding:1rem;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:1.6rem;border-bottom:1px solid var(--line)}
details{background:var(--card);border:1px solid var(--line);border-radius:6px;margin:.4rem 0;padding:.3rem .7rem}
summary{cursor:pointer}code,pre{font:13px/1.4 ui-monospace,monospace}pre{overflow-x:auto;margin:.4rem 0}
.w{display:inline-block;min-width:2.5rem;text-align:right;color:var(--accent);font-weight:600}
table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:.15rem .5rem;text-align:left}
.m{color:var(--mute)}img{max-width:100%;border:1px solid var(--line)}
"""


def _e(x: object) -> str:
    return html.escape(str(x), quote=True)


def _image_tags(f: Finding, capture_dir: Path | None) -> list[str]:
    tags = []
    crops = f.details.get("crops") or {}
    rels = [crops[k] for k in ("atl", "android") if k in crops]
    for rel in rels if capture_dir else []:
        try:
            data = (capture_dir / rel).read_bytes()
        except OSError:
            continue
        label = {crops.get("atl"): "ATL", crops.get("android"): "Android"}.get(rel)
        tag = f'<img alt="{_e(rel)}" src="data:image/png;base64,{base64.b64encode(data).decode()}">'
        tags.append(f'<figure style="display:inline-block;margin:.4rem"><figcaption class="m">{label}</figcaption>{tag}'
                    f'</figure>' if label else tag)
    return tags


def _html_finding(f: Finding, capture_dir: Path | None) -> str:
    occ = "".join(f"<li>{_e(o.phase)} ({_e(o.side)}): {o.count}x{' - ' + _e(o.where) if o.where else ''}</li>"
                  for o in f.occurrences)
    from .sources.aosp import aosp_line

    sugg = f"<p>{_e(f.suggestion)}</p>" if f.suggestion else ""
    a = aosp_line(f)
    sugg += f"<p>{_e(a)}</p>" if a else ""
    det = f"<pre>{_e(json.dumps(f.details, indent=2, sort_keys=True, ensure_ascii=False))}</pre>" if f.details else ""
    return (f'<details id="{_e(f.short_id)}"><summary><span class="w">{f.weight}</span> {_e(f.title)} '
            f'<span class="m">{_e(f.short_id)} [{f.total_count}x]</span></summary>'
            f'{sugg}<ul>{occ}</ul>{det}{"".join(_image_tags(f, capture_dir))}</details>')


def _step_table_html(steps: list[dict]) -> str:
    head = _steps_header(steps)
    rows = "".join(f"<tr><td>{x['n']}</td><td>{x['line']}</td><td>{_e(x['step'])}</td><td>{_e(_cell(x, 'frames'))}</td>"
                   f"<td>{_e(_cell(x, 'scroll_y_dp'))}</td><td>{_e(_cell(x, 'scroll_x_dp'))}</td></tr>" for x in steps)
    return (f"<table><thead><tr><th>#</th><th>line</th><th>step</th><th>frames ({head})</th>"
            f"<th>scroll y ({head}, dp)</th><th>scroll x ({head}, dp)</th></tr></thead><tbody>{rows}</tbody></table>")


def render_html(r: Report, capture_dir: Path | None = None) -> str:
    ranked = rank(r.findings)
    body = ["<h1>atl-doctor report</h1>"]

    def section(title: str, inner: str) -> None:
        if inner:
            body.append(f"<h2>{_e(title)}</h2>{inner}")

    for title, pred in SECTIONS:
        if title == "Environment":
            section(title, "<pre>" + _e("\n".join(_environment_lines(r))) + "</pre>")
            if r.mode == "scan":
                for t, lines in _coverage_sections(r):
                    section(t, "<pre>" + _e("\n".join(lines)) + "</pre>")
        elif title == "Fix first":
            top = [f for f in ranked if f.klass is not Klass.OTHER][:FIX_FIRST_MAX]
            section(title, "<ol>" + "".join(
                f'<li><a href="#{_e(f.short_id)}">{_e(f.title)}</a> <span class="m">{f.weight}, {_e(f.short_id)}</span></li>'
                for f in top) + "</ol>" if top else "")
        elif title == "Exceptions":
            shown, android, harmless = exception_groups(ranked)
            inner = "".join(_html_finding(f, capture_dir) for f in shown)
            if android:
                inner += (f"<details><summary>Also on Android (comparable frequency, weight 0): {len(android)} findings</summary>"
                          + "".join(_html_finding(f, capture_dir) for f in android) + "</details>")
            if harmless:
                rules = "".join(f'<li><code>{_e(k["id"])}</code> {c}x in {nf} findings: {_e(k["reason"])} '
                                f'<span class="m">{_e(k["source"])}</span></li>' for k, nf, c in _harmless_rules(harmless))
                inner += (f"<details><summary>Known harmless: {len(harmless)} findings</summary><ul>{rules}</ul>"
                          + "".join(_html_finding(f, capture_dir) for f in harmless) + "</details>")
            section(title, inner)
        else:
            section(title, "".join(_html_finding(f, capture_dir) for f in ranked if pred(f)))
    if r.extras.get("steps"):
        section("Steps", _step_table_html(r.extras["steps"]))
    if r.extras.get("replay"):
        from .bedienung import replay_html

        section("Replayed input (ATL / Android)", replay_html(r.extras["replay"], _e))
    if r.extras.get("timeline"):
        from .sources.timeline import timeline_lines

        section("Time line", "<pre>" + _e("\n".join(timeline_lines(r.extras["timeline"]))) + "</pre>")
    notes = _notes(r)
    section("Notes", "<ul>" + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul>" if notes else "")
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>atl-doctor report</title>'
            f"<style>{_CSS}</style></head><body>\n" + "\n".join(body) + "\n</body></html>\n")


def _parse_fail_on(value: str | None) -> set[Klass]:
    if not value:
        return set()
    try:
        return {Klass(v) for v in value.split(",") if v}
    except ValueError as e:
        raise UsageError(f"--fail-on: {e}; valid classes: {', '.join(k.value for k in Klass)}") from e


def _emit(args: argparse.Namespace, r: Report, capture_dir: Path | None) -> int:
    fail = _parse_fail_on(args.fail_on)
    if args.format == "html" and not args.output:
        raise UsageError("-f html needs -o FILE")
    text = {"text": lambda: render_text(r), "json": lambda: render_json(r),
            "html": lambda: render_html(r, capture_dir)}[args.format]()
    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except OSError as e:
            raise DoctorError(f"cannot write {args.output}: {e.strerror or e}") from e
    else:
        sys.stdout.write(text)
    return EXIT_FOUND if any(f.klass in fail for f in r.findings) else 0


ENV_KEYS = ("package", "version_name", "version_code", "atl_commit", "art_commit", "apk", "started", "args",
            "exit", "signal", "duration", "agent")


def _run_report(args: argparse.Namespace) -> int:
    if not args.log and not args.dirs:
        raise UsageError("report needs --log FILE or a capture directory")
    if args.log and args.dirs:
        raise UsageError("--log and a capture directory exclude each other")
    if not args.log:
        return _report_capture(args)
    atl_src = resolve(args.atl_src, "ATL_DOCTOR_ATL_SRC")
    from .sources.stubs import build_stub_list, stub_index

    entries = build_stub_list(Path(atl_src), []) if atl_src else []
    r = build_log_report([Path(p) for p in args.log], entries if atl_src else None)
    if (needs_aosp(r) and atl_src) or resolve(args.aosp, "ATL_DOCTOR_AOSP"):  # no stub list: nothing to compare
        apply_aosp(r, args.aosp, None, stub_index(entries), no_fetch=args.no_aosp_fetch)
    return _emit(args, r, None)


def build_log_report(paths: list[Path], stubs: list | None = None) -> Report:
    """Report of one or more logs (one phase each); stubs: a stub list to match logged stub messages against."""
    from .sources.log import parse_logs
    from .sources.stubs import match_log_stubs

    res = parse_logs(paths)
    findings = match_log_stubs(res.findings, stubs) if stubs is not None else res.findings
    env = {k: res.header[k] for k in ENV_KEYS if k in res.header}
    return Report("log", env, [], res.phases, findings)


def needs_aosp(r: Report) -> bool:
    return any(f.kind in ("stub", "missing") and "#" in f.key for f in r.findings)


def _prefix_world(prefix: str | None):
    """Class world of the ATL jars under prefix (or $ATL_DOCTOR_PREFIX); None if there are none."""
    from .env import atl_jars
    from .sources.dex import World

    jars = atl_jars(Path(prefix)) if prefix else []
    if not jars and resolve(None, "ATL_DOCTOR_PREFIX"):
        jars = atl_jars(Path(resolve(None, "ATL_DOCTOR_PREFIX")))
    return World.from_jars(jars) if jars else None


def apply_aosp(r: Report, aosp: str | None, prefix: str | None, stubs: dict, world=None, no_fetch: bool = False) -> None:
    """AOSP comparison for the stub/missing findings of r; the detected version goes into the environment.

    Always against real AOSP source: --aosp DIR, else the cached or freshly fetched tree (sources/aosp_fetch.py).
    If neither is possible the report says so and the stub hits stay unfiltered."""
    from .sources.aosp import attach_aosp, open_aosp
    from .sources.aosp_fetch import DEFAULT_SDK, FetchError, ensure_aosp

    path = resolve(aosp, "ATL_DOCTOR_AOSP")
    if not path:
        why = "--no-aosp-fetch given" if no_fetch else ""
        if not no_fetch:
            try:
                path = str(ensure_aosp(DEFAULT_SDK).path)
            except FetchError as e:
                why = str(e)
        if not path:
            r.environment["aosp"] = (f"AOSP comparison not possible ({why}; use --aosp DIR); "
                                     "stub hits unfiltered")
            return
    tree = open_aosp(Path(path))
    world = world or _prefix_world(prefix)
    attach_aosp(r.findings, tree, world, stubs)
    r.environment["aosp"] = str(path)
    r.environment["aosp_sdk"] = str(tree.sdk) if tree.sdk else "unknown"
    if world is None:
        r.environment["aosp_deps"] = "not checked against ATL (no ATL jars; recorded prefix or $ATL_DOCTOR_PREFIX)"


def apply_capture_aosp(r: Report, root: Path, aosp: str | None = None, no_fetch: bool = False) -> None:
    """AOSP comparison for a capture report (build_capture_report), like `atl-doctor report DIR`: --aosp DIR or
    $ATL_DOCTOR_AOSP, else the cached or fetched AOSP source; without both r.environment["aosp"] says why."""
    from .capture import TRACE_DIR, Capture
    from .sources.stubs import load_stub_tsv, stub_index

    if not needs_aosp(r) and not resolve(aosp, "ATL_DOCTOR_AOSP"):
        return
    cap = Capture.open(root)
    tsv = cap.path(TRACE_DIR) / "stubs.tsv"
    stubs = stub_index(load_stub_tsv(tsv)) if tsv.is_file() else {}
    apply_aosp(r, aosp, cap.read_manifest().environment.get("prefix"), stubs, no_fetch=no_fetch)


def build_capture_report(path: Path, jank_min_frames: int = JANK_MIN_FRAMES) -> tuple[Report, Path]:
    from .capture import AGENT, TRACE_DIR, TREES, Capture
    from .sources import load_capture_findings
    from .sources.exceptions import agent_problems, parse_agent
    from .env import atl_jars
    from .sources.dex import PARSE_ERRORS, framework_result_uses, read_apk, result_used_refs
    from .sources.stubs import load_stub_tsv

    cap = Capture.open(path)
    m = cap.read_manifest()
    stubs = cap.path(TRACE_DIR) / "stubs.tsv"
    entries = load_stub_tsv(stubs) if stubs.is_file() else None
    env = {k: v for k, v in (("package", m.package), ("version_name", m.version_name), ("apk", m.apk),
                             ("started", m.started), ("ended", m.ended), ("signal", m.signal)) if v}
    if m.exit_status is not None:
        env["exit"] = str(m.exit_status)
    if m.side == "atl":
        from .sources.windowclose import DETAIL_KEY, KEY, details, read, summary

        wc = read(m.environment)
        env[KEY] = summary(wc)
        if details(wc):
            env[DETAIL_KEY] = details(wc)
    if m.environment.get("trace"):
        env["trace"] = m.environment["trace"]
    if m.environment.get("tracecount"):
        env["tracecount"] = m.environment["tracecount"]
    used: set[tuple[str, str]] = set()
    if entries is not None:
        try:
            used = result_used_refs(read_apk(Path(m.apk)))
        except (DoctorError, *PARSE_ERRORS) as e:
            env["apk"] = f"{m.apk} (unreadable, stub results not checked: {e})"
        prefix = m.environment.get("prefix")
        try:
            used |= framework_result_uses(atl_jars(Path(prefix)) if prefix else [])  # ATL's own callers count too
        except (DoctorError, *PARSE_ERRORS) as e:
            env["atl_jars"] = f"unreadable, ATL's own use of stub results not checked: {e}"
    if cap.path(AGENT).is_file():
        problems = agent_problems(parse_agent(cap.path(AGENT)))
        if problems:
            env["agent"] = "; ".join(problems)
    notes: list[str] = []
    phases, findings = load_capture_findings(cap, entries, used, jank_min_frames, notes)
    if m.side == "atl" and any(cap.path(TREES).glob("*.xml")):
        from .sources.render import NOT_DETERMINABLE

        notes.append(NOT_DETERMINABLE)
    probes = [Probe(p["name"], p["available"], p.get("reason", ""), p.get("hint", "")) for p in m.probes]
    r = Report("record", env, probes, phases, mark_harmless(findings))
    if notes:
        r.extras["limits"] = notes
    from .sources.stepstats import step_table

    steps = step_table(cap)
    if steps:
        r.extras["steps"] = steps
    from .sources.timeline import read_timeline

    timeline = read_timeline(cap.path(TRACE_DIR), cap.read_marks())
    if timeline:
        r.extras["timeline"] = timeline
    return r, cap.root


def build_pair_report(atl_dir: Path, android_dir: Path, density: float | None,
                      jank_min_frames: int = JANK_MIN_FRAMES) -> tuple[Report, Path]:
    """ATL capture against an Android capture: phases and view trees paired by mark name."""
    from .capture import AGENT, TREES, Capture
    from .record_atl import tree_name
    from .sources import load_capture_findings
    from .sources.exceptions import phases_by_name, compare_sides, lifecycle_findings, lifecycle_sequence, parse_agent
    from .sources.memory import growth, read_memory
    from .sources.perf import android_phase_frames, frame_stats
    from .sources.shots import attach_crops
    from .sources.tree import load_tree, tree_findings

    r, root = build_capture_report(atl_dir, jank_min_frames)
    acap = Capture.open(android_dir)
    am = acap.read_manifest()
    if Capture.open(atl_dir).read_manifest().side != "atl" or am.side != "android":
        raise UsageError("report DIR ANDROID-DIR: the first must be an ATL capture, the second one from record --android")
    _, android = load_capture_findings(acap, None, set())
    findings = mark_harmless(compare_sides(r.findings, android))
    from .sources.sysanswers import annotate_broadcasts, answer_findings

    notes: list[str] = []
    and_events = parse_agent(acap.path(AGENT)) if acap.path(AGENT).is_file() else []
    atl_events = parse_agent(root.joinpath(AGENT)) if root.joinpath(AGENT).is_file() else []
    annotate_broadcasts(findings, and_events)
    findings += answer_findings(atl_events, and_events, Capture(root).read_marks(), notes)
    if notes:
        r.extras["limits"] = r.extras.get("limits", []) + notes
    from .sources.render import android_ids

    same = android_ids(acap)   # a rendering defect that Android shows as well is not an ATL defect
    findings = [f for f in findings if not (f.kind == "render" and f.id in same)]
    atl_marks, and_marks = Capture(root).read_marks(), acap.read_marks()
    atl_names, and_names = [k.name for k in atl_marks], [k.name for k in and_marks]
    if root.joinpath(AGENT).is_file() and acap.path(AGENT).is_file():
        findings += lifecycle_findings(lifecycle_sequence(parse_agent(root / AGENT), atl_marks),
                                       lifecycle_sequence(parse_agent(acap.path(AGENT)), and_marks), atl_names, and_names)
    if density is None and am.environment.get("density"):
        density = float(am.environment["density"])
    trees: dict[str, Finding] = {}
    pairs = phases_by_name(and_names)
    for key, i in phases_by_name(atl_names).items():
        j = pairs.get(key)
        if j is None:
            continue
        ta = root / TREES / f"{tree_name(atl_marks[i])}.xml"
        tb = acap.path(TREES) / f"{tree_name(and_marks[j])}.xml"
        if not (ta.is_file() and tb.is_file()):
            continue
        for f in tree_findings(load_tree(tb, "android", density), load_tree(ta, "atl"), atl_marks[i]):
            if f.id in trees:
                trees[f.id].occurrences += f.occurrences
            else:
                trees[f.id] = f
    findings += trees.values()
    attach_crops(trees.values(), Capture(root), acap, density)
    and_frames = android_phase_frames(acap)
    atl_to_android = {i: pairs[k] for k, i in phases_by_name(atl_names).items() if k in pairs}
    for f in findings:  # Android frame times as comparison values of the ATL jank findings
        if f.kind == "jank":
            j = atl_to_android.get(f.occurrences[0].phase_index)
            f.details["android"] = frame_stats(and_frames[j]) if j in and_frames else None
    and_mem = read_memory(acap)
    for f in findings:  # Android memory of the same phase and metric as comparison values
        if f.kind == "mem":
            phase, metric = f.key.rsplit(":", 1)
            vals = [v for _, v in and_mem.get(metric, {}).get(phase, [])]
            f.details["android"] = {"values_mb": [round(v, 1) for v in vals],
                                    "growth_mb": round(growth(vals), 1)} if vals else None
    r.findings = findings
    from .sources.stepstats import step_table

    steps = step_table(Capture(root), acap)
    if steps:
        r.extras["steps"] = steps
    from .bedienung import replay_table

    replay = replay_table(Capture(root), acap, density)
    if replay:
        r.extras["replay"] = replay
    r.environment["android_capture"] = str(android_dir)
    if density is None and trees:
        r.environment["density"] = "unknown (assumed 1.0; pass --density)"
    elif density is not None:
        r.environment["density"] = str(density)
    return r, root


def _report_capture(args: argparse.Namespace) -> int:
    from .archive import is_archive

    if args.dirs and is_archive(Path(args.dirs[0])):
        return _report_archive(args)
    if len(args.dirs) > 2:
        raise UsageError("report takes at most two capture directories (ATL and Android)")
    if len(args.dirs) == 2:
        r, root = build_pair_report(Path(args.dirs[0]), Path(args.dirs[1]), args.density, args.jank_min_frames)
    else:
        r, root = build_capture_report(Path(args.dirs[0]), args.jank_min_frames)
    apply_capture_aosp(r, root, args.aosp, args.no_aosp_fetch)
    return _emit(args, r, root)


def _report_archive(args: argparse.Namespace) -> int:
    """Report each capture of an APK Starter archive; with several, -o names a directory (one file per capture)."""
    import tempfile

    from .archive import unpack

    with tempfile.TemporaryDirectory(prefix="atl-doctor-") as tmp:
        caps = unpack(Path(args.dirs[0]), Path(tmp))
        if len(caps) == 1:
            args.dirs = [str(caps[0]), *args.dirs[1:]]
            return _report_capture(args)
        if len(args.dirs) > 1:
            raise UsageError("an archive with several captures takes no Android capture directory")
        out = Path(args.output) if args.output else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
        if args.format == "html" and not out:
            raise UsageError("-f html needs -o DIR for an archive with several captures")
        rc = 0
        for cap in caps:
            r, root = build_capture_report(cap, args.jank_min_frames)
            apply_capture_aosp(r, root, args.aosp, args.no_aosp_fetch)
            args.output = str(out / f"{cap.name}.{args.format if args.format != 'text' else 'txt'}") if out else None
            if not out:
                sys.stdout.write(f"== {cap.name}\n")
            rc = max(rc, _emit(args, r, root))
        return rc


def render_explain(f: Finding, r: Report, capture_dir: Path | None) -> str:
    first = [f"{f.id}  ({f.short_id})", f.title, "",
             f"class: {f.klass}  weight: {f.weight} (class rank {RANK[f.klass]}"
             f"{', minus 5 for Android' if f.on_android else ''})  seen: {f.total_count}x"]
    if f.suggestion:
        first += ["", "suggestion: " + f.suggestion]
    if is_harmless(f):
        k = f.details["known_harmless"]
        first += ["", f"known harmless ({k['id']}): {k['reason']}", f"source: {k['source']}"]
    out = first + ["", "occurrences:"]
    out += [f"  {o.phase} [{o.side}]: {o.count}x{'  ' + o.where if o.where else ''}" for o in f.occurrences] or ["  -"]
    stack = f.details.get("stack")
    if stack:
        out += ["", "stack:"] + [f"  {fr}" for fr in stack]
    if f.details.get("callers"):
        out += ["", "callers:"] + [f"  {c}" for c in f.details["callers"]]
    if f.details:
        out += ["", "details:", json.dumps(f.details, indent=2, sort_keys=True, ensure_ascii=False)]
    a = f.details.get("aosp")
    if like_aosp(f):
        from .sources.aosp import aosp_line

        out += ["", aosp_line(f) or ""]
    if "aosp" in f.details:
        out += ["", f"AOSP comparison (SDK {f.details.get('aosp_sdk') or 'unknown'}):"]
        if not a:
            out.append("  not found in the AOSP tree")
        else:
            out += [f"  verdict: {a['verdict']}", f"  file: {a['file']}:{a['lines'][0]}-{a['lines'][1]}"
                    f" ({a['size_lines']} lines{', ' + str(a['overloads']) + ' overloads' if a['overloads'] > 1 else ''})"]
            out += [f"  missing in ATL: {d}" for d in a["deps_missing"]] + [f"  stub in ATL: {d}" for d in a["deps_stub"]]
            if not a.get("deps_checked", True):
                out.append("  dependencies not checked against ATL (no ATL jars)")
            if a["markers"]:
                out.append("  markers: " + ", ".join(a["markers"]))
    crops = f.details.get("crops")
    if crops and capture_dir:
        out += ["", "crops (ATL, Android):"] + [f"  {capture_dir / crops[k]}" for k in ("atl", "android") if k in crops]
    return "\n".join(out) + "\n"


def _run_explain(args: argparse.Namespace) -> int:
    from .findings import lookup

    if not args.dir:
        raise UsageError("explain needs -d DIR (capture directory or JSON report)")
    src = Path(args.dir)
    if src.is_dir():
        r, root = build_capture_report(src, args.jank_min_frames)
    else:
        try:
            r, root = load_json(src.read_text(encoding="utf-8")), None
        except OSError as e:
            raise DoctorError(f"cannot read {src}: {e.strerror}") from e
    try:
        f = lookup(r.findings, args.id)
    except LookupError as e:
        if str(e).startswith("ambiguous"):
            raise UsageError(str(e)) from e
        raise DoctorError("no finding matches ID" + f" {args.id}") from e
    if resolve(args.aosp, "ATL_DOCTOR_AOSP") and f.kind in ("stub", "missing") and "#" in f.key:
        one = Report(r.mode, {}, [], [], [f])
        if root:
            apply_capture_aosp(one, root, args.aosp, args.no_aosp_fetch)
        else:
            apply_aosp(one, args.aosp, None, {}, no_fetch=args.no_aosp_fetch)
    sys.stdout.write(render_explain(f, r, root))
    return 0


def _positive_int(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError("expects an integer >= 1")
    return n


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import command, done

    p, g = command(sub, "report", "report from a capture directory or a log",
                   "Evaluate a capture directory (or an APK Starter archive, or a log) into a report of\n"
                   "findings. With a second, Android capture the two sides are compared.")
    g.args.add_argument("dirs", nargs="*", metavar="DIR",
                        help="capture directory or APK Starter archive (.tar.gz), then optionally the Android "
                             "capture directory")
    g.input.add_argument("--log", action="append", metavar="FILE",
                         help="log mode: one log file per phase (repeatable)")
    g.input.add_argument("--density", type=float, metavar="D",
                         help="Android display density (dp scale) for the tree comparison (default: from the "
                              "Android capture)")
    g.output.add_argument("-f", "--format", choices=["text", "json", "html"], default="text",
                          help="report format (default: text)")
    g.output.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of stdout")
    g.output.add_argument("--fail-on", metavar="CLASS[,CLASS]",
                          help="exit 4 if a finding of one of these classes exists (" +
                               ", ".join(k.value for k in Klass) + ")")
    g.atl.add_argument("--atl-src", metavar="DIR",
                       help="ATL source tree; match logged stub messages against its stub list "
                            "(default: $ATL_DOCTOR_ATL_SRC)")
    g.atl.add_argument("--aosp", metavar="DIR",
                       help="AOSP source tree (root or frameworks/base) to compare stubs and missing methods "
                            "with (default: $ATL_DOCTOR_AOSP)")
    g.advanced.add_argument("--no-aosp-fetch", action="store_true",
                            help="do not download AOSP source when --aosp is not given (default: fetch once into "
                                 "the cache)")
    g.advanced.add_argument("--jank-min-frames", type=_positive_int, default=JANK_MIN_FRAMES, metavar="N",
                            help=f"fewest frames a phase needs for a jank finding (default: {JANK_MIN_FRAMES}); "
                                 "fewer is noted, not reported")
    done(p, g, _run_report)

    p, g = command(sub, "explain", "show everything known about one finding",
                   "Show all evidence for one finding of a report, found by its ID.")
    g.args.add_argument("id", metavar="ID", help="full ID, short ID or unique short-ID prefix")
    g.input.add_argument("-d", "--dir", metavar="DIR", help="capture directory or JSON report")
    g.atl.add_argument("--aosp", metavar="DIR",
                       help="AOSP source tree (root or frameworks/base; default: $ATL_DOCTOR_AOSP)")
    g.advanced.add_argument("--no-aosp-fetch", action="store_true",
                            help="do not download AOSP source when --aosp is not given (default: fetch once into "
                                 "the cache)")
    g.advanced.add_argument("--jank-min-frames", type=_positive_int, default=JANK_MIN_FRAMES, metavar="N",
                            help=f"fewest frames a phase needs for a jank finding (default: {JANK_MIN_FRAMES}); "
                                 "fewer is noted, not reported")
    done(p, g, _run_explain)
