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

"""Start, track and stop the Claude Code sessions of a project.

Sessions run in real, visible (minimised) console windows and are tracked
by PID, so the bridge can rotate the executor by actually ending the old
process before starting the new one - never two live sessions fighting
over the same seat.
"""

import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import projlock, store

CREATE_NEW_CONSOLE = 0x00000010
SW_SHOWMINNOACTIVE = 7

ROLE_DEFAULTS = {
    "executor": {"permission_mode": "auto", "title": "Executor"},
    # auto is only the default - the panel can set any mode for either role;
    # it was plan until 2026-09-30 (DECISIONS 8.59)
    "planner": {"permission_mode": "auto", "title": "Planner"},
}

# (project, role) -> Popen, and the project is store.norm'd - the same key
# the daemon uses for everything else. It was os.path.normpath here alone,
# which folds separators but not case, so a project reached by a differently
# cased path got a second entry: launch() recorded the window under one
# spelling and alive()/stop() looked for it under another, found nothing,
# and reported a live session as gone.
PROCS = {}

# THE NAME launch() gives the window it opens, asked of the daemon: a
# callable (project, role, resume_id) -> "<Project> <Role> NNNN", or
# None for no name. Asked here, at the one place every window starts,
# and not passed in by each caller: there are seven, and a name that
# depends on each of them remembering it is the one the eighth forgets.
# -> DECISIONS.md 8.75
WINDOW_NAME = None


# Markers the CLIENT puts in the environment of anything it spawns. They
# say "you are running inside a Claude Code session", and a window that
# inherits them is treated as a nested run: on 2026-08-21 that showed up as
# "Transcript saving is off", and a window with no transcript is one the
# bridge cannot read an rc link OR a context size from - blind for the
# whole of its life, with the wall accounting silently guessing.
#
# How it got in: the daemon was restarted at 10:57 with
# `python -m bridgecore.relayout --now` typed INSIDE a Claude Code session,
# so the daemon inherited that session's environment, and launch() copies
# os.environ wholesale into every window it opens.
#
# The list is narrow on purpose - NOT everything matching CLAUDE_*. ONE of
# those is ours and must survive, CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, and
# stripping by prefix would take it too.
#
# CLAUDE_AUTOCOMPACT_PCT_OVERRIDE stood in that same sentence as "ours"
# until 2026-09-01. It is not ours any more - launch() sets no compaction
# threshold, by the owner's decision that the bridge does not manage
# auto-compaction at all - and the moment it stopped being SET it had to
# start being STRIPPED. Those are the same question from opposite ends,
# and answering only one of them changes nothing.
#
# Measured, not reasoned. The same suite on the same code, twice: with the
# variable in the parent environment the stub client recorded 70 in a
# window the bridge had just opened; with it removed, the same launch
# recorded nothing. So a daemon restarted from inside a window that has
# one - which is exactly how `relayout --now` gets typed - would have gone
# on handing it out invisibly, and with reg_pid no longer recording a
# threshold there would have been nothing anywhere to say that it had.
#
# CLAUDE_CODE_AUTO_COMPACT_WINDOW joined it on 2026-10-10 for the same
# reason from the other end: launch() SETS it now, per role (8.61), and a
# value nobody chose that every window inherits - a daemon restarted from
# inside an executor would carry 222000 into every planner it opened - is
# the one thing worse than none. One source, and it is launch().
INHERITED_CLIENT_MARKS = (
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)


def clean_env(env=None):
    """A copy of the environment with another session's marks taken out.

    Used for every window the bridge opens and for the daemon itself, so
    that a bridge restarted from inside somebody's session does not pass
    that session's identity down to everything it later starts.
    """
    out = dict(os.environ if env is None else env)
    for name in INHERITED_CLIENT_MARKS:
        out.pop(name, None)
    return out


def _bridge_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_command(project, role, resume_id=None, permission_mode=None,
                  model=None, disallow=None, prompt=None, effort=None,
                  settings=None, name=None):
    """The command line for one window.

    `prompt` is the client's positional [prompt] - "claude --help" calls it
    "Your prompt" - and it is what makes a window's FIRST act something the
    bridge chose. V1 uses it to run /init in a replacement executor. It
    goes LAST, after every flag, because it is positional; anything that
    took it for a flag value would eat it.
    """
    cfg = ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["executor"])
    cmd = ["claude"]
    if resume_id:
        cmd += ["--resume", resume_id]
    cmd += ["--permission-mode", permission_mode or cfg["permission_mode"]]
    if name:
        # the client's two names for one window (2.1.296 --help): --name
        # sets "a display name for this session", and the value of
        # --remote-control names its Remote Control session ("optionally
        # named"); a name another live session on this machine holds,
        # the client changes (DECISIONS.md 8.75)
        cmd += ["--name", name, "--remote-control", name]
    else:
        cmd += ["--remote-control"]
    # both sessions host the bridge channel (role decides its behaviour),
    # and custom channels need the development flag during the preview
    cmd += ["--dangerously-load-development-channels", "server:bridge"]
    if model:
        cmd += ["--model", model]
    # the effort level of this window's session, when the bridge names one
    # (daemon.effort_for - the planner's is max, 8.60); a flag, so it is
    # this window's and no other session's, and --resume takes it too
    if effort:
        cmd += ["--effort", effort]
    # this window's own client settings, when the bridge names any - today
    # only the planner's {"idleCompaction": false} (8.61). The client reads
    # the flag source above the project's files, and only the keys given
    if settings:
        cmd += ["--settings", json.dumps(settings, separators=(",", ":"))]
    if disallow:
        # a deny beats every permission mode, so this holds whatever mode
        # the window was started in
        cmd += ["--disallowedTools", ",".join(disallow)]
    if prompt:
        cmd.append(prompt)
    return cmd


def ensure_marks(project, role=""):
    """Never launch a pair into a project that cannot answer.

    This sits inside launch() rather than at any of its callers because
    there are seven of them - the panel's start, a handover, an automatic
    restart, the archive seat, the bridge's own window - and a gate that
    only covers the one that was in mind when it was written is a gate with
    the door left open beside it. One place, every path.

    It REPAIRS rather than refusing, and the two are not obviously the same
    call. `relayout.retired_tree_users` deliberately reports and does not
    repair, because a setting pointing at an old tree is somebody's
    decision. This is the opposite case: the bridge's own marks are not
    anybody's decision, install() merges and never overwrites (a project's
    own hooks and its other MCP servers survive - checked on the project
    this was written for, whose own server came through untouched), and
    refusing here would leave the human with a pair that will not start and
    no way to start it. So it repairs, and it says so loudly enough that
    nobody has to guess it happened: the warn names every mark and its
    file, before and after.

    Refusing to launch was considered and rejected for one more reason: the
    failure this exists to prevent was already silent for ten minutes and
    then produced a message that named nothing. Trading a silent blind pair
    for a silent refusal is not a fix.

    Never raises. A launch that cannot be checked still happens - losing the
    loop to a permissions error while inspecting a settings file would be a
    worse bug than the one being fixed.
    """
    try:
        from . import install as installer
    except Exception:
        return []
    try:
        missing = installer.marks_missing(project)
        if not missing:
            return []
        store.journal(
            "session",
            "%s is missing bridge marks, so this %s window would have "
            "started blind - repairing before launch: %s"
            % (os.path.basename(project.rstrip("\\/")) or project,
               role or "session", "; ".join(missing)),
            os.path.basename(project.rstrip("\\/")), role, "warn",
            project_dir=project)
        installer.install(project, role or None)
        left = installer.marks_missing(project)
        if left:
            # Repaired what it could and says what it could not. The launch
            # goes ahead: a half-installed pair that reports some events is
            # worth more than no pair, and the line below is what a person
            # needs in order to finish the job by hand.
            store.journal(
                "session",
                "%s: install ran but these marks are still absent, so the "
                "pair may still be partly blind: %s"
                % (os.path.basename(project.rstrip("\\/")) or project,
                   "; ".join(left)),
                os.path.basename(project.rstrip("\\/")), role, "warn",
                project_dir=project)
        else:
            store.journal(
                "session",
                "%s: bridge marks restored, launching a %s that can answer"
                % (os.path.basename(project.rstrip("\\/")) or project,
                   role or "session"),
                os.path.basename(project.rstrip("\\/")), role, "log",
                project_dir=project)
        return missing
    except Exception as exc:
        try:
            store.journal("session",
                          "could not check the bridge marks of %s: %s"
                          % (project, exc),
                          os.path.basename(str(project).rstrip("\\/")),
                          role, "warn", project_dir=project)
        except Exception:
            pass
        return []


def project_compact_window(project):
    """The `autoCompactWindow` this project's own settings ask for, or None.

    Read from the project's `.claude/settings.json` at every launch rather
    than remembered: it is the owner's file, edited by hand, and a value
    cached anywhere else would be a second authority over it.
    """
    p = os.path.join(project, ".claude", "settings.json")
    try:
        with io.open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    n = data.get("autoCompactWindow")
    return int(n) if isinstance(n, int) and n > 0 else None


def compact_pct_for(project, pct, window=None):
    """The percentage this window may honestly be given, or None.

    BOTH HALVES OR NEITHER, and that is the whole of what 8.2 measured. The
    percentage and `autoCompactWindow` MULTIPLY - 700000 x 70 % gave a point
    at 476-482k over 14 samples - and WITHOUT the key the variable is inert:
    the pair next door received the same 70 and compacted at the ceiling
    regardless. So sending a percentage to a project that has no key would
    not merely do nothing; it would write a threshold into `reg_pid` that
    the window is not running under, which is the class of claim this
    project keeps having to unpick.

    The other half may now come from the BRIDGE: `window` is the auto-compact
    window launch() hands this window itself (CLAUDE_CODE_AUTO_COMPACT_WINDOW,
    8.61), and the client lowers that by the percentage just as it lowers the
    file's - 222000 x 90 compacted at 180-193k on 10.10.

    Returns None when either half is missing, so the caller sends nothing
    and records nothing.
    """
    if not pct:
        return None
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        return None
    if not 0 < pct <= 100:
        return None
    return pct if (window or project_compact_window(project)) else None


def window_value(window):
    """`window` as the positive int the client accepts, or None.

    The client takes 100k-1M ("Expected 'auto' or 100k-1M tokens"); a value
    outside that, or one that is not a number, is not sent - a window the
    client would refuse is worse than the project's own file deciding.
    """
    try:
        n = int(window)
    except (TypeError, ValueError):
        return None
    return n if 100000 <= n <= 1000000 else None


