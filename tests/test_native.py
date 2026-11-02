# SPDX-License-Identifier: GPL-3.0-or-later
from atl_doctor.findings import Klass
from atl_doctor.sources.native import native_findings

LOG = """\
E/linker  ( 1234): dlopen failed: library "libfoo.so" not found
java.lang.UnsatisfiedLinkError: dlopen failed: library "libfoo.so" not found
libbar.so: cannot open shared object file: No such file or directory
CANNOT LINK EXECUTABLE: cannot locate symbol "AAssetManager_open" referenced by "libgame.so"
symbol lookup error: /x/libgame.so: undefined symbol: ANativeWindow_lock
W/EGL     ( 1234): eglCreateWindowSurface failed: EGL_BAD_NATIVE_WINDOW
E/AAudio  ( 1234): AAudioStream_requestStart() returned AAUDIO_ERROR_INTERNAL
ALSA lib pcm.c:2722:(snd_pcm_open_noupdate) Unknown PCM default
I/Other: everything fine
"""


def _by_key(fs):
    return {f.key: f for f in fs}


def test_native_findings_kinds_and_counts():
    fs = _by_key(native_findings(LOG, "atl"))
    assert fs["dlopen:libfoo.so"].total_count == 2 and fs["dlopen:libfoo.so"].klass == Klass.MISSING
    assert "dlopen:libbar.so" in fs
    assert "symbol:AAssetManager_open" in fs and "symbol:ANativeWindow_lock" in fs
    assert "egl:EGL_BAD_NATIVE_WINDOW" in fs
    assert "audio:AAudio" in fs and "audio:ALSA" in fs
    assert all(f.kind == "native" for f in fs.values()) and len(fs) == 7


def test_native_findings_nothing_in_a_clean_log():
    assert native_findings("I/Other: everything fine\nE/Foo: some error\n", "atl") == []


def test_native_findings_side_and_example_line():
    f = native_findings('dlopen failed: library "libfoo.so" not found\n', "android")[0]
    assert f.occurrences[0].side == "android"
    assert "libfoo.so" in f.details["line"]
