# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import re
from pathlib import Path

import pytest

from atl_doctor.cli import build_parser, main

MAN = Path(__file__).parent.parent / "doc" / "atl-doctor.1"
COMMANDS = ["record", "mark", "report", "diff", "explain", "check", "scan", "vergleich", "zustand"]


def _subparsers():
    top = build_parser()
    action = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    return top, action.choices


def _plain(text: str) -> str:
    """Man page text without troff escapes: \\- is -, font escapes are dropped."""
    text = text.replace("\\-", "-")
    return re.sub(r"\\f[BRIP]|\\fR|\\&", "", text)


def test_manpage_lists_every_subcommand_and_option():
    text = _plain(MAN.read_text(encoding="utf-8"))
    top, subs = _subparsers()
    assert sorted(subs) == sorted(COMMANDS)
    missing = []
    for name, sp in subs.items():
        if not re.search(rf"^\.B {name}$", text, re.M):
            missing.append(f"command {name}")
        for a in sp._actions:
            for opt in a.option_strings:
                if not re.search(rf"(?<![\w-]){re.escape(opt)}(?![\w-])", text):
                    missing.append(f"{name} {opt}")
    for opt in [o for a in top._actions for o in a.option_strings]:
        if not re.search(rf"(?<![\w-]){re.escape(opt)}(?![\w-])", text):
            missing.append(opt)
    assert not missing


def test_manpage_has_the_usual_sections():
    heads = re.findall(r"^\.SH (.+)$", MAN.read_text(encoding="utf-8"), re.M)
    for h in ("NAME", "SYNOPSIS", "DESCRIPTION", "ENVIRONMENT", "EXIT STATUS", "FILES", "SEE ALSO"):
        assert h in heads


@pytest.mark.parametrize("cmd", COMMANDS)
def test_help_per_subcommand(cmd, capsys):
    assert main([cmd, "--help"]) == 0
    assert "usage: atl-doctor " + cmd in capsys.readouterr().out
