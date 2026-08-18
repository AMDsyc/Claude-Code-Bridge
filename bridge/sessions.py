"""Start, track and stop the Claude Code sessions of a project.

Sessions run in real, visible (minimised) console windows and are tracked
by PID, so the bridge can rotate the executor by actually ending the old
process before starting the new one - never two live sessions fighting
over the same seat.
"""

import json
import os
import signal
import subprocess
import sys
import time

from . import store

CREATE_NEW_CONSOLE = 0x00000010
SW_SHOWMINNOACTIVE = 7

ROLE_DEFAULTS = {
    "executor": {"permission_mode": "auto", "title": "Executor"},
    # plan is only the default - the panel can set any mode for either role
    "planner": {"permission_mode": "plan", "title": "Planner"},
}

# (project, role) -> Popen, and the project is store.norm'd - the same key
# the daemon uses for everything else. It was os.path.normpath here alone,
# which folds separators but not case, so a project reached by a differently
# cased path got a second entry: launch() recorded the window under one
# spelling and alive()/stop() looked for it under another, found nothing,
# and reported a live session as gone.
PROCS = {}


def _bridge_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_command(project, role, resume_id=None, permission_mode=None,
                  model=None, disallow=None):
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
    if disallow:
        # a deny beats every permission mode, so this holds whatever mode
        # the window was started in
        cmd += ["--disallowedTools", ",".join(disallow)]
    return cmd


def launch(project, role, resume_id=None, permission_mode=None, model=None,
           disallow=None, autocompact_pct=None):
    """Start a session in its own minimised console. Returns pid."""
    if not os.path.isdir(project):
        raise ValueError("no such folder: %s" % project)

    env = dict(os.environ)
    env["BRIDGE_ROLE"] = role
    if autocompact_pct:
        # Where compaction fires stops being a guess the moment we set it.
        # Claude Code's own default has moved around between versions and
        # reports disagree about it, so the bridge names the number instead
        # of trying to infer it.
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(int(autocompact_pct))
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = _bridge_root() + os.pathsep + env.get("PYTHONPATH", "")
    if role == "executor":
        env.setdefault("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "200")

    cmd = build_command(project, role, resume_id, permission_mode, model,
                        disallow)

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


def stop(project, role, pid=None):
    """End a session process (and its children). Best effort, never raises."""
    key = (store.norm(project), role)
    proc = PROCS.pop(key, None)
    target = pid or (proc.pid if proc else None)
    if not target:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            try:
                os.killpg(os.getpgid(target), signal.SIGTERM)
            except Exception:
                os.kill(target, signal.SIGTERM)
        return True
    except Exception:
        return False


def pid_alive(pid):
    """Is this pid still a live process? Cross-platform, best effort."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
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
