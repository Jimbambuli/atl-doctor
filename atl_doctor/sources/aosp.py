# SPDX-License-Identifier: GPL-3.0-or-later
"""AOSP comparison for stubs and missing methods: where is the method in AOSP, how big, what does it need.

The AOSP version is never fixed: the tree comes from `--aosp DIR` / `ATL_DOCTOR_AOSP` and the platform
version is detected from it.  Verdicts:

  native-or-hidden  the method is native, calls a native method of the same file, or uses ServiceManager,
                    SystemProperties, com.android.internal.* or a binder stub (I...Stub)
  needs-deps        a framework method/constructor it calls (one level) is missing in ATL or a stub there
  portable          neither

Markers (no effect on the verdict): @hide, @UnsupportedAppUsage, @SystemApi, @TestApi, @FlaggedApi(...) and
aconfig flag checks `Flags.x()` (generated classes, not in the source tree; Android 15+ uses them a lot).
"""
from __future__ import annotations

import bisect
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import DoctorError
from ..findings import Finding, Klass
from .dex import World, descriptor
from .stubs import (_KW, TRIVIAL_KINDS, MethodSpan, StubEntry, _desc_simple, _params, mask, match_brace,
                    trivial_pattern, walk_methods)

FRAMEWORK_PREFIXES = ("android.", "com.android.")
_HIDDEN_RE = re.compile(r"\b(?:ServiceManager|SystemProperties)\b|\bcom\.android\.internal\.|\bI[A-Z]\w*\.Stub\b")
_ANNOT_RE = re.compile(r"@(UnsupportedAppUsage|SystemApi|TestApi)\b")
_FLAGGED_RE = re.compile(r"@FlaggedApi\s*\(\s*([^)]*?)\s*\)")
_CALL_RE = re.compile(r"(?<![\w.])(?:(\w+)\s*\.\s*)?(\w+)\s*\(")
_NEW_RE = re.compile(r"\bnew\s+([\w.]+)\s*(?:<[^>]*>)?\s*\(")


@dataclass
class AospTree:
    root: Path
    framework_base: Path
    sdk: int | None


@dataclass
class AospMatch:
    cls: str
    method: str
    file: str              # relative to the tree root
    lines: tuple[int, int]
    size_lines: int
    deps_missing: list[str]
    deps_stub: list[str]
    verdict: str           # "portable" | "needs-deps" | "native-or-hidden"
    markers: list[str] = field(default_factory=list)
    overloads: int = 1
    deps_checked: bool = True  # False: no ATL class world, missing dependencies could not be checked


def _max_int(texts: list[str], pattern: str, below: int = 1000) -> int | None:
    vals = [int(v) for t in texts for v in re.findall(pattern, t)]
    vals = [v for v in vals if v < below]  # VERSION_CODES.CUR_DEVELOPMENT = 10000
    return max(vals) if vals else None


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_sdk(root: Path, framework_base: Path | None = None) -> int | None:
    """Platform SDK version of an AOSP tree; fixed order of sources, None if none gives a number."""
    root = Path(root)
    mk = root / "build/make/core/version_defaults.mk"
    m = re.search(r"^\s*PLATFORM_SDK_VERSION\s*:?=\s*(\d+)", _read(mk), re.M)
    if m:
        return int(m.group(1))
    rel = root / "build/release"
    if rel.is_dir():
        v = _max_int([_read(p) for p in rel.rglob("RELEASE_PLATFORM_SDK_VERSION.textproto")],
                     r"(?:int_value|string_value)\s*:\s*\"?(\d+)")
        if v:
            return v
    fb = framework_base or root / "frameworks/base"
    build, _ = mask(_read(fb / "core/java/android/os/Build.java"))
    vc = re.search(r"\bclass VERSION_CODES\s*\{", build)
    if vc:
        return _max_int([build[vc.start():match_brace(build, vc.end() - 1) + 1]], r"public static final int \w+ = (\d+);")
    return None


def open_aosp(path: Path) -> AospTree:
    """`path` is the AOSP root (with frameworks/base) or frameworks/base itself."""
    p = Path(path).resolve()
    if (p / "frameworks/base").is_dir():
        root, fb = p, p / "frameworks/base"
    elif (p / "core/java/android").is_dir() or (p / "graphics/java/android").is_dir():
        root, fb = p.parent.parent, p
    else:
        raise DoctorError(f"not an AOSP tree: {path} (no frameworks/base)")
    return AospTree(root, fb, detect_sdk(root, fb))


def class_index(tree: AospTree) -> dict[str, Path]:
    """Top-level classes under frameworks/base/**/java/: package line + file name."""
    out: dict[str, Path] = {}
    for f in sorted(tree.framework_base.rglob("*.java")):
        if "/java/" not in f.as_posix():
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as h:
                m = re.search(r"^\s*package\s+([\w.]+)\s*;", h.read(16384), re.M)
        except OSError:
            continue
        if m:
            out.setdefault(f"{m.group(1)}.{f.stem}", f)
    return out


