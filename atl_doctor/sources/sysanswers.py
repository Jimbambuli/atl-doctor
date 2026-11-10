# SPDX-License-Identifier: GPL-3.0-or-later
"""System answers and broadcasts from the agent: Q lines (what the system answers the app, asked by the agent
itself at every mark) and B lines (registerReceiver / onReceive of the app's receivers).

Findings: a receiver registered for a sticky action that was never delivered (also without an Android recording), and
per query the answer of ATL against the answer of Android, where they differ.
"""
from __future__ import annotations

from collections import Counter

from ..capture import Mark
from ..findings import Finding, Klass, Occurrence
from .exceptions import AgentEvent

# answers that depend on the device or window by nature: shown, but not a cause to fix first
DEVICE_QUERIES = ("display.metrics",)
SYSTEM_PREFIXES = ("android.", "com.android.")
# actions Android delivers at once on registration (sticky). Any other action is sent only on a real event, so a
# receiver that never gets it is normal and not reported.
STICKY_ACTIONS = frozenset({
    "android.net.conn.CONNECTIVITY_CHANGE",   # ConnectivityManager.CONNECTIVITY_ACTION
    "android.net.wifi.WIFI_STATE_CHANGED",
    "android.net.wifi.STATE_CHANGE",
    "android.media.action.HDMI_AUDIO_PLUG",
    "android.intent.action.HEADSET_PLUG",
    "android.intent.action.BATTERY_CHANGED",
    "android.intent.action.ACTION_POWER_CONNECTED",
    "android.intent.action.ACTION_POWER_DISCONNECTED",
    "android.intent.action.DOCK_EVENT",
})
SUGGEST_BCAST = ("The app waits for this broadcast. If ATL is supposed to send it, it does not; compare with an "
                 "Android recording (record --android).")
SUGGEST_EARLY = ("The receiver was registered before the agent was armed, so a delivery before that (a sticky broadcast "
                 "at registration) is not seen.")
LIMIT_ASKED = ("System answers (Q lines) are asked by the agent itself through the Application, not recorded from the "
               "app's own calls; the connectivity answer is the one at each mark, the report compares the last one.")
LIMIT_ARGS = "Broadcast actions could not be read for {n} register/deliver calls (ART refused the argument); they are not counted."
LIMIT_BCAST = ("No broadcast is reported as never delivered: {n} deliveries had no readable action, so any of them may have "
               "been the sticky one (an outdated JVMTI plugin is a common cause: tools/build-jvmti-plugin.sh).")


def answers(events: list[AgentEvent]) -> dict[str, list[tuple[int, str]]]:
    """Per query the (mark index, answer) pairs in time order."""
    out: dict[str, list[tuple[int, str]]] = {}
    for e in events:
        if e.tag == "Q":
            mark, query, value = e.fields
            out.setdefault(query, []).append((int(mark) if mark.isdigit() else 0, value))
    return out


def last_answers(events: list[AgentEvent]) -> dict[str, str]:
    return {q: v[-1][1] for q, v in answers(events).items()}


def broadcasts(events: list[AgentEvent]) -> tuple[dict[str, set[tuple[str, str]]], Counter, int]:
    """(action -> registering receivers (class, hash), action -> deliveries, calls whose action was unreadable).
    The overloads of registerReceiver call each other: the same receiver and action count once. "early" lines
    (ATL: receivers registered before the agent's breakpoints) count as registrations."""
    registered: dict[str, set[tuple[str, str]]] = {}
    delivered: Counter = Counter()
    unreadable = 0
    for e in events:
        if e.tag != "B":
            continue
        kind, actions, cls, h = e.fields
        if actions == "?":
            unreadable += 1
            continue
        for action in actions.split(","):
            if action in ("", "-"):
                continue
            if kind in ("register", "early"):
                registered.setdefault(action, set()).add((cls, h))
            else:
                delivered[action] += 1
    return registered, delivered, unreadable


def broadcast_findings(events: list[AgentEvent], marks: list[Mark], side: str, notes: list[str] | None = None) -> list[Finding]:
    """One finding per sticky action a receiver was registered for and that was never delivered."""
    registered, delivered, unreadable = broadcasts(events)
    live = {a for e in events if e.tag == "B" and e.fields[0] == "register" for a in e.fields[1].split(",")}
    if unreadable and notes is not None:
        notes.append(LIMIT_ARGS.format(n=unreadable))
    blind = sum(1 for e in events if e.tag == "B" and e.fields[0] == "deliver" and e.fields[1] == "?")
    if blind:   # any of these deliveries may have been the sticky one: "never delivered" cannot be shown
        if notes is not None:
            notes.append(LIMIT_BCAST.format(n=blind))
        return []
    last = marks[-1] if marks else None
    where = [Occurrence(last.name, last.index, side, 1, "agent")] if last else []
    out = []
    for action in sorted(registered):
        if delivered[action] or action not in STICKY_ACTIONS:
            continue
        receivers = sorted({c for c, _ in registered[action]})
        early = action not in live   # registered before the breakpoints were set: earlier deliveries are not seen
        system = action.startswith(SYSTEM_PREFIXES)   # an app's own action is sent by the app itself, not by the system
        out.append(Finding("bcast", action, Klass.SYSTEM if system else Klass.OTHER, f"Receiver registered for {action}, {action} never delivered",
                           SUGGEST_BCAST + (" " + SUGGEST_EARLY if early else ""), list(where),
                           {"action": action, "receivers": receivers, "registrations": len(registered[action]),
                            "registered_before_agent": early, "system_action": system}))
    return out


def annotate_broadcasts(findings: list[Finding], android_events: list[AgentEvent]) -> None:
    """Put what Android did with the action next to each ATL-only broadcast finding."""
    registered, delivered, _ = broadcasts(android_events)
    for f in findings:
        if f.kind != "bcast" or f.on_android:
            continue
        a = f.details["action"]
        f.details["android_registered"] = a in registered
        f.details["android_delivered"] = delivered[a]
        early = " " + SUGGEST_EARLY if f.details.get("registered_before_agent") else ""
        if delivered[a]:
            f.suggestion = (f"Android delivered it {delivered[a]}x in the same run; ATL registered the receiver but "
                            "never sent the broadcast." + early)
        elif a not in registered:
            f.suggestion = ("The agent did not see the app register this receiver on Android (registrations before it was "
                            "armed are not seen there).")


def answer_findings(atl: list[AgentEvent], android: list[AgentEvent], marks: list[Mark], notes: list[str] | None = None) -> list[Finding]:
    """Per query where the last answer of ATL and of Android differ. Nothing without Q lines on both sides."""
    a, b = last_answers(atl), last_answers(android)
    if not a or not b:
        if notes is not None:
            notes.append("System answers differ: " + ("no Q lines in the ATL agent trace" if not a else
                                                      "no Q lines in the Android agent trace") + " (agent without the sys probes, or app not reached).")
        return []
    last = marks[-1] if marks else None
    where = [Occurrence(last.name, last.index, "atl", 1, "agent")] if last else []
    out = []
    for q in sorted(set(a) & set(b)):
        if a[q] == b[q]:
            continue
        device = q in DEVICE_QUERIES
        out.append(Finding("sys", q, Klass.OTHER if device else Klass.SYSTEM, f"{q}: ATL {a[q]}, Android {b[q]}",
                           "Depends on the device and window; shown for completeness." if device else
                           "The app sees a different answer from the system than on Android.",
                           list(where), {"query": q, "atl": a[q], "android": b[q]}))
    if notes is not None:
        notes.append(LIMIT_ASKED)
    return out
