# SPDX-License-Identifier: GPL-3.0-or-later
"""Static stub list: which ATL methods are stubs, found in the ATL source tree.

Every Java method in `src/{api-impl,ghax,gstub}` is found with a small scanner
(comments/literals masked, braces counted, nested classes -> Outer$Inner,
methods of anonymous/local classes skipped).  Body kinds:

  empty        void without any statement      empty0      only return null/false/0/empty array or collection
  emptythis    only `return this`              const       only return <literal or CONSTANT>
  returnsarg   only `return <parameter>` (the input comes back unchanged, e.g. a default value)
  throws       throws UnsupportedOperationException, or throws with stub text (short body)
  logs         string literal with stub/not implemented/unimplemented/TODO/FIXME (short body)
  throws-part / logs-part   the same in one branch of a longer body (method otherwise implemented)
  native-*     native method; kind from the C body in src/api-impl-jni (empty/empty0/const/logs),
               native-missing = no C function Java_<class>_<name> found

Constructors with an empty body do not count.  The signature (DEX descriptor) and the bytecode kind
come from the JARs when given: match by name + simple parameter types, else name + arity when unique.
"""
from __future__ import annotations

import bisect
import re
import struct
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..errors import DoctorError
from ..findings import Finding, Klass, Occurrence, merge
from .dex import SIZE, Dex

STUB_KINDS = {"empty", "empty0", "emptythis", "const", "returnsarg", "throws", "logs", "throws-part", "logs-part",
              "native-empty", "native-empty0", "native-const", "native-logs", "native-missing"}
RESULT_KINDS = {"empty0", "emptythis", "const", "returnsarg", "native-empty0", "native-const"}

STUBTEXT = re.compile(r"stub|not implemented|unimplemented|\btodo\b|fixme", re.I)
_KIND_NAMES = {"leer": "empty", "leer0": "empty0", "leerthis": "emptythis", "konst": "const", "wirft": "throws",
               "meldet": "logs"}  # historic names used inside the scanner


@dataclass(frozen=True)
class StubEntry:
    kind: str
    cls: str
    method: str
    sig: str
    location: str  # "src/...:line"
    bytecode_kind: str
    comment: str
    ret: str = ""  # returned value (empty0/const: expression, returnsarg: parameter index); "" = unknown/none


# ---------------------------------------------------------------- Java scanner
_KW = {"if", "for", "while", "switch", "catch", "synchronized", "try", "do", "else", "return", "new", "throw",
       "finally", "case", "assert", "super", "this"}
_TYPE_RE = re.compile(r"\b(class|interface|enum|record)\s+(\w+)")
_METH_RE = re.compile(r"(\w+)\s*\(((?:[^()]|\([^()]*\))*)\)\s*(?:throws\s+[\w.,\s<>]+)?$")
_NATIVE_RE = re.compile(r"\bnative\b[^;{}=]*?\b(\w+)\s*\(((?:[^()]|\([^()]*\))*)\)\s*(?:throws\s+[\w.,\s]+)?$")
_ZERO = (r"(?:null|false|0|0[lLfFdD]|0\.0[fFdD]?|\(\w+\)\s*0|\'x+\'|new\s+[\w.<>]+\s*\[\s*0\s*\]|"
         r"new\s+[\w.<>]+\s*\[\s*\]\s*\{\s*\}|Collections\.empty\w*\(\s*\)|\"\"|JNI_FALSE|NULL)")
_CONST = r"(?:true|-?\d[\w.]*|\"x*\"|[A-Z][\w]*(?:\.[A-Z_][A-Z0-9_]*)+|[A-Z_][A-Z0-9_]{2,})"


def mask(src: str) -> tuple[str, list[tuple[int, str]]]:
    """Comments -> blanks, string/char contents -> 'x'; length and lines are kept.  Returns (mask, comments)."""
    out = list(src)
    i, n = 0, len(src)
    comments: list[tuple[int, str]] = []
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, src[i:j]))
            out[i:j] = " " * (j - i)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            comments.append((i, src[i:j]))
            out[i:j] = [ch if ch == "\n" else " " for ch in src[i:j]]
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out[i + 3:j - 3] = [ch if ch == "\n" else "x" for ch in src[i + 3:j - 3]]
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out[i + 1:j] = "x" * (j - i - 1)
            i = j + 1
        else:
            i += 1
    return "".join(out), comments


def match_brace(m: str, o: int) -> int:
    d = 0
    for k in range(o, len(m)):
        if m[k] == "{":
            d += 1
        elif m[k] == "}":
            d -= 1
            if d == 0:
                return k
    return len(m) - 1


