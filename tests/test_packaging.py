# SPDX-License-Identifier: GPL-3.0-or-later
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_pyproject_uses_license_expression_only():
    """PEP 639: a license expression excludes license classifiers (current setuptools refuses the mix)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["license"] == "GPL-3.0-or-later"
    assert not [c for c in project["project"].get("classifiers", []) if c.startswith("License ::")]
    assert "setuptools>=77" in project["build-system"]["requires"]

