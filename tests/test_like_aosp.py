# SPDX-License-Identifier: GPL-3.0-or-later
"""Stub hits that are just as trivial in AOSP are not ATL gaps: marked "like AOSP", left out of the stub counts."""
import json
from pathlib import Path

from atl_doctor.findings import Finding, Klass, Occurrence
from atl_doctor.sources.aosp import attach_aosp, open_aosp
from atl_doctor.sources.stubs import build_stub_list, load_stub_tsv, save_stub_tsv, stub_index, trivial_pattern

DATA = Path(__file__).parent / "data"
V = "android.view.View"


def _entries():
    return build_stub_list(DATA / "like-atl-src", [])


def _findings(*methods):
    return [Finding("stub", f"{V}#{m}", Klass.STUB_RESULT_USED, f"stub {V}#{m}",
                    occurrences=[Occurrence("p", 0, "atl", 3)]) for m in methods]


ALL = ("isInEditMode", "onApplyWindowInsets", "isShown", "getScaleMode", "onFinishInflate", "mutate", "layout")


def test_trivial_pattern():
    assert trivial_pattern(" return false; ") == ("empty0", "false")
    assert trivial_pattern(" return ( 1 ); ") == ("const", "1")
    assert trivial_pattern(" return this; ") == ("emptythis", "")
    assert trivial_pattern(" return b; ", "int a, int b") == ("returnsarg", "1")
    assert trivial_pattern("      \n ") == ("empty", "")
    assert trivial_pattern(" x = 1; ") is None
    assert trivial_pattern(' throw new UnsupportedOperationException(); ') is None


def test_ret_is_in_the_stub_list_and_survives_tsv(tmp_path):
    e = {(x.cls, x.method): x for x in _entries()}
    assert (e[(V, "isInEditMode")].kind, e[(V, "isInEditMode")].ret) == ("empty0", "false")
    assert e[(V, "getScaleMode")].ret == "1" and e[(V, "onApplyWindowInsets")].ret == "0"
    save_stub_tsv(list(e.values()), tmp_path / "s.tsv")
    assert load_stub_tsv(tmp_path / "s.tsv") == list(e.values())


def test_old_stub_list_without_ret_column_still_loads(tmp_path):
    (tmp_path / "s.tsv").write_text("kind\tclass\tmethod\tsig\tlocation\tbytecode_kind\tcomment\n"
                                    "empty0\ta.B\tc\t()Z\tx:1\t\t\n")
    (e,) = load_stub_tsv(tmp_path / "s.tsv")
    assert e.ret == "" and e.kind == "empty0"


def test_aosp_comparison_marks_identical_trivial_methods():
    fs = _findings(*ALL)
    attach_aosp(fs, open_aosp(DATA / "like-aosp"), None, stub_index(_entries()))
    like = {f.key.split("#")[1]: f.details.get("like_aosp", False) for f in fs}
    assert like == {"isInEditMode": True, "onApplyWindowInsets": True, "mutate": True, "onFinishInflate": True,
                    "isShown": False,        # real code in AOSP
                    "getScaleMode": False,   # AOSP returns 2, ATL 1
                    "layout": False}         # overload (int,int) is real code in AOSP, no signature: not provable
    assert all(f.klass is Klass.OTHER for f in fs if f.details.get("like_aosp"))
    assert all(f.klass is Klass.STUB_RESULT_USED for f in fs if not f.details.get("like_aosp"))


def test_signature_picks_the_overload():
    fs = [Finding("stub", f"{V}#layout", Klass.STUB_RESULT_USED, "t", details={"sigs": ["(I)V"]})]
    st = stub_index([e for e in _entries() if e.method == "layout" and False])
    es = [e for e in _entries() if e.method == "layout"]
    sig = {1: "(I)V", 2: "(II)V"}
    es = [e.__class__(e.kind, e.cls, e.method, sig[1], e.location, e.bytecode_kind, e.comment, e.ret) for e in es[:1]]
    attach_aosp(fs, open_aosp(DATA / "like-aosp"), None, stub_index(es))
    assert st == {} and fs[0].details["like_aosp"] is True


def test_unknown_value_is_conservative():
    es = [e.__class__(e.kind, e.cls, e.method, e.sig, e.location, e.bytecode_kind, e.comment, "")
          for e in _entries() if e.method in ("getScaleMode", "isInEditMode")]
    fs = _findings("getScaleMode", "isInEditMode")
    attach_aosp(fs, open_aosp(DATA / "like-aosp"), None, stub_index(es))
    assert [f.details.get("like_aosp", False) for f in fs] == [False, True]   # zero values depend on the type only


def _report():
    from atl_doctor.report import Report

    fs = _findings("isInEditMode", "getScaleMode")
    attach_aosp(fs, open_aosp(DATA / "like-aosp"), None, stub_index(_entries()))
    return Report("record", {}, [], ["p"], fs)


def test_text_report_leaves_them_out_and_notes_them():
    from atl_doctor.report import render_text

    out = render_text(_report())
    hit = out.split("Hit stubs")[1].split("\n\n")[0]
    assert "getScaleMode" in hit and "isInEditMode" not in hit
    assert "Fix first" in out and "isInEditMode" not in out.split("Fix first")[1].split("\n\n")[0]
    notes = out.split("Notes")[-1]
    assert "like AOSP" in notes and "android.view.View#isInEditMode" in notes and "1 stub" in notes


def test_html_report_leaves_them_out_and_notes_them():
    from atl_doctor.report import render_html

    out = render_html(_report())
    assert "like AOSP" in out and out.count("isInEditMode") == 1   # only the note


def test_json_report_keeps_them_with_flag():
    from atl_doctor.report import render_json

    fl = {f["key"]: f for f in json.loads(render_json(_report()))["findings"]}
    assert fl[f"{V}#isInEditMode"]["details"]["like_aosp"] is True and fl[f"{V}#isInEditMode"]["class"] == "other"
    assert "like_aosp" not in fl[f"{V}#getScaleMode"]["details"]


def test_explain_says_like_aosp():
    from atl_doctor.report import render_explain

    r = _report()
    assert "like AOSP" in render_explain(r.findings[0], r, None)
