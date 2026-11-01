# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from atl_doctor.cli import main

DATA = Path(__file__).parent / "data"


def test_report_log_text_golden(capsys, assert_golden):
    assert main(["report", "--log", str(DATA / "logs/plain.log")]) == 0
    assert_golden(capsys.readouterr().out, "log-plain.txt")


def test_usage_error_exit_2(capsys):
    assert main(["report"]) == 2
    assert capsys.readouterr().err.startswith("atl-doctor: ")


def test_unknown_subcommand_exit_2(capsys):
    assert main(["frobnicate"]) == 2
    assert main([]) == 2


def test_missing_log_file_exit_1(capsys):
    assert main(["report", "--log", "/nonexistent.log"]) == 1
    assert "atl-doctor: " in capsys.readouterr().err


def test_help_exit_0(capsys):
    assert main(["report", "--help"]) == 0


def test_report_log_with_atl_src(capsys):
    assert main(["report", "--log", str(DATA / "logs/plain.log"), "--atl-src", str(DATA / "atl-src")]) == 0
    out = capsys.readouterr().out
    assert "stub android.content.res.AssetManager#destroy" in out
    assert "AssetManager: AssetManager.destroy()" not in out


def test_report_log_atl_src_from_environment(capsys, monkeypatch):
    monkeypatch.setenv("ATL_DOCTOR_ATL_SRC", str(DATA / "atl-src"))
    assert main(["report", "--log", str(DATA / "logs/plain.log")]) == 0
    assert "stub android.content.res.AssetManager#destroy" in capsys.readouterr().out
    assert main(["report", "--log", str(DATA / "logs/plain.log"), "--atl-src", "/nonexistent"]) == 1
