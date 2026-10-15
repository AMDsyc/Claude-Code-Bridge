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
import socket
import stat
import subprocess
import sys
import tempfile
import time

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


def tmux_home():
    """Off Windows: a folder of this process's own for tmux's sockets, made
    with make(). A suite points TMUX_TMPDIR at it itself - this module
    touches no variable of the process - so every tmux server its windows
    start binds there and not in the shared /tmp/tmux-<uid>, and goes with
    the folder.
    tmux 3.8 leaves a socket behind when its server ends: 887 dead ones
    were there on a Mac by 2026-10-13 (DECISIONS 8.103). Made in /tmp,
    not in mkdtemp's usual place: a socket path binds only up to 104 bytes,
    and a Mac's temp folder is half of that before the label. None on
    Windows, where a window is a console."""
    if os.name == "nt":
        return None
    return make("bt-", dir="/tmp")


def tmux_left(path, wait=0.0):
    """The sockets in a tmux_home() folder that a tmux server still answers
    on, by name - a connect, not a tmux run. [] for None. Asked again for
    up to `wait` seconds while any answers: kill-server, and a last pane's
    end, return before the server has stopped listening."""
    end = time.time() + wait
    live = _tmux_listening(path)
    while live and time.time() < end:
        time.sleep(0.1)
        live = _tmux_listening(path)
    return live


def tmux_made(path):
    """Every socket a tmux server made in a tmux_home() folder, answering
    or not - not empty when the run's windows did bind here. [] for None."""
    where = _tmux_where(path)
    return sorted(os.listdir(where)) if where else []


def tmux_stop(path, tmux):
    """Stop the tmux servers still answering in a tmux_home() folder THIS
    process made, each by its socket's own path (-S), so nothing outside the
    folder can be reached. `tmux` is the command as a list; a tmux server is
    nobody's child, and a stub window still asleep keeps its server up after
    the run that opened it. The names stopped; [] and nothing run for a
    folder make() did not return here, or no tmux."""
    if not tmux or not owned(path):
        return []
    live = _tmux_listening(path)
    for name in live:
        try:
            subprocess.run(list(tmux) + ["-S", os.path.join(
                _tmux_where(path), name), "kill-server"],
                capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    return live


def _tmux_where(path):
    where = os.path.join(path or "", "tmux-%d" % getattr(os, "getuid",
                                                         lambda: 0)())
    return where if path and os.path.isdir(where) else None


def _tmux_listening(path):
    where = _tmux_where(path)
    if not where:
        return []
    live = []
    for name in sorted(os.listdir(where)):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(2)
            s.connect(os.path.join(where, name))
            live.append(name)
        except OSError:
            pass
        finally:
            s.close()
    return live


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
