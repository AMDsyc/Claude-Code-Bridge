# -*- coding: utf-8 -*-
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

"""Is what ships the same bytes as what was tested?

Running the suites from an unpacked copy proves the copy WORKS. It does not
prove the copy is the code that was reviewed: a file could be stale, a build
could have picked up a different tree, a zip entry could have been written
twice. This compares sha256 of every one of the 28 files across all three
places - repository, archive entry, unpacked file - and a package that does
not match on all three is not delivered, it is a failed build.

Usage:  python verify_package.py <repo> <zip> <unpacked> [out.txt]
Exit 0 only when every file matches in all three places.
"""
import hashlib
import io
import os
import sys
import zipfile

# This file is IN the list, and on purpose: a package whose recipient cannot
# check it is a weaker package. It is not a diagnostic like probe_context.py -
# it is the recipe's own proof, and it travels with what it proves.
# The archive mirrors the working layout exactly, so an unpacked
# package IS a working bridge. Shipping it flat instead would need a
# second bridge.bat with different contents, and one name meaning two
# files is what this layout was rebuilt to end.
FILES = ["bridge.bat", "add-project.bat",
         "source/README.md", "source/LICENSE", "source/HONESTY.md",
         # The evidence half ships with the package - the suites read it and
         # a pair's own history is part of the product. It is a different
         # question from the PUBLIC repository, where it must never go: it
         # quotes private messages and names closed projects.
         "source/HONESTY_CASES.md", "source/verify_package.py",
         "source/test_cases.py", "source/test_handover.py",
         "source/test_archive.py", "source/test_search.py",
         "source/test_wall_handover.py", "source/test_multipair.py",
         "source/bridgecore/__init__.py", "source/bridgecore/archive.py",
         "source/bridgecore/channel.py", "source/bridgecore/daemon.py",
         "source/bridgecore/discover.py", "source/bridgecore/hook.py",
         "source/bridgecore/install.py", "source/bridgecore/models.py",
         "source/bridgecore/panel.html", "source/bridgecore/relayout.py",
         "source/bridgecore/remote.py",
         "source/bridgecore/sessions.py", "source/bridgecore/statusline.py",
         "source/bridgecore/store.py", "source/bridgecore/telegram.py"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def compare(repo, zpath, unpacked):
    rows, bad = [], []
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        for rel in FILES:
            got = {}
            try:
                got["repo"] = sha(read(os.path.join(repo, rel)))
            except Exception as exc:
                got["repo"] = "MISSING (%s)" % exc.__class__.__name__
            try:
                got["zip"] = sha(z.read(rel))
            except Exception:
                got["zip"] = "MISSING"
            try:
                got["unpacked"] = sha(read(os.path.join(unpacked, rel)))
            except Exception:
                got["unpacked"] = "MISSING"
            same = got["repo"] == got["zip"] == got["unpacked"]
            rows.append((rel, got, same))
            if not same:
                bad.append(rel)
    extra = sorted(set(names) - set(FILES))
    return rows, bad, extra, names


def main():
    # Rewrapping stdout belongs HERE, not at import time. At module
    # level it replaces the wrapper of whoever imported this file, and
    # everything they had already buffered goes out with the old one -
    # which is exactly what happened: a suite that exec'd this lost
    # three cases' worth of output and still printed "all cases pass".
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    repo, zpath, unpacked = sys.argv[1], sys.argv[2], sys.argv[3]
    rows, bad, extra, names = compare(repo, zpath, unpacked)
    out = []
    out.append("package byte-for-byte check")
    out.append("repo     : %s" % os.path.abspath(repo))
    out.append("archive  : %s" % os.path.abspath(zpath))
    out.append("unpacked : %s" % os.path.abspath(unpacked))
    out.append("")
    out.append("%-28s %-16s %s" % ("file", "sha256 (first 16)", "verdict"))
    out.append("-" * 64)
    for rel, got, same in rows:
        out.append("%-28s %-16s %s"
                   % (rel, got["repo"][:16], "same in all three" if same
                      else "MISMATCH repo=%s zip=%s unpacked=%s"
                           % (got["repo"][:12], got["zip"][:12],
                              got["unpacked"][:12])))
    out.append("")
    out.append("files listed   : %d" % len(FILES))
    out.append("entries in zip : %d" % len(names))
    out.append("unexpected     : %s" % (", ".join(extra) if extra else "none"))
    out.append("stray nesting  : %s"
               % (", ".join(n for n in names
                            if n.startswith("source/bridgecore/bridge"))
                  or "none - the package holds modules only"))
    out.append("")
    ok = not bad and not extra and len(names) == len(FILES)
    out.append("RESULT: %s" % (("all %d files identical in repository, "
                                "archive and unpacked copy" % len(FILES))
                               if ok else
                               "NOT DELIVERED - %s" %
                               (("mismatched: " + ", ".join(bad)) if bad
                                else "the archive holds entries that are not "
                                     "on the list")))
    text = "\n".join(out)
    print(text)
    if len(sys.argv) > 4:
        with open(sys.argv[4], "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