def kind_of(body_m: str, body_raw: str, ctor: bool, params: str = "") -> str | None:
    b = re.sub(r"\s+", " ", body_m).strip()
    stubtext = any(STUBTEXT.search(s) for s in re.findall(r'"((?:[^"\\\n]|\\.)*)"', body_raw))
    short = b.count(";") <= 3 and not re.search(r"\b(for|while|switch)\b", b)  # else one branch of a longer body
    if re.search(r"throw new UnsupportedOperationException", b) or (stubtext and re.search(r"\bthrow new ", b)):
        return "throws" if short else "throws-part"
    if stubtext:
        return "logs" if short else "logs-part"
    if b in ("", "return;"):
        return None if ctor else "empty"
    if re.fullmatch(r"return\s*\(?\s*" + _ZERO + r"\s*\)?\s*;", b):
        return "empty0"
    if re.fullmatch(r"return\s+this\s*;", b):
        return "emptythis"
    if re.fullmatch(r"return\s*\(?\s*" + _CONST + r"\s*\)?\s*;", b):
        return "const"
    names = re.findall(r"(\w+)\s*(?:\[\s*\])*\s*(?:,|$)", params.strip())
    r = re.fullmatch(r"return\s*\(?\s*(\w+)\s*\)?\s*;", b)
    if r and r.group(1) in names:
        return "returnsarg"
    return None


TRIVIAL_KINDS = ("empty", "empty0", "emptythis", "const", "returnsarg")


def trivial_pattern(body_m: str, params: str = "") -> tuple[str, str] | None:
    """(kind, returned value) of a trivial body (masked source), None if the body is real code or a stub marker.

    Two methods with the same pattern behave the same: `return false;` and `return false;`, but not
    `return 0;` and `return 1;`.  returnsarg: the value is the index of the returned parameter."""
    kd = kind_of(body_m, "", False, params)
    if kd not in TRIVIAL_KINDS:
        return None
    b = re.sub(r"\s+", " ", body_m).strip()
    if kd in ("empty0", "const"):
        m = re.fullmatch(r"return\s*\(?\s*(.*?)\s*\)?\s*;", b)
        return kd, re.sub(r"\s+", "", m.group(1)) if m else ""
    if kd == "returnsarg":
        names = re.findall(r"(\w+)\s*(?:\[\s*\])*\s*(?:,|$)", params.strip())
        m = re.fullmatch(r"return\s*\(?\s*(\w+)\s*\)?\s*;", b)
        return kd, str(names.index(m.group(1))) if m and m.group(1) in names else ""
    return kd, ""


def _simple(t: str) -> str:
    t = re.sub(r"@\w+(\([^)]*\))?", "", t)
    t = re.sub(r"\bfinal\b", "", t).strip()
    while "<" in t:
        t2 = re.sub(r"<[^<>]*>", "", t)
        if t2 == t:
            break
        t = t2
    arr = t.count("[]") + (1 if "..." in t else 0)
    parts = re.sub(r"\[\]|\.\.\.", " ", t).split()
    return (parts[0] if parts else "").split(".")[-1] + "[]" * arr


def _params(p: str) -> list[str]:
    p = p.strip()
    if not p:
        return []
    parts, depth, cur = [], 0, ""
    for ch in p:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [_simple(re.sub(r"\s+\w+\s*((\[\])*)$", r"\1", x.strip())) for x in parts]


@dataclass(frozen=True)
class MethodSpan:
    """One method declaration found by `walk_methods` (offsets into the masked source)."""
    cls: str          # package + Outer$Inner
    name: str         # "<init>" for constructors
    params: str       # raw parameter list
    head: int         # first non-blank character of the declaration (annotations included)
    open: int         # "{" of the body, -1 for native methods
    close: int        # "}" of the body, ";" for native methods
    last: int         # end of the previous statement/member (comments in last..head belong to the method)
    ctor: bool
    native: bool


