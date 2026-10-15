# -*- coding: utf-8 -*-
r"""Refuse to publish anything personal.

Run this over a folder before it becomes a public repository. It reads every
file and every file and folder NAME, and it exits non-zero on the first
category it finds anything in. A build that does not pass is not published.

This exists because the rule "no personal data in the public repo" is exactly
the kind of rule that lives in prose and gets obeyed until the day it is
inconvenient - and by then the data is in a commit that everyone has cloned.
Rule 24 of the canon: a rule with no gate does not act.

What it looks for:

  paths       C:\\Users\\..., absolute paths on local drives, a mounted
              share's /Volumes/<name>, the client's temp folder, and a home
              or share folder as the client spells it in a folder name
              (-Users-<name>-...) - these leak both a name and a layout
  private     the words that are private only as words - the closed
              projects' names, both machines' accounts and names - read from
              a list OUTSIDE the repository: BRIDGE_PUBLIC_DENY, else
              private/public-deny.txt beside the source folder. The private
              tree refuses to scan without it; a public clone has none and
              says so
  cyrillic    any Cyrillic at all. The public set is English; Cyrillic in it
              is either an untranslated string or a quotation from somebody's
              private messages, and neither belongs
  secrets     the shared secret, Telegram bot tokens, chat ids, session ids,
              claude.ai session links
  people      e-mail addresses
  logs        leftovers that point at the private working tree
  machines    what THIS machine is called and what it knows of its
              neighbours, asked of it at every run and written nowhere
              here: the account, the host names, the ssh hosts and the
              keys it holds and trusts with their names and fingerprints,
              the share's login, its network's addresses, a login or a
              share behind a known machine's name (<machine>\<account>,
              \\<machine>\<share>)
              - and the forms no machine has to be asked about: an ssh
              key, a key fingerprint, a *.local name, a /Users/<name>
              folder, a "MacN" label

Three named exceptions, named rather than general on purpose - a blanket
"ignore this pattern" is how a scanner stops scanning:

  AUTHOR_LINE       the authorship line the owner asked for, and the
                    copyright line of the licence and the AGPL
                    notice at the head of every source file
  check_public.py   its own source, which by construction holds every
                    pattern it looks for - for the PATTERNS only: a value
                    this machine gives or the private list holds is a hit
                    here too, so a copy of this file carries no name past
  placeholders      the generic forms the docs are supposed to use:
                    C:\path\to\project and session_XXXXXXXX. Every
                    segment must be a placeholder word, so a real path is
                    still caught.

Usage:  python check_public.py <folder> [report.txt]
Exit 0 only when nothing was found.
"""
import base64
import getpass
import glob
import hashlib
import io
import os
import re
import socket
import subprocess
import sys

AUTHOR_LINE = '"Claude Code Bridge" is made by AMDsyc and Claude, 2026'

# The five constants that are allowed to carry the Russian spellings, and
# nothing else. Checked against the LINE, so a quotation that happens to
# mention one of these names is still caught.
# Nothing is exempt from the Cyrillic rule any more: the verdict markers are
# English, so the public set has no reason to carry a Russian word at all.

# NOT skipped any more. __pycache__ used to be walked past as noise, and
# that is exactly how a leak gets through a scanner: a .pyc is binary, so
# nothing in it is searchable by these rules, and it carries co_filename -
# the ABSOLUTE path of the source on the machine that compiled it. Measured,
# not assumed: bridge/__pycache__/hook.cpython-312.pyc held
# "<drive>:\...\Bridge Git\bridge\hook.py". Running the suites inside the
# folder is what creates them.
#
# So the rule is presence, not content. Anything whose extension is not on
# the publishable list is refused for being there at all - which also
# catches .log files, state .json, and whatever else a test run leaves.
SKIP_DIRS = {".git", ".idea", ".vscode"}

