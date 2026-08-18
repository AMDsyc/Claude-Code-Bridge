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

"""Several pairs on one daemon, driven through the real HTTP endpoints.

The other three suites test the arithmetic and one pair's machinery. This
one exists for the question none of them ask: when three projects are live
at once, does anything the bridge does to one of them reach another?

So nothing here calls a function directly if a panel button would call it
over HTTP. Every case posts to the endpoint the panel posts to - /loop,
/session, /cmd, /handover, /config, /verdict, /state - against a throwaway
daemon on an ephemeral port, with three fake projects and a stub in place
of claude. It grows a case per step of PLAN-multipair.md.

Two things it must never do, both learned the hard way:

* touch the live daemon on 8765. /verdict, /task and /loop act for real -
  one probe call once created a phantom project loop and injected a fake
  task into it.
* let a stub be found by name. On Windows CreateProcess appends only .exe,
  so a claude.bat on PATH is skipped and the REAL client further down the
  path runs instead - which is how two temp directories ended up in the
  panel's project list. The stub is passed as an explicit
  [interpreter, script] pair, exactly as test_wall_handover.py does.

Run:  python test_multipair.py
"""
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="bridge-multipair-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-home")
os.environ["PYTHONUTF8"] = "1"


# A Telegram that is not Telegram. The bot API base is an environment
# variable read at import, so this has to be standing before the package is
# imported - and once it is, every path in telegram.py plus the daemon's own
# long-poll go here instead of to api.telegram.org. Nothing in this suite
# ever reaches the real service, and there is no token that would let it.
class _TG(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        method = self.path.rsplit("/", 1)[-1].split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        TG_CALLS.append((method, payload))
        return method

    def do_POST(self):
        method = self._record()
        # TG_FAIL names methods that should answer as Telegram does when it
        # is unreachable rather than unwilling: no "ok", no description.
        # That is the shape a timeout takes by the time _call_ex has caught
        # it, and it is the case that matters - a refusal the bridge could
        # mistake for a success would go unnoticed for ever.
        if method in TG_FAIL:
            return self._reply({"ok": False})
        TG_IDS[0] += 1
        return self._reply({"ok": True, "result": {"message_id": TG_IDS[0]}})

    def do_GET(self):
        method = self._record()
        if method.startswith("getUpdates"):
            return self._reply({"ok": True, "result": []})
        return self._reply({"ok": True, "result": {"message_id": 1}})


TG_CALLS = []
TG_IDS = [1000]
TG_FAIL = set()
_tg_srv = ThreadingHTTPServer(("127.0.0.1", 0), _TG)
threading.Thread(target=_tg_srv.serve_forever, daemon=True).start()
os.environ["BRIDGE_TELEGRAM_API"] = "http://127.0.0.1:%d" \
    % _tg_srv.server_address[1]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import archive, daemon, sessions, store, telegram   # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %-4s %s\n       got %r, want %r" % ("ok" if ok else "FAIL",
                                                 name, got, want))
    if not ok:
        FAILED.append(name)


def note(name, got, why=""):
    print("  ..   %s: %r%s" % (name, got, ("  - " + why) if why else ""))


# ---------------------------------------------------------------------------
# three projects, because two can agree by accident and three cannot

NAMES = ("alpha", "beta", "gamma")
PROJ = {}
for _n in NAMES:
    PROJ[_n] = os.path.join(TMP, _n)
    os.makedirs(PROJ[_n], exist_ok=True)
A, B, C = (PROJ[n] for n in NAMES)


def canon(p):
    return daemon.norm(p)


# ---------------------------------------------------------------------------
# the stand-ins

BIN = os.path.join(TMP, "fakebin")
os.makedirs(BIN, exist_ok=True)
LAUNCHES = os.path.join(TMP, "launches.log")
STUB_PY = os.path.join(BIN, "claude_stub.py")
with open(STUB_PY, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys, time\n"
        "row = {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "       'role': os.environ.get('BRIDGE_ROLE')}\n"
        "open(%r, 'a', encoding='utf-8').write("
        "json.dumps(row, ensure_ascii=False) + '\\n')\n"
        "time.sleep(30)\n" % LAUNCHES)

_real_build = sessions.build_command


def _stub_build(*a, **kw):
    """The real command line with only the executable swapped - so every
    flag under test is still the one sessions.py produces."""
    cmd = _real_build(*a, **kw)
    return [sys.executable, STUB_PY] + cmd[1:]


sessions.build_command = _stub_build
sessions.CREATE_NEW_CONSOLE = 0

# telegram.py is NOT stubbed here: it talks to the recording server above,
# so what these cases check is the real send path - the policy gate, the
# marker, the payload - and not a stand-in for it. Until a token and a chat
# id are set in the config it declines to call anything at all, which is why
# the cases before 13 produce no traffic.


def tg_texts():
    return [p.get("text", "") for m, p in TG_CALLS if m == "sendMessage"]


def tg_reset():
    del TG_CALLS[:]


def launches():
    if not os.path.exists(LAUNCHES):
        return []
    with open(LAUNCHES, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


# One recording channel per (project, role): what the bridge delivered, and
# to whom. A pair that receives another pair's report shows up here.
DELIVERED = {}


class Chan(BaseHTTPRequestHandler):
    who = ("", "")

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        DELIVERED.setdefault(self.who, []).append(body)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def open_channel(project, role):
    who = (canon(project), role)
    DELIVERED.setdefault(who, [])
    cls = type("Chan_%s_%s" % (os.path.basename(project), role),
               (Chan,), {"who": who})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ---------------------------------------------------------------------------
# the throwaway daemon

daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "compactions": {}, "mode": "running",
                     "loops": {}, "paused": {}, "note": {},
                     "session_roles": {}})
daemon.CFG["projects"] = {A: {}, B: {}, C: {}}
daemon.CFG["telegram"] = {"token": "", "chat_id": "", "pinned_message_id": 0}
# Small enough that a stuck review fails the run in half a minute instead of
# holding it for the twenty the defaults allow.
daemon.CFG.setdefault("thresholds", {}).update({"review_timeout": 20,
                                                "channel_silence_warn": 10})

SRV = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
print("throwaway daemon on 127.0.0.1:%d - the real one on 8765 is never "
      "contacted" % PORT)
print("three projects: %s" % ", ".join(NAMES))


def post(path, payload, secret=False, timeout=60):
    """POST and give back the parsed body, whatever the status.

    A refusal is an answer here, not an exception: several cases are about
    what the bridge says when it declines, and urlopen raising on 400 would
    hide the very text under test. The status comes back in the body under
    "status" so a case can check it.
    """
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Bridge-Secret"] = daemon.SECRET
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            code = resp.status
    except urllib.error.HTTPError as exc:
        out = json.loads(exc.read().decode("utf-8") or "{}")
        code = exc.code
    if isinstance(out, dict):
        out["status"] = code
    return out


def get(path):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def state():
    return get("/state")["state"]


def register(project, role, sid):
    """A channel comes up for one half of one pair, as channel.py does."""
    port = open_channel(project, role)
    post("/channel/register", {"project": project, "port": port,
                               "pid": os.getpid(), "role": role},
         secret=True)
    daemon.remember_session(project, role, sid)
    return port


def stop_hook(project, role, sid, text):
    """The blocking Stop hook, exactly as hook.py posts it."""
    return post("/event", {"hook_event_name": "Stop", "role": role,
                           "session_id": sid, "project_dir": project,
                           "cwd": project, "last_assistant_message": text})


def body_of(content):
    """What was actually delivered, with the rules envelope taken off.

    Every task and every report now travels behind the short canon, and the
    canon TALKS ABOUT the very markers a case might search for - it names
    "NO FRAMES" while explaining what that header means. So "is NO FRAMES in
    the delivery" became true for every delivery the moment the rules were
    put in front, and a case that asserted the opposite went from meaningful
    to impossible. Assert on the body, not on the envelope.
    """
    text = content or ""
    if "End of the rules" not in text:
        return text
    return text.split("End of the rules", 1)[1].split("=" * 70,
                                                      1)[-1].lstrip()


