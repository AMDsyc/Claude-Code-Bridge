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


def pending(base=None):
    """Is there an old tree still to move?"""
    return os.path.isdir(os.path.join(base or BASE, "bridge"))


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


def patiently(fn, what, tries=8, wait=2.0):
    """Windows hands out transient locks on folders just written to."""
    for attempt in range(tries):
        try:
            return fn()
        except PermissionError:
            if attempt == tries - 1:
                raise
            say("   %s is busy, waiting" % what)
            time.sleep(wait)


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


def migrate(base=None, out=say):
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

    patiently(lambda: shutil.rmtree(old), "the old folder")
    out("   the old folder is gone; everything lives in source/ now")

    # Every watched project's hooks name the package and the folder it is
    # imported from. Both just changed, so every project needs the installer
    # run again - without this the hooks point at a folder that no longer
    # exists, and because hook.py exits 0 whatever happens (an edge path must
    # never kill the session someone is working in) the loop would simply
    # stop, silently, in every project at once.
    out("   pointing every project's hooks at the new package")
    for line in reinstall(base, os.path.join(data, "config.json")):
        out("      " + line)

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


def stop_daemon(port=None, timeout=90):
    """Ask the daemon to stop the way closing its window would.

    taskkill WITHOUT /F sends the console a close event, and the daemon has a
    handler on it: it writes clean_shutdown, says goodbye in Telegram and
    exits. /F skips all of that, and the next start comes up in 'recovered'
    mode telling the owner about a crash that never happened - so it is the
    second thing tried, never the first.

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
    subprocess.run(["taskkill", "/PID", str(pid)],
                   capture_output=True, text=True, errors="replace")
    polite = min(timeout, 45)
    end = time.time() + polite
    while time.time() < end:
        if not process_alive(pid) and not port_open(port):
            return True, "stopped cleanly (pid %d)" % pid
        time.sleep(1.5)
    # It did not take the hint. A bridge that will not stop is worse than a
    # 'recovered' banner, so escalate - and say plainly what it cost.
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, text=True, errors="replace")
    end = time.time() + max(timeout - polite, 20)
    while time.time() < end:
        if not process_alive(pid) and not port_open(port):
            return True, ("pid %d ignored the close request and had to be "
                          "killed, so the next start will report a recovery "
                          "- that is this, not a crash" % pid)
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
    try:
        subprocess.Popen(["cmd", "/c", bat] if os.name == "nt" else [bat],
                         cwd=base, creationflags=flags, close_fds=True)
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


def run_now(base=None, port=None, start_wait=180, out=say):
    """The whole thing, with a way back. Returns a dict."""
    base = base or BASE
    out("Bridge: finishing the folder rebuild.")
    ok, why = stop_daemon(port)
    out("   %s" % why)
    if not ok:
        return {"ok": False, "stage": "stop", "why": why}

    result = migrate(base, out=out)
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
