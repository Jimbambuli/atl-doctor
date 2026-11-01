# SPDX-License-Identifier: GPL-3.0-or-later
"""Probes that turn raw data into findings."""
from __future__ import annotations

from ..capture import AGENT, ATL_LOG, TRACE_DIR, Capture
from ..findings import Finding, Klass, Occurrence, merge
from .exceptions import exception_findings, parse_agent
from .log import SUGGEST_CRASH, parse_log_text
from .memory import memory_findings
from .perf import atl_frame_intervals, perf_findings
from .stubs import StubEntry
from .trace import read_counter_output, stub_findings

TRACE_PREFIX = "tr"  # trace/tr.methods, trace/tr.counts ...


def load_capture_findings(cap: Capture, entries: list[StubEntry] | None,
                          result_used: set[tuple[str, str]]) -> tuple[list[str], list[Finding]]:
    """Phase names and all findings of one capture (trace, agent, atl.log, exit status)."""
    m = cap.read_manifest()
    marks = cap.read_marks()
    phases = [k.name for k in marks]
    last = marks[-1] if marks else None
    end = [Occurrence(last.name, last.index, m.side, 1, "manifest")] if last else []

    found: list[Finding] = []
    if entries is not None:
        hits = read_counter_output(cap.path(TRACE_DIR) / TRACE_PREFIX)
        found += stub_findings(hits, entries, marks, result_used)
    agent_ids: set[str] = set()
    events = parse_agent(cap.path(AGENT)) if cap.path(AGENT).is_file() else []
    if m.side == "atl":  # Android memory is only for comparison (report DIR ANDROID-DIR)
        found += memory_findings(cap, events)
    if cap.path(AGENT).is_file():
        exc = exception_findings(events, marks, m.side)
        agent_ids = {f.id for f in exc}
        found += exc
        if m.side == "atl":  # Android frame times are only for comparison (report DIR ANDROID-DIR)
            found += perf_findings(atl_frame_intervals(events, marks), None, [e for e in events if e.tag == "H"],
                                   [e for e in events if e.tag == "G"], marks)
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
    if m.ended is None:
        found.append(Finding("crash", "aborted-recording", Klass.OTHER, "recording did not finish cleanly",
                             "The recording was interrupted; data up to that point is shown.", list(end)))
    return phases, merge(found)
