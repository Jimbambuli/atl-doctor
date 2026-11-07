# SPDX-License-Identifier: GPL-3.0-or-later
"""Static coverage scan: which framework APIs does an APK reference, and what has ATL?"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from ..env import atl_jars, libcore_jars, resolve
from ..errors import DoctorError, PrerequisiteMissing, UsageError
from ..findings import Finding, Klass, Occurrence, merge, to_dict
from .dex import PARSE_ERRORS, ApkInfo, Ref, World, read_apk, framework_refs, result_used_refs
from .stubs import STUB_KINDS, StubEntry, build_stub_list, stub_finding, stub_index

COMPOSE_MIN = 100
GMS_PREFIXES = ("com.google.android.gms.", "com.google.firebase.", "com.google.android.play.",
                "com.google.android.finsky.")
RATED_PREFIXES = ("android.", "com.android.")
RATED_EXCLUDED = ("android.support.",)
LIBCORE_PREFIXES = ("java.", "javax.", "dalvik.", "libcore.", "sun.", "org.json.", "org.xml.", "org.w3c.",
                    "org.xmlpull.", "org.apache.http.", "junit.", "org.junit.", "org.apache.harmony.")
ATL_MIN_SDK_INT = 9
# Stub kinds that count as "stub" in the coverage: bodies that do nothing useful.  `const` and the
# `-part` kinds are mostly real implementations; `native-missing` is a link problem, not a stub.
COUNTED_STUB_KINDS = STUB_KINDS - {"const", "returnsarg", "throws-part", "logs-part", "native-missing"}
LIMITS = ["Reflection is only detected through string constants.",
          "Referenced does not mean called at runtime."]
EXCL_GMS = "Google Play Services/Firebase"
EXCL_COMPOSE = "Jetpack Compose"
EXCL_ARM = "native libraries only for ARM"


@dataclass
class AtlState:
    name: str
    world: World
    libcore: World
    stubs: list[StubEntry]

    def __post_init__(self) -> None:
        self.merged = World()
        self.merged.classes = {**self.libcore.classes, **self.world.classes}
        self._stub_sigs = {(e.cls, e.method + e.sig) for e in self.stubs if e.sig and e.kind in COUNTED_STUB_KINDS}
        self._stub_names = {(e.cls, e.method) for e in self.stubs if not e.sig and e.kind in COUNTED_STUB_KINDS}


def load_state(name: str, atl_src: Path, jars: list[Path], libcore: list[Path]) -> AtlState:
    return AtlState(name, World.from_jars(jars), World.from_jars(libcore), build_stub_list(atl_src, jars))


@dataclass
class AppCoverage:
    package: str
    version_name: str | None
    refs_android: int
    implemented: int
    stub: int
    missing_member: int
    missing_class: int
    coverage_pct: float
    coverage_incl_stub_pct: float
    java_missing: int
    min_sdk: int | None
    target_sdk: int | None
    sdk_int: int
    target_above_sdk_int: bool
    abis: list[str]
    gms_classes: int
    compose_classes: int
    exclusions: list[str]


def _is_rated(cls: str) -> bool:
    return cls.startswith(RATED_PREFIXES) and not cls.startswith(RATED_EXCLUDED)


def _pct(a: int, b: int) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def _status(state: AtlState, ref: Ref) -> tuple[str, str | None]:
    """('impl'|'stub'|'missing-member'|'missing-class', declaring class)"""
    present = [c for c in (ref.cls, *ref.via) if state.merged.has_class(c)]
    for c in present:
        d = state.merged.declaring_class(c, ref.member)
        if d:
            name = ref.member.split("(")[0].split(":")[0]
            if (d, ref.member) in state._stub_sigs or (d, name) in state._stub_names:
                return "stub", d
            return "impl", d
    return ("missing-member" if present else "missing-class"), None


def sdk_int_for(min_sdk: int | None, override: int | None) -> int:
    return override if override is not None else max(min_sdk or ATL_MIN_SDK_INT, ATL_MIN_SDK_INT)


def scan_apk(apk: ApkInfo, state: AtlState, sdk_int: int | None) -> tuple[AppCoverage, list[Finding]]:
    refs = framework_refs(apk)
    used = result_used_refs(apk)
    package = apk.package or apk.path.name
    occ = [Occurrence("-", 0, "static", 1, package)]
    found: dict[str, Finding] = {}
    counts: Counter[str] = Counter()
    java_missing = 0
    family: dict[str, set[str]] = {}  # class -> every member the app references that ATL lacks

    def add(f: Finding) -> None:
        found.setdefault(f.id, f)

    def missing_class(cls: str) -> None:
        add(Finding("missing", cls, Klass.MISSING, f"missing class {cls}", f"Implement {cls} in ATL (src/api-impl).",
                    list(occ)))

    for r in sorted(refs, key=lambda r: (r.kind, r.cls, r.member)):
        classes = (r.cls, *r.via)
        rated = [c for c in classes if _is_rated(c)]
        if r.kind == "C":
            if rated and not state.merged.has_class(r.cls):
                missing_class(r.cls)
            continue
        if not rated:
            if any(c.startswith(LIBCORE_PREFIXES) for c in classes) and _status(state, r)[0].startswith("missing"):
                java_missing += 1
            continue
        status, decl = _status(state, r)
        counts[status] += 1
        name = r.member.split("(")[0].split(":")[0]
        if status == "stub":
            add(stub_finding(decl, name, state.stubs, (decl, name) in used or (r.cls, name) in used, list(occ), []))
        elif status == "missing-member":
            cls = rated[0]
            family.setdefault(cls, set()).add(r.member)
            add(Finding("missing", f"{cls}#{name}", Klass.MISSING, f"missing member {cls}#{r.member}",
                        f"Implement {cls}.{name} in ATL (src/api-impl).", list(occ)))
        elif status == "missing-class":
            missing_class(rated[0])

    for f in found.values():  # the whole family: the other members of this class that the app uses and ATL lacks
        if f.kind == "missing" and "#" in f.key:
            cls, name = f.key.split("#", 1)
            rest = sorted(m for m in family.get(cls, ()) if m.split("(")[0].split(":")[0] != name)
            if rest:
                f.details["also_missing_in_class"] = rest
    total = counts["impl"] + counts["stub"] + counts["missing-member"] + counts["missing-class"]
    gms = len({r.cls for r in refs if r.cls.startswith(GMS_PREFIXES)})
    compose = sum(1 for c in apk.app_classes() if c.startswith("Landroidx/compose/ui/"))
    excl = []
    if gms:
        excl.append(EXCL_GMS)
    if compose >= COMPOSE_MIN:
        excl.append(EXCL_COMPOSE)
    if apk.abis and not {"x86_64", "x86"} & set(apk.abis):
        excl.append(EXCL_ARM)
    sdk = sdk_int_for(apk.min_sdk, sdk_int)
    cov = AppCoverage(package, apk.version_name, total, counts["impl"], counts["stub"], counts["missing-member"],
                      counts["missing-class"], _pct(counts["impl"], total), _pct(counts["impl"] + counts["stub"], total),
                      java_missing, apk.min_sdk, apk.target_sdk, sdk,
                      apk.target_sdk is not None and apk.target_sdk > sdk, list(apk.abis), gms, compose, excl)
    return cov, list(found.values())


def scan_many(paths: list[Path], states: list[AtlState], sdk_int: int | None):
    from ..report import Report

    apps: dict[str, list[dict]] = {s.name: [] for s in states}
    per_state: dict[str, list[Finding]] = {s.name: [] for s in states}
    skipped: list[dict[str, str]] = []
    histogram: Counter[str] = Counter()
    for p in paths:
        try:
            apk = read_apk(Path(p))
        except DoctorError as e:
            skipped.append({"apk": Path(p).name, "reason": str(e)})
            continue
        try:  # the DEX is parsed here, a damaged one fails only now
            results = [(s, scan_apk(apk, s, sdk_int)) for s in states]
        except PARSE_ERRORS as e:
            skipped.append({"apk": Path(p).name, "reason": f"unreadable APK: {Path(p).name}: {e}"})
            continue
        if apk.target_sdk is not None:
            histogram[str(apk.target_sdk)] += 1
        for s, (cov, fs) in results:
            apps[s.name].append(asdict(cov))
            per_state[s.name] += fs
    merged = {name: merge(fs) for name, fs in per_state.items()}
    extras = {"apps": apps, "skipped": skipped,
              "target_sdk_histogram": dict(sorted(histogram.items(), key=lambda kv: int(kv[0]))),
              "limits": list(LIMITS)}
    if len(states) > 1:
        extras["state_findings"] = {n: [to_dict(f) for f in fs] for n, fs in list(merged.items())[1:]}
    env = {"states": ", ".join(s.name for s in states)}
    return Report("scan", env, [], [], merged[states[0].name], extras)


# ---------------------------------------------------------------- CLI
def _compare_state(src: Path, atl_src_libcore: list[Path]) -> AtlState:
    for build in sorted(src.glob("build*")):
        hits = sorted(build.rglob("api-impl.jar"))
        if hits:
            jars = [hits[0]] + [j for j in (hits[0].parent / "gstub.jar", hits[0].parent / "ghax.jar") if j.exists()]
            return load_state("compare", src, jars, atl_src_libcore)
    raise PrerequisiteMissing("build the compare tree first (meson compile)")


def _run_scan(args: argparse.Namespace) -> int:
    if args.format == "html" and not args.output:
        raise UsageError("-f html needs -o FILE")
    prefix = resolve(args.prefix, "ATL_DOCTOR_PREFIX")
    atl_src = resolve(args.atl_src, "ATL_DOCTOR_ATL_SRC")
    if not prefix:
        raise PrerequisiteMissing("scan needs the ATL installation: --prefix DIR or ATL_DOCTOR_PREFIX")
    if not atl_src:
        raise PrerequisiteMissing("scan needs the ATL source tree: --atl-src DIR or ATL_DOCTOR_ATL_SRC")
    jars, core = atl_jars(Path(prefix)), libcore_jars(Path(prefix))
    if not jars:
        raise PrerequisiteMissing(f"no ATL jars under {prefix}/lib64/java/dex/android_translation_layer")
    states = [load_state("main", Path(atl_src), jars, core)]
    if args.compare_src:
        states.append(_compare_state(Path(args.compare_src), core))
    rep = scan_many([Path(a) for a in args.apks], states, args.sdk_int)
    from ..report import apply_aosp, needs_aosp, render_html, render_json, render_text

    if needs_aosp(rep) or resolve(args.aosp, "ATL_DOCTOR_AOSP"):
        apply_aosp(rep, args.aosp, str(prefix), stub_index(states[0].stubs), states[0].world, no_fetch=args.no_aosp_fetch)

    text = {"text": render_text, "json": render_json, "html": render_html}[args.format](rep)
    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except OSError as e:
            raise DoctorError(f"cannot write {args.output}: {e.strerror or e}") from e
    else:
        sys.stdout.write(text)
    return 1 if rep.extras["skipped"] else 0


def register(sub: argparse._SubParsersAction) -> None:
    from ..cli import common_parser

    p = sub.add_parser("scan", help="static coverage of APKs against ATL (no start)", parents=[common_parser()])
    p.add_argument("-f", "--format", choices=["text", "json", "html"], default="text")
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of stdout")
    p.add_argument("--atl-src", metavar="DIR", help="ATL source tree (default: $ATL_DOCTOR_ATL_SRC)")
    p.add_argument("--prefix", metavar="DIR", help="ATL installation prefix (default: $ATL_DOCTOR_PREFIX)")
    p.add_argument("--compare-src", metavar="DIR", help="second ATL source tree (with build*/api-impl.jar) to compare")
    p.add_argument("--aosp", metavar="DIR", help="AOSP source tree (root or frameworks/base) to compare stubs and "
                                                 "missing methods with (default: $ATL_DOCTOR_AOSP)")
    p.add_argument("--no-aosp-fetch", action="store_true", help="do not download AOSP source when --aosp is not given (default: fetch once into the cache)")
    p.add_argument("--sdk-int", type=int, metavar="N", help="SDK_INT ATL reports (default: the app's minSdk, at least 9)")
    p.add_argument("apks", nargs="+", metavar="APK")
    p.set_defaults(func=_run_scan)