PUBLISHABLE_SUFFIX = (".md", ".py", ".bat", ".html", ".txt", ".gitignore")
# Files the build really does produce that the suffix list does not cover.
# Listed one by one rather than allowed by a pattern: the whole point of
# this check is that a file nobody meant to publish is refused for being
# there, and a pattern would quietly let in the next thing that matched it.
# .make_public.json is the build's own witness - its name and the files it
# wrote - and is READ like any text file, not waved through unread; this
# gate refused it the first time the build wrote one (DECISIONS 8.46).
# bridge.command is the Mac's launcher, a shell script read like any text
# file - by its name, not by .command: the acceptance of 2026-10-12 refused
# it as a binary artefact, and a suffix would wave in any other (8.91).
# .gitattributes keeps that launcher's LF ends through a Windows clone (8.114).
PUBLISHABLE_NAMES = ("LICENSE", ".gitignore", "Makefile", ".make_public.json",
                     "bridge.command", ".gitattributes")

# The generic forms the documentation and the fixtures are SUPPOSED to use.
# Named and narrow: every segment has to be a placeholder word, so
# C:\path\to\project passes and C:\Users\someone\real-work does not. Without
# this the scanner would refuse the very shape it exists to enforce, which is
# the fastest way to teach people to ignore it.
PLACEHOLDER_PATH = re.compile(
    r"^[C-Zc-z]:[\\/]{1,2}(path|work|projects?)([\\/]{1,2}"
    r"(to|path|projects?|work|GAME|Game|game|my-[\w-]+|\.\.\.))*$")

# A session link in a fixture is written in shouting placeholders on purpose -
# XXXXXXXX, RETRY1. A real id is not all-caps.
PLACEHOLDER_SESSION = re.compile(r"session_[A-Z0-9_]+$")

# A share's login in a fixture, smb://user1@WORKPC.local/... - the account
# and the PC the suites invent, which the e-mail rule read as an address
# and so refused the public tree (a Mac's run, 2026-10-12, 8.91). Named, not
# every .local: a real account at a real PC's name is still a hit.
PLACEHOLDER_LOGIN = re.compile(r"^user\d*@WORKPC\.local$")

CYRILLIC = re.compile(r"[\u0400-\u04FF]")

# Cyrillic written as an escape is still Cyrillic, and a scanner that misses
# it is one you can walk past on purpose. Closed here before anyone reaches
# for it - including me: the daemon's own vocabulary is written this way, and
# that is exactly why it needs a NAMED exemption rather than a blind spot.
CYR_ESCAPE = re.compile(r"\\u04[0-9a-fA-F]{2}")

