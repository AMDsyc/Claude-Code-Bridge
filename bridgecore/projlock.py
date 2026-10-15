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

"""One bridge per project folder, across machines.

A project on a network share is the same folder seen from two machines,
and a bridge on each would run two pairs on one set of files - each
writing bridge-logs, each committing, neither knowing. Nothing on either
machine can see the other's processes, so the folder itself carries the
claim: bridge-logs/daemon-lock.json, made with O_CREAT|O_EXCL (one winner,
on a local disk and on SMB alike), naming the machine, the pid, since when,
and a heartbeat the holder moves every BEAT_SEC.

When is a claim dead? Two answers, and NEVER another machine's clock:
- on the SAME machine, its pid is not running - taken over at once;
- anywhere, its heartbeat has not moved for STALE_SEC as measured by THIS
  process's own clock, from the first time this process saw it. The PC's
  and the Mac's clocks need not agree, and a pid on the other machine
  means nothing here.
A taker moves the dead claim aside by rename (one taker gets it), checks it
moved the claim it judged and not a fresh one, and only then races for
O_EXCL again. That is exactly one winner for two takers - a PC and a Mac.
Three taking over the same dead claim in the same milliseconds can leave
two believing they hold it until the loser's next beat says it lost.

It is taken where windows start (sessions.launch), BEFORE install repairs
the project's hooks: those live in the project folder, so a second
machine's repair would point the first machine's live windows at an
interpreter that does not exist there. -> DECISIONS.md 8.87

It is held while this bridge WORKS the folder, not for the bridge's whole
life: daemon.lock_follows_work gives it back while the loop is off or a
person paused, so another machine can take the project, and takes it
again before work resumes. A heartbeat that cannot be written is said
once, not swallowed: unwritten for STALE_SEC, it lets another machine
take the folder. -> DECISIONS.md 8.90
"""

import json
import os
import socket
import threading
import time

from . import store

NAME = "daemon-lock.json"
BEAT_SEC = 30          # the holder moves its heartbeat this often
STALE_SEC = 150        # five missed beats, by the reader's own clock
MACHINE = None         # test seam: None is socket.gethostname()

HELD = {}              # lock path -> the text this process last wrote there
SEEN = {}              # lock path -> (foreign text, monotonic first seen)
FAILS = {}             # lock path -> heartbeats in a row that were not written
_lk = threading.RLock()
_beating = [None]


def machine():
    return MACHINE or socket.gethostname() or "unknown"


def lock_path(project):
    # the folded key, so HELD has one entry per folder: a claim taken under
    # one spelling and asked about under another read as somebody else's,
    # and its next heartbeat said the bridge had lost its own lock (8.90)
    return os.path.join(store.norm(project), "bridge-logs", NAME)


def held_projects():
    """The folders whose claim this process holds, as their keys spell them."""
    with _lk:
        return [os.path.dirname(os.path.dirname(p)) for p in HELD]


def holds(project):
    with _lk:
        return lock_path(project) in HELD


def _read(path):
    """The lock's text; None when there is no lock file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        return ""


def _parse(text):
    try:
        d = json.loads(text or "")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _content(beat=0, since=None):
    now = time.strftime("%Y-%m-%d %H:%M:%S%z")
    return json.dumps({"machine": machine(), "pid": os.getpid(),
                       "since": since or now, "beat": beat, "at": now},
                      ensure_ascii=False)


def _pid_alive(pid):
    from . import sessions      # sessions imports this module
    return sessions.pid_alive(pid)


def _holder(text):
    d = _parse(text)
    if not d:
        return "a claim that cannot be read (being written, or broken)"
    return "the bridge on %s (pid %s) since %s" % (
        d.get("machine") or "?", d.get("pid") or "?", d.get("since") or "?")


def _stale(path, text):
    """(dead?, why). Only this machine's own facts decide."""
    d = _parse(text)
    if d.get("machine") == machine() and d.get("pid") and \
            d.get("pid") != os.getpid():
        try:
            if not _pid_alive(int(d["pid"])):
                return True, "its pid %s is not running on this machine" % (
                    d["pid"])
        except (TypeError, ValueError):
            pass
    now = time.monotonic()
    first = SEEN.get(path)
    if not first or first[0] != text:
        SEEN[path] = (text, now)
        return False, ("its heartbeat is being watched from now: if it does "
                       "not move for %d s, the next try takes it over"
                       % STALE_SEC)
    still = now - first[1]
    if still >= STALE_SEC:
        return True, "its heartbeat has not moved for %d s" % still
    return False, ("its heartbeat has not moved for %d s of the %d that make "
                   "it dead" % (still, STALE_SEC))


def _take_away(path, text):
    """Move the dead claim aside: (the way is clear to race, I moved it)."""
    aside = "%s.dead-%s-%d-%d" % (path, machine(), os.getpid(),
                                 int(time.time() * 1000))
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return True, False      # another taker moved it first
    except OSError:
        return False, False
    moved = _read(aside)
    if moved != text:
        # between the judging and the rename somebody else took it over:
        # what was moved is a FRESH claim - it goes back, and this try loses
        try:
            os.replace(aside, path)
        except OSError:
            pass
        return False, False
    try:
        os.remove(aside)
    except OSError:
        pass
    return True, True


def _say(project, text, level):
    try:
        base = os.path.basename(project.rstrip("\\/")) or project
        store.journal("session", "%s: %s" % (base, text), base,
                      level=level, project_dir=project)
    except Exception:
        pass


