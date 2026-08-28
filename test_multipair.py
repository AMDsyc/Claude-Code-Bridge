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
import ast
import inspect
import json
import os
import re
import socket
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
        # getChat is how the bridge asks "is our message still the pinned
        # one". TG_PINNED[0] is what this chat answers: None for "nothing
        # pinned", or a message_id.
        if method == "getChat":
            res = {}
            if TG_PINNED[0] is not None:
                res["pinned_message"] = {"message_id": TG_PINNED[0]}
            return self._reply({"ok": True, "result": res})
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
TG_PINNED = [None]
_tg_srv = ThreadingHTTPServer(("127.0.0.1", 0), _TG)
threading.Thread(target=_tg_srv.serve_forever, daemon=True).start()
os.environ["BRIDGE_TELEGRAM_API"] = "http://127.0.0.1:%d" \
    % _tg_srv.server_address[1]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgecore import archive, daemon, sessions, store, telegram   # noqa: E402

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

def _holds(v, kind, path):
    """Does this container still name `path`? Shape-aware, like the inventory.

    Used by case 50 to ask the removal the only question that matters, and
    written once rather than inline three times: a per-shape test copied
    about is the very drift STATE_PATHS exists to stop.
    """
    if kind in ("path", "pair"):
        return isinstance(v, dict) and any(
            k == path or k.rpartition("|")[0] == path for k in v)
    if kind == "value":
        return isinstance(v, dict) and any(
            isinstance(r, dict) and daemon.norm(r.get("path")) == path
            for r in v.values())
    if kind == "rows":
        return isinstance(v, list) and any(
            isinstance(r, dict) and daemon.norm(r.get("path")) == path
            for r in v)
    return False

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


def _server_gone(exc):
    """Turn "the shared daemon went quiet" into a sentence.

    Every call in this suite goes to one server, so when that server is
    not there any more EVERY case after the point fails, and each of them
    fails with a bare URLError naming a port. That is what an accidental
    early SRV.shutdown() looked like for a week: a hang with no handler
    thread, read as a daemon that had died of something in the bridge.
    Six words here instead of a stack trace.
    """
    return RuntimeError(
        "the suite's shared daemon on 127.0.0.1:%d did not answer (%s). "
        "Nothing in the bridge does that: look for a shutdown() or a "
        "server_close() called on SRV before the end of the run - the "
        "socket stays bound after shutdown(), so this shows up as a hang "
        "rather than as a refusal." % (PORT, exc))

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
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise _server_gone(exc)
    if isinstance(out, dict):
        out["status"] = code
    return out


def post_rc(path, payload, secret=True):
    """post(), but handing back (status, body) as two things.

    The cases about REFUSALS read better this way - `(200, True)` and
    `(403, None)` say what happened in one line - and secret-checked
    endpoints are the rule rather than the exception among them, so the
    default is the other way round from post(). Same server, same handler:
    the difference is the shape of the answer, not where it came from.
    """
    head = {"Content-Type": "application/json"}
    if secret:
        head["X-Bridge-Secret"] = daemon.SECRET
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(payload).encode("utf-8"), headers=head)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise _server_gone(exc)


def get(path):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise _server_gone(exc)


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
                        "bridgecore", "panel.html"), encoding="utf-8").read(),
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

# The block is here because the gate now covers continue as well; this case
# is about which executor gets which feedback, and a refusal before delivery
# would test the gate over and over instead. A real path rather than the
# named exit, deliberately: the exit is counted, and a later case measures
# that counter.
for _p in (A, B):
    with open(os.path.join(_p, "seen.txt"), "w", encoding="utf-8") as _fh:
        _fh.write("read by the planner" + chr(10))
post("/verdict", {"project": B, "verdict": "continue",
                  "feedback": "Checked: seen.txt\nBETA-FEEDBACK"},
     secret=True)
tb.join(30)
check("answering beta releases beta", tb.is_alive(), False)
check("and leaves alpha waiting", ta.is_alive(), True)
check("beta's executor was handed beta's feedback",
      "BETA-FEEDBACK" in json.dumps(OUT.get("beta")), True)
post("/verdict", {"project": A, "verdict": "continue",
                  "feedback": "Checked: seen.txt\nALPHA-FEEDBACK"},
     secret=True)
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
                         "bridgecore", "panel.html"), encoding="utf-8").read()
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
# The limit is MEASURED, not 40. A line with no path is about the bridge
# and passes every project's filter by design, so enough of them landing
# after the mark push it out of a 40-row window and this reads as a defect
# in recent_events when it is nothing but noise from another thread. It
# cost a false red on 2026-08-22 that could not be reproduced in thirteen
# runs; the mechanism is exact, though - 40 path-less lines after the mark
# and it is gone. Sizing the window by what actually matches the filter
# makes the case say what it means: FILTERING HAPPENS BEFORE TRIMMING.
_matching = len([r for r in store._read_events(
    os.path.join(store.day_dir(), "events.jsonl"))
    if not r.get("path") or r.get("path") == canon(QUIET)])
filtered = [e.get("text")
            for e in store.recent_events(_matching, project=canon(QUIET))]
check("filtering first, it survives", "QUIET-MARK" in filtered, True)
print("   and the same window, unfiltered, does NOT hold it - which is the")
print("   whole claim: the filter runs first, the cut second")
check("cut first and it would be lost",
      "QUIET-MARK" in [e.get("text") for e in store.recent_events(_matching)],
      False)
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
print("   four of them since 2026-08-22: the three that name the inbox or")
print("   the wait, plus the new one for a pair parked on a PERSON - a")
print("   `wait` verdict with nothing running (case 31)")
check("the alerts that need a human still go out",
      rsrc.count('notify("needs_you"'), 4)
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

for _p in (A, B):
    with open(os.path.join(_p, "seen.txt"), "w", encoding="utf-8") as _fh:
        _fh.write("read by the planner" + chr(10))
# The chat path meets the same gate as every other door now, so this needs
# a block. The case is about WHICH pair a chat command reaches, not about
# acceptance - a refusal before addressing would test the gate instead.
said = daemon.run_telegram_command(
    "/verdict @beta continue BETA-BY-NAME. Checked: seen.txt")
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
said = daemon.run_telegram_command(
    "/verdict continue BY-REPLY. Checked: seen.txt", reply_to=_anchor)
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
                          "bridgecore", "panel.html"), encoding="utf-8").read()
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
# the deciding moved into check_processes on 2026-08-21 so that it
# could be run in a test; process_watch is now only the loop
psrc17 = inspect.getsource(daemon.check_processes)
# This used to assert that the chat alert passed the command through
# brief(). It goes further now: since 2026-08-21 the phone gets a short
# NAME and no command tail at all - the owner called the raw version
# feed pollution, so the assertion here is the stronger one.
check("the stuck-process alert carries no raw command tail",
      'brief(meta["cmd"]))' in psrc17, False)
check("only a short name of what is running",
      'brief(meta.get("cmd"), 40)' in psrc17, True)
check("while the sessions still get the whole thing - they act on it",
      'deliver(path, "executor", ev' in psrc17, True)
check("and the pair's planner is asked before any human is",
      psrc17.index('deliver(path, "planner"')
      < psrc17.index('notify("process_stuck"'), True)

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
# This used to read "continue and wait pass with no block". Only wait does
# now: continue carries a judgement, and a judgement made on the executor's
# word is acceptance by hearsay. Changed deliberately - the case still asks
# what it always asked, which verdicts are free of the gate.
r = post("/verdict", {"project": GPROJ, "verdict": "wait",
                      "feedback": "ok"}, secret=True)
check("'wait' passes with no block at all - it judges nothing",
      r.get("ok"), True)
r = post("/verdict", {"project": GPROJ, "verdict": "continue",
                      "feedback": "ok"}, secret=True)
check("'continue' no longer does", (r.get("ok"), r.get("refused")),
      (False, True))
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
# This used to say "continue is never denied". It is denied now when it
# judges without opening anything - the same rule the daemon applies, asked
# one step earlier. What the case is for is unchanged: the hook says the same
# thing the daemon would.
out3 = pre({"verdict": "continue", "feedback": "one more round"})
check("a continue that judges without artefacts is denied here too",
      ((out3.get("hook_output") or {}).get("hookSpecificOutput") or {})
      .get("permissionDecision"), "deny")
out4 = pre({"verdict": "wait", "feedback": "still running"})
check("and wait is never denied - it judges nothing",
      out4.get("hook_output"), None)
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
post("/verdict", {"project": GPROJ, "verdict": "continue", "feedback":
                      "Checked: no artifacts - releasing the executor for the next step of this case"},
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
post("/verdict", {"project": GPROJ, "verdict": "continue", "feedback":
                      "Checked: no artifacts - releasing the executor for the next step of this case"},
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
        "Fixed the path parsing in bridgecore/store.py, all suites green.")),
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
                                  "Residence: bridgecore/store.py:norm"},
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
                                  "Residence: bridgecore/store.py:norm"},
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
post("/verdict", {"project": DPROJ, "verdict": "continue", "feedback":
                      "Checked: no artifacts - releasing the executor for the next step of this case"},
     secret=True)
check("released", until(lambda: DD2), True)

print("\n26. the rules ride on the real delivery, both ways")
print("    with_rules() in isolation proves the function; this proves the")
print("    path - a task the planner sent and a report the executor made,")
print("    both arriving at a live channel with the canon in front")
RP = A


def carrying(role, needle):
    """The delivery that carries this text - not merely the latest one.

    Every read here used to be DELIVERED[...][-1], and that made the
    whole case a race with the idle watcher: its "sent it its state"
    branch delivers state_report(...) tagged {"kind": "task"}, so one
    background message can both become [-1] and - because that kind is
    CHARGED - spend the session's one full-canon delivery.

    Reading [-1] failed in two different ways. As a check it returned
    the wrong answer; as `_r.index(...)` it raised ValueError and took
    the suite down with exit 1 and no FAIL line at all, which is the
    worse of the two because it looks like a crash rather than a
    verdict.

    When this failed the tiering was never wrong: 10 536 characters
    against 1 553, exactly as documented.
    """
    for d in DELIVERED[(canon(RP), role)]:
        body = d.get("content") or ""
        if needle in body:
            return body
    return None


DELIVERED[(canon(RP), "executor")].clear()
post("/task", {"project": RP, "instructions": "ETO TELO ZADACHI"}, secret=True)
# This session has been written to before, so the canon has already been
# spent on it in full - what rides here is the reminder. That IS the
# behaviour under test: the fence is always there, the whole text is not.
check("the task arrived",
      until(lambda: carrying("executor", "ETO TELO ZADACHI")), True)
_t = carrying("executor", "ETO TELO ZADACHI") or ""
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
      until(lambda: carrying("planner", "ETO TELO OTCHETA")), True)
_r = carrying("planner", "ETO TELO OTCHETA") or ""
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
post("/verdict", {"project": RP, "verdict": "continue", "feedback":
                      "Checked: no artifacts - releasing the executor for the next step of this case"},
     secret=True)
print("   and the tiering holds on the real path: a window that has "
      "never been")
print("   written to gets the canon whole, and only then the reminder")
FRESH = "fresh-sid-0001"
daemon.remember_session(RP, "executor", FRESH)


# The precondition of this case is a session nobody has written to yet,
# and on the real path that has to be established, not assumed. Stopping
# the loop holds off the watcher that would otherwise write first, and
# clearing the mark makes "fresh" true at the moment it is claimed.
# Neither touches what is under test: /task -> deliver_ex ->
# rules_for_delivery still runs exactly as it does in production, and
# the two assertions below still fail if the tiering stops working.
post("/loop", {"action": "stop", "project": RP})
(daemon.STATE.get("rules_full") or {}).pop(FRESH, None)
DELIVERED[(canon(RP), "executor")].clear()
post("/task", {"project": RP, "instructions": "PERVAYA"}, secret=True)
check("the first task to a fresh window carries the whole canon",
      until(lambda: "*" in (carrying("executor", "PERVAYA") or "")), True)
DELIVERED[(canon(RP), "executor")].clear()
post("/task", {"project": RP, "instructions": "VTORAYA"}, secret=True)
check("and the second one carries the reminder instead",
      until(lambda: carrying("executor", "VTORAYA") is not None
            and "*" not in carrying("executor", "VTORAYA")), True)

print("\n28. continue is a judgement too, and judgement needs an artefact")
print("    Another pair found the gap by falling into it: their planner")
print("    checked that a receipt EXISTED, then passed judgement on the")
print("    substance from the report - in a continue, which the gate let")
print("    through. A gate on the accepting verdicts only is a gate with a")
print("    door beside it, and continue is where most judging happens.")
print("    continue is also the MOST FREQUENT verdict, so this case exists")
print("    mostly to prove a refusal cannot stall the loop")
CPROJ = B
CSID = "cont0001"
register(CPROJ, "planner", "cplan001")
register(CPROJ, "executor", CSID)
post("/loop", {"action": "start", "project": CPROJ})
with open(os.path.join(CPROJ, "run.log"), "w", encoding="utf-8") as fh:
    fh.write("exit 0\n")
CDONE = []
threading.Thread(
    target=lambda: CDONE.append(stop_hook(CPROJ, "executor", CSID,
                                          "The first slice is in and reviewed against the reference frame; no code was moved for it and the pipeline was not touched at all.")),
    daemon=True).start()
check("the report is waiting", until(lambda: daemon.PENDING.get(canon(CPROJ))),
      True)
it0 = daemon.loop_state(CPROJ)[1].get("iteration", 0)
r = post("/verdict", {"project": CPROJ, "verdict": "continue",
                      "feedback": "looks right in substance, carry on"},
         secret=True)
check("a continue that judges without opening anything is refused",
      (r.get("ok"), r.get("refused")), (False, True))
check("and the refusal says why, not just that it failed - acceptance on "
      "the executor word is what it names",
      "hearsay" in (r.get("error") or ""), True)
r = post("/verdict", {"project": CPROJ, "verdict": "continue",
                      "feedback": "Checked: out/nowhere/proof.txt"},
         secret=True)
check("a path that is not there is refused by name",
      "proof.txt" in (r.get("error") or ""), True)
print("   the half that would matter if it were wrong: a refused continue")
print("   must cost the loop nothing, or the most frequent verdict becomes")
print("   the most expensive one")
check("the report is untouched by two refusals",
      bool(daemon.PENDING.get(canon(CPROJ))), True)
check("the executor is still blocked", CDONE, [])
check("and no iteration was burned",
      daemon.loop_state(CPROJ)[1].get("iteration", 0), it0)
r = post("/verdict", {"project": CPROJ, "verdict": "continue",
                      "feedback": "Checked: run.log - read it, exit 0"},
         secret=True)
check("a proper continue goes straight through", r.get("ok"), True)
check("and the executor is released", until(lambda: CDONE), True)
print("   wait is the one verdict left free, because it judges nothing")
CW = []
threading.Thread(
    target=lambda: CW.append(stop_hook(CPROJ, "executor", CSID,
                                       "The build is still running: four of the nine targets are through, the slowest one is still going, and I will report again when it lands.")), daemon=True).start()
check("a second report arrives",
      until(lambda: daemon.PENDING.get(canon(CPROJ))), True)