RULES = [
    # No word of anyone's is written in this list. An account name and the
    # closed projects' names stood here until 2026-10-14 and were published
    # by this very file; they are the private list's now (DENY_ENV below).
    ("home directory",re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+",
                                  re.I)),
    # The first segment after the drive has to be a real name, three
    # characters or more. Without that, "%d:\n\n%s" in a format string reads
    # as a path on drive d: - three of those were being reported, and a
    # scanner that cries wolf is one people learn to scroll past.
    ("absolute local path",
     re.compile(r"\b[C-Zc-z]:[\\/]{1,2}[A-Za-z0-9_.\-]{3,}"
                r"[^\s\"'`)\]]*")),
    ("telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("telegram chat id", re.compile(r"chat[_ ]?id\s*[:=]\s*[\"']?-?\d{6,}")),
    ("shared secret value", re.compile(r"bridge-secret\s*[:=]\s*[\"'][^\"']+")),
    ("session id", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                              r"[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("claude session link", re.compile(r"claude\.ai/code/session_\w+", re.I)),
    ("e-mail address",
     re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # "bridge-logs" and "data/logs" are folders the product creates and the
    # README has to name them; flagging the words made this rule fire 71
    # times on documentation and mean nothing. What is private is a
    # LEFTOVER - a transcript, or a path into the private working set.
    # ".jsonl" is the format the product reads and writes; twenty of the hits
    # were the code doing its job. What leaks is a jsonl carrying an ABSOLUTE
    # path or a real session id, and a dated path into the private working
    # set. Narrowed twice now, each time because the rule was firing on the
    # product rather than on a leak - narrowing beats silencing.
    ("log leftovers",
     re.compile(r"([A-Za-z]:[\\/][^\s\"']*\.jsonl|"
                r"[0-9a-f]{8}-[0-9a-f]{4}[^\s\"']*\.jsonl|"
                r"test-results[\\/]20\d\d|"
                r"bridge-logs[\\/]20\d\d)")),
    # The forms a machine does not have to be asked about (8.98): any key,
    # any fingerprint, any mDNS name, any home folder on a Mac or Linux,
    # and the "MacN" label the private notes give a Mac. A placeholder in
    # angle brackets - <PC-name>.local, /Users/<account> - is not matched.
    ("ssh key", re.compile(r"\b(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-"
                           r"nistp\d+)\s+AAAA[0-9A-Za-z+/]{20,}")),
    ("key fingerprint", re.compile(r"\bSHA256:[0-9A-Za-z+/]{43}")),
    ("machine label", re.compile(r"\bMac\d+\b")),
    # a file name (settings.local.json) and a call (threading.local()) are
    # not hosts: a host name ends where the name does
    ("mDNS host name", re.compile(r"(?<![%\w.<-])[A-Za-z0-9][A-Za-z0-9-]*"
                                  r"\.local\b(?![.(]\w|\(|-)")),
    ("home directory (Mac, Linux)",
     re.compile(r"(?<![\w.])/(?:Users|home)/(?!Shared\b)[A-Za-z0-9._-]+")),
    # A mounted share is named by whoever mounted it, and a Mac mounts it
    # under /Volumes/<its name> - the name the private list cannot hold
    # for every machine that will run this.
    ("mounted share path",
     re.compile(r"(?<![\w.])/Volumes/[A-Za-z0-9._-]+")),
    # The same folders as the client spells them in a folder name of its
    # own: /Users/<name>/x is ~/.claude/projects/-Users-<name>-x, and
    # /Volumes/<share>/x is -Volumes-<share>-x. The slash rules never see
    # that form, and the Mac's private notes of 2026-10-14 carry it in
    # every scratchpad path - one paste away from a published file.
    ("home or share slug",
     re.compile(r"(?<![A-Za-z0-9])-(?:Users|Volumes|home)-[A-Za-z0-9_]+")),
    # the client's temp folder on a Mac carries the account's uid, and a
    # scratchpad under it the project's slug and a session's id
    ("client temp folder",
     re.compile(r"(?<![\w.])(?:/private)?/tmp/claude-\d+")),
    # an address in a home or office network, whoever's: the machine is
    # asked for its own networks above, and a neighbour on another one
    # was not a hit anywhere (8.105). The product binds 127.0.0.1 only.
    ("private network address",
     re.compile(r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])"
                r"|169\.254)\.\d{1,3}\.\d{1,3}(?![\d])")),
]


def exempt(line, kind, rel="", values=False):
    """Is this hit one of the named exceptions?

    Three, and all three named rather than general - a blanket "ignore this
    pattern" is how a scanner quietly stops scanning. `values` is a hit of
    something this file does not hold - a value this machine gave, a word
    of the private list, a session's first eight - and its own source is no
    exception for those.
    """
    if not values and os.path.basename(rel).startswith("check_public.py"):
        # This file necessarily contains every pattern it searches for, and a
        # scanner that flags its own source is a scanner nobody runs twice.
        # A VALUE is not a pattern, and nothing here needs one: when the
        # exemption covered values too, any file named check_public.py
        # carried any name past the scan (stage 2, 2026-10-14).
        return True
    if ("Copyright (C) 2026  AMDsyc" in line
            or "Copyright (c) 2026 AMDsyc" in line):
        return True
    if AUTHOR_LINE in line:
        return True
    # MARKER_CONSTANTS used to be exempt here, back when the bridge accepted
    # both spellings. The markers are English now, so nothing needs the
    # exemption - and an exemption nobody needs is one that quietly widens.
    return False


def placeholder(kind, hit):
    """Is this hit the generic form the documentation is meant to use?"""
    if kind == "absolute local path":
        return bool(PLACEHOLDER_PATH.match(hit.replace("\\\\", "\\")))
    if kind == "claude session link":
        return bool(PLACEHOLDER_SESSION.search(hit))
    if kind == "e-mail address":
        return bool(PLACEHOLDER_LOGIN.match(hit))
    if kind == "mDNS host name":
        return hit == "WORKPC.local"
    if kind == "home directory (Mac, Linux)":
        return hit.split("/")[2].lower() in STAND_IN_ACCOUNTS
    if kind == "mounted share path":
        return hit.split("/")[2].lower() in STAND_IN_SHARES
    if kind == "home or share slug":
        return hit.split("-")[2].lower() in (
            STAND_IN_SHARES if hit.split("-")[1] == "Volumes"
            else STAND_IN_ACCOUNTS)
    return False


