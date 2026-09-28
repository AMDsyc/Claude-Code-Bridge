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

"""One entry point for every Claude Code hook.

Claude Code passes the event as JSON on stdin. This script forwards it to
the daemon and prints whatever the hook protocol needs on stdout.

It exits 0 whatever happens. A bug in the bridge should degrade the loop,
never kill the session you were working in.
"""

import io
import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
URL = "http://127.0.0.1:%d/event" % PORT


def client_pid(prefix="claude", start=None, depth=8):
    """The nearest ancestor whose executable starts with `prefix` - the
    window this hook belongs to - or 0.

    NOT os.getppid(). A headless `claude -p` runs a hook directly under the
    client, and the first measurement (15.1 step 0) saw only that; a live
    window runs it through a shell, so the parent is a transient bash that
    is gone a second later - measured the day it shipped, one session of a
    watched project reporting four different parents, none of them its
    window. So the walk starts at the parent and goes up to the client;
    finding none answers 0 and the daemon links nothing, which is the
    direction the deaf hold fails in anyway. -> DECISIONS.md 8.29

    Windows reads a toolhelp snapshot - names and parents, nothing
    attached. Elsewhere the parent is the only answer there is.
    """
    try:
        if os.name != "nt":
            return os.getppid()
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
            return 0
        table = {}
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32First(ctypes.c_void_p(h), ctypes.byref(e))
            while ok:
                table[e.th32ProcessID] = (
                    e.th32ParentProcessID,
                    e.szExeFile.decode("mbcs", "replace").lower())
                ok = k.Process32Next(ctypes.c_void_p(h), ctypes.byref(e))
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
        pid = (table.get(start or os.getpid()) or (0, ""))[0]
        for _ in range(depth):
            row = table.get(pid)
            if not row:
                return 0
            if row[1].startswith(prefix):
                return pid
            pid = row[0]
    except Exception:
        return 0
    return 0


def post(payload):
    # A Stop event may block while the planner reviews the report, so it
    # gets a long timeout. Everything else stays snappy.
    timeout = 1500 if payload.get("hook_event_name") == "Stop" else 8
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    # A process the bridge spawned that is not half of the pair - the
    # archive search agent - runs with this set. It is a plain `claude -p`
    # in a folder that happens to sit inside a watched project, so the
    # project's hooks are its hooks; without this it would report a
    # SessionStart, take a seat in the panel and start being watched for
    # silence, for a one-off run that answers a question and exits.
    if os.environ.get("BRIDGE_NO_HOOKS"):
        sys.exit(0)
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8",
                                 errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", write_through=True)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    event["project_dir"] = os.environ.get("CLAUDE_PROJECT_DIR", event.get("cwd", ""))
    event["role"] = (os.environ.get("BRIDGE_ROLE") or "").strip().lower()
    # The window this session lives in - the client process above this
    # hook, found by name (client_pid says why not the parent).
    wp = client_pid()
    if wp:
        event["window_pid"] = wp

    try:
        reply = post(event)
    except Exception:
        # Daemon is down. Say nothing, let the session carry on.
        sys.exit(0)

    out = reply.get("hook_output")
    if out:
        print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