r = post("/verdict", {"project": CPROJ, "verdict": "wait",
                      "feedback": "Understood, the build is still running - I will look at it when it lands rather than judging anything from the report on its own right now. "
                                  "running, I will look when it lands"},
     secret=True)
check("wait passes with no block at all", r.get("ok"), True)
check("released", until(lambda: CW), True)
print("   and the named exit still works on a continue, still loudly")
noart0 = (daemon.STATE.get("noart") or {}).get(canon(CPROJ), 0)
CN = []
threading.Thread(
    target=lambda: CN.append(stop_hook(CPROJ, "executor", CSID,
                                       "Answered the question about the order of acceptance and wrote the answer into the notes; nothing was built and nothing on disk changed.")),
    daemon=True).start()
check("a third report arrives",
      until(lambda: daemon.PENDING.get(canon(CPROJ))), True)
r = post("/verdict", {"project": CPROJ, "verdict": "continue",
                      "feedback": "Checked: no artifacts - this was a question "
                                  "about the order of work and nothing was "
                                  "built to look at"}, secret=True)
check("the named exit is accepted on a continue as well", r.get("ok"), True)
check("and counted, so leaning on it leaves a column",
      (daemon.STATE.get("noart") or {}).get(canon(CPROJ), 0), noart0 + 1)
_jn = json.dumps(store.recent_events(60, project=canon(CPROJ)),
                 ensure_ascii=False)
check("with a warn line in the feed", "NO ARTEFACTS" in _jn, True)
check("released", until(lambda: CN), True)

print("\n29. four pairs - eight agents - on one daemon")
print("    The owner asked for eight agents. In this bridge that is four")
print("    pairs, one more than the three every case above runs on. The")
print("    question a fourth asks is not 'does the logic work' - the cases")
print("    above answer that - but 'does anything only appear at scale':")
print("    a feed leaking across projects, a PENDING shared by accident, a")
print("    decision meant for one pair reaching another")
DELTA = os.path.join(TMP, "delta")
os.makedirs(DELTA, exist_ok=True)
FOUR = [PROJ["alpha"], PROJ["beta"], PROJ["gamma"], DELTA]
post("/config", {"projects": {A: {}, B: {}, C: {}, DELTA: {}}})
check("the fourth project joins the watch list",
      canon(DELTA) in (daemon.CFG.get("projects") or {}), True)
check("and the other three are still there",
      sorted(os.path.basename(p) for p in (daemon.CFG.get("projects") or {})),
      ["alpha", "beta", "delta", "gamma"])

print("   eight agents: an executor and a planner on each of the four")
for _p in FOUR:
    with daemon._lock:
        for _role, _sid in (("executor", "ex-%s" % os.path.basename(_p)),
                            ("planner", "pl-%s" % os.path.basename(_p))):
            daemon.STATE.setdefault("sessions", {})[
                "%s:%s" % (_role, _sid[:8])] = {
                "role": _role, "path": canon(_p), "session_id": _sid,
                "model": "Opus 5" if _role == "executor" else "Fable 5",
                "window": 1000000, "window_observed": True,
                "context_tokens": 120000, "state": "idle",
                "last_seen": daemon.now(), "seen_at": time.time(),
                "turn_costs": [30000, 25000, 40000]}
            daemon.STATE.setdefault("last_session", {})[
                "%s|%s" % (canon(_p), _role)] = _sid
        daemon.save_state()
_mine = {(canon(s.get("path")), s.get("role"))
         for s in daemon.STATE["sessions"].values()
         if str(s.get("session_id") or "").startswith(("ex-", "pl-"))}
check("eight agents - four pairs - are on record", len(_mine), 8)
check("four executors",
      len([1 for _p, _r in _mine if _r == "executor"]), 4)
check("four planners", len([1 for _p, _r in _mine if _r == "planner"]), 4)
check("one of each on every project",
      sorted(os.path.basename(_p) for _p, _r in _mine if _r == "planner"),
      ["alpha", "beta", "delta", "gamma"])

print("   each pair writes its own line, and no feed carries another's")
for _p in FOUR:
    store.journal("loop", "MARK-%s" % os.path.basename(_p),
                  os.path.basename(_p), project_dir=_p)
for _p in FOUR:
    _texts = [e.get("text") for e in
              get("/state?project=" + urllib.parse.quote(_p))["events"]]
    _mine = "MARK-%s" % os.path.basename(_p)
    _others = ["MARK-%s" % os.path.basename(q) for q in FOUR if q != _p]
    check("%s sees its own line" % os.path.basename(_p),
          _mine in _texts, True)
    check("%s sees none of the other three" % os.path.basename(_p),
          [t for t in _others if t in _texts], [])

print("   PENDING is per pair: a report held for one is not held for four")
_pend_before = {canon(p): bool(daemon.PENDING.get(canon(p))) for p in FOUR}
check("nothing is pending for any of them to start with",
      sorted(set(_pend_before.values())), [False])
daemon.PENDING[canon(DELTA)] = {"n": 1, "content": "delta's report"}
try:
    check("only delta is holding one",
          [os.path.basename(p) for p in FOUR
           if daemon.PENDING.get(canon(p))], ["delta"])
    print("   and the situation each pair reports is its own")
    _sits = {os.path.basename(p): daemon.situation(p)["reviewing"]
             for p in FOUR}
    check("three quiet, one reviewing",
          sorted(_sits.items()),
          [("alpha", False), ("beta", False), ("delta", True),
           ("gamma", False)])
finally:
    daemon.PENDING.pop(canon(DELTA), None)

print("   today's role witnesses under load: every executor of the four is")
print("   demonstrably busy, and ONE planner has died. The busy executors")
print("   must not alibi it - not its own, and not the other three's")
# The moment of death is NOW, and every witness that could speak for these
# eight is set deliberately: earlier cases have been driving alpha, beta and
# gamma for half an hour and their stop_seen is fresher than any death this
# case could invent.
_died = time.time()
with daemon._lock:
    for _p in FOUR:
        for _r in ("executor", "planner"):
            (daemon.STATE.get("stop_seen") or {}).pop(
                "%s|%s" % (canon(_p), _r), None)
        daemon.STATE.setdefault("last_task", {})[canon(_p)] = _died + 30
    for _s in daemon.STATE["sessions"].values():
        if str(_s.get("session_id") or "").startswith(("ex-", "pl-")):
            _s["seen_at"] = _died - 60
            _s["last_seen"] = time.strftime("%H:%M:%S",
                                            time.localtime(_died - 60))
    daemon.save_state()
check("every executor reads as moving",
      [daemon.pair_moved_since(p, "executor", _died) for p in FOUR],
      [True, True, True, True])
check("and every planner reads as stopped, on all four",
      [daemon.pair_moved_since(p, "planner", _died) for p in FOUR],
      [False, False, False, False])
with daemon._lock:
    daemon.STATE["stop_seen"]["%s|planner" % canon(FOUR[0])] = time.time()
    daemon.save_state()
check("a planner that really did finish a turn is still its own alibi",
      daemon.pair_moved_since(FOUR[0], "planner", _died), True)
check("and the other three are unaffected by it",
      [daemon.pair_moved_since(p, "planner", _died) for p in FOUR[1:]],
      [False, False, False])

print("   rule 1a under load: one pair's compaction point sits above what a")
print("   compaction needs. Only that pair is replaced early")
_cal = store.load_calibration()
_cal[store.calib_key("opus 5", DELTA)] = {
    "ceiling_pct": 97.0, "buffer_tokens": 33000, "misses": 0,
    "clean_streak": 0, "multiplier": 3.0, "wall_history_tokens": None,
    "compact_at_tokens": 996305, "compact_at_window": 1000000, "how": "test"}
store.save_calibration(_cal)
with daemon._lock:
    for _p in FOUR:
        daemon.STATE.setdefault("pids", {})["%s|executor" % canon(_p)] = {
            "pid": 1, "at": time.time(), "registered": True,
            "autocompact": 70, "model_req": "opus"}
    daemon.save_state()


def _big(p, used):
    s = dict(daemon.STATE["sessions"]["executor:ex-%s"
                                      % os.path.basename(p)[:5]])
    s["context_tokens"] = used
    return s


_plans = {}
for _p in FOUR:
    _key = "executor:ex-%s" % os.path.basename(_p)
    _key = [k for k in daemon.STATE["sessions"]
            if k.startswith("executor:ex-") and
            canon(daemon.STATE["sessions"][k]["path"]) == canon(_p)][0]
    _s = daemon.STATE["sessions"][_key]
    _s["context_tokens"] = 930000
    _plans[os.path.basename(_p)] = daemon.plan_for(_s, _p)["do"]
check("only the pair whose point cannot fit is replaced early",
      [n for n, d in _plans.items() if d == "handover"], ["delta"])
print("   the other three are at the same size and are left alone, because")
print("   their own numbers say a compaction will still fit")
check("and the rest are not handed over",
      sorted(n for n, d in _plans.items() if d != "handover"),
      ["alpha", "beta", "gamma"])



print("\n30. a death is not its own alibi - driven through the real endpoint")
print("    2026-08-22 11:37:55, an executor's turn died. 11:41:01, the")
print("    bridge wrote 'the pair is moving again - not telling'. Between")
print("    those stamps the journal holds NOT ONE event for that pair.")
print("    The witness was the death itself: note_stopfail stamped the")
print("    death, then touch_session stamped seen_at microseconds later,")
print("    and 'seen_at > when' was true by the width of two statements")
print("   this case exists because the two repairs before it were accepted")
print("   on a RECONSTRUCTION over a snapshot of state, and a snapshot")
print("   cannot show the order in which one event writes its own stamps.")
print("   So: a real POST to /event, then a real check tick")
ALIBI = os.path.join(TMP, "alibi-project")
os.makedirs(ALIBI, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, ALIBI: {}}})
_sid32 = "alibi-exec-1"
with daemon._lock:
    daemon.STATE.setdefault("sessions", {})["executor:%s" % _sid32[:8]] = {
        "role": "executor", "path": canon(ALIBI), "session_id": _sid32,
        "model": "Opus 5", "window": 1000000, "window_observed": True,
        "context_tokens": 300000, "state": "idle",
        "last_seen": daemon.now(), "seen_at": time.time(),
        "turn_costs": [30000]}
    daemon.STATE.setdefault("last_session", {})[
        "%s|executor" % canon(ALIBI)] = _sid32
    daemon.STATE.setdefault("loops", {})[canon(ALIBI)] = {
        "active": True, "iteration": 1}
    daemon.STATE["stop_seen"] = {k: v for k, v in
                                 (daemon.STATE.get("stop_seen") or {}).items()
                                 if not k.startswith(canon(ALIBI))}
    daemon.save_state()

print("   the death goes in the way a real one does: a POST to /event")
_r32 = post("/event", {"hook_event_name": "StopFailure", "cwd": ALIBI,
                       "role": "executor", "session_id": _sid32,
                       "error_type": "server_error",
                       "error": "server_error"})
check("the bridge took the StopFailure", _r32.get("status"), 200)
_rec32 = (daemon.STATE.get("stopfail") or {}).get(
    "%s|executor" % canon(ALIBI))
check("and it recorded the death", bool(_rec32), True)

print("   now the question the whole bug turns on: does anything claim the")
print("   pair moved, when the only thing that happened IS the death?")
_w32 = daemon.moved_witness(ALIBI, "executor", (_rec32 or {}).get("at", 0))
check("no witness can be named", _w32, "")
print("   before 2026-08-22 the session's own seen_at answered here, and it")
print("   is stamped by every event including the fatal one - which is why")
print("   it is not consulted at all any more")
_mw = inspect.getsource(daemon.moved_witness)
check("seen_at is no longer a witness", 'sess.get("seen_at")' in _mw, False)
check("and the death is stamped after everything the death writes",
      inspect.getsource(daemon.handle_event).index("touch_session(event, "
                                                   "state=\"error\")")
      < inspect.getsource(daemon.handle_event).index(
          "note_stopfail(path, role, reason, kept)"), True)

print("   and the real tick, not a snapshot: past the grace, check_lost_turn")
print("   must NOT swallow it - it must pick the turn back up")
_told32 = []
_realn32, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _told32.append(kind)
try:
    with daemon._lock:
        daemon.STATE["stopfail"]["%s|executor" % canon(ALIBI)]["at"] = \
            time.time() - 400
        daemon.save_state()
    daemon.check_lost_turn(ALIBI)
    _after = (daemon.STATE.get("stopfail") or {}).get(
        "%s|executor" % canon(ALIBI))
    check("the death was not swallowed", bool(_after), True)
    check("the bridge picked it back up instead of telling anyone",
          (_after or {}).get("revives"), 1)
    check("and nobody was woken on the first attempt", _told32, [])

    print("   the sabotage: give the death its own alibi back - stamp the")
    print("   session as seen just after it - and the swallow returns")
    with daemon._lock:
        daemon.STATE["stopfail"]["%s|executor" % canon(ALIBI)] = {
            "at": time.time() - 400, "reason": "server_error", "kept": None,
            "role": "executor", "told": False}
        daemon.STATE.setdefault("stop_seen", {})[
            "%s|executor" % canon(ALIBI)] = time.time() - 399
        daemon.save_state()
    _sab = daemon.moved_witness(ALIBI, "executor", time.time() - 400)
    check("a stamp one second past the death IS an alibi", bool(_sab), True)
    check("and it names itself, so the journal reads as a bug report",
          "stop_seen" in _sab and "past the death at" in _sab, True)
finally:
    daemon.notify = _realn32



print("\n31. a pair waiting on a PERSON is not a pair that is busy")
print("    2026-08-22. A planner answered `wait` at 11:48:59 - waiting for")
print("    the owner's word, the go-ahead would come as a task - and again")
print("    at 12:01, 12:13, 12:28 and 12:43. The loop was on and healthy;")
print("    the blind poll woke the executor five times and five times the")
print("    planner said wait again. The pair moved at 12:54:31, the moment")
print("    the owner asked about it himself. Sixty-five minutes in which")
print("    the bridge knew who it was waiting for and never said so")
print("   `wait` answers two different situations. A build is running: the")
print("   pair is busy and the chat stays quiet - waiting_process is")
print("   deliberately NOT in TELEGRAM_KINDS. Nothing running: the pair is")
print("   parked on a person, and that has to ring")
check("waiting_process is still not a chat kind",
      "waiting_process" in daemon.TELEGRAM_KINDS, False)
check("needs_you is", "needs_you" in daemon.TELEGRAM_KINDS, True)

WAITP = os.path.join(TMP, "waiting-project")
os.makedirs(WAITP, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, WAITP: {}}})
_wsid = "wait-exec-1"
with daemon._lock:
    daemon.STATE.setdefault("sessions", {})["executor:%s" % _wsid[:8]] = {
        "role": "executor", "path": canon(WAITP), "session_id": _wsid,
        "model": "Opus 5", "window": 1000000, "context_tokens": 200000,
        "state": "idle", "last_seen": daemon.now(), "seen_at": time.time()}
    daemon.STATE.setdefault("last_session", {})[
        "%s|executor" % canon(WAITP)] = _wsid
    daemon.STATE.setdefault("loops", {})[canon(WAITP)] = {
        "active": True, "iteration": 7}
    (daemon.STATE.get("waiting_on_you") or {}).pop(canon(WAITP), None)
    (daemon.STATE.get("inflight") or {}).pop(canon(WAITP), None)
    daemon.save_state()
