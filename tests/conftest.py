# SPDX-License-Identifier: GPL-3.0-or-later
import os
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"


def _assert_golden(text: str, name: str) -> None:
    path = GOLDEN / name
    if os.environ.get("ATL_DOCTOR_UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_network_for_aosp(tmp_path_factory, monkeypatch):
    """The AOSP source is fetched on demand: tests never reach android.googlesource.com and never touch ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
    monkeypatch.setenv("ATL_DOCTOR_AOSP_URL", "/nonexistent/aosp-mirror")


@pytest.fixture
def assert_golden():
    return _assert_golden


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """The C helpers, built once per session with meson (all warnings incl. -Wpedantic are errors)."""
    import shutil
    import subprocess
    if not shutil.which("meson") or not shutil.which("ninja"):
        pytest.skip("meson/ninja missing")
    b = tmp_path_factory.mktemp("build") / "b"
    agent = Path(__file__).parent.parent / "agent"
    subprocess.run(["nice", "-n", "19", "meson", "setup", str(b), str(agent), "-Dwerror=true", "-Dwarning_level=3"],
                   check=True, capture_output=True)
    r = subprocess.run(["nice", "-n", "19", "ninja", "-C", str(b), "-j", "2"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return b