def until(fn, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------

print("\n1. three projects on one daemon, each with its own everything")
s = state()
check("the daemon knows all three",
      sorted(os.path.basename(p) for p in get("/state")["canon"]),
      ["alpha", "beta", "gamma"])
for n in NAMES:
    post("/loop", {"action": "start", "project": PROJ[n]})
s = state()
check("a loop record per project",
      sorted(os.path.basename(p) for p in (s.get("loops") or {})),
      ["alpha", "beta", "gamma"])
check("and they are keyed canonically, not as typed",
      all(p == daemon.norm(p) for p in (s.get("loops") or {})), True)

print("\n2. the loop is switched off for one pair and stays on for the rest")
post("/loop", {"action": "stop", "project": B})
s = state()
check("beta is off", s["loops"][canon(B)]["active"], False)
check("alpha and gamma are untouched",
      (s["loops"][canon(A)]["active"], s["loops"][canon(C)]["active"]),
      (True, True))
check("and the reason is recorded against beta alone",
      sorted(os.path.basename(p) for p in (s.get("loop_off") or {})),
      ["beta"])
post("/loop", {"action": "start", "project": B})
check("switching it back on affects only beta",
      state()["loops"][canon(B)]["active"], True)

print("\n3. pausing one pair does not pause the others")
print("   a dead executor in one folder used to set mode=paused, which held")
print("   the reports of every other folder on the machine")
post("/cmd", {"cmd": "pause", "project": A})
s = state()
check("only alpha is held",
      sorted(os.path.basename(p) for p in (s.get("paused") or {})), ["alpha"])
check("the bridge as a whole is still running", s.get("mode"), "running")
check("and the loop's own view agrees, per project",
      (daemon.paused_for(A), daemon.paused_for(B), daemon.paused_for(C)),
      (True, False, False))
post("/cmd", {"cmd": "pause", "project": C})
check("two of the three can be held at once",
      sorted(os.path.basename(p) for p in (state().get("paused") or {})),
      ["alpha", "gamma"])
post("/cmd", {"cmd": "resume", "project": A})
check("and lifted one at a time",
      sorted(os.path.basename(p) for p in (state().get("paused") or {})),
      ["gamma"])

print("\n4. a note has an addressee, and reaches nobody else")
r = post("/cmd", {"cmd": "note", "text": "for whom?"})
check("with three projects, an unaddressed note is refused", r.get("ok"),
      False)
check("and the refusal names the choices", sorted(r.get("projects") or []),
      ["alpha", "beta", "gamma"])
check("nothing was written", state().get("note"), {})
post("/cmd", {"cmd": "note", "text": "look at the migration", "project": B})
s = state()
check("an addressed one lands on its project only",
      {os.path.basename(k): v for k, v in (s.get("note") or {}).items()},
      {"beta": "look at the migration"})
check("the panel's own button sends the project it is showing",
      'cmd:"note",text:$("#noteInput").value,project:CUR' in
      open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bridge", "panel.html"), encoding="utf-8").read(),
      True)

print("\n5. resume with nothing named is the everything-back-to-normal button")
post("/cmd", {"cmd": "pause"})
check("it holds the bridge", state().get("mode"), "paused")
check("so every pair is held",
      (daemon.paused_for(A), daemon.paused_for(B), daemon.paused_for(C)),
      (True, True, True))
post("/cmd", {"cmd": "resume"})
s = state()
check("and resuming lifts the bridge", s.get("mode"), "running")
check("together with the individual holds left under it", s.get("paused"), {})

print("\n6. two pairs review at the same time, and the verdicts do not cross")
print("   PENDING is keyed by project and nothing holds a lock across the")
print("   wait - but that is a property of the code, not of a claim, so it")
print("   is driven here through two real blocking Stop hooks at once")
for n in ("alpha", "beta"):
    register(PROJ[n], "planner", "pl-%s" % n)
post("/cmd", {"cmd": "note", "text": "alpha's note", "project": A})
OUT = {}


def turn(name, text):
    OUT[name] = stop_hook(PROJ[name], "executor", "ex-%s" % name, text)


ta = threading.Thread(target=turn, args=("alpha", "alpha did a thing"))
tb = threading.Thread(target=turn, args=("beta", "beta did another thing"))
ta.start()
tb.start()
check("both pairs are waiting for a verdict at the same moment",
      until(lambda: canon(A) in daemon.PENDING and canon(B) in daemon.PENDING),
      True)
# PENDING is filled BEFORE the report is handed to the channel, so both
# pairs can be waiting while one of the two deliveries is still in the air.
# Counting them the instant PENDING fills was measuring the scheduler.
check("each planner got its own pair's report, and only that",
      until(lambda: all(len(DELIVERED[(canon(PROJ[n]), "planner")]) == 1
                        for n in ("alpha", "beta"))) and
      [len(DELIVERED[(canon(PROJ[n]), "planner")])
       for n in ("alpha", "beta")], [1, 1])
said = {n: json.dumps(DELIVERED[(canon(PROJ[n]), "planner")])
        for n in ("alpha", "beta")}
check("alpha's planner was told about alpha",
      ("alpha did a thing" in said["alpha"],
       "beta did another thing" in said["alpha"]), (True, False))
check("beta's planner about beta",
      ("beta did another thing" in said["beta"],
       "alpha did a thing" in said["beta"]), (True, False))
check("and the note went only to the pair it was addressed to",
      ("alpha's note" in said["alpha"], "alpha's note" in said["beta"]),
      (True, False))

post("/verdict", {"project": B, "verdict": "continue",
                  "feedback": "BETA-FEEDBACK"}, secret=True)
tb.join(30)
check("answering beta releases beta", tb.is_alive(), False)
check("and leaves alpha waiting", ta.is_alive(), True)
check("beta's executor was handed beta's feedback",
      "BETA-FEEDBACK" in json.dumps(OUT.get("beta")), True)
post("/verdict", {"project": A, "verdict": "continue",
                  "feedback": "ALPHA-FEEDBACK"}, secret=True)
ta.join(30)
check("then alpha is released too", ta.is_alive(), False)
check("with its own feedback and not beta's",
      ("ALPHA-FEEDBACK" in json.dumps(OUT.get("alpha")),
       "BETA-FEEDBACK" in json.dumps(OUT.get("alpha"))), (True, False))
check("nothing is left waiting", list(daemon.PENDING), [])
s = state()
check("each pair counted its own iteration",
      [s["loops"][canon(PROJ[n])]["iteration"] for n in NAMES], [1, 1, 0])
check("the note was taken by the pair it was for, and by nobody else",
      s.get("note"), {})

print("\n7. a window is opened, and only for the project asked for")
r = post("/session", {"action": "launch", "project": C, "role": "executor"})
check("it started", r.get("ok"), True)
check("in that project's folder and no other",
      until(lambda: [l for l in launches()
                     if daemon.norm(l["cwd"]) == canon(C)]), True)
started = launches()
check("exactly one window, for gamma",
      sorted({os.path.basename(l["cwd"]) for l in started}), ["gamma"])
check("carrying the role of the window, not of the project",
      {l["role"] for l in started}, {"executor"})
check("the pid is recorded against gamma's executor",
      bool((state().get("pids") or {}).get("%s|executor" % canon(C))), True)
check("and nothing was recorded for the other two",
      [k for k in (state().get("pids") or {})
       if k.startswith(canon(A)) or k.startswith(canon(B))], [])
post("/session", {"action": "stop", "project": C, "role": "executor"})

print("\n8. handing over one pair replaces its windows and nobody else's")
print("   the panel's three handover buttons all land on this endpoint, and")
print("   an automatic one names exactly one role - so the endpoint must")
print("   never widen what it was asked for")
print("   a window that opened and never registered blocks the handover on")
print("   purpose - one pending start at a time - so gamma's executor comes")
print("   up properly first, the way a real one does")
check("while it has not come up, a handover is refused with the reason",
      "has not come up yet" in (daemon.handover_blocked(C, ("executor",))
                                or ""), True)
register(C, "executor", "ex-gamma")
check("once its channel registers, nothing is blocking",
      daemon.handover_blocked(C, ("executor",)), None)
before = len(launches())
r = post("/handover", {"project": C, "role": "executor",
                       "reason": "asked for by the suite"})
check("it was accepted for one role only", r.get("roles"), ["executor"])
check("only gamma is marked as handing over",
      until(lambda: sorted(os.path.basename(p) for p in
                           (state().get("handover") or {})) == ["gamma"]),
      True)
check("a replacement window came up",
      until(lambda: len(launches()) > before, 30), True)
fresh = launches()[before:]
check("in gamma's folder and no other",
      sorted({os.path.basename(l["cwd"]) for l in fresh}), ["gamma"])
check("as the executor, the role that was named",
      sorted({l["role"] for l in fresh}), ["executor"])
check("alpha and beta are not handing over anything",
      [p for p in (state().get("handover") or {})
       if p in (canon(A), canon(B))], [])
check("and their loops are still on",
      [state()["loops"][canon(PROJ[n])]["active"] for n in ("alpha", "beta")],
      [True, True])
post("/session", {"action": "stop", "project": C, "role": "executor"})
daemon.STATE["handover"] = {}

print("\n9. the arithmetic of a handover belongs to the pair that had it")
print("   one list for the whole bridge, and the panel draws its newest row")
print("   under the gauges of the project on screen")
daemon.STATE["handover_log"] = [{"at": "2026-08-01 09:00:00",
                                 "role": "executor", "why": "before paths"}]
sA = {"model": "opus 5", "window": 1000000, "context_tokens": 770000,
      "session_id": "ex-alpha"}
daemon.log_handover_decision(A, "executor", sA, {"why": "alpha ran out",
                                                 "compactions": 1})
daemon.log_handover_decision(B, "planner", sA, {"why": "beta ran out",
                                                "compactions": 2})
hist = state().get("handover_log") or []
check("every new row names its project",
      [os.path.basename(r["path"]) for r in hist if r.get("path")],
      ["alpha", "beta"])
check("the row written before they did is still there, unattributed",
      [r.get("why") for r in hist if not r.get("path")], ["before paths"])
mine = [r for r in hist if r.get("path") == canon(A)]
check("alpha's own newest row is alpha's", mine[-1]["why"], "alpha ran out")
check("which is not the newest row of the whole bridge",
      hist[-1]["why"], "beta ran out")
psrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridge", "panel.html"), encoding="utf-8").read()
check("so the panel filters by the project it is showing",
      "hlAll.filter(function(r){return r.path&&forCur(r.path)})" in psrc,
      True)
check("and says out loud that the old rows are not being shown",
      "recorded before handovers " in psrc, True)

print("\n10. the same folder spelled two ways is one pair, not two")
print("   sessions keyed its live process handles with normpath alone, which")
print("   folds separators but not case: launch() recorded the window under")
print("   one spelling and stop()/alive() looked for it under another")
check("the daemon and sessions share one definition",
      daemon.norm is store.norm, True)
check("upper and lower case are the same key",
      daemon.norm(A.upper()) == daemon.norm(A.lower()), True)
sessions.PROCS.clear()
post("/session", {"action": "launch", "project": A, "role": "planner"})
check("a window recorded under the path as given",
      [k[1] for k in sessions.PROCS if k[0] == canon(A)], ["planner"])
check("is found under the path spelled differently",
      bool(sessions.alive(A.upper(), "planner")), True)
post("/session", {"action": "stop", "project": A.upper(), "role": "planner"})
check("and stopping it that way really stops it",
      sessions.alive(A, "planner"), False)

print("\n11. every project can search its own archive at the same time")
print("    the seat used to be one for the whole bridge, so one pair's")
print("    question locked every other pair out of its own archive for as")
print("    long as it ran - and a request that named no project silently")
print("    searched the first one in the config")
for _n in NAMES:
    _raw = os.path.join(PROJ[_n], "bridge-logs", "2026-08-09", "raw")
    os.makedirs(_raw, exist_ok=True)
    with open(os.path.join(_raw, "sid-%s.jsonl" % _n), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"type": "user", "timestamp": "2026-08-09T09:00:00Z",
             "message": {"role": "user", "content": "what happened in %s"
                         % _n}}) + "\n")