def real_client_refused(cmd):
    """Why this process must not spawn that command, or "" if it may.

    A SUITE MAY NOT OPEN A REAL CLIENT WINDOW. Every suite stubs
    build_command, so the rule was kept by each of them remembering it -
    and on 2026-09-04 seventeen real windows were left open by runs where
    that did not hold. A precondition kept by memory is not a
    precondition, so this is asked HERE, at the one place a process is
    actually started, and it refuses rather than warns.

    The test is structural: a suite puts BRIDGE_DATA in a fresh temp
    directory, and the bridge never does. It looks at the executable
    only - a stubbed command runs an interpreter, so it is never touched -
    and BRIDGE_REAL_CLIENT=1 says the real thing is meant.
    """
    if os.environ.get("BRIDGE_REAL_CLIENT") == "1":
        return ""
    exe = os.path.basename((list(cmd) or [""])[0] or "")
    if os.path.splitext(exe)[0].lower() != "claude":
        return ""
    data = os.environ.get("BRIDGE_DATA") or ""
    if not data:
        return ""
    try:
        tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        here = os.path.normcase(os.path.abspath(data))
    except Exception:
        return ""
    if here != tmp and not here.startswith(tmp + os.sep):
        return ""
    return ("BRIDGE_DATA is %s, under the temp folder, so this is a test "
            "harness - and the command would start a REAL client (%s) with "
            "the real flags, in a window nothing here will close. Stub "
            "sessions.build_command, or set BRIDGE_REAL_CLIENT=1 to mean "
            "it." % (data, exe))


# ---------------------------------------------------------------------------
# THE CLIENT'S LOGIN WHERE NOBODY LOGS IT IN BY HAND. A Mac, 2026-10-12: the
# client there has no login of its own, so the owner ran `claude
# setup-token` and saved what it printed into this file with umask 077 (the
# owner's line (a)). setup-token prints `export CLAUDE_CODE_OAUTH_TOKEN=<token>`,
# and a person may paste that whole line, so the prefix is taken off. Read
# at every launch and put into that one window's environment AFTER
# clean_env(), so nothing the daemon inherited decides it. No file means a
# machine whose client logs in by itself - this PC - and changes nothing.
# A file with no usable token in it is one journal line that never carries
# the file's content. -> DECISIONS.md 8.86

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".bridge-claude-token")
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~+/=-]")
TOKEN_FILE_ON = None    # a TEST SEAM, never a setting: None asks the config
_LOGIN_TOLD = []


def token_file_on():
    """Is the token file read at all? Only with `use_token_file` on in the
    config. The file holds ONE account's login, and every window and every
    `claude -p` on the machine would sit on that account: on a Mac it was
    the owner's, and a colleague's sessions there would have run on it
    (2026-10-12, 8.91). Off - the default - a window logs in however the
    client does on its own: its keychain, after `claude` was signed in once
    at the machine's screen."""
    if TOKEN_FILE_ON is not None:
        return bool(TOKEN_FILE_ON)
    try:
        return bool(store.load_config().get("use_token_file"))
    except Exception:
        return False


def say_own_login(os_name=None):
    """One journal line per daemon, off Windows, where a token file is a
    question at all: the windows log in with the client's own sign-in, and
    a token file lying there is NOT read."""
    if (os_name or os.name) == "nt" or _LOGIN_TOLD:
        return
    _LOGIN_TOLD.append(True)
    try:
        store.journal("session", "Windows here log in with the client's own "
                      "sign-in (`claude`, signed in once at this machine's "
                      "screen). %s" % (
                          "The token file %s is there and is NOT read: "
                          "use_token_file is off in the config" % TOKEN_FILE
                          if os.path.exists(TOKEN_FILE) else
                          "A token file is read only with use_token_file on "
                          "in the config"), level="log")
    except Exception:
        pass


def window_token(project, role):
    """The token a window is given, or "". The file is read only when
    token_file_on(); a file with no usable token in it is one warn line per
    window that never quotes it (8.86)."""
    if not token_file_on():
        say_own_login()
        return ""
    token, trouble = read_token()
    if trouble:
        try:
            base = os.path.basename(project.rstrip("\\/")) or project
            store.journal("session", "%s: the token file %s %s - this %s "
                          "window gets no token from it and logs in however "
                          "the client does on its own" % (
                              base, TOKEN_FILE, trouble, role),
                          base, role, "warn", project_dir=project)
        except Exception:
            pass
    return token


def read_token(path=None):
    """(token, trouble) from the token file. Never raises.

    ("", "") when there is no file at all. ("", trouble) when there is a
    file and no token in it, `trouble` saying why in words that never quote
    the file - it goes into the journal, and half a token is still a secret.
    """
    path = path or TOKEN_FILE
    try:
        with open(path, "rb") as fh:
            raw = fh.read(65536)
    except FileNotFoundError:
        return "", ""
    except OSError as exc:
        return "", "cannot be read (%s)" % (exc.strerror or exc.__class__
                                            .__name__)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", "is not UTF-8 text"
    t = text.strip()
    if t.startswith("export ") or t.startswith("export\t"):
        t = t[len("export"):].strip()
    if t.startswith(TOKEN_VAR + "="):
        t = t[len(TOKEN_VAR) + 1:].strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        t = t[1:-1].strip()
    if not t:
        return "", "is empty"
    lines = len([ln for ln in t.splitlines() if ln.strip()])
    if lines > 1:
        return "", "holds %d lines, not one token" % lines
    for i, ch in enumerate(t):
        if not _TOKEN_CHARS.match(ch):
            what = ("a space" if ch.isspace() else
                    "%r" % ch if ord(ch) < 128 and ch.isprintable() else
                    "a non-ASCII character" if ord(ch) >= 128 else
                    "a control character")
            return "", ("is broken: %s at character %d of %d"
                        % (what, i + 1, len(t)))
    return t, ""


# where the client's native installer puts it, and where a daemon started
# by launchd or over ssh - with a PATH that never read the login profile -
# does not look on its own (8.89)
LOCAL_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin")


def client_exe(exe, env=None):
    """The client named without a path, found where a window finds it: on
    the PATH it will run with, then in LOCAL_BIN. "" when it is in neither;
    a name that has a path already is its own answer."""
    if os.path.dirname(exe):
        return exe
    found = shutil.which(exe, path=(os.environ if env is None
                                    else env).get("PATH"))
    home = os.path.join(LOCAL_BIN, exe)
    if not found and os.access(home, os.X_OK):
        found = home
    return found or ""


def headless_client(cmd, env=None):
    """(cmd, env, trouble) for a `claude -p` the daemon runs itself - the
    archive's search and the model probe - found and logged in the way a
    window is. A Mac, 2026-10-12: the windows got the client's full path and
    the token file in M1, these two did not, so on a Mac the archive asked
    a client that was not on the daemon's PATH, or one with no login.

    The full path is looked up off Windows only: there the client is on
    PATH, and a lookup could swap the name for an npm `claude.cmd` that
    Popen does not run the way it runs an exe. The token file is read on
    every machine, as launch() reads it. `trouble` is read_token's, for the
    caller's journal; it never quotes the file. Only with the token file
    on, as for a window (8.91)."""
    cmd = list(cmd)
    env = dict(os.environ if env is None else env)
    if cmd and os.name != "nt":
        cmd[0] = client_exe(cmd[0], env) or cmd[0]
    token, trouble = read_token() if token_file_on() else ("", "")
    if token:
        env[TOKEN_VAR] = token
    return cmd, env, trouble


# ---------------------------------------------------------------------------
# THE WINDOW ON POSIX IS A TMUX PANE. The Popen branch below has no
# terminal, and the client is interactive - on a Mac it cannot run there at
# all. So a window is a detached tmux session: its text is `capture-pane`,
# its Enter is `send-keys`, and what the owner sees of it is a Terminal
# window ATTACHED to it (show_window below), opened by the daemon only -
# never by a suite, a check or a probe (viewer_refused). -> DECISIONS.md 8.93
#
# ONE TMUX SERVER PER WINDOW, on its own label. A server copies the
# environment of the command that STARTS it and hands that to every pane,
# so a server shared by two windows gives the second the first one's
# BRIDGE_ROLE and token. A fresh label is a fresh server, and the window's
# environment is exactly launch()'s. The server goes when its pane does.
#
# THE PANE'S PROCESS IS THE CLIENT. tmux execs a command given as several
# words without a shell, and the wrapper below execs the client in its own
# place, so the pid tmux reports is the client's - the number Windows gets
# from Popen - and BRIDGE_WINDOW_PID, set to $$ before the exec, carries it
# to every hook under the client (hook.client_pid). -> DECISIONS.md 8.86

TMUX = None             # a TEST SEAM, never a setting: the command that is
                        # tmux, as a list - set, it is used on Windows too
