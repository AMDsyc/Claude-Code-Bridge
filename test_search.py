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

"""Regression suite for the archive search agent.

No real Claude is ever launched here: every case runs against a stub named
`claude` that records the argv and environment it was handed and answers
with whatever the case needs - the fakebin pattern of §12. What is being
tested is the bridge's half of it: what the agent is allowed to do, that a
failure arrives with its reason, that two questions cannot start two
processes, and that the extract lands.

Run:  python test_search.py
"""
import json
import os
import shutil

import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="bridge-search-test-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
os.environ["PYTHONUTF8"] = "1"
# BRIDGE_DATA isolates the bridge's own files and nothing else. The thing
# being tested spawns a client, and a client writes its transcript into its
# OWN store under ~/.claude/projects - which discover.scan() reads, and
# which is how an earlier run of this suite put two folders from a temp
# directory into Max's list of projects. The client's store is pointed
# inside the temp folder too, so that even a real claude reaching this code
# by accident leaves nothing behind.
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-home")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import archive, daemon, store          # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %-4s %s\n       got %r, want %r" % ("ok" if ok else "FAIL",
                                                 name, got, want))
    if not ok:
        FAILED.append(name)


PROJ = os.path.join(TMP, "proj")
LOGS = os.path.join(PROJ, "bridge-logs")
RAW = os.path.join(LOGS, "2026-08-02", "raw")
os.makedirs(RAW, exist_ok=True)
with open(os.path.join(RAW, "sid-1.jsonl"), "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "timestamp": "2026-08-02T09:00:00Z",
                         "message": {"role": "user",
                                     "content": "fix delivery"}}) + "\n")
    fh.write(json.dumps({"type": "assistant",
                         "timestamp": "2026-08-02T09:00:01Z",
                         "message": {"role": "assistant", "model": "m",
                                     "content": [{"type": "text", "text": "k"}],
                                     "usage": {"input_tokens": 5,
                                               "cache_read_input_tokens": 20}}
                         }) + "\n")

BIN = os.path.join(TMP, "fakebin")
os.makedirs(BIN, exist_ok=True)
LOG = os.path.join(TMP, "launches.log")

ANSWER = """Delivery was fixed by answering the HTTP request before writing
to stdout.

## Sources
- `2026-08-02/raw/sid-1.jsonl` - the turn where it was changed, 09:00
"""


def stub(name, body):
    """A fake `claude`, as [interpreter, script].

    Deliberately not a .bat shim: the prompt is tens of kilobytes of
    multi-line markdown, and putting cmd.exe between the bridge and the
    stub re-parses it - the argument arrives truncated at the first
    newline, and the test then fails about the product rather than about
    itself. This is the same list form a machine would configure
    archive_claude with.
    """
    py = os.path.join(BIN, "%s.py" % name)
    with open(py, "w", encoding="utf-8") as fh:
        fh.write(body)
    return [sys.executable, py]


RECORDER = '''
import json, os, sys
row = {"argv": sys.argv[1:], "cwd": os.getcwd(),
       "no_hooks": os.environ.get("BRIDGE_NO_HOOKS"),
       "role": os.environ.get("BRIDGE_ROLE")}
with open(%(log)r, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False) + "\\n")
%(tail)s
'''


def good_stub():
    return stub("claude", RECORDER % {
        "log": LOG,
        "tail": "sys.stdout.write(%r)\nsys.exit(0)" % ANSWER})


def failing_stub():
    return stub("claude-bad", RECORDER % {
        "log": LOG,
        "tail": ("sys.stderr.write('Credit balance is too low')\n"
                 "sys.exit(2)")})


def slow_stub():
    return stub("claude-slow", RECORDER % {
        "log": LOG, "tail": "import time\ntime.sleep(30)"})


def launches():
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


print("\n1. the command line is a headless, read-only, unaffiliated run")
cmd = archive.build_command("why?", "sonnet", "claude")
check("headless, and the question is the prompt", cmd[:3],
      ["claude", "-p", "why?"])
check("a configured list command is taken as given",
      archive.build_command("why?", "sonnet", ["py", "-3", "claude.py"])[:4],
      ["py", "-3", "claude.py", "-p"])
check("only the three reading tools are allowed",
      cmd[cmd.index("--allowedTools") + 1], "Read,Grep,Glob")
denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
for tool in ("Edit", "Write", "Bash"):
    check("%s is denied outright" % tool, tool in denied, True)
check("no MCP configuration reaches it", "--strict-mcp-config" in cmd, True)
check("the model is the one asked for", cmd[cmd.index("--model") + 1],
      "sonnet")
print("   nothing that would make it look like half of the pair")
for flag in ("--dangerously-load-development-channels", "--remote-control",
             "--resume"):
    check("no %s" % flag, flag in cmd, False)

print("\n2. it runs, and the answer becomes an extract with its sources")
rec = archive.search(PROJ, "how was delivery fixed?", model="sonnet",
                     claude=good_stub(), timeout=60)
check("it finished", rec["state"], "done")
check("the answer came back", rec["answer"].splitlines()[0],
      "Delivery was fixed by answering the HTTP request before writing")
check("the file it cited was read out of its own Sources section",
      rec["cited"], ["2026-08-02/raw/sid-1.jsonl"])
check("an extract was written", os.path.exists(rec["extract"]), True)
check("it lives in bridge-logs/extracts",
      os.path.basename(os.path.dirname(rec["extract"])), "extracts")
body = open(rec["extract"], encoding="utf-8").read()
check("the extract opens with the question",
      body.splitlines()[0], "# how was delivery fixed?")
check("and carries the answer", "answering the HTTP request" in body, True)
check("and says which files it cited", "sid-1.jsonl" in body.split("---")[-1],
      True)

