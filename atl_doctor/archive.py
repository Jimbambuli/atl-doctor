# SPDX-License-Identifier: GPL-3.0-or-later
"""Archives exported by APK Starter: a tar(.gz) with capture directories and/or one tar.gz per capture."""
from __future__ import annotations

import tarfile
from pathlib import Path

from .errors import DoctorError

SUFFIXES = (".tar.gz", ".tgz", ".tar")


def is_archive(p: Path) -> bool:
    return p.is_file() and p.name.endswith(SUFFIXES)


def _extract(arc: Path, dest: Path) -> None:
    try:
        with tarfile.open(arc) as t:
            t.extractall(dest, filter="data")  # refuses absolute paths, ../ and links leaving dest
    except (tarfile.TarError, OSError) as e:
        raise DoctorError(f"cannot unpack {arc.name}: {e}") from e


def unpack(arc: Path, dest: Path) -> list[Path]:
    """Unpack arc into dest (nested capture archives too); the capture directories found, sorted by name."""
    _extract(arc, dest)
    for inner in sorted(p for p in dest.iterdir() if is_archive(p)):
        _extract(inner, dest)
        inner.unlink()
    caps = sorted(p for p in dest.iterdir() if p.is_dir() and (p / "atl.log").is_file())
    if not caps:
        raise DoctorError(f"no capture in {arc.name}")
    return caps
