# SPDX-License-Identifier: GPL-3.0-or-later
from atl_doctor.findings import Finding, Klass, Occurrence
from atl_doctor.report import Report, render_text


def test_fix_first_excludes_other():
    r = Report("log", {}, [], ["p"], [Finding("stublog", "a:b", Klass.OTHER, "x"),
                                      Finding("missing", "a#b", Klass.MISSING, "y")])
    txt = render_text(r)
    fix = txt.split("Fix first")[1].split("\n\n")[0]
    assert "y" in fix and "  x  " not in fix


def test_empty_sections_omitted_and_order():
    r = Report("log", {}, [], ["p"], [Finding("crash", "signal:SIGSEGV", Klass.CRASH, "c")])
    txt = render_text(r)
    assert "Memory growth" not in txt and txt.index("Environment") < txt.index("Fix first") < txt.index("Crashes")


def test_exception_sides():
    e = Finding("exc", "k", Klass.EXC_ATL_ONLY, "boom", occurrences=[Occurrence("p", 0, "android")])
    assert "only Android" in render_text(Report("record", {}, [], ["p"], [e]))
