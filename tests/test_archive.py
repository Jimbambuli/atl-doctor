# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tarfile
from pathlib import Path

from atl_doctor.cli import main

DATA = Path(__file__).parent / "data"
CAP = DATA / "capture-atl"


def _tgz(path: Path, members: dict[str, Path]) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for name, src in members.items():
            t.add(src, arcname=name)
    return path


def test_archive_with_one_capture_equals_directory(tmp_path, capsys):
    arc = _tgz(tmp_path / "Archiv.tar.gz", {"app-1": CAP})
    assert main(["report", "-f", "json", str(CAP)]) == 0
    want = json.loads(capsys.readouterr().out)["findings"]
    assert main(["report", "-f", "json", str(arc)]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == want


def test_starter_archive_with_nested_captures(tmp_path, capsys):
    inner = _tgz(tmp_path / "app-2.tar.gz", {"app-2": CAP})
    arc = _tgz(tmp_path / "Archiv.tar.gz", {"app-1": CAP, "app-2.tar.gz": inner})
    assert main(["report", str(arc)]) == 0
    out = capsys.readouterr().out
    assert "== app-1" in out and "== app-2" in out


def test_archive_html_writes_one_file_per_capture(tmp_path):
    inner = _tgz(tmp_path / "app-2.tar.gz", {"app-2": CAP})
    arc = _tgz(tmp_path / "Archiv.tar.gz", {"app-1": CAP, "app-2.tar.gz": inner})
    out = tmp_path / "html"
    assert main(["report", "-f", "html", "-o", str(out), str(arc)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["app-1.html", "app-2.html"]


def test_archive_without_capture_is_an_error(tmp_path, capsys):
    (tmp_path / "x.txt").write_text("x")
    arc = _tgz(tmp_path / "leer.tar.gz", {"x.txt": tmp_path / "x.txt"})
    assert main(["report", str(arc)]) == 1
    assert "no capture" in capsys.readouterr().err


def test_archive_member_outside_is_rejected(tmp_path, capsys):
    (tmp_path / "x.txt").write_text("x")
    arc = _tgz(tmp_path / "boese.tar.gz", {"../x.txt": tmp_path / "x.txt"})
    assert main(["report", str(arc)]) == 1
