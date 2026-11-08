# SPDX-License-Identifier: GPL-3.0-or-later
"""Probes that turn raw data into findings."""
from __future__ import annotations

from ..capture import AGENT, ATL_LOG, STUB_HITS, TRACE_DIR, Capture
from ..findings import Finding, Klass, Occurrence, merge
from .exceptions import exception_findings, exit_findings, parse_agent
from .log import SUGGEST_CRASH, parse_log_text
from .memory import memory_findings
from .render import render_findings
from .perf import JANK_MIN_FRAMES, atl_frame_intervals, perf_findings
from .stubs import StubEntry
from .sysanswers import broadcast_findings
from .windowclose import window_close_findings
from .trace import read_counter_output, read_stub_hits, stub_findings

TRACE_PREFIX = "tr"  # trace/tr.methods, trace/tr.counts ...


def load_capture_findings(cap: Capture, entries: list[StubEntry] | None,
                          result_used: set[tuple[str, str]], jank_min_frames: int = JANK_MIN_FRAMES,
                          notes: list[str] | None = None) -> tuple[list[str], list[Finding]]:
    """Phase names and all findings of one capture (trace, agent, atl.log, exit status)."""
    m = cap.read_manifest()
    marks = cap.read_marks()
    phases = [k.name for k in marks]
    last = marks[-1] if marks else None
    end = [Occurrence(last.name, last.index, m.side, 1, "manifest")] if last else []

    found: list[Finding] = []
    if entries is not None or cap.path(STUB_HITS).is_file():
        entries = entries or []
        hits = read_counter_output(cap.path(TRACE_DIR) / TRACE_PREFIX)
        own, extra = read_stub_hits(cap.path(STUB_HITS), marks, f"{m.package}." if m.package else "", entries)
        found += stub_findings(hits + own, entries + extra, marks, result_used)
    agent_ids: set[str] = set()
    events = parse_agent(cap.path(AGENT)) if cap.path(AGENT).is_file() else []
    if m.side == "atl":  # Android memory is only for comparison (report DIR ANDROID-DIR)
        found += memory_findings(cap, events)
        found += render_findings(cap)
    if cap.path(AGENT).is_file():
        exc = exception_findings(events, marks, m.side)
        agent_ids = {f.id for f in exc}
        found += exc
        found += broadcast_findings(events, marks, m.side, notes if m.side == "atl" else None)
        if m.side == "atl":  # Android frame times are only for comparison (report DIR ANDROID-DIR)
            found += perf_findings(atl_frame_intervals(events, marks), None, [e for e in events if e.tag == "H"],
                                   [e for e in events if e.tag == "G"], marks, jank_min_frames, notes)
    if cap.path(ATL_LOG).is_file():
        text = cap.path(ATL_LOG).read_bytes().decode("utf-8", "replace")
        _, logged = parse_log_text(text, 0, phases[0] if phases else "start", m.side)
        found += [f for f in logged if f.id not in agent_ids]  # the agent's version has stack and phase
    if m.signal:
        found.append(Finding("crash", f"signal:{m.signal}", Klass.CRASH, f"fatal signal {m.signal}", SUGGEST_CRASH,
                             list(end)))
    elif m.exit_status:
        found.append(Finding("crash", f"exit:{m.exit_status}", Klass.CRASH, f"exit status {m.exit_status}",
                             SUGGEST_CRASH, list(end)))
    if m.side == "atl" and m.ended is not None:
        found += exit_findings(events, m.exit_status, m.signal, marks, m.side)
    if m.side == "atl" and last:
        found += window_close_findings(m.environment, last.name, last.index, m.side)
    if m.ended is None:
        found.append(Finding("crash", "aborted-recording", Klass.OTHER, "recording did not finish cleanly",
                             "The recording was interrupted; data up to that point is shown.", list(end)))
    return phases, merge(found)