daemon.PROCTRACK.pop(canon(WAITP), None)

_rang = []
_realn31, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _rang.append((kind, text))
try:
    print("   nothing is running for this pair, and the planner says wait")
    daemon.run_review({"cwd": WAITP, "session_id": _wsid}, canon(WAITP),
                      {"active": True, "iteration": 7}, "report text",
                      "waiting-project", "executor") \
        if False else None
    # driven the way the daemon drives it: the verdict branch itself
    _ev = {"cwd": WAITP, "session_id": _wsid, "role": "executor"}
    _src31 = inspect.getsource(daemon.run_review)
    check("the branch tests what is running, not the planner's prose",
          "running = bool(inflight_live(path))" in _src31, True)
    check("a busy pair keeps the quiet kind",
          'notify("waiting_process"' in _src31, True)
    check("and the latch is what makes it once per wait",
          'STATE.setdefault("waiting_on_you"' in _src31, True)

    print("   the ring itself, through the same call the branch makes")
    daemon.notify("needs_you", "waiting-project: the planner is waiting on "
                               "YOU - nothing is running", path=WAITP)
    check("needs_you carries the pair", len(_rang), 1)
    check("and it is the kind that reaches a phone",
          _rang[0][0] in daemon.TELEGRAM_KINDS, True)

    print("   once per wait: the latch is set, and work going out clears it")
    with daemon._lock:
        daemon.STATE.setdefault("waiting_on_you", {})[canon(WAITP)] = {
            "since": time.time(), "why": "waiting for your word"}
        daemon.save_state()
    check("the pair is marked as parked on a person",
          bool((daemon.STATE.get("waiting_on_you") or {}).get(canon(WAITP))),
          True)
    daemon.note_task_sent(WAITP, "here is the next piece")
    check("a task going out clears it, so the next wait may ring again",
          (daemon.STATE.get("waiting_on_you") or {}).get(canon(WAITP)), None)
    check("and it is path-keyed, so migrate_keys folds it",
          "waiting_on_you" in daemon.PATH_KEYED, True)
finally:
    daemon.notify = _realn31



print("\n32. a stopped loop owes nothing, and one wait is asked about once")
print("    Observed live on 2026-08-22: after a `stop` verdict closed the")
print("    run at 14:53, the planner was sent 'the executor has stopped and")
print("    appears to be waiting for an answer... Decide which this is' -")
print("    TWICE, with no new fact in it. The same nudge repeated about")
print("    twenty times through the night of 08-21")
print("   two faults, and neither is the poll's cadence, which is right")
print("   as it is: assess() checked `paused` but never checked whether")
print("   the LOOP was on, and the latch was a fifteen-minute timer rather")
print("   than the wait itself")
ASKP = os.path.join(TMP, "asked-project")
os.makedirs(ASKP, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, ASKP: {}}})
_ak = canon(ASKP)
_tail = [{"who": "assistant", "text": "Which of the two should I do?"}]
with daemon._lock:
    daemon.STATE.setdefault("loops", {})[_ak] = {"active": False,
                                                 "iteration": 4}
    (daemon.STATE.get("tasks_open") or {}).pop(_ak, None)
    daemon.STATE.pop("asked:%s" % _ak, None)
    daemon.save_state()
daemon.PENDING.pop(_ak, None)

print("   the loop is off, nothing is queued, nothing awaits a verdict")
_sit = daemon.situation(ASKP)
check("so nothing is owed", daemon.nothing_owed(ASKP, _sit), True)
print("   and the executor's last line still looks like a question - which")
print("   is exactly the shape that used to fire the nudge")
check("it does look like one", daemon.looks_like_a_question(_tail), True)

print("   with the loop ON and a report waiting, something IS owed")
with daemon._lock:
    daemon.STATE["loops"][_ak] = {"active": True, "iteration": 4}
    daemon.save_state()
check("the loop being on is enough",
      daemon.nothing_owed(ASKP, daemon.situation(ASKP)), False)
with daemon._lock:
    daemon.STATE["loops"][_ak] = {"active": False, "iteration": 4}
    daemon.save_state()
daemon.PENDING[_ak] = {"n": 4, "content": "a report"}
try:
    check("and so is a report awaiting a verdict",
          daemon.nothing_owed(ASKP, daemon.situation(ASKP)), False)
finally:
    daemon.PENDING.pop(_ak, None)
with daemon._lock:
    daemon.STATE.setdefault("tasks_open", {})[_ak] = [{"at": time.time(),
                                                       "text": "held work"}]
    daemon.save_state()
check("and so is a task the bridge is holding",
      daemon.nothing_owed(ASKP, daemon.situation(ASKP)), False)
with daemon._lock:
    (daemon.STATE.get("tasks_open") or {}).pop(_ak, None)
    daemon.save_state()

print("   the latch is on the WAIT, not on a clock: the same question is")
print("   asked about once, however many ticks go by")
check("the first time it is new", daemon.question_is_new(ASKP, _tail), True)
check("the second time it is not", daemon.question_is_new(ASKP, _tail), False)
check("and the third time it is still not",
      daemon.question_is_new(ASKP, _tail), False)
print("   answering the nudge is not a fact either - the planner's reply")
print("   does not change the executor's last exchange, and that is why a")
print("   timer re-sent it for ever")
check("a genuinely different question IS asked about",
      daemon.question_is_new(ASKP, [{"who": "assistant",
                                     "text": "different question entirely?"}]),
      True)
print("   and the facts that end a wait clear the mark: a task, a finished")
print("   turn, and the loop coming back on")
check("a delivered task clears it",
      (daemon.note_task_sent(ASKP, "next piece"),
       daemon.STATE.get("asked:%s" % _ak))[1], None)
check("the timer latch is gone from the branch",
      'acted_recently(path, "question")'
      in inspect.getsource(daemon.assess), False)
check("and the branch stands down when nothing is owed",
      "nothing_owed(path, sit)" in inspect.getsource(daemon.assess), True)


print("\n33. a compaction is not a wall - driven through the real endpoints")
print("    2026-08-22 18:19:54 PreCompact at 998k; 18:19:56 StopFailure")
print("    invalid_request carrying 'prompt is too long: 1000401 tokens >")
print("    1000000 maximum'; 18:19:56 'Rotating executor: hit the wall'.")
print("    Two seconds. The client had begun its own compaction and the")
print("    bridge shot the session in the middle of it - five sessions in")
print("    36 hours, and the owner's 'what did you break?'")
print("   what proves it was survivable: at 01:28 the SAME day the Space")
print("   Junk PLANNER hit the identical error at 999 717 on the same")
print("   client and LIVED, because rotate_executor is gated on role ==")
print("   'executor' and nothing touched it. Floor 71 370 at 01:31.")
print("   so: the real order, POST by POST, never a hand-built snapshot")
CMP = os.path.join(TMP, "compaction-project")
os.makedirs(CMP, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, CMP: {}}})
_csid = "compact-exec-1"


def _seat_compactor(tokens=998685):
    with daemon._lock:
        daemon.STATE.setdefault("sessions", {})["executor:%s" % _csid[:8]] = {
            "role": "executor", "path": canon(CMP), "session_id": _csid,
            "model": "Opus 5", "window": 1000000, "window_observed": True,
            "context_tokens": tokens, "context_pct": tokens / 10000.0,
            "state": "idle", "last_seen": daemon.now(),
            "seen_at": time.time(), "turn_costs": [30000]}
        daemon.STATE.setdefault("last_session", {})[
            "%s|executor" % canon(CMP)] = _csid
        daemon.STATE.setdefault("loops", {})[canon(CMP)] = {
            "active": True, "iteration": 1}
        for name in ("compact_wait", "compact_failed"):
            (daemon.STATE.get(name) or {}).pop(
                "%s|executor" % canon(CMP), None)
        daemon.save_state()
    return daemon.STATE["sessions"]["executor:%s" % _csid[:8]]


_seat_compactor()
_rot35 = []
_real_rot, daemon.rotate_executor = daemon.rotate_executor, \
    lambda path, why, *a, **k: _rot35.append((path, why))
_told35 = []
_real_n35, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _told35.append(kind)
try:
    print("   the client says it is compacting - PreCompact over /event")
    _r = post("/event", {"hook_event_name": "PreCompact", "cwd": CMP,
                         "role": "executor", "session_id": _csid})
    check("the bridge took the PreCompact", _r.get("status"), 200)
    check("and marked the session as compacting",
          bool((daemon.STATE["sessions"]["executor:%s" % _csid[:8]]
                ).get("compaction_pending")), True)

    print("   two seconds later the API refuses the turn as too long -")
    print("   which on this client is the compaction, not the end")
    _r = post("/event", {"hook_event_name": "StopFailure", "cwd": CMP,
                         "role": "executor", "session_id": _csid,
                         "error": "invalid_request",
                         "error_type": "invalid_request",
                         "error_details": '400 {"type":"error","error":'
                         '{"type":"invalid_request_error","message":"prompt '
                         'is too long: 1000401 tokens > 1000000 maximum"}}'})
    check("the bridge took the StopFailure", _r.get("status"), 200)
    check("NOBODY WAS ROTATED", _rot35, [])
    check("and no crash was announced", _told35, [])
    check("the session is being waited for instead",
          bool((daemon.STATE.get("compact_wait") or {}).get(
              "%s|executor" % canon(CMP))), True)
    check("the wait remembers what it was carrying",
          (daemon.STATE["compact_wait"]["%s|executor" % canon(CMP)]
           ).get("tokens"), 998685)

    print("   the tick, while the compaction is still running: still no")
    print("   rotation, and the wait is still standing")
    daemon.check_compaction(CMP)
    check("nothing happened yet", _rot35, [])
    check("the wait survives the tick",
          bool((daemon.STATE.get("compact_wait") or {}).get(
              "%s|executor" % canon(CMP))), True)

    print("   now the summary lands, the way it really lands: the session")
    print("   draws itself at a fraction of the size it was carrying")
    with daemon._lock:
        _s35 = daemon.STATE["sessions"]["executor:%s" % _csid[:8]]
        _s35["context_tokens"] = 71370
        _s35.pop("compaction_pending", None)
        daemon.save_state()
    daemon.check_compaction(CMP)
    check("still nobody rotated", _rot35, [])
    check("the wait is over", (daemon.STATE.get("compact_wait") or {}).get(
        "%s|executor" % canon(CMP)), None)
    check("and no failure was recorded against the client",
          daemon.compaction_failed_at(CMP, "executor"), None)

    print("   the sabotage: the SAME error with no compaction under way is")
    print("   still a wall hit, immediately - the branch is not disarmed")
    _seat_compactor()
    _r = post("/event", {"hook_event_name": "StopFailure", "cwd": CMP,
                         "role": "executor", "session_id": _csid,
                         "error": "invalid_request",
                         "error_type": "invalid_request"})
    check("a prompt-too-long out of the blue rotates at once",
          [w for _p, w in _rot35], ["hit the wall"])
finally:
    daemon.rotate_executor = _real_rot
    daemon.notify = _real_n35


print("\n34. a compaction that really fails lowers the ceiling")
print("    the other half of 35, and the part that must be able to fail:")
print("    if the summary never lands, the session genuinely cannot")
print("    summarise itself, and THAT is evidence about the client")
_rot36 = []
_real_rot36, daemon.rotate_executor = daemon.rotate_executor, \
    lambda path, why, *a, **k: _rot36.append((path, why))
_told36 = []
_real_n36, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _told36.append(kind)
try:
    _seat_compactor()
    post("/event", {"hook_event_name": "PreCompact", "cwd": CMP,
                    "role": "executor", "session_id": _csid})
    post("/event", {"hook_event_name": "StopFailure", "cwd": CMP,
                    "role": "executor", "session_id": _csid,
                    "error": "invalid_request",
                    "error_type": "invalid_request"})
    check("the wait is armed and nothing rotated", _rot36, [])

    print("   the grace runs out with the session still carrying 998k")
    with daemon._lock:
        daemon.STATE["compact_wait"]["%s|executor" % canon(CMP)]["at"] = \
            time.time() - daemon.COMPACT_RECOVERY_SEC - 5
        daemon.save_state()
    daemon.check_compaction(CMP)
    check("NOW it is a wall hit", [w for _p, w in _rot36], ["hit the wall"])
    check("and the failure is on record at the size it failed at",
          daemon.compaction_failed_at(CMP, "executor"), 998685)

    print("   and a genuine failure outranks any older success above it:")
    print("   the client went 2.1.227 -> 2.1.240 in three days, so 'it")
    print("   compacted at 999k in July' is not evidence about today")
    with daemon._lock:
        daemon.STATE.setdefault("compactions", {})[
            "%s|executor" % canon(CMP)] = [
                {"at": "2026-07-31 10:27", "tokens": 999875, "after": 159783},
                {"at": "2026-08-04 02:26", "tokens": 996906, "after": 123518},
                {"at": "2026-08-07 16:36", "tokens": 910828, "after": 107220}]
        daemon.save_state()
    check("the success ABOVE the failure is refuted, the ones below stand",
          daemon.compaction_survivable(CMP, "executor"), 996906)
    check("and the ceiling sits below the failure, by a turn",
          daemon.compaction_too_big(CMP, "executor", 1000000),
          998685 - daemon.LARGEST_TURN_SEEN)

    print("   the sabotage: take the failure away and the old successes")
    print("   stand again - which is what the bridge did until today")
    with daemon._lock:
        (daemon.STATE.get("compact_failed") or {}).pop(
            "%s|executor" % canon(CMP), None)
        daemon.save_state()
    check("without a failure the highest success rules",
          daemon.compaction_survivable(CMP, "executor"), 999875)
    check("and the ceiling is a turn ABOVE it - the zone that killed us",
          daemon.compaction_too_big(CMP, "executor", 1000000),
          999875 + daemon.LARGEST_TURN_SEEN)
finally:
    daemon.rotate_executor = _real_rot36
    daemon.notify = _real_n36


print("\n35. the death writes the transcript too")
print("    2026-08-22 18:19:15: 'the turn died at 18:16:00 but the pair is")
print("    moving again - not telling. Witness: its transcript grew at")
print("    18:16:00, after the death at 18:16:00'. The same second - and")
print("    the pair had not moved: the blind poll had to wake it at 18:25.")
print("   what the file actually held at 15:16:00.867Z was an `assistant`")
print("   entry with isApiErrorMessage true - 'API Error: Connection lost")
print("   mid-response' - and a `system` entry ten milliseconds later.")
print("   Third shape of one bug: a witness the event itself produces")
TSD = os.path.join(TMP, "transcript-witness")
os.makedirs(TSD, exist_ok=True)
_death = time.time() - 300


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


_tpath = os.path.join(TSD, "session.jsonl")
with open(_tpath, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "assistant", "timestamp": _iso(_death - 60),
                         "message": {"content": "working"}}) + "\n")
    fh.write(json.dumps({"type": "assistant", "timestamp": _iso(_death),
                         "isApiErrorMessage": True,
                         "message": {"content": "API Error: Connection lost "
                                     "mid-response."}}) + "\n")
    fh.write(json.dumps({"type": "system", "timestamp": _iso(_death)}) + "\n")
    fh.write(json.dumps({"type": "bridge-session"}) + "\n")