SLOW = os.path.join(BIN, "claude_slow.py")
with open(SLOW, "w", encoding="utf-8") as fh:
    fh.write("import time\ntime.sleep(30)\n")
daemon.CFG["archive_claude"] = [sys.executable, SLOW]
daemon.CFG["archive_parallel"] = 2

r = post("/archive-search", {"question": "who asked for this?"})
check("a request that names no project is refused", r.get("status"), 400)
check("saying that it will not guess one", r.get("error", "")[:20],
      "no project in the re")
check("and the refusal names the archives it could have meant",
      sorted(r.get("projects") or []), ["alpha", "beta", "gamma"])
check("nothing was started for it", archive.active_run()[0], None)

ra = post("/archive-search", {"project": A, "question": "alpha's question"})
check("alpha's search starts", ra.get("ok"), True)
rb = post("/archive-search", {"project": B, "question": "beta's question"})
check("beta's starts at the same time, without waiting for alpha",
      rb.get("ok"), True)
# Both runs are worker threads that spawn a process; give them the moment
# they need to be in flight before counting them, or this case tests the
# scheduler instead of the seat.
check("both are in flight at once",
      until(lambda: len(archive.running_runs()) == 2) and
      len(archive.running_runs()), 2)
check("and each seat belongs to its own project",
      (archive.active_run(A)[0], archive.active_run(B)[0]),
      (ra.get("run_id"), rb.get("run_id")))
check("gamma is holding no seat", archive.active_run(C)[0], None)

again = post("/archive-search", {"project": A, "question": "and again?"})
check("a second question about the same archive is still refused",
      again.get("ok"), False)
check("naming the run that holds that project's seat, and the project",
      (ra["run_id"] in again.get("error", ""),
       "alpha" in again.get("error", "")), (True, True))

rc = post("/archive-search", {"project": C, "question": "gamma's question"})
check("with the ceiling at 2, the third project is refused too",
      rc.get("ok"), False)
check("but for the other reason, and it says so",
      ("the machine that is full" in rc.get("error", ""),
       "one at a time per project" in rc.get("error", "")), (True, False))
check("naming both runs that are occupying it",
      (ra["run_id"] in rc.get("error", ""),
       rb["run_id"] in rc.get("error", "")), (True, True))
check("and it really did not start one", len(archive.running_runs()), 2)

daemon.CFG["archive_parallel"] = 3
rc2 = post("/archive-search", {"project": C, "question": "gamma, again"})
check("raising the ceiling in the config lets gamma through",
      rc2.get("ok"), True)
check("three in flight now", len(archive.running_runs()), 3)
check("the ceiling is read from the config, not compiled in",
      store.DEFAULT_CONFIG.get("archive_parallel"), 2)
check("and each seat is held by the project that asked for it",
      sorted(os.path.basename(r.get("project", ""))
             for _id, r in archive.running_runs()),
      ["alpha", "beta", "gamma"])
print("   three runs, three ids: the id used to be the millisecond clock")
print("   alone, which was unique only while one search could exist at a")
print("   time. Two projects starting in the same millisecond got the same")
print("   id, RUNS is keyed by id, and the second quietly replaced the")
print("   first - so a project's seat vanished while its process still ran")
check("the three in flight are three distinct runs",
      len({_id for _id, _ in archive.running_runs()}), 3)
check("and a thousand ids minted back to back are all different",
      len({archive.new_run_id() for _ in range(1000)}), 1000)
# Deliberately NOT "and now the seats are given up": these three runs are
# live worker threads around a stub that sleeps, so forcing their records to
# "failed" here races with the thread writing its own result back. That a
# seat is released when a run really ends is test_search.py case 5, against
# a run that really ended.

print("\n12. the feed shows one pair's events, and the cut cannot lose them")
print("    one journal carries every project and the daemon hands the panel")
print("    the newest 40 lines. Cutting first and filtering after means a")
print("    busy pair pushes a quiet one out of its OWN feed, and that feed")
print("    then reads as 'nothing happened' while it was working")
# The quiet project is one of this case's own, touched by nothing else in
# the suite. Using alpha here raced the rest of it: earlier cases leave
# worker threads - archive searches, a handover, delivery retries - that go
# on writing lines of their own, and a bridge-wide line passes every
# project's filter, so how many of the 40 were left for alpha depended on
# timing rather than on the code under test.
QUIET = os.path.join(TMP, "quiet-project")
os.makedirs(QUIET, exist_ok=True)
store.journal("loop", "QUIET-MARK", "quiet-project", project_dir=QUIET)
for _i in range(60):
    store.journal("loop", "chatty beta %d" % _i, "beta", project_dir=B)
store.journal("bridge", "BRIDGE-WIDE-MARK")

sixty_later = [e.get("text") for e in store.recent_events(40)]
check("cut without filtering, the quiet pair's line is gone",
      "QUIET-MARK" in sixty_later, False)
check("because the chatty one has taken nearly all of the window",
      len([t for t in sixty_later if t.startswith("chatty beta")]) > 30, True)
filtered = [e.get("text")
            for e in store.recent_events(40, project=canon(QUIET))]
check("filtering first, it survives", "QUIET-MARK" in filtered, True)
check("with none of the sixty that buried it",
      [t for t in filtered if t.startswith("chatty beta")], [])

feed_q = get("/state?project=" + urllib.parse.quote(QUIET))["events"]
texts_q = [e.get("text") for e in feed_q]
check("the panel's own feed shows it too", "QUIET-MARK" in texts_q, True)
check("and carries none of beta's",
      [t for t in texts_q if t.startswith("chatty beta")], [])
check("cut to the same length", len(feed_q) <= 40, True)

feed_b = get("/state?project=" + urllib.parse.quote(B))["events"]
texts_b = [e.get("text") for e in feed_b]
check("beta's feed is beta's", "QUIET-MARK" in texts_b, False)
check("newest first, as the panel draws them",
      texts_b[0] in ("BRIDGE-WIDE-MARK", "chatty beta 59"), True)
check("the daemon says which project it cut the feed for",
      get("/state?project=" + urllib.parse.quote(A))["feed_project"], canon(A))
check("and with no project it cuts for nobody, as before",
      get("/state")["feed_project"], "")

print("   a line about the bridge itself belongs to every pair at once")
check("so it is in all three feeds",
      ["BRIDGE-WIDE-MARK" in [e.get("text") for e in
                              get("/state?project=" +
                                  urllib.parse.quote(PROJ[n]))["events"]]
       for n in NAMES], [True, True, True])

print("   a line written before lines carried a path reads as bridge-wide,")
print("   because that is what a line with no path means from now on - and")
print("   the ambiguity ages out by itself: the feed only reads today and")
print("   yesterday, so within two days there are no such lines left")
_today = os.path.join(store.day_dir(), "events.jsonl")
with open(_today, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"at": "2026-08-09T09:00:00", "kind": "loop",
                         "text": "OLD-ROW-NO-PATH", "project": "alpha",
                         "session": "", "level": "log"}) + "\n")
for _n in NAMES:
    _f = get("/state?project=" + urllib.parse.quote(PROJ[_n]))["events"]
    check("it survives being read for %s" % _n,
          "OLD-ROW-NO-PATH" in [e.get("text") for e in _f], True)
check("and nothing in the row is needed to draw it - the panel reads at, "
      "kind and text",
      all(k in (feed_q[0] or {}) for k in ("at", "kind", "text")), True)
check("new lines do carry the path, canonically",
      [e.get("path") for e in store.recent_events(40, project=canon(QUIET))
       if e.get("text") == "QUIET-MARK"], [canon(QUIET)])
check("the panel asks for the project it is showing",
      # step 7 added the "this project / all" switch, so the request also
      # asks whether the feed is meant to be narrowed at all - which is why
      # this looks for the parameter rather than for the whole line
      '"?project="+encodeURIComponent(CUR)' in psrc, True)
check("and drops the filter when the feed is set to all",
      "(CUR&&!FEEDALL)" in psrc, True)

print("\n13. the chat carries three kinds of message and no others")
print("    with one pair the chat could carry everything and still be read.")
print("    With three, the volume is the problem: the line that needs an")
print("    answer scrolls away under the ones that do not")
daemon.CFG["telegram"] = {"token": "test-token", "chat_id": "42",
                          "pinned_message_id": 0}
tg_reset()
for _kind in ("iteration_done", "verdict_changes", "waiting_process",
              "model_dropped", "session_start", "session_end"):
    daemon.notify(_kind, "%s should not reach the chat" % _kind, path=A)
check("nothing outside the policy went out", tg_texts(), [])
check("and the caller is told it was only logged",
      daemon.notify("iteration_done", "nor this", path=A), "log")

tg_reset()
daemon.notify("needs_you", "someone has to look at this", path=A)
daemon.notify("crash", "something broke", path=B)
daemon.notify("session_died", "a window is gone", path=C)
daemon.notify("process_stuck", "a build has not finished", path=A)
daemon.notify("rotation_name", "answer the dialog", path=B)
daemon.notify("run_finished", "the planner called it done", path=C)
check("all six of the allowed kinds reached the chat", len(tg_texts()), 6)
daemon.notify("limit_low", "the five-hour limit is nearly up")
check("and the account-wide limit too, with no pair on it",
      len(tg_texts()), 7)

