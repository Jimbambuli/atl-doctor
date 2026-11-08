# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed check: does ATL's process end after the app window was closed (close-window step)?"""
from __future__ import annotations

import json

from ..findings import Finding, Klass, Occurrence

KEY = "Ends after window close"
DETAIL_KEY = "window close details"
NOT_CHECKED = "not checked (no close-window step, or the run was stopped before the check)"
SUGGEST = ("Closing the last window must end the app process. Look at what keeps it alive: the remaining "
           "processes and threads, a non-daemon thread or a missing finish() of the last activity.")


def read(environment: dict[str, str]) -> dict | None:
    raw = environment.get("window_close")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) and "ended" in d else None


def _procs(d: dict) -> str:
    rem = d.get("remaining") or []
    return ", ".join(f"{p['pid']} {p['name']}" for p in rem) if rem else "none"


def summary(d: dict | None) -> str:
    if d is None:
        return NOT_CHECKED
    if d["ended"]:
        return f"yes ({d['seconds']:.1f} s)"
    return f"no (still running after {d['limit_s']:g} s)"


def details(d: dict | None) -> str | None:
    if d is None:
        return None
    how = (f"killed by {d['signal']}" if d.get("signal") else f"exit {d['exit']}") if d["ended"] else "running"
    return f"{how}; remaining processes: {_procs(d)}"


def window_close_findings(environment: dict[str, str], last_phase: str, last_index: int, side: str) -> list[Finding]:
    d = read(environment)
    if d is None or d["ended"]:
        return []
    return [Finding("lifecycle", "no-exit-after-window-close", Klass.LIFECYCLE,
                    f"Process still running {d['limit_s']:g} s after the window was closed", SUGGEST,
                    [Occurrence(last_phase, last_index, side, 1, "manifest")],
                    {"limit_s": d["limit_s"], "remaining": d.get("remaining", [])})]
