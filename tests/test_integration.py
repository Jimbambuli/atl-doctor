# SPDX-License-Identifier: GPL-3.0-or-later
"""Needs a real ATL: ATL_DOCTOR_PREFIX and ATL_DOCTOR_TEST_APK (a small app, not part of the repository)."""
import json
import os
from pathlib import Path

import pytest

from atl_doctor.cli import main

pytestmark = pytest.mark.integration

PREFIX = os.environ.get("ATL_DOCTOR_PREFIX")
APK = os.environ.get("ATL_DOCTOR_TEST_APK")


@pytest.mark.skipif(not (PREFIX and APK and Path(APK).is_file()),
                    reason="set ATL_DOCTOR_PREFIX and ATL_DOCTOR_TEST_APK to run against a real ATL")
def test_integration_record_report(tmp_path, capsys):
    out = tmp_path / "cap"
    steps = tmp_path / "s.steps"
    steps.write_text("wait 5\nmark Main\nwait 1\n")
    assert main(["record", "--steps", str(steps), "-o", str(out), "--prefix", PREFIX, APK]) == 0
    capsys.readouterr()
    assert main(["report", "-f", "json", str(out)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["format"] == 1 and doc["mode"] == "record"