TMUX_PLACES = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux")
TMUX_PREFIX = "bridge-"
def keep_awake(pid=None, popen=None, platform=None):
    """A Mac does not idle-sleep while the daemon lives. Returns the
    caffeinate process, or None where there is nothing to do.

    `caffeinate -i -w <pid>` holds the assertion and lets it go by itself
    when that pid exits, so a daemon killed outright leaves nothing behind
    and there is nothing to stop. A CLOSED LID still sleeps a Mac laptop:
    no assertion a user process can make stops that, and the pairs stand
    still until it is opened - the Mac's lid, 2026-10-12. -> DECISIONS.md 8.87
    """
    if (platform or sys.platform) != "darwin":
        return None
    try:
        return (popen or subprocess.Popen)(
            ["/usr/bin/caffeinate", "-i", "-w", str(pid or os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError as exc:
        store.journal("bridge", "could not keep this Mac awake while the "
                      "bridge runs - it may idle-sleep and stop the pairs: "
                      "%s" % exc, level="warn")
        return None


TMUX_SIZE = ("160", "48")
TMUX_WRAP = 'BRIDGE_WINDOW_PID=$$; export BRIDGE_WINDOW_PID; exec "$@"'
SEATS = {}              # client pid -> (label, pane id)


def tmux_command():
    """The tmux windows run in, as a command list, or None.

    None on Windows unless TMUX names one: the console window is the
    Windows path and stays it. Elsewhere tmux on the PATH, else Homebrew's
    two places - a daemon started outside a login shell has neither
    /opt/homebrew/bin nor /usr/local/bin on its PATH (a Mac, 2026-10-12).
    """
    if TMUX:
        return list(TMUX)
    if os.name == "nt":
        return None
    found = shutil.which("tmux")
    for place in () if found else TMUX_PLACES:
        if os.access(place, os.X_OK):
            found = place
            break
    return [found] if found else None


def tmux_dir():
    """Where tmux keeps its servers' sockets, by tmux's own rule:
    $TMUX_TMPDIR or /tmp, then tmux-<uid>."""
    uid = getattr(os, "getuid", lambda: 0)()
    return os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp",
                        "tmux-%d" % uid)


def _tmux(label, *args, **kw):
    """Run one tmux command against the server `label`. The completed
    process, or None when there is no tmux or it could not be run."""
    tm = tmux_command()
    if not tm:
        return None
    try:
        return subprocess.run(tm + ["-L", label] + list(args),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=kw.get("timeout", 15),
                              env=kw.get("env"), cwd=kw.get("cwd"))
    except Exception:
        return None


def socket_dead(path):
    """Is this a tmux socket nobody listens on any more? Asked with a
    connect, not a tmux run. tmux 3.8 leaves its socket behind when its
    server ends, and tmux_seat ran tmux once per socket: 326 of them on
    a Mac on 2026-10-13, 7.25 s for one pid that was no window, and every
    stop asks that. A file that is not a socket (case 130's fake tmux
    keeps a file) is not judged here. -> DECISIONS.md 8.93"""
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return False
    except OSError:
        return True
    if not hasattr(socket, "AF_UNIX"):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2)
        s.connect(path)
        return False
    except OSError:
        return True
    finally:
        s.close()


def forget_socket(label):
    """Remove the socket of a server the bridge itself has ended, by its
    exact path, once nothing listens on it - tmux leaves it there."""
    path = os.path.join(tmux_dir(), label)
    if label.startswith(TMUX_PREFIX) and socket_dead(path):
        try:
            os.remove(path)
        except OSError:
            pass


def tmux_seat(pid):
    """(label, pane id) of the bridge's tmux pane whose process is `pid`, or
    None. Asked of tmux, not remembered: SEATS is only where to ask first,
    and a daemon restarted under live windows has an empty one. A socket
    no server listens on is not asked at all (socket_dead)."""
    if not pid or not tmux_command():
        return None
    pid = int(pid)
    first = SEATS.get(pid)
    labels = [first[0]] if first else []
    try:
        where = tmux_dir()
        labels += sorted(n for n in os.listdir(where)
                         if n.startswith(TMUX_PREFIX) and n not in labels
                         and not socket_dead(os.path.join(where, n)))
    except OSError:
        pass
    for label in labels:
        r = _tmux(label, "list-panes", "-a", "-F", "#{pane_pid} #{pane_id}")
        if not r or r.returncode:
            continue
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == str(pid):
                SEATS[pid] = (label, parts[1])
                return SEATS[pid]
    SEATS.pop(pid, None)
    return None


class TmuxSeat(object):
    """What PROCS holds for a window in tmux: the client's pid and the four
    things a caller asks of a Popen. The tmux client that opened it exits at
    once, so it is not the thing to hold."""

    def __init__(self, pid, label, pane):
        self.pid, self.label, self.pane = int(pid), label, pane
        self.returncode = None

    def poll(self):
        if self.returncode is None and not pid_alive(self.pid):
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        end = None if timeout is None else time.time() + timeout
        while self.poll() is None:
            if end is not None and time.time() >= end:
                raise subprocess.TimeoutExpired("tmux pane %d" % self.pid,
                                                timeout)
            time.sleep(0.1)
        return self.returncode

    def kill(self):
        try:
            os.kill(self.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except Exception:
            pass
        _tmux(self.label, "kill-server")


def _launch_tmux(tm, project, role, cmd, env, name=None):
    """Open the window as a detached tmux session; returns the client's pid.

    A client named without a path is found HERE, on the window's own PATH
    and then where the native installer puts it, because inside tmux a
    missing one is a pane that dies the moment it opens and a window that
    simply never came up. Raises with the reason instead.
    """
    exe = cmd[0]
    if not os.path.dirname(exe):
        found = client_exe(exe, env)
        if not found:
            raise RuntimeError(
                "%s is neither on this daemon's PATH nor in ~/.local/bin, so "
                "the %s window would die the moment it opened" % (exe, role))
        cmd = [found] + list(cmd[1:])
    env = dict(env)
    # a daemon started inside somebody's tmux would hand its panes that one
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    label = "%s%s-%s" % (TMUX_PREFIX, role or "window", uuid.uuid4().hex[:12])
    session = re.sub(r"[.:]", "_", name or role or "window")
    r = _tmux(label, "new-session", "-d", "-s", session, "-c", project,
              "-x", TMUX_SIZE[0], "-y", TMUX_SIZE[1],
              "-P", "-F", "#{pane_pid} #{pane_id}",
              "/bin/sh", "-c", TMUX_WRAP, "bridge-window", *cmd,
              env=env, cwd=project, timeout=30)
    parts = ((r.stdout if r else "") or "").split()
    if not r or r.returncode or len(parts) != 2 or not parts[0].isdigit():
        raise RuntimeError(
            "tmux did not open the %s window: %s" % (
                role, "it could not be run" if not r else "exit %s, %s" % (
                    r.returncode, (r.stderr or r.stdout or "").strip()[:300])))
    pid = int(parts[0])
    SEATS[pid] = (label, parts[1])
    PROCS[(store.norm(project), role)] = TmuxSeat(pid, label, parts[1])
    view_options(label)
    return pid


# ---------------------------------------------------------------------------
# A WINDOW ON A MAC IS SHOWN IN TERMINAL - the owner's word of 2026-10-12:
# "make the Claude Code windows the bridge starts on a Mac visible, because
# when they are invisible it is inconvenient". The tmux session stays the
# window; a Terminal window attached to it is a VIEWER and nothing more:
#
#  - it is opened by `open -g -a Terminal <label>.command`: LaunchServices
#    opens it, Terminal does not come to the front and no focus is taken,
#    and macOS asks no permission for any of it;
#  - closing it ends its tmux client and nothing else - the window runs on,
#    and the panel's "show" opens another;
#  - the window keeps its own 160x48 whatever a viewer measures
#    (window-size manual), so console_screen and console_answer read and
#    press the same screen with or without one, of any size, on it;
#  - when the window ends, its viewer's client ends, and Terminal keeps a
#    dead "[Process completed]" window: "When the shell exits: Don't close
#    the window" is its default, measured on a Mac on 2026-10-13. Only
#    Terminal itself can close one, over Apple Events, so sweep_viewers
#    does, and only after macOS has answered WITHOUT a dialog that it may.
#
# NEVER FROM A SUITE, A CHECK OR A PROBE: viewer_refused is asked before
# anything is opened or closed. -> DECISIONS.md 8.93

TERMINAL_OPEN = None    # a TEST SEAM, never a setting: called with the argv
                        # that would open a viewer, instead of running it
TERMINAL_CLOSE = None   # a TEST SEAM: called with the ttys whose dead
                        # windows would be closed; answers the ones closed
TERMINAL_HUSKS = None   # a TEST SEAM: called with the ids to close (empty:
                        # only list); answers the ids of tabless windows
SCREEN_LOCKED = None    # a TEST SEAM: answers True while the screen is locked
VIEWER_TRIES = 8        # sweeps a gone window's viewer gets to fall idle
VIEWER_WAIT_SEC = 20    # how long an opened viewer is waited for
_VIEW_TOLD = []


def viewer_refused(platform=None, data=None, writer=None):
    """Why this process may not put a window on the screen, or "".

    Asked of the PROCESS, not remembered by its callers - the lesson of
    real_client_refused: off a Mac there is a console of its own; on data
    under the temp folder it is a suite's, a check's or a probe's run; and
    a process that has not claimed the live folder is not the daemon.
    """
    if (platform or sys.platform) != "darwin":
        return "not a Mac - a window there is a console of its own"
    if store.data_is_throwaway(data):
        return ("BRIDGE_DATA is under the temp folder - a suite's, a "
                "check's or a probe's run puts nothing on the screen")
    if not (store._WRITER["daemon"] if writer is None else writer):
        return "this process is not the daemon serving the bridge"
    return ""


def view_dir():
    return os.path.join(store.DATA, "viewers")


def load_viewers():
    """{tmux label: {"ttys": [...], "at": t, "tries": n}} - the viewers the
    bridge opened or found, kept on disk so a restart can close them."""
    try:
        with io.open(os.path.join(view_dir(), "viewers.json"),
                     encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def save_viewers(reg):
    try:
        os.makedirs(view_dir(), exist_ok=True)
        tmp = os.path.join(view_dir(), "viewers.json.tmp")
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(view_dir(), "viewers.json"))
    except OSError:
        pass


def viewer_ttys(label):
    """The ttys of the tmux clients on server `label` - its viewers - or
    None when that server does not answer: the window is gone."""
    r = _tmux(label, "list-clients", "-F", "#{client_tty}")
    if not r or r.returncode:
        return None
    return sorted(t for t in (r.stdout or "").split() if t.startswith("/dev/"))


def view_options(label):
    """The window's size is its own, and a viewer's title is its name."""
    for opt in (("window-size", "manual"), ("set-titles", "on"),
                ("set-titles-string", "#S")):
        _tmux(label, "set-option", "-g", *opt)


def note_viewer(label, ttys=None, opened=False):
    reg = load_viewers()
    rec = reg.setdefault(label, {})
    if ttys is not None:
        rec["ttys"] = sorted(set(ttys))
    rec.setdefault("ttys", [])
    if opened:
        rec["opened_at"] = time.time()
    rec["at"] = time.time()
    rec["tries"] = 0
    save_viewers(reg)


def open_viewer(label):
    """Write the viewer's .command and have Terminal open it, in the
    background. (ok, what was done or why not)."""
    tm = tmux_command()
    if not tm:
        return False, "there is no tmux to attach a viewer with"
    os.makedirs(view_dir(), exist_ok=True)
    path = os.path.join(view_dir(), label + ".command")
    q = " ".join(shlex.quote(w) for w in tm + ["-L", label])
    # Terminal starts the file with ITS environment, not the daemon's: a
    # socket folder the daemon was given must be written into it
    where = os.environ.get("TMUX_TMPDIR")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\n"
                 "# The bridge's window on tmux server %s, on the screen.\n"
                 "# Closing this Terminal window leaves that window running;\n"
                 "# the panel's \"show\" opens it again. Written by the bridge,\n"
                 "# removed by it when the window is gone.\n"
                 "%s"
                 "printf '\\033[8;%s;%st'\n"
                 "exec %s attach-session\n"
                 % (label, "TMUX_TMPDIR=%s; export TMUX_TMPDIR\n"
                    % shlex.quote(where) if where else "",
                    TMUX_SIZE[1], TMUX_SIZE[0], q))
    os.chmod(path, 0o700)
    argv = ["/usr/bin/open", "-g", "-a", "Terminal", path]
    try:
        if TERMINAL_OPEN:
            TERMINAL_OPEN(argv)
        else:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=30)
            if r.returncode:
                return False, "open answered %s: %s" % (
                    r.returncode, (r.stderr or r.stdout or "").strip()[:200])
    except Exception as exc:
        return False, "could not run open: %s" % exc
    note_viewer(label, opened=True)
    threading.Thread(target=_learn_viewer, args=(label,),
                     name="viewer:" + label, daemon=True).start()
    return True, "opened in Terminal"