print("\n3. the stub was handed the question and the archive to read")
last = launches()[-1]
check("the question is in the prompt", "how was delivery fixed?" in
      last["argv"][1], True)
check("the map is given inline", "MAP.md" in last["argv"][1], True)
check("it runs inside bridge-logs",
      os.path.normcase(last["cwd"]), os.path.normcase(LOGS))
check("hooks are switched off for it", last["no_hooks"], "1")
check("and it carries no role", last["role"], None)

print("\n4. a failure arrives with the reason the process gave (1.6.11)")
notes = []
rec = archive.search(PROJ, "what broke?", claude=failing_stub(), timeout=60,
                     journal=lambda k, t, lvl="log": notes.append((k, t, lvl)))
check("it is marked failed", rec["state"], "failed")
check("the reason is the stub's own words",
      "Credit balance is too low" in rec["error"], True)
check("no extract was written for it", rec["extract"], "")
check("the journal was told, out loud",
      [n for n in notes if n[2] == "sound" and "Credit balance" in n[1]] != [],
      True)
print("   and a command that is not there is not silence either")
rec = archive.search(PROJ, "anything", claude="claude-does-not-exist-xyz",
                     timeout=60)
check("also failed", rec["state"], "failed")
check("naming what could not be run",
      "claude-does-not-exist-xyz" in rec["error"], True)

print("\n5. one at a time - a second question does not start a second run")
slow = slow_stub()
before = len(launches())
rid, why = archive.search_async(PROJ, "the slow one", claude=slow, timeout=2)
check("the first one started", bool(rid), True)
for _ in range(50):                      # let the process actually spawn
    if len(launches()) > before:
        break
    time.sleep(0.1)
rid2, why2 = archive.search_async(PROJ, "the second one", claude=slow)
check("the second one did not", rid2, None)
check("and it was told why, with the run holding the seat",
      "already running" in (why2 or "") and rid in (why2 or ""), True)
check("only one process was ever started", len(launches()) - before, 1)
print("   an empty question is refused before anything is started")
check("no run", archive.search_async(PROJ, "   ")[0], None)
check("with a reason", "no question" in archive.search_async(PROJ, "")[1],
      True)
print("   and the seat is given up when the run ends, however it ends")
for _ in range(200):
    if (archive.get_run(rid) or {}).get("state") != "running":
        break
    time.sleep(0.1)
check("the slow run is over", (archive.get_run(rid) or {})["state"], "failed")
check("nothing is holding the seat now", archive.active_run()[0], None)

print("\n6. a run that overruns is stopped, and says so")
rec = archive.search(PROJ, "the endless one", claude=slow, timeout=1)
check("failed rather than hung", rec["state"], "failed")
check("the limit is named", "past its 1-second limit" in rec["error"], True)
check("nothing is left running", archive.active_run()[0], None)

print("\n7. searching a folder with no archive fails at once, with the reason")
bare = os.path.join(TMP, "bare")
os.makedirs(bare, exist_ok=True)
rec = archive.search(bare, "anything", claude=good_stub())
check("failed", rec["state"], "failed")
check("naming what is missing", "no bridge-logs folder" in rec["error"], True)

print("\n8. /archive-search end to end, on a throwaway daemon")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "session_roles": {}})
daemon.CFG.setdefault("projects", {})[PROJ] = {}
daemon.CFG["archive_model"] = "sonnet"
daemon.CFG["archive_timeout"] = 60
srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(path, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


daemon.CFG["archive_claude"] = good_stub()   # what the endpoint will run
t0 = time.time()
out = post("/archive-search", {"project": PROJ,
                               "question": "when was delivery fixed?"})
check("the endpoint answered at once", time.time() - t0 < 5, True)
check("with a run id", bool(out.get("run_id")), True)
check("and the run is running", out["run"]["state"], "running")
run = None
for _ in range(200):
    run = post("/archive-search", {"run_id": out["run_id"]})["run"]
    if run["state"] != "running":
        break
    time.sleep(0.1)
check("it finished", run["state"], "done")
check("the answer came through the endpoint",
      "answering the HTTP request" in run["answer"], True)
check("an unknown run id is refused, not invented",
      post("/archive-search", {"run_id": "nope"})["ok"], False)
try:
    post("/archive-search", {"project": os.path.join(TMP, "nowhere"),
                             "question": "x"})
    check("a missing project is refused", "not refused", "refused")
except urllib.error.HTTPError as e:
    check("a missing project is refused", e.code, 400)
srv.shutdown()

print("\n9. this suite leaves nothing behind in anybody's project list")
print("   an earlier run spawned a real claude - CreateProcess appends")
print("   only .exe, so the .bat stub on PATH was skipped and the real")
print("   one further down was found - and its transcript became two")
print("   phantom projects called bridge-logs in the panel")
check("the stub is addressed directly, never resolved through PATH",
      isinstance(good_stub(), list), True)
check("and it is an interpreter plus a script",
      os.path.basename(good_stub()[0]).lower().startswith("python"), True)
check("the client's own store is inside the temp folder too",
      os.environ["CLAUDE_CONFIG_DIR"].startswith(TMP), True)
from bridge import discover                                  # noqa: E402
check("and a bridge-logs folder is never offered as a project",
      discover._ours(os.path.join(TMP, "proj", "bridge-logs")), True)
check("nor one nested deeper in it",
      discover._ours(os.path.join(TMP, "proj", "bridge-logs", "2026-08-02")),
      True)
check("while a real project still is",
      discover._ours(r"C:\path\to\project"), False)
check("and a project merely containing the word is not caught",
      discover._ours(r"C:\path\to\my-bridge-logs-viewer"), False)

print("\n" + ("-" * 60))
shutil.rmtree(TMP, ignore_errors=True)
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