def walk_methods(m: str) -> Iterator[MethodSpan]:
    """Every method with a body and every native method of one masked Java source (see `mask`)."""
    pkg_m = re.search(r"^\s*package\s+([\w.]+)\s*;", m, re.M)
    pkg = pkg_m.group(1) + "." if pkg_m else ""
    stack: list[tuple[str, str, int]] = []  # (type|anon|block, name, close)
    last = 0
    k = 0
    while k < len(m):
        while stack and k > stack[-1][2]:
            stack.pop()
        ch = m[k]
        if ch in ";}":
            if ch == ";" and stack and stack[-1][0] == "type":
                hd = m[last:k].strip()
                nm = _NATIVE_RE.search(hd)
                if nm:
                    cls = pkg + "$".join(s[1] for s in stack)
                    yield MethodSpan(cls, nm.group(1), nm.group(2), last + len(m[last:k]) - len(m[last:k].lstrip()),
                                     -1, k, last, False, True)
            last = k + 1
        elif ch == "{":
            hd = m[last:k]
            hds = hd.strip()
            close = match_brace(m, k)
            in_type = stack and stack[-1][0] == "type" or not stack
            tm = _TYPE_RE.search(hds)
            mm = _METH_RE.search(hds)
            if in_type and tm and not mm:
                stack.append(("type", tm.group(2), close))
            elif (in_type and stack and mm and mm.group(1) not in _KW
                  and not re.search(r"\bnew\s+[\w.<>]+\s*\($", hds[:mm.end(1) + 1] + "(")
                  and not re.search(r"[=(,]\s*$", hds[:mm.start()])):
                name = mm.group(1)
                cls = pkg + "$".join(s[1] for s in stack)
                head_wo = hds[:mm.start()]
                ctor = name == stack[-1][1] and not re.search(
                    r"\w", re.sub(r"\b(public|protected|private|static|final|synchronized|@\w+(\([^)]*\))?)\b", "", head_wo))
                yield MethodSpan(cls, "<init>" if ctor else name, mm.group(2), last + len(hd) - len(hd.lstrip()),
                                 k, close, last, ctor, False)
                stack.append(("block", name, close))
            elif re.search(r"\bnew\s+[\w.<>]+\s*\((?:[^()]|\([^()]*\))*\)\s*$", hds) or (tm and not in_type):
                stack.append(("anon", "?", close))
            else:
                stack.append(("block", "", close))
            last = k + 1
        k += 1


def java_methods(root: Path, rel: str) -> Iterator[tuple[str, str, str, list[str], int, str, str]]:
    """yield (kind, class, method, simple param types, line, stub-text comment, returned value) for the
    stub-like methods of one file"""
    src = (root / rel).read_text(encoding="utf-8", errors="replace")
    m, comments = mask(src)
    lines = [0] + [k + 1 for k, ch in enumerate(src) if ch == "\n"]
    for s in walk_methods(m):
        line = bisect.bisect_right(lines, s.head)
        if s.native:
            yield ("native", s.cls, s.name, _params(s.params), line, "", "")
            continue
        kd = kind_of(m[s.open + 1:s.close], src[s.open + 1:s.close], s.ctor, s.params)
        if kd:
            com = "".join(c for o, c in comments if s.open < o < s.close)
            note = re.sub(r"\s+", " ", com).strip()[:120] if STUBTEXT.search(com) else ""
            pat = trivial_pattern(m[s.open + 1:s.close], s.params)
            yield (kd, s.cls, s.name, _params(s.params), line, note, pat[1] if pat and pat[0] == kd else "")


# ---------------------------------------------------------------- C side
def _demangle(s: str) -> str:
    out, i = "", 0
    while i < len(s):
        if s[i] == "_" and i + 1 < len(s) and s[i + 1] in "0123":
            if s[i + 1] == "0":
                out += chr(int(s[i + 2:i + 6], 16))
                i += 6
            else:
                out += {"1": "_", "2": ";", "3": "["}[s[i + 1]]
                i += 2
        elif s[i] == "_":
            out += "\x00"
            i += 1
        else:
            out += s[i]
            i += 1
    return out


def _c_functions(jni: Path, base: Path) -> dict[tuple[str, str], tuple[str | None, str]]:
    res: dict[tuple[str, str], tuple[str | None, str]] = {}
    if not jni.is_dir():
        return res
    for p in sorted(jni.rglob("*.c")):
        src = p.read_text(encoding="utf-8", errors="replace")
        m, _ = mask(src)
        for mt in re.finditer(r"\b(Java_\w+)\s*\(([^)]*)\)\s*\{", m):
            close = match_brace(m, mt.end() - 1)
            b = re.sub(r"\s+", " ", m[mt.end():close]).strip()
            raw_strings = re.findall(r'"((?:[^"\\\n]|\\.)*)"', src[mt.end():close])
            if any(STUBTEXT.search(s) for s in raw_strings):
                kd: str | None = "logs"
            elif b in ("", "return;"):
                kd = "empty"
            elif re.fullmatch(r"return\s*\(?\s*(\(\w+\)\s*)?" + _ZERO + r"\s*\)?\s*;", b):
                kd = "empty0"
            elif re.fullmatch(r"return\s*\(?\s*(\(\w+\)\s*)?(?:true|JNI_TRUE|-?\d[\w.]*|[A-Z_][A-Z0-9_]{2,})\s*\)?\s*;", b):
                kd = "const"
            else:
                kd = None
            parts = _demangle(mt.group(1)[5:].split("__")[0]).split("\x00")
            res[(".".join(parts[:-1]), parts[-1])] = (kd, f"{p.relative_to(base)}:{src.count(chr(10), 0, mt.start()) + 1}")
    return res


