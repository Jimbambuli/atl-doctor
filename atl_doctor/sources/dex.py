# SPDX-License-Identifier: GPL-3.0-or-later
"""Read DEX files and APKs: framework references, result-used calls, class worlds.

Pure standard library.  Class names are descriptors (`Lpkg/C;`) in `Dex` and
`World`; `Ref` and the public lookups use dotted names.
"""
from __future__ import annotations

import array
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..errors import DoctorError


def uleb(b: bytes, o: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[o]
        o += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, o


def dotted(desc: str) -> str:
    """`Lpkg/C;` -> `pkg.C`"""
    return desc[1:-1].replace("/", ".") if desc.startswith("L") and desc.endswith(";") else desc


def descriptor(name: str) -> str:
    """`pkg.C` -> `Lpkg/C;`"""
    return "L" + name.replace(".", "/") + ";"


class Dex:
    """One classes.dex.  Indexes are DEX ids."""

    def __init__(self, data: bytes):
        if data[:4] != b"dex\n" or len(data) < 0x70:
            raise ValueError("bad dex header")
        self.b = data
        (self.str_n, self.str_o, self.type_n, self.type_o, self.proto_n, self.proto_o,
         self.field_n, self.field_o) = struct.unpack_from("<8I", data, 0x38)
        self.meth_n, self.meth_o, self.cls_n, self.cls_o = struct.unpack_from("<4I", data, 0x58)
        self._s: dict[int, str] = {}

    def string(self, i: int) -> str:
        if i not in self._s:
            o = struct.unpack_from("<I", self.b, self.str_o + 4 * i)[0]
            _, o = uleb(self.b, o)
            e = self.b.index(b"\0", o)
            self._s[i] = self.b[o:e].decode("utf-8", "replace")
        return self._s[i]

    def type(self, i: int) -> str:
        return self.string(struct.unpack_from("<I", self.b, self.type_o + 4 * i)[0])

    def _proto(self, i: int) -> str:
        _, ret, par = struct.unpack_from("<3I", self.b, self.proto_o + 12 * i)
        args = ""
        if par:
            n = struct.unpack_from("<I", self.b, par)[0]
            args = "".join(self.type(struct.unpack_from("<H", self.b, par + 4 + 2 * k)[0]) for k in range(n))
        return "(" + args + ")" + self.type(ret)

    def method(self, i: int) -> tuple[str, str]:
        c, p, n = struct.unpack_from("<HHI", self.b, self.meth_o + 8 * i)
        return self.type(c), self.string(n) + self._proto(p)

    def field(self, i: int) -> tuple[str, str]:
        c, ty, n = struct.unpack_from("<HHI", self.b, self.field_o + 8 * i)
        return self.type(c), self.string(n) + ":" + self.type(ty)

    def _class_data(self, data_off: int) -> Iterator[tuple[str, int, int, int]]:
        """yield (kind 'f'|'m', member idx, code_off, access flags) of one class_data_item"""
        o = data_off
        sf, o = uleb(self.b, o)
        inf, o = uleb(self.b, o)
        dm, o = uleb(self.b, o)
        vm, o = uleb(self.b, o)
        for cnt, kind in ((sf, "f"), (inf, "f"), (dm, "m"), (vm, "m")):
            idx = 0
            for _ in range(cnt):
                d, o = uleb(self.b, o)
                idx += d
                acc, o = uleb(self.b, o)
                code = 0
                if kind == "m":
                    code, o = uleb(self.b, o)
                yield kind, idx, code, acc

    def classes(self) -> Iterator[tuple[str, str | None, list[str], set[str]]]:
        """(name, super, interfaces, declared members)"""
        for k in range(self.cls_n):
            ci, _, sup, ifo, _, _, data, _ = struct.unpack_from("<8I", self.b, self.cls_o + 32 * k)
            supn = self.type(sup) if sup != 0xFFFFFFFF else None
            ifs: list[str] = []
            if ifo:
                n = struct.unpack_from("<I", self.b, ifo)[0]
                ifs = [self.type(struct.unpack_from("<H", self.b, ifo + 4 + 2 * j)[0]) for j in range(n)]
            members: set[str] = set()
            if data:
                for kind, idx, _, _ in self._class_data(data):
                    members.add(self.method(idx)[1] if kind == "m" else self.field(idx)[1])
            yield self.type(ci), supn, ifs, members

    def code_items(self) -> Iterator[tuple[str, int, str]]:
        """(class, code_off, member) for every method with code"""
        for k in range(self.cls_n):
            ci, _, _, _, _, _, data, _ = struct.unpack_from("<8I", self.b, self.cls_o + 32 * k)
            if not data:
                continue
            for kind, idx, code, _ in self._class_data(data):
                if kind == "m" and code:
                    yield self.type(ci), code, self.method(idx)[1]

    def methods(self) -> Iterator[tuple[str, str, int, int]]:
        """(class, member, code_off, access flags) for every method defined here, code_off 0 if none"""
        for k in range(self.cls_n):
            ci, _, _, _, _, _, data, _ = struct.unpack_from("<8I", self.b, self.cls_o + 32 * k)
            if not data:
                continue
            for kind, idx, code, acc in self._class_data(data):
                if kind == "m":
                    yield self.type(ci), self.method(idx)[1], code, acc

    def instructions(self, code_off: int) -> Iterator[tuple[int, array.array, int]]:
        """yield (opcode, units, pc) of one code item; payload pseudo-instructions are skipped"""
        n = struct.unpack_from("<I", self.b, code_off + 12)[0]
        u = array.array("H")
        u.frombytes(self.b[code_off + 16:code_off + 16 + 2 * n])
        if sys.byteorder == "big":
            u.byteswap()
        pc = 0
        while pc < n:
            w = u[pc]
            op = w & 0xFF
            if op == 0 and w >> 8:
                if w == 0x100:
                    pc += 4 + 2 * u[pc + 1]
                elif w == 0x200:
                    pc += 2 + 4 * u[pc + 1]
                elif w == 0x300:
                    pc += 4 + (((u[pc + 2] | (u[pc + 3] << 16)) * u[pc + 1] + 1) // 2)
                else:
                    pc += 1
                continue
            yield op, u, pc
            pc += SIZE[op]


# Instruction lengths in code units, indexed by opcode.
SIZE = [1] * 256
for _rng, _n in (((0x02, 0x05, 0x08), 2), ((0x03, 0x06, 0x09), 3)):
    for _o in _rng:
        SIZE[_o] = _n
for _o, _n in {0x13: 2, 0x14: 3, 0x15: 2, 0x16: 2, 0x17: 3, 0x18: 5, 0x19: 2, 0x1A: 2, 0x1B: 3, 0x1C: 2,
               0x1F: 2, 0x20: 2, 0x22: 2, 0x23: 2, 0x24: 3, 0x25: 3, 0x26: 3, 0x29: 2, 0x2A: 3, 0x2B: 3,
               0x2C: 3, 0xFA: 4, 0xFB: 4, 0xFC: 3, 0xFD: 3, 0xFE: 2, 0xFF: 2}.items():
    SIZE[_o] = _n
for _a, _b, _n in ((0x2D, 0x3D, 2), (0x44, 0x6D, 2), (0x6E, 0x72, 3), (0x74, 0x78, 3), (0x90, 0xAF, 2), (0xD0, 0xE2, 2)):
    for _o in range(_a, _b + 1):
        SIZE[_o] = _n

INVOKE_OPS = frozenset(list(range(0x6E, 0x73)) + list(range(0x74, 0x79)) + [0xFA, 0xFB])
FIELD_OPS = range(0x52, 0x6E)
CLASS_OPS = frozenset((0x1C, 0x1F, 0x20, 0x22, 0x23))
MOVE_RESULT_OPS = frozenset((0x0A, 0x0B, 0x0C))


@dataclass(frozen=True)
class Ref:
    kind: str  # "M" | "F" | "C"
    cls: str  # dotted
    member: str  # "name(sig)ret" | "name:type" | ""
    # other framework classes through which an app class reaches the member (interfaces)
    via: tuple[str, ...] = ()


# ---------------------------------------------------------------- manifest
def parse_axml(data: bytes) -> dict[str, str]:
    """Minimal binary-XML reader for AndroidManifest.xml.

    Keys: package, versionName, versionCode, minSdkVersion, targetSdkVersion (only those present).
    """
    out: dict[str, str] = {}
    try:
        sp = 8
        _, _, sz = struct.unpack_from("<HHI", data, sp)
        scount, _, flags, sstart = struct.unpack_from("<4I", data, sp + 8)
        offs = struct.unpack_from("<%dI" % scount, data, sp + 28)
        base = sp + sstart

        def s16(i: int) -> str:
            o = base + offs[i]
            n = struct.unpack_from("<H", data, o)[0]
            o += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, o)[0]
                o += 2
            return data[o:o + 2 * n].decode("utf-16le", "replace")

        def s8(i: int) -> str:
            o = base + offs[i]
            ln = data[o]
            o += 1
            if ln & 0x80:
                o += 1
            ln = data[o]
            o += 1
            if ln & 0x80:
                ln = ((ln & 0x7F) << 8) | data[o]
                o += 1
            return data[o:o + ln].decode("utf-8", "replace")

        S = s8 if flags & 0x100 else s16
        pos = sp + sz
        while pos + 8 <= len(data):
            t, _, sz = struct.unpack_from("<HHI", data, pos)
            if sz < 8:
                break
            if t == 0x0102:
                tag = S(struct.unpack_from("<I", data, pos + 20)[0])
                na = struct.unpack_from("<H", data, pos + 28)[0]
                for k in range(na):
                    ao = pos + 36 + 20 * k
                    name = S(struct.unpack_from("<I", data, ao + 4)[0])
                    raw = struct.unpack_from("<I", data, ao + 8)[0]
                    dt = data[ao + 15]
                    val = struct.unpack_from("<I", data, ao + 16)[0]
                    if tag == "manifest" and name in ("package", "versionName") and raw != 0xFFFFFFFF:
                        out[name] = S(raw)
                    elif tag == "manifest" and name == "versionCode" and dt in (0x10, 0x11):
                        out[name] = str(val)
                    elif tag == "uses-sdk" and name in ("minSdkVersion", "targetSdkVersion") and dt in (0x10, 0x11):
                        out[name] = str(val)
            pos += sz
    except (struct.error, IndexError, ValueError):
        pass
    return out


# ---------------------------------------------------------------- APK
@dataclass
class ApkInfo:
    path: Path
    package: str | None
    version_name: str | None
    version_code: str | None
    min_sdk: int | None
    target_sdk: int | None
    abis: list[str]
    dex_names: list[str]
    dexes: list[Dex]
    _app: dict[str, tuple[str | None, list[str], set[str]]] | None = field(default=None, repr=False, compare=False)

    def app_classes(self) -> dict[str, tuple[str | None, list[str], set[str]]]:
        if self._app is None:
            app: dict[str, tuple[str | None, list[str], set[str]]] = {}
            for d in self.dexes:
                for name, sup, ifs, mem in d.classes():
                    app.setdefault(name, (sup, ifs, mem))
            self._app = app
        return self._app


def _int(s: str | None) -> int | None:
    return int(s) if s and s.isdigit() else None


def read_apk(path: Path) -> ApkInfo:
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "AndroidManifest.xml" not in names:
                raise DoctorError(f"not an APK: {path.name}: no AndroidManifest.xml")
            dex_names = sorted(n for n in names if "/" not in n and n.startswith("classes") and n.endswith(".dex"))
            if not dex_names:
                raise DoctorError(f"not an APK: {path.name}: no classes.dex")
            manifest = parse_axml(z.read("AndroidManifest.xml"))
            dexes = [Dex(z.read(n)) for n in dex_names]
    except (zipfile.BadZipFile, ValueError, struct.error, OSError, NotImplementedError, EOFError) as e:
        raise DoctorError(f"not an APK: {path.name}: {e}") from e
    abis = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.endswith(".so") and n.count("/") >= 2})
    return ApkInfo(path, manifest.get("package"), manifest.get("versionName"), manifest.get("versionCode"),
                   _int(manifest.get("minSdkVersion")), _int(manifest.get("targetSdkVersion")), abis, dex_names, dexes)


