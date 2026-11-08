# SPDX-License-Identifier: GPL-3.0-or-later
"""The whole repository is English: no German word of a short check list in any file name or file."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THIS = Path(__file__).resolve()
GERMAN = {
    "vergleich", "zustand", "bedienung", "anfrage", "szenario", "bericht", "sichern", "laden", "tief", "datei",
    "fehler", "fehlt", "ohne", "erfasst", "ende", "und", "nicht", "nur", "der", "das", "weg", "leer", "wirft",
    "meldet", "konst", "hallo", "weiter", "suche", "schritt", "aufnahme", "ordner", "angabe", "agenten",
}


def _files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [ROOT / n for n in out.decode().split("\0") if n]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-zäöüß]+", text.lower()))


def test_no_german_word_in_any_name_or_file():
    hits = []
    for path in _files():
        if path == THIS or not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        found = _words(rel) & GERMAN
        try:
            found |= _words(path.read_text(encoding="utf-8")) & GERMAN
        except UnicodeDecodeError:
            pass
        if found:
            hits.append(f"{rel}: {sorted(found)}")
    assert not hits, "\n".join(hits)
