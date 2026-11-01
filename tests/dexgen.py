# SPDX-License-Identifier: GPL-3.0-or-later
"""Build minimal DEX files, binary manifests and APKs/JARs for tests (no real app data)."""
from __future__ import annotations

import re
import struct
import zipfile
from pathlib import Path

OPS = {"nop": 0x00, "move-result": 0x0A, "move-result-wide": 0x0B, "move-result-object": 0x0C, "return-void": 0x0E,
       "const-class": 0x1C, "new-instance": 0x22, "iget": 0x52, "sget": 0x60,
       "invoke-virtual": 0x6E, "invoke-super": 0x6F, "invoke-direct": 0x70, "invoke-static": 0x71,
       "invoke-interface": 0x72}


def uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _shorty(proto: str) -> str:
    m = re.match(r"\((.*)\)(.*)", proto)
    args, ret = m.groups()
    toks = re.findall(r"\[*(?:L[^;]*;|[ZBSCIJFDV])", args)
    f = lambda t: "L" if t[0] in "L[" else t
    return f(ret) + "".join(f(t) for t in toks)


def _types_of(proto: str) -> tuple[list[str], str]:
    m = re.match(r"\((.*)\)(.*)", proto)
    return re.findall(r"\[*(?:L[^;]*;|[ZBSCIJFDV])", m.group(1)), m.group(2)


class _Pool:
    def __init__(self) -> None:
        self.strings: list[str] = []
        self.types: list[str] = []
        self.protos: list[str] = []
        self.fields: list[tuple[str, str, str]] = []
        self.methods: list[tuple[str, str, str]] = []

    @staticmethod
    def _idx(lst: list, v) -> int:
        if v not in lst:
            lst.append(v)
        return lst.index(v)

    def s(self, v: str) -> int:
        return self._idx(self.strings, v)

    def t(self, v: str) -> int:
        self.s(v)
        return self._idx(self.types, v)

    def p(self, proto: str) -> int:
        args, ret = _types_of(proto)
        self.s(_shorty(proto)); self.t(ret)
        for a in args:
            self.t(a)
        return self._idx(self.protos, proto)

    def f(self, cls: str, name: str, ty: str) -> int:
        self.t(cls); self.t(ty); self.s(name)
        return self._idx(self.fields, (cls, name, ty))

    def m(self, cls: str, name: str, proto: str) -> int:
        self.t(cls); self.s(name); self.p(proto)
        return self._idx(self.methods, (cls, name, proto))


def _encode_insn(pool: _Pool, insn) -> list[int]:
    op = insn[0]
    if op == "raw":
        return list(insn[1])
    code = OPS[op]
    if op.startswith("invoke-"):
        cls, rest = insn[1].split("->")
        name, proto = rest.split("(", 1)
        return [code, pool.m(cls, name, "(" + proto), 0]
    if op in ("move-result", "move-result-wide", "move-result-object"):
        return [(insn[1] << 8) | code]
    if op in ("sget", "iget"):
        cls, rest = insn[1].split("->")
        name, ty = rest.split(":")
        return [code, pool.f(cls, name, ty)]
    if op in ("new-instance", "const-class"):
        return [code, pool.t(insn[1])]
    return [code]