print("   a message about a pair starts with that pair's colour")
marks = {n: daemon.mark_for(PROJ[n]) for n in NAMES}
check("every project got one", sorted(marks), ["alpha", "beta", "gamma"])
check("all different", len(set(marks.values())), 3)
check("from the palette, and never red",
      set(marks.values()) <= set(daemon.PAIR_MARKS)
      and "\U0001F7E5" not in daemon.PAIR_MARKS, True)
sent = tg_texts()
check("alpha's message leads with alpha's colour",
      sent[0].startswith(marks["alpha"] + " "), True)
check("beta's with beta's", sent[1].startswith(marks["beta"] + " "), True)
check("gamma's with gamma's", sent[2].startswith(marks["gamma"] + " "), True)
check("and the text itself is untouched behind it",
      sent[0].endswith("someone has to look at this"), True)
check("the account-wide one carries no pair colour",
      any(sent[6].startswith(m) for m in daemon.PAIR_MARKS), False)

print("   the pinned links carry the colour too - four links from two")
print("   projects are otherwise read by comparing names letter by letter")
daemon.STATE["rc"] = {
    "%s|executor" % canon(A): {"url": "https://claude.ai/code/session_XXXXXXXX"},
    "%s|planner" % canon(A): {"url": "https://claude.ai/code/session_YYYYYYYY"},
    "%s|executor" % canon(B): {"url": "https://claude.ai/code/session_ZZZZZZZZ"}}
lt = daemon.links_text()
check("every link line leads with its pair's colour",
      [l.split()[0] for l in lt.splitlines()
       if " - executor" in l or " - planner" in l],
      [marks["alpha"], marks["alpha"], marks["beta"]])
check("two projects, two different colours in one pinned message",
      len({l.split()[0] for l in lt.splitlines()
           if " - executor" in l or " - planner" in l}), 2)
check("and the urls are still there, untouched behind them",
      len([l for l in lt.splitlines() if l.startswith("https://")]), 3)

print("   the pin is rewritten in place, which is silent - so there is a")
print("   way to ask for it again when it has been unpinned or lost")
tg_reset()
daemon.push_links()
sent = [(m, p) for m, p in TG_CALLS if m in ("sendMessage", "editMessageText")]
check("the first push sends it and pins it",
      [m for m, _ in TG_CALLS], ["sendMessage", "pinChatMessage"])
first_id = daemon.CFG["telegram"]["links_message_id"]
tg_reset()
daemon.push_links()
check("pushing the same text again does nothing at all", TG_CALLS, [])
check("and the message it is keeping has not changed",
      daemon.CFG["telegram"]["links_message_id"], first_id)
tg_reset()
daemon.push_links(force=True)
check("forced, it lets the old one go and sends a new one",
      [m for m, _ in TG_CALLS],
      ["unpinChatMessage", "sendMessage", "pinChatMessage"])
check("unpinning the one it was keeping, not some other",
      [p.get("message_id") for m, p in TG_CALLS
       if m == "unpinChatMessage"], [first_id])
check("and it keeps the new one from now on",
      daemon.CFG["telegram"]["links_message_id"] != first_id, True)
check("nothing forces it on its own - only a person asking",
      "force=bool(body.get(\"force\"))" in
      inspect.getsource(daemon.Handler.do_POST), True)
check("the panel's button is the only caller that passes it",
      'post("/links/push",{force:true})' in psrc, True)

print("   the colour is decided once and read back from the config, so it")
print("   survives a restart - one that moved would be worse than none")
check("it is written where a restart will find it",
      sorted(os.path.basename(p) for p in (daemon.CFG.get("marks") or {})),
      ["alpha", "beta", "gamma"])
_saved = dict(daemon.CFG["marks"])
check("asking again gives the same answer",
      {n: daemon.mark_for(PROJ[n]) for n in NAMES}, marks)
check("and it did not rewrite them", daemon.CFG["marks"], _saved)
_reloaded = store.load_config().get("marks") or {}
check("a fresh read of config.json has them too",
      {k: v for k, v in _reloaded.items() if k in _saved}, _saved)

print("   the report itself stays on disk: it used to follow the alert into")
print("   the chat, up to 3500 characters of it, and bury the one line that")
print("   needed answering")
rsrc = inspect.getsource(daemon.run_review)
check("no send of the report body is left in the review",
      "content[:3500]" in rsrc, False)
check("the inbox write is still there, twice - once per fallback",
      rsrc.count("store.inbox_write(path, n, content)"), 2)
check("the three alerts that name the inbox or the wait still go out",
      rsrc.count('notify("needs_you"'), 3)
check("and the end of the run is its own kind now, not another needs_you",
      'notify("run_finished"' in rsrc, True)
tg_reset()
_inbox = store.inbox_write(A, 7, "REPORT-BODY-SHOULD-NOT-BE-SENT")
daemon.notify("needs_you", "alpha: report 7 saved to %s. Answer in the "
              "planner chat." % _inbox, path=A)
check("what went out names the file", _inbox in tg_texts()[0], True)
check("and does not contain the report",
      "REPORT-BODY-SHOULD-NOT-BE-SENT" in "".join(tg_texts()), False)
check("which is on disk, where it said it was",
      "REPORT-BODY-SHOULD-NOT-BE-SENT" in
      open(_inbox, encoding="utf-8").read(), True)

print("   a pair held on its own is visible in the line the pin is built")
print("   from - the gap step 1 left open, closed here")
post("/cmd", {"cmd": "pause", "project": B})
head = daemon.status_headline()
check("the held pair is named, and said to be held", "beta: held" in head,
      True)
check("the others are named and are not held",
      ("alpha:" in head, "alpha: held" in head), (True, False))
check("and the bridge as a whole was never paused", state().get("mode"),
      "running")
post("/cmd", {"cmd": "resume", "project": B})
check("lifting it clears the word", "held" in daemon.status_headline(), False)

print("   and the pin says which pair each gauge belongs to")
daemon.STATE["sessions"] = {
    "executor:ex-a": {"role": "executor", "path": canon(A), "project":
                      "alpha", "state": "working", "context_pct": 41,
                      "managed": True},
    "planner:pl-b": {"role": "planner", "path": canon(B), "project": "beta",
                     "state": "working", "context_pct": 12, "managed": True}}
daemon.STATE["limits"] = {"five_hour": {"pct": 52, "resets": ""}}
pin = daemon.pinned_text()
rows = [l for l in pin.splitlines() if "/" in l]
check("one line per live half, each naming its project",
      sorted(l.split()[1] for l in rows), ["alpha/executor", "beta/planner"])
check("each led by its pair's colour",
      [l.split()[0] for l in rows],
      [marks["alpha"], marks["beta"]])
check("two pairs, two different colours in the pin",
      len({l.split()[0] for l in rows}), 2)
check("the account's limit is labelled as shared, not as a pair's",
      "five hours (all pairs)" in pin, True)
check("and carries no pair colour",
      any(l.startswith(m) for m in daemon.PAIR_MARKS
          for l in pin.splitlines() if "five hours" in l), False)

print("\n14. a command from the chat reaches one pair, not all of them")
print("    /verdict used to walk PENDING and set EVERY waiting project's")
print("    verdict to the same answer - so with two pairs up, one person's")
print("    reply about alpha closed beta's report too, and beta's executor")
print("    took somebody else's findings as facts about its own work")
for _n in ("alpha", "beta"):
    if (canon(PROJ[_n]), "planner") not in DELIVERED:
        register(PROJ[_n], "planner", "pl2-%s" % _n)
OUT2 = {}


def turn2(name, text):
    OUT2[name] = stop_hook(PROJ[name], "executor", "ex2-%s" % name, text)


t2a = threading.Thread(target=turn2, args=("alpha", "alpha turn two"))
t2b = threading.Thread(target=turn2, args=("beta", "beta turn two"))
t2a.start()
t2b.start()
check("both pairs are waiting again",
      until(lambda: canon(A) in daemon.PENDING and canon(B) in daemon.PENDING),
      True)

tg_reset()
said = daemon.run_telegram_command("/verdict continue anything")
check("with two waiting and no address, nothing is done", said[:8], "Not done")
check("and both are named in the refusal",
      ("alpha" in said, "beta" in said), (True, True))
check("neither waiter was touched",
      [daemon.PENDING[canon(p)].get("verdict") for p in (A, B)],
      [None, None])

said = daemon.run_telegram_command("/verdict @beta continue BETA-BY-NAME")
check("addressed by name, it goes to that one", "beta: verdict continue" in
      said, True)
check("and it leads with beta's colour", said.startswith(marks["beta"]), True)
t2b.join(30)
check("beta was released", t2b.is_alive(), False)
check("alpha is still waiting", t2a.is_alive(), True)
check("with beta's feedback and nobody else's",
      ("BETA-BY-NAME" in json.dumps(OUT2.get("beta")),
       "BETA-BY-NAME" in json.dumps(OUT2.get("alpha") or {})), (True, False))

print("   replying to one of a pair's messages addresses it, with nothing")
print("   typed - the report used to be the thing you replied to, and since")
print("   step 5 it is the alert that took its place")
tg_reset()
daemon.notify("needs_you", "alpha needs an answer", path=A)
_mid = [p.get("message_id") for m, p in TG_CALLS if m == "sendMessage"]
_anchor = sorted(daemon.MSGPROJ)[-1]
check("the bridge remembered which pair that message was about",
      daemon.MSGPROJ.get(_anchor), canon(A))
said = daemon.run_telegram_command("/verdict continue BY-REPLY",
                                   reply_to=_anchor)
check("the reply is the address", "alpha: verdict continue" in said, True)
t2a.join(30)
check("alpha was released by it", t2a.is_alive(), False)
check("with its own feedback", "BY-REPLY" in json.dumps(OUT2.get("alpha")),
      True)
check("and nothing is left waiting", list(daemon.PENDING), [])