# ---------------------------------------------------------------- bytecode kind
def bytecode_kind(d: Dex, co: int, member: str) -> str:
    """kind of a method body from its bytecode ('' if none of the stub kinds)"""
    regs, ins = struct.unpack_from("<HH", d.b, co)
    ops: list[tuple[int, int | None, int]] = []
    strings: list[str] = []
    news: list[str] = []
    for op, u, pc in d.instructions(co):
        if op == 0x1A:
            strings.append(d.string(u[pc + 1]))
        elif op == 0x1B:
            strings.append(d.string(u[pc + 1] | (u[pc + 2] << 16)))
        elif op == 0x22:
            news.append(d.type(u[pc + 1]))
        val = None
        if op == 0x12:
            val = u[pc] >> 12
        elif op in (0x13, 0x15, 0x16, 0x19):
            val = u[pc + 1]
        elif op in (0x14, 0x17):
            val = u[pc + 1] | (u[pc + 2] << 16)
        elif op == 0x18:
            val = u[pc + 1] | (u[pc + 2] << 16) | (u[pc + 3] << 32) | (u[pc + 4] << 48)
        ops.append((op, val, u[pc]))
    stubtext = any(STUBTEXT.search(s) for s in strings)
    if "Ljava/lang/UnsupportedOperationException;" in news:
        return "throws"
    if stubtext and any(o == 0x27 for o, _, _ in ops):
        return "throws"
    if stubtext:
        return "logs"
    if [o for o, _, _ in ops if o != 0x00] == [0x0E]:
        return "empty"
    if (len(ops) == 2 and ops[0][0] in (0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19) and ops[0][1] == 0
            and ops[1][0] in (0x0F, 0x10, 0x11)):
        return "empty0"
    if len(ops) == 1 and ops[0][0] == 0x11 and (ops[0][2] >> 8) == regs - ins and not member.startswith("<"):
        return "emptythis"
    return ""


_PRIM = {"I": "int", "Z": "boolean", "J": "long", "F": "float", "D": "double", "B": "byte", "C": "char",
         "S": "short", "V": "void"}


def _desc_simple(d: str) -> list[str]:
    out, i = [], 0
    while i < len(d):
        a = 0
        while d[i] == "[":
            a += 1
            i += 1
        if d[i] == "L":
            j = d.index(";", i)
            t = re.split(r"[/$]", d[i + 1:j])[-1]
            i = j + 1
        else:
            t = _PRIM[d[i]]
            i += 1
        out.append(t + "[]" * a)
    return out


def _jar_methods(jars: list[Path]) -> dict[tuple[str, str], list[tuple[list[str], str, str]]]:
    dexm: dict[tuple[str, str], list[tuple[list[str], str, str]]] = defaultdict(list)
    for j in jars:
        try:
            with zipfile.ZipFile(j) as z:
                dexes = [Dex(z.read(n)) for n in sorted(z.namelist())
                         if "/" not in n and n.startswith("classes") and n.endswith(".dex")]
        except (zipfile.BadZipFile, ValueError, struct.error, OSError) as e:
            raise DoctorError(f"cannot read {j}: {e}") from e
        for d in dexes:
            for cname, member, co, acc in d.methods():
                cls = cname[1:-1].replace("/", ".")
                name, sig = member.split("(", 1)
                sig = "(" + sig
                bc = bytecode_kind(d, co, member) if co else ("native" if acc & 0x100 else "abstract")
                dexm[(cls, name)].append((_desc_simple(sig[1:sig.index(")")]), sig, bc))
    return dexm


def _norm(x: list[str]) -> list[str]:
    return ["Object" if re.fullmatch(r"[A-Z]\w?(\[\])*", t) and t.rstrip("[]") != "R" else t for t in x]