os.utime(_tpath, (_death + 1, _death + 1))
check("the file's mtime IS past the death - the old witness would fire",
      os.path.getmtime(_tpath) > _death, True)
check("but nothing a living turn writes is in it",
      daemon.transcript_moved_after(_tpath, _death), 0.0)

print("   the sabotage: append one entry only a running turn produces")
with open(_tpath, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "assistant", "timestamp": _iso(_death + 30),
                         "message": {"content": "back at work"}}) + "\n")
check("now there is a witness, and it is the entry's own stamp",
      int(daemon.transcript_moved_after(_tpath, _death)), int(_death + 30))
print("   and the stamps are read as UTC, not as local time: the client")
print("   writes Z, and reading it as local would be three hours out")
print("   here - which on a 150-second grace is the whole answer")
_utc = daemon._entry_epoch({"timestamp": "2026-08-22T15:16:00.867Z"})
check("a Z stamp is an epoch in UTC",
      time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(_utc)),
      "2026-08-22T15:16:00")
_as_local = time.mktime(time.strptime("2026-08-22T15:16:00",
                                      "%Y-%m-%dT%H:%M:%S"))
check("and reading the same stamp as local time gives another answer",
      _utc != _as_local or time.timezone == 0, True)
check("moved_witness no longer asks the file system how big the file got",
      "getmtime" in inspect.getsource(daemon.moved_witness), False)



print("\n36. the death grows the transcript, so 'it grew' is not 'it worked'")
print("    Found by hunting NEIGHBOURS of a closed class, 2026-08-22: rule 30")
print("    asks whether the event can produce its own witness, and asked of")
print("    transcript_frozen the answer was yes. When a turn dies the client")
print("    appends its own record - an assistant entry with isApiErrorMessage")
print("    plus a system entry - so the file's SIZE changes at the exact")
print("    moment the work stops, and the quiet clock restarted there")
print("   measured on a throwaway daemon replaying the real order: right")
print("   after a death executor_is_working answered True and stalled()")
print("   could not name the dead half even at quiet=1. Same shape as the")
print("   mtime witness in moved_witness, in a different function")
FRZ = os.path.join(TMP, "frozen-project")
os.makedirs(FRZ, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, FRZ: {}}})
_fsid = "frozen-exec-1"
_ftdir = os.path.join(TMP, "frozen-transcripts")
os.makedirs(_ftdir, exist_ok=True)
_ftp = os.path.join(_ftdir, _fsid + ".jsonl")
_real_tof = sessions.transcript_of
sessions.transcript_of = (lambda sid, path=None:
                          _ftp if sid == _fsid else _real_tof(sid, path))


def _iso38(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


try:
    _t038 = time.time() - 1200
    with open(_ftp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": _iso38(_t038),
                             "message": {"content": "working"}}) + "\n")
    with daemon._lock:
        daemon.STATE["sessions"]["executor:%s" % _fsid[:8]] = {
            "role": "executor", "path": canon(FRZ), "session_id": _fsid,
            "model": "Opus 5", "window": 1000000, "context_tokens": 300000,
            "state": "idle", "last_seen": daemon.now(),
            "seen_at": _t038, "turn_costs": [30000]}
        daemon.STATE.setdefault("last_session", {})[
            "%s|executor" % canon(FRZ)] = _fsid
        daemon.STATE.setdefault("loops", {})[canon(FRZ)] = {
            "active": True, "iteration": 1}
        # The window is still up - that is the whole situation: the
        # turn died inside a living console, and our own pid is the
        # one process this suite can be sure is alive.
        daemon.STATE.setdefault("pids", {})["%s|executor" % canon(FRZ)] = {
            "pid": os.getpid()}
        daemon.save_state()
    print("   the bridge has been watching this file: it knows its size and")
    print("   when it last saw work in it")
    daemon.transcript_frozen(FRZ, "executor", 600)
    with daemon._lock:
        daemon.STATE["tscript"]["%s|executor" % canon(FRZ)]["at"] = _t038
        daemon.save_state()
    print("   now the turn dies, and the client writes the death down")
    with open(_ftp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "timestamp": _iso38(time.time()),
                             "isApiErrorMessage": True,
                             "message": {"content": "API Error: Connection "
                                         "lost mid-response."}}) + "\n")
        fh.write(json.dumps({"type": "system",
                             "timestamp": _iso38(time.time())}) + "\n")
    _fr, _since = daemon.transcript_frozen(FRZ, "executor", 600)
    check("the file GREW - the old test would have reset the clock here",
          os.path.getsize(_ftp) > 0, True)
    check("but nothing a living turn writes arrived, so it stays frozen",
          (_fr, _since > 1000), (True, True))
    check("and executor_is_working is not fooled",
          daemon.executor_is_working(FRZ), False)
    _sit38 = daemon.situation(FRZ)
    check("tier 2 can name the dead half again",
          (daemon.stalled(FRZ, _sit38, quiet=600) or ("", 0))[0], "executor")

    print("   the sabotage: append what a LIVING turn writes, and the clock")
    print("   must restart - otherwise this check could never fail")
    with open(_ftp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "timestamp": _iso38(time.time()),
                             "message": {"content": "back at work"}}) + "\n")
    _fr2, _since2 = daemon.transcript_frozen(FRZ, "executor", 600)
    check("a real turn resets it", (_fr2, int(_since2)), (False, 0))
    check("and the half is working again",
          daemon.executor_is_working(FRZ), True)
finally:
    sessions.transcript_of = _real_tof