# A SESSION'S FIRST EIGHT HEX, WHICH NO PATTERN CAN KNOW. A whole session id
# is caught above; the eight-character prefix a person writes into a comment
# or a test print ("session 1a2b3c4d") is an ordinary hex word to a pattern,
# and real ones reached the published tree four times (DECISIONS 8.48). So
# the scan asks THIS machine which sessions exist - the names of the
# transcripts in the client's projects folder, read at every run - and
# nothing about any of them is written here. CLAUDE_CONFIG_DIR is the
# client's own way to move that folder, so it is honoured the same way.
HEX8 = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})(?![0-9a-fA-F])")
SESSION_PREFIXES = None


def known_session_prefixes():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    out = set()
    for p in glob.glob(os.path.join(base, "projects", "*", "*.jsonl")):
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", stem):
            out.add(stem[:8])
    return out


# the accounts the suites make up for a home folder: no one's, by design
STAND_IN_ACCOUNTS = {"u", "someone", "user", "me", "name", "account"}
# and the shares they mount, /Volumes/<one of these>: no one's either.
# A name of a real share is not added here - the fixture is renamed
# (stage 2, 2026-10-14: the suites used the Mac's own share once)
STAND_IN_SHARES = {"x", "public", "other"}


# WHAT THIS MACHINE AND ITS NEIGHBOURS ARE CALLED, ASKED OF IT AT EVERY RUN.
# The owner, 2026-10-12: the public version mentions no machine, no key and
# nothing personal from these computers. A pattern cannot know a name, and
# the names written here would be published in this very file - so the scan
# asks the machine it runs on, as it asks for the sessions above: who is
# logged in, what the machine and its ssh config call the hosts, which share
# is mounted under which login, which addresses it has and lets in, and
# which keys it holds and trusts, by name, body and fingerprint. Only the
# count is printed; a value is shown where it is found. -> DECISIONS 8.98
MACHINE_VALUES = None
# what a machine's name is made of, never a leak on its own; a forge's
# too, since host() takes a known host's first label as well: a PC's
# known_hosts that gained github.com mid-scan had "github" refused (8.149)
GENERIC_WORDS = {"macbook", "pro", "air", "mac", "mini", "imac", "studio",
                 "desktop", "laptop", "local", "localhost", "users", "public",
                 "shared", "admin", "administrator", "user", "root", "home",
                 "default", "guest", "id_ed25519", "id_rsa", "id_ecdsa",
                 "id_dsa", "known_hosts", "authorized_keys", "bridge",
                 "share", "github.com", "gitlab.com", "github", "gitlab"}


def generic(value):
    """Is this value made of GENERIC_WORDS and nothing else - one of them,
    or several joined by - _ . or a space? It names no machine then. The
    PC's ssh setup of 2026-10-13 tagged the Mac's key "bridge-mac", and
    the product's own bridge-mac.json was refused on that PC as the key's
    name; a word of its own ("otherbox-key") still makes a name a hit.
    -> DECISIONS 8.100"""
    v = (value or "").lower()
    parts = [p for p in re.split(r"[-_.\s]+", v) if p]
    return v in GENERIC_WORDS or bool(parts) and all(
        p in GENERIC_WORDS for p in parts)


def _ask(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=15)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _key_values(line):
    """[(kind, value)] of one OpenSSH key line: its body, its fingerprint as
    ssh-keygen -l prints it, and the words of its comment."""
    words = line.split()
    for i, w in enumerate(words):
        if w.startswith("AAAA") and len(w) > 40:
            try:
                blob = base64.b64decode(w + "=" * (-len(w) % 4))
            except ValueError:
                return []
            fp = base64.b64encode(hashlib.sha256(blob).digest()).decode()
            out = [("ssh key", w[:40]), ("key fingerprint",
                                         "SHA256:" + fp.rstrip("="))]
            for c in words[i + 1:]:
                out += [("ssh key name", x) for x in re.split(r"[@,]", c)]
            return out
    return []


