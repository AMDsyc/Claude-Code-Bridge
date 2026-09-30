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
import signal
import subprocess
import sys
import tempfile
import time

from . import store

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
INHERITED_CLIENT_MARKS = (
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
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
                  model=None, disallow=None, prompt=None, effort=None):
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


def compact_pct_for(project, pct):
    """The percentage this window may honestly be given, or None.

    BOTH HALVES OR NEITHER, and that is the whole of what 8.2 measured. The
    percentage and `autoCompactWindow` MULTIPLY - 700000 x 70 % gave a point
    at 476-482k over 14 samples - and WITHOUT the key the variable is inert:
    the pair next door received the same 70 and compacted at the ceiling
    regardless. So sending a percentage to a project that has no key would
    not merely do nothing; it would write a threshold into `reg_pid` that
    the window is not running under, which is the class of claim this
    project keeps having to unpick.

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
    return pct if project_compact_window(project) else None


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


def launch(project, role, resume_id=None, permission_mode=None, model=None,
           disallow=None, compact_pct=None, prompt=None, effort=None):
    """Start a session in its own minimised console. Returns pid."""
    if not os.path.isdir(project):
        raise ValueError("no such folder: %s" % project)

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
    applied = compact_pct_for(project, compact_pct)
    if applied:
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(applied)

    cmd = build_command(project, role, resume_id, permission_mode, model,
                        disallow, prompt, effort=effort)
    why = real_client_refused(cmd)
    if why:
        raise RuntimeError(why)

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
    if not pid_alive(target):
        return True                     # already gone; nothing to wait for
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(target)]
                               + (["/T"] if tree else []) + ["/F"],
                               capture_output=True, timeout=15)
            # A non-zero code here is a refusal - access denied, or a pid
            # that has already gone. It was swallowed, so "stopped" came
            # back even when nothing had been stopped. It is not fatal on
            # its own (the process may still die, or may already be dead),
            # so the wait below decides; what matters is that it can no
            # longer be mistaken for success by itself.
            if r.returncode and not pid_alive(target):
                return True
        else:
            if not tree:
                os.kill(target, signal.SIGTERM)
            else:
                try:
                    os.killpg(os.getpgid(target), signal.SIGTERM)
                except Exception:
                    os.kill(target, signal.SIGTERM)
    except Exception:
        return not pid_alive(target)
    end = time.time() + (STOP_WAIT_SEC if wait is None else max(0.0, wait))
    while time.time() < end:
        if not pid_alive(target):
            return True
        time.sleep(0.1)
    return not pid_alive(target)


SHELLS = ("bash", "sh.exe", "zsh", "cmd.exe", "powershell", "pwsh")


def child_pids(pid, names=SHELLS):
    """Live processes whose parent is `pid` - by name prefix, lowercase.

    The witness for a background job whose window was stopped without its
    tree (8.31). Its record carries no pid of its own - the client hands
    back only a task id - but the job runs in a shell the window started,
    and on Windows a snapshot keeps naming a dead parent's pid as the
    parent of what it left behind. Shells only: the window's MCP servers
    are its children too, and they end themselves when their stdin does.

    None when it cannot be established (not Windows, or the snapshot
    failed): a record without this witness ends at BG_MAX_SEC, as before.
    """
    if os.name != "nt":
        return None
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


def process_table():
    """{pid: (parent pid, lowercase image name)} for every process, from one
    snapshot - or None when it cannot be taken (not Windows, or the snapshot
    failed). A caller walks a tree from it without a second snapshot, so the
    tree is one moment and not several. -> DECISIONS.md 8.50
    """
    if os.name != "nt":
        return None
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


def proc_cpu(pid):
    """CPU seconds this process has used so far (kernel + user), or None
    when it cannot be read. Only a LIVE process is counted: Windows keeps
    no running total of a process's children, so what a finished child used
    is gone - a caller measures a tree by summing the live members.
    -> DECISIONS.md 8.50
    """
    if os.name != "nt":
        return None
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

    POSIX keeps os.kill(pid, 0), which has the same blind spot for zombies;
    the bridge's children are reaped by Popen there, and nothing has yet
    depended on the difference. Said plainly rather than left to be found.
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
        return True
    except Exception:
        return False


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