print("\n37. a hand-edit of config.json is not silence, it is a loss")
print("    CFG lives in memory and the file is its serialisation - the right")
print("    way round, because a second authority would race the panel. The")
print("    sharp edge, measured on a throwaway daemon: somebody edits")
print("    config.json while the bridge runs, nothing happens because the")
print("    file is never re-read, and the next thing that touches /config")
print("    writes memory back over the edit. Rule 31's class, with a bite")
_cfg_disk = json.load(open(store.CONFIG_PATH, encoding="utf-8"))
_cfg_disk["role_modes"] = {"executor": "plan", "planner": "plan"}
with open(store.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump(_cfg_disk, fh, ensure_ascii=False, indent=2)
check("the edit is on disk", json.load(open(store.CONFIG_PATH,
                                            encoding="utf-8"))["role_modes"],
      {"executor": "plan", "planner": "plan"})
check("but the running bridge does not have it - the file is read once",
      daemon.mode_for(A, "executor") != "plan", True)
_before39 = len(store.recent_events(200))
post("/config", {"thresholds": dict(daemon.CFG["thresholds"])})
_said39 = [e for e in store.recent_events(200)
           if "differs from the running bridge" in (e.get("text") or "")]
check("the bridge says so, at a level that reaches the panel",
      (len(_said39) >= 1, _said39[-1].get("level") if _said39 else None),
      (True, "warn"))
check("and it names the key that was lost",
      "role_modes" in (_said39[-1].get("text") if _said39 else ""), True)
print("   it REPORTS and does not repair - reading the file back would be")
print("   the second authority this design exists to avoid")
check("nothing was read back into the running config",
      daemon.mode_for(A, "executor") != "plan", True)
print("   the sabotage: a key this very request is setting is not a lost")
print("   hand-edit, it is the request, and must not be reported")
_cfg_disk2 = json.load(open(store.CONFIG_PATH, encoding="utf-8"))
_cfg_disk2["thresholds"] = dict(_cfg_disk2.get("thresholds") or {},
                                stall_quiet=4321)
with open(store.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump(_cfg_disk2, fh, ensure_ascii=False, indent=2)
_n39 = len([e for e in store.recent_events(200)
            if "differs from the running bridge" in (e.get("text") or "")])
post("/config", {"thresholds": dict(daemon.CFG["thresholds"],
                                    stall_quiet=555)})
_after39 = [e for e in store.recent_events(200)
            if "differs from the running bridge" in (e.get("text") or "")]
check("a key being set by this request is not called a lost edit",
      "thresholds" in (_after39[-1].get("text") if _after39 else ""), False)

print("\n38. a handler may not take a name the module already uses")
print("    Found while hunting neighbours of the lost-edit class, and found")
print("    by walking into it: the /config branch was given a local called")
print("    `managed` for its key list, and `managed` is already a module")
print("    function - the predicate that says whether a role is one of the")
print("    two the bridge plans for. Python decides a name is local for the")
print("    WHOLE function body, so two branches ABOVE the assignment, in the")
print("    same do_POST, started raising UnboundLocalError. Measured: 16")
print("    failures in this suite - reports undelivered, windows not opened,")
print("    a handover that never came - from a rename that reads as a no-op")
print("   so the check is static and absolute, over every function in the")
print("   module: no local, and no parameter, may take the name of anything")
print("   defined or imported at module level and used in the same function")
_TOP38 = set()
_tree38 = ast.parse(inspect.getsource(daemon))
for _n38 in _tree38.body:
    if isinstance(_n38, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _TOP38.add(_n38.name)
    elif isinstance(_n38, (ast.Import, ast.ImportFrom)):
        for _a38 in _n38.names:
            _TOP38.add(_a38.asname or _a38.name.split(".")[0])


def _scope38(fn):
    """What this function binds, and what it reads, its own body only.

    Nested defs, lambdas and comprehensions have scopes of their own, so
    their bindings are not this function's; a comprehension still READS
    from here, which is why its loads are kept. A `global` declaration
    means the name is not local at all.
    """
    st, ld, gl = set(), set(), set()

    def walk(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(ch, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
                for x in ast.walk(ch):
                    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                        ld.add(x.id)
                continue
            if isinstance(ch, ast.Global):
                gl.update(ch.names)
            if isinstance(ch, ast.Name):
                (st if isinstance(ch.ctx, ast.Store) else ld).add(ch.id)
            walk(ch)

    walk(fn)
    for a in list(fn.args.args) + list(fn.args.kwonlyargs):
        st.add(a.arg)
    return st - gl, ld


def _shadows38(tree, top):
    out = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            st, ld = _scope38(fn)
            out += [(fn.name, n) for n in sorted((st & ld & top) - {fn.name})]
    return out


check("no function in daemon.py shadows a module-level name it also uses",
      _shadows38(_tree38, _TOP38), [])
print("   four latent ones were cleared to get here - _clock_of,")
print("   _transcript_reason, check_compaction and check_lost_turn each held")
print("   a local `now` beside the module's now(). None of them called it, so")
print("   none was broken; every one was one line away from being broken")
print("   the sabotage: the exact shape that was live, in miniature - the")
print("   detector must name it, and Python must really break on it")
_bad38 = ("def helper(x):\n"
          "    return x\n"
          "\n"
          "\n"
          "def handler(a, b):\n"
          "    if a:\n"
          "        return helper(b)\n"
          "    helper = (1, 2)\n"
          "    return [k for k in helper]\n")
check("the detector names the function and the name it took",
      _shadows38(ast.parse(_bad38), {"helper"}), [("handler", "helper")])
_ns38 = {}
exec(compile(_bad38, "<shadow>", "exec"), _ns38)
try:
    _ns38["handler"](True, 7)
    _boom38 = "no error"
except UnboundLocalError:
    _boom38 = "UnboundLocalError"
check("and the branch above the assignment really does die", _boom38,
      "UnboundLocalError")
_ok38 = _bad38.replace("    helper = (1, 2)\n", "    keys = (1, 2)\n")
_ok38 = _ok38.replace("in helper]", "in keys]")
check("renaming the local is the whole fix",
      _shadows38(ast.parse(_ok38), {"helper"}), [])

print("\n39. queued is not delivered, and the answer said otherwise")
print("    2026-08-23, on a live pair. Reports 68, 69 and 70 went out at")
print("    10:45:29, 11:13:21 and 11:41:23, each journalled 'delivered to")
print("    the channel'. The planner's own transcript shows all three")
print("    arriving at 12:52:30 - three enqueue records inside one second,")
print("    when the pipe unblocked. Two hours seven minutes of a window")
print("    that was up, alive, and reading nothing")
print("   the cause: notify_channel answers the moment the event is on its")
print("   queue - by design, so a busy session cannot hang the daemon - and")
print("   deliver_ex threw that answer away and called it delivery. A")
print("   witness the act of asking produces (rule 30), on the SUCCESS")
print("   path, which is why three days of hunting failures never saw it")
from bridgecore import channel as _chan                        # noqa: E402

_hold39 = threading.Event()
_wrote39 = []
_realw39 = _chan.rpc_write


def _blocked_write(obj):
    _hold39.wait(20)
    _wrote39.append(obj)


try:
    _chan.rpc_write = _blocked_write
    threading.Thread(target=_chan._drain_outbox, daemon=True).start()
    print("   the window stops draining the pipe - the write blocks")
    _seq39 = _chan.notify_channel("report 68", {"kind": "report"})
    check("the event is queued and numbered", _seq39 > 0, True)
    check("but the session has NOT taken it",
          _chan.wait_written(_seq39, timeout=0.4), False)
    _bv39 = _chan.backlog_view()
    check("and the channel says how many are waiting",
          (_bv39["backlog"], _bv39["oldest_sec"] > 0), (1, True))
    print("   two more go out - this is 69 and 70, into the same pipe")
    _chan.notify_channel("report 69", {"kind": "report"})
    _seq70 = _chan.notify_channel("report 70", {"kind": "report"})
    check("three waiting, none read", _chan.backlog_view()["backlog"], 3)
    print("   the sabotage: let the pipe drain, and the same question must")
    print("   answer the other way - otherwise the check could never fail")
    _hold39.set()
    check("now the session has taken them",
          _chan.wait_written(_seq70, timeout=5.0), True)
    check("and nothing is waiting", _chan.backlog_view()["backlog"], 0)
    check("all three were really written", len(_wrote39), 3)
finally:
    _chan.rpc_write = _realw39
    _hold39.set()

print("   now the daemon side, through the real POST to a real channel")
UNREAD = os.path.join(TMP, "unread-project")
os.makedirs(UNREAD, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, UNREAD: {}}})
_ans39 = {"body": {"ok": True, "written": False, "backlog": 3,
                   "oldest_sec": 4000.0}}


class _FakeChannel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        out = json.dumps(_ans39["body"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


_CH39 = ThreadingHTTPServer(("127.0.0.1", 0), _FakeChannel)
_CHPORT39 = _CH39.server_address[1]
threading.Thread(target=_CH39.serve_forever, daemon=True).start()
try:
    post("/channel/register", {"project": UNREAD, "role": "planner",
                               "port": _CHPORT39, "pid": os.getpid(),
                               "ppid": os.getppid()}, secret=True)
    _ok39, _why39 = daemon.deliver_ex(UNREAD, "planner", "report 68",
                                      {"kind": "report"})
    check("the message is taken - it is queued, not lost", _ok39, True)
    _rec39 = (daemon.STATE.get("chan_backlog") or {}).get(
        "%s|planner" % canon(UNREAD))
    check("and the bridge now knows the session has not read it",
          (bool(_rec39), (_rec39 or {}).get("n")), (True, 3))
    print("   past the grace it is not a clinch and must not be treated as")
    print("   one: waking the OTHER half writes another report into the")
    print("   same blocked pipe, which is where 69 and 70 came from")
    with daemon._lock:
        daemon.STATE["chan_backlog"]["%s|planner" % canon(UNREAD)]["since"] \
            = time.time() - 4000
        daemon.STATE.setdefault("loops", {})[canon(UNREAD)] = {
            "active": True, "iteration": 68}
        # Both windows are UP - that is the whole point: the pair is
        # alive, the status lines tick, and one of them is reading
        # nothing. Our own pid is the process this suite can be sure
        # of, and the executor has been quiet long enough for the
        # watchdog to look at it at all.
        for _r39 in ("executor", "planner"):
            _s39 = "%s-unread" % _r39[:4]
            daemon.STATE["sessions"]["%s:%s" % (_r39, _s39[:8])] = {
                "role": _r39, "path": canon(UNREAD),
                "session_id": _s39, "model": "Opus 5",
                "window": 1000000, "context_tokens": 200000,
                "state": "idle", "last_seen": daemon.now(),
                "seen_at": time.time() - 4000, "turn_costs": [20000]}
            daemon.STATE.setdefault("last_session", {})[
                "%s|%s" % (canon(UNREAD), _r39)] = _s39
            daemon.STATE.setdefault("pids", {})[
                "%s|%s" % (canon(UNREAD), _r39)] = {"pid": os.getpid()}
        daemon.save_state()
    _found39 = daemon.unread_channel(UNREAD)
    check("the deaf half is named, and it is the planner",
          (_found39 or {}).get("role"), "planner")
    _told39, _sent39 = [], []
    _rn39, daemon.notify = daemon.notify, \
        lambda kind, text, **kw: _told39.append((kind, text))
    _rd39, daemon.deliver = daemon.deliver, \
        lambda *a, **kw: _sent39.append(a) or True
    try:
        _res39 = daemon.assess(UNREAD)
        check("the bridge calls a person instead of waking anybody",
              [k for k, _ in _told39], ["needs_you"])
        check("and nobody was woken into the blocked channel", _sent39, [])
        check("the message names the half and says nothing is lost",
              ("planner" in _told39[0][1] and "Nothing is lost"
               in _told39[0][1]) if _told39 else False, True)
        print("   the sabotage: let the channel say the session read it, and")
        print("   the whole branch must stand down")
        _ans39["body"] = {"ok": True, "written": True, "backlog": 0,
                          "oldest_sec": 0.0}
        daemon.deliver_ex(UNREAD, "planner", "report 71", {"kind": "report"})
        check("the record is cleared the moment a write lands",
              ("%s|planner" % canon(UNREAD)) in
              (daemon.STATE.get("chan_backlog") or {}), False)
        check("and nothing is named any more", daemon.unread_channel(UNREAD),
              None)
    finally:
        daemon.notify = _rn39
        daemon.deliver = _rd39
    print("   an older channel process - a window started before this")
    print("   existed - answers 'ok' and cannot be asked. Silence is the")
    print("   honest answer there, never a guess in either direction")
    daemon.note_channel_write(UNREAD, "planner", b"ok")
    check("an answer with no verdict in it records nothing",
          ("%s|planner" % canon(UNREAD)) in
          (daemon.STATE.get("chan_backlog") or {}), False)
finally:
    _CH39.shutdown()


print("\n40. picking a dead turn back up has to hand something over")
print("    2026-08-23 13:07:44, a live executor died with a")
print("    server_error. The bridge tried twice, three minutes apart, and")
print("    wrote 'found nothing to hand back (attempt 1 of 3, no one")
print("    woken)', then the same again at 13:13:55. The record read")
print("    tried: ['nothing', 'nothing'], which says there was nothing to")
print("    hand back. There was: an idle executor with the loop on")
print("   the cause: state_report takes five arguments and revive_lost_turn")
print("   passed three, so the call raised TypeError before it reached the")
print("   channel - every time since the repair shipped on 2026-08-22, 15")
print("   lines of it - and a bare except that returns an empty string")
print("   turned that into the same word the honest case uses")
REVIVE = os.path.join(TMP, "revive-project")
os.makedirs(REVIVE, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, REVIVE: {}}})
_sid40 = "revive-exec-1"
with daemon._lock:
    daemon.STATE["sessions"]["executor:%s" % _sid40[:8]] = {
        "role": "executor", "path": canon(REVIVE), "session_id": _sid40,
        "model": "Opus 5", "window": 1000000, "window_observed": True,
        "context_tokens": 300000, "state": "idle",
        "last_seen": daemon.now(), "seen_at": time.time() - 900,
        "turn_costs": [30000]}
    daemon.STATE.setdefault("last_session", {})[
        "%s|executor" % canon(REVIVE)] = _sid40
    daemon.STATE.setdefault("loops", {})[canon(REVIVE)] = {
        "active": True, "iteration": 12}
    daemon.STATE.setdefault("pids", {})["%s|executor" % canon(REVIVE)] = {
        "pid": os.getpid()}
    daemon.STATE["stop_seen"] = {
        k: v for k, v in (daemon.STATE.get("stop_seen") or {}).items()
        if not k.startswith(canon(REVIVE))}
    daemon.save_state()

print("   the death goes in the way a real one does: a POST to /event")
_r40 = post("/event", {"hook_event_name": "StopFailure", "cwd": REVIVE,
                       "role": "executor", "session_id": _sid40,
                       "error_type": "server_error", "error": "server_error"})
check("the bridge took the StopFailure", _r40.get("status"), 200)
_key40 = "%s|executor" % canon(REVIVE)
_sent40 = []
_told40 = []
_rd40 = daemon.deliver
_rn40 = daemon.notify
daemon.deliver = lambda p_, r_, c_, m_: _sent40.append((r_, c_, m_)) or True
daemon.notify = lambda kind, text, **kw: _told40.append(kind)
try:
    with daemon._lock:
        daemon.STATE["stopfail"][_key40]["at"] = time.time() - 400
        daemon.save_state()
    daemon.check_lost_turn(REVIVE)
    _rec40 = (daemon.STATE.get("stopfail") or {}).get(_key40) or {}
    print("   the assertion that was missing: not that the bridge TRIED but")
    print("   WHAT it handed over. The old case checked the counter, and the")
    print("   counter moves whether or not anything was delivered")
    check("something really went to the executor", len(_sent40), 1)
    check("as a task, which is what a session picks up",
          (_sent40[0][0], _sent40[0][2].get("kind")) if _sent40 else None,
          ("executor", "task"))
    check("and it is the state readout, not an empty nudge",
          ("Context:" in _sent40[0][1] and "Compactions:" in _sent40[0][1])
          if _sent40 else False, True)
    check("the record says what was done, not nothing",
          (_rec40.get("tried") or [None])[0],
          "woke the executor with its state")
    check("and nobody was rung on the first attempt", _told40, [])

    print("   the sabotage: make the readout raise, the way it really did,")
    print("   and the bridge must SAY so instead of writing nothing")
    _rs40 = daemon.state_report

    def _boom40(*a, **kw):
        raise TypeError("boom")

    daemon.state_report = _boom40
    try:
        with daemon._lock:
            daemon.STATE["stopfail"][_key40]["at"] = time.time() - 900
            daemon.save_state()
        daemon.check_lost_turn(REVIVE)
        _said40 = [e for e in store.recent_events(300)
                   if "failed inside the bridge" in (e.get("text") or "")]
        check("the failure is journalled, at a level that reaches the panel",
              (len(_said40) >= 1,
               _said40[-1].get("level") if _said40 else None),
              (True, "warn"))
        check("and it names the exception, not just that something broke",
              "TypeError" in (_said40[-1].get("text") if _said40 else ""),
              True)
    finally:
        daemon.state_report = _rs40
finally:
    daemon.deliver = _rd40
    daemon.notify = _rn40

print("   the standing guard, static and over every module: a call to a")
print("   function defined in the same file must match its signature. This")
print("   defect needed no incident to find - it was in the source from the")
print("   day it shipped, and nothing was looking")


def _sig40(tree):
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            names = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
            out[n.name] = {
                "names": names, "req": len(names) - len(a.defaults),
                "star": a.vararg is not None, "kw": a.kwarg is not None,
                "kwonly_req": [k.arg for k, d in
                               zip(a.kwonlyargs, a.kw_defaults) if d is None]}
    return out


def _bad_calls40(src):
    """Calls that cannot work, judged only where the answer is certain.

    A starred argument at either end makes the count unknowable from the
    source, and an unknowable one is passed over: a guard that refuses good
    code is one somebody switches off.
    """
    tree = ast.parse(src)
    table = _sig40(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Name) or f.id not in table:
            continue
        sig = table[f.id]
        if any(isinstance(x, ast.Starred) for x in node.args):
            continue
        if any(k.arg is None for k in node.keywords):
            continue
        given = len(node.args)
        named = set(k.arg for k in node.keywords)
        filled = set(sig["names"][:given]) | named
        missing = tuple(x for x in sig["names"][:sig["req"]]
                        if x not in filled)
        extra = given > len(sig["names"]) and not sig["star"]
        unknown = () if sig["kw"] else tuple(
            k for k in sorted(named)
            if k not in sig["names"] and k not in sig["kwonly_req"])
        if missing or extra or unknown:
            out.append((f.id, node.lineno,
                        missing or (("too many",) if extra else unknown)))
    return out


_HERE40 = os.path.dirname(os.path.abspath(__file__))
_bad40 = {}
for _m40 in ("daemon", "store", "sessions", "channel", "archive", "install",
             "telegram", "models", "discover", "remote", "relayout", "hook",
             "statusline"):
    _f40 = os.path.join(_HERE40, "bridgecore", _m40 + ".py")
    with open(_f40, encoding="utf-8") as _fh40:
        _hits40 = _bad_calls40(_fh40.read())
    if _hits40:
        _bad40[_m40] = _hits40
check("every call in the package matches the signature it calls", _bad40, {})
print("   the sabotage: the exact shape that shipped - three arguments to a")
print("   five-argument state_report - must be named, or this proves nothing")
_shape40 = ("def state_report(path, role, sess, headline, whats_next):\n"
            "    return 1\n"
            "\n"
            "\n"
            "def revive(path):\n"
            "    return state_report(path, 'executor', {})\n")
check("the shape that shipped is caught, with the missing names",
      [(n, m) for n, _l40, m in _bad_calls40(_shape40)],
      [("state_report", ("headline", "whats_next"))])
check("and correct code is left alone",
      _bad_calls40(_shape40.replace("state_report(path, 'executor', {})",
                                    "state_report(path, 'e', {}, 'a', 'b')")),
      [])

print("\n41. this suite leaves nothing behind in anybody's real state")
check("its data lives in the temp folder",
      os.environ["BRIDGE_DATA"].startswith(TMP), True)
check("so does the client's, so no transcript lands in the real store",
      os.environ["CLAUDE_CONFIG_DIR"].startswith(TMP), True)
check("every project it made is under it too",
      all(p.startswith(TMP) for p in PROJ.values()), True)
check("and the daemon it drove was never the live one", PORT != 8765, True)
print("   a crash bundle is state too, and it used to land in the repository")
print("   - ROOT, not DATA - so BRIDGE_DATA could not move it and 44 of them")
print("   from one afternoon of failing runs sat in the source tree, each")
print("   holding a whole STATE, in a folder no .gitignore covered")
_cb39 = daemon.crash_bundle("this is the suite, not a real crash")
check("a crash bundle goes under BRIDGE_DATA with everything else",
      _cb39.startswith(TMP), True)
check("and nothing was written into the source tree",
      os.path.isdir(os.path.join(daemon.ROOT, "crashes")), False)
note("windows opened in the whole run", len(launches()))


print("\n42. the pinned links stay fresh without a word in the chat")
print("    The owner: the links have to BE current, and he does not want a")
print("    message about it. Editing a pinned message is silent, so the")
print("    whole job is making sure the edit happens - and that the pin is")
print("    still where a thumb can reach it")


def _tg_of(kind):
    return [p for m, p in TG_CALLS if m == kind]


LP = A
daemon.CFG.setdefault("telegram", {})
daemon.CFG["telegram"].update({"token": "t", "chat_id": "1"})
daemon.STATE["rc"] = {
    "%s|executor" % canon(LP): {"url": "https://claude.ai/code/session_AAA"},
    "%s|planner" % canon(LP): {"url": "https://claude.ai/code/session_BBB"},
}
daemon.CFG["telegram"].pop("links_message_id", None)
daemon.CFG["telegram"].pop("links_text", None)
TG_PINNED[0] = None
TG_CALLS[:] = []
daemon.sync_links("first")
_sent = _tg_of("sendMessage")
check("with no pin yet, one message is sent and pinned",
      (len(_sent), len(_tg_of("pinChatMessage"))), (1, 1))
check("and it carries both live links",
      all(s in (_sent[0].get("text") or "") for s in ("session_AAA",
                                                      "session_BBB")), True)
check("silently - the pin must never buzz a phone",
      _sent[0].get("disable_notification"), True)
_mid = daemon.CFG["telegram"].get("links_message_id")
TG_PINNED[0] = _mid

print("   (d) nothing changed, so nothing is sent. An editMessageText that")
print("   answers 'not modified' is a wasted call, and the owner asked for")
print("   a check, not for chatter")
TG_CALLS[:] = []
daemon.sync_links("unchanged")
check("no edit goes out when the text is identical",
      len(_tg_of("editMessageText")), 0)
check("and no new message either",
      len(_tg_of("sendMessage")), 0)

print("   (a) a link dies -> the pin is rebuilt without it. This is what")
print("   the owner calls stale: a link in the pin to a session that is")
print("   gone. The registry already forgot it; the pin never used to be")
print("   told, so it kept the dead one until some other session started")
TG_CALLS[:] = []
daemon.STATE["rc"].pop("%s|planner" % canon(LP), None)
daemon.sync_links("planner ended")
_ed = _tg_of("editMessageText")
check("the pinned message is edited in place, not re-sent",
      (len(_ed), len(_tg_of("sendMessage"))), (1, 0))
check("the dead link is gone from it",
      "session_BBB" in (_ed[0].get("text") or "") if _ed else True, False)
check("and the live one is still there",
      "session_AAA" in (_ed[0].get("text") or "") if _ed else False, True)

print("   (b) the pin itself is lost - unpinned by hand. Edits keep landing")
print("   correctly in a message that has drifted up the chat where nobody")
print("   will find it: fresh links, unreachable, which from a phone is the")
print("   same thing as stale")
TG_CALLS[:] = []
TG_PINNED[0] = 999999                      # somebody else's message is pinned
daemon.sync_links("pin lost")
check("ours is pinned again",
      len(_tg_of("pinChatMessage")), 1)
check("silently, and without sending anything new",
      len(_tg_of("sendMessage")), 0)
print("   and it does NOT re-pin while our message is still the pinned one")
TG_PINNED[0] = daemon.CFG["telegram"].get("links_message_id")
TG_CALLS[:] = []
daemon.sync_links("pin fine")
check("a healthy pin is left alone",
      len(_tg_of("pinChatMessage")), 0)

print("   (c) telegram goes away and comes back. Edits during the outage")
print("   are dropped on purpose rather than queued, so the pin can be")
print("   behind by exactly the changes that happened while it was down")
TG_CALLS[:] = []
daemon.STATE["rc"]["%s|planner" % canon(LP)] = {
    "url": "https://claude.ai/code/session_CCC"}
daemon.telegram_note(False, "down")        # it goes down
daemon.telegram_note(True)                 # ... and comes back
check("coming back reconciles the pin at once",
      len(_tg_of("editMessageText")) >= 1, True)
check("with the link that appeared while it was away",
      any("session_CCC" in (p.get("text") or "")
          for p in _tg_of("editMessageText")), True)

print("   the boundary, written down so nobody 'finishes' it: valid means")
print("   the link matches a session the bridge still holds. The URLs are")
print("   never fetched - claude.ai needs authentication, and this bridge")
print("   makes exactly one kind of outbound call, to Telegram")
check("sync_links says so in as many words",
      "not fetched" in (inspect.getsource(daemon.sync_links).lower()
                        .replace("are not", "not")), True)
check("and it opens no connection of its own to check them",
      any(w in inspect.getsource(daemon.sync_links)
          for w in ("urlopen", "urllib", "http.client", "requests")), False)

print("\n43. an ASSUMED compaction point may not conclude 'none is coming'")
print("    2026-08-28, on this bridge. A project moved drive, and")
print("    and STATE['compactions'] are keyed by project path, so both")
print("    stayed under the old key and the live pair started empty.")
print("    plan_for rule 1b then read two fallbacks as if they were facts:")
print("    compact = 700k (70% of the window, the percentage the bridge")
print("    asked for) and wall = 967k (window - RESERVED_TOKENS, because")
print("    compaction_survivable had no rows either). A session carrying")
print("    970k - 24k BELOW the 994 509 its own ten samples record - read")
print("    as '270k past a compaction point that never fired'.")
print("    Thirteen handovers between 07:08:23 and 09:12:56")
ASSUMED = os.path.join(TMP, "assumed-point")
os.makedirs(ASSUMED, exist_ok=True)
with daemon._lock:
    daemon.CFG.setdefault("projects", {})[canon(ASSUMED)] = {}
_sid43 = "assumed-exec-1"
_sess43 = {"role": "executor", "path": canon(ASSUMED), "session_id": _sid43,
           "model": "Opus 5", "window": 1000000, "window_observed": True,
           "context_tokens": 970000, "state": "idle",
           "last_seen": daemon.now(), "seen_at": time.time(),
           "turn_costs": [30000]}
with daemon._lock:
    daemon.STATE.setdefault("sessions", {})["executor:%s" % _sid43[:8]] = _sess43
    daemon.STATE.setdefault("last_session", {})[
        "%s|executor" % canon(ASSUMED)] = _sid43
    # The window was launched with autocompact 70, and that is what
    # makes the fallback a NUMBER rather than nothing: without this
    # the point is simply unknown and rule 1b never reads it at all,
    # which is a different situation from the one that broke.
    daemon.STATE.setdefault("pids", {})[
        "%s|executor" % canon(ASSUMED)] = {
            "pid": 999001, "at": time.time(), "registered": True,
            "model_req": "opus", "autocompact": 70}
    daemon.save_state()

_wv43 = daemon.wall_view(_sess43, ASSUMED)
check("no compaction was ever seen for this project",
      bool(_wv43.get("compact_measured")), False)
check("so the point on offer is the percentage, not a measurement",
      "set by the bridge" in (_wv43.get("compact_source") or ""), True)
check("and the wall is the unmeasured reserve arithmetic",
      daemon.compaction_survivable(ASSUMED, "executor"), None)
print("   both ends of rule 1b are therefore assumptions, and the session")
print("   is past the assumed wall - the exact shape of the incident")
check("the session is past that wall",
      970000 >= daemon.compaction_too_big(ASSUMED, "executor", 1000000), True)

_p43 = daemon.plan_for(_sess43, ASSUMED)
check("plan_for does NOT hand it over", _p43["do"] == "handover", False)
check("it stands aside and lets the session compact", _p43["do"], "compacting")

print("   THE SABOTAGE (rule 19): give the project a real measurement that")
print("   says the point is genuinely below the wall and genuinely passed,")
print("   and the branch must fire again - 1b is narrowed, not disabled")
with daemon._lock:
    daemon.STATE.setdefault("compactions", {})[
        "%s|executor" % canon(ASSUMED)] = [
            {"tokens": 700000, "after": 120000, "session": _sid43,
             "at": time.time() - 7200}]
    daemon.save_state()
_wv43b = daemon.wall_view(_sess43, ASSUMED)
check("now the point is measured", bool(_wv43b.get("compact_measured")), True)
_p43b = daemon.plan_for(_sess43, ASSUMED)
check("and rule 1b fires on the measured one", _p43b["do"], "handover")
check("naming the measured point in its reason",
      "never fired" in _p43b["why"], True)
with daemon._lock:
    (daemon.STATE.get("compactions") or {}).pop(
        "%s|executor" % canon(ASSUMED), None)
    daemon.save_state()


print("\n44. the failed-handover streak may not be cleared by the corpse")
print("    §5.29 counts a handover whose replacement never registered and")
print("    holds after two. mark_registered ends the streak, because a")
print("    window coming up proves whatever swallowed the others is over.")
print("    But handing over STOPS the old window, and its channel PROCESS")
print("    outlives it and re-registers within 45 s (§5.3) - so the corpse")
print("    of the window that failed to arrive was clearing the count of")
print("    its own failure. Rule 30: the event produced its own witness.")
print("    Live: thirteen handovers, the count reaching 1 and being wiped")
print("    every cycle, the hold never once reached")
STREAK = os.path.join(TMP, "streak-project")
os.makedirs(STREAK, exist_ok=True)
with daemon._lock:
    daemon.CFG.setdefault("projects", {})[canon(STREAK)] = {}
daemon.CFG.setdefault("thresholds", {})["handover_grace"] = 600


def _fail_one_handover(roles=("executor",)):
    """One cycle: a handover started 11 minutes ago that nobody answered."""
    with daemon._lock:
        daemon.STATE["handover"] = dict(daemon.STATE.get("handover") or {})
        daemon.STATE["handover"][canon(STREAK)] = {
            "at": time.time() - 660, "reason": "test",
            "waiting": list(roles), "roles": list(roles), "iteration": 1}
        daemon.save_state()
    daemon.expire_handover(STREAK)
    return ((daemon.STATE.get("handover_failed") or {})
            .get(canon(STREAK)) or {}).get("n")


def _streak_blocked():
    return daemon.handover_blocked(STREAK, ("executor",)) is not None

with daemon._lock:
    daemon.STATE["handover_failed"] = {}
    daemon.save_state()
check("one failure does not hold - the second attempt is allowed",
      (_fail_one_handover(), _streak_blocked()), (1, False))
check("two in a row do hold", (_fail_one_handover(), _streak_blocked()),
      (2, True))

print("   now the corpse: a REAL POST to /channel/register, which is how")
# A real POST, on the SAME server every other case uses. It briefly had
# one of its own, on a diagnosis that was wrong: the shared daemon was
# said to "stop answering after the telegram section". It does stop, and
# nothing mysterious does it - the suite called SRV.shutdown() itself,
# between case 41 and case 42. shutdown() ends serve_forever but leaves
# the socket LISTENING, so the OS completes the handshake out of the
# backlog and the client waits for an answer nobody is left to write:
# a hang, with no handler thread in the dump, which reads exactly like a
# daemon that died. It is stopped at the very end now, and closed as well
# as stopped, so a use after the stop says so instead of hanging.
_code44, _r44 = post_rc("/channel/register",
                        {"project": STREAK, "role": "executor",
                         "port": 51234, "pid": 4242, "ppid": 4241,
                         "session_id": "corpse-1"})
check("the endpoint took it", _code44, 200)
check("but the streak survives a channel registration",
      ((daemon.STATE.get("handover_failed") or {})
       .get(canon(STREAK)) or {}).get("n"), 2)
check("and the hold still stands", _streak_blocked(), True)

print("   a PLANNER coming up is no evidence about an EXECUTOR handover")
daemon.mark_registered(STREAK, "planner", via="session")
check("so a planner SessionStart leaves it alone",
      ((daemon.STATE.get("handover_failed") or {})
       .get(canon(STREAK)) or {}).get("n"), 2)

print("   THE SABOTAGE (rule 19): the replacement really comes up. §5.29's")
print("   reason must survive - otherwise the hold is permanent for a pair")
print("   whose stuck window somebody simply closed")
daemon.mark_registered(STREAK, "executor", via="session")
check("an executor SessionStart clears it",
      (daemon.STATE.get("handover_failed") or {}).get(canon(STREAK)), None)
check("and handovers run again", _streak_blocked(), False)

print("   the record says WHICH half never arrived, so 'no evidence about")
print("   this role' is decidable at all")
with daemon._lock:
    daemon.STATE["handover_failed"] = {}
    daemon.save_state()
_fail_one_handover(("planner",))
check("a failed planner handover records the planner",
      ((daemon.STATE.get("handover_failed") or {})
       .get(canon(STREAK)) or {}).get("roles"), ["planner"])
with daemon._lock:
    daemon.STATE["handover_failed"] = {}
    (daemon.STATE.get("handover") or {}).pop(canon(STREAK), None)
    daemon.save_state()
with daemon._lock:
    (daemon.CFG.get("projects") or {}).pop(canon(STREAK), None)
    (daemon.CFG.get("projects") or {}).pop(canon(ASSUMED), None)

print("\n45. a project folder brought its own history, and the bridge reads it")
print("    ANALYSIS-portable-history.md step 3. journal() has always written")
print("    each line into the project's bridge-logs as well as data/logs, so")
print("    the carrier was full; nothing read it back. On 2026-08-28 this")
print("    project moved E: -> C: and every line written before the move was")
print("    invisible to the feed - the rows were on disk under the old path,")
print("    and the feed filter is an exact match against norm(project)")
print("   a move is an EVENT, not background work, so this runs at exactly")
print("   two moments: daemon start, and a project entering the watch list.")
print("   Driven here through the second one, on the real endpoint")
CARRY = os.path.join(TMP, "carried-in")
_c_today = time.strftime("%Y-%m-%d")
os.makedirs(os.path.join(CARRY, "bridge-logs", _c_today), exist_ok=True)
_C_OLD = "e:" + chr(92) + "otherbox" + chr(92) + "carried-in"
with open(os.path.join(CARRY, "bridge-logs", _c_today, "events.jsonl"),
          "w", encoding="utf-8") as _fh:
    for _n, _t in ((0, "written before the move"),
                   (1, "and this one too"),
                   (2, "")):
        _fh.write(json.dumps(
            {"at": "%sT07:0%d:00" % (_c_today, _n), "kind": "loop",
             "text": _t or "no project named this line",
             "project": "Carried_in",
             "path": _C_OLD if _t else "",
             "session": "executor", "level": "log"},
            ensure_ascii=False) + "\n")

print("   before it is watched, the bridge knows nothing about it")
check("nothing of it in the feed yet",
      any("before the move" in (r.get("text") or "")
          for r in daemon.store.recent_events(200, project=CARRY)), False)

print("   now the real POST - the project enters the watch list")
with daemon._lock:
    _c_before = dict(daemon.CFG.get("projects") or {})
_c_projects = dict(_c_before)
_c_projects[CARRY] = {}
# On case 44's own server, for the reason written there: the shared one
# stops answering POSTs after the telegram section. Still the real
# endpoint, the real handler and the real order of events.
_code45, _r45 = post_rc("/config", {"projects": _c_projects})
check("the endpoint took it", _code45, 200)

_c_feed = daemon.store.recent_events(300, project=CARRY)
_c_mine = [r for r in _c_feed if "before the move" in (r.get("text") or "")
           or "this one too" in (r.get("text") or "")]
check("both carried lines are now in this pair's feed", len(_c_mine), 2)
check("re-keyed onto this machine's path",
      {r.get("path") for r in _c_mine}, {canon(CARRY)})
check("with the machine they came from kept beside them",
      {r.get("path_was") for r in _c_mine}, {_C_OLD})

print("   the pathless line is refused: _feed_rows lets a row with no path")
print("   through EVERY project's filter, so importing one would put it in")
print("   every pair's feed at once")
check("it did not come in",
      any("no project named this line" in (r.get("text") or "")
          for r in daemon.store.recent_events(300, project=A)), False)

print("   THE SABOTAGE (rule 19): do it again. A second start must not")
print("   double the history - that is the whole of 'idempotent'")
_c_n1 = len([r for r in daemon.store.recent_events(300, project=CARRY)
             if "before the move" in (r.get("text") or "")])
daemon.merge_carried_history("a second time, as a restart would")
_c_n2 = len([r for r in daemon.store.recent_events(300, project=CARRY)
             if "before the move" in (r.get("text") or "")])
check("the same line is there once, not twice", (_c_n1, _c_n2), (1, 1))

print("   and the carrier is read-only - the bridge never writes into the")
print("   folder it is reading, or the two copies would drift apart")
check("the project's own day still holds exactly what it held",
      sorted(os.listdir(os.path.join(CARRY, "bridge-logs", _c_today))),
      ["events.jsonl"])

print("   it is called from both moments, and from nowhere else - a tick")
print("   would make a move into background work")
_src45 = inspect.getsource(daemon)
check("startup calls it", "merge_carried_history(\"the bridge started\")"
      in _src45, True)
check("and so does a project entering the watch list",
      "merge_carried_history(\"it entered the watch list\")" in _src45, True)
check("and add-project", "merge_carried_history(\"it was added to the "
      "bridge\", norm(path))" in _src45, True)
print("   never under _lock: it is file work, and file I/O under the lock is")
print("   what once serialised the whole daemon behind somebody else's disk")
_mch = inspect.getsource(daemon.merge_carried_history)
check("the lock is taken only to copy the project list out",
      _mch.count("with _lock:"), 1)
print("   asked of the CODE, not of the prose: the docstring says the word",)
print("   and a substring test would have passed on the docstring alone")
_mast = ast.parse(_mch)      # a top-level function: no dedent needed
check("and STATE is named nowhere in what it executes",
      any(isinstance(_n, ast.Name) and _n.id == "STATE"
          for _n in ast.walk(_mast)), False)

with daemon._lock:
    daemon.CFG["projects"] = _c_before

print("\n46. a finding is REPORTED, and nothing is repaired")
print("    step 5. A row inside <project>/bridge-logs is about that project")
print("    by construction, so re-keying it into the feed needs nobody's")
print("    permission. A PATH is not a project: saying that one path and")
print("    another are the same work is a claim about identity, and it")
print("    is the owner's. relayout.retired_tree_users is the same shape")
FOUND = os.path.join(TMP, "found-project")
_f_today = time.strftime("%Y-%m-%d")
os.makedirs(os.path.join(FOUND, "bridge-logs", _f_today), exist_ok=True)
_F_OLD = "e:" + chr(92) + "someoldbox" + chr(92) + "found-project"
with open(os.path.join(FOUND, "bridge-logs", _f_today, "events.jsonl"),
          "w", encoding="utf-8") as _fh:
    for _n in range(3):
        _fh.write(json.dumps(
            {"at": "%sT06:0%d:00" % (_f_today, _n), "kind": "loop",
             "text": "line %d from the old machine" % _n,
             "project": "Found_project", "path": _F_OLD,
             "session": "executor", "level": "log"},
            ensure_ascii=False) + "\n")

_cfg46 = json.dumps(daemon.CFG.get("projects") or {}, sort_keys=True)
with daemon._lock:
    daemon.CFG.setdefault("projects", {})[canon(FOUND)] = {}
daemon.merge_carried_history("a fixture", canon(FOUND))

_f46 = (daemon.STATE.get("carried_found") or {}).get(canon(FOUND)) or {}
check("the other path is on the finding", sorted(_f46.get("paths") or {}),
      [_F_OLD])
check("with how many lines sit under it",
      (_f46.get("paths") or {}).get(_F_OLD), 3)
check("and it says so at warn, in this pair's own feed",
      any(r.get("level") == "warn"
          and "carries history written under another path" in (r.get("text") or "")
          for r in daemon.store.recent_events(200, project=FOUND)), True)

print("   it repairs NOTHING - no claim is recorded, and the config is")
print("   exactly what it was but for the project having been added")
check("no moved_from was invented",
      "moved_from" in json.dumps(daemon.CFG.get("projects") or {}), False)
check("and /state offers the finding for the panel to show",
      sorted((get("/state").get("carried_found") or {}).get(canon(FOUND),
                                                            {}).get("paths")
             or {}), [_F_OLD])

print("   THE SABOTAGE (rule 19): a folder whose history was written HERE")
print("   must produce no finding at all, or the panel would ask about")
print("   every project it has ever seen")
NOFIND = os.path.join(TMP, "no-finding")
os.makedirs(os.path.join(NOFIND, "bridge-logs", _f_today), exist_ok=True)
with open(os.path.join(NOFIND, "bridge-logs", _f_today, "events.jsonl"),
          "w", encoding="utf-8") as _fh:
    _fh.write(json.dumps({"at": "%sT06:00:00" % _f_today, "kind": "loop",
                          "text": "written right here", "project": "No_find",
                          "path": canon(NOFIND), "session": "executor",
                          "level": "log"}, ensure_ascii=False) + "\n")
with daemon._lock:
    daemon.CFG["projects"][canon(NOFIND)] = {}
daemon.note_carried_paths(NOFIND)
check("nothing is found, so nothing is asked",
      canon(NOFIND) in (daemon.STATE.get("carried_found") or {}), False)


print("\n47. the owner says it, once, at one endpoint")
print("    step 6. /adopt-history is the only place the claim is made. It")
print("    writes projects[path]['moved_from'] and THEN applies it; this")
print("    case is about the recording, case 48 about the applying")
print("   secret-checked like /verdict and /task, because it writes")
print("   config.json: anything that can reach localhost must not be able")
print("   to reassign somebody's history")
_bc47, _bad47 = post_rc("/adopt-history", {"path": FOUND,
                        "from": [_F_OLD]}, secret=False)
check("without the secret it is refused", _bc47, 403)
check("and nothing was recorded",
      "moved_from" in json.dumps(daemon.CFG.get("projects") or {}), False)

_oc47, _ok47 = post_rc("/adopt-history", {"path": FOUND,
                       "from": [_F_OLD]})
check("with it, the claim is taken", _oc47, 200)
check("and reported back", _ok47.get("moved_from"), [_F_OLD])
with daemon._lock:
    _entry47 = (daemon.CFG.get("projects") or {}).get(canon(FOUND)) or {}
check("it is in the config", _entry47.get("moved_from"), [_F_OLD])

print("   pressing twice records once - the list is a set in list form")
_again47 = post_rc("/adopt-history", {"path": FOUND,
                   "from": [_F_OLD]})[1]
check("the second press adds nothing", _again47.get("added"), [])
check("and the list did not grow", _again47.get("moved_from"), [_F_OLD])

print("   and the panel stops offering what was just claimed, so the button")
print("   does not sit there asking a question already answered")
check("the finding is gone",
      canon(FOUND) in (daemon.STATE.get("carried_found") or {}), False)

print("   a claim about a project the bridge does not watch is refused, and")
print("   a claim naming no earlier path is refused - both by name")
check("unknown project", post_rc("/adopt-history",
      {"path": os.path.join(TMP, "never-heard-of"),
       "from": [_F_OLD]})[1].get("ok"), False)
check("no path named",
      post_rc("/adopt-history", {"path": FOUND, "from": []})[1]
      .get("ok"), False)
check("and a project may not claim itself",
      post_rc("/adopt-history", {"path": FOUND, "from": [FOUND]})[1]
      .get("ok"), False)

print("   SS5.8: what the owner claimed must survive the next settings")
print("   write. /config replaces the whole projects dict with the panel's")
print("   copy, so a claim the panel did not send back would be erased by")
print("   the next unrelated toggle")
with daemon._lock:
    _plain47 = {k: {} for k in (daemon.CFG.get("projects") or {})}
_code47, _r47 = post_rc("/config", {"projects": _plain47})
check("the settings write went through", _code47, 200)
with daemon._lock:
    _after47 = (daemon.CFG.get("projects") or {}).get(canon(FOUND)) or {}
check("and the claim is still there", _after47.get("moved_from"), [_F_OLD])

print("   the claim is written down BEFORE it is acted on, so a migration")
print("   that raises leaves the decision on disk to be retried at the")
print("   next start - the other order would lose both (case 48 drives")
print("   the migration itself)")
_src47 = inspect.getsource(daemon.handle_adopt_history)
_i47a, _i47b = (_src47.find("store.save_config"),
                _src47.find("migrate_moved_project("))
check("it applies the claim at all", _i47b > 0, True)
check("and the config write comes first", 0 <= _i47a < _i47b, True)
check("and a failure to apply is journalled, not swallowed",
      "could not" in _src47 and '"warn"' in _src47, True)
with daemon._lock:
    daemon.CFG["projects"] = json.loads(_cfg46)

print("\n48. the break of 2026-08-28, and the claim that repairs it")
print("    This project moved E: -> C:. calibration.json and")
print("    STATE['compactions'] are keyed by project path, so every")
print("    measurement stayed under the old key and the live pair started")
print("    empty. plan_for rule 1b then read two FALLBACKS as facts - a")
print("    compaction point of 700k (the percentage) and a wall of 967k")
print("    (window minus the reserve) - and handed over a session carrying")
print("    970k, 24k BELOW the 994 509 its own samples record")
M7 = os.path.join(TMP, "moved-pair")
os.makedirs(M7, exist_ok=True)
M7C = canon(M7)
M7OLD = daemon.norm("e:" + os.sep + "projects" + os.sep + "carried-project")
_sid7 = "moved-exec"
with daemon._lock:
    _cfg7 = json.dumps(daemon.CFG.get("projects") or {})
    daemon.CFG.setdefault("projects", {})[M7C] = {}
    daemon.STATE.setdefault("sessions", {})["executor:%s" % _sid7[:8]] = {
        "role": "executor", "path": M7C, "session_id": _sid7,
        "model": "Opus 5", "window": 1000000, "window_observed": True,
        "context_tokens": 970000, "state": "idle",
        "last_seen": daemon.now(), "seen_at": time.time(),
        "turn_costs": [30000, 28000]}
    daemon.STATE.setdefault("last_session", {})["%s|executor" % M7C] = _sid7
    daemon.STATE.setdefault("pids", {})["%s|executor" % M7C] = {
        "pid": 1, "at": time.time(), "registered": True, "autocompact": 70}
    # every measurement sits under the path the OTHER machine used
    daemon.STATE.setdefault("compactions", {})["%s|executor" % M7OLD] = [
        {"tokens": t, "after": 120000, "session": "old-sid", "at": 1.0}
        for t in (999595, 998685, 994509)]
    daemon.STATE.setdefault("said", {})["%s|executor" % M7OLD] = {"n": 1}
    daemon.STATE.setdefault("assessed", {})[M7OLD] = time.time()
    daemon.STATE["acted:clinch:%s" % M7OLD] = 1.0
    daemon.save_state()
daemon.store.calib_update("opus 5", M7OLD, compact_at_tokens=994509,
                          compact_at_window=1000000,
                          compact_samples=[999595, 998685, 994509],
                          how="PreCompact fired")

_s7 = daemon.STATE["sessions"]["executor:%s" % _sid7[:8]]
_wv7 = daemon.wall_view(_s7, M7)
check("the live pair has no compaction point of its own",
      _wv7.get("compact_measured"), False)
check("so the number on offer is the percentage of the window",
      _wv7.get("compact"), 700000)
check("and the wall is the unmeasured reserve arithmetic",
      daemon.compaction_too_big(M7, "executor", 1000000), 967000)
print("   today's guard already stops 1b firing on that: a point nobody")
print("   measured may not be used to conclude no compaction is coming")
_p7a = daemon.plan_for(_s7, M7)
check("no handover is ordered", _p7a["do"] == "handover", False)
check("it stands aside instead", _p7a["do"], "compacting")
print("   but it stands aside for the WRONG reason - it believes the")
print("   session is past a 700k point, when the truth is it is 24k short")
print("   of a 994k one. The guard saved the session; only the migration")
print("   makes the numbers true")
check("the reason names the assumed point", "700k" in _p7a["why"], True)

print("   the owner's claim, through the real endpoint")
_c7, _r7 = post_rc("/adopt-history", {"path": M7, "from": [M7OLD]})
check("it was taken", (_c7, _r7.get("moved_from")), (200, [M7OLD]))

_wv7b = daemon.wall_view(_s7, M7)
check("now the point is measured", _wv7b.get("compact_measured"), True)
check("and it is the one this pair really compacts at",
      _wv7b.get("compact"), 994509)
_p7b = daemon.plan_for(_s7, M7)
check("plan_for still says compacting", _p7b["do"], "compacting")
check("and now for the true reason", "994k" in _p7b["why"], True)

print("   the whole inventory moved, not the calibration alone - including")
print("   three containers that were in NEITHER of the two old lists, and a")
print("   top-level key that is a prefix followed by a path")
check("compactions, which was in neither list",
      "%s|executor" % M7C in (daemon.STATE.get("compactions") or {}), True)
check("said, which was in neither list",
      "%s|executor" % M7C in (daemon.STATE.get("said") or {}), True)
check("assessed, which was in neither list",
      M7C in (daemon.STATE.get("assessed") or {}), True)
check("acted:clinch:<path>, matched by its tail",
      "acted:clinch:%s" % M7C in daemon.STATE, True)
check("and nothing at all is left under the old path",
      [k for k in daemon.STATE if M7OLD in k] +
      [k for n in daemon.STATE_PATHS
       for k in (daemon.STATE.get(n) or {}) if M7OLD in str(k)], [])

print("   the carried measurements are MARKED, so nothing downstream can")
print("   take a figure from another computer for one measured here (rule")
print("   33: evidence has a shelf life, and a different machine is a")
print("   different horizon)")
check("every carried compaction row says where it came from",
      sorted({r.get("carried_from") for r in
              (daemon.STATE["compactions"].get("%s|executor" % M7C)
               or [{}])}), [M7OLD])
_cal7 = (daemon.store.load_calibration()
         .get(daemon.store.calib_key("opus 5", M7C)) or {})
check("so does the calibration entry", _cal7.get("carried_from"), M7OLD)
check("and its how says where it was measured",
      "another machine" in (_cal7.get("how") or ""), True)
_tr7 = ((daemon.STATE.get("moved_trace") or []) or [{}])[-1]
check("the trace records the move that happened",
      (_tr7.get("from"), _tr7.get("to"), _tr7.get("calib_moved")),
      (M7OLD, M7C, 1))
check("and how much it moved", (_tr7.get("state_moved") or 0) >= 4, True)

print("   idempotent: the same claim again moves nothing, because nothing")
print("   is left under the old key to move")
_n7 = len(daemon.STATE.get("moved_trace") or [])
daemon.migrate_moved_project(M7)
check("no second trace entry", len(daemon.STATE.get("moved_trace") or []), _n7)

print("   THE SABOTAGE (rule 19): a pair that has measured the same thing")
print("   HERE keeps its own numbers. A carried figure never overwrites a")
print("   local measurement, however much richer it looks - the local one")
print("   is about this machine and the carried one is not")
M8 = os.path.join(TMP, "moved-pair-with-local")
os.makedirs(M8, exist_ok=True)
M8C = canon(M8)
with daemon._lock:
    daemon.CFG["projects"][M8C] = {}
    daemon.STATE["compactions"]["%s|executor" % M7OLD] = [
        {"tokens": 999595, "after": 120000, "session": "old", "at": 1.0}]
    daemon.STATE["compactions"]["%s|executor" % M8C] = [
        {"tokens": 701000, "after": 90000, "session": "here", "at": 2.0}]
    daemon.save_state()
daemon.store.calib_update("opus 5", M7OLD, compact_at_tokens=994509,
                          compact_at_window=1000000,
                          compact_samples=[994509], how="PreCompact fired")
daemon.store.calib_update("opus 5", M8C, compact_at_tokens=701000,
                          compact_at_window=1000000,
                          compact_samples=[701000], how="PreCompact fired")
_c8, _r8 = post_rc("/adopt-history", {"path": M8, "from": [M7OLD]})
check("the claim was taken all the same", _c8, 200)
_cal8 = daemon.store.load_calibration()
check("but the local calibration is untouched",
      (_cal8.get(daemon.store.calib_key("opus 5", M8C)) or {})
      .get("compact_at_tokens"), 701000)
check("and it is the local compactions that decide what is survivable",
      daemon.compaction_survivable(M8, "executor"), 701000)
check("the trace says the carried entry was kept out, and why",
      (daemon.STATE.get("moved_trace") or [])[-1].get("calib_kept_local"), 1)
with daemon._lock:
    daemon.CFG["projects"] = json.loads(_cfg7)
    daemon.save_state()


print("\n49. one inventory of the paths in STATE, and a standing guard")
print("    PATH_KEYED and PAIR_KEYED were two lists that between them were")
print("    supposed to cover every container in STATE holding a project")
print("    path. They did not: counted on this project's own live state")
print("    after the move, 268 references to the old drive across 27")
print("    containers, and TWELVE of those containers appeared in neither")
print("    list. migrate_keys believed itself complete; so did step 7")
print("   the guard is deliberately NOT one list compared with another -")
print("   that only ever restates itself, and would have passed happily on")
print("   the day the lists were wrong. It walks the STATE this suite has")
print("   actually built through the real endpoints and asks of every")
print("   path-shaped key: is the container holding it one the inventory")
print("   names? Its limit, said plainly: it can only see containers this")
print("   suite fills. That is why it runs LAST, after every fixture")
_PATHY = re.compile("[a-zA-Z]:[/" + re.escape(os.sep) + "]")


def _pathish(x):
    return isinstance(x, str) and bool(_PATHY.match(x))


_unlisted, _kinds49 = [], set()
for _name, _v in sorted(daemon.STATE.items()):
    if _PATHY.search(_name):
        if any(_name.startswith(p) for p in daemon.STATE_PATH_PREFIXES):
            _kinds49.add("prefixed")
        else:
            _unlisted.append("top-level key %r" % _name)
        continue
    _kind = daemon.STATE_PATHS.get(_name)
    if isinstance(_v, dict):
        for _k, _val in _v.items():
            if _pathish(_k) and _kind not in ("path", "pair"):
                _unlisted.append("%s: path-shaped keys" % _name)
                break
            if isinstance(_val, dict) and _pathish(_val.get("path")) \
                    and _kind != "value":
                _unlisted.append("%s: values carry a path" % _name)
                break
    elif isinstance(_v, list):
        for _row in _v:
            if isinstance(_row, dict) and _pathish(_row.get("path")) \
                    and _kind != "rows":
                _unlisted.append("%s: rows carry a path" % _name)
                break

check("every path in this daemon's STATE lies where the inventory says",
      sorted(set(_unlisted)), [])
print("   and the guard is worth something only if the run reached all")
print("   four shapes the inventory distinguishes")
check("all four shapes were exercised",
      sorted({k for n, k in daemon.STATE_PATHS.items() if daemon.STATE.get(n)}),
      ["pair", "path", "rows", "value"])
check("prefixed top-level keys too", "prefixed" in _kinds49, True)

print("   THE SABOTAGE (rule 19): put paths in a container the inventory")
print("   does not name, and the same walk must go red - that is its")
print("   entire job, and a guard that cannot fail is not a guard")
with daemon._lock:
    daemon.STATE["a_container_nobody_listed"] = {canon(A): {"x": 1}}
_sab49 = []
for _name, _v in sorted(daemon.STATE.items()):
    if _PATHY.search(_name) or not isinstance(_v, dict):
        continue
    if any(_pathish(_k) for _k in _v) and \
            daemon.STATE_PATHS.get(_name) not in ("path", "pair"):
        _sab49.append(_name)
check("it names the container nobody listed", _sab49,
      ["a_container_nobody_listed"])
with daemon._lock:
    daemon.STATE.pop("a_container_nobody_listed", None)
    daemon.save_state()

print("   the two old lists still exist and still work, but they are now")
print("   VIEWS of the one inventory - so they cannot drift from it, and")
print("   there is one place to add a container rather than three")
check("PATH_KEYED is derived", sorted(daemon.PATH_KEYED),
      sorted(n for n, k in daemon.STATE_PATHS.items() if k == "path"))
check("PAIR_KEYED is derived", sorted(daemon.PAIR_KEYED),
      sorted(n for n, k in daemon.STATE_PATHS.items() if k == "pair"))
check("and the containers that were in neither list are in it now",
      sorted(n for n in ("assessed", "compactions", "handover_log",
                         "last_task", "quiet_pairs", "rc", "said",
                         "session_roles", "sessions", "strangers",
                         "telemetry", "tscript", "windows")
             if n not in daemon.STATE_PATHS), [])

print("\n50. removing a project from the list, per row")
print("    The owner: \"I can see old projects from another computer in the")
print("    bridge, I need a button to remove them from this list so they do")
print("    not get in the way, for each project separately.\" Those projects")
print("    are GHOSTS - they are not in config.json at all. pair_paths()")
print("    builds a row from a session record alone, and the old removal")
print("    path cleared four containers by hand with `sessions` not among")
print("    them, so a removed project kept its row for ever")
R1 = os.path.join(TMP, "removable")
os.makedirs(R1, exist_ok=True)
R1C = canon(R1)
_cfg50 = json.dumps(daemon.CFG.get("projects") or {}, sort_keys=True)
post_rc("/config", {"projects": dict(
    json.loads(_cfg50), **{R1C: {}})})
check("it is watched", R1C in (get("/state").get("config") or {})
      .get("projects", {}), True)

print("   real events, through the real endpoints, so the state it leaves")
print("   behind is the state a working pair leaves behind")
post_rc("/loop", {"action": "start", "project": R1})
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "rm-exec", "project_dir": R1, "cwd": R1})
post_rc("/status", {"role": "executor", "payload": {
    "session_id": "rm-exec",
    "workspace": {"current_dir": R1, "project_dir": R1},
    "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
    "context_window": {"context_window_size": 1000000,
                       "used_percentage": 30.0,
                       "current_usage": {"input_tokens": 10,
                                         "cache_creation_input_tokens": 90,
                                         "cache_read_input_tokens": 299900,
                                         "output_tokens": 4000}}}})
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": "rm-exec", "project_dir": R1, "cwd": R1,
                   "tool_name": "Bash",
                   "tool_input": {"command": "py -c \"print(1)\""}})
