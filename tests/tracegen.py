# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic ART streaming method traces (the format `-Xmethod-trace-stream` writes)."""
import struct
from pathlib import Path

MAGIC = 0x574F4C53
ENTER, EXIT = 0, 1


def header(version: int, start_us: int) -> bytes:
    h = struct.pack("<IHHQ", MAGIC, version, 32, start_us)
    h += struct.pack("<H", 14 if version >= 3 else 10)  # record size (only read for v3)
    return h.ljust(32, b"\0")


def method_op(methods: list[tuple[int, str, str, str]]) -> bytes:
    text = "".join(f"0x{mid << 2:x}\t{cls}\t{name}\t{sig}\tX.java\n" for mid, cls, name, sig in methods).encode()
    return struct.pack("<HBH", 0, 1, len(text)) + text


def thread_op(tid: int, name: str) -> bytes:
    b = name.encode()
    return struct.pack("<HBHH", 0, 2, tid, len(b)) + b


def encode_records(version: int, recs: list[tuple[int, int, int, int]]) -> bytes:
    """recs: (tid, method_id, action, wall delta in us since start)"""
    out = b""
    for tid, mid, action, wall in recs:
        out += struct.pack("<HI", tid, mid << 2 | action)
        out += struct.pack("<II", wall, wall) if version >= 3 else struct.pack("<I", wall)
    return out


def make_trace(path: Path, version: int, start_us: int, methods: list[tuple[int, str, str, str]],
               records: list[tuple[int, int, int, int]]) -> None:
    Path(path).write_bytes(header(version, start_us) + method_op(methods) + thread_op(1, "main")
                           + encode_records(version, records))
