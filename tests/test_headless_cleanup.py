# SPDX-License-Identifier: GPL-3.0-or-later
"""The session directory is removed without ever touching a file system mounted below it (the Flatpak document
portal mounts the user's real documents at XDG_RUNTIME_DIR/doc)."""
import os
import subprocess
from pathlib import Path

import pytest

from atl_doctor import headless
from atl_doctor.errors import DoctorError


def _session_in_tmp(tmp_path, monkeypatch) -> headless.HeadlessSession:
    """A session whose directory is a temporary one with a link in the capture (as with a long capture path)."""
    monkeypatch.setattr(headless, "SOCKET_PATH_MAX", 0)
    monkeypatch.setattr(headless.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "tmp-session"))
    (tmp_path / "tmp-session").mkdir()
    s = headless.HeadlessSession(tmp_path / "capture-run")
    s.proc = subprocess.Popen(["true"], start_new_session=True)
    s.proc.wait()
    return s


def test_stop_leaves_a_foreign_file_system_below_the_session_untouched(tmp_path, monkeypatch):
    """doc/ stands for the portal's FUSE mount: another device, not listed (mount namespace) or not unmountable."""
    s = _session_in_tmp(tmp_path, monkeypatch)
    doc = s.run_dir / "doc"
    (doc / "by-app").mkdir(parents=True)
    precious = doc / "by-app" / "thesis.odt"
    precious.write_text("real document")
    (s.run_dir / "kwin.log").write_text("log")
    real_lstat = os.lstat

    class Foreign:  # os.stat_result of the mount: same as the real one but on another device
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return self._st.st_dev + 1 if name == "st_dev" else getattr(self._st, name)

    def lstat(p, *a, **k):
        st = real_lstat(p, *a, **k)
        return Foreign(st) if Path(p) == doc or doc in Path(p).parents else st

    monkeypatch.setattr(headless.os, "lstat", lstat)
    monkeypatch.setattr(headless, "_mounts_below", lambda path: [], raising=False)
    s.stop()
    assert precious.read_text() == "real document"
    assert (tmp_path / "capture-run" / "kwin.log").read_text() == "log"  # the logs came back into the capture


def test_stop_removes_nothing_when_a_mount_below_cannot_be_unmounted(tmp_path, monkeypatch, capsys):
    s = _session_in_tmp(tmp_path, monkeypatch)
    doc = s.run_dir / "doc"
    doc.mkdir()
    (doc / "a.txt").write_text("x")
    (s.run_dir / "other").write_text("y")
    calls = []
    monkeypatch.setattr(headless, "_mounts_below", lambda path: [str(doc)])
    monkeypatch.setattr(headless.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1))
    s.stop()
    assert (tmp_path / "tmp-session" / "doc" / "a.txt").read_text() == "x"
    assert (tmp_path / "tmp-session" / "other").read_text() == "y"
    assert calls and calls[0][:2] == ["fusermount3", "-u"]
    assert "still mounted" in capsys.readouterr().err


def test_remove_tree_unmounts_first_and_then_removes(tmp_path, monkeypatch):
    d = tmp_path / "s"
    (d / "doc").mkdir(parents=True)
    (d / "f").write_text("z")
    mounted = [str(d / "doc")]
    monkeypatch.setattr(headless, "_mounts_below", lambda path: list(mounted))

    def run(cmd, **k):
        mounted.clear()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(headless.subprocess, "run", run)
    headless.remove_tree(d)
    assert not d.exists()


def test_mounts_below_reads_mountinfo(tmp_path, monkeypatch):
    info = tmp_path / "mountinfo"
    info.write_text("22 1 0:20 / / rw - ext4 /dev/x rw\n"
                    f"40 22 0:50 / {tmp_path}/s/doc rw - fuse.portal portal rw\n"
                    f"41 22 0:51 / {tmp_path}/s/my\\040dir rw - tmpfs t rw\n"
                    f"42 22 0:52 / {tmp_path}/sibling rw - tmpfs t rw\n")
    monkeypatch.setattr(headless, "MOUNTINFO", str(info))
    assert headless._mounts_below(tmp_path / "s") == [f"{tmp_path}/s/my dir", f"{tmp_path}/s/doc"]


def test_session_bus_has_a_document_store_of_its_own(tmp_path, monkeypatch):
    """A document portal the session bus starts must not read the user's document store (XDG_DATA_HOME)."""
    seen = {}

    class P:
        pid = 1

        def __init__(self, cmd, env, **k):
            seen.update(env)

        def poll(self):
            return 1

    monkeypatch.setattr(headless.subprocess, "Popen", P)
    monkeypatch.setattr(headless, "START_TIMEOUT_S", 0)
    monkeypatch.setenv("XDG_DATA_HOME", "/home/someone/.local/share")
    s = headless.HeadlessSession(tmp_path / "r")
    monkeypatch.setattr(s, "stop", lambda: None)
    with pytest.raises(DoctorError):
        s.start()
    assert seen["XDG_DATA_HOME"] == str(s.run_dir / "data")


def test_stop_unmounts_a_portal_left_in_the_capture(tmp_path, monkeypatch):
    """A session directory in the capture is kept, but no mount may stay in it for a later rm -rf."""
    s = headless.HeadlessSession(tmp_path / "r")
    s.proc = subprocess.Popen(["true"], start_new_session=True)
    s.proc.wait()
    mounted = [str(s.run_dir / "doc")]
    calls = []
    monkeypatch.setattr(headless, "_mounts_below", lambda path: list(mounted))

    def run(cmd, **k):
        calls.append(cmd)
        mounted.clear()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(headless.subprocess, "run", run)
    s.stop()
    assert calls == [["fusermount3", "-u", str(s.run_dir / "doc")]]