# ---------------------------------------------------------------- one source file
@dataclass
class _File:
    src: str
    m: str
    comments: list[tuple[int, str]]
    lines: list[int]
    pkg: str
    imports: dict[str, str]
    methods: list[MethodSpan]

    def line(self, off: int) -> int:
        return bisect.bisect_right(self.lines, off)


_FILES: dict[Path, _File] = {}


def _load(path: Path) -> _File:
    f = _FILES.get(path)
    if f is None:
        src = _read(path)
        m, comments = mask(src)
        pkg_m = re.search(r"^\s*package\s+([\w.]+)\s*;", m, re.M)
        imports = {i.rsplit(".", 1)[-1]: i for i in re.findall(r"^\s*import\s+([\w.]+)\s*;", m, re.M)}
        f = _File(src, m, comments, [0] + [k + 1 for k, ch in enumerate(src) if ch == "\n"],
                  pkg_m.group(1) if pkg_m else "", imports, list(walk_methods(m)))
        if len(_FILES) > 256:
            _FILES.clear()
        _FILES[path] = f
    return f


def _resolve(name: str, f: _File, own: str, index: dict[str, Path]) -> str | None:
    """Fully qualified class for a simple or qualified name used in the file, None if not a known class."""
    if "." in name:
        return name if name in index else None
    top = own.split("$", 1)[0]
    if any(s.cls == f"{top}${name}" or s.cls.startswith(f"{top}${name}$") for s in f.methods):
        return f"{top}${name}"
    if name in f.imports:
        return f.imports[name]
    if f"{f.pkg}.{name}" in index:
        return f"{f.pkg}.{name}"
    return None


def _has_name(world: World, cls: str, name: str) -> bool:
    """Class or one of its supers declares a member called `name` (any signature)."""
    seen: set[str] = set()
    todo = [descriptor(cls)]
    while todo:
        c = todo.pop()
        if c in seen or c not in world.classes:
            continue
        seen.add(c)
        sup, ifs, members = world.classes[c]
        if any(x.startswith(name + "(") for x in members):
            return True
        todo += ([sup] if sup else []) + list(ifs)
    return False


def _pick(spans: list[MethodSpan], sig: str) -> MethodSpan:
    if sig and "(" in sig:
        want = _desc_simple(sig[sig.index("(") + 1:sig.index(")")])
        same = [s for s in spans if _params(s.params) == want] or [s for s in spans if len(_params(s.params)) == len(want)]
        if same:
            return same[0]
    return max(spans, key=lambda s: s.close - s.head)  # the real implementation, not a delegating overload


def _empty_in_aosp(index: dict[str, Path], cls: str, name: str) -> bool:
    """Every overload of cls#name has an empty body in AOSP (a hook that does nothing): ATL needs nothing for it."""
    path = index.get(cls.split("$", 1)[0])
    if path is None:
        return False
    f = _load(path)
    spans = [x for x in f.methods if x.cls == cls and x.name == name]
    return bool(spans) and all(x.open >= 0 and not f.m[x.open + 1:x.close].strip() for x in spans)


def match(tree: AospTree, index: dict[str, Path], cls: str, method: str, world: World | None,
          stubs: dict, sig: str = "") -> AospMatch | None:
    """AOSP location, size, one level of dependencies and verdict of `cls#method`; None if AOSP lacks it."""
    path = index.get(cls.split("$", 1)[0])
    if path is None:
        return None
    f = _load(path)
    spans = [s for s in f.methods if s.cls == cls and s.name == method]
    if not spans:
        return None
    s = _pick(spans, sig)
    first, last = f.line(s.head), f.line(s.close)
    head = f.m[s.head:s.open if s.open >= 0 else s.close]
    body = f.m[s.open + 1:s.close] if s.open >= 0 else ""

    markers: list[str] = []
    doc = "".join(c for o, c in f.comments if s.last <= o < s.head)
    if "@hide" in doc:
        markers.append("@hide")
    markers += [f"@{a}" for a in dict.fromkeys(_ANNOT_RE.findall(head))]
    markers += [f"@FlaggedApi({x})" for x in _FLAGGED_RE.findall(head)]
    if s.native:
        markers.append("native")

    natives = {x.name for x in f.methods if x.native and x.cls == cls}
    hidden = s.native or bool(_HIDDEN_RE.search(body))
    deps: dict[str, None] = {}
    for m in _NEW_RE.finditer(body):
        outer, _, inner = m.group(1).partition(".")  # new Outer.Inner(...) -> Outer$Inner
        c = _resolve(outer, f, cls, index)
        if c:
            deps[f"{c}{'$' + inner.replace('.', '$') if inner else ''}#<init>"] = None
    for m in _CALL_RE.finditer(body):
        recv, name = m.group(1), m.group(2)
        if name in _KW or body[:m.start()].rstrip().endswith("new"):
            continue
        if recv == "Flags":
            markers.append(f"flag {_resolve('Flags', f, cls, index) or f.imports.get('Flags', 'Flags')}.{name}()")
            continue
        if recv in (None, "this", "super"):
            if name in natives:
                hidden = True
            elif name != method:
                deps[f"{cls}#{name}"] = None
            continue
        if not recv[0].isupper():
            continue  # variable: type unknown without a compiler
        c = _resolve(recv, f, cls, index)
        if c:
            deps[f"{c}#{name}"] = None
    if any(d.startswith("com.android.internal.") for d in deps):
        hidden = True

    missing, stubbed = [], []
    for d in deps:
        c, n = d.split("#", 1)
        if not c.startswith(FRAMEWORK_PREFIXES) or _empty_in_aosp(index, c, n):
            continue
        if (c, n) in stubs:
            stubbed.append(d)
        elif world is not None and not _has_name(world, c, n):
            missing.append(d)
    verdict = "native-or-hidden" if hidden else "needs-deps" if missing or stubbed else "portable"
    return AospMatch(cls, method, path.relative_to(tree.root).as_posix(), (first, last), last - first + 1,
                     missing, stubbed, verdict, list(dict.fromkeys(markers)), len(spans), world is not None)