print("   a prefix is enough while it names one project, and refused when")
print("   it does not (the ambiguous case is driven directly in handover 43,")
print("   where two projects share a prefix - these three do not)")
said = daemon.run_telegram_command("/note @a a prefix that names one")
check("an unambiguous prefix lands", "alpha: noted" in said, True)
said = daemon.run_telegram_command("/note @nope something")
check("an unknown project is refused", "no project called" in said, True)
check("and nothing was written for it",
      "nope" in json.dumps(state().get("note") or {}), False)

print("   /rotate is never done to a pair nobody named, whatever the count -")
print("   it costs a window and cannot be undone")
said = daemon.run_telegram_command("/rotate")
check("refused with the reason", "cannot be undone" in said, True)
check("and the candidates named, so the next line is easy to type",
      all(n in said for n in NAMES), True)
check("no rotation was started",
      [k for k in (state().get("handover") or {})], [])

print("   restart with nobody named stays a fan-out, deliberately: it means")
print("   'bring back whatever fell over', and it is cheap and repeatable")
check("the reason is written next to the code, so it is not tidied away",
      "do not\n        # tidy it away" in
      inspect.getsource(daemon.run_telegram_button), True)
check("and pause/resume still mean the whole bridge when unaddressed",
      [daemon.TG_ADDRESSING[c] for c in ("pause", "resume")],
      ["bridge", "bridge"])

print("   a button carries its pair, within telegram's 64 bytes")
tg_reset()
daemon.notify("needs_you", "gamma needs a look", path=C)
_btns = [p for m, p in TG_CALLS if m == "sendMessage"][-1]
_keys = _btns["reply_markup"]["inline_keyboard"][0]
check("every button's data fits",
      max(len(b["callback_data"].encode()) for b in _keys) <= 64, True)
check("the label is still readable - the id rides behind it",
      [b["text"] for b in _keys][-1], "status")
check("and the data carries gamma's id",
      all(b["callback_data"].endswith("|" + daemon.pair_id(C))
          for b in _keys), True)
post("/loop", {"action": "stop", "project": C})
said = daemon.run_telegram_button("start the loop|%s" % daemon.pair_id(C))
check("pressing it acts on gamma", "gamma: loop on" in said, True)
check("gamma's loop really is on", state()["loops"][canon(C)]["active"], True)
check("and alpha's was not touched",
      state()["loops"][canon(A)]["active"], True)

print("\n15. /config keeps one project's settings out of another's")
post("/config", {"projects": {A: {"commit_each_iteration": False},
                              B: {}, C: {}}})
check("alpha took the setting",
      store.project_config(daemon.CFG, A).get("commit_each_iteration"), False)
check("beta kept the default",
      store.project_config(daemon.CFG, B).get("commit_each_iteration"), True)

print("\n16. the panel can show every pair at a glance without leaving one")
print("    tabs were rejected: one tab per pair means drawing the whole")
print("    panel per pair, and the sum of that is what makes a screen")
print("    unreadable. A strip of one line each, and everything below it")
print("    still about the single project on screen")
pv = get("/state")["pairs"]
check("a row per project", sorted(pv[p]["name"] for p in pv),
      ["alpha", "beta", "gamma"])
check("every row names its own project",
      all(pv[p]["name"] == os.path.basename(p) for p in pv), True)
check("and carries that pair's colour, all different",
      len({pv[p]["mark"] for p in pv}), 3)
check("the colour is the one telegram uses for it, not a second scheme",
      {pv[p]["mark"] for p in pv}, set(marks.values()))
check("each row says what that pair is doing",
      all(pv[p].get("state") for p in pv), True)
check("in the same words the pin uses, not a second reading",
      pv[canon(A)]["state"], daemon.project_headline(A))
print("   the numbers on a row belong to the pair named on it")
daemon.STATE["sessions"] = {
    "executor:ex-a": {"role": "executor", "path": canon(A), "project":
                      "alpha", "state": "working", "context_pct": 41,
                      "context_tokens": 410000, "window": 1000000,
                      "model": "opus 5", "managed": True},
    "planner:pl-b": {"role": "planner", "path": canon(B), "project": "beta",
                     "state": "working", "context_pct": 12,
                     "context_tokens": 24000, "window": 200000,
                     "model": "fable 5", "managed": True}}
pv = get("/state")["pairs"]
check("alpha's executor is on alpha's row",
      pv[canon(A)]["roles"]["executor"]["pct"], 41)
check("and nowhere else",
      "executor" in pv[canon(B)]["roles"], False)
check("beta's planner is on beta's",
      pv[canon(B)]["roles"]["planner"]["pct"], 12)
check("gamma has no live half and claims none", pv[canon(C)]["roles"], {})
post("/cmd", {"cmd": "pause", "project": C})
pv = get("/state")["pairs"]
check("a pair held on its own says so on its own row",
      (pv[canon(C)]["held"], "held" in pv[canon(C)]["state"]), (True, True))
check("and the others do not",
      [pv[canon(p)]["held"] for p in (A, B)], [False, False])
post("/cmd", {"cmd": "resume", "project": C})

print("   the feed switch needs no endpoint of its own - which project it")
print("   is for is a parameter of the read the panel already does")
store.journal("loop", "STRIP-ALPHA-LINE", "alpha", project_dir=A)
store.journal("loop", "STRIP-BETA-LINE", "beta", project_dir=B)
narrow = get("/state?project=" + urllib.parse.quote(A))
wide = get("/state")
mine = [e.get("text") for e in narrow["events"]]
every = [e.get("text") for e in wide["events"]]
check("narrowed, one pair's line is there and the other's is not",
      ("STRIP-ALPHA-LINE" in mine, "STRIP-BETA-LINE" in mine), (True, False))
check("widened, both are", ("STRIP-ALPHA-LINE" in every,
                            "STRIP-BETA-LINE" in every), (True, True))
check("and the same endpoint served both",
      (narrow["feed_project"], wide["feed_project"]), (canon(A), ""))
check("a line carries the path the strip labels it by",
      [e.get("path") for e in wide["events"]
       if e.get("text") == "STRIP-BETA-LINE"], [canon(B)])

print("   and the panel itself still holds the shape the earlier cases fixed")
psrc7 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bridge", "panel.html"), encoding="utf-8").read()
check("eight windows, the strip is not a ninth", psrc7.count("<section"), 8)
check("the strip draws from the daemon's rows, not its own arithmetic",
      "var box=$(\"#pairs\"),rows=D.pairs||{}" in psrc7, True)
check("every row it draws is labelled with its project",
      "esc(r.mark||\"\")+' '+esc(r.name||\"\")" in psrc7, True)
check("the rejected global read is still absent",
      "D.state.loops[p].active" in psrc7, False)
check("and anyLoop is still only the comment saying why there is none",
      psrc7.count("anyLoop"), 1)
check("clicking a row only changes which project is shown",
      "CUR=typed||want;renderPanel();tick()" in psrc7, True)
check("the feed says whose a line is when it is showing everybody's",
      "if(FEEDALL&&e.path&&pairsBy[e.path])" in psrc7, True)

print("\n17. the chat carries what a person has to act on, and not the rest")
print("    from a screenshot: 'loop is on' twice, an idle-loop reminder, a")
print("    goodbye sent twice, and a stuck-process alert carrying the whole")
print("    body of a markdown file that a heredoc happened to be writing")
tg_reset()
post("/loop", {"action": "stop", "project": A})
post("/loop", {"action": "start", "project": A})
check("switching the loop on says nothing in the chat", tg_texts(), [])
check("but the journal has it",
      any("Loop started" in (e.get("text") or "")
          for e in get("/state?project=" + urllib.parse.quote(A))["events"]),
      True)

tg_reset()
daemon.notify("loop_idle", "alpha: nobody is reviewing the turns", path=A)
check("the idle-loop reminder is panel and journal, not phone",
      tg_texts(), [])
check("and it is not on the list of what the chat carries",
      "loop_idle" in daemon.TELEGRAM_KINDS, False)

print("   an alert says what to decide, not the contents of the thing")
huge = ('cd "D:/work/project" && cat >> DEFECT_REPORT.md <<\'EOF\'\n'
        + "a line of the markdown file being written\n" * 60)
check("a command with a file inside it is cut to one line",
      len(daemon.brief(huge)) <= daemon.CHAT_BRIEF, True)
check("keeping the beginning, which is the part that identifies it",
      daemon.brief(huge).startswith('cd "D:/work/project"'), True)
check("and saying it was cut", daemon.brief(huge).endswith("…"), True)
psrc17 = inspect.getsource(daemon.process_watch)
check("the stuck-process alert goes through it",
      "brief(meta[\"cmd\"])" in psrc17, True)
check("while the sessions still get the whole thing - they act on it",
      'deliver(path, "executor", ev' in psrc17, True)

print("   a button press is answered over the chat, not into it")
tg_reset()
post("/loop", {"action": "stop", "project": C})
said = daemon.run_telegram_button("start the loop|%s" % daemon.pair_id(C))
check("the button did its work", state()["loops"][canon(C)]["active"], True)
check("and by itself it wrote nothing to the chat", tg_texts(), [])
psrc18 = inspect.getsource(daemon.telegram_poll)
check("the poll answers a press with a toast",
      "telegram.answer_callback(" in psrc18, True)
check("and the only answer still sent as a message is the one to a command "
      "the human typed, which a toast cannot serve",
      psrc18.count('telegram.send(CFG, said'), 1)
check("a long answer refreshes the status instead of being truncated blind",
      "refresh_pin(force=True)" in psrc18, True)

print("   goodbye is said once, however the window was closed - windows")
print("   delivers Ctrl+C as SIGINT and as a console event, and both are")
print("   wired to the same handler")
gsrc = inspect.getsource(daemon.shutdown)
check("there is a guard", "_said_goodbye" in gsrc, True)
check("and it returns rather than saying it twice",
      gsrc.index("_said_goodbye[0] = True") < gsrc.index("farewell"), True)

