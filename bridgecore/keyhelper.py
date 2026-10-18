# Claude Code Bridge - a review loop for Claude Code sessions
# Copyright (C) 2026  AMDsyc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""The API key of a window that runs on one (DECISIONS.md 8.157): read
from the key file, and - run as a script - that window's apiKeyHelper.

The standard library only, and nothing of the bridge: the client runs it
as `<python> -I keyhelper.py <key file>`, where neither the bridge's
folder nor PYTHONPATH is on the path, and sessions.py imports read_key
from here so that the file is read one way, by one function.

The key goes to one place only - the client, as the helper's output - and
is never quoted in a trouble: a trouble goes into the journal, and half a
key is still a secret.
"""
import os
import re
import stat
import sys

KEY_VAR = "ANTHROPIC_API_KEY"
_KEY_CHARS = re.compile(r"[A-Za-z0-9_-]")
# what a person may paste in front of the key: a line for sh, for cmd, or
# for PowerShell - each taken off, as the token file's `export` is
_PREFIXES = ("export ", "export\t", "set ", "set\t", "$env:")


def read_key(path):
    """(key, trouble). Never raises. ("", "") when there is no file at all;
    ("", trouble) when there is a file and no usable key in it, or - where
    a file has modes - one that others may read. `trouble` says why in
    words that never quote the file."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "", ""
    except OSError as exc:
        return "", "cannot be read (%s)" % (exc.strerror or exc.__class__
                                            .__name__)
    if os.name != "nt" and st.st_mode & 0o077:
        return "", ("may be read by others (mode %03o) - it has to be 600: "
                    "chmod 600 it" % stat.S_IMODE(st.st_mode))
    try:
        with open(path, "rb") as fh:
            raw = fh.read(65536)
    except OSError as exc:
        return "", "cannot be read (%s)" % (exc.strerror or exc.__class__
                                            .__name__)
    try:
        # PowerShell 5's `>` and Out-File write UTF-16 with its mark
        text = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                else raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return "", "is not UTF-8 text"
    t = text.strip()
    for pre in _PREFIXES:
        if t[:len(pre)].lower() == pre.lower():
            t = t[len(pre):].strip()
            break
    # PowerShell's `$env:ANTHROPIC_API_KEY = "..."` has spaces round it
    named = re.match(re.escape(KEY_VAR) + r"\s*=\s*", t)
    if named:
        t = t[named.end():].strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        t = t[1:-1].strip()
    if not t:
        return "", "is empty"
    lines = len([ln for ln in t.splitlines() if ln.strip()])
    if lines > 1:
        return "", "holds %d lines, not one key" % lines
    for i, ch in enumerate(t):
        if not _KEY_CHARS.match(ch):
            what = ("a space" if ch.isspace() else
                    "a non-ASCII character" if ord(ch) >= 128 else
                    "a control character" if not ch.isprintable() else
                    "a character no key holds")
            return "", ("is broken: %s at character %d of %d"
                        % (what, i + 1, len(t)))
    return t, ""


def main(argv):
    """The helper: the key on stdout, and nothing after it - no newline, so
    no shell and no Windows console can add a character to it."""
    if len(argv) != 2:
        sys.stderr.write("usage: keyhelper.py <key file>\n")
        return 2
    key, trouble = read_key(argv[1])
    if not key:
        sys.stderr.write("the key file %s %s\n" % (
            argv[1], trouble or "is not there"))
        return 1
    out = getattr(sys.stdout, "buffer", None)
    if out is None:
        sys.stdout.write(key)
    else:
        out.write(key.encode("ascii"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