def machine_values():
    """[(kind, value)] this machine says about itself and its neighbours."""
    out = []

    def add(kind, value):
        value = (value or "").strip().strip('"').strip("[]")
        if len(value) >= 3 and not generic(value) \
                and (kind, value) not in out:
            out.append((kind, value))

    def host(kind, value):
        value = (value or "").strip().strip("[]").split("]:")[0]
        if not value or "*" in value or "?" in value or value[0] == "|":
            return
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", value):
            add("network address", value.rsplit(".", 1)[0] + ".")
            return
        add(kind, value)
        add(kind, value.split(".")[0])

    home = os.path.expanduser("~")
    for v in (getpass.getuser(), os.path.basename(home.rstrip("\\/")),
              os.environ.get("USERNAME"), os.environ.get("USER")):
        add("account name", v)
    for v in (socket.gethostname(), os.environ.get("COMPUTERNAME")):
        host("machine name", v)
    if sys.platform == "darwin":
        for k in ("LocalHostName", "ComputerName"):
            name = _ask(["/usr/sbin/scutil", "--get", k]).strip()
            add("machine name", name)
            for w in re.split(r"[^\w]+", name):
                add("machine name", w)
    ssh = os.path.join(home, ".ssh")
    for line in _read(os.path.join(ssh, "config")).splitlines():
        m = re.match(r"\s*(Host|HostName|User|IdentityFile)\s+(.+)", line,
                     re.I)
        if not m:
            continue
        key, rest = m.group(1).lower(), m.group(2)
        for v in rest.split():
            if key == "user":
                add("account name", v)
            elif key == "identityfile":
                add("ssh key name", os.path.basename(v))
            else:
                host("ssh host", v)
    for name in sorted(os.listdir(ssh)) if os.path.isdir(ssh) else []:
        if name.endswith(".pub"):
            add("ssh key name", name[:-4])
            add("ssh key name", name)
    keyfiles = glob.glob(os.path.join(ssh, "*.pub")) + [
        os.path.join(ssh, "authorized_keys"), os.path.join(ssh, "known_hosts")]
    if os.name == "nt":
        pd = os.environ.get("PROGRAMDATA") or "C:\\ProgramData"
        keyfiles += glob.glob(os.path.join(pd, "ssh", "*.pub")) + [
            os.path.join(pd, "ssh", "administrators_authorized_keys")]
    for path in keyfiles:
        for line in _read(path).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            for kind, v in _key_values(line):
                add(kind, v)
            for net in re.findall(r"(\d+\.\d+\.\d+)\.[\d*]+", line):
                add("network address", net + ".")
            if os.path.basename(path) == "known_hosts":
                for h in line.split()[0].split(","):
                    host("ssh host", h)
    # the share's login and its server, as the mount table says them
    mounts = _ask(["/sbin/mount"]) if sys.platform == "darwin" else (
        _ask(["net", "use"]) if os.name == "nt" else _read("/proc/mounts"))
    for who, server in re.findall(r"//(?:([^@/\s:;]+)(?:;[^@/\s]*)?@)?"
                                  r"([^/\s]+)/", mounts):
        if who:
            add("share login", who + "@")
        host("share server", server.split("._smb")[0])
    for server in re.findall(r"\\\\([^\\\s]+)\\", mounts):
        host("share server", server)
    # this machine's own addresses, as its network says them
    nets = _ask(["ipconfig"]) if os.name == "nt" else _ask(
        ["/sbin/ifconfig"] if os.path.exists("/sbin/ifconfig")
        else ["ip", "-4", "addr"])
    for a in re.findall(r"(\d+\.\d+\.\d+)\.\d+", nets):
        if not a.startswith(("127.", "0.", "255.")) \
                and not a.endswith(".255"):
            add("network address", a + ".")
    return out


def machine_patterns():
    global MACHINE_VALUES
    if MACHINE_VALUES is None:
        MACHINE_VALUES = []
        for kind, v in machine_values():
            if kind == "network address":
                pat = r"(?<![\d.])" + re.escape(v) + r"\d{1,3}(?![\d])"
            elif kind == "share login":
                pat = r"(?<![\w.-])" + re.escape(v)
            elif kind == "ssh key":
                # the body's first forty characters: the key goes on past
                # them, so nothing may be asked of what follows
                pat = r"(?<![A-Za-z0-9+/])" + re.escape(v)
            else:
                pat = r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])"
            MACHINE_VALUES.append((kind, v, re.compile(pat, re.I)))
    return MACHINE_VALUES