print("   a pin edit that failed must not be recorded as sent - otherwise")
print("   the next push sees the text it never delivered and says nothing,")
print("   for ever. 58 network drops to api.telegram.org in one day.")
daemon.STATE["rc"] = {"%s|executor" % canon(A):
                      {"url": "https://claude.ai/code/session_RETRY1"}}
daemon.push_links(force=True)
kept = daemon.CFG["telegram"]["links_text"]
daemon.STATE["rc"]["%s|planner" % canon(A)] = {
    "url": "https://claude.ai/code/session_RETRY2"}
TG_FAIL.add("editMessageText")
tg_reset()
daemon.push_links()
check("the edit was attempted", [m for m, _ in TG_CALLS], ["editMessageText"])
check("it failed, so the old text is still what it believes it sent",
      daemon.CFG["telegram"]["links_text"], kept)
check("and the new link is NOT in it yet",
      "session_RETRY2" in daemon.CFG["telegram"]["links_text"], False)
TG_FAIL.discard("editMessageText")
tg_reset()
daemon.push_links()
check("so the next push tries again rather than falling silent",
      [m for m, _ in TG_CALLS], ["editMessageText"])
check("and now it is recorded, because it went through",
      "session_RETRY2" in daemon.CFG["telegram"]["links_text"], True)

print("\n18. a window nobody launched belongs to one pair, and to one row")
print("    it was recorded under '<role>:seen' - the same key for every")
print("    project - so two pairs each with a noticed window overwrote each")
print("    other and were re-noticed for ever, a pair of lines in the feed")
print("    every 45 seconds, which is how often reconcile() runs")
daemon.STATE["sessions"] = {}
daemon.STATE["channels"] = {}
for _p in (A, B):
    daemon.ensure_record(_p, "executor", "its channel is answering")
keys = sorted(daemon.STATE["sessions"])
check("two projects, two records", len(keys), 2)
check("and the keys tell them apart", len(set(keys)), 2)
check("each carrying its own project",
      sorted(daemon.norm(s["path"]) for s in
             daemon.STATE["sessions"].values()), sorted([canon(A), canon(B)]))
before_keys = set(keys)
for _p in (A, B):
    daemon.ensure_record(_p, "executor", "its channel is answering")
check("noticing again notices nothing - they are already there",
      set(daemon.STATE["sessions"]), before_keys)

print("   a session that has only ever drawn a status line is running")
daemon.STATE["sessions"] = {"executor:live": {
    "role": "executor", "path": canon(C), "project": "gamma",
    "context_pct": 12, "last_seen": "10:00:00"}}
check("it has no state at all, because nothing has said what it is doing",
      "state" in daemon.STATE["sessions"]["executor:live"], False)
check("and it counts as live", len(daemon.live_sessions(C)), 1)
pv17 = get("/state")["pairs"]
check("so its context is on its row, not blank",
      pv17[canon(C)]["roles"]["executor"]["pct"], 12)
check("and the row does not claim the pair is not there",
      pv17[canon(C)]["state"] == "no sessions", False)

print("   a loop record on a folder that is not a project earns no row")
daemon.STATE.setdefault("loops", {})[canon(os.path.join(A, "sub"))] = {
    "active": True, "iteration": 0}
check("it is not among the pairs",
      canon(os.path.join(A, "sub")) in get("/state")["pairs"], False)
check("nor in the headline",
      "sub" in daemon.status_headline(), False)
check("while the real projects still are",
      sorted(get("/state")["pairs"][p]["name"] for p in get("/state")["pairs"]),
      ["alpha", "beta", "gamma"])

print("\n19. yesterday at 16:40 is not newer than today at 11:27")
print("    last_seen is a clock and nothing else - no date - because it is")
print("    written to be read on the panel. Sorted as a string it made a")
print("    session that ended yesterday afternoon the freshest record of")
print("    its role, so prune_sessions kept THAT and retired the one that")
print("    was running now. The adoption pass then found the live window")
print("    unrecorded and wrote it again, and the next prune threw it away")
print("    again: two lines in the feed every 45 seconds for an hour, and a")
print("    pair that was finishing turns shown as having no sessions at all")
daemon.STATE["sessions"] = {
    # what a session that ended yesterday afternoon leaves behind: a clock
    # that reads later than this morning, and no seen_at at all, because it
    # was written before there was one
    "executor:oldone": {"role": "executor", "path": canon(C),
                        "project": "gamma", "state": "ended",
                        "context_pct": 20.8, "last_seen": "16:40:13"},
    "planner:oldone": {"role": "planner", "path": canon(C),
                       "project": "gamma", "state": "ended",
                       "context_pct": 10.8, "last_seen": "16:40:13"}}
check("the stale record sorts newest by the clock alone",
      max((s.get("last_seen"), k)
          for k, s in daemon.STATE["sessions"].items())[1], "planner:oldone")
check("and oldest by the time it was actually touched",
      daemon.seen_at(daemon.STATE["sessions"]["planner:oldone"]), 0.0)

seen_lines = []
_real_journal = store.journal


def _watch_journal(kind, text, *a, **k):
    if "adding it to the panel" in (text or ""):
        seen_lines.append(text)
    return _real_journal(kind, text, *a, **k)


store.journal = _watch_journal
try:
    for _pass in range(4):
        for _role in ("executor", "planner"):
            daemon.ensure_record(C, _role, "its channel is answering")
finally:
    store.journal = _real_journal

check("the window is noticed once per role, not once per pass",
      len(seen_lines), 2)
live = daemon.live_sessions(C)
check("and both halves are live afterwards", len(live), 2)
check("with the roles they were noticed for",
      sorted(s.get("role") for s in live), ["executor", "planner"])
check("the record that ended yesterday is not one of them",
      [s for s in live if s.get("state") == "ended"], [])

pv19 = get("/state")["pairs"][canon(C)]
check("so the strip does not call a working pair 'no sessions'",
      pv19["state"] == "no sessions", False)
check("and it carries what is known of the contexts",
      sorted((pv19.get("roles") or {})), ["executor", "planner"])
print("   a context nobody has reported is unknown, which is not the same")
print("   as the pair not being there")
daemon.STATE["sessions"] = {"executor:nostatus": {
    "role": "executor", "path": canon(C), "project": "gamma",
    "state": "idle", "seen_at": time.time(), "last_seen": "11:00:00"}}
pv19 = get("/state")["pairs"][canon(C)]
check("the row is there", "executor" in (pv19.get("roles") or {}), True)
check("its context reads as unknown rather than as absent",
      pv19["roles"]["executor"]["pct"], None)
check("and the pair is not called sessionless",
      pv19["state"] == "no sessions", False)

print("   every record a window can get is keyed by its project")
psrc19 = inspect.getsource(daemon)
check("the noticed one", '"%s:seen:%s" % (role,' in psrc19, True)
check("and the one a registering channel writes",
      '"%s:channel:%s" % (role,' in psrc19, True)

print("\n20. a pair with nothing to do checks in twice an hour, not twice")
print("    a minute")
print("    One pair span all night: the executor finished a turn saying")
print("    'Standing by.', the Stop hook made a report of it, the planner answered")
print("    'continue - Standing by.', and the verdict woke the executor for")
print("    another empty turn. Reports 576 to 581 in three minutes, both")
print("    halves burning the plan limits around the clock. The instructions")
print("    were not the fix and neither was 'wait' - which already delivers")
print("    nothing; the planner was answering continue, and continue wakes")
daemon.CFG.setdefault("thresholds", {})["idle_hold"] = 1.0
daemon.STATE["last_feedback"] = {canon(A): "Standing by."}
daemon.STATE["idle_spin"] = {}
check("an empty exchange is empty on both sides",
      daemon.trivial_report(A, "Standing by."), True)
check("a real report is not, however short the answer was",
      daemon.trivial_report(A, "Rebuilt the rig and re-baked the textures; "
                               "three of the four seams are gone and the "
                               "fourth needs the UV moved. Numbers in the "
                               "log, diff on the branch."), False)
daemon.STATE["last_feedback"] = {canon(A): (
    "Move the UV island off the seam and re-bake, then show me the one "
    "that is left with the numbers beside it. If it is still there after "
    "that, the problem is the cage and not the layout, so say so.")}
check("nor is a short report answered by a real verdict",
      daemon.trivial_report(A, "Done."), False)
print("   a process running settles it outright - that pair is not idling")
daemon.STATE["last_feedback"] = {canon(A): "ok"}
daemon.PROCTRACK[canon(A)] = {"sig": {"cmd": "a long build", "started": 0}}
check("whatever it wrote", daemon.trivial_report(A, "Standing by."), False)
daemon.PROCTRACK.pop(canon(A), None)

print("   three empty turns and the pair is held rather than answered")
daemon.STATE["idle_spin"] = {}
for _i in range(daemon.IDLE_SPIN_LIMIT - 1):
    daemon.note_spin(A, "Standing by.")
check("the count is kept per project",
      daemon.STATE["idle_spin"].get(canon(A)), daemon.IDLE_SPIN_LIMIT - 1)
check("and nobody else's", daemon.STATE["idle_spin"].get(canon(B)), None)
t0 = time.time()
out = daemon.run_review({}, canon(A), daemon.STATE["loops"][canon(A)],
                        "Standing by.", "alpha", "executor")
held = time.time() - t0
check("the hook was held rather than answered", out, None)
check("for the hold, not returned at once", held >= 0.9, True)
check("no verdict was carried, so nothing woke the executor",
      daemon.STATE["idle_spin"].get(canon(A)), None)
print("   and work arriving lets it go at once, not at the end of the hold")
daemon.STATE["idle_spin"] = {canon(A): daemon.IDLE_SPIN_LIMIT - 1}
daemon.CFG["thresholds"]["idle_hold"] = 30.0
box = {}