def _rep_of(app, c: str) -> str:
    seen = set()
    while c in app and app[c][0] and c not in seen:
        seen.add(c)
        c = app[c][0]
    return c


def _in_app(app, c: str, m: str, seen: set[str]) -> bool:
    e = app.get(c)
    if e is None or c in seen:
        return False
    seen.add(c)
    if m in e[2]:
        return True
    return any(_in_app(app, n, m, seen) for n in ([e[0]] if e[0] else []) + e[1])


def _entries(app, c: str, out: set[str], seen: set[str]) -> None:
    if c in seen:
        return
    seen.add(c)
    e = app.get(c)
    if e is None:
        out.add(c)
        return
    for n in ([e[0]] if e[0] else []) + e[1]:
        _entries(app, n, out, seen)


def _raw_refs(d: Dex) -> set[tuple[str, str, str]]:
    ms: set[int] = set()
    fs: set[int] = set()
    cs: set[int] = set()
    for _, co, _ in d.code_items():
        for op, u, pc in d.instructions(co):
            if op in INVOKE_OPS:
                ms.add(u[pc + 1])
            elif op in FIELD_OPS:
                fs.add(u[pc + 1])
            elif op in CLASS_OPS:
                cs.add(u[pc + 1])
    raw = {("M", *d.method(i)) for i in ms} | {("F", *d.field(i)) for i in fs} | {("C", d.type(i), "") for i in cs}
    return raw