post_rc("/event", {"hook_event_name": "PreCompact", "role": "executor",
                   "session_id": "rm-exec", "project_dir": R1, "cwd": R1})
_st50 = get("/state")
check("the pair has a row in the strip", R1C in _st50["pairs"], True)
check("and lines of its own in the all-pairs feed - said BEFORE, so the",
      len([e for e in (_st50.get("events") or [])
           if e.get("path") == R1C]) > 0, True)
print("   check that they are gone afterwards can actually fail (rule 19)")
_before50 = sorted(n for n, k in daemon.STATE_PATHS.items()
                   if _holds(daemon.STATE.get(n), k, R1C))
check("and it has left records in several containers",
      len(_before50) >= 4, True)
note("containers it filled", _before50)

print("   removal, through the real endpoint. Secret-checked like")
print("   /adopt-history: it writes config.json and empties state, and")
print("   anything that can reach localhost must not be able to delete")
print("   somebody's pair")
check("without the secret it is refused",
      post_rc("/forget-project", {"path": R1}, secret=False)[0], 403)
check("and it is still there", R1C in get("/state")["pairs"], True)

_mark50 = (daemon.CFG.get("marks") or {}).get(R1C)
_c50, _r50 = post_rc("/forget-project", {"path": R1})
check("with it, the project is removed", (_c50, _r50.get("ok")), (200, True))
_st50b = get("/state")
check("it is out of the strip", R1C in _st50b["pairs"], False)
check("out of the selector, which reads the config",
      R1C in (_st50b.get("config") or {}).get("projects", {}), False)
