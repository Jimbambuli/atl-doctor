# SPDX-License-Identifier: GPL-3.0-or-later
"""Known harmless exceptions: platform code throws them on purpose and catches them itself.

An entry matches a caught exception by type and by the frame that caught it (and, where the platform
only swallows some causes, by a message pattern).  Every entry needs a reason and a source; an exception
is only listed here when the cited code shows that the catch is the designed path, not an error handling
of something that went wrong.  Sources are the AOSP tag android-14.0.0_r1 (platform/frameworks/base,
platform/libcore); the code at the cited place was read, not assumed.  Library behaviour that differs
between versions (androidx, kotlinx, ...) is not listed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..findings import Finding, Klass

_AOSP = "https://android.googlesource.com/platform/"
_TAG = "/+/refs/tags/android-14.0.0_r1/"


@dataclass(frozen=True)
class Harmless:
    id: str
    type: str                 # exception class, fully qualified
    catchers: tuple[str, ...] # frames that catch it (as the agent reports them, numbers normalised)
    reason: str
    source: str
    message: str = ""         # regex that must match the exception message (empty: any)


HARMLESS: tuple[Harmless, ...] = (
    Harmless(
        "getidentifier-numeric", "java.lang.NumberFormatException",
        ("android.content.res.Resources.getIdentifier", "android.content.res.ResourcesImpl.getIdentifier"),
        "getIdentifier(name, ...) first tries Integer.parseInt(name) and ignores the exception: a resource name "
        "that is not a number (every named lookup, e.g. system_neutral1_10) takes this path.",
        _AOSP + "frameworks/base" + _TAG + "core/java/android/content/res/ResourcesImpl.java#256"),
    Harmless(
        "serial-private-method", "java.lang.NoSuchMethodException",
        ("java.io.ObjectStreamClass.getPrivateMethod",),
        "Java serialization probes a class for a private writeObject/readObject/readResolve; absent means 'use the "
        "default', getPrivateMethod returns null.",
        _AOSP + "libcore" + _TAG + "ojluni/src/main/java/java/io/ObjectStreamClass.java#1557"),
    Harmless(
        "serial-declared-suid", "java.lang.NoSuchFieldException",
        ("java.io.ObjectStreamClass.getDeclaredSUID",),
        "Java serialization looks for an explicit serialVersionUID; a class without one gets a computed value, "
        "getDeclaredSUID catches everything and returns null.",
        _AOSP + "libcore" + _TAG + "ojluni/src/main/java/java/io/ObjectStreamClass.java#1791"),
    Harmless(
        "exif-orf-probe", "java.io.IOException",
        ("android.media.ExifInterface.isOrfFormat",),
        "ExifInterface tests the file signature against each raw format in turn; isOrfFormat swallows the read "
        "error of every file that is not ORF (the byte order of a PNG or JPEG is not an ORF byte order).",
        _AOSP + "frameworks/base" + _TAG + "media/java/android/media/ExifInterface.java#2730"),
    Harmless(
        "nonblocking-connect", "android.system.ErrnoException",
        ("libcore.io.IoBridge.connectErrno",),
        "A non-blocking connect(2) returns EINPROGRESS by design; connectErrno rethrows every other errno and "
        "polls for the result on this one.",
        _AOSP + "libcore" + _TAG + "luni/src/main/java/libcore/io/IoBridge.java#198", message=r"EINPROGRESS"),
    Harmless(
        "prefs-first-load", "android.system.ErrnoException",
        ("android.app.SharedPreferencesImpl.loadFromDiskLocked",),
        "stat(2) of a preferences file that does not exist yet (first start) fails; the code treats the failure as "
        "'empty/non-existing'.",
        _AOSP + "frameworks/base" + _TAG + "core/java/android/app/SharedPreferencesImpl.java#178"),
    Harmless(
        "can-open-read-only", "android.system.ErrnoException",
        ("libcore.io.IoUtils.canOpenReadOnly",),
        "canOpenReadOnly(path) is a yes/no question; any errno from open(2) means 'no' and the method returns false.",
        _AOSP + "libcore" + _TAG + "luni/src/main/java/libcore/io/IoUtils.java#328"),
)


def match(f: Finding) -> Harmless | None:
    """The entry for a caught-exception finding, None if there is none."""
    if f.kind != "exc":
        return None
    typ = f.key.split("@", 1)[0]
    catcher = f.details.get("catcher", "")
    msg = str(f.details.get("message", ""))
    for h in HARMLESS:
        if h.type == typ and catcher in h.catchers and (not h.message or re.search(h.message, msg)):
            return h
    return None


def mark_harmless(findings: list[Finding]) -> list[Finding]:
    """Flag known harmless exceptions (details['known_harmless']) and take them out of the ranking (class other)."""
    for f in findings:
        h = match(f)
        if h is not None:
            f.klass = Klass.OTHER
            f.details["known_harmless"] = {"id": h.id, "reason": h.reason, "source": h.source}
    return findings


def is_harmless(f: Finding) -> bool:
    return bool(f.details.get("known_harmless"))
