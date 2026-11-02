# SPDX-License-Identifier: GPL-3.0-or-later
"""Missing native pieces in a log: libraries that do not load, symbols that do not resolve, EGL and audio errors."""
from __future__ import annotations

import re

from ..findings import Finding, Klass, Occurrence

# (category, pattern whose group 1 names the piece, class)
PATTERNS: list[tuple[str, re.Pattern, Klass]] = [
    ("dlopen", re.compile(r'dlopen failed: library "([^"]+)"'), Klass.MISSING),
    ("dlopen", re.compile(r'couldn\'t find "([^"]+\.so)"'), Klass.MISSING),
    ("dlopen", re.compile(r"([\w.+-]+\.so[\w.]*): cannot open shared object file"), Klass.MISSING),
    ("symbol", re.compile(r'cannot locate symbol "([^"]+)"'), Klass.MISSING),
    ("symbol", re.compile(r"undefined symbol: (\S+)"), Klass.MISSING),
    ("egl", re.compile(r"\b(EGL_BAD_\w+)"), Klass.OTHER),
    ("egl", re.compile(r"\b(egl\w+) failed", re.I), Klass.OTHER),
    ("audio", re.compile(r"\b(AAudio|OpenSL ?ES|AudioTrack|AudioRecord)\w*.*(?:error|fail|not supported)", re.I),
     Klass.OTHER),
    ("audio", re.compile(r"^(ALSA) lib .*(?:Unknown|error|fail|cannot)", re.I), Klass.OTHER),
]
TITLES = {"dlopen": "native library does not load: {}", "symbol": "native symbol not found: {}",
          "egl": "EGL error: {}", "audio": "audio error ({})"}
SUGGEST = {"dlopen": "Provide the library (or the one it depends on) in ATL's native search path.",
           "symbol": "Implement the missing native symbol in ATL's NDK libraries.",
           "egl": "Check ATL's EGL/surface glue for this call.",
           "audio": "Check ATL's audio backend for this API."}


def native_findings(text: str, side: str, phase: str = "start", phase_index: int = 0) -> list[Finding]:
    """One finding per missing piece, counted per log line (first matching pattern of a line wins)."""
    found: dict[str, Finding] = {}
    for line in text.splitlines():
        body = re.sub(r"^\s*[VDIWEF]/[^:]*:\s?", "", line)  # logcat prefix
        for cat, rx, klass in PATTERNS:
            m = rx.search(body)
            if not m:
                continue
            what = m.group(1)
            what = "ALSA" if cat == "audio" and what.upper() == "ALSA" else what
            key = f"{cat}:{what}"
            if key not in found:
                found[key] = Finding("native", key, klass, TITLES[cat].format(what), SUGGEST[cat],
                                     [Occurrence(phase, phase_index, side, 0)], {"line": body.strip()[:200]})
            o = found[key].occurrences[0]
            found[key].occurrences[0] = Occurrence(o.phase, o.phase_index, o.side, o.count + 1)
            break
    return list(found.values())