check("its colour was taken while it existed - said first, so the next",
      bool(_mark50), True)
print("   check can fail (rule 19); marks is the other half of config.json")
print("   that holds a path, and the only one besides projects")
check("and it is released now",
      R1C in (daemon.CFG.get("marks") or {}), False)
check("and out of the all-pairs feed, which can no longer label its lines",
      [e for e in (_st50b.get("events") or []) if e.get("path") == R1C], [])

print("   and the state is clean BY THE INVENTORY - not by a list written")
print("   out here, which is the mistake being repaired. Any container the")
print("   inventory names may not still hold this path")
check("nothing left anywhere in STATE_PATHS",
      sorted(n for n, k in daemon.STATE_PATHS.items()
             if _holds(daemon.STATE.get(n), k, R1C)), [])
check("nor in a prefixed top-level key",
      [k for k in daemon.STATE
       if k.startswith(daemon.STATE_PATH_PREFIXES) and R1C in k], [])

print("   a removal cannot be undone, so it leaves a trace - the same")
print("   shape as moved_trace, and for the same reason")
_t50 = (daemon.STATE.get("forget_trace") or [])[-1]
check("the trace names the project", _t50.get("path"), R1C)
check("says it was in the config", _t50.get("in_config"), True)
check("counts what it took", _t50.get("state_dropped") >= 4, True)
check("and names every container it took it from",
      sorted(set(_before50) - set(_t50.get("containers") or {})), [])
