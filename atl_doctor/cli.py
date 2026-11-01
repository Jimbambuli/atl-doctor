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


def common_parser() -> argparse.ArgumentParser:
    """Parent parser giving every subcommand -q/-v."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="only errors on stderr")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more messages on stderr")
    return p


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would exit 2 itself with a different format
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    from . import env, record_atl, report
    from .sources import scan

    parser = _Parser(prog="atl-doctor", description="Diagnose why an Android app does not run correctly under ATL.",
                     parents=[common_parser()])
    parser.add_argument("--version", action="version", version=f"atl-doctor {__version__}")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser, metavar="COMMAND")
    for mod in (record_atl, report, scan, env):
        mod.register(sub)
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
