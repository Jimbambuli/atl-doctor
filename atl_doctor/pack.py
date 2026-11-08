# SPDX-License-Identifier: GPL-3.0-or-later
"""`atl-doctor pack`: a capture directory as one .tar.gz that is safe to hand to someone else.

Two stages. Default: cleaned (paths, network addresses, secrets, e-mail addresses replaced by stable placeholders,
text typed into fields removed); `--full`: the data stays as recorded. In both, files the report does not read, empty
files and duplicates are left out. The archive says what was done in SANITIZED.txt (rules and counts, never values).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import tarfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DoctorError, UsageError

NOTE = "SANITIZED.txt"

# What report, explain, compare and state read from a capture directory (traced with an open() audit, then checked
# against the sources). Everything else is recorder bookkeeping (armed, tr.stop, tr.counts, watch.txt,
# tracecount.log, kwin.log, ...) or raw material that was already turned into one of these files (logcat.txt).
KEEP = re.compile(
    r"manifest\.json|atl\.log|agent\.tsv|stubhits\.tsv|marks\.tsv|memory\.tsv|steps\.log|steps\.tsv|input\.jsonl|replay\.jsonl"
    r"|trace/(?:stubs\.tsv|tr\.methods|tr\.callers|tr\.time|tr\.threads)"
    r"|trees/[^/]+\.xml|shots/[^/]+\.(?:png|origin)|perf/gfxinfo-[^/]+\.txt")
BINARY = (".png",)
IMAGES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".origin")
EDITABLE = ("EditText", "AutoCompleteTextView", "SearchAutoComplete", "MultiAutoCompleteTextView")

SECRET_NAMES = (r"token|access_token|refresh_token|id_token|auth_token|password|passwd|pwd|secret|client_secret|"
                r"api_key|apikey|sessionid|session_id|authkey")
QUERY_NAMES = SECRET_NAMES + r"|key|sid|session|auth|sig|signature|code|ticket"

RULES = (  # (name, what it does); the order is the order of application
    ("home", "home directory of the user and /home/NAME, /Users/NAME -> ~"),
    ("user", "user name -> <user>"),
    ("host", "host name -> <host>"),
    ("secret-header", "Authorization, Cookie, Set-Cookie and X-*-Token/Key/Auth header values -> <secret>"),
    ("secret-bearer", "Bearer tokens -> <secret>"),
    ("secret-param", "URL query and key=value parameters such as token, key, password, sid -> <secret>"),
    ("secret-json", "JSON members such as \"password\": \"...\" -> \"<secret>\""),
    ("account", "account names (Account {name=...}, accountName=...) -> <account-N>"),
    ("email", "e-mail addresses -> <email-N>"),
    ("mac", "MAC addresses -> <mac-N>"),
    ("lan-ip", "private IPv4 and IPv6 addresses (LAN, link-local, ULA, CGNAT) -> <lan-ip-N>"),
    ("ip", "public IPv4 and IPv6 addresses -> <ip-N>"),
    ("edit-text", "text of input fields in trees/*.xml -> empty"),
    ("input-text", "typed text in input.jsonl (only the length stays)"),
)

_IPV4 = re.compile(r"(?<![\w.(-])(?:\d{1,3}\.){3}\d{1,3}(?![\w-]|\.\d)")
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_MAC = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![\w:])")
_EMAIL = re.compile(r"(?<![\w@.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?!(?:dex|jar|apk|so)\b)[A-Za-z]{2,}(?![\w@-])")
_ACCOUNT = re.compile(r"(Account\s*\{name=)([^,}\s]+)(?=,\s*type=)|((?i:account_?name)[\"']?\s*[=:]\s*[\"']?)([^\s\"',;&}]+)")
_HEADER = re.compile(r"(?i)\b((?:set-)?cookie|authorization|proxy-authorization|x-[\w-]*(?:token|key|auth|secret)[\w-]*)"
                     r"(\s*[:=]\s*)(?!<secret>)[^\r\n\t]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)(?!<secret>)[A-Za-z0-9._~+/=-]{8,}")
_PARAM = re.compile(r"(?i)(?:(?<=[?&;])(" + QUERY_NAMES + r")|(?<![\w.-])(" + SECRET_NAMES + r"))=(?!<secret>|null\b)([^&\s\"'<>;\t]+)")
_JSON = re.compile(r"(?i)(\"(?:" + SECRET_NAMES + r")\"\s*:\s*)\"[^\"]*\"")
_NODE = re.compile(r"<node\b[^>]*>")


@dataclass
class Sanitizer:
    """Replaces personal data in text; one instance per archive so that equal values get equal placeholders."""
    home: str = ""
    user: str = ""
    host: str = ""
    counts: Counter = field(default_factory=Counter)
    _ids: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.homes = {h.rstrip("/") for h in (self.home,) if h and h.rstrip("/")}
        self.users = {u for u in (self.user,) if len(u) >= 3 and u not in ("root", "user", "app")}
        self.hosts = {h for h in (self.host,) if len(h) >= 3 and h not in ("localhost",)}

    def _ph(self, kind: str, value: str) -> str:
        key = (kind, value.lower() if kind in ("email", "mac") else value)
        n = self._ids.setdefault(key, 1 + sum(1 for k in self._ids if k[0] == kind))
        return f"<{kind}-{n}>"

    def _sub(self, rule: str, rx: re.Pattern, repl, text: str) -> str:
        text, n = rx.subn(repl, text)
        self.counts[rule] += n
        return text

    # -- the rules ---------------------------------------------------------------------------------------------
    def _paths(self, text: str) -> str:
        for h in sorted(self.homes, key=len, reverse=True):
            text = self._sub("home", re.compile(re.escape(h) + r"(?![\w.-])"), "~", text)
        text = self._sub("home", re.compile(r"(?:/(?:home|Users)/|\b(?:home|Users)@)([^/@\s\"':;,\\]+)"),
                         lambda m: self._found_user(m), text)
        for u in sorted(self.users):
            text = self._sub("user", re.compile(r"(?<![\w.-])" + re.escape(u) + r"(?![\w-])"), "<user>", text)
        for h in sorted(self.hosts):
            text = self._sub("host", re.compile(r"(?<![\w.-])" + re.escape(h) + r"(?![\w-])", re.I), "<host>", text)
        return text

    def _found_user(self, m: re.Match) -> str:
        name = m.group(1)
        if name == "~" or name.startswith("<"):
            return m.group(0)
        if len(name) >= 3 and name not in ("root", "user", "app"):
            self.users.add(name)
        return "~" if m.group(0).startswith("/") else m.group(0)[:m.start(1) - m.start()] + "<user>"

    def _ip(self, m: re.Match) -> str:
        s = m.group(0)
        if "version" in m.string[max(0, m.start() - 24):m.start()].lower():
            return s  # "version_name": "2.26.43.74"
        try:
            a = ipaddress.ip_address(s)
        except ValueError:
            return s
        if a.is_loopback or a.is_unspecified or a.is_multicast or (a.version == 4 and a.is_reserved):
            return s
        if a.version == 6:
            groups = [g for g in s.split(":") if g]
            if len(groups) < 3 and not s.lower().startswith("fe80"):
                return s  # "a::b" in a symbol name
        elif s.split(".")[0] == "0":
            return s
        lan = a.is_private or a.is_link_local or (a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"))
        rule = "lan-ip" if lan else "ip"
        self.counts[rule] += 1
        return self._ph(rule, s)

    def text(self, text: str) -> str:
        text = self._paths(text)
        text = self._sub("secret-header", _HEADER, lambda m: m.group(1) + m.group(2) + "<secret>", text)
        if "earer" in text:
            text = self._sub("secret-bearer", _BEARER, lambda m: m.group(1) + "<secret>", text)
        if "=" in text:
            text = self._sub("secret-param", _PARAM,
                             lambda m: f"{m.group(1) or m.group(2)}=<secret>", text)
        if '"' in text:
            text = self._sub("secret-json", _JSON, lambda m: m.group(1) + '"<secret>"', text)
        if "ccount" in text:
            text = self._sub("account", _ACCOUNT, self._account, text)
        if "@" in text:
            text = self._sub("email", _EMAIL, lambda m: self._ph("email", m.group(0)), text)
        if ":" in text:
            text = self._sub("mac", _MAC, lambda m: self._ph("mac", m.group(0)), text)
        if "." in text:
            text = _IPV4.sub(self._ip, text)
        if ":" in text:
            text = _IPV6.sub(self._ip, text)
        return text

    def _account(self, m: re.Match) -> str:
        if m.group(1):
            return m.group(1) + self._ph("account", m.group(2))
        if m.group(4).startswith("<"):
            self.counts["account"] -= 1
            return m.group(0)
        return m.group(3) + self._ph("account", m.group(4))

    def tree(self, text: str) -> str:
        def blank(m: re.Match) -> str:
            node = m.group(0)
            cls = re.search(r'\sclass="([^"]*)"', node)
            if cls and cls.group(1).rsplit(".", 1)[-1].rsplit("$", 1)[-1].endswith(EDITABLE):
                node, n = re.subn(r'(\stext=")[^"]+(")', r"\1\2", node, count=1)
                self.counts["edit-text"] += n
            return node
        return self.text(_NODE.sub(blank, text))

    def input_jsonl(self, text: str) -> str:
        out = []
        for line in text.split("\n"):
            if not line.strip():
                out.append(line)
                continue
            try:
                e = json.loads(line)
            except ValueError:
                out.append(self.text(line))
                continue
            if isinstance(e.get("text"), dict) and "value" in e["text"]:
                del e["text"]["value"]
                self.counts["input-text"] += 1
            if e.get("kind") == "header":
                e["full"] = False
            tg = e.get("target")
            if isinstance(tg, dict) and tg.get("text") and str(tg.get("cls", "")).rsplit(".", 1)[-1].rsplit("$", 1)[-1].endswith(EDITABLE):
                tg["text"] = ""
                self.counts["input-text"] += 1
            out.append(self.text(json.dumps(e, ensure_ascii=False, separators=(",", ":"))))
        return "\n".join(out)

    def file(self, rel: str, data: bytes) -> bytes:
        if rel.endswith(BINARY):
            return data
        s = data.decode("utf-8", "surrogateescape")
        if rel == "input.jsonl":
            s = self.input_jsonl(s)
        elif rel.startswith("trees/"):
            s = self.tree(s)
        else:
            s = self.text(s)
        return s.encode("utf-8", "surrogateescape")


# -- packing ---------------------------------------------------------------------------------------------------
def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def pack(cap: Path, out: Path, full: bool = False, home: str = "", user: str = "", host: str = "") -> dict:
    """Write the archive `out` for the capture directory `cap`; returns what was done."""
    cap = Path(cap)
    if not (cap / "manifest.json").is_file():
        raise DoctorError(f"{cap}: not a capture directory (no manifest.json)")
    san = Sanitizer(home, user, host)
    omitted: list[tuple[str, str]] = []
    kept: dict[str, bytes] = {}
    seen: dict[str, str] = {}
    for rel in _files(cap):
        if not full and (rel.split("/", 1)[0] == "shots" or rel.lower().endswith(IMAGES)):
            omitted.append((rel, "screenshots only with --full"))   # also if the recorder left some in the folder
            continue
        if not KEEP.fullmatch(rel):
            omitted.append((rel, "not read by report"))
            continue
        data = (cap / rel).read_bytes()
        if not data:
            omitted.append((rel, "empty"))
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            omitted.append((rel, f"same content as {seen[digest]}"))
            continue
        seen[digest] = rel
        kept[rel] = data if full else san.file(rel, data)
    sizes = (sum(len((cap / r).read_bytes()) for r in _files(cap)), sum(map(len, kept.values())))
    note = _note(cap.name, full, san, omitted, sorted(kept), sizes)
    base = cap.name
    tmp = out.with_name(f".{out.name}.tmp")
    try:
        with open(tmp, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as t:
            for rel, data in [(NOTE, note.encode())] + sorted(kept.items()):
                ti = tarfile.TarInfo(f"{base}/{rel}")
                ti.size, ti.mode = len(data), 0o644
                t.addfile(ti, io.BytesIO(data))
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return {"archive": out, "kept": len(kept), "omitted": omitted, "counts": dict(san.counts),
            "before": sizes[0], "after": sizes[1]}


def _note(name: str, full: bool, san: Sanitizer, omitted, kept, sizes) -> str:
    lines = [f"atl-doctor pack: {name}", "stage: " + ("full (data as recorded, passwords and addresses in plain text)"
                                                      if full else "cleaned"), ""]
    if full:
        lines += ["No replacements were made.", ""]
    else:
        lines += ["Replacements (rule: count; the original values are not listed):"]
        lines += [f"  {n:<14} {san.counts.get(n, 0):>6}  {what}" for n, what in RULES]
        lines += ["", "Placeholders are stable inside this archive: the same value always gets the same number.",
                  "Not changed: UI texts in trees/*.xml other than input fields, package, class and method names.",
                  "screenshots: omitted (only --full keeps them)."]
    lines += ["", f"Files kept ({len(kept)}):"] + [f"  {r}" for r in kept]
    lines += ["", f"Files left out ({len(omitted)}):"] + [f"  {r}  ({why})" for r, why in omitted]
    lines += ["", f"Size of the files before: {sizes[0]} bytes, in the archive: {sizes[1]} bytes (uncompressed)"]
    return "\n".join(lines) + "\n"


def archive_name(cap: Path, full: bool) -> str:
    return f"{Path(cap).resolve().name}{'-FULL' if full else ''}.tar.gz"


# -- command ---------------------------------------------------------------------------------------------------
def _run(args: argparse.Namespace) -> int:
    cap = Path(args.dir)
    if not cap.is_dir():
        raise UsageError(f"{cap}: not a directory")
    out = Path(args.output) if args.output else cap.resolve().parent / archive_name(cap, args.full)
    r = pack(cap, out, args.full, args.home if args.home is not None else str(Path.home()),
             args.user if args.user is not None else _user(), args.host if args.host is not None else socket.gethostname())
    print(out)
    from .cli import log
    log(0, f"{r['kept']} files, {len(r['omitted'])} left out, {r['before']} -> {r['after']} bytes"
           + ("" if args.full else ", replacements: " + ", ".join(f"{k} {v}" for k, v in r["counts"].items() if v)))
    return 0


def _user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:  # no passwd entry in a container
        return os.environ.get("USER", "")


def register(sub: argparse._SubParsersAction) -> None:
    from .cli import command, done

    p, g = command(sub, "pack", "pack a recording as .tar.gz, cleaned of personal data",
                   "Pack a capture directory into one .tar.gz that can be handed on. Cleaned (default): paths, user and\n"
                   "host name, IP and MAC addresses, tokens and passwords, e-mail and account names are replaced\n"
                   "by placeholders, typed text is removed, and no screenshots are included. With --full everything stays as recorded. Files that\n"
                   "report does not read, empty files and duplicates are left out in both. SANITIZED.txt in the\n"
                   "archive lists the rules and how often each was applied. Prints the archive's path.")
    g.args.add_argument("dir", metavar="DIR", help="capture directory (name PACKAGE-DATE-TIME)")
    g.output.add_argument("-o", "--output", metavar="FILE",
                          help="archive to write (default: next to DIR, DIR.tar.gz, or DIR-FULL.tar.gz with --full)")
    g.output.add_argument("--full", action="store_true",
                          help="do not clean: all data in plain text, including passwords; name ends in -FULL")
    g.advanced.add_argument("--user", metavar="NAME", help="user name to replace (default: the current user)")
    g.advanced.add_argument("--home", metavar="DIR", help="home directory to replace (default: $HOME)")
    g.advanced.add_argument("--host", metavar="NAME", help="host name to replace (default: this machine's)")
    done(p, g, _run)
