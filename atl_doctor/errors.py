# SPDX-License-Identifier: GPL-3.0-or-later
"""Exceptions and exit codes."""

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_MISSING = 3
EXIT_FOUND = 4


class DoctorError(Exception):
    """An error reported as one line on stderr; `code` is the exit status."""
    code: int = EXIT_ERROR


class UsageError(DoctorError):
    code = EXIT_USAGE


class PrerequisiteMissing(DoctorError):
    code = EXIT_MISSING