# THE WORDS THAT ARE PRIVATE ONLY BECAUSE THEY ARE WORDS. The closed
# projects' names and the accounts and names of both machines: a pattern
# cannot know them, the machine is not asked about all of them (the PC's
# account is no value of the Mac's), and written here they were published
# by this very file - "account name" and "closed project name" stood in
# RULES until 2026-10-14. So they are read from a list that is not in the
# repository and that make_public cannot reach: BRIDGE_PUBLIC_DENY, else
# private/public-deny.txt beside the source folder. One entry a line,
# "kind<TAB>value", and a third field "cs" for a value that is a hit in
# that case only (a name in capitals that is a plain word in small
# letters); "#" opens a comment. A value is a hit where no letter or
# digit is glued to it -
# except digits after one that ends in a letter, which is how a numbered
# copy of a project is named. Only the count is printed; a value is shown
# where it is found, as this machine's are.
#
# The PRIVATE tree must have the list. Its mark is test_cases.py beside
# this file - make_public never publishes it (NEVER) - and there a missing
# list is a refusal: a gate that opens when its list goes astray is the
# shape of 8.46 again. A list named by BRIDGE_PUBLIC_DENY and not there is
# a refusal everywhere. A public clone has no list and says so in one line.
# -> DECISIONS 8.141
DENY_ENV = "BRIDGE_PUBLIC_DENY"
PRIVATE_MARK = "test_cases.py"
DENY_VALUES = None      # [(kind, value, compiled pattern)]
DENY_STATE = ""         # the report's line: where the list came from
DENY_PROBLEM = ""       # why the scan refuses whatever the folder holds


def deny_path():
    return os.environ.get(DENY_ENV) or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "private",
        "public-deny.txt")


def private_tree():
    """Does this scanner stand in the private tree, not in a public clone?"""
    return os.path.isfile(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), PRIVATE_MARK))


def load_deny(path):
    """[(kind, value, case_sensitive)] of the private list at `path`.
    A line that is not an entry is a ValueError naming its number - never
    its text, which may be the very word the list keeps out."""
    out = []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) not in (2, 3) or not parts[0] \
                    or len(parts[1]) < 3 \
                    or len(parts) == 3 and parts[2] != "cs":
                raise ValueError("line %d is not kind<TAB>value[<TAB>cs]" % n)
            out.append((parts[0], parts[1], len(parts) == 3))
    return out


def deny_patterns():
    global DENY_VALUES, DENY_STATE, DENY_PROBLEM
    if DENY_VALUES is not None:
        return DENY_VALUES
    DENY_VALUES = []
    path = os.path.abspath(deny_path())
    named = bool(os.environ.get(DENY_ENV))
    if not os.path.isfile(path):
        if named or private_tree():
            DENY_PROBLEM = "not found"
            DENY_STATE = "private list: NOT FOUND at %s - %s" % (
                path, "named by " + DENY_ENV if named else
                "this is the private tree (%s beside the scanner)"
                % PRIVATE_MARK)
        else:
            DENY_STATE = "private list: none - a public clone"
        return DENY_VALUES
    try:
        entries = load_deny(path)
    except (OSError, ValueError) as exc:    # a bad encoding is a ValueError
        DENY_PROBLEM = "unreadable"
        DENY_STATE = "private list: UNREADABLE at %s - %s" % (path, exc)
        return DENY_VALUES
    if not entries:
        # a list that keeps nothing out is a gate open by accident - and
        # what a loader broken to hand back nothing looks like (case 156)
        DENY_PROBLEM = "holds no entry"
        DENY_STATE = "private list: HOLDS NO ENTRY at %s" % path
        return DENY_VALUES
    for kind, v, cs in entries:
        tail = r"(?![A-Za-z])" if v[-1].isalpha() else r"(?![A-Za-z0-9])"
        pat = r"(?<![A-Za-z0-9])" + re.escape(v) + tail
        DENY_VALUES.append((kind, v, re.compile(pat, 0 if cs else re.I)))
    DENY_STATE = "private list: %d entries from %s" % (len(DENY_VALUES), path)
    return DENY_VALUES


