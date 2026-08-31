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

"""Finish the folder rebuild without anybody having to do anything.

The layout changed on 2026-08-19: `bridge/` became `source/`, the package
`bridge/bridge/` became `source/bridgecore/`, and the launchers moved to the
top. Everything except the last move was done while the daemon was running,
because a folder cannot be renamed out from under a live process.

That last move was, briefly, a file the owner had to run himself, and he
said plainly that it should not be his job. He was right: a rebuild that
ends in a manual step is a rebuild that ends in somebody's hands, and this
one had already cost a wrong restart - the old bridge started again from
inside the package folder, which left a second data/ in there holding a
config with one project in it instead of four.

So this module finishes it. Two ways in, and neither is a step anybody takes
on purpose:

    python -m bridgecore.relayout            silent no-op if there is
                                             nothing to move; bridge.bat
                                             calls this before every start
    python -m bridgecore.relayout --now      stop the running daemon, move
                                             everything, start it again, and
                                             put it all back if the new one
                                             does not come up

Nothing here is one-way. Everything that could be lost is zipped into
releases/ BEFORE the first file moves, and the --now path restores from that
zip and restarts the old bridge if the new one fails to answer. The owner
must never be left with a bridge that does not run.
"""

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                  # .../source
BASE = os.path.dirname(ROOT)                  # .../Bridge
OLD = os.path.join(BASE, "bridge")            # the tree being retired
DATA = os.path.join(ROOT, "data")
RELEASES = os.path.join(BASE, "releases")
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))

# Everything a data folder holds, and how to treat it. The single files are
# taken from whichever folder won on content; the two directories are MERGED
# from every candidate, because a journal is evidence and losing one because
# it sat in the folder that lost a vote would be the worst outcome here.
DATA_FILES = ("state.json", "calibration.json", "models.json",
              "profiles.json")
DATA_DIRS = ("logs", "backups")


def say(msg):
    print(msg)
    sys.stdout.flush()


# The owner's decision, written where the code can read it. A marker file
# rather than a config key on purpose: config.json is rewritten from memory
# by a running daemon, and this has to survive being read by a process that
# runs BEFORE any daemon exists.
KEEP_MARK = "KEPT-ON-PURPOSE.txt"


def kept_on_purpose(base=None):
    """Has the owner said the old tree stays?

    On 2026-08-19 the migration finished except for its last step: the
    delete failed on read-only git objects (WinError 5). The owner then
    decided the tree stays, and said why: not because the folder is
    wanted, but because we had not shown that removing it could be done
    without risk. That decision lived only in CLAUDE.md.

    A decision that only a document knows is not a decision, it is a wish:
    bridge.bat runs this module BEFORE the daemon at every single start, so
    the very next start by anybody, for any reason, would have deleted the
    tree regardless of what the document said - and the read-only retry
    added the same morning would by then have made it succeed. This is the
    gate under rule 24, and the mark is a file inside the tree it protects,
    so anyone wondering why it survives finds the answer standing in it.

    It is honoured only when the new layout is actually complete. A marker
    dropped into a half-moved tree must not strand the move.
    """
    base = base or BASE
    if not os.path.isfile(os.path.join(base, "bridge", KEEP_MARK)):
        return False
    return os.path.isfile(os.path.join(base, "source", "data", "config.json"))


def names_retired(text, base=None):
    """Does this text name the retired tree, or merely start like it?

    A substring test is wrong here and was wrong in practice: the launcher
    everybody is meant to use is `<base>\\bridge.bat`, which begins with the
    same characters as `<base>\\bridge`. The first census of who still used
    the old tree found exactly one user, and it was this mistake rather
    than a fact. The path counts only when the component ends after it.
    """
    old = os.path.normcase(os.path.join(base or BASE, "bridge"))
    # Forward slashes and JSON's doubled backslashes both have to fold down
    # to one form first. A path inside .mcp.json or settings.json is stored
    # with every separator written twice, and matching the raw text missed
    # every one of them - the case that catches an .mcp.json relapse is
    # what found this, after the gate had already been called finished.
    hay = os.path.normcase(str(text or "")).replace("/", "\\")
    while "\\\\" in hay:
        hay = hay.replace("\\\\", "\\")
    i = 0
    while True:
        i = hay.find(old, i)
        if i < 0:
            return False
        if hay[i + len(old):i + len(old) + 1] in ("", "\\", '"', "'", " ", ";"):
            return True
        i += 1


