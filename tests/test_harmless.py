# SPDX-License-Identifier: GPL-3.0-or-later
from atl_doctor.findings import Finding, Klass, Occurrence
from atl_doctor.report import Report, render_explain, render_html, render_json, render_text, load_json
from atl_doctor.sources.exceptions import compare_sides
from atl_doctor.sources.harmless import HARMLESS, mark_harmless, match


def exc(typ, where, catcher, msg="m", side="atl", n=1):
    f = Finding("exc", f"{typ}@{where}", Klass.OTHER, f"{typ}: {msg}", occurrences=[Occurrence("p", 0, side, n)],
                details={"message": msg, "catcher": catcher, "stack": [where], "app_frame": ""})
    return f


def nfe(n=5, side="atl"):
    return exc("java.lang.NumberFormatException", "java.lang.Integer.parseInt", "android.content.res.Resources.getIdentifier",
               'For input string: "system_neutral1_10"', side, n)


def test_every_entry_has_reason_and_https_source():
    assert len({h.id for h in HARMLESS}) == len(HARMLESS)
    for h in HARMLESS:
        assert h.reason.strip() and h.source.startswith("https://") and h.type and h.catchers


def test_match_by_type_and_catcher_not_by_message():
    assert match(nfe()).id == "getidentifier-numeric"
    other = exc("java.lang.NumberFormatException", "java.lang.Integer.parseInt", "org.example.App.parse")
    assert match(other) is None                       # same exception, other catcher: a real finding
    assert match(Finding("crash", "x", Klass.CRASH, "x")) is None


def test_message_pattern_limits_errno_entry():
    e = lambda m: exc("android.system.ErrnoException", "libcore.io.Linux.connect", "libcore.io.IoBridge.connectErrno", m)
    assert match(e("connect failed: EINPROGRESS (Operation now in progress)")) is not None
    assert match(e("connect failed: ECONNREFUSED (Connection refused)")) is None


def test_mark_takes_harmless_out_of_the_ranking_after_compare():
    atl = [nfe(100)]
    atl[0].klass = Klass.OTHER
    out = mark_harmless(compare_sides(atl, []))       # ATL only would be EXC_ATL_ONLY
    assert out[0].klass is Klass.OTHER and out[0].details["known_harmless"]["id"] == "getidentifier-numeric"
    assert out[0].weight == 0


def test_weight_never_negative_for_android_seen_other():
    f = exc("java.lang.IllegalStateException", "a.B.c", "a.B.d")
    f.on_android = True
    assert f.weight == 0


def _report(findings):
    return Report("record", {}, [], ["p"], findings)


def test_text_summarises_harmless_and_android_noise():
    own = exc("java.lang.IllegalStateException", "a.B.c", "a.B.d", "real", n=2)
    own.klass = Klass.EXC_ATL_ONLY
    both = exc("java.lang.RuntimeException", "a.B.e", "a.B.f", "both", n=4)
    both.on_android = True
    both.occurrences.append(Occurrence("p", 0, "android", 3))
    txt = render_text(_report(mark_harmless([own, both, nfe(7)])))
    assert "IllegalStateException: real" in txt                          # real finding shown in full
    assert "also on Android (comparable frequency, weight 0): 1 findings, 4 events on ATL" in txt
    assert "[ATL 4x / Android 3x]" in txt
    assert "known harmless: 7 events in 1 findings (getidentifier-numeric 7x)" in txt
    assert "NumberFormatException: For input" not in txt                 # the harmless one is not listed on its own


def test_json_and_html_keep_the_reason_and_source():
    r = _report(mark_harmless([nfe(3)]))
    d = load_json(render_json(r))
    k = d.findings[0].details["known_harmless"]
    assert k["id"] == "getidentifier-numeric" and k["source"].startswith("https://android.googlesource.com/")
    h = render_html(r)
    assert "Known harmless: 1 findings" in h and "ResourcesImpl.java" in h
    assert "known harmless (getidentifier-numeric)" in render_explain(r.findings[0], r, None)


def test_atl_three_times_as_often_stays_visible():
    atl = exc("java.lang.RuntimeException", "a.B.e", "a.B.f", n=9)
    android = exc("java.lang.RuntimeException", "a.B.e", "a.B.f", n=3, side="android")
    out = compare_sides([atl], [android])
    assert out[0].klass is Klass.EXC_ATL_ONLY and out[0].on_android
    assert "also on Android" not in render_text(_report(out))
