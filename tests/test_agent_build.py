# SPDX-License-Identifier: GPL-3.0-or-later
import shutil

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("meson"), reason="meson missing")


def test_agent_builds_without_warnings(built):
    assert (built / "libatl_doctor_agent.so").exists()