# A LOGIN OR A SHARE BEHIND A MACHINE'S NAME. Windows writes a login as
# <machine>\<account> - whoami, a scheduled task's /ru, an error line - and
# a share as \\<machine>\<share>; what stands after a known machine's name
# is a hit even where neither this machine nor the private list knows it:
# the Mac does not know the PC's account unless the list says it, and a
# share called by an ordinary word is no word for the list (the planner,
# on report 49: "Share" stays out of it). The name itself is a hit of its
# own already; this one shows what stands beside it.
HOST_KINDS = ("machine name", "ssh host", "share server")
HOST_KIND = "login or share behind a machine's name"
HOST_ACCOUNTS = None


def host_patterns():
    global HOST_ACCOUNTS
    if HOST_ACCOUNTS is None:
        names = {v for k, v, _p in machine_patterns() if k in HOST_KINDS}
        names |= {v for k, v, _p in deny_patterns() if k == "machine name"}
        HOST_ACCOUNTS = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(h)
                                    + r"\\{1,2}[A-Za-z0-9._-]{2,}", re.I)
                         for h in sorted(names)]
    return HOST_ACCOUNTS


def scan_text(rel, text, found):
    global SESSION_PREFIXES
    if SESSION_PREFIXES is None:
        SESSION_PREFIXES = known_session_prefixes()
    machine = machine_patterns()
    deny = deny_patterns()
    hosts = host_patterns()
    for n, line in enumerate(text.split("\n"), 1):
        for kind, _v, pat in machine:
            m = pat.search(line)
            if m and not exempt(line, kind, rel, values=True):
                found.append((rel, n, "this machine's " + kind,
                              m.group(0)[:70], line.strip()[:90]))
        for kind, _v, pat in deny:
            m = pat.search(line)
            if m and not exempt(line, kind, rel, values=True):
                found.append((rel, n, "private list's " + kind,
                              m.group(0)[:70], line.strip()[:90]))
        # one hit a line: a machine's name comes as several values (<PC>
        # and <PC>.local alike), and one login is not several leaks
        m = next((m for m in (p.search(line) for p in hosts) if m), None)
        if m and not exempt(line, HOST_KIND, rel, values=True):
            found.append((rel, n, HOST_KIND, m.group(0)[:70],
                          line.strip()[:90]))
        for m in HEX8.finditer(line):
            h = m.group(1).lower()
            if h in SESSION_PREFIXES and \
                    not exempt(line, "session id prefix", rel, values=True):
                found.append((rel, n, "session id prefix", h,
                              line.strip()[:90]))
        for kind, pat in RULES:
            # the first hit that is not a placeholder: a WORKPC.local early
            # in a line no longer hides a real name later in it (8.98)
            m = next((m for m in pat.finditer(line)
                      if not placeholder(kind, m.group(0))), None)
            if m and not exempt(line, kind, rel):
                found.append((rel, n, kind, m.group(0)[:70], line.strip()[:90]))
        m = CYRILLIC.search(line)
        if m and not exempt(line, "cyrillic", rel):
            run = CYRILLIC.findall(line)
            found.append((rel, n, "cyrillic", "".join(run)[:40],
                          line.strip()[:90]))
        m = CYR_ESCAPE.search(line)
        if m and not exempt(line, "cyrillic", rel):
            found.append((rel, n, "cyrillic (escaped)", m.group(0),
                          line.strip()[:90]))


def unexpected(name):
    """A file the build did not produce. Judged by name, never opened."""
    if name in PUBLISHABLE_NAMES:
        return False
    return not name.lower().endswith(PUBLISHABLE_SUFFIX)


# One name must mean one file. This is not a privacy rule, it is the rule
# the layout was rebuilt around on 2026-08-19: the working tree had three
# levels all called `bridge` and a `bridge.bat` in four places, and the owner
# deleted the live package because he could not tell which folder was the
# real one. A scanner that would have said "bridge.bat - 4 places" before the
# commit is cheaper than that morning was.
#
# __init__.py is the one name a Python tree may legitimately repeat, once per
# package. It is listed rather than guessed at, so a second exception has to
# be argued for in writing.
DUPLICATE_OK = {"__init__.py"}


