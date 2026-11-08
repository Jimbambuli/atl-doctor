# SPDX-License-Identifier: GPL-3.0-or-later
"""atl-doctor pack: cleaned and full archives, the rules, stable placeholders, the report on a cleaned archive."""
import json
import re
import shutil
import tarfile
from pathlib import Path

import pytest

from atl_doctor import pack as P
from atl_doctor.archive import unpack
from atl_doctor.cli import main

DATA = Path(__file__).parent / "data"
HOME, USER, HOST = "/home/tester", "tester", "testbox"
NAME = "org.example.demo-20260101-120000"


def san() -> P.Sanitizer:
    return P.Sanitizer(HOME, USER, HOST)


# -- rules -------------------------------------------------------------------------------------------------------
def test_paths_user_host():
    s = san()
    out = s.text("open /home/tester/.cache/x and /home/other/y on testbox, dalvik home@tester@.var, user tester")
    assert out == "open ~/.cache/x and ~/y on <host>, dalvik home@<user>@.var, user <user>"
    assert s.counts["home"] == 3 and s.counts["host"] == 1 and s.counts["user"] == 1
    assert "other" in s.users                      # a /home/NAME from another machine is found and replaced everywhere
    assert s.text("name other here") == "name <user> here"


def test_short_or_generic_names_are_not_replaced():
    s = P.Sanitizer("/root", "root", "localhost")
    assert s.text("root localhost /root/x") == "root localhost ~/x"


def test_ip_placeholders_are_stable_and_classified():
    s = san()
    out = s.text("a 192.168.1.5 b 10.0.0.7 c 192.168.1.5 d 8.8.8.8 e 8.8.4.4 f 8.8.8.8 g 127.0.0.1 h 224.0.0.1 i 0.0.0.0")
    assert out == "a <lan-ip-1> b <lan-ip-2> c <lan-ip-1> d <ip-1> e <ip-2> f <ip-1> g 127.0.0.1 h 224.0.0.1 i 0.0.0.0"
    assert (s.counts["lan-ip"], s.counts["ip"]) == (3, 3)


def test_ipv6_and_things_that_only_look_like_it():
    s = san()
    assert s.text("fe80::1ff:fe23:4567:890a x fd00:1:2::5 y 2606:4700:4700::1111 z ::1") == \
        "<lan-ip-1> x <lan-ip-2> y <ip-1> z ::1"
    assert s.text("time 16:25:22 at 2026:11:06 std::string a::b") == "time 16:25:22 at 2026:11:06 std::string a::b"


def test_version_numbers_are_not_addresses():
    s = san()
    text = '"version_name": "2.26.43.74" and v1.2.3.4 and app-1.2.3.4.apk and 1.2.3.4.5'
    assert s.text(text) == text


def test_mac_and_email_equal_values_equal_placeholders():
    s = san()
    out = s.text("aa:bb:cc:dd:ee:01 AA:BB:CC:DD:EE:01 11:22:33:44:55:66 mail Me@Example.org me@example.org you@example.org")
    assert out == "<mac-1> <mac-1> <mac-2> mail <email-1> <email-1> <email-2>"


def test_file_names_with_at_are_no_email():
    s = san()
    text = "ApkStarter@data@apps@org.example.splits@config.mdpi.apk@classes.dex"
    assert s.text(text) == text


def test_secrets():
    s = san()
    out = s.text("GET /d?token=abc123&x=1&key=K9&sid=77&keep=1 password=hunter2 password=null\n"
                 "Authorization: Bearer abcdefgh12345678\nCookie: a=b; c=d\n"
                 '{"password": "pw", "name": "n"} bearer ZZZZZZZZZZZZ')
    assert out == ("GET /d?token=<secret>&x=1&key=<secret>&sid=<secret>&keep=1 password=<secret> password=null\n"
                   "Authorization: <secret>\nCookie: <secret>\n"
                   '{"password": "<secret>", "name": "n"} bearer <secret>')
    assert "hunter2" not in out and "abc123" not in out
    assert s.counts["secret-param"] == 4 and s.counts["secret-header"] == 2 and s.counts["secret-json"] == 1


def test_account_names():
    s = san()
    out = s.text("Account {name=alice, type=com.google} and accountName=alice, accountName=bob")
    assert out == "Account {name=<account-1>, type=com.google} and accountName=<account-1>, accountName=<account-2>"


def test_edit_text_in_trees_is_emptied():
    s = san()
    xml = ('<node text="visible" class="android.widget.TextView" x="1"/>'
           '<node text="my secret" resource-id="a" class="android.widget.EditText"/>'
           '<node text="q" class="androidx.appcompat.widget.SearchView$SearchAutoComplete"/>')
    out = s.tree(xml)
    assert 'text="visible"' in out and "my secret" not in out and 'text="q"' not in out
    assert s.counts["edit-text"] == 2