def make_dex(classes: list[dict]) -> bytes:
    """classes: [{name, super, interfaces, fields: ["n:T"], methods: [{name, proto, code: [insn...]}]}]

    insn: ("invoke-static", "Lc;->m(I)V"), ("move-result-object", 0), ("sget", "Lc;->f:I"),
    ("new-instance", "Lc;"), ("raw", [units]) ...
    """
    pool = _Pool()
    prepared = []
    for c in classes:
        cidx = pool.t(c["name"])
        sidx = pool.t(c["super"]) if c.get("super") else 0xFFFFFFFF
        ifs = [pool.t(i) for i in c.get("interfaces", [])]
        fl = sorted(pool.f(c["name"], *f.split(":", 1)) for f in c.get("fields", []))
        ms = []
        for mth in c.get("methods", []):
            midx = pool.m(c["name"], mth["name"], mth["proto"])
            units = [u for i in mth.get("code", []) + [("return-void",)] for u in _encode_insn(pool, i)]
            ms.append((midx, units))
        prepared.append((cidx, sidx, ifs, fl, sorted(ms)))
    # layout
    n_s, n_t, n_p, n_f, n_m, n_c = (len(pool.strings), len(pool.types), len(pool.protos), len(pool.fields),
                                    len(pool.methods), len(classes))
    off = 0x70
    o_str = off; off += 4 * n_s
    o_typ = off; off += 4 * n_t
    o_pro = off; off += 12 * n_p
    o_fld = off; off += 8 * n_f
    o_mth = off; off += 8 * n_m
    o_cls = off; off += 32 * n_c
    data = bytearray()

    def here() -> int:
        return off + len(data)

    def align4() -> None:
        while here() % 4:
            data.append(0)

    str_offs = []
    for s in pool.strings:
        str_offs.append(here())
        data += uleb(len(s)) + s.encode("utf-8") + b"\0"
    align4()
    proto_params = []
    for pr in pool.protos:
        args, _ = _types_of(pr)
        if args:
            align4()
            proto_params.append(here())
            data += struct.pack("<I", len(args)) + b"".join(struct.pack("<H", pool.types.index(a)) for a in args)
        else:
            proto_params.append(0)
    cls_rows = []
    for cidx, sidx, ifs, fl, ms in prepared:
        ifo = 0
        if ifs:
            align4()
            ifo = here()
            data += struct.pack("<I", len(ifs)) + b"".join(struct.pack("<H", i) for i in ifs)
        code_offs = []
        for midx, units in ms:
            align4()
            code_offs.append(here())
            data += struct.pack("<4HII", 1, 0, 0, 0, 0, len(units)) + b"".join(struct.pack("<H", u) for u in units)
        cd = 0
        if fl or ms:
            cd = here()
            data += uleb(0) + uleb(len(fl)) + uleb(len(ms)) + uleb(0)
            prev = 0
            for f in fl:
                data += uleb(f - prev) + uleb(1); prev = f
            prev = 0
            for (midx, _), co in zip(ms, code_offs):
                data += uleb(midx - prev) + uleb(1) + uleb(co); prev = midx
        cls_rows.append(struct.pack("<8I", cidx, 1, sidx, ifo, 0xFFFFFFFF, 0, cd, 0))
    out = bytearray(off)
    for i, so in enumerate(str_offs):
        struct.pack_into("<I", out, o_str + 4 * i, so)
    for i, t in enumerate(pool.types):
        struct.pack_into("<I", out, o_typ + 4 * i, pool.strings.index(t))
    for i, pr in enumerate(pool.protos):
        _, ret = _types_of(pr)
        struct.pack_into("<3I", out, o_pro + 12 * i, pool.strings.index(_shorty(pr)), pool.types.index(ret), proto_params[i])
    for i, (c, n, t) in enumerate(pool.fields):
        struct.pack_into("<HHI", out, o_fld + 8 * i, pool.types.index(c), pool.types.index(t), pool.strings.index(n))
    for i, (c, n, p) in enumerate(pool.methods):
        struct.pack_into("<HHI", out, o_mth + 8 * i, pool.types.index(c), pool.protos.index(p), pool.strings.index(n))
    for i, row in enumerate(cls_rows):
        out[o_cls + 32 * i:o_cls + 32 * i + 32] = row
    out += data
    out[0:8] = b"dex\n035\0"
    struct.pack_into("<IIII", out, 0x20, len(out), 0x70, 0x12345678, 0)
    struct.pack_into("<8I", out, 0x38, n_s, o_str, n_t, o_typ, n_p, o_pro, n_f, o_fld)
    struct.pack_into("<4I", out, 0x58, n_m, o_mth, n_c, o_cls)
    return bytes(out)


