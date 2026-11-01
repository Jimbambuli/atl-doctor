# SPDX-License-Identifier: GPL-3.0-or-later
"""Log mode: findings from an ordinary ATL / logcat-style log (no agent needed)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import DoctorError
from ..findings import Finding, Klass, Occurrence, merge, normalize_text

PLATFORM_PREFIXES = (
    "android.", "com.android.", "dalvik.", "java.", "javax.", "libcore.", "sun.", "jdk.",
    "org.apache.harmony.", "org.json.", "org.xmlpull.", "org.w3c.", "org.xml.", "org.chromium.",
    "com.google.android.gles_jni.",
)


def is_platform_frame(frame: str) -> bool:
    """True for platform code; `androidx.` is app code."""
    return frame.startswith(PLATFORM_PREFIXES) and not frame.startswith("androidx.")


@dataclass
class LogResult:
    header: dict[str, str]
    phases: list[str]
    findings: list[Finding]


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOGCAT = re.compile(r"^\s*([VDIWEF])/([^(]+)\(\s*\d+\):\s?(.*)$")
_HEADER = re.compile(r"^#\s*apk-starter:\s*([\w.-]+)=(.*)$")
_TRAILER = re.compile(r"^(exit|signal)=(\S+)\s*$")
_AT = re.compile(r"^\s+at ([^\s(]+)\(")
_EXC = re.compile(r'^(?:Exception in thread "[^"]*"\s+)?((?:[\w$]+\.)+[\w$]*(?:Exception|Error))(?::\s?(.*))?$')
_NO_METHOD = re.compile(
    r"NoSuchMethodError: No (?:static |virtual |direct |interface |super )?method (\S+?)\((.*?)\)(\S+) in class L([\w/$]+);")
_NO_FIELD = re.compile(r"NoSuchFieldError: No (?:static |instance )?field (\w+) of type \S+ in class L([\w/$]+);")
_NO_CLASS = re.compile(r"NoClassDefFoundError: Failed resolution of: L([\w/$]+);")
_NO_NATIVE = re.compile(r"UnsatisfiedLinkError: No implementation found for \S+ ([\w.$]+)\.(\w+)\(")
_SIGNAL = re.compile(r"Fatal signal (\d+) \((SIG[A-Z]+)\)")
_EXIT = re.compile(r"System\.exit\((-?\d+)\)")
_STUB_WORDS = ("stub", "not implemented", "unimplemented")

SUGGEST_MISSING = "Implement {cls}.{name} in ATL (src/api-impl)."
SUGGEST_STUBLOG = "Implement the stubbed method logged by {tag}."
SUGGEST_CRASH = "Fix the crash first; later findings may be consequences."


def _dotted(cls: str) -> str:
    return cls.replace("/", ".")


def parse_log_text(text: str, phase_index: int, phase: str, side: str = "atl") -> tuple[dict[str, str], list[Finding]]:
    header: dict[str, str] = {}
    out: list[Finding] = []

    def occ(n: int) -> list[Occurrence]:
        return [Occurrence(phase, phase_index, side, 1, f"line {n}")]

    def add_missing(key: str, title: str, cls: str, name: str, n: int, native: bool = False) -> None:
        f = Finding("missing", key, Klass.MISSING, title, SUGGEST_MISSING.format(cls=cls, name=name), occ(n))
        if native:
            f.details["native"] = True
        out.append(f)

    def add_crash(key: str, title: str, n: int, klass: Klass = Klass.CRASH) -> None:
        out.append(Finding("crash", key, klass, title, SUGGEST_CRASH if klass is Klass.CRASH else "", occ(n)))

    pending: tuple[str, str, bool, int] | None = None  # type, message, uncaught, line
    fatal_seen_at = -10
    for n, line in enumerate(_ANSI.sub("", text).splitlines(), 1):
        m = _LOGCAT.match(line)
        logcat = m.groups() if m else None
        body = logcat[2] if logcat else line

        h = _HEADER.match(line)
        if h:
            header.setdefault(h.group(1), h.group(2).strip())
            continue
        t = _TRAILER.match(line)
        if t:
            header.setdefault(t.group(1), t.group(2))
            continue

        if pending:
            a = _AT.match(body)
            if a:
                ptype, pmsg, uncaught, pn = pending
                pending = None
                frame = normalize_text(a.group(1))
                title = f"{ptype}: {normalize_text(pmsg)}".rstrip(": ")
                if uncaught:
                    add_crash(f"uncaught:{ptype}@{frame}", f"uncaught {title}", pn)
                else:
                    out.append(Finding("exc", f"{ptype}@{frame}", Klass.OTHER, title, "", occ(pn)))
                continue
            pending = None

        if "FATAL EXCEPTION" in body:
            fatal_seen_at = n

        handled = False
        mm = _NO_METHOD.search(body)
        if mm:
            cls = _dotted(mm.group(4))
            add_missing(f"{cls}#{mm.group(1)}", f"missing method {cls}#{mm.group(1)}({mm.group(2)}){mm.group(3)}",
                        cls, mm.group(1), n)
            handled = True
        mm = _NO_FIELD.search(body)
        if mm:
            cls = _dotted(mm.group(2))
            add_missing(f"{cls}#{mm.group(1)}", f"missing field {cls}#{mm.group(1)}", cls, mm.group(1), n)
            handled = True
        mm = _NO_CLASS.search(body)
        if mm:
            cls = _dotted(mm.group(1))
            add_missing(cls, f"missing class {cls}", cls, "", n)
            handled = True
        mm = _NO_NATIVE.search(body)
        if mm:
            cls, name = mm.group(1), mm.group(2)
            add_missing(f"{cls}#{name}", f"missing native method {cls}#{name}", cls, name, n, native=True)
            handled = True

        mm = _SIGNAL.search(body)
        if mm:
            add_crash(f"signal:{mm.group(2)}", f"fatal signal {mm.group(2)}", n)
        mm = _EXIT.search(body)
        if mm:
            code = int(mm.group(1))
            add_crash(f"exit:{code}", f"System.exit({code})", n, Klass.CRASH if code else Klass.OTHER)

        if not handled:
            e = _EXC.match(body.strip())
            if e and not body.lstrip().startswith("Caused by"):
                uncaught = "Exception in thread" in body or n - fatal_seen_at <= 2
                pending = (e.group(1), e.group(2) or "", uncaught, n)

        if logcat and any(w in body.lower() for w in _STUB_WORDS) and not handled:
            tag, msg = logcat[1], body.strip()
            out.append(Finding("stublog", f"{tag}:{normalize_text(msg)}", Klass.OTHER,
                               f"{tag}: {msg}", SUGGEST_STUBLOG.format(tag=tag), occ(n)))

    if "signal" in header:
        add_crash(f"signal:{header['signal']}", f"fatal signal {header['signal']}", 0)
    if "exit" in header:
        try:
            code = int(header["exit"])
        except ValueError:
            code = None
        if code is not None:
            add_crash(f"exit:{code}", f"exit status {code}", 0, Klass.CRASH if code else Klass.OTHER)
    return header, merge(out)


def parse_logs(paths: list[Path]) -> LogResult:
    header: dict[str, str] = {}
    phases: list[str] = []
    findings: list[Finding] = []
    for i, p in enumerate(paths):
        try:
            data = Path(p).read_bytes()
        except OSError as e:
            raise DoctorError(f"cannot read {p}: {e.strerror}") from e
        h, fs = parse_log_text(data.decode("utf-8", "replace"), i, Path(p).stem)
        for k, v in h.items():
            header.setdefault(k, v)
        phases.append(Path(p).stem)
        findings += fs
    return LogResult(header, phases, merge(findings))