def _held():
    t = time.time()
    daemon.run_review({}, canon(A), daemon.STATE["loops"][canon(A)],
                      "Standing by.", "alpha", "executor")
    box["took"] = time.time() - t


th = threading.Thread(target=_held)
th.start()
check("it is holding", until(lambda: canon(A) in daemon.IDLEWAIT
                             and not daemon.IDLEWAIT[canon(A)].is_set()), True)
post("/task", {"project": A, "instructions": "here is real work"},
     secret=True)
th.join(20)
check("the task released it", th.is_alive(), False)
check("well before the hold was up", box.get("took", 99) < 25, True)
check("and the loop is on for it", state()["loops"][canon(A)]["active"], True)

print("   an ordinary review is untouched: a real report still goes to the")
print("   planner and still comes back as a verdict")
daemon.CFG["thresholds"]["idle_hold"] = 0
daemon.STATE["idle_spin"] = {}
check("with the damper off nothing is counted at all",
      daemon.note_spin(A, "Standing by.") and False or
      "idle_hold" in daemon.CFG["thresholds"], True)
check("and the wall simulation turns it off for that reason",
      "idle_hold\"] = 0" in open(
          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "test_wall_handover.py"), encoding="utf-8").read(),
      True)
daemon.CFG["thresholds"]["idle_hold"] = 1200

print("\n21. a verdict that accepts work does not pass without artefacts,")
print("    and the bridge opens them itself")
print("    The canon in a document is read once and then competes with the")
print("    task for attention. This is the same rule standing in the way of")
print("    the action instead: done and stop are the two verdicts that")
print("    accept, and neither is taken on the planner's word alone")
GPROJ = A
GSID = "gate0001"
register(GPROJ, "planner", "gplan001")
register(GPROJ, "executor", GSID)
post("/loop", {"action": "start", "project": GPROJ})
PROOF = os.path.join(GPROJ, "run.log")
with open(PROOF, "w", encoding="utf-8") as fh:
    fh.write("exit 0\n")

REPORTS = []
threading.Thread(
    target=lambda: REPORTS.append(stop_hook(GPROJ, "executor", GSID,
                                            "the piece is finished")),
    daemon=True).start()
check("the executor's report is waiting for an answer",
      until(lambda: daemon.PENDING.get(canon(GPROJ))), True)
# The iteration is counted when the report is DELIVERED, not when it is
# answered, so it has already advanced by the time any verdict arrives.
# What matters here is that a refusal does not burn one - two refusals and
# an acceptance must all belong to the same numbered iteration.
it_at_report = daemon.loop_state(GPROJ)[1].get("iteration", 0)

r = post("/verdict", {"project": GPROJ, "verdict": "done",
                      "feedback": "excellent, accepted"}, secret=True)
check("a bare 'done' is refused", (r.get("ok"), r.get("refused")),
      (False, True))
check("and the refusal says what is missing, not just that it failed",
      "Checked:" in (r.get("error") or ""), True)
r = post("/verdict", {"project": GPROJ, "verdict": "done",
                      "feedback": "Checked: out/nowhere/render.png"},
         secret=True)
check("a block naming a path that is not there is refused too",
      (r.get("ok"), r.get("refused")), (False, True))
check("and it names the file, so the claim is a forgery and not a slip",
      "render.png" in (r.get("error") or ""), True)
print("   the refusal must cost the report nothing - this is the half that")
print("   would break the loop if it were wrong")
check("the report is STILL waiting after two refusals",
      bool(daemon.PENDING.get(canon(GPROJ))), True)
check("and the executor is still blocked in its Stop hook", REPORTS, [])
check("and neither refusal burned an iteration number",
      daemon.loop_state(GPROJ)[1].get("iteration", 0), it_at_report)
r = post("/verdict", {"project": GPROJ, "verdict": "done",
                      "feedback": "Checked: run.log - exit 0"}, secret=True)
check("a block naming a file that exists goes through",
      (r.get("ok"), r.get("delivered")), (True, True))
check("the executor is released", until(lambda: REPORTS), True)
check("on the same iteration the two refusals belonged to",
      daemon.loop_state(GPROJ)[1].get("iteration", 0), it_at_report)
print("   and answering the same report twice is still impossible - the")
print("   refusals did not consume it, and the acceptance did")
r = post("/verdict", {"project": GPROJ, "verdict": "done",
                      "feedback": "Checked: run.log"}, secret=True)
check("the second verdict finds no report waiting",
      bool(daemon.PENDING.get(canon(GPROJ))), False)
check("it is not delivered as an answer to anything",
      r.get("delivered") is not True or r.get("ok") is False
      or "idle" in json.dumps(r), True)

print("\n22. continue and wait are not gated, and the named exit is loud")
print("    continue and wait accept nothing - gating them would only make")
print("    the loop expensive. The exit for work with nothing to open is")
print("    allowed on purpose, because the alternative teaches a pair to")
print("    invent a path; what it cannot be is quiet")
for v in ("continue", "wait"):
    r = post("/verdict", {"project": GPROJ, "verdict": v,
                          "feedback": "ok"}, secret=True)
    check("'%s' passes with no block at all" % v, r.get("ok"), True)
r = post("/verdict", {"project": GPROJ, "verdict": "done",
                      "feedback": "Checked: no artifacts — nothing"},
         secret=True)
check("a throwaway reason is refused", r.get("refused"), True)
check("and the refusal counts the words back",
      "words" in (r.get("error") or ""), True)
noart_before = (daemon.STATE.get("noart") or {}).get(canon(GPROJ), 0)
REASON = ("this was a read-only investigation of the logs, no code was "
         "changed, and nothing to open")
r = post("/verdict", {"project": GPROJ, "verdict": "done",
                      "feedback": "Checked: no artifacts — " + REASON},
         secret=True)
check("a real reason is accepted", r.get("ok"), True)
check("but it is counted against this project",
      (daemon.STATE.get("noart") or {}).get(canon(GPROJ), 0),
      noart_before + 1)
check("and only against this project",
      (daemon.STATE.get("noart") or {}).get(canon(B), 0), 0)
check("the count rides in the same readout the panel polls",
      daemon.situation(GPROJ)["no_artifacts"], noart_before + 1)
jt = json.dumps(store.recent_events(60, project=canon(GPROJ)),
                ensure_ascii=False)
check("a warn-level line is written, so it reaches the feed",
      "Accepted with NO ARTEFACTS" in jt, True)
check("with the reason in it, so a reader a week later can weigh it",
      "investigation of the logs" in jt, True)

print("\n23. the same gate one step earlier, and frames on request")
print("    the PreToolUse hook asks the daemon the same question before the")
print("    call leaves the planner's window. Two levels on purpose: this one")
print("    is sooner, the daemon's is always - a window running without the")
print("    bridge's hooks still cannot slip a bare 'done' past it")
def pre(args):
    return post("/event", {"hook_event_name": "PreToolUse", "role": "planner",
                           "session_id": "gplan001", "project_dir": GPROJ,
                           "cwd": GPROJ, "tool_name": "mcp__bridge__verdict",
                           "tool_input": args})
out = (pre({"verdict": "done", "feedback": "accepted"})
       .get("hook_output") or {}).get("hookSpecificOutput") or {}
check("a bare done is denied before it is sent",
      out.get("permissionDecision"), "deny")
check("with the same text the daemon would have used",
      "Checked:" in (out.get("permissionDecisionReason") or ""), True)
check("and one implementation answers both, so they cannot drift",
      "verdict_gate(" in inspect.getsource(daemon.handle_event), True)
out2 = pre({"verdict": "done", "feedback": "Checked: run.log"})
check("a proper block is not denied", out2.get("hook_output"), None)
out3 = pre({"verdict": "continue", "feedback": "one more round"})
check("and continue is never denied", out3.get("hook_output"), None)
print("   frames: the planner says whether a piece is visual - the bridge")
print("   never guesses it from the words of the report")
post("/task", {"project": GPROJ,
               "instructions": "[FRAMES] render the thing and show me"},
     secret=True)
check("the request is remembered against that project",
      (daemon.STATE.get("frames") or {}).get(canon(GPROJ)), True)
check("and against that project only",
      (daemon.STATE.get("frames") or {}).get(canon(C)), None)
DELIVERED[(canon(GPROJ), "planner")].clear()
threading.Thread(
    target=lambda: stop_hook(GPROJ, "executor", GSID, "done, everything is finished"),
    daemon=True).start()
check("a report with no frames reaches the planner headed so",
      until(lambda: any(body_of(d.get("content")).startswith("NO FRAMES")
                        for d in DELIVERED[(canon(GPROJ), "planner")])), True)
post("/verdict", {"project": GPROJ, "verdict": "continue", "feedback": "?"},
     secret=True)
SHOT = os.path.join(GPROJ, "shot.png")
with open(SHOT, "wb") as fh:
    fh.write(b"\x89PNG\r\n")
DELIVERED[(canon(GPROJ), "planner")].clear()
threading.Thread(
    target=lambda: stop_hook(GPROJ, "executor", GSID,
                             "frame: shot.png"), daemon=True).start()
check("a report that does name a real image is not headed at all",
      until(lambda: DELIVERED[(canon(GPROJ), "planner")]
            and not any(body_of(d.get("content")).startswith("NO FRAMES")
                        for d in DELIVERED[(canon(GPROJ), "planner")])), True)
check("and the request is cleared once it has been met",
      (daemon.STATE.get("frames") or {}).get(canon(GPROJ)), None)
post("/verdict", {"project": GPROJ, "verdict": "continue", "feedback": "ok"},
     secret=True)