def make_axml(attrs: dict[str, str]) -> bytes:
    """Binary manifest: <manifest package versionName versionCode><uses-sdk minSdkVersion targetSdkVersion/>"""
    manifest = {k: attrs[k] for k in ("package", "versionName", "versionCode") if k in attrs}
    sdk = {k: attrs[k] for k in ("minSdkVersion", "targetSdkVersion") if k in attrs}
    strings: list[str] = []

    def S(v: str) -> int:
        if v not in strings:
            strings.append(v)
        return strings.index(v)

    def element(tag: str, a: dict[str, str]) -> bytes:
        body = b""
        for k, v in a.items():
            if k in ("versionCode", "minSdkVersion", "targetSdkVersion"):
                body += struct.pack("<IIIHBBI", 0xFFFFFFFF, S(k), 0xFFFFFFFF, 8, 0, 0x10, int(v))
            else:
                body += struct.pack("<IIIHBBI", 0xFFFFFFFF, S(k), S(v), 8, 0, 0x03, S(v))
        size = 36 + len(body)
        return (struct.pack("<HHIII", 0x0102, 16, size, 1, 0xFFFFFFFF) + struct.pack("<IIHHHHHH", 0xFFFFFFFF, S(tag), 20, 20, len(a), 0, 0, 0)
                + body)

    def end(tag: str) -> bytes:
        return struct.pack("<HHIII", 0x0103, 16, 24, 1, 0xFFFFFFFF) + struct.pack("<II", 0xFFFFFFFF, S(tag))

    chunks = element("manifest", manifest)
    if sdk:
        chunks += element("uses-sdk", sdk) + end("uses-sdk")
    chunks += end("manifest")
    blobs = []
    for s in strings:
        raw = s.encode("utf-16le")
        blobs.append(struct.pack("<H", len(s)) + raw + b"\0\0")
    offs, pos = [], 0
    for b in blobs:
        offs.append(pos); pos += len(b)
    pool_data = b"".join(blobs)
    while len(pool_data) % 4:
        pool_data += b"\0"
    sstart = 28 + 4 * len(strings)
    pool = struct.pack("<HHIIIIII", 0x0001, 28, sstart + len(pool_data), len(strings), 0, 0, sstart, 0)
    pool += b"".join(struct.pack("<I", o) for o in offs) + pool_data
    total = 8 + len(pool) + len(chunks)
    return struct.pack("<HHI", 0x0003, 8, total) + pool + chunks


def make_apk(path: Path, dex_list: list[bytes], manifest_attrs: dict[str, str] | None = None,
             extra_files: dict[str, bytes] | None = None, manifest: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        if manifest:
            z.writestr("AndroidManifest.xml", make_axml(manifest_attrs or {"package": "org.example.demo"}))
        for i, d in enumerate(dex_list):
            z.writestr("classes.dex" if i == 0 else f"classes{i + 1}.dex", d)
        for n, b in (extra_files or {}).items():
            z.writestr(n, b)
    return path


def make_jar(path: Path, dex_list: list[bytes]) -> Path:
    return make_apk(path, dex_list, manifest=False)


def cls(name: str, super_: str | None = "Ljava/lang/Object;", members: list[str] | None = None,
        interfaces: list[str] | None = None, calls: list | None = None) -> dict:
    """Shorthand class spec: members "name(sig)ret" are methods, "name:T" fields."""
    methods = [{"name": m.split("(")[0], "proto": "(" + m.split("(", 1)[1]} for m in (members or []) if "(" in m]
    fields = [m for m in (members or []) if "(" not in m]
    if calls is not None:
        methods.append({"name": "run", "proto": "()V", "code": calls})
    return {"name": name, "super": super_, "interfaces": interfaces or [], "fields": fields, "methods": methods}