def acquire(project):
    """Claim the folder for this process. (True, "") or (False, why)."""
    path = lock_path(project)
    took = None
    with _lk:
        for _ in range(4):
            text = _read(path)
            for _wait in range(10):     # made by O_EXCL, not yet written:
                if text != "":          # judge it once it says who
                    break
                time.sleep(0.05)
                text = _read(path)
            if text is not None:
                d = _parse(text)
                if text == HELD.get(path) or (
                        d.get("machine") == machine()
                        and d.get("pid") == os.getpid()):
                    HELD[path] = text
                    _start_beat()
                    return True, ""
                dead, why = _stale(path, text)
                if not dead:
                    return False, _refusal(project, text, why)
                clear, moved = _take_away(path, text)
                if not clear:
                    continue
                if moved:
                    took = (text, why)
            mine = _content()
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | getattr(os, "O_BINARY", 0))
            except FileExistsError:
                continue        # lost the race: judge the winner's claim
            except OSError as exc:
                return False, ("%s: the project lock %s could not be made "
                               "(%s); nothing was started" % (
                                   os.path.basename(project.rstrip("\\/")),
                                   path, exc))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(mine)
                fh.flush()
                os.fsync(fh.fileno())
            HELD[path] = mine
            SEEN.pop(path, None)
            _start_beat()
            if took:            # said once it is really ours, not on moving
                _say(project, "took the project lock over from %s - %s"
                     % (_holder(took[0]), took[1]), "warn")
            return True, ""
        text = _read(path)
        return False, _refusal(project, text,
                               "several tries in a row lost the race for it")


def _refusal(project, text, why):
    base = os.path.basename(project.rstrip("\\/")) or project
    return ("%s is held by %s; %s. Two bridges starting windows in one "
            "folder would run two pairs on the same files, so nothing was "
            "started. Stop that bridge - or, if it is gone, try again once "
            "its heartbeat has stood still %d s (%s)."
            % (base, _holder(text), why, STALE_SEC, lock_path(project)))


def beat():
    """Move the heartbeat of every claim this process holds.

    A claim found changed or gone was taken over (this process slept
    through STALE_SEC, or the folder went away): it is dropped and said,
    never written back - writing it back is two holders."""
    with _lk:
        for path, mine in list(HELD.items()):
            now = _read(path)
            project = os.path.dirname(os.path.dirname(path))
            if now != mine:
                HELD.pop(path, None)
                FAILS.pop(path, None)
                if not os.path.isdir(project):
                    # the folder went - deleted, or its share dropped, which
                    # from here look the same. Said, in the bridge's own
                    # log: a share that went silent took the lock with it
                    # (M4, 8.90; it used to be a `continue` and nothing)
                    _say(project, "the project folder cannot be reached, so "
                         "its lock is no longer held here. Windows already "
                         "running here go on; no new one starts until the "
                         "folder is back and the lock is this bridge's "
                         "again", "warn")
                    continue
                _say(project, "the project lock is no longer this bridge's: "
                     "it is %s now. Windows already running here go on; no "
                     "new one starts until the lock is this bridge's again"
                     % (_holder(now) if now is not None else "gone"), "warn")
                continue
            d = _parse(mine)
            text = _content(int(d.get("beat") or 0) + 1, d.get("since"))
            tmp = "%s.beat-%d" % (path, os.getpid())
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, path)
                HELD[path] = text
            except OSError as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                # said on the first miss, not every one: a share that is
                # slow or gone answers like this, and unwritten for
                # STALE_SEC the claim reads as dead to another machine
                FAILS[path] = FAILS.get(path, 0) + 1
                if FAILS[path] == 1:
                    _say(project, "the heartbeat of the project lock could "
                         "not be written (%s). Unwritten for %d s, it lets "
                         "another machine take this folder; it is tried "
                         "again every %d s" % (exc, STALE_SEC, BEAT_SEC),
                         "warn")
                continue
            missed = FAILS.pop(path, 0)
            if missed:
                _say(project, "the heartbeat of the project lock is written "
                     "again, after %d that were not" % missed, "log")


def _beat_loop():
    # short sleeps against a live BEAT_SEC, so a changed figure is obeyed
    # at once and not after the sleep that was already under way. It ends
    # once nothing is held: a beat with nothing to move outlived every
    # release, and test_multipair's last case found it as the one bridge
    # thread that never ended (2026-10-12). Decided under _lk, where
    # acquire starts a beat - a claim taken at that moment gets a new one.
    me = threading.current_thread()
    last = time.monotonic()
    while True:
        time.sleep(max(0.05, min(1.0, BEAT_SEC)))
        with _lk:
            if not HELD:
                if _beating[0] is me:
                    _beating[0] = None
                return
        if time.monotonic() - last < BEAT_SEC:
            continue
        last = time.monotonic()
        try:
            beat()
        except Exception:
            pass


def _start_beat():
    t = _beating[0]
    if t is None or not t.is_alive():
        t = threading.Thread(target=_beat_loop, name="projlock-beat",
                             daemon=True)
        _beating[0] = t
        t.start()


def release(project=None):
    """Give back one claim, or every claim this process holds. Returns how
    many it held - the caller says it, only when there was one."""
    gave = 0
    with _lk:
        paths = [lock_path(project)] if project else list(HELD)
        for path in paths:
            mine = HELD.pop(path, None)
            FAILS.pop(path, None)
            if mine is None:
                continue
            gave += 1
            if _read(path) == mine:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return gave