def duplicates(folder):
    """Every basename that exists in more than one place. Names only."""
    where = {}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            rel = os.path.relpath(os.path.join(root, name),
                                  folder).replace("\\", "/")
            where.setdefault(name, []).append(rel)
    return [(name, places) for name, places in sorted(where.items())
            if len(places) > 1 and name not in DUPLICATE_OK]


def scan(folder):
    global SESSION_PREFIXES
    # read once, before any file: the count is printed whatever
    # the folder holds, and a count never taken must not read 0
    SESSION_PREFIXES = known_session_prefixes()
    machine_patterns()
    host_patterns()
    found, files = [], 0
    if DENY_PROBLEM:
        found.append(("<private list>", 0, "private list", DENY_PROBLEM,
                      "the private words cannot be checked without it - "
                      "nothing scanned here is publishable until it is read"))
    for name, places in duplicates(folder):
        found.append((", ".join(places), 0, "duplicate file name", name,
                      "one name in %d places - a reader cannot tell which is "
                      "the real one, and an editor can change the copy that "
                      "is not read" % len(places)))
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for d in dirs:
            rel = os.path.relpath(os.path.join(root, d), folder)
            scan_text(rel + "  (folder name)", d, found)
        for name in names:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, folder).replace("\\", "/")
            scan_text(rel + "  (file name)", name, found)
            files += 1
            if unexpected(name):
                # Refused for being present. Not read: a binary artefact is
                # a leak whatever is inside it, and reading it would only
                # give the rules something they cannot match anyway.
                found.append((rel, 0, "binary artefact",
                              os.path.splitext(name)[1] or name,
                              "not produced by the build - a test run leaves "
                              "these, and a .pyc carries the absolute path "
                              "of its source"))
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    scan_text(rel, fh.read(), found)
            except Exception as exc:
                found.append((rel, 0, "unreadable", exc.__class__.__name__, ""))
    return found, files


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    folder = sys.argv[1]
    found, files = scan(folder)
    out = ["privacy scan of %s" % os.path.abspath(folder),
           "files read: %d" % files,
           "sessions known on this machine, by their transcripts: %d"
           % len(SESSION_PREFIXES or ()),
           "names, addresses and keys this machine gave: %d"
           % len(MACHINE_VALUES or ()), DENY_STATE, ""]
    # A SCAN THAT READ NOTHING HAS CHECKED NOTHING. Over a folder that did
    # not exist this said "no personal data found" and exited 0 - a gate
    # green over nothing, which a build that failed would pass straight
    # through (DECISIONS 8.46). It is a refusal now, and says why.
    empty = files == 0
    if empty:
        out.append("read 0 files - %s - NOT publishable: a scan that read "
                   "nothing has checked nothing"
                   % ("there is no such folder" if not os.path.isdir(folder)
                      else "the folder holds no file"))
    elif not found:
        out.append("личных данных не найдено / no personal data found")
        out.append("")
        out.append("checked for: " + ", ".join(k for k, _ in RULES)
                   + ", session id prefix, this machine's names, addresses "
                   "and keys, a login or share behind a machine's name, the "
                   "private list's words, cyrillic, duplicate file name")
        out.append("exceptions used: the authorship and copyright lines, "
                   "this scanner's own source for its patterns (never for "
                   "a value), and the documented placeholder forms "
                   "(C:\\path\\to\\..., session_XXXX, user1@WORKPC.local, "
                   "/Users/<account> and the suites' stand-in accounts)")
    else:
        by = {}
        for rel, n, kind, hit, line in found:
            by.setdefault(kind, []).append((rel, n, hit, line))
        out.append("FOUND %d, in %d categories - NOT publishable"
                   % (len(found), len(by)))
        out.append("")
        for kind in sorted(by, key=lambda k: -len(by[k])):
            rows = by[kind]
            out.append("%s - %d" % (kind, len(rows)))
            for rel, n, hit, line in rows[:25]:
                out.append("    %s:%s  %s" % (rel, n, hit))
                if line:
                    out.append("        %s" % line)
            if len(rows) > 25:
                out.append("    ... and %d more" % (len(rows) - 25))
            out.append("")
    text = "\n".join(out)
    print(text)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if found or empty else 0


if __name__ == "__main__":
    sys.exit(main())
