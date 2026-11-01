# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib

import pytest

from atl_doctor.findings import (Finding, Klass, Occurrence, from_dict, lookup, merge,
                                 normalize_text, rank, to_dict)


def test_id_and_short_id():
    f = Finding("stub", "android.graphics.drawable.Drawable#parseBlendMode", Klass.STUB_RESULT_USED, "t")
    assert f.id == "stub:android.graphics.drawable.Drawable#parseBlendMode"
    assert f.short_id == hashlib.sha1(f.id.encode()).hexdigest()[:8]


def test_normalize_text_makes_ids_stable():
    a = normalize_text("Index 17 out of bounds for length 3 at 0x7f3a2c00 (Foo@1a2b3c)")
    b = normalize_text("Index 4 out of bounds for length 9 at 0x55aa0010 (Foo@ffee01)")
    assert a == b == "Index N out of bounds for length N at X (Foo@X)"
    assert normalize_text("a.B$$ExternalSyntheticLambda3.run") == normalize_text("a.B$$ExternalSyntheticLambda0.run")
    assert normalize_text("a.B$1.run") == "a.B$N.run"


def test_weight_and_android_penalty():
    f = Finding("exc", "k", Klass.EXC_ATL_ONLY, "t")
    assert f.weight == 50
    f.on_android = True
    assert f.weight == 45


def test_rank_order():
    # class before count, then earlier phase, then id
    crash = Finding("crash", "signal:SIGSEGV", Klass.CRASH, "c", occurrences=[Occurrence("p2", 2, "atl")])
    stub = Finding("stub", "a#b", Klass.STUB_RESULT_USED, "s", occurrences=[Occurrence("p0", 0, "atl", 500)])
    jank1 = Finding("jank", "p1", Klass.JANK, "j", occurrences=[Occurrence("p1", 1, "atl", 3)])
    jank0 = Finding("jank", "p0", Klass.JANK, "j", occurrences=[Occurrence("p0", 0, "atl", 3)])
    assert [f.id for f in rank([jank1, stub, jank0, crash])] == [
        "crash:signal:SIGSEGV", "stub:a#b", "jank:p0", "jank:p1"]


def test_merge_same_id_combines_occurrences_and_keeps_highest_klass():
    a = Finding("exc", "k", Klass.OTHER, "t", occurrences=[Occurrence("p0", 0, "atl", 2)])
    b = Finding("exc", "k", Klass.EXC_ATL_ONLY, "t", occurrences=[Occurrence("p1", 1, "atl", 1)])
    (m,) = merge([a, b])
    assert m.klass is Klass.EXC_ATL_ONLY and m.total_count == 3 and m.first_phase_index == 0


def test_dict_roundtrip():
    f = Finding("tree", "id/name:height", Klass.TREE_VISIBLE, "t", "s",
                [Occurrence("p", 0, "atl", 1, "x")], {"atl": 20, "android": 48})
    d = to_dict(f)
    assert d["id"] == f.id and d["short_id"] == f.short_id and d["weight"] == 30
    assert d["weight_explained"] == {"class": "tree-visible", "rank": 30, "android_penalty": 0}
    assert from_dict(d) == f


def test_lookup_by_full_short_and_prefix():
    fs = [Finding("stub", "a#b", Klass.OTHER, "t"), Finding("stub", "a#c", Klass.OTHER, "t")]
    assert lookup(fs, "stub:a#b") is fs[0]
    assert lookup(fs, fs[1].short_id) is fs[1]
    # pick the shortest prefix (>= 4) that is unique
    n = 4
    while fs[0].short_id[:n] == fs[1].short_id[:n]:
        n += 1
    assert lookup(fs, fs[1].short_id[:n]) is fs[1]
    with pytest.raises(LookupError):
        lookup(fs, "nope")