def retired_tree_users(config, base=None):
    """Every place that would send work back into the retired tree.

    The owner's requirement is not that the folder disappear - it stays -
    but that nothing use it. That was an assertion until it was checked,
    and checking found a live channel process running out of it. So the
    question gets asked by the bridge from now on, rather than remembered.

    Returns a list of (place, value). Empty is the healthy answer. Nothing
    here changes a setting: it reports, and the caller decides.
    """
    base = base or BASE
    found = []
    for path in (config.get("projects") or {}):
        if names_retired(path, base):
            found.append(("a watched project", path))
        s = os.path.join(path, ".claude", "settings.json")
        try:
            with io.open(s, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        env = data.get("env") or {}
        for key in ("PYTHONPATH", "PYTHONSAFEPATH"):
            if names_retired(env.get(key), base):
                found.append(("%s env %s" % (path, key), env.get(key)))
        for _event, entries in (data.get("hooks") or {}).items():
            for entry in entries or []:
                for h in entry.get("hooks") or []:
                    if names_retired(h.get("command"), base):
                        found.append(("%s hook" % path, h.get("command")))
        if names_retired(json.dumps(data.get("statusLine") or {}), base):
            found.append(("%s statusLine" % path, "see settings.json"))
        m = os.path.join(path, ".mcp.json")
        try:
            with io.open(m, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        if names_retired(text, base):
            found.append(("%s .mcp.json" % path, "names the retired tree"))
    return found


def pending(base=None):
    """Is there an old tree still to move?

    Answers no for a tree that is being kept deliberately: there is nothing
    left to do with it, and saying yes made the whole migration re-run at
    every start, writing another ~9 MB backup zip into releases/ each time.
    """
    base = base or BASE
    if kept_on_purpose(base):
        return False
    return os.path.isdir(os.path.join(base, "bridge"))


def data_dirs(old):
    """Every data folder the old tree ended up with.

    There are two, and the second one is the accident this fixes: the bridge
    was once started from inside the package folder, so it made itself a
    fresh data/ in there and wrote a config with one project in it.
    """
    found = []
    for rel in ("data", os.path.join("bridge", "data")):
        p = os.path.join(old, rel)
        if os.path.isdir(p):
            found.append(p)
    return found


def score_config(path):
    """How complete a config is. Bigger is better, compared as a tuple.

    Judged by CONTENT, never by where it sits: the full one happens to be in
    bridge/data today, but a rule about paths would silently pick the wrong
    file the next time the accident takes a different shape. Projects first,
    because that is what the owner actually loses.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return (-1, 0, 0, 0)
    projects = cfg.get("projects") or {}
    telegram = cfg.get("telegram") or {}
    marks = cfg.get("pair_marks") or cfg.get("marks") or {}
    return (len(projects), 1 if telegram.get("token") else 0,
            1 if marks else 0, os.path.getsize(path))


def pick_config(paths):
    """(winner, [losers]) among candidate config files."""
    live = [p for p in paths if os.path.isfile(p)]
    if not live:
        return None, []
    ranked = sorted(live, key=score_config, reverse=True)
    return ranked[0], ranked[1:]


def backup(base, tag="relayout"):
    """Everything that could be lost, before anything moves."""
    os.makedirs(os.path.join(base, "releases"), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(base, "releases", "backup-%s-%s.zip" % (stamp, tag))
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in ("bridge", "source"):
            root = os.path.join(base, rel)
            if not os.path.isdir(root):
                continue
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if d != "__pycache__"]
                for fn in fns:
                    full = os.path.join(dp, fn)
                    try:
                        z.write(full, os.path.relpath(full, base))
                        n += 1
                    except OSError:
                        pass
    return out, n


def _copy_missing(src, dst):
    """Merge src into dst without ever overwriting. Returns files added."""
    added = 0
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d != "__pycache__"]
        rel = os.path.relpath(dp, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in fns:
            d = os.path.join(target, fn)
            if os.path.exists(d):
                continue
            try:
                shutil.copy2(os.path.join(dp, fn), d)
                added += 1
            except OSError:
                pass
    return added


def patiently(fn, what, tries=8, wait=2.0, out=say):
    """Windows hands out transient locks on folders just written to."""
    for attempt in range(tries):
        try:
            return fn()
        except PermissionError:
            if attempt == tries - 1:
                raise
            out("   %s is busy, waiting" % what)
            time.sleep(wait)


def _unlock(path):
    """Clear the read-only flag. Returns True if there was one to clear."""
    try:
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWRITE)
            return True
    except OSError:
        pass
    return False


def _retry_readonly(func, path, _exc):
    """rmtree's error hook: clear read-only and do that one step again."""
    if _unlock(path):
        func(path)
    else:
        raise


def remove_tree(path, out=say, tries=8, wait=2.0):
    """Delete a folder, including the parts git marks read-only.

    Two different things wear the same PermissionError on Windows and they
    need opposite answers, which is why they are told apart here rather than
    both being called "busy":

      - a file or folder held open by a process. Waiting is the answer, and
        it usually works within a second or two.
      - a file marked read-only. Waiting is useless - it will be read-only
        for ever. git does this to everything under .git/objects, so any
        tree that has ever been a repository hits it, and this one did:
        rmtree stopped on .git/objects/10/82bb... and the whole move
        reported "busy", which sent the reader looking for a process that
        was never there.
    """
    kw = ({"onexc": _retry_readonly} if sys.version_info >= (3, 12)
          else {"onerror": lambda f, p, e: _retry_readonly(f, p, e)})
    for attempt in range(tries):
        try:
            shutil.rmtree(path, **kw)
            return True, ""
        except PermissionError as exc:
            stuck = getattr(exc, "filename", None) or path
            if _unlock(stuck):
                out("   %s was read-only, flag cleared - carrying on"
                    % os.path.basename(stuck))
                continue
            if attempt == tries - 1:
                return False, ("%s is held by another process: %s"
                               % (os.path.basename(stuck), exc))
            out("   %s is held by a process, waiting"
                % os.path.basename(stuck))
            time.sleep(wait)
        except OSError as exc:
            return False, str(exc)
    return False, "gave up after %d attempts" % tries


def reinstall(base, config_path):
    """Re-run the installer in every watched project. Returns lines to show.

    The config keys are stored as they were typed, so one project can appear
    twice under two spellings - one of them is in there right now with
    capitals and without. normcase/normpath is what the daemon keys
    everything by, so that is what decides identity here too, and the folder
    is installed once rather than twice.
    """
    lines = []
    try:
        with open(config_path, encoding="utf-8") as fh:
            projects = (json.load(fh).get("projects") or {})
    except (OSError, ValueError) as exc:
        return ["could not read the config, so no project was reinstalled: "
                "%s" % exc]
    seen = set()
    for raw in projects:
        key = os.path.normcase(os.path.normpath(raw))
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isdir(raw):
            lines.append("%s - not on disk, skipped" % raw)
            continue
        try:
            r = subprocess.run(
                [sys.executable, "-m", "bridgecore.install", raw,
                 "--role", "executor"],
                cwd=os.path.join(base, "source"), capture_output=True,
                text=True, errors="replace", timeout=120)
            lines.append("%s - %s" % (raw, "ok" if r.returncode == 0
                                      else "FAILED: %s"
                                      % (r.stderr or r.stdout or "")[-200:]))
        except (OSError, subprocess.SubprocessError) as exc:
            lines.append("%s - could not run the installer: %s" % (raw, exc))
    return lines or ["no projects are watched yet"]


def migrate(base=None, out=say, assume_stopped=False, port=None):
    """Move the old tree into the new one. Safe to run twice.

    Returns a short dict describing what happened, so a caller (or a suite)
    can check rather than read the console.
    """
    base = base or BASE
    old = os.path.join(base, "bridge")
    new = os.path.join(base, "source")
    data = os.path.join(new, "data")
    if not os.path.isdir(old):
        return {"moved": False, "why": "nothing to move"}
    if not os.path.isdir(new):
        return {"moved": False, "why": "there is no source folder to move "
                                       "into - refusing"}
    # Never with a live daemon. This function moves the state file and the
    # journals a running bridge is writing to, and half of that is worse
    # than none of it: it would be reading one data folder and writing
    # another. The two callers that are allowed here have already made sure
    # - bridge.bat runs before the daemon starts, and the panel button waits
    # for the port to go quiet - so anyone reaching this with the port open
    # is doing it by hand, and gets told rather than obeyed.
    if not assume_stopped and port_open(port):
        return {"moved": False,
                "why": "the bridge is still running on 127.0.0.1:%d. Nothing "
                       "was moved - moving the state out from under a live "
                       "daemon would leave it reading one folder and writing "
                       "another. Stop it first, or press the rebuild button "
                       "in the panel, which stops it for you."
                       % (port or PORT)}

    kept, files = backup(base)
    out("   the old layout is saved first: %s (%d files)"
        % (os.path.basename(kept), files))

    os.makedirs(data, exist_ok=True)
    folders = data_dirs(old)

    # The config, chosen by what is in it.
    winner, losers = pick_config([os.path.join(d, "config.json")
                                  for d in folders])
    projects = 0
    if winner:
        projects = score_config(winner)[0]
        if not os.path.exists(os.path.join(data, "config.json")):
            shutil.copy2(winner, os.path.join(data, "config.json"))
        out("   config taken from %s - %d projects in it"
            % (os.path.relpath(winner, base), projects))
    for lost in losers:
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        ev = os.path.join(base, "releases",
                          "config-not-used-%s.json" % stamp)
        try:
            shutil.copy2(lost, ev)
            out("   the other config had %d projects and was NOT used; it is "
                "kept as %s" % (score_config(lost)[0], os.path.basename(ev)))
        except OSError:
            pass

    # The rest of the state travels with the config that won.
    home = os.path.dirname(winner) if winner else (folders[0] if folders
                                                   else None)
    for name in DATA_FILES:
        if not home:
            break
        src, dst = os.path.join(home, name), os.path.join(data, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    # Journals and backups are merged from BOTH, never overwritten.
    added = 0
    for folder in folders:
        for name in DATA_DIRS:
            src = os.path.join(folder, name)
            if os.path.isdir(src):
                added += _copy_missing(src, os.path.join(data, name))
    if added:
        out("   %d journal and backup files carried over, none overwritten"
            % added)

    # The git history and the project's own logs.
    for name in (".git", "bridge-logs"):
        src, dst = os.path.join(old, name), os.path.join(new, name)
        if os.path.exists(src) and not os.path.exists(dst):
            patiently(lambda s=src, d=dst: shutil.move(s, d), name)

    # BEFORE the delete, not after. The first live run crashed inside
    # rmtree - git marks its objects read-only - and everything below it
    # was skipped, so every project was left with hooks naming a folder
    # that had just been removed. Nothing here needs the old tree gone,
    # so nothing here should wait for it.
    #
    # Every watched project's hooks name the package and the folder it is
    # imported from. Both just changed, so every project needs the installer
    # run again - without this the hooks point at a folder that no longer
    # exists, and because hook.py exits 0 whatever happens (an edge path must
    # never kill the session someone is working in) the loop would simply
    # stop, silently, in every project at once.
    out("   pointing every project's hooks at the new package")
    for line in reinstall(base, os.path.join(data, "config.json")):
        out("      " + line)

    if kept_on_purpose(base):
        # Belt as well as braces: pending() already keeps us out of here,
        # but this is the line that actually deletes, and the decision it
        # answers to was made about THIS folder rather than about whether
        # a migration was due.
        out("   the old folder is kept on purpose (%s) - not removing it"
            % KEEP_MARK)
        return {"moved": True, "backup": kept, "config": winner,
                "projects": projects, "not_used": losers, "merged": added,
                "kept": True}
    gone, why = remove_tree(old, out=out)
    if not gone:
        out("   the old folder could NOT be removed: %s" % why)
        out("   everything else moved, and the bridge will run from source/;"
            " the leftover folder is safe to delete by hand")
    else:
        out("   the old folder is gone; everything lives in source/ now")

    # The manual step this replaced.
    stale = os.path.join(base, "finish-layout.bat")
    if os.path.exists(stale):
        try:
            os.remove(stale)
            out("   removed finish-layout.bat - there is no step to take")
        except OSError:
            pass
    return {"moved": True, "backup": kept, "config": winner,
            "projects": projects, "not_used": losers, "merged": added}


# ---- the part that needs the daemon out of the way -----------------------

def port_open(port=None, host="127.0.0.1", timeout=1.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port or PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pid_on_port(port=None):
    """The pid listening on the port, from netstat. None if nobody is."""
    port = port or PORT
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True,
                           text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" \
                and parts[1].endswith(":%d" % port) \
                and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def process_alive(pid):
    """Is that pid still a running process?"""
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                           capture_output=True, text=True, errors="replace",
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True                      # cannot tell: assume the worst
    return str(pid) in (r.stdout or "")


# What a child process runs to close somebody else's console window.
#
# It lives here as source rather than as a function because AttachConsole is
# per-PROCESS and there is no safe way back: a process that has swapped its
# console cannot reliably return to the one it started with, and `--now` is
# watched by a person, so every line printed after that would go nowhere.
# A child created with CREATE_NO_WINDOW has no console of its own to lose.
#
# argv[1] is the pid whose console to close; argv[2] is the caller's own
# console window, or 0. The guard is not theoretical - a helper that closed
# the window it was reporting into would take the report with it.
#
# FreeConsole FIRST, and it is load-bearing: CREATE_NO_WINDOW gives a child
# a console with no window, NOT no console, and AttachConsole answers
# ERROR_ACCESS_DENIED (5) to a process that already has one. Dropping that
# one line made every close fail while stop_daemon still returned True -
# case 103 caught it, which is the entire reason the case asks the stub
# whether its handler ran instead of asking the caller how it went.
_CLOSE_CONSOLE_SRC = """
import ctypes, sys
pid = int(sys.argv[1])
mine = int(sys.argv[2])
k = ctypes.WinDLL('kernel32', use_last_error=True)
u = ctypes.WinDLL('user32', use_last_error=True)
k.GetConsoleWindow.restype = ctypes.c_void_p
u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_void_p]
k.FreeConsole()
if not k.AttachConsole(pid):
    print('pid %d has no console to attach to (error %d)'
          % (pid, ctypes.get_last_error()))
    raise SystemExit(2)
h = k.GetConsoleWindow()
if not h:
    print('attached to the console of pid %d, but it has no window' % pid)
    raise SystemExit(3)
if mine and int(h) == mine:
    print('that is my own console window - refusing to close it')
    raise SystemExit(4)
if not u.PostMessageW(ctypes.c_void_p(h), 0x0010, None, None):
    print('WM_CLOSE would not post to window %d (error %d)'
          % (int(h), ctypes.get_last_error()))
    raise SystemExit(5)
# LET GO OF IT BEFORE IT GOES. This process is attached to the console it
# has just closed, and Windows takes every attached process down with the
# window - so without this line the helper is killed on the way out, its
# exit code is not 0, and the caller reads a success as a failure. Measured:
# stop_daemon then said 'no console window' in the same breath as 'closed
# console window 3802802', and ran the taskkill fallback for nothing.
k.FreeConsole()
print('closed console window %d of pid %d' % (int(h), pid))
"""


def _own_console_window():
    """This process's console window, as an int, or 0. Never attaches."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32")
        k.GetConsoleWindow.restype = ctypes.c_void_p
        return int(k.GetConsoleWindow() or 0)
    except Exception:
        return 0


def close_console(pid, timeout=20):
    """Close the console WINDOW that pid is attached to. Returns (ok, why).

    THE WINDOW IS NOT THE PROCESS'S. This is the whole reason the polite
    branch below used to be a no-op. `taskkill /PID` without /F posts
    WM_CLOSE to top-level windows owned by threads of the TARGET process,
    and a console application owns none of them: the window belongs to
    whoever created the console. Measured on 2026-08-31 against the live
    bridge - console window 134498 owned by cmd.exe 12032, the batch a
    double-click started, with py.exe and then the daemon's python.exe
    below it in that same console. taskkill answered "can only be
    terminated forcefully", nobody read the return code, the loop waited
    its full 45 seconds and then used /F - which skips the console handler,
    so clean_shutdown was never written and the next start came up in
    'recovered' mode reporting a crash that had not happened. Precisely
    what stop_daemon's docstring says it exists to avoid.

    The target is reachable without guessing at anybody's ancestry: attach
    to that console and ask THAT console for its window. Windows then sends
    every process attached to it CTRL_CLOSE_EVENT, which is the event the
    daemon's SetConsoleCtrlHandler is waiting for - so this is the same
    path as the X button, and RESTART.md's "close the window" said so all
    along.

    One case this does NOT cover, and does not pretend to: under a tabbed
    host one window can carry several tabs, and closing it would take them
    all. What was measured here is the classic console a double-clicked
    bridge.bat gives. If that ever changes, this is the paragraph to read.
    """
    if os.name != "nt":
        return False, "not Windows - there is no console window to close"
    try:
        r = subprocess.run(
            [sys.executable, "-c", _CLOSE_CONSOLE_SRC,
             str(pid), str(_own_console_window())],
            capture_output=True, text=True, errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "could not ask: %s" % exc
    why = ((r.stdout or "").strip() or (r.stderr or "").strip()
           or "no answer from the helper")
    return r.returncode == 0, why


def stop_daemon(port=None, timeout=90):
    """Ask the daemon to stop the way closing its window would.

    Closing the console WINDOW sends every process attached to that console
    a close event, and the daemon has a handler on it: it writes
    clean_shutdown, says goodbye in Telegram and exits. /F skips all of
    that, and the next start comes up in 'recovered' mode telling the owner
    about a crash that never happened - so it is the last thing tried,
    never the first.

    Until 2026-08-31 the polite branch was `taskkill /PID` alone, which
    cannot reach a console application at all - see close_console() for the
    measurement. It failed on every run, silently, because nothing read its
    return code; the only visible symptoms were a 45-second pause and a
    'recovered' banner nobody could account for. The return value did not
    change between the two outcomes, which is why it went unnoticed for as
    long as it did, and why the case for this is an end-to-end one.

    Waiting on the PROCESS, not on the port. Measured in simulation: after a
    polite taskkill the port stopped accepting connections while netstat
    still showed it LISTENING on the dead pid. Had this returned there, the
    replacement would have bound the same port alongside the corpse - on
    Windows SO_REUSEADDR permits exactly that - and every connection would
    have gone to the socket that no longer had a process behind it. The
    bridge would have looked started and answered nothing.
    """
    pid = pid_on_port(port)
    if pid is None:
        return True, "the bridge was not running"
    # NOT /T. The claude windows are children of the daemon, and the tree
    # form of taskkill would take every pair down with it - including the
    # session that asked for this. The bridge is built the other way round:
    # the sessions outlive the daemon and simply stop being carried while it
    # is away, which is what makes a restart cheap.
    closed, why_close = close_console(pid)
    if not closed:
        # Nothing to close - a daemon started some other way, or a console
        # with no window. Fall back to what this used to do on its own: it
        # is harmless where it does not apply, and it IS right for a
        # process that genuinely owns a window of its own.
        subprocess.run(["taskkill", "/PID", str(pid)],
                       capture_output=True, text=True, errors="replace")
    polite = min(timeout, 45)
    end = time.time() + polite
    while time.time() < end:
        if not process_alive(pid) and not port_open(port):
            return True, "stopped cleanly (pid %d, %s)" % (
                pid, why_close if closed else "no console window: " +
                why_close + "; taskkill instead")
        time.sleep(1.5)
    # It did not take the hint. A bridge that will not stop is worse than a
    # 'recovered' banner, so escalate - and say plainly what it cost.
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, text=True, errors="replace")
    end = time.time() + max(timeout - polite, 20)
    while time.time() < end:
        if not process_alive(pid) and not port_open(port):
            return True, ("pid %d did not stop after the close request "
                          "(%s), so it had to be killed - the next start "
                          "will report a recovery, and that is this, not a "
                          "crash" % (pid, why_close))
        time.sleep(1.5)
    return False, "pid %d is still there after %ds" % (pid, timeout)


def start_daemon(base=None, wait=180, port=None):
    """Start the bridge from the base folder and wait for it to answer."""
    base = base or BASE
    bat = os.path.join(base, "bridge.bat")
    if not os.path.exists(bat):
        return False, "there is no bridge.bat in %s" % base
    # A new console, not a detached process: the two flags are mutually
    # exclusive on Windows, and the bridge is meant to have a window the
    # owner can see and close.
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" \
        else 0
    # Through cmd, not straight at the .bat: CreateProcess cannot launch a
    # batch file itself, and when it is handed one with a relative name it
    # resolves it against the PARENT's working directory rather than the cwd
    # given here. Measured - Popen(["hello.bat"], cwd=...) raises WinError 2.
    # A bridge is very often restarted from inside somebody's Claude Code
    # session - `relayout --now` is a command a pair runs. Without this the
    # daemon inherits that session's client markers and hands them down to
    # every window it later opens, which the client reads as a nested run
    # and answers by not saving a transcript. That happened on 2026-08-21:
    # a rotated window came up with "Transcript saving is off", so the
    # bridge could read neither its rc link nor its context size, and the
    # damage outlived the restart that caused it. The cure has to be at the
    # spawn, or the next restart-from-a-session brings it straight back.
    try:
        from . import sessions as _sessions
        clean = _sessions.clean_env()
    except Exception:
        clean = None
    try:
        subprocess.Popen(["cmd", "/c", bat] if os.name == "nt" else [bat],
                         cwd=base, creationflags=flags, close_fds=True,
                         env=clean)
    except OSError as exc:
        return False, "could not start it: %s" % exc
    end = time.time() + wait
    while time.time() < end:
        if port_open(port):
            return True, "the bridge is answering again"
        time.sleep(2)
    return False, "it did not answer within %ds" % wait


def restore(base, kept, out=say):
    """Put everything back exactly as the backup found it."""
    base = base or BASE
    old = os.path.join(base, "bridge")
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    with zipfile.ZipFile(kept) as z:
        z.extractall(base)
    out("   restored from %s" % os.path.basename(kept))
    return True


def wait_port_free(port=None, timeout=120):
    """Wait for a daemon that is already stopping to let go of its port.

    This is the panel-button path: the daemon spawns this helper and then
    shuts itself down through its own normal path, so there is nobody to
    kill - only somebody to wait for. Waiting rather than killing is what
    keeps the clean_shutdown record and the goodbye message intact.
    """
    end = time.time() + timeout
    while time.time() < end:
        if not port_open(port):
            return True, "the bridge has stopped"
        time.sleep(1.5)
    return False, "the bridge is still holding its port after %ds" % timeout


def run_now(base=None, port=None, start_wait=180, out=say, stop=True):
    """The whole thing, with a way back. Returns a dict.

    stop=False means the daemon is already on its way out - it asked for
    this itself - so wait for the port instead of killing anything.
    """
    base = base or BASE
    out("Bridge: finishing the folder rebuild.")
    if stop:
        ok, why = stop_daemon(port)
    else:
        ok, why = wait_port_free(port)
    out("   %s" % why)
    if not ok:
        return {"ok": False, "stage": "stop", "why": why}

    result = migrate(base, out=out, assume_stopped=True, port=port)
    if not result.get("moved"):
        out("   nothing to move - starting again")
        started, why = start_daemon(base, start_wait, port)
        return {"ok": started, "stage": "start", "why": why}

    started, why = start_daemon(base, start_wait, port)
    if started:
        out("   %s" % why)
        return dict(result, ok=True, stage="done", why=why)

    out("   the new layout did not come up: %s" % why)
    out("   putting everything back")
    restore(base, result["backup"], out=out)
    back, why2 = start_daemon(base, start_wait, port)
    out("   the old bridge is %s" % ("running again" if back else
                                     "NOT running: %s" % why2))
    return {"ok": False, "stage": "rolled back", "why": why,
            "restored": True, "old_running": back}


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", write_through=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    port = None
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    base = BASE
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]

    # The panel button: the daemon spawned this and is shutting itself down.
    if "--after-shutdown" in argv:
        r = run_now(base, port, stop=False)
        if not r.get("ok"):
            say("")
            say("The rebuild did not take, and everything has been put back.")
            say("Reason: %s" % r.get("why"))
            say("This window is left open on purpose - the lines above are "
                "the whole story.")
            try:
                input("Press Enter to close. ")
            except (EOFError, OSError):
                pass
        return 0 if r.get("ok") else 1

    if "--now" in argv:
        r = run_now(base, port)
        return 0 if r.get("ok") else 1

    # The quiet path, called by bridge.bat before every start. Silent when
    # there is nothing to do, which is every start after the first.
    if not pending(base):
        return 0
    say("Bridge: finishing the folder rebuild before starting.")
    migrate(base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