def test_input_jsonl_full_recording_loses_its_texts():
    s = san()
    lines = [{"kind": "header", "full": True, "package": "p"},
             {"seq": 1, "kind": "text", "target": {"cls": "android.widget.EditText", "text": "typed", "id": "i"},
              "text": {"len": 5, "value": "typed"}},
             {"seq": 2, "kind": "tap", "target": {"cls": "android.widget.Button", "text": "OK", "id": "b"}}]
    out = [json.loads(x) for x in s.input_jsonl("\n".join(json.dumps(x) for x in lines) + "\n").strip().split("\n")]
    assert out[0]["full"] is False
    assert out[1]["text"] == {"len": 5} and out[1]["target"]["text"] == ""
    assert out[2]["target"]["text"] == "OK"
    assert s.counts["input-text"] == 2


# -- packing -----------------------------------------------------------------------------------------------------
@pytest.fixture
def cap(tmp_path):
    d = tmp_path / NAME
    shutil.copytree(DATA / "capture-atl", d)
    m = json.loads((d / "manifest.json").read_text())
    m["apk"] = f"{HOME}/apps/demo.apk"
    (d / "manifest.json").write_text(json.dumps(m))
    with open(d / "atl.log", "a") as f:
        f.write(f"connect 192.168.1.50 from {HOME}/x host testbox mac aa:bb:cc:dd:ee:ff\n"
                "GET https://api.example.org/v1?token=SECRETVALUE&a=1 user me@example.org public 93.184.216.34\n")
    (d / "armed").write_text("")
    (d / "trace/tr.stop").write_text("")
    (d / "trace/tr.counts").write_text("1\t2\n")
    (d / "logcat.txt").write_text("raw log\n")
    (d / "trace/tracecount.log").write_text("noise\n")
    shutil.copy(d / "trace/tr.threads", d / "trace/tr.time")
    return d


def run(cap, *extra):
    assert main(["pack", "--user", USER, "--home", HOME, "--host", HOST, *extra, str(cap)]) == 0


def names(arc):
    with tarfile.open(arc) as t:
        return sorted(m.name for m in t.getmembers())


def test_default_name_and_content(cap):
    run(cap)
    arc = cap.parent / f"{NAME}.tar.gz"
    assert arc.is_file() and not (cap.parent / f"{NAME}-FULL.tar.gz").exists()
    got = names(arc)
    assert f"{NAME}/SANITIZED.txt" in got
    for junk in ("armed", "trace/tr.stop", "trace/tr.counts", "logcat.txt", "trace/tracecount.log"):
        assert f"{NAME}/{junk}" not in got
    assert f"{NAME}/trace/tr.threads" in got and f"{NAME}/trace/tr.time" not in got   # equal content: first one stays
    out = cap.parent / "x"
    unpack(arc, out)
    text = "".join(p.read_text() for p in (out / NAME).rglob("*") if p.is_file())
    for leak in (HOME, "tester", "testbox", "192.168.1.50", "aa:bb:cc", "SECRETVALUE", "me@example.org", "93.184"):
        assert leak not in text
    log = (out / NAME / "atl.log").read_text()
    assert "<lan-ip-1>" in log and "<ip-1>" in log and "<mac-1>" in log and "<email-1>" in log and "token=<secret>" in log
    assert json.loads((out / NAME / "manifest.json").read_text())["apk"] == "~/apps/demo.apk"


def test_full_name_and_plain_content(cap):
    run(cap, "--full")
    arc = cap.parent / f"{NAME}-FULL.tar.gz"
    assert arc.is_file() and not (cap.parent / f"{NAME}.tar.gz").exists()
    assert f"{NAME}/armed" not in names(arc) and f"{NAME}/logcat.txt" not in names(arc)   # junk goes in both stages
    out = cap.parent / "x"
    unpack(arc, out)
    assert (out / NAME / "atl.log").read_bytes() == (cap / "atl.log").read_bytes()
    assert "No replacements" in (out / NAME / "SANITIZED.txt").read_text()


def test_sanitized_txt_golden_and_no_values(cap, assert_golden):
    run(cap)
    out = cap.parent / "x"
    unpack(cap.parent / f"{NAME}.tar.gz", out)
    note = (out / NAME / "SANITIZED.txt").read_text()
    assert HOME not in note and "SECRETVALUE" not in note and "example.org" not in note
    assert_golden(note, "pack-sanitized.txt")


def test_output_option_and_deterministic_archive(cap, tmp_path):
    a, b = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"
    run(cap, "-o", str(a))
    run(cap, "-o", str(b))
    assert a.read_bytes() == b.read_bytes()


def test_report_of_a_cleaned_archive_has_the_same_findings(cap, tmp_path, capsys):
    def findings(path):
        assert main(["report", "-f", "json", "--no-aosp-fetch", str(path)]) == 0
        j = json.loads(capsys.readouterr().out)
        return sorted(f["id"] for f in (j["findings"] if isinstance(j, dict) else j))
    before = findings(cap)
    assert before
    run(cap)
    capsys.readouterr()
    assert findings(cap.parent / f"{NAME}.tar.gz") == before


def test_not_a_capture(tmp_path, capsys):
    (tmp_path / "x").mkdir()
    assert main(["pack", str(tmp_path / "x")]) != 0
    assert "no manifest.json" in capsys.readouterr().err