def framework_refs(apk: ApkInfo) -> set[Ref]:
    """References from the code that the app classes do not declare themselves."""
    app = apk.app_classes()
    raw: set[tuple[str, str, str]] = set()
    for d in apk.dexes:
        raw |= _raw_refs(d)
    refs: set[Ref] = set()
    for kind, c, m in sorted(raw):
        if c.startswith("["):
            c = c.lstrip("[")
            if not c.startswith("L"):
                continue
        if kind == "C":
            if c not in app:
                refs.add(Ref("C", dotted(c), ""))
            continue
        if c not in app:
            refs.add(Ref("C", dotted(c), ""))
        if _in_app(app, c, m, set()):
            continue
        ent: set[str] = set()
        _entries(app, c, ent, set())
        rep = _rep_of(app, c)
        via = tuple(sorted(dotted(e) for e in ent if e != rep))
        refs.add(Ref(kind, dotted(rep), m, via))
    return refs


def result_used_refs(apk: ApkInfo) -> set[tuple[str, str]]:
    """(class, method name) of invokes directly followed by a move-result.

    The class is the first framework class on the app superclass chain, the same
    one `framework_refs` reports.
    """
    app = apk.app_classes()
    return _result_uses(apk.dexes, lambda c: _rep_of(app, c))


def framework_result_uses(jars: list[Path]) -> set[tuple[str, str]]:
    """(class, method name) whose result ATL's own code uses (invoke + move-result in the ATL jars)."""
    dexes: list[Dex] = []
    for j in jars:
        try:
            with zipfile.ZipFile(j) as z:
                dexes += [Dex(z.read(n)) for n in sorted(z.namelist())
                          if "/" not in n and n.startswith("classes") and n.endswith(".dex")]
        except (zipfile.BadZipFile, ValueError, struct.error, OSError) as e:
            raise DoctorError(f"cannot read {j}: {e}") from e
    return _result_uses(dexes, lambda c: c)


