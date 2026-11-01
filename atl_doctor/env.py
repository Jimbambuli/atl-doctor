# SPDX-License-Identifier: GPL-3.0-or-later
"""Environment: option/variable resolution, ATL installation layout, probes."""
from __future__ import annotations

import os
from pathlib import Path


def resolve(cli_value: str | None, env_name: str) -> str | None:
    """Option before environment variable; empty values count as unset."""
    return cli_value or os.environ.get(env_name) or None


def atl_jars(prefix: Path | None) -> list[Path]:
    """ATL's Java classes (api-impl, gstub, ghax ...) of an installation prefix."""
    if prefix is None:
        return []
    return sorted((Path(prefix) / "lib64/java/dex/android_translation_layer").glob("*.jar"))


def libcore_jars(prefix: Path | None) -> list[Path]:
    """libcore host DEX jars shipped with art_standalone."""
    if prefix is None:
        return []
    return sorted((Path(prefix) / "lib64/java/dex/art").glob("*-hostdex.jar"))
