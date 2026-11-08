# SPDX-License-Identifier: GPL-3.0-or-later
"""--help: commands grouped by workflow, every argument explained, 80 columns."""
import argparse
import re

import pytest

from atl_doctor.cli import COMMAND_GROUPS, build_parser, main

COMMANDS = [c for _, names in COMMAND_GROUPS for c in names]


@pytest.fixture(autouse=True)
def _width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")


def _help(capsys, *argv) -> str:
    assert main([*argv, "-h"]) == 0
    return capsys.readouterr().out


def _subparsers():
    top = build_parser()
    return next(a for a in top._actions if isinstance(a, argparse._SubParsersAction)).choices


def test_main_help_golden(capsys, assert_golden):
    assert_golden(_help(capsys), "help-main.txt")


@pytest.mark.parametrize("cmd", COMMANDS)
def test_command_help_golden(cmd, capsys, assert_golden):
    assert_golden(_help(capsys, cmd), f"help-{cmd}.txt")


def test_main_help_groups_commands_in_workflow_order(capsys):
    out = _help(capsys)
    assert [t for t, _ in COMMAND_GROUPS] == ["Record", "Analyse", "Compare", "Setup"]
    assert [m.group(1) for m in re.finditer(r"^  ([a-z][a-z-]*) {2,}\S", out, re.M)] == COMMANDS
    assert out.count("\nExamples:\n") == 1
    assert len(re.findall(r"^  atl-doctor ", out.split("Examples:")[1], re.M)) == 3


@pytest.mark.parametrize("cmd", COMMANDS)
def test_help_fits_80_columns(cmd, capsys):
    for text in (_help(capsys), _help(capsys, cmd)):
        assert all(len(line) <= 80 for line in text.splitlines()), [l for l in text.splitlines() if len(l) > 80]


def test_every_argument_has_help_text():
    for name, sp in _subparsers().items():
        for a in sp._actions:
            assert a.help and "\n" not in a.help, f"{name}: {a.dest}"


def test_only_known_group_titles_in_this_order():
    order = ["Arguments", "Input", "Output", "ATL environment", "Advanced", "General"]
    for name, sp in _subparsers().items():
        titles = [g.title for g in sp._action_groups if g._group_actions]
        assert titles == sorted(titles, key=order.index), name


def test_defaults_are_readable(capsys):
    out = _help(capsys, "compare")
    assert "wait 15 s, then one screenshot" in " ".join(out.split()) and "\\n" not in out
