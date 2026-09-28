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

"""Temp folders THIS PROCESS made - and the removal of those, and nothing else.

2026-09-28 (DECISIONS 8.43): a sweep that every suite ran over the real temp
folder - "our prefixes, older than a day" - had its prefix check taken out
by a sabotage run, and for five minutes it deleted every folder there older
than a day, the scratchpads of every Claude Code session on the machine
among them. A second lock on a sweep was refused: there is no sweep. A
process removes the folder it made itself, by the exact path mkdtemp gave
it; this module keeps those paths and refuses any other.

It reads nothing from the package, because the suites import it first:
they make their folder, point BRIDGE_DATA into it, and only then import the
modules that read BRIDGE_DATA.
"""

import os
import shutil
import stat
import sys
import tempfile

_MADE = set()


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def make(prefix, dir=None):
    """mkdtemp, and the exact path written down as this process's own."""
    path = tempfile.mkdtemp(prefix=prefix, dir=dir)
    _MADE.add(_key(path))
    return path


def owned(path):
    """Did THIS process make this folder with make()?"""
    return bool(path) and _key(path) in _MADE


def _clear_readonly(func, path, _exc):
    # git makes its objects read-only, and rmtree on Windows stops at them
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove(path):
    """Remove a folder this process made. (True, "") when it is gone.

    Any path make() did not return in this process is refused, and nothing
    is touched: (False, the reason). No listing, no pattern, no age - the
    only thing this can reach is a path it handed out itself.
    """
    if not owned(path):
        return False, "refused - %s was not made by this process" % path
    kw = ({"onexc": _clear_readonly} if sys.version_info >= (3, 12)
          else {"onerror": _clear_readonly})
    try:
        shutil.rmtree(path, **kw)
    except OSError:
        pass
    if os.path.exists(path):
        return False, "not removed whole - %s" % path
    _MADE.discard(_key(path))
    return True, ""


def finish(path, failed):
    """A suite's last act: remove its own folder when it passed; keep it and
    say where when it failed - that folder is the evidence."""
    if failed:
        print("the temp folder is kept for a look: %s" % path)
        return False
    ok, why = remove(path)
    if not ok:
        print("the temp folder was not removed: %s" % why)
    return ok
