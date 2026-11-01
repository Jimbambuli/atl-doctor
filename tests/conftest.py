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