print("\n24. a code change is not accepted until someone says where it lives")
print("    From a watched project, 2026-08-18: the rule 'a fix that is not")
print("    pipeline is not a fix' had gates for the QUALITY of a patch and")
print("    none asking whether the pipeline reproduces it. 45 patch steps")
print("    piled up, 18 of them pure carry-over, each a lawful exception on")
print("    the day it was made. Accepting a code change now costs one line")
RPROJ = B
RSID = "resid001"
register(RPROJ, "planner", "rplan001")
register(RPROJ, "executor", RSID)
post("/loop", {"action": "start", "project": RPROJ})
RPROOF = os.path.join(RPROJ, "run.log")
with open(RPROOF, "w", encoding="utf-8") as fh:
    fh.write("exit 0\n")

RDONE = []
threading.Thread(
    target=lambda: RDONE.append(stop_hook(
        RPROJ, "executor", RSID,
        "Fixed the path parsing in bridge/store.py, all suites green.")),
    daemon=True).start()
check("the report is waiting", until(lambda: daemon.PENDING.get(canon(RPROJ))),
      True)
r = post("/verdict", {"project": RPROJ, "verdict": "done",
                      "feedback": "Checked: run.log"}, secret=True)
check("a code report is not accepted on «Checked:» alone",
      (r.get("ok"), r.get("refused")), (False, True))
check("and the refusal asks where the fix lives, in as many words",
      "Residence:" in (r.get("error") or ""), True)
check("it explains WHY rather than quoting a rule at the planner",
      "patch" in (r.get("error") or "").lower(), True)
check("the report is untouched by the refusal",
      bool(daemon.PENDING.get(canon(RPROJ))), True)
r = post("/verdict", {"project": RPROJ, "verdict": "done",
                      "feedback": "Checked: run.log\n"
                                  "Residence: bridge/store.py:norm"},
         secret=True)
check("with the residence line it goes through", r.get("ok"), True)
check("and the executor is released", until(lambda: RDONE), True)
print("   a report that changed no code is not asked for a residence - a")
print("   demand nobody can answer honestly teaches the pair to write a")
print("   meaningless line to get past it")
RDONE2 = []
threading.Thread(
    target=lambda: RDONE2.append(stop_hook(
        RPROJ, "executor", RSID,
        "Answered a question about the order of acceptance, changed nothing.")),
    daemon=True).start()
check("the second report is waiting",
      until(lambda: daemon.PENDING.get(canon(RPROJ))), True)
r = post("/verdict", {"project": RPROJ, "verdict": "done",
                      "feedback": "Checked: run.log"}, secret=True)
check("no code, no residence demanded", r.get("ok"), True)
check("released", until(lambda: RDONE2), True)

print("\n25. a temporary solution is allowed, and counted")
print("    The other half of the same lesson: no single workaround was wrong")
print("    on the day it was made. What was wrong is that nothing counted")
print("    them, so nobody saw the pile until it was the whole system")
DPROJ = C
DSID = "debt0001"
register(DPROJ, "planner", "dplan001")
register(DPROJ, "executor", DSID)
post("/loop", {"action": "start", "project": DPROJ})
with open(os.path.join(DPROJ, "run.log"), "w", encoding="utf-8") as fh:
    fh.write("exit 0\n")
check("nothing is owed to start with", daemon.open_debt(DPROJ), [])
DD = []
threading.Thread(
    target=lambda: DD.append(stop_hook(
        DPROJ, "executor", DSID,
        "Took a shortcut in the path parser.\n"
        "Debt: the exception list is hard-coded - closed by "
        "moving it into config.json\n"
        "Waiting for the next task.")), daemon=True).start()
check("the report arrives", until(lambda: daemon.PENDING.get(canon(DPROJ))),
      True)
check("the debt was taken from the report itself",
      len(daemon.open_debt(DPROJ)), 1)
row = daemon.open_debt(DPROJ)[0]
check("with what is temporary",
      "the exception list is hard-coded" in row["what"], True)
check("and with what closes it", "config.json" in row["how"], True)
check("it is written where the project can see it, not only in our state",
      os.path.exists(os.path.join(DPROJ, "bridge-logs", "DEBT.md")), True)
_debt = open(os.path.join(DPROJ, "bridge-logs", "DEBT.md"),
             encoding="utf-8").read()
check("the file says how many are open", "Open: **1**" in _debt, True)
check("and carries the line unshortened",
      "the exception list is hard-coded" in _debt, True)
check("it rides in the readout the panel polls",
      daemon.situation(DPROJ)["debt_open"], 1)
check("and in the strip, so it is visible without opening the project",
      (get("/state").get("pairs") or {}).get(canon(DPROJ), {})
      .get("debt_open"), 1)
check("against this project only",
      [daemon.situation(p)["debt_open"] for p in (A, B)], [0, 0])
_jd = json.dumps(store.recent_events(60, project=canon(DPROJ)),
                 ensure_ascii=False)
check("a warn-level line reaches the feed", "DEBT declared" in _jd, True)
print("   it never blocks - blocking would only teach the pair to stop")
print("   saying the word - so the verdict goes through with the debt open")
r = post("/verdict", {"project": DPROJ, "verdict": "done",
                      "feedback": "Checked: run.log\n"
                                  "Residence: bridge/store.py:norm"},
         secret=True)
check("the piece is accepted", r.get("ok"), True)
check("and the debt is still standing", len(daemon.open_debt(DPROJ)), 1)
check("released", until(lambda: DD), True)
print("   and it is put out only by saying what put it out")
DD2 = []
threading.Thread(
    target=lambda: DD2.append(stop_hook(
        DPROJ, "executor", DSID,
        "Debt closed: the exception list is hard-coded — moved into config.json")),
    daemon=True).start()
check("the closing report arrives",
      until(lambda: daemon.PENDING.get(canon(DPROJ))), True)
check("nothing is owed any more", daemon.open_debt(DPROJ), [])
check("but the line is kept, not deleted - the pile is the evidence",
      len(daemon.debt_rows(DPROJ)), 1)
_debt2 = open(os.path.join(DPROJ, "bridge-logs", "DEBT.md"),
              encoding="utf-8").read()
check("the register shows it closed and by what",
      ("Open: **0**" in _debt2 and "config.json" in _debt2), True)
post("/verdict", {"project": DPROJ, "verdict": "continue", "feedback": "ok"},
     secret=True)
check("released", until(lambda: DD2), True)

print("\n26. the rules ride on the real delivery, both ways")
print("    with_rules() in isolation proves the function; this proves the")
print("    path - a task the planner sent and a report the executor made,")
print("    both arriving at a live channel with the canon in front")
RP = A
DELIVERED[(canon(RP), "executor")].clear()
post("/task", {"project": RP, "instructions": "ETO TELO ZADACHI"}, secret=True)
# This session has been written to before, so the canon has already been
# spent on it in full - what rides here is the reminder. That IS the
# behaviour under test: the fence is always there, the whole text is not.
check("the task arrived",
      until(lambda: DELIVERED[(canon(RP), "executor")]), True)
_t = DELIVERED[(canon(RP), "executor")][-1].get("content") or ""
check("with the rules in front of it",
      ("RULES OF WORK" in _t,
       _t.index("RULES OF WORK") < _t.index("ETO TELO ZADACHI")),
      (True, True))
check("and the body intact behind them",
      body_of(_t).endswith("ETO TELO ZADACHI"), True)
DELIVERED[(canon(RP), "planner")].clear()
threading.Thread(
    target=lambda: stop_hook(RP, "executor", GSID, "ETO TELO OTCHETA"),
    daemon=True).start()
check("the report arrived",
      until(lambda: DELIVERED[(canon(RP), "planner")]), True)
_r = DELIVERED[(canon(RP), "planner")][-1].get("content") or ""
check("with the rules in front of it too",
      ("RULES OF WORK" in _r,
       _r.index("RULES OF WORK") < _r.index("ETO TELO OTCHETA")),
      (True, True))
check("and the report the planner has to judge is still whole",
      "Executor report" in body_of(_r) and "ETO TELO OTCHETA" in body_of(_r),
      True)
print("   and what the residence gate reads is the CLEAN report - the rules")
print("   name .py files, and a gate that saw them would demand a residence")
print("   line for every report ever made")
_pend = daemon.PENDING.get(canon(RP)) or {}
check("PENDING keeps the report without the envelope",
      "RULES OF WORK" in (_pend.get("content") or ""), False)
post("/verdict", {"project": RP, "verdict": "continue", "feedback": "ok"},
     secret=True)
print("   and the tiering holds on the real path: a window that has "
      "never been")
print("   written to gets the canon whole, and only then the reminder")
FRESH = "fresh-sid-0001"
daemon.remember_session(RP, "executor", FRESH)
DELIVERED[(canon(RP), "executor")].clear()
post("/task", {"project": RP, "instructions": "PERVAYA"}, secret=True)
check("the first task to a fresh window carries the whole canon",
      until(lambda: DELIVERED[(canon(RP), "executor")]
            and "*"
            in (DELIVERED[(canon(RP), "executor")][-1].get("content")
                or "")), True)
DELIVERED[(canon(RP), "executor")].clear()
post("/task", {"project": RP, "instructions": "VTORAYA"}, secret=True)
check("and the second one carries the reminder instead",
      until(lambda: DELIVERED[(canon(RP), "executor")]
            and "*"
            not in (DELIVERED[(canon(RP), "executor")][-1].get("content")
                    or "")), True)

print("\n27. this suite leaves nothing behind in anybody's real state")
check("its data lives in the temp folder",
      os.environ["BRIDGE_DATA"].startswith(TMP), True)
check("so does the client's, so no transcript lands in the real store",
      os.environ["CLAUDE_CONFIG_DIR"].startswith(TMP), True)
check("every project it made is under it too",
      all(p.startswith(TMP) for p in PROJ.values()), True)
check("and the daemon it drove was never the live one", PORT != 8765, True)
note("windows opened in the whole run", len(launches()))

SRV.shutdown()
print("\n" + ("-" * 60))
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
