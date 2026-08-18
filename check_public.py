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

  paths       the account name, C:\\Users\\..., and absolute paths on local
              drives - these leak both a name and a machine layout
  projects    the names of the closed projects this bridge has been running
  cyrillic    any Cyrillic at all. The public set is English; Cyrillic in it
              is either an untranslated string or a quotation from somebody's
              private messages, and neither belongs
  secrets     the shared secret, Telegram bot tokens, chat ids, session ids,
              claude.ai session links
  people      e-mail addresses
  logs        leftovers that point at the private working tree

Three named exceptions, named rather than general on purpose - a blanket
"ignore this pattern" is how a scanner stops scanning:

  AUTHOR_LINE       the authorship line the owner asked for, and the
                    copyright line of the licence and the AGPL
                    notice at the head of every source file
  check_public.py   its own source, which by construction holds every
                    pattern it looks for
  placeholders      the generic forms the docs are supposed to use:
                    C:\path\to\project and session_XXXXXXXX. Every
                    segment must be a placeholder word, so a real path is
                    still caught.

Usage:  python check_public.py <folder> [report.txt]
Exit 0 only when nothing was found.
"""
import io
import os
import re
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
# "<drive>:\...\Bridge Gitridge\hook.py". Running the suites inside the
# folder is what creates them.
#
# So the rule is presence, not content. Anything whose extension is not on
# the publishable list is refused for being there at all - which also
# catches .log files, state .json, and whatever else a test run leaves.
SKIP_DIRS = {".git", ".idea", ".vscode"}

PUBLISHABLE_SUFFIX = (".md", ".py", ".bat", ".html", ".txt", ".gitignore")
PUBLISHABLE_NAMES = ("LICENSE", ".gitignore")

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

CYRILLIC = re.compile(r"[\u0400-\u04FF]")

# Cyrillic written as an escape is still Cyrillic, and a scanner that misses
# it is one you can walk past on purpose. Closed here before anyone reaches
# for it - including me: the daemon's own vocabulary is written this way, and
# that is exactly why it needs a NAMED exemption rather than a blind spot.
CYR_ESCAPE = re.compile(r"\\u04[0-9a-fA-F]{2}")

RULES = [
    ("account name", re.compile(r"amdsyc", re.I)),
    ("home directory", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+",
                                  re.I)),
    # The first segment after the drive has to be a real name, three
    # characters or more. Without that, "%d:\n\n%s" in a format string reads
    # as a path on drive d: - three of those were being reported, and a
    # scanner that cries wolf is one people learn to scroll past.
    ("absolute local path",
     re.compile(r"\b[C-Zc-z]:[\\/]{1,2}[A-Za-z0-9_.\-]{3,}"
                r"[^\s\"'`)\]]*")),
    ("closed project name",
     re.compile(r"\b(Space[_ ]?junk|Texture[_ ]?baker|ENEMY[_ ]?ANIMATION|"
                r"sj_model_import|Auriga|spaceman_?\d*|pianino)\b", re.I)),
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
]


def exempt(line, kind, rel=""):
    """Is this hit one of the named exceptions?

    Three, and all three named rather than general - a blanket "ignore this
    pattern" is how a scanner quietly stops scanning.
    """
    if os.path.basename(rel).startswith("check_public.py"):
        # This file necessarily contains every pattern it searches for, and a
        # scanner that flags its own source is a scanner nobody runs twice.
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
    return False


def scan_text(rel, text, found):
    for n, line in enumerate(text.split("\n"), 1):
        for kind, pat in RULES:
            m = pat.search(line)
            if m and not exempt(line, kind, rel) \
                    and not placeholder(kind, m.group(0)):
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


def scan(folder):
    found, files = [], 0
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
           "files read: %d" % files, ""]
    if not found:
        out.append("личных данных не найдено / no personal data found")
        out.append("")
        out.append("checked for: " + ", ".join(k for k, _ in RULES)
                   + ", cyrillic")
        out.append("exceptions used: the authorship and copyright lines, "
                   "this scanner's own source, and the documented "
                   "placeholder forms (C:\\path\\to\\..., session_XXXX)")
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
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