def _result_uses(dexes: list[Dex], rep) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for d in dexes:
        for _, co, _ in d.code_items():
            prev: int | None = None
            for op, u, pc in d.instructions(co):
                if prev is not None and op in MOVE_RESULT_OPS:
                    c, m = d.method(prev)
                    out.add((dotted(rep(c.lstrip("["))), m.split("(", 1)[0]))
                prev = u[pc + 1] if op in INVOKE_OPS else None
    return out


# ---------------------------------------------------------------- class world
class World:
    """Classes of a set of JARs/DEXes; member lookup like the VM (super before interfaces)."""

    def __init__(self) -> None:
        self.classes: dict[str, tuple[str | None, list[str], set[str]]] = {}
        self._memo: dict[tuple[str, str], str | int | None] = {}

    @classmethod
    def from_dexes(cls, dexes: list[Dex]) -> "World":
        w = cls()
        w.add_dexes(dexes)
        return w

    @classmethod
    def from_jars(cls, jars: list[Path]) -> "World":
        w = cls()
        for j in jars:
            try:
                with zipfile.ZipFile(j) as z:
                    w.add_dexes([Dex(z.read(n)) for n in sorted(z.namelist())
                                 if "/" not in n and n.startswith("classes") and n.endswith(".dex")])
            except (zipfile.BadZipFile, ValueError, struct.error, OSError) as e:
                raise DoctorError(f"cannot read {j}: {e}") from e
        return w

    def add_dexes(self, dexes: list[Dex]) -> None:
        for d in dexes:
            for name, sup, ifs, mem in d.classes():
                self.classes.setdefault(name, (sup, ifs, mem))

    def _find(self, c: str, member: str) -> str | int | None:
        """declaring class descriptor, None = not found, 0 = class missing"""
        k = (c, member)
        if k in self._memo:
            return self._memo[k]
        self._memo[k] = None  # cycle guard
        e = self.classes.get(c)
        if e is None:
            r: str | int | None = 0
        elif member in e[2]:
            r = c
        else:
            r = None
            for n in ([e[0]] if e[0] else []) + e[1]:
                x = self._find(n, member)
                if x:
                    r = x
                    break
        self._memo[k] = r
        return r

    def declaring_class(self, cls: str, member: str) -> str | None:
        """dotted name of the class declaring the member (searching supers), or None"""
        r = self._find(descriptor(cls), member)
        return dotted(r) if isinstance(r, str) else None

    def has(self, cls: str, member: str = "") -> bool:
        if not member:
            return self.has_class(cls)
        return self.declaring_class(cls, member) is not None

    def has_class(self, cls: str) -> bool:
        return descriptor(cls) in self.classes

    def class_names(self) -> set[str]:
        return {dotted(c) for c in self.classes}
