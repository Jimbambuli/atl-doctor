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


@pytest.fixture
def assert_golden():
    return _assert_golden


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """The C helpers, built once per session with meson (warnings are errors)."""
    import shutil
    import subprocess
    if not shutil.which("meson") or not shutil.which("ninja"):
        pytest.skip("meson/ninja missing")
    b = tmp_path_factory.mktemp("build") / "b"
    agent = Path(__file__).parent.parent / "agent"
    subprocess.run(["nice", "-n", "19", "meson", "setup", str(b), str(agent), "-Dwerror=true", "-Dwarning_level=2"],
                   check=True, capture_output=True)
    r = subprocess.run(["nice", "-n", "19", "ninja", "-C", str(b), "-j", "2"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return b