def _learn_viewer(label):
    """Wait for the viewer opened on `label` to attach, and keep its tty:
    the window it is in can only be closed by that."""
    end = time.time() + VIEWER_WAIT_SEC
    while time.time() < end:
        ttys = viewer_ttys(label)
        if ttys is None:
            return
        if ttys:
            note_viewer(label, ttys)
            return
        time.sleep(0.5)


def show_window(pid):
    """Put the bridge's tmux window `pid` on the screen in Terminal.
    (ok, said). Never raises.

    A window that already has a viewer gets no second one, and nor does
    one whose viewer was opened a moment ago and is still attaching - a
    launch at the start and the start's own pass over the windows would
    otherwise open two."""
    try:
        why = viewer_refused()
        if why:
            return False, why
        seat = tmux_seat(pid)
        if not seat:
            return False, "pid %s is no tmux window of the bridge's" % pid
        view_options(seat[0])
        ttys = viewer_ttys(seat[0])
        if ttys:
            note_viewer(seat[0], ttys)
            return True, "already on the screen (%s)" % ", ".join(ttys)
        rec = load_viewers().get(seat[0]) or {}
        if time.time() - float(rec.get("opened_at") or 0) < VIEWER_WAIT_SEC:
            return True, "being opened"
        return open_viewer(seat[0])
    except Exception as exc:
        return False, "%s" % exc


AE_PROBE = r'''import ctypes, sys
AE = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreServices.framework/CoreServices")
class D(ctypes.Structure):
    _fields_ = [("t", ctypes.c_uint32), ("h", ctypes.c_void_p)]
f = lambda s: int.from_bytes(s.encode(), "big")
AE.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long,
                            ctypes.POINTER(D)]
AE.AEDeterminePermissionToAutomateTarget.argtypes = [
    ctypes.POINTER(D), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_ubyte]
AE.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
b = sys.argv[1].encode()
d = D()
AE.AECreateDesc(f("bund"), b, len(b), ctypes.byref(d))
print(AE.AEDeterminePermissionToAutomateTarget(ctypes.byref(d), f("****"),
                                               f("****"), 0))
'''

CLOSE_SCRIPT = '''on run argv
  set hits to {}
  set done to {}
  tell application "Terminal"
    repeat with w in windows
      try
        if (count of tabs of w) is 1 then
          set t to tab 1 of w
          if (tty of t) is in argv and (count of (processes of t)) is 0 then
            set end of hits to {id of w, tty of t}
          end if
        end if
      end try
    end repeat
    repeat with h in hits
      try
        close (window id (item 1 of h))
        set end of done to item 2 of h
      end try
    end repeat
  end tell
  set AppleScript's text item delimiters to " "
  return done as text
end run'''


def may_script_terminal():
    """May this bridge send Terminal Apple Events? Asked of macOS with
    AEDeterminePermissionToAutomateTarget and askUserIfNeeded FALSE, so it
    never puts a dialog up: 0 is yes, -1744 is "the person would be asked",
    -1743 is "refused", -600 is "Terminal is not running". (ok, code)."""
    try:
        r = subprocess.run([sys.executable, "-c", AE_PROBE,
                            "com.apple.Terminal"], capture_output=True,
                           text=True, timeout=15)
        code = (r.stdout or "").strip().splitlines()[-1:] or ["?"]
        return code[0] == "0", code[0]
    except Exception as exc:
        return False, "%s" % exc


def close_dead_terminals(ttys):
    """Close the Terminal windows on these ttys whose tab runs nothing any
    more. The ones closed, as a list. Only a window with ONE tab, on one of
    the viewers' own ttys, and with no process left in it qualifies: a tty
    is reused, and a window a person opened on it has a shell."""
    if not ttys:
        return []
    if TERMINAL_CLOSE:
        return list(TERMINAL_CLOSE(list(ttys)) or [])
    ok, code = may_script_terminal()
    if not ok:
        if code not in _VIEW_TOLD:
            _VIEW_TOLD.append(code)
            store.journal("bridge", "A closed window's Terminal viewer stays "
                          "on the screen as \"[Process completed]\": macOS "
                          "has not let the bridge script Terminal (answer "
                          "%s), and the bridge does not ask. Close it by "
                          "hand, or set Terminal > Settings > Profiles > "
                          "Shell > When the shell exits: Close if the shell "
                          "exited cleanly." % code, level="warn")
        return []
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", CLOSE_SCRIPT]
                           + list(ttys), capture_output=True, text=True,
                           timeout=30)
        return (r.stdout or "").split()
    except Exception:
        return []


HUSKS_SCRIPT = '''on run argv
  set ids to {}
  tell application "Terminal"
    repeat with w in windows
      try
        if (count of tabs of w) is 0 then set end of ids to ((id of w) as text)
      end try
    end repeat
    if (count of argv) > 0 then
      set asked to {}
      repeat with i in ids
        if (contents of i) is in argv then
          try
            close (window id ((contents of i) as integer))
          end try
          set end of asked to (contents of i)
        end if
      end repeat
      set ids to asked
    end if
  end tell
  set AppleScript's text item delimiters to " "
  return ids as text
end run'''


def terminal_husks(close=()):
    """The ids of Terminal's windows with NO tab - what a close leaves while
    the screen is locked: the tab goes, the window stays empty, and a second
    close does nothing until the unlock (the Mac, 2026-10-13). With <close>,
    those of the ids given that are still tabless are closed, and the ids
    asked of are answered; a window with a tab is never closed here,
    whatever its id. [] wherever Terminal may not be scripted. -> 8.116"""
    if TERMINAL_HUSKS:
        return list(TERMINAL_HUSKS(list(close)) or [])
    if not may_script_terminal()[0]:
        return []
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", HUSKS_SCRIPT]
                           + [str(i) for i in close], capture_output=True,
                           text=True, timeout=30)
        return (r.stdout or "").split()
    except Exception:
        return []


def screen_locked():
    """Is the Mac's screen locked? IOConsoleLocked at the top of ioreg's
    Root: True or False, None where it cannot be read (not a Mac)."""
    if SCREEN_LOCKED:
        return SCREEN_LOCKED()
    if sys.platform != "darwin":
        return None
    try:
        import plistlib
        r = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"],
                           capture_output=True, timeout=30)
        top = plistlib.loads(r.stdout)
        if isinstance(top, list):
            top = top[0] if top else {}
        v = top.get("IOConsoleLocked")
        return None if v is None else bool(v)
    except Exception:
        return None


def console_tty():
    """The terminal this process was started in, or "". A Mac's bridge
    started by bridge.command knows its Terminal window by it, and the
    next start closes that window once nothing runs in it (8.94)."""
    if os.name == "nt":
        return ""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                return os.ttyname(fd)
        except OSError:
            pass
    return ""


def sweep_viewers():
    """Learn the viewers of the windows still there; close the Terminal
    windows of those whose window has gone. The number closed. Never
    raises; does nothing at all where viewer_refused says no."""
    if viewer_refused():
        return 0
    return _sweep_viewers()


def _sweep_viewers():
    """sweep_viewers past its gate - the part a suite can run."""
    try:
        reg = load_viewers()
        dead = {}
        for label, rec in list(reg.items()):
            ttys = viewer_ttys(label)
            if ttys is not None:
                rec["ttys"] = ttys
                rec["tries"] = 0
            else:
                dead[label] = rec
        want = sorted({t for rec in dead.values()
                       for t in rec.get("ttys") or []})
        closed = set(close_dead_terminals(want)) if want else set()
        for label, rec in dead.items():
            left = [t for t in rec.get("ttys") or [] if t not in closed]
            rec["tries"] = int(rec.get("tries") or 0) + 1
            if left and rec["tries"] < VIEWER_TRIES:
                rec["ttys"] = left
                continue
            reg.pop(label, None)
            try:
                os.remove(os.path.join(view_dir(), label + ".command"))
            except OSError:
                pass
        save_viewers(reg)
        return len(closed)
    except Exception:
        return 0


def sweep_soon(delay=2.0):
    """A sweep a moment after a window was ended: its viewer's shell needs
    that long to go, and a dead window should not wait for the next
    round."""
    if viewer_refused():
        return
    t = threading.Timer(delay, sweep_viewers)
    t.daemon = True
    t.start()


