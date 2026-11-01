# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from atl_doctor import env

ROOT = Path(__file__).parent.parent


def test_pyproject_uses_license_expression_only():
    """PEP 639: a license expression excludes license classifiers (current setuptools refuses the mix)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["license"] == "GPL-3.0-or-later"
    assert not [c for c in project["project"].get("classifiers", []) if c.startswith("License ::")]
    assert "setuptools>=77" in project["build-system"]["requires"]


@pytest.mark.skipif(not shutil.which("meson"), reason="meson missing")
def test_meson_installs_helpers_where_check_looks(built):
    """Every installed helper lands in <prefix>/lib/atl-doctor, independent of the distribution's libdir."""
    r = subprocess.run(["meson", "introspect", "--installed", str(built)], capture_output=True, text=True, check=True)
    installed = [Path(p) for p in json.loads(r.stdout).values()]
    opts = json.loads(subprocess.run(["meson", "introspect", "--buildoptions", str(built)], capture_output=True,
                                     text=True, check=True).stdout)
    prefix = Path(next(o["value"] for o in opts if o["name"] == "prefix"))
    helpers = [p for p in installed if p.suffix != ".1"]
    assert helpers
    for p in helpers:
        assert p.parent in env.HELPER_DIRS(prefix), p


def test_viewdump_hook_patch_is_in_this_repository():
    """The view-tree hook ships as a patch here; doc and patch agree with the protocol atl-doctor speaks."""
    from atl_doctor.sources.tree import VIEWDUMP_REQUEST

    doc = (ROOT / "docs/atlviewdump.md").read_text()
    patches = sorted((ROOT / "contrib/atl").glob("*ATLViewDump*.patch"))
    assert len(patches) == 1 and str(patches[0].relative_to(ROOT)) in doc
    assert "atl-patches" not in doc and "lokal" not in doc
    patch = patches[0].read_text()
    assert f'new File(DIR, "{VIEWDUMP_REQUEST}")' in patch and 'System.getenv("ATL_VIEWDUMP")' in patch