_SIG_RE = re.compile(r"#[\w$<>]+(\([^)]*\)\S*)")


# ---------------------------------------------------------------- "like AOSP": trivial there as well
def _same_behaviour(e: StubEntry, pat: tuple[str, str] | None) -> bool:
    """ATL's trivial body does what the trivial AOSP body does (same kind, same value)."""
    if pat is None or e.kind not in TRIVIAL_KINDS or pat[0] != e.kind:
        return False
    if e.kind in ("empty", "emptythis"):
        return True
    # a list written before the value column: zero values depend on the return type only, other values are unknown
    return e.ret == pat[1] or (e.kind == "empty0" and not e.ret)


def _like_in_tree(index: dict[str, Path], entries: list[StubEntry]) -> bool:
    path = index.get(entries[0].cls.split("$", 1)[0])
    if path is None:
        return False
    f = _load(path)
    for e in entries:
        spans = [x for x in f.methods if x.cls == e.cls and x.name == e.method]
        if e.sig and "(" in e.sig:
            want = _desc_simple(e.sig[e.sig.index("(") + 1:e.sig.index(")")])
            spans = [x for x in spans if _params(x.params) == want] or [x for x in spans if len(_params(x.params)) == len(want)]
        if not spans or not all(
                x.open >= 0 and _same_behaviour(e, trivial_pattern(f.m[x.open + 1:x.close], x.params)) for x in spans):
            return False
    return True


def _mark_like(f: Finding) -> None:
    f.details["like_aosp"] = True
    f.klass = Klass.OTHER  # not a gap in ATL: no weight, not in "Fix first"


def _stub_entries(f: Finding, stubs: dict) -> list[StubEntry]:
    cls, method = f.key.split("#", 1)
    return [e for e in stubs.get((cls, method), []) if e.cls == cls]


def attach_aosp(findings: list[Finding], tree: AospTree, world: World | None, stubs: dict) -> None:
    """details["aosp"] (None if AOSP lacks the method) and details["aosp_sdk"] for stub:/missing: methods;
    stub hits that are just as trivial in AOSP get details["like_aosp"]."""
    index = class_index(tree)
    for f in findings:
        if f.kind not in ("stub", "missing") or "#" not in f.key:
            continue
        cls, method = f.key.split("#", 1)
        sigs = [x for x in f.details.get("sigs", []) if x]
        t = _SIG_RE.search(f.title)
        sig = sigs[0] if sigs else t.group(1) if t else ""
        m = match(tree, index, cls, method, world, stubs, sig)
        f.details["aosp"] = asdict(m) if m else None
        f.details["aosp_sdk"] = tree.sdk
        if f.kind == "stub" and m:
            es = _stub_entries(f, stubs)
            if es and _like_in_tree(index, es):
                _mark_like(f)


def aosp_line(f: Finding) -> str | None:
    """One report line for the AOSP comparison of a finding, None if it was not compared."""
    if f.details.get("like_aosp"):
        return f"like AOSP: trivial there as well (AOSP {f.details.get('aosp_sdk') or '?'}); not an ATL gap"
    if "aosp" not in f.details:
        return None
    sdk = f.details.get("aosp_sdk") or "?"
    a = f.details["aosp"]
    if not a:
        return f"AOSP {sdk}: not found"
    lo, hi = a["lines"]
    deps = a["deps_missing"] + a["deps_stub"]
    extra = f"; needs {', '.join(deps[:3])}{' ...' if len(deps) > 3 else ''}" if deps else ""
    marks = f"; {', '.join(a['markers'])}" if a["markers"] else ""
    return f"AOSP {sdk}: {a['verdict']}, {a['file']}:{lo}-{hi} ({a['size_lines']} lines){extra}{marks}"
