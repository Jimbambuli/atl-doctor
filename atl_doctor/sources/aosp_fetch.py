# SPDX-License-Identifier: GPL-3.0-or-later
"""Real AOSP source on demand: a sparse, shallow clone of platform/frameworks/base into the user cache.

The "like AOSP" comparison always reads AOSP source.  Without `--aosp DIR` the part that is needed (the
`java/` directories of frameworks/base, no tests) is cloned once per release tag into
`$XDG_CACHE_HOME/atl-doctor/aosp/<tag>/frameworks/base` (default `~/.cache/...`) and reused offline afterwards.
The tag follows the target API level; libcore is not needed (the comparison only reads frameworks/base).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import DoctorError

DEFAULT_URL = "https://android.googlesource.com/platform/frameworks/base"
DEFAULT_SDK = 34

# API level -> tag series.  Source: https://source.android.com/docs/setup/reference/build-numbers
# (codenames/tags: "android-<release>.0.0_r<N>"; the tags are listed by `git ls-remote --tags` of the repository).
SDK_SERIES = {
    36: "android-16.0.0_r",
    35: "android-15.0.0_r",
    34: "android-14.0.0_r",
    33: "android-13.0.0_r",
    32: "android-12.1.0_r",
    31: "android-12.0.0_r",
    30: "android-11.0.0_r",
    29: "android-10.0.0_r",
}
# non-cone sparse patterns: every java/ directory except tests
SPARSE = ["java/", "!/tests/", "!/**/tests/"]


class FetchError(DoctorError):
    pass


@dataclass
class Fetched:
    path: Path      # root with frameworks/base below
    tag: str
    fetched: bool   # False: found in the cache


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "atl-doctor" / "aosp"


def url() -> str:
    return os.environ.get("ATL_DOCTOR_AOSP_URL") or DEFAULT_URL


def tag_series(sdk: int) -> str | None:
    """Series for the SDK level: the level itself, else the next lower known one; newer than the table: the newest;
    older than the table: None."""
    if sdk > max(SDK_SERIES):
        return SDK_SERIES[max(SDK_SERIES)]
    lower = [k for k in SDK_SERIES if k <= sdk]
    return SDK_SERIES[max(lower)] if lower else None


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=1800,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError as e:
        raise FetchError("git is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise FetchError("git timed out") from e
    if r.returncode:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise FetchError(f"git {args[0]} failed: {' '.join(lines[:2])}"[:300])
    return r.stdout


def latest_tag(repo: str, sdk: int) -> str:
    """Highest `_rN` tag of the series for `sdk`; lower series if the repository has none for it."""
    out = _git("ls-remote", "--tags", "--refs", repo)
    tags = [ln.split("refs/tags/", 1)[1] for ln in out.splitlines() if "refs/tags/" in ln]
    for level in sorted((k for k in SDK_SERIES if k <= sdk), reverse=True):
        series = SDK_SERIES[level]
        found = [(int(m.group(1)), t) for t in tags if series and (m := re.fullmatch(re.escape(series) + r"(\d+)", t))]
        if found:
            return max(found)[1]
    raise FetchError(f"no tag for SDK {sdk} in {repo}")


def _cached(sdk: int) -> Fetched | None:
    """Newest complete cache entry of the series for sdk (offline use)."""
    series = tag_series(sdk)
    root = cache_root()
    if not series or not root.is_dir():
        return None
    done = [(int(m.group(1)), p) for p in root.iterdir()
            if (m := re.fullmatch(re.escape(series) + r"(\d+)", p.name)) and (p / ".complete").is_file()]
    return Fetched(max(done)[1], max(done)[1].name, False) if done else None


def _clone(repo: str, tag: str, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    fb = tmp / "frameworks/base"
    fb.parent.mkdir(parents=True)
    try:
        _git("clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout", "--branch", tag, repo, str(fb))
        _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=fb)
        _git("checkout", "-q", cwd=fb)
        (tmp / ".complete").write_text(tag + "\n")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def ensure_aosp(sdk: int | None = None) -> Fetched:
    """Cached tree for the SDK level (default 34); clones it when not cached.  FetchError if that is impossible."""
    sdk = sdk or DEFAULT_SDK
    hit = _cached(sdk)
    if hit:
        return hit
    repo = url()
    try:
        tag = latest_tag(repo, sdk)
        dest = cache_root() / tag
        if not (dest / ".complete").is_file():
            _clone(repo, tag, dest)
        return Fetched(dest, tag, True)
    except FetchError as e:
        raise FetchError(f"{e} (SDK {sdk}, {repo})") from e