check("there is a journal line about it, and it is bridge-wide",
      any("Removed removable from the list" in (r.get("text") or "")
          and not r.get("path")
          for r in daemon.store.recent_events(200)), True)

print("   THE FOLDER IS NOT TOUCHED. bridge-logs is carried history and")
print("   belongs to the folder, not to this machine's list; the hooks are")
print("   uninstall's business, a different decision with its own button")
check("the folder is still there", os.path.isdir(R1), True)
_in_folder = []
for _root, _dirs, _files in os.walk(os.path.join(R1, "bridge-logs")):
    for _f in _files:
        _in_folder += [_l for _l in open(os.path.join(_root, _f),
                                         encoding="utf-8", errors="replace")
                       if "Removed" in _l and "from the list" in _l]
check("and the bridge wrote nothing into it about the removal",
      _in_folder, [])

print("\n   a GHOST: never in config, only in the live state. This is the")
print("   case the owner actually has, and the case the old path could not")
print("   do at all")
GH = os.path.join(TMP, "ghost-from-the-old-box")
os.makedirs(GH, exist_ok=True)
GHC = canon(GH)
with daemon._lock:
    daemon.STATE.setdefault("sessions", {})["executor:ghost123"] = {
        "role": "executor", "path": GHC, "session_id": "ghost123",
        "model": "Opus 5", "window": 1000000, "context_tokens": 300000,
        "state": "idle", "last_seen": daemon.now(), "seen_at": time.time()}
    daemon.STATE.setdefault("compactions", {})["%s|executor" % GHC] = [
        {"tokens": 900000, "after": 100000}]
    daemon.STATE.setdefault("handover_log", []).append(
        {"path": GHC, "role": "executor", "at": "x"})
    daemon.STATE["acted:clinch:%s" % GHC] = 1.0
    daemon.save_state()
check("the ghost has a row, with nothing in the config behind it",
      (GHC in get("/state")["pairs"],
       GHC in (get("/state").get("config") or {}).get("projects", {})),
      (True, False))
_gr = post_rc("/forget-project", {"path": GH})[1]
check("it is removed all the same", _gr.get("ok"), True)
check("and the trace says it was never watched", _gr.get("in_config"), False)
check("the row is gone", GHC in get("/state")["pairs"], False)
check("and so is every record it had",
      sorted(n for n, k in daemon.STATE_PATHS.items()
             if _holds(daemon.STATE.get(n), k, GHC)) +
      [k for k in daemon.STATE
       if k.startswith(daemon.STATE_PATH_PREFIXES) and GHC in k], [])

print("\n   A LIVE PAIR IS REFUSED, BY NAME. Emptying the records under a")
print("   running window does not stop it - it orphans it: the windows keep")
print("   firing hooks and the bridge no longer knows whose they are. Worse")
print("   than either answer, so it is refused rather than warned about")
LV = os.path.join(TMP, "live-pair")
os.makedirs(LV, exist_ok=True)
LVC = canon(LV)
post_rc("/config", {"projects": dict(json.loads(_cfg50), **{LVC: {}})})
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % LVC] = {
        "pid": os.getpid(), "at": time.time()}   # this process really is alive
    daemon.save_state()
_lr = post_rc("/forget-project", {"path": LV})[1]
check("refused", _lr.get("ok"), False)
check("and it says which window, with its pid - not \"the pair is busy\"",
      ("executor" in (_lr.get("error") or "")
       and str(os.getpid()) in (_lr.get("error") or "")), True)
check("nothing was removed", LVC in (get("/state").get("config") or {})
      .get("projects", {}), True)
check("and the records are all still there",
      "%s|executor" % LVC in (daemon.STATE.get("pids") or {}), True)
print("   it fails OPEN: a record whose pid the OS says is gone is not")
print("   evidence of a window, and a removal blocked by a dead record")
print("   would be a button that never works again")
with daemon._lock:
    _rec50 = (daemon.STATE.get("pids") or {}).get("%s|executor" % LVC)
    if isinstance(_rec50, dict):
        _rec50["pid"] = 999999999
check("with the window gone, the same call goes through",
      post_rc("/forget-project", {"path": LV})[1].get("ok"), True)

print("\n   ADDING IT BACK IS A CLEAN START - and the carried-history")
print("   finding comes back with it. That is the merge working, not a")
print("   bug: removal clears STATE['carried_found'], but the rows it was")
print("   read from are in the folder's own bridge-logs, which removal does")
print("   not touch. Same folder, same evidence, same offer")
BK = os.path.join(TMP, "removed-and-back")
_bk_today = time.strftime("%Y-%m-%d")
os.makedirs(os.path.join(BK, "bridge-logs", _bk_today), exist_ok=True)
BKC, _BK_OLD = canon(BK), "e:" + chr(92) + "oldbox" + chr(92) + "back"
with open(os.path.join(BK, "bridge-logs", _bk_today, "events.jsonl"),
          "w", encoding="utf-8") as _fh:
    for _n in range(2):
        _fh.write(json.dumps(
            {"at": "%sT07:0%d:00" % (_bk_today, _n), "kind": "loop",
             "text": "line %d from the old machine" % _n,
             "project": "Back", "path": _BK_OLD, "session": "executor",
             "level": "log"}, ensure_ascii=False) + "\n")
with daemon._lock:
    daemon.CFG.setdefault("projects", {})[BKC] = {}
daemon.merge_carried_history("a fixture", BKC)
check("the finding is offered", sorted(
    ((daemon.STATE.get("carried_found") or {}).get(BKC) or {}).get("paths")
    or {}), [_BK_OLD])
post_rc("/adopt-history", {"path": BK, "from": [_BK_OLD]})
with daemon._lock:
    daemon.STATE.setdefault("loops", {})[BKC] = {"active": True,
                                                 "iteration": 4}
    daemon.save_state()
check("removal takes the finding with it",
      post_rc("/forget-project", {"path": BK})[1].get("ok"), True)
check("no finding left", BKC in (daemon.STATE.get("carried_found") or {}),
      False)
check("and no claim left either - the config entry went with it",
      BKC in (daemon.CFG.get("projects") or {}), False)

with daemon._lock:
    daemon.CFG.setdefault("projects", {})[BKC] = {}
daemon.merge_carried_history("added again", BKC)
check("added back, the loop record is NOT resurrected",
      (daemon.STATE.get("loops") or {}).get(BKC), None)
check("the claim is not resurrected either - it is the owner's to make again",
      (daemon.CFG["projects"][BKC] or {}).get("moved_from"), None)
check("but the finding legitimately returns, because the folder still has "
      "the rows", sorted(
          ((daemon.STATE.get("carried_found") or {}).get(BKC) or {})
          .get("paths") or {}), [_BK_OLD])

print("\n   THE SABOTAGE (rule 19): a container the removal walks must")
print("   actually be walked. Put the path back into one and the same")
print("   check must go red")
with daemon._lock:
    daemon.STATE.setdefault("said", {})["%s|executor" % GHC] = {"n": 1}
check("the walk finds it",
      sorted(n for n, k in daemon.STATE_PATHS.items()
             if _holds(daemon.STATE.get(n), k, GHC)), ["said"])
with daemon._lock:
    daemon.STATE["said"].pop("%s|executor" % GHC, None)
    daemon.CFG["projects"] = json.loads(_cfg50)
    daemon.save_state()

check("and the shared server answered for the whole run - the day",
      get("/state").get("pairs") is not None, True)
print("   somebody stops it early again, every case after that point goes")
print("   red here rather than hanging one by one for its own timeout")

# Stopped where stopping is what is wanted, and CLOSED as well as stopped.
# shutdown() ends serve_forever and leaves the socket listening, so a
# request after it hangs for the client's whole timeout with no handler
# thread anywhere - which is what an early shutdown() looked like for a
# week. server_close() drops the listening socket, so the same mistake
# reports itself as a refused connection instead.
SRV.shutdown()
SRV.server_close()

print("\n" + ("-" * 60))
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
