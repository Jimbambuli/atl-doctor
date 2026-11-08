# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line entry point: argparse with one subcommand per module `register()`."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE, DoctorError, UsageError

_verbosity = 0  # -1 quiet, 0 normal, 1 verbose


def log(level: int, msg: str) -> None:
    """Message on stderr. level 0: always (unless -q), 1: only with -v."""
    if _verbosity < 0 or level > _verbosity:
        return
    print(f"atl-doctor: {msg}", file=sys.stderr)


class _Formatter(argparse.HelpFormatter):
    """Texts wrapped to the terminal width, option texts in one column."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=26)


class _RawFormatter(_Formatter, argparse.RawDescriptionHelpFormatter):
    """The same, but description and epilog (the command list, the examples) are laid out by hand."""


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kw) -> None:
        kw.setdefault("formatter_class", _Formatter)
        kw.setdefault("add_help", False)  # -h goes last, into the "General" group (see finish)
        super().__init__(*args, **kw)

    def error(self, message: str):  # argparse would exit 2 itself with a different format
        raise UsageError(message)


class Groups:
    """The argument groups every command help is built from, in this order (empty ones are not printed)."""

    def __init__(self, p: argparse.ArgumentParser) -> None:
        self.args = p.add_argument_group("Arguments")
        self.input = p.add_argument_group("Input")
        self.output = p.add_argument_group("Output")
        self.atl = p.add_argument_group("ATL environment")
        self.advanced = p.add_argument_group("Advanced")
        self.general = p.add_argument_group("General")


def command(sub, name: str, help: str, description: str, aliases: tuple[str, ...] = ()) -> tuple[_Parser, Groups]:
    """Add a subcommand; `aliases` are old names that still work but do not show in --help."""
    p = sub.add_parser(name, help=help, description=description, aliases=list(aliases))
    return p, Groups(p)


def done(p: argparse.ArgumentParser, g: Groups, func) -> None:
    """Close a command: -h/-q/-v into General and the function to run."""
    finish(g.general)
    p.set_defaults(func=func)


def alias(group, old: str, new: str) -> None:
    """Accept the old option name `old` for the option `new` of `group`, without showing it in --help."""
    act = next(a for a in group._group_actions if new in a.option_strings)
    kw: dict = {"dest": act.dest, "default": act.default, "help": argparse.SUPPRESS}
    if act.nargs == 0:
        kw["action"] = "store_true" if act.const is True else "store_false"
    else:
        kw.update(type=act.type, metavar=act.metavar)
    group.add_argument(old, **kw)


def finish(g) -> None:
    """-h, -q and -v last, into the group "General" `g`."""
    g.add_argument("-h", "--help", action="help", help="show this help and exit")
    g.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="only errors on stderr")
    g.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more messages on stderr")


COMMAND_GROUPS = (
    ("Record", ("record", "mark", "pack")),
    ("Analyse", ("report", "explain", "diff", "scan", "scenario-from")),
    ("Compare", ("compare", "state")),
    ("Setup", ("check",)),
)

EXAMPLES = (
    ("atl-doctor check", "what can be measured on this machine"),
    ("atl-doctor record -o run1 app.apk", "record until the app ends (or Ctrl-C)"),
    ("atl-doctor report run1", "text report of the recording on stdout"),
)


def _overview(sub) -> str:
    """The commands grouped by workflow, one line each; the texts are the `help` of the registered commands."""
    texts = {a.dest: a.help for a in sub._choices_actions}
    listed = [n for _, names in COMMAND_GROUPS for n in names]
    assert sorted(listed) == sorted(texts), "COMMAND_GROUPS and the registered commands differ"
    lines = ["Diagnose why an Android app does not run correctly under ATL.", ""]
    for title, names in COMMAND_GROUPS:
        lines.append(f"{title}:")
        lines += [f"  {n:<15} {texts[n]}" for n in names]
        lines.append("")
    return "\n".join(lines).rstrip()


def build_parser() -> argparse.ArgumentParser:
    from . import bedienung, diff, env, pack, record_atl, report, vergleich, zustand
    from .sources import scan

    parser = _Parser(prog="atl-doctor", formatter_class=_RawFormatter)
    sub = parser.add_subparsers(dest="command", parser_class=_Parser, metavar="COMMAND", help=argparse.SUPPRESS)
    for mod in (record_atl, report, diff, scan, env, vergleich, zustand, bedienung, pack):
        mod.register(sub)
    parser.usage = "atl-doctor [-h] [-q] [-v] [--version] COMMAND ..."
    parser.description = _overview(sub)
    width = max(len(c) for c, _ in EXAMPLES)
    parser.epilog = "Examples:\n" + "\n".join(f"  {c:<{width}}  # {t}" for c, t in EXAMPLES) + \
        "\n\nAll options of a command: atl-doctor COMMAND -h"
    g = parser.add_argument_group("General")
    finish(g)
    g.add_argument("--version", action="version", version=f"atl-doctor {__version__}", help="show the version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _verbosity
    try:
        args = build_parser().parse_args(argv)
        if not args.command:
            raise UsageError("no command given (try --help)")
        _verbosity = -1 if getattr(args, "quiet", False) else 1 if getattr(args, "verbose", False) else 0
        rc = args.func(args)
        return EXIT_OK if rc is None else rc
    except SystemExit as e:  # --help / --version
        return e.code if isinstance(e.code, int) else EXIT_OK
    except DoctorError as e:
        print(f"atl-doctor: {e}", file=sys.stderr)
        return e.code
    except BrokenPipeError:
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