def build_stub_list(atl_src: Path, jars: list[Path]) -> list[StubEntry]:
    atl_src = Path(atl_src)
    root = atl_src / "src" if (atl_src / "src").is_dir() else atl_src
    if not any((root / s).is_dir() for s in ("api-impl", "ghax", "gstub")):
        raise DoctorError(f"not an ATL source tree: {atl_src} (no src/api-impl)")
    dexm = _jar_methods(jars) if jars else {}
    cfun = _c_functions(root / "api-impl-jni", atl_src)
    entries: list[StubEntry] = []
    for sub in ("api-impl", "ghax", "gstub"):
        for f in sorted((root / sub).rglob("*.java")):
            rel = str(f.relative_to(atl_src))
            for kd, cls, name, ps, line, note, ret in java_methods(atl_src, rel):
                cands = dexm.get((cls, name), [])
                same_arity = [c for c in cands if len(c[0]) == len(ps)]
                hit = ([c for c in cands if c[0] == ps] or [c for c in cands if _norm(c[0]) == _norm(ps)]
                       or (same_arity if len(same_arity) == 1 else []))
                sig, bc = (hit[0][1], hit[0][2]) if hit else ("", "")
                if kd == "native":
                    c = cfun.get((cls, name))
                    if not c:
                        kd = "native-missing"
                    elif c[0]:
                        kd = "native-" + c[0]
                    else:
                        continue
                entries.append(StubEntry(kd, cls, name, sig, f"{rel}:{line}", bc, note, ret))
    return entries


# ---------------------------------------------------------------- TSV and index
_TSV_HEAD = ("kind", "class", "method", "sig", "location", "bytecode_kind", "comment", "ret")


def save_stub_tsv(entries: list[StubEntry], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(_TSV_HEAD) + "\n")
        for e in entries:
            f.write("\t".join((e.kind, e.cls, e.method, e.sig, e.location, e.bytecode_kind,
                               e.comment.replace("\t", " ").replace("\n", " "), e.ret.replace("\t", " "))) + "\n")


def load_stub_tsv(path: Path) -> list[StubEntry]:
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    head = tuple(lines[0].split("\t")) if lines else ()
    if head not in (_TSV_HEAD, _TSV_HEAD[:7]):  # lists written before the ret column are still readable
        raise DoctorError(f"not a stub list: {path}")
    out = []
    for ln in lines[1:]:
        if ln:
            f = (ln.split("\t") + [""] * 8)[:8]
            out.append(StubEntry(*f))
    return out


def stub_index(entries: list[StubEntry]) -> dict[tuple[str, str], list[StubEntry]]:
    idx: dict[tuple[str, str], list[StubEntry]] = defaultdict(list)
    for e in entries:
        idx[(e.cls, e.method)].append(e)
    return dict(idx)


def stub_finding(cls: str, method: str, entries: list[StubEntry], result_used: bool,
                 occurrences: list[Occurrence], callers: list[str]) -> Finding:
    mine = [e for e in entries if e.cls == cls and e.method == method]
    kinds = sorted({e.kind for e in mine})
    used = result_used and any(k in RESULT_KINDS for k in kinds)
    klass = Klass.STUB_RESULT_USED if used else Klass.OTHER
    note = f" ({', '.join(kinds)})" if kinds else ""
    sug = f"Implement {cls}.{method}{note}" + (f" at {mine[0].location}." if mine else ".")
    return Finding("stub", f"{cls}#{method}", klass, f"stub {cls}#{method}{note}", sug, list(occurrences),
                   {"kinds": kinds, "locations": [e.location for e in mine], "sigs": [e.sig for e in mine],
                    "callers": callers[:10]})


_LOG_CALL = re.compile(r"(\w+)\.(\w+)\(")


def match_log_stubs(findings: list[Finding], entries: list[StubEntry]) -> list[Finding]:
    """Replace `stublog:` findings that name exactly one known stub (`Class.method(`) by `stub:` findings."""
    by_simple: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for e in entries:
        by_simple[(e.cls.replace("$", ".").split(".")[-1], e.method)].add((e.cls, e.method))
    out: list[Finding] = []
    for f in findings:
        if f.kind == "stublog":
            hits = {h for m in _LOG_CALL.finditer(f.title) for h in by_simple.get((m.group(1), m.group(2)), ())}
            if len(hits) == 1:
                cls, method = next(iter(hits))
                out.append(stub_finding(cls, method, entries, False, f.occurrences, []))
                continue
        out.append(f)
    return merge(out)