def launch(project, role, resume_id=None, permission_mode=None, model=None,
           disallow=None, compact_pct=None, prompt=None, effort=None,
           compact_window=None, idle_compaction=None):
    """Start a session in its own minimised console. Returns pid.

    `compact_window` is this window's own auto-compact window (daemon
    .window_for - 222000 for an executor, 1000000 for a planner, 8.61). It
    goes in as CLAUDE_CODE_AUTO_COMPACT_WINDOW, which the client reads
    before the project's settings files, so the shared file is never
    touched to give one half a different window from the other.
    `idle_compaction` False switches the client's idle compaction off in
    this window (daemon.idle_compaction_for - the planner's, 8.61); None
    leaves it as the client has it.
    """
    if not os.path.isdir(project):
        raise ValueError("no such folder: %s" % project)

    # one bridge per folder, across machines - and BEFORE ensure_marks:
    # the hooks it repairs are the folder's, shared by both machines (8.87)
    held, why = projlock.acquire(project)
    if not held:
        raise RuntimeError(why)

    ensure_marks(project, role)

    env = clean_env()
    env["BRIDGE_ROLE"] = role
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = _bridge_root() + os.pathsep + env.get("PYTHONPATH", "")
    if role == "executor":
        env.setdefault("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "200")
    # 2026-09-02, the owner's decision, a deliberate partial reversal of
    # 2026-09-01: the threshold comes back, but as a PROJECT setting and
    # only where the project's own settings carry the key it multiplies
    # with. `clean_env` still strips an inherited one, so there is exactly
    # one source for this variable and it is this line. -> DECISIONS.md 8.2
    window = window_value(compact_window)
    if window:
        # the same rule for the window: clean_env took any inherited one
        # out, so what the client reads is this line's and nobody else's
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    applied = compact_pct_for(project, compact_pct, window)
    if applied:
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(applied)
    # the FILE decides, over any token the daemon inherited: clean_env
    # leaves this one alone (a login, not another session's mark), so only
    # a value set here, after it, is sure to be the file's (8.86) - and
    # only when the config turns the file on (8.91)
    token = window_token(project, role)
    if token:
        env[TOKEN_VAR] = token

    name = WINDOW_NAME(project, role, resume_id) if WINDOW_NAME else None
    cmd = build_command(project, role, resume_id, permission_mode, model,
                        disallow, prompt, effort=effort, name=name,
                        settings={"idleCompaction": False}
                        if idle_compaction is False else None)
    why = real_client_refused(cmd)
    if why:
        raise RuntimeError(why)

    tm = tmux_command()
    if tm:
        pid = _launch_tmux(tm, project, role, cmd, env, name)
        # on the screen as the console is on Windows, where it may be (8.93)
        if not viewer_refused():
            shown, said = show_window(pid)
            store.journal("session", "The %s window (pid %d) %s" % (
                role, pid, "is shown in Terminal" if shown
                else "is not on the screen - " + said),
                os.path.basename(project.rstrip("/\\")), role,
                "log" if shown else "warn", project_dir=project)
        return pid
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_SHOWMINNOACTIVE
        proc = subprocess.Popen(cmd, cwd=project, env=env,
                                creationflags=CREATE_NEW_CONSOLE,
                                startupinfo=si)
    else:
        proc = subprocess.Popen(cmd, cwd=project, env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    PROCS[(store.norm(project), role)] = proc
    return proc.pid


# How long to wait for a force-killed window to actually go. The
# precedent is relayout.stop_daemon, which waits on the PROCESS and not on
# what the process was holding: it gives a polite close 45s and then at
# least 20s more after the forced kill. Nothing polite is tried here - a
# session window has no clean-shutdown handler to honour it - so only that
# second figure applies. A process that has taken a /F and is still there
# 20 seconds later is not slow, it is stuck, and saying so is the point.
STOP_WAIT_SEC = 20
# and after it, off Windows, what a SIGKILL is given before False is said
KILL_WAIT_SEC = 5


def signal_window(pid, sig, tree=True):
    """POSIX: send `sig` to a window, and with `tree` to its process group -
    but only when that group is the window's OWN. A window is born a group
    leader (start_new_session, or the setsid of a tmux pane that exec keeps),
    so its group id is its pid. A pid in anybody else's group gets the
    signal alone: getpgid of a process started without a session of its own
    is its STARTER's group, and killpg of that killed the starter.
    On 2026-10-12 test_wall_handover and test_wake_sim on a Mac ended -15
    that way, a stand-in window's stop taking the suite down (8.89).
    Returns "group" or "alone"; raises what os.kill raises."""
    if tree:
        try:
            own = os.getpgid(pid) == pid
        except Exception:
            own = False
        if own:
            os.killpg(pid, sig)
            return "group"
    os.kill(pid, sig)
    return "alone"


def stop(project, role, pid=None, wait=None, tree=True):
    """End a session process (and its children), and WAIT for it to go.

    `tree=False` ends the CLIENT ALONE. A window's background jobs are its
    children, and `taskkill /T` took them down with it: on 2026-09-23 a
    watched project's executor was replaced at the wall and ten background
    records were dropped a second later - night runs and waits killed as a
    side effect of changing hands. The owner's decision (8.31, variant a):
    the old client goes, its jobs finish. Measured before this was written:
    a child of a process stopped with `/F` and no `/T` lives on, and a
    toolhelp snapshot still names the dead parent as its parent.
    Off Windows `tree=False` is a SIGKILL to the client alone, the analog of
    `/F` without `/T`: given SIGTERM, the client ends its background shells
    itself on the way out (2026-10-14, client 2.1.299, +0.03 s), the very
    loss 8.31 prevents. The price, accepted: a config lock left for the
    client's next save to clear, a stray temporary file beside the config,
    and an ungraceful shutdown recorded for a session nobody resumes. A
    whole stop still asks first. -> DECISIONS.md 8.138

    It used to issue the kill and return True on the strength of having
    issued it, swallowing taskkill's exit code on the way. Both halves of
    that were wrong, and the same mistake relayout.stop_daemon exists to
    avoid: wait on the PROCESS, not on the request.

    What it cost, 2026-08-21. At 05:02 a pair was stopped and immediately
    relaunched with --resume onto its own session ids. stop() said True,
    the windows were still alive, and the replacements sat trying to resume
    conversations the old processes still held. Both halves stayed dark for
    four and a half hours; the SessionEnd events of the sessions "stopped"
    at 05:02 only reached the journal at 09:41:41, when something else
    finally cleared them - and the pair came up ten seconds later.

    So the return value now means what every caller already assumed it
    meant: the process is gone. False means it is still there, and a caller
    that is about to start a replacement must not.

    Never raises. False on anything unexpected, because False is a fact a
    caller can act on and True would be a guess wearing its clothes.
    """
    key = (store.norm(project), role)
    proc = PROCS.pop(key, None)
    target = pid or (proc.pid if proc else None)
    if not target:
        return False
    # asked before the kill: once the pane's process is gone tmux no longer
    # names it, and the server it leaves is what a whole stop also ends
    seat = tmux_seat(target)
    if seat and not viewer_refused():
        # its viewers' ttys, while its server can still name them: once
        # it is gone they are only what was written down (8.93)
        _ttys = viewer_ttys(seat[0])
        if _ttys:
            note_viewer(seat[0], _ttys)
    gone = None
    if not pid_alive(target):
        gone = True                     # already gone; nothing to wait for
    else:
        try:
            if os.name == "nt":
                r = subprocess.run(["taskkill", "/PID", str(target)]
                                   + (["/T"] if tree else []) + ["/F"],
                                   capture_output=True, timeout=15)
                # A non-zero code here is a refusal - access denied, or a
                # pid that has already gone. It was swallowed, so "stopped"
                # came back even when nothing had been stopped. It is not
                # fatal on its own (the process may still die, or may
                # already be dead), so the wait below decides; what matters
                # is that it can no longer be mistaken for success by itself.
                if r.returncode and not pid_alive(target):
                    gone = True
            else:
                # ALONE IS A KILL, as /F is: a client given SIGTERM sends
                # TERM to the group of each of its background shells on
                # its way out, and the jobs 8.31 keeps died with it on a
                # Mac (client 2.1.299, 2026-10-14). -> DECISIONS.md 8.138
                signal_window(target, signal.SIGTERM if tree
                              else getattr(signal, "SIGKILL", 9), tree)
        except Exception:
            # a refusal, as taskkill's non-zero code above is: the wait
            # decides. Off Windows a refused signal is an exception, and
            # this answered False at once over a window still alive, with
            # no SIGKILL after it (a Mac, 2026-10-12, 8.89)
            if not pid_alive(target):
                gone = True
    if gone is None:
        end = time.time() + (STOP_WAIT_SEC if wait is None
                             else max(0.0, wait))
        while time.time() < end and pid_alive(target):
            time.sleep(0.1)
        gone = not pid_alive(target)
        # a SIGTERM is a request, and a window that ignores it was left
        # running behind a False; taskkill's /F needs no second step
        if not gone and os.name != "nt":
            try:
                signal_window(target, getattr(signal, "SIGKILL", 9), tree)
            except Exception:
                pass
            end = time.time() + KILL_WAIT_SEC
            while time.time() < end and pid_alive(target):
                time.sleep(0.1)
            gone = not pid_alive(target)
    # a client stopped ALONE keeps its jobs (8.31), and they may still hold
    # the pane's terminal - the server is left to go with its last pane
    if gone and seat and tree:
        _tmux(seat[0], "kill-server")
        forget_socket(seat[0])
    if gone and seat:
        sweep_soon()
    return gone


SHELLS = ("bash", "sh.exe", "zsh", "cmd.exe", "powershell", "pwsh")
# off Windows a name is the whole program name, not a prefix: "sh" as a
# prefix would take ssh-agent and shasum for shells
POSIX_SHELLS = ("bash", "zsh", "sh", "dash", "ksh", "fish")


def child_pids(pid, names=SHELLS):
    """Live processes whose parent is `pid` - by name prefix, lowercase.

    The witness for a background job whose window was stopped without its
    tree (8.31). Its record carries no pid of its own - the client hands
    back only a task id - but the job runs in a shell the window started,
    and on Windows a snapshot keeps naming a dead parent's pid as the
    parent of what it left behind. Shells only: the window's MCP servers
    are its children too, and they end themselves when their stdin does.

    None when it cannot be established (the snapshot failed): a record
    without this witness ends at BG_MAX_SEC, as before.

    Off Windows the snapshot is one `ps` of every process. It returned None
    there until a Mac, 2026-10-12, so a job a replaced Mac window left
    running had no witness at all and its record stood for BG_MAX_SEC.
    A job's shell outlives its window there too - it is handed to launchd -
    and it is named while the window is still alive, which is when the
    daemon asks. -> 8.89
    """
    if os.name != "nt":
        return _posix_children(pid, names)
    try:
        import ctypes
        import ctypes.wintypes as W

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                        ("th32ProcessID", W.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                        ("th32ParentProcessID", W.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", W.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        h = k.CreateToolhelp32Snapshot(2, 0)
        if not h or h == ctypes.c_void_p(-1).value:
            return None
        out = []
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32First(ctypes.c_void_p(h), ctypes.byref(e))
            while ok:
                name = e.szExeFile.decode("mbcs", "replace").lower()
                if e.th32ParentProcessID == int(pid) and (
                        not names or name.startswith(tuple(names))):
                    out.append(int(e.th32ProcessID))
                ok = k.Process32Next(ctypes.c_void_p(h), ctypes.byref(e))
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
        return out
    except Exception:
        return None


def is_shell(name, nt=None):
    """Is a process table's name a shell? By prefix on Windows ("cmd.exe",
    "powershell.exe"), by the whole name off it, where "sh" as a prefix
    would take ssh-agent and shasum. -> 8.89, 8.99"""
    nt = (os.name == "nt") if nt is None else nt
    return (name or "").startswith(SHELLS) if nt else \
        (name or "") in POSIX_SHELLS


def _posix_table():
    """{pid: (parent pid, lowercase program name)} from one `ps` of every
    process, or None. A Mac prints the program's path and a login shell as
    "-zsh"; both come out as the bare name. -> 8.89, 8.99"""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid=",
                            "-o", "comm="], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
    except Exception:
        return None
    if r.returncode:
        return None
    out = {}
    for ln in (r.stdout or "").splitlines():
        f = ln.split(None, 2)
        if len(f) < 3 or not (f[0].isdigit() and f[1].isdigit()):
            continue
        out[int(f[0])] = (int(f[1]),
                          os.path.basename(f[2].strip()).lstrip("-").lower())
    return out or None


def _posix_children(pid, names):
    table = _posix_table()
    if table is None:
        return None
    return [p for p, (pp, name) in sorted(table.items())
            if pp == int(pid) and (
                not names or (is_shell(name, nt=False) if names is SHELLS
                              else name.startswith(tuple(names))))]


def process_table():
    """{pid: (parent pid, lowercase image name)} for every process, from one
    snapshot - or None when it cannot be taken. A caller walks a tree from
    it without a second snapshot, so the tree is one moment and not several.
    -> DECISIONS.md 8.50

    Off Windows it is one `ps` (_posix_table). It answered None there until
    2026-10-13, so on a Mac no long command's own tree was ever looked at,
    and one that was working was asked about as stuck (test_multipair 131).
    -> 8.99
    """
    if os.name != "nt":
        return _posix_table()
    try:
        import ctypes
        import ctypes.wintypes as W

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                        ("th32ProcessID", W.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                        ("th32ParentProcessID", W.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", W.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        h = k.CreateToolhelp32Snapshot(2, 0)
        if not h or h == ctypes.c_void_p(-1).value:
            return None
        out = {}
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32First(ctypes.c_void_p(h), ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = (
                    int(e.th32ParentProcessID),
                    e.szExeFile.decode("mbcs", "replace").lower())
                ok = k.Process32Next(ctypes.c_void_p(h), ctypes.byref(e))
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
        return out
    except Exception:
        return None


def tcp_owner(local_port, remote_port):
    """The pid that owns the loopback IPv4 TCP connection whose local end is
    `local_port` and whose remote end is 127.0.0.1:`remote_port` - for a
    request the daemon has in hand, the CLIENT's end of it: local is the
    port the client connected from, remote is the daemon's own. None when it
    cannot be told: the table refused, no `lsof` off Windows, or no such
    row (the client already let go). Never raises. -> DECISIONS.md 8.68
    """
    if os.name != "nt":
        return _posix_tcp_owner(local_port, remote_port)
    try:
        import ctypes
        import socket
        import ctypes.wintypes as W
        ip = ctypes.WinDLL("iphlpapi")
        ip.GetExtendedTcpTable.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(W.DWORD), W.BOOL,
                                           W.ULONG, ctypes.c_int, W.ULONG]
        ip.GetExtendedTcpTable.restype = W.DWORD
        # AF_INET; TCP_TABLE_OWNER_PID_ALL - every state, with the owner
        size = W.DWORD(0)
        ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
        buf = None
        # the table can grow between the call that sizes it and the call
        # that fills it (122 = ERROR_INSUFFICIENT_BUFFER)
        for _ in range(3):
            buf = ctypes.create_string_buffer(size.value + 4096)
            size = W.DWORD(len(buf))
            rc = ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5,
                                        0)
            if rc == 0:
                break
            if rc != 122:
                return None
        else:
            return None
        count = ctypes.cast(buf, ctypes.POINTER(W.DWORD))[0]
        rows = ctypes.cast(ctypes.addressof(buf) + 4,
                           ctypes.POINTER(W.DWORD * 6))
        # a row is state, local addr, local port, remote addr, remote port,
        # owner; the ports sit in network order in the low word
        loop = 0x0100007F           # 127.0.0.1 in network order, as read
        for i in range(count):
            _st, _la, lp, ra, rp, pid = rows[i]
            if socket.ntohs(lp & 0xFFFF) == int(local_port) \
                    and socket.ntohs(rp & 0xFFFF) == int(remote_port) \
                    and ra == loop:
                return int(pid)
        return None
    except Exception:
        return None



def parse_netstat(text):
    """The rows of a Mac's `netstat -anv -p tcp` as (local, foreign, state,
    pid), the addresses as it prints them ("127.0.0.1.8765"). None when its
    header has no process:pid column, so a caller knows to ask lsof
    instead. Pure, so a suite reads it on any machine.

    The pid is the first word past the state that ends ":<digits>": the
    process name before it can hold a space ("Google Chrome He:67263",
    2026-10-14) and can be empty (":98139"); a pid of 0 names no process
    and is not taken. The column is Darwin 25.6's; a netstat without it is
    asked nothing more. -> DECISIONS.md 8.131"""
    rows, named = [], False
    for ln in (text or "").splitlines():
        w = ln.split()
        if not w:
            continue
        if w[0] == "Proto":
            named = "process:pid" in w
            continue
        if not named or not w[0].startswith("tcp") or len(w) < 7:
            continue
        for tok in w[6:]:
            _name, colon, num = tok.rpartition(":")
            if colon and num.isdigit():
                if int(num):
                    rows.append((w[3], w[4], w[5], int(num)))
                break
    return rows if named else None


def netstat_rows():
    """parse_netstat of this machine's own table, or None off a Mac and
    when netstat cannot name the processes. netstat reads the kernel's
    table and nothing else; lsof walks every process's files, and one
    process stuck on the share (a grep in state U on 2026-10-14) held a
    system-wide lsof for 17-80 s while netstat answered in 0.01 s - so the
    /session guard could not name its asker, and pid_on_port named no
    daemon on a port it served. -> DECISIONS.md 8.131"""
    if sys.platform != "darwin":
        return None
    netstat = shutil.which("netstat") or "/usr/sbin/netstat"
    try:
        r = subprocess.run([netstat, "-anv", "-p", "tcp"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=10)
    except Exception:
        return None
    return parse_netstat(r.stdout)


def _posix_tcp_owner(local_port, remote_port):
    """tcp_owner off Windows: on a Mac the row of netstat_rows that reads
    127.0.0.1:`local_port` -> 127.0.0.1:`remote_port`; where netstat names
    no process, `lsof` of the sockets with an end at 127.0.0.1:`local_port`,
    and the pid whose own row reads local->remote. The daemon's end of the
    same connection is listed too, the other way round, and is not taken.
    Until 2026-10-13 this was None, so on a Mac no asker was ever
    established and 8.68's refusal let every request through
    (test_multipair 153). lsof is given 5 s, not 15: it is the fallback,
    and a stuck one is what 8.131 is about. -> DECISIONS.md 8.68, 8.99,
    8.131"""
    rows = netstat_rows()
    if rows is not None:
        here = "127.0.0.1.%d" % int(local_port)
        there = "127.0.0.1.%d" % int(remote_port)
        for local, foreign, state, pid in rows:
            # only while a process holds the socket: an end its client let
            # go stays in the table, FIN_WAIT_2 then TIME_WAIT, with the
            # pid of a process already gone (test_handover 155)
            if local == here and foreign == there \
                    and state in ("ESTABLISHED", "CLOSE_WAIT"):
                return pid
        return None
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        r = subprocess.run([lsof, "-nP", "-iTCP@127.0.0.1:%d" % int(local_port),
                            "-Fpn"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
    except Exception:
        return None
    want = "n127.0.0.1:%d->127.0.0.1:%d" % (int(local_port), int(remote_port))
    pid = None
    for ln in (r.stdout or "").splitlines():
        if ln[:1] == "p" and ln[1:].isdigit():
            pid = int(ln[1:])
        elif ln == want and pid:
            return pid
    return None

def proc_cpu(pid):
    """CPU seconds this process has used so far (kernel + user), or None
    when it cannot be read. Only a LIVE process is counted: Windows keeps
    no running total of a process's children, so what a finished child used
    is gone - a caller measures a tree by summing the live members.
    -> DECISIONS.md 8.50

    Off Windows: /proc/<pid>/stat where there is one (Linux), else `ps`
    (a Mac). None there too until 2026-10-13 (8.99).
    """
    if os.name != "nt":
        return _posix_cpu(pid)
    try:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED
        if not h:
            return None
        try:
            created, exited = wt.FILETIME(), wt.FILETIME()
            kernel, user = wt.FILETIME(), wt.FILETIME()
            if not k32.GetProcessTimes(h, ctypes.byref(created),
                                       ctypes.byref(exited),
                                       ctypes.byref(kernel),
                                       ctypes.byref(user)):
                return None
        finally:
            k32.CloseHandle(h)
        t = ((kernel.dwHighDateTime << 32) | kernel.dwLowDateTime) + \
            ((user.dwHighDateTime << 32) | user.dwLowDateTime)
        return t / 1e7
    except Exception:
        return None


def cpu_seconds(text):
    """`ps -o time=` as seconds, or None: "455:59.77" on a Mac (minutes with
    no hours above them) and "[D-]HH:MM:SS" from procps. -> 8.99"""
    t = (text or "").strip()
    try:
        days = 0
        if "-" in t:
            d, t = t.split("-", 1)
            days = int(d)
        secs = 0.0
        for part in t.split(":"):
            secs = secs * 60 + float(part)
        return days * 86400 + secs if t else None
    except ValueError:
        return None


def _posix_cpu(pid):
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as fh:
            st = fh.read().decode("utf-8", "replace")
        # utime and stime, fields 14 and 15, counted after the ")" that
        # closes a name which may itself hold spaces
        f = st[st.rfind(")") + 2:].split()
        return (int(f[11]) + int(f[12])) / float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        pass
    except Exception:
        return None
    try:
        r = subprocess.run(["ps", "-o", "time=", "-p", str(int(pid))],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=10)
    except Exception:
        return None
    return cpu_seconds(r.stdout) if r.returncode == 0 else None


def terminate_and_wait(pid, timeout=30.0):
    """Kill a process and WAIT on it. True when it is really gone.

    The point is ownership, not patience. Every other way of asking "has it
    gone yet" in this project is a poll with a margin on it, and a margin is
    a guess about how long dying takes - the same thing S5.38 refused when
    it was about a transcript. A guess that is right on a quiet machine and
    wrong on a busy one is worse than either answer, because it makes the
    result depend on who runs it: measured 2026-08-31, the same suite on the
    same code was green here and red on the planner's run, which shares this
    machine with a live daemon and two pairs.

    On Windows there is a real thing to wait on. The process handle is
    signalled when the process ends, so WaitForSingleObject with a genuine
    timeout returns AT the moment of death rather than at the next poll. Two
    details make it deterministic rather than merely likelier:

      * the handle is opened BEFORE TerminateProcess, so there is no window
        in which the pid could be reused and the wait could attach to a
        stranger; and
      * holding the handle keeps the process object alive after exit, so a
        process that dies instantly is still waitable - the handle is simply
        already signalled and the wait returns at once.

    OpenProcess failing means there is nothing to open, which for a pid we
    were about to kill means it has gone and been reaped. That is True, not
    an error.

    POSIX has no such handle for a process that is not our child, so there
    it stays a bounded poll - said plainly rather than pretended away. The
    fixtures this exists for run on Windows.
    """
    if not pid:
        return True
    pid = int(pid)
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        # SYNCHRONIZE | PROCESS_TERMINATE
        h = k32.OpenProcess(0x100000 | 0x0001, False, pid)
        if not h:
            return not pid_alive(pid)
        try:
            k32.TerminateProcess(h, 1)
            ms = 0xFFFFFFFF if timeout is None else int(max(0.0, timeout)
                                                        * 1000)
            return k32.WaitForSingleObject(h, ms) == 0   # WAIT_OBJECT_0
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    end = time.time() + (timeout or 0)
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def pid_alive(pid):
    """Is this pid still a RUNNING process? Cross-platform, best effort.

    On Windows this used to ask only whether OpenProcess succeeded, and
    that is not the same question. A process that has exited but whose
    handle is still held - by its own parent, which is exactly what the
    bridge is for every window it launches - remains openable. So a killed
    child reported itself alive until somebody reaped it.

    That mattered the moment stop() began waiting for death instead of
    assuming it: every rotation and every handover would have waited the
    full timeout and then refused to start a replacement, for a window
    that had died on time. A repair that blocks the thing it repairs.

    WaitForSingleObject with a zero timeout asks the right question: the
    handle is signalled when the process ends, so WAIT_OBJECT_0 means gone
    and WAIT_TIMEOUT means still running. GetExitCodeProcess would have
    done too, except that a process exiting with code 259 is indisinguishable
    from STILL_ACTIVE, and 259 is a real exit code somebody will hit one day.

    POSIX had the same blind spot through os.kill(pid, 0): a zombie answers
    it. That was said here as harmless "because Popen reaps", and a Mac
    showed otherwise on 2026-10-12 - a window that died while nothing
    polled its Popen read as alive, stop() waited 20 s to say False, and a
    relaunch was refused for a window "sitting on a startup dialog". So a
    pid that answers is asked once more: is it a zombie (`proc_state`)?
    One of ours is reaped on the spot, which Popen survives (it reads the
    lost status as 0). -> 8.89
    """
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x100000, False, int(pid))   # SYNCHRONIZE
            if not h:
                return False
            try:
                return k32.WaitForSingleObject(h, 0) != 0    # 0 = signalled
            finally:
                k32.CloseHandle(h)
        os.kill(int(pid), 0)
    except Exception:
        return False
    if proc_state(pid) != "Z":
        return True
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except Exception:
        pass
    return False


_LIBC = []


def _libc():
    if not _LIBC:
        import ctypes
        import ctypes.util
        _LIBC.append(ctypes.CDLL(ctypes.util.find_library("c")
                                 or "libc.dylib", use_errno=True))
    return _LIBC[0]


def kinfo(pid, libc=None):
    """macOS: (start in epoch seconds, p_stat) of a pid, from sysctl
    KERN_PROC_PID; None when there is no such pid or the answer does not
    hold together. struct kinfo_proc opens with struct extern_proc: a union
    holding p_starttime (a timeval) at 0, p_stat (a char) at 36 and p_pid at
    40 on a 64-bit Mac. p_pid is checked against the pid asked for, so a
    layout that ever moves reads as None and not as a wrong answer.
    `libc` is a seam for the PC, which has no sysctl."""
    import ctypes
    import struct
    try:
        lib = libc or _libc()
        mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))  # KERN, PROC, PROC_PID
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        if lib.sysctl(mib, 4, buf, ctypes.byref(size), None,
                      ctypes.c_size_t(0)) != 0 or size.value < 44:
            return None
        sec, usec = struct.unpack_from("<qi", buf.raw, 0)
        stat = struct.unpack_from("<b", buf.raw, 36)[0]
        if struct.unpack_from("<i", buf.raw, 40)[0] != int(pid) or sec <= 0:
            return None
        return sec + usec / 1e6, stat
    except Exception:
        return None


def mac_boottime(libc=None):
    """macOS: when this machine booted, in epoch seconds (sysctl
    kern.boottime, a timeval), or None."""
    import ctypes
    import struct
    try:
        lib = libc or _libc()
        buf = ctypes.create_string_buffer(16)
        size = ctypes.c_size_t(16)
        if lib.sysctlbyname(b"kern.boottime", buf, ctypes.byref(size), None,
                            ctypes.c_size_t(0)) != 0 or size.value < 12:
            return None
        sec, usec = struct.unpack_from("<qi", buf.raw, 0)
        return sec + usec / 1e6 if sec > 0 else None
    except Exception:
        return None


def proc_state(pid, platform=None):
    """POSIX: "Z" for a zombie, "" for a process that is not one, None when
    it cannot be told. Linux reads /proc/<pid>/stat; a Mac asks kinfo, where
    SZOMB is 5. Windows has no zombies: "" there."""
    platform = platform or sys.platform
    if os.name == "nt" and platform == sys.platform:
        return ""
    if platform == "darwin":
        k = kinfo(pid)
        return None if k is None else ("Z" if k[1] == 5 else "")
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as fh:
            st = fh.read().decode("utf-8", "replace")
        return "Z" if st[st.rfind(")") + 2:][:1] == "Z" else ""
    except Exception:
        return None


def alive(project, role):
    proc = PROCS.get((store.norm(project), role))
    return proc is not None and proc.poll() is None


def transcript_of(session_id, cwd=None):
    """Path to a session's transcript on disk, if it exists."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(base):
        return None
    if session_id:
        for folder in os.listdir(base):
            cand = os.path.join(base, folder, "%s.jsonl" % session_id)
            if os.path.exists(cand):
                return cand
    if cwd:
        import re
        # Deliberately NOT store.norm: this reproduces the folder name
        # Claude Code itself made under ~/.claude/projects, and it encodes
        # the path as it was given, case and all. Folding the case here
        # would build a name that is not on disk and find nothing.
        enc = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd))
        folder = os.path.join(base, enc)
        if os.path.isdir(folder):
            files = [os.path.join(folder, f) for f in os.listdir(folder)
                     if f.endswith(".jsonl")]
            if files:
                files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return files[0]
    return None


def usage_from_transcript(path, tail_bytes=400000):
    """How much context a session is carrying, read from its own transcript.

    The status line only reports while a session is drawing itself, so a
    window sitting at its prompt - or on a startup dialog - tells the bridge
    nothing. The transcript is written as the session goes and stays on
    disk, and every assistant turn in it records what the request cost. The
    last such record is the size of the conversation right now, available
    without asking the session for anything.

    ``context_tokens`` is the carried context of §1.3 and nothing else:
    input + cache_creation + cache_read, by name. This used to add
    output_tokens, which made it a different quantity from the one the
    status-line path computes - and both wrote to the same field, so a turn
    cost could be measured between two readings that did not mean the same
    thing. The last turn's output is still reported, under its own name,
    for anyone who actually wants it.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()          # drop the partial line
            chunk = fh.read().decode("utf-8", "replace")
    except Exception:
        return None
    best = None
    for line in chunk.splitlines():
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        msg = row.get("message") or {}
        usage = msg.get("usage") or row.get("usage") or {}
        if not usage:
            continue
        total, fields = store.carried_from_usage(usage)
        if not total or total <= 0:
            continue
        out = usage.get("output_tokens")
        best = {"context_tokens": total,
                "token_fields": fields,
                # named separately, never folded into the carried figure
                "last_output_tokens": (int(out)
                                       if isinstance(out, (int, float))
                                       else None),
                "model": msg.get("model") or row.get("model") or "",
                "at": row.get("timestamp") or ""}
    if best:
        try:
            best["file_mtime"] = os.path.getmtime(path)
        except Exception:
            pass
    return best


def _text_of(msg):
    content = (msg or {}).get("content")
    if isinstance(content, str):
        return content
    out = []
    for block in (content or []):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(block.get("text") or "")
        elif kind == "tool_use":
            out.append("[ran %s]" % (block.get("name") or "a tool"))
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, str):
                out.append("[result] " + body[:300])
            else:
                out.append("[result]")
    return "\n".join(x for x in out if x)


def tail_of_transcript(path, turns=6, per_turn=1200, tail_bytes=600000):
    """The last few exchanges of a session, as readable text.

    Numbers say how full a session is; they cannot say what it is waiting
    for. A session stopped because it asked a question, one stopped because
    a build is running, and one stopped because the bridge cut its turn all
    look identical from the outside - and call for three different answers.
    The words are the only thing that tells them apart.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            chunk = fh.read().decode("utf-8", "replace")
    except Exception:
        return []
    rows = []
    for line in chunk.splitlines():
        if '"type"' not in line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        kind = row.get("type")
        if kind not in ("user", "assistant"):
            continue
        text = _text_of(row.get("message") or {})
        if not text.strip():
            continue
        rows.append({"who": kind, "text": text[:per_turn],
                     "at": row.get("timestamp") or ""})
    return rows[-turns:]


# ---------------------------------------------------------------------------
# What a window that has not come up is ASKING
#
# A window the bridge launches is born minimised and unfocused (rule 29), so
# a question at its start is a question nobody is looking at. On 2026-09-04
# one sat like that from 04:02 to 10:13 - six hours without an executor -
# and it was the client's `--dangerously-load-development-channels` warning,
# waiting for Enter. Measured that day: nothing in the client's config
# answers that one, and `--channels` instead removes the prompt and silently
# stops delivering. So the only thing left is to read the screen and answer
# it. -> source/ANALYSIS-client-silence.md
#
# BOTH HELPERS RUN IN A CHILD PROCESS, and for the same three reasons
# relayout.close_console documents: FreeConsole must come first (a process
# born with CREATE_NO_WINDOW has a console without a window, and
# AttachConsole then answers ACCESS_DENIED); the console must be let go
# before the child exits; and - this one is ours - once attached, anything
# the child prints goes INTO the watched window and would type itself into
# the very prompt being measured. So the screen is written to a file and
# only the path is printed, after FreeConsole.
#
# AND BOTH ARE RAW STRINGS, r""" - which is the whole difference between a
# helper that runs and one that has never run. These hold PYTHON SOURCE, so
# every escape in them belongs to the child and not to this file: written
# without the r, the two characters backslash-n inside `"\n".join(lines)`
# became a real newline HERE, and the child was handed an unterminated
# string literal. It died of SyntaxError before reading a thing - and
# because console_screen catches everything and answers "", a dead helper
# and a genuinely blank screen are the same answer, so nothing anywhere
# said so. Measured 2026-09-04: nudge_deaf_window was reached ten times
# that day and journalled neither of its two loud lines, and console_screen
# returned 0 characters for all four live windows, including the one doing
# the asking. _ANSWER_SRC carried the same defect at its '\r', so no Enter
# was ever sent either. probe_console_screen.py has that same line in its
# OWN source, where nothing nests it, which is why the measurement of that
# day was honest about the technique and silent about this path.
# test_multipair case 82 compiles both and then RUNS them against a real
# console. -> DECISIONS.md 8.10

_SCREEN_SRC = r"""
import ctypes, ctypes.wintypes as w, sys
pid, out = int(sys.argv[1]), sys.argv[2]
k = ctypes.WinDLL('kernel32', use_last_error=True)


class COORD(ctypes.Structure):
    _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [('Left', ctypes.c_short), ('Top', ctypes.c_short),
                ('Right', ctypes.c_short), ('Bottom', ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [('dwSize', COORD), ('dwCursorPosition', COORD),
                ('wAttributes', ctypes.c_ushort), ('srWindow', SMALL_RECT),
                ('dwMaximumWindowSize', COORD)]


k.FreeConsole()
if not k.AttachConsole(pid):
    raise SystemExit(2)
k.CreateFileW.restype = w.HANDLE
h = k.CreateFileW('CONOUT$', 0x80000000 | 0x40000000, 0x3, None, 3, 0, None)
if h == w.HANDLE(-1).value:
    k.FreeConsole()
    raise SystemExit(3)
info = CSBI()
if not k.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
    k.FreeConsole()
    raise SystemExit(4)
width = info.dwSize.X
lines = []
buf = ctypes.create_unicode_buffer(width + 1)
got = w.DWORD(0)
for y in range(info.dwSize.Y):
    if not k.ReadConsoleOutputCharacterW(h, buf, width, COORD(0, y),
                                         ctypes.byref(got)):
        break
    lines.append(buf[:got.value].rstrip())
k.FreeConsole()
body = "\n".join(lines).strip()
open(out, 'w', encoding='utf-8').write(body)
"""

_ANSWER_SRC = r"""
import ctypes, ctypes.wintypes as w, sys
pid = int(sys.argv[1])
k = ctypes.WinDLL('kernel32', use_last_error=True)


class CHAR_U(ctypes.Union):
    _fields_ = [('UnicodeChar', ctypes.c_wchar), ('AsciiChar', ctypes.c_char)]


class KEY_EVENT(ctypes.Structure):
    _fields_ = [('bKeyDown', ctypes.c_int),
                ('wRepeatCount', ctypes.c_ushort),
                ('wVirtualKeyCode', ctypes.c_ushort),
                ('wVirtualScanCode', ctypes.c_ushort),
                ('uChar', CHAR_U), ('dwControlKeyState', ctypes.c_ulong)]


class EVENT_U(ctypes.Union):
    _fields_ = [('KeyEvent', KEY_EVENT)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [('EventType', ctypes.c_ushort), ('Event', EVENT_U)]


k.FreeConsole()
if not k.AttachConsole(pid):
    raise SystemExit(2)
k.CreateFileW.restype = w.HANDLE
h = k.CreateFileW('CONIN$', 0x80000000 | 0x40000000, 0x3, None, 3, 0, None)
if h == w.HANDLE(-1).value:
    k.FreeConsole()
    raise SystemExit(3)
recs = (INPUT_RECORD * 2)()
for i, down in enumerate((1, 0)):
    recs[i].EventType = 1
    recs[i].Event.KeyEvent.bKeyDown = down
    recs[i].Event.KeyEvent.wRepeatCount = 1
    recs[i].Event.KeyEvent.wVirtualKeyCode = 0x0D
    recs[i].Event.KeyEvent.uChar.UnicodeChar = '\r'
written = w.DWORD(0)
ok = k.WriteConsoleInputW(h, recs, 2, ctypes.byref(written))
k.FreeConsole()
raise SystemExit(0 if ok and written.value == 2 else 5)
"""


QUICK_EDIT_FLAG = 0x0040        # ENABLE_QUICK_EDIT_MODE
EXTENDED_FLAGS = 0x0080         # ENABLE_EXTENDED_FLAGS

# RAW, and this one is not a formality: _SCREEN_SRC and _ANSWER_SRC held
# their own Python source in NON-raw strings for the whole of their lives
# and never compiled once (-> DECISIONS.md 8.10). Every child source in
# this module is raw from now on, whether it currently needs it or not.
_QUIET_EDIT_SRC = r"""
import ctypes, json, sys
from ctypes import wintypes

QUICK_EDIT = 0x0040
EXTENDED = 0x0080
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
SHARE_RW = 0x00000003
OPEN_EXISTING = 3

pid = int(sys.argv[1])
apply_it = sys.argv[2] == "1"
forced = int(sys.argv[3])
out = {"pid": pid}
k = ctypes.windll.kernel32
k.FreeConsole()
if not k.AttachConsole(pid):
    out["error"] = "AttachConsole failed for %d" % pid
else:
    try:
        k.CreateFileW.restype = wintypes.HANDLE
        h = k.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, SHARE_RW,
                          None, OPEN_EXISTING, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            out["error"] = "CONIN$ could not be opened"
        else:
            m = wintypes.DWORD(0)
            if not k.GetConsoleMode(h, ctypes.byref(m)):
                out["error"] = "GetConsoleMode failed"
            else:
                out["before"] = int(m.value)
                if apply_it:
                    if forced >= 0:
                        want = forced
                    else:
                        want = (m.value | EXTENDED) & ~QUICK_EDIT
                    out["asked"] = int(want)
                    out["ok"] = bool(k.SetConsoleMode(h, want))
                m2 = wintypes.DWORD(0)
                if k.GetConsoleMode(h, ctypes.byref(m2)):
                    out["after"] = int(m2.value)
            k.CloseHandle(h)
    except Exception as exc:
        out["error"] = str(exc)
    finally:
        k.FreeConsole()

with open(sys.argv[4], "w", encoding="utf-8") as fh:
    json.dump(out, fh)
"""


def console_quiet_edit(pid, apply=True, mode=None, timeout=20):
    """Turn QuickEdit OFF on the console this pid is attached to.

    Returns {"before", "after", "asked", "ok", "error"} - always a dict,
    never raises. `before` and `after` are the console's input mode as it
    was and as it is; both are read with GetConsoleMode, so the caller
    reports a measurement rather than its own intention (rule 30). With
    apply=False nothing is written and only `before` is filled.

    WHY THIS EXISTS. 2026-09-05: the owner's windows kept freezing - the
    picture stopping while the model carried on, tool calls and all, and a
    keystroke reviving it without interrupting the turn. The legacy console
    host blocks the application in WriteConsole while a SELECTION is up,
    and Esc clears the selection and is eaten by the host, so nothing ever
    reached the client and no transcript ever recorded a break. It was
    caught from outside, with nothing attached: the frozen window's TITLE
    read the host's own word for a selection, and across 78 s that title
    never changed while two control windows changed five times each.

    AND THE 0x80 IS THE WHOLE POINT. CLAUDE.md excluded QuickEdit in
    August on a reading of 0x0208 in which bit 0x40 was clear - but 0x0208
    also has ENABLE_EXTENDED_FLAGS clear, and with THAT bit clear the mode
    word carries no QuickEdit or Insert bit at all: they come from the
    console's defaults, and HKCU\\Console\\QuickEdit on this machine is 1.
    So `mode & ~QUICK_EDIT` is a no-op on exactly the consoles that need
    fixing - 0x0208 in, 0x0208 out - and the flag has to be turned on in
    the same write that turns QuickEdit off. test_multipair case 88 pins
    the arithmetic and then does it on a console the suite owns.

    `mode` is a TEST SEAM, never a setting: it writes exactly that value,
    so a case can put a console into the client's 0x0208 and show what the
    August reading could and could not see. -> DECISIONS.md 8.14
    """
    if os.name != "nt" or not pid:
        return {"error": "not windows" if os.name != "nt" else "no pid"}
    res = os.path.join(tempfile.gettempdir(),
                       "bridge-qe-%d-%d.json" % (pid, int(time.time() * 1000)))
    try:
        subprocess.run([sys.executable, "-c", _QUIET_EDIT_SRC, str(pid),
                        "1" if apply else "0",
                        str(-1 if mode is None else int(mode)), res],
                       capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW",
                                             0))
        with io.open(res, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        try:
            os.remove(res)
        except OSError:
            pass


def console_screen(pid, timeout=20):
    """The text on the console pid is attached to, or "". Never raises."""
    seat = tmux_seat(pid)
    if seat:
        # shaped as _SCREEN_SRC shapes the console: each line right-trimmed,
        # the whole stripped, so prompt_line reads both the same way
        r = _tmux(seat[0], "capture-pane", "-p", "-t", seat[1],
                  timeout=timeout)
        if not r or r.returncode:
            return ""
        return "\n".join(ln.rstrip() for ln in
                         (r.stdout or "").splitlines()).strip()
    if os.name != "nt":
        return ""
    tmp = os.path.join(tempfile.gettempdir(),
                       "bridge-screen-%d-%d.txt" % (pid, int(time.time())))
    try:
        subprocess.run([sys.executable, "-c", _SCREEN_SRC, str(pid), tmp],
                       capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW",
                                             0))
        with io.open(tmp, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def console_answer(pid, timeout=20):
    """Press Enter in that console. True if the key was accepted.

    NEVER call this on a window that has come up. A live session's prompt
    takes Enter as "send what is typed here", and what is typed there is
    whatever the last thing to touch that window left behind. The only
    caller checks `registered` first, and that check is the safety, not
    politeness.
    """
    seat = tmux_seat(pid)
    if seat:
        r = _tmux(seat[0], "send-keys", "-t", seat[1], "Enter",
                  timeout=timeout)
        return bool(r) and r.returncode == 0
    if os.name != "nt":
        return False
    try:
        r = subprocess.run([sys.executable, "-c", _ANSWER_SRC, str(pid)],
                           capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def claude_processes():
    """Every claude process running on this machine, by pid.

    The indirect signals all have holes: the channel registry is in memory
    and empties when the bridge restarts, a pid record only exists for
    windows the bridge itself opened, and a session that is simply sitting
    at its prompt stops reporting. The operating system has none of those
    holes - if a window is open, its process is there.
    """
    pids = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV",
                 "/NH"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15).stdout or ""
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) > 1 and parts[0].lower().startswith("claude"):
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
            if not pids:
                # claude may run as a node process; ask for the command line
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process | "
                     "Where-Object { $_.CommandLine -match 'claude' -and "
                     "$_.CommandLine -notmatch 'bridge' } | "
                     "Select-Object -ExpandProperty ProcessId"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=25).stdout or ""
                for line in out.split():
                    try:
                        pids.append(int(line))
                    except ValueError:
                        pass
        else:
            out = subprocess.run(["pgrep", "-f", "claude"],
                                 capture_output=True, text=True,
                                 timeout=15).stdout or ""
            for line in out.split():
                try:
                    pids.append(int(line))
                except ValueError:
                    pass
    except Exception:
        return []
    return sorted(set(pids))


def past_sessions(project, limit=12):
    """Past sessions of this project, newest first, from the transcripts."""
    home = os.path.expanduser("~")
    base = os.path.join(home, ".claude", "projects")
    rows = []
    if not os.path.isdir(base):
        return rows
    for entry in os.listdir(base):
        folder = os.path.join(base, entry)
        if not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(folder, fn)
            meta = _transcript_meta(path)
            if not meta:
                continue
            if store.norm(meta["cwd"]) != store.norm(project):
                continue
            meta["mtime"] = os.path.getmtime(path)
            rows.append(meta)
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    for r in rows:
        r["when"] = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(r.pop("mtime")))
    return rows[:limit]


def _transcript_meta(path, scan_lines=4000):
    sid = cwd = first_user = None
    turns = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > scan_lines:
                    break
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                sid = sid or row.get("sessionId") or row.get("session_id")
                cwd = cwd or row.get("cwd")
                t = row.get("type")
                if t in ("user", "assistant"):
                    turns += 1
                if not first_user and t == "user":
                    msg = row.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        first_user = content[:90]
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                first_user = (c.get("text") or "")[:90]
                                break
    except Exception:
        return None
    if not sid or not cwd:
        return None
    return {"session_id": sid, "cwd": cwd, "turns": turns,
            "first_line": first_user or ""}
