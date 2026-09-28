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
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# first: it reads nothing from the package, and the folder it makes
# is the only one this run may remove (DECISIONS.md 8.43, 8.45)
from bridgecore import owntemp                 # noqa: E402
TMP = owntemp.make("bridge-multipair-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
# The client's own config is isolated too: install() marks a project trusted
# there, and without this a suite would merge its throwaway temp projects into
# the real ~/.claude.json on this machine.
os.environ["BRIDGE_CLAUDE_JSON"] = os.path.join(TMP, ".claude.json")
# and the user-level settings approve_channel merges into - never the
# real one (DECISIONS.md 8.35)
os.environ["BRIDGE_CLAUDE_SETTINGS"] = os.path.join(TMP, "user-settings.json")
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


def read_or_fail(path, what):
    """Read a file whose existence a check has JUST asserted.

    Returns "" when it is not there, and says so. The form this replaces
    was: check the file exists, then open it on the next line regardless of
    the answer - so `check` marked FAIL, and the read three characters
    later raised and killed the script, taking every block below it with
    it. Silently: the output simply stops, with no summary and no FAIL
    line, which reads like a hang rather than a failure. Measured on the
    public tree, where QUIET.md is deliberately absent: 83 of 103 blocks
    ran, and the twenty that did not were never reported missing.

    A check that has already spoken must not be able to un-speak itself by
    crashing. Every caller has to be safe with "" - that is the point: the
    dependent checks then fail on their own terms, in the output, where a
    person can see which ones.
    """
    if not path or not os.path.isfile(path):
        print("   !! %s is not there, so the checks below it cannot pass"
              % (what,))
        return ""
    return open(path, encoding="utf-8").read()



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
    """The rows the stub windows wrote - only the finished ones.

    A stub may be writing its row at the very moment this reads: the line is
    a row only once its newline is on disk. Reading it half-written crashed
    the whole suite with "Expecting value: line 1 column 1" (2026-09-23,
    under six suites at once, in case 55 - a NUL-filled tail), taking every
    block below it and its summary. So the unterminated tail is left for the
    next read; every caller already waits for the row it wants with until().
    """
    if not os.path.exists(LAUNCHES):
        return []
    with open(LAUNCHES, encoding="utf-8") as fh:
        text = fh.read()
    return [json.loads(l) for l in text.split("\n")[:-1] if l.strip()]


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


def register(project, role, sid, ppid=None):
    """A channel comes up for one half of one pair, as channel.py does.

    `ppid` is the window the channel names as its parent - channel.py has
    sent it since the parentage rule (5.19). Left out, the registration is
    an old channel.py's, which names no window; since 8.36 a handover
    refuses to replace a half whose only witness is such a channel, so a
    case that hands a pair over gives it a window to name.
    """
    port = open_channel(project, role)
    body = {"project": project, "port": port, "pid": os.getpid(),
            "role": role}
    if ppid:
        body["ppid"] = ppid
    post("/channel/register", body, secret=True)
    daemon.remember_session(project, role, sid)
    return port


STAND_INS = []


def dead_pid():
    """The pid of a process that has already exited - a closed window."""
    p = subprocess.Popen([sys.executable, "-c", "pass"],
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
    p.wait(20)
    return p.pid


def stand_in(project, role):
    """A LIVE stand-in for the half's window, and the record pointed at it.

    The suite's stub windows exit as soon as they have recorded themselves,
    so a case that needs the window a handover replaces to be alive points
    the record at a process that is - the form test_wall_handover's A4b
    has used since 8.31. Killed at the end of the suite if nothing
    stopped it first.
    """
    p = subprocess.Popen([sys.executable, "-c",
                          "import time" + chr(10) + "time.sleep(900)"],
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
    STAND_INS.append(p)
    with daemon._lock:
        _rec = daemon.STATE.setdefault("pids", {}).setdefault(
            "%s|%s" % (canon(project), role), {})
        _rec["pid"] = p.pid
        # WRITTEN NOW, for the process born now. This kept the record's old
        # time, or put it an hour back - a record of a launch an hour older
        # than the process it names, which no writer in the bridge produces
        # and which the one definition (daemon.record_alive, 8.46) reads,
        # rightly, as a number that has passed to somebody else.
        _rec["at"] = time.time()
        _rec["registered"] = True
        _rec["registered_via"] = _rec.get("registered_via") or "session"
        daemon.save_state()
    return p.pid


# WINDOWS A CASE DECLARES ALIVE. Several cases stand in for a live window
# with an invented, odd pid - never a real Windows one, those are multiples
# of four - and stub its console. Since 8.46 the daemon asks every pid it
# acts on whether it is alive and is still that window, so a case that
# says "this window is alive" tells the daemon so, as piece 22 did for 74
# and the recovery simulation. A declared pid answers alive; every other
# pid is asked for real.
DECLARED_ALIVE = set()
_pid_alive_real = sessions.pid_alive


def _pid_alive_declared(pid):
    try:
        if int(pid or 0) in DECLARED_ALIVE:
            return True
    except (TypeError, ValueError):
        pass
    return _pid_alive_real(pid)


sessions.pid_alive = _pid_alive_declared


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
# THE WINDOW BEING REPLACED IS ALIVE, as a real one is: the stub of case 7
# has exited, and since 8.36 a handover names the window it replaces
# before it opens one - a dead record and a channel that names no window
# is a half that cannot be named, and it is refused.
stand_in(C, "executor")
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


def _matching_now():
    """How many of today's rows this project's filter lets through.

    A line with no path is ABOUT THE BRIDGE and passes every project's
    filter by design, so this number moves whenever any daemon thread
    journals - which is the whole difficulty below.
    """
    return len([r for r in store._read_events(
        os.path.join(store.day_dir(), "events.jsonl"))
        if not r.get("path") or r.get("path") == canon(QUIET)])


CHATTY = 60          # the separation the fixture puts between the two bounds


def _quiet_reading(tries=8):
    """Lay the fixture, then read with a window that is not already stale.

    THE FLAKE THIS REPLACES. Sizing the window by what actually matches is
    what makes the case say what it means - but counting and then reading
    is two reads with a gap, and every path-less line landing in that gap
    is a matching row the window no longer covers. Measured: one control
    run in five went red, always on the same three checks, and it reads
    like a defect in recent_events when it is nothing but another thread
    doing its job.

    THE ACCEPTANCE TEST IS NOT STILLNESS. That was tried first and is both
    too strict and beside the point: under a journal that never stops it
    fails where the old single-shot form passed, and it fails on its own
    bookkeeping rather than on the claim. What the reading actually needs
    is a window at least as large as the matching rows present when it was
    taken - and it may lag by up to CHATTY, because the sixty beta lines
    are exactly the distance the case puts between "big enough to keep the
    mark when filtered" and "small enough to lose it when not". Drift
    inside that separation cannot change either answer. The number is the
    fixture's own, measured here, not a tolerance chosen to make a red go
    away.

    Returns the readings and whether the window was still good for them.
    """
    out = None
    for _ in range(tries):
        # LAID AFRESH each attempt, and it has to be. A refused reading is
        # not just stale, it is unrecoverable for the panel's half: that
        # window is a fixed forty rows server-side, so a mark with two
        # hundred matching lines now after it can never come back into it,
        # however carefully we read. Re-laying puts the mark back at the
        # top; the sixty beta lines that follow are what bury it in the
        # UNFILTERED order, which is the other half of the claim.
        store.journal("loop", "QUIET-MARK", "quiet-project", project_dir=QUIET)
        for _i in range(60):
            store.journal("loop", "chatty beta %d" % _i, "beta", project_dir=B)
        store.journal("bridge", "BRIDGE-WIDE-MARK")
        n1 = _matching_now()
        out = {
            "forty": [e.get("text") for e in store.recent_events(40)],
            "filtered": [e.get("text") for e in
                         store.recent_events(n1, project=canon(QUIET))],
            "raw": [e.get("text") for e in store.recent_events(n1)],
            "feed": get("/state?project="
                        + urllib.parse.quote(QUIET))["events"],
        }
        out["drift"] = _matching_now() - n1
        if out["drift"] < CHATTY:
            out["still"] = True
            return out
        time.sleep(0.25)
    out["still"] = False
    return out


_q12 = _quiet_reading()
check("the window was read before enough lines landed to move either "
      "bound - otherwise nothing below means anything", _q12["still"], True)
note("matching rows that landed while it was reading", _q12.get("drift"))
sixty_later = _q12["forty"]
check("cut without filtering, the quiet pair's line is gone",
      "QUIET-MARK" in sixty_later, False)
check("because the chatty one has taken nearly all of the window",
      len([t for t in sixty_later if t.startswith("chatty beta")]) > 30, True)
filtered = _q12["filtered"]
check("filtering first, it survives", "QUIET-MARK" in filtered, True)
print("   and the same window, unfiltered, does NOT hold it - which is the")
print("   whole claim: the filter runs first, the cut second")
check("cut first and it would be lost", "QUIET-MARK" in _q12["raw"], False)
check("with none of the sixty that buried it",
      [t for t in filtered if t.startswith("chatty beta")], [])

feed_q = _q12["feed"]
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


# WHICH SCENARIO THIS IS, said out loud. ensure_record exists for a window
# that answers its port having fired no hooks - "because the bridge
# restarted after it registered", in its own words. Since 2026-09-02
# handover_awaits can tell that apart from "a replacement was launched and
# has not come up yet", and refuses the channel witness for the second
# (case 63). Earlier cases left this pair a young launch record, so the
# fixture has to say these windows DID come up, or it is quietly testing
# the other scenario.
for _r19 in ("executor", "planner"):
    daemon.mark_registered(C, _r19, via="session")

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
_debt = read_or_fail(os.path.join(DPROJ, "bridge-logs", "DEBT.md"),
                     "DEBT.md")
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
_debt2 = read_or_fail(os.path.join(DPROJ, "bridge-logs", "DEBT.md"),
                      "DEBT.md")
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
          (_rang[0][0] if _rang else None) in daemon.TELEGRAM_KINDS, True)

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

    print("   the sabotage: the SAME error with nothing to say it is an")
    print("   overflow is still a wall hit, immediately - the branch is not")
    print("   disarmed. It is the SESSION that changes here, not the error:")
    print("   an invalid_request with no numbers in it, from a session at")
    print("   300k of a 1M window, is a broken request and nothing else")
    print("   (this test moved on 2026-08-30 - see case 55. Before it, ANY")
    print("   invalid_request with no PreCompact rotated at once, and that")
    print("   is what killed the executor at 18:27:43: the client never")
    print("   sends a PreCompact when the API refuses first. What decides")
    print("   now is the text and the size, so the sabotage has to take")
    print("   both away, and this one does)")
    _seat_compactor(tokens=300000)
    _r = post("/event", {"hook_event_name": "StopFailure", "cwd": CMP,
                         "role": "executor", "session_id": _csid,
                         "error": "invalid_request",
                         "error_type": "invalid_request"})
    check("a broken request well below the wall rotates at once",
          [w for _p, w in _rot35], ["hit the wall"])
    check("and it left no wait behind",
          (daemon.STATE.get("compact_wait") or {}).get(
              "%s|executor" % canon(CMP)), None)

    print("   and the other side of that boundary, so it is a boundary and")
    print("   not a rule: the same wordless error from a session already at")
    print("   the size where a compaction stops fitting IS waited for")
    del _rot35[:]
    _seat_compactor(tokens=999000)
    post("/event", {"hook_event_name": "StopFailure", "cwd": CMP,
                    "role": "executor", "session_id": _csid,
                    "error": "invalid_request",
                    "error_type": "invalid_request"})
    check("nobody was rotated", _rot35, [])
    check("the size alone was enough to wait",
          (daemon.STATE.get("compact_wait") or {}).get(
              "%s|executor" % canon(CMP), {}).get("via"), "prompt_too_long")
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
    print("   'by a turn' is measured for this pair now, not a literal, so")
    print("   the case states the turn it is arguing from")
    daemon.note_turn_cost(CMP, "executor", 200000, _csid)
    _tw34 = daemon.turn_widest(CMP, "executor")
    check("the width is this pair's own", _tw34, (200000, "measured"))
    check("and the ceiling sits below the failure, by a turn",
          daemon.compaction_too_big(CMP, "executor", 1000000),
          998685 - _tw34[0])

    print("   the sabotage: take the failure away and the old successes")
    print("   stand again - which is what the bridge did until today")
    with daemon._lock:
        (daemon.STATE.get("compact_failed") or {}).pop(
            "%s|executor" % canon(CMP), None)
        daemon.save_state()
    check("without a failure the highest success rules",
          daemon.compaction_survivable(CMP, "executor"), 999875)
    # Anchored first: without this the expectation calls the very
    # function under test, so a wrong turn figure moves both sides of the
    # comparison together and the check passes on it (rule 19). The sibling
    # check above pins the width the same way.
    _ord35 = daemon.turn_ordinary(CMP, "executor")
    check("the ordinary turn is this pair's own, measured", _ord35,
          (200000, "measured"))
    check("and the ceiling is a turn ABOVE it - the zone that killed us",
          daemon.compaction_too_big(CMP, "executor", 1000000),
          999875 + _ord35[0])
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
        # one process this suite can be sure is alive. REGISTERED,
        # because that is what "still up" means on this record since
        # 2026-08-30: a pid on its own says a process exists, and a
        # window sitting on a startup dialog is a process that never
        # became a session (case 53). This one did - it ran turns.
        daemon.STATE.setdefault("pids", {})["%s|executor" % canon(FRZ)] = {
            "pid": os.getpid(), "at": time.time(), "registered": True}
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
check("the endpoint took it", (_code44, _r44.get("ok")), (200, True))
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
# ok, not the status code: /adopt-history answers 200 when it REFUSES too,
# so a check on the code alone passes either way (rule 19).
check("with it, the claim is taken", (_oc47, _ok47.get("ok")), (200, True))
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
# The case says the claim is taken AND the calibration untouched. On the
# status code alone a refused claim leaves the calibration untouched too,
# so both halves went green for the wrong reason. The body decides.
check("the claim was taken all the same", (_c8, _r8.get("ok")), (200, True))
check("and it is the claim that was recorded", _r8.get("moved_from"),
      [M7OLD])
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

print("\n51. a second live window may take the seat; a subagent may not")
print("    2026-08-28, 12:30:40. A planner window came up that this bridge")
print("    did NOT launch - it adopted it (\"Noticed a live planner window")
print("    (its channel is answering) - adding it to the panel\") and never")
print("    updated STATE['pids'], which still named the window it had")
print("    launched the evening before and which was still running. So the")
print("    new window's channel had a parent that was not the recorded")
print("    window, while the old window's channel was still its child -")
print("    and channel_supersedes refused the newcomer 232 times over two")
print("    hours, every 45 s. Reports 86, 87 and 88 were carried to a live")
print("    window nobody was reading; at three unanswered the pair was held")
print("   every guard downstream was silent and RIGHT to be: the channel")
print("   really was draining, so chan_backlog stayed empty and")
print("   unread_channel had nothing to fire on; deliver_ex's fallback")
print("   fired all three times into bridge-logs/inbox; the blind poll woke")
print("   the executor. Nothing measures \"delivered to the wrong live")
print("   window\", and the repair is not another watcher - it is that the")
print("   seat is decided correctly in the first place")

_TREE = os.path.join(TMP, "tree.py")
with open(_TREE, "w", encoding="utf-8") as _fh:
    _fh.write(
        "import os, subprocess, sys, time\n"
        "d = int(sys.argv[1])\n"
        "if d > 0:\n"
        "    p = subprocess.Popen([sys.executable, __file__, str(d - 1)],\n"
        "                         stdout=subprocess.PIPE, text=True)\n"
        "    print('%d %s' % (os.getpid(), p.stdout.readline().strip()),\n"
        "          flush=True)\n"
        "else:\n"
        "    print(os.getpid(), flush=True)\n"
        "time.sleep(180)\n")


def _kill_chain(pids):
    """Kill EVERY process in the chain, not only the one we started.

    `p.kill()` reaches the head and nothing else, and the head is the only
    thing subprocess knows about - the fixture is a CHAIN on purpose,
    because the question under test is parentage, and on Windows killing a
    parent leaves its children running. They exit on their own when their
    sleep runs out, which is why this looked like nothing for weeks; found
    2026-08-31 with three runs' worth still alive, one of them holding a
    directory open so it could not be deleted. A run that leaves a process
    behind will one day take a port or a file from the next run, and the red
    will look like a defect in the code.

    Returns the pids that were still there after the wait, so the caller
    checks a FACT rather than a hope. It used to be `os.kill(pid, 9)` and
    nothing else: on Windows that is TerminateProcess, which returns before
    the process is reaped, and the descendants are not Popen objects so
    there was nobody to wait on them. The check that followed therefore
    raced, and was green here and red on the planner's run of the same code.
    A longer sleep would only have moved the boundary (S5.38).
    """
    left = []
    for pid in pids:
        try:
            if not sessions.terminate_and_wait(pid, 30):
                left.append(pid)
        except Exception:
            left.append(pid)
    return left


def _alive(pid):
    """Is this pid still running?

    sessions.pid_alive, which is the project's own and correct on Windows
    (OpenProcess plus WaitForSingleObject - see 5.14, where asking only
    whether OpenProcess succeeded called every exited-but-unreaped child
    alive). Two wrong answers were tried first. `tasklist` through
    subprocess.run(text=True) hands back stdout=None on this machine: the
    console codepage is not UTF-8, the decode raises inside subprocess's own
    reader thread, and the exception is printed there while the attribute is
    left unset - a failure that arrives looking like an empty result. And
    `os.kill(pid, 0)`, which on Windows does not probe anything at all: it
    calls TerminateProcess with 0 as the exit code and KILLS what it was
    asked about.
    """
    return sessions.pid_alive(pid)


def _tree(depth):
    """A real chain of real processes: [self, child, grandchild, ...].

    Real ones because the whole question is parentage, and a fixture that
    hands the code the pids it wants to hear would answer it by assertion.
    """
    p = subprocess.Popen([sys.executable, _TREE, str(depth)],
                         stdout=subprocess.PIPE, text=True)
    pids = [int(x) for x in p.stdout.readline().split()]
    _TREES.append(p)
    _TREE_PIDS.extend(pids)
    return pids


_TREES = []
_TREE_PIDS = []
_W, _WC, _S = _tree(2)      # the recorded window, its channel, a subagent's
_X, _XC = _tree(1)          # a DIFFERENT window and its channel
note("recorded window / its channel / a process inside it", (_W, _WC, _S))
note("a second live window / its channel", (_X, _XC))
check("the fixture really is a chain, not three unrelated numbers",
      len({_W, _WC, _S, _X, _XC}), 5)

CH = os.path.join(TMP, "channel-seat")
os.makedirs(CH, exist_ok=True)
CHC = canon(CH)
_cfg51 = json.dumps(daemon.CFG.get("projects") or {})
with daemon._lock:
    daemon.CFG.setdefault("projects", {})[CHC] = {}
daemon.reg_pid(CH, "planner", _W)

print("   the window's own channel registers first, as it always does")
check("taken", post_rc("/channel/register",
                       {"project": CH, "role": "planner", "port": 40001,
                        "pid": _WC, "ppid": _W,
                        "session_id": "seat-1"})[1].get("ok"), True)
check("and holds the seat",
      ((daemon.STATE.get("channels") or {}).get("%s|planner" % CHC)
       or {}).get("pid"), _WC)

print("   SS5.19 unchanged: a channel started INSIDE that window inherits")
print("   PROJECT and ROLE and registers under the same key. It is younger,")
print("   so age alone would hand it the seat and every report would go")
print("   into a subagent. It must be refused - five times over, because")
print("   the repeat is what the new warning counts")
for _i in range(5):
    _r51 = post_rc("/channel/register",
                   {"project": CH, "role": "planner", "port": 40002,
                    "pid": _S, "ppid": _WC, "session_id": "seat-2"})[1]
    check("refused (%d of 5)" % (_i + 1), (_r51.get("ok"), _r51.get("why")),
          (False, "superseded"))
check("the window's channel still holds the seat",
      ((daemon.STATE.get("channels") or {}).get("%s|planner" % CHC)
       or {}).get("pid"), _WC)

print("   and the REPEAT is a fact of its own. Every refusal stays in the")
print("   journal - 232 of them are what let this be found - but one warn")
print("   says the thing they never said: this contender is not going away")
_warn51 = [r for r in daemon.store.recent_events(300, project=CH)
           if r.get("level") == "warn" and "has been refused" in (r.get("text")
                                                                  or "")]
check("said once, not five times", len(_warn51), 1)
check("and it names the contender, its parent and the seat's holder",
      all(str(x) in ((_warn51[0] if _warn51 else {}).get("text") or "")
          for x in (_S, _WC, _W)), True)

print("   the seat itself is NOT moved by this change, and that is a")
print("   decision. The same test refuses a second LIVE window - the app")
print("   forks the planner conversation into a new local window, and its")
print("   channel is a sibling, not a subagent. Three ways to tell them")
print("   apart were tried against the live machine and all three failed:")
print("   the process tree dies on an exited ancestor, channel.py sends no")
print("   session id, and no birth time is kept for a session. So the hole")
print("   is named and reported rather than closed with a guess")
_c51, _r51b = post_rc("/channel/register",
                      {"project": CH, "role": "planner", "port": 40003,
                       "pid": _XC, "ppid": _X, "session_id": "seat-3"})
check("a sibling window is refused too, today", _r51b.get("ok"), False)
check("and the seat has not moved",
      ((daemon.STATE.get("channels") or {}).get("%s|planner" % CHC)
       or {}).get("pid"), _WC)

print("   THE SABOTAGE (rule 19): with the counter never reaching its")
print("   threshold the warning is silent, and two hours of refusals say")
print("   nothing new again - which is the whole failure being repaired")
_saved51 = daemon.CHANNEL_REFUSE_TELL
try:
    daemon.CHANNEL_REFUSE_TELL = 10 ** 6
    with daemon._lock:
        (daemon.STATE.get("chan_refused") or {}).pop("%s|planner" % CHC,
                                                     None)
    print("   and the refusal line says only what age SHOWS. It used to end")
    print("   'so it is a leftover from a window that has been replaced',")
    print("   which age cannot establish: measured 2026-08-31 on a live")
    print("   pair, the refused contender's window was alive and its own")
    print("   transcript 30 minutes old - a second REAL planner, not a")
    print("   corpse, and the journal had said otherwise 902 times that day")
    _lines51 = [r.get("text") or "" for r in
                daemon.store.recent_events(300, project=CH)
                if "Refused a channel registration" in (r.get("text") or "")]
    check("refusals were journalled, or there is nothing to read",
          bool(_lines51), True)
    check("none of them asserts the contender is a leftover",
          [t for t in _lines51 if "it is a leftover" in t], [])
    check("and they do say the newer one keeps the seat",
          all("keeps the seat" in t for t in _lines51), True)
    print("   the same line also claimed 'its port is NOT being used'. That")
    print("   was false too, and the planner reasoned from it: netstat showed")
    print("   the contender LISTENING on that very port and a connection was")
    print("   accepted. It says what the BRIDGE will do now, not what the")
    print("   port is.")
    check("no refusal claims the contender's port is dead",
          [t for t in _lines51 if "NOT being used" in t], [])
    check("they say what the bridge will do instead",
          all("Nothing will be delivered" in t for t in _lines51), True)

    _before51 = len([r for r in daemon.store.recent_events(300, project=CH)
                     if "has been refused" in (r.get("text") or "")])
    for _i in range(6):
        post_rc("/channel/register",
                {"project": CH, "role": "planner", "port": 40002,
                 "pid": _S, "ppid": _WC, "session_id": "seat-2"})
    check("nothing new is said",
          len([r for r in daemon.store.recent_events(300, project=CH)
               if "has been refused" in (r.get("text") or "")]), _before51)
finally:
    daemon.CHANNEL_REFUSE_TELL = _saved51

_LEFT51 = _kill_chain(_TREE_PIDS)
for _p in _TREES:
    try:
        _p.wait(5)
    except Exception:
        pass
print("   and the fixture is cleaned up completely - every process in the")
print("   chain, not just the one subprocess knows about")
print("   FAILURE WOULD LOOK LIKE: p.kill() on the head alone, which is what")
print("   this was: the children outlived the run until their own sleep ran")
print("   out, and three runs' worth were found alive at once")
print("   THE CONTROL (rule 19): the check below can only mean something if")
print("   _alive can still SEE a live process at this point in the run. A")
print("   probe that has quietly started answering False for everything")
print("   would make an empty list look like a clean fixture. Spawned")
print("   outside _TREE_PIDS on purpose, so this cleanup cannot touch it.")
_ctl51 = subprocess.Popen([sys.executable, "-c",
                           "import time; time.sleep(60)"])
check("a process nobody has killed reads as alive", _alive(_ctl51.pid), True)
check("terminate_and_wait says it is gone",
      sessions.terminate_and_wait(_ctl51.pid, 30), True)
check("and the probe agrees, with no sleep in between",
      _alive(_ctl51.pid), False)
# Never let the control CRASH the suite. When the sabotage above is real -
# terminate_and_wait lying, which is exactly what this control exists to
# catch - the process is still running, wait(5) raises TimeoutExpired, and
# the run dies before it prints its FAIL summary. That is S5.9: a suite that
# crashes instead of reporting is not a gate. It is also rule 9: the control
# must not be the thing that leaks.
try:
    _ctl51.wait(5)
except Exception:
    try:
        _ctl51.kill()
        _ctl51.wait(5)
    except Exception:
        pass

check("kill_chain reports nothing left behind", _LEFT51, [])
check("no process of this fixture is left running",
      [pid for pid in _TREE_PIDS if _alive(pid)], [])
with daemon._lock:
    daemon.CFG["projects"] = json.loads(_cfg51)
    (daemon.STATE.get("chan_refused") or {}).pop("%s|planner" % CHC, None)
    daemon.save_state()

print("\n52. the model chosen in the panel is the model the window starts on")
print("    2026-08-30, the owner: he chose Opus for BOTH halves in the Launch")
print("    window and an ordinary pair came up. The choice never reached the")
print("    POST. launchRole() sends CHAINS[role][0], and the drop-down fed")
print("    only the add button, which APPENDS - so a planner whose saved")
print("    chain was [fable] started fable however Opus was picked, and")
print("    STATE['pids'] kept model_req='fable' against it as the proof.")
print("    Two halves, because the break was in one of them and the other")
print("    had to be cleared by fact: the daemon end really does carry a")
print("    named model all the way to argv, and the panel now has a gesture")
print("    that puts one at the head of the chain and keeps it there.")

MP52 = A
_pre52 = len(launches())
r = post("/session", {"action": "launch", "project": MP52,
                      "role": "executor", "model": "sonnet"})
check("the launch was accepted", r.get("ok"), True)
check("a window came up", until(lambda: len(launches()) > _pre52, 30), True)
_av52 = launches()[-1]["argv"]
check("and it was started on the model that was asked for, not on a default",
      ("--model" in _av52,
       _av52[_av52.index("--model") + 1] if "--model" in _av52 else None),
      (True, "sonnet"))
post("/session", {"action": "stop", "project": MP52, "role": "executor"})

print("    THE SABOTAGE (rule 19): a check that reads argv would pass on a")
print("    constant just as happily, so the same path is driven again with a")
print("    different choice, and once more with none at all. If the choice")
print("    were being dropped anywhere between the POST and CreateProcess,")
print("    these three could not disagree with each other.")
_pre52b = len(launches())
post("/session", {"action": "launch", "project": MP52, "role": "planner",
                  "model": "haiku"})
check("a different choice starts a different model",
      until(lambda: len(launches()) > _pre52b, 30), True)
_av52b = launches()[-1]["argv"]
check("carried through the same path, unchanged",
      _av52b[_av52b.index("--model") + 1] if "--model" in _av52b else None,
      "haiku")
post("/session", {"action": "stop", "project": MP52, "role": "planner"})

_pre52c = len(launches())
post("/session", {"action": "launch", "project": MP52, "role": "executor"})
check("and with nothing chosen the client keeps its own default",
      until(lambda: len(launches()) > _pre52c, 30), True)
check("no --model is invented for it",
      "--model" in launches()[-1]["argv"], False)
post("/session", {"action": "stop", "project": MP52, "role": "executor"})

check("an alias is passed through as an alias, so a new release needs no edit",
      daemon.models.resolve("opus", {"opts": {"prefer_aliases": True}}),
      "opus")
check("and pinned to the concrete id when the owner asked for that instead",
      daemon.models.resolve("opus", {"opts": {"prefer_aliases": False},
                                     "map": {"opus": {"id": "claude-opus-5"}}}),
      "claude-opus-5")

_panel52 = open(os.path.join(os.path.dirname(daemon.__file__), "panel.html"),
                encoding="utf-8").read()
check("the panel launches on the head of the chain and nothing else",
      "(CHAINS[role]||[])[0]" in _panel52, True)
check("there is a gesture that puts a chosen model at that head",
      'data-first="executor"' in _panel52 and 'data-first="planner"'
      in _panel52, True)
_seg52 = _panel52[_panel52.index('$$("[data-first]")'):]
_seg52 = _seg52[:_seg52.index("renderLaunch()")]
check("it moves the model rather than copying it, so the ladder keeps one "
      "entry per model",
      ("rest.unshift(sel.value)" in _seg52,
       "filter(function(m){return m!==sel.value})" in _seg52), (True, True))
check("and it latches, so the render 2.5s later cannot throw the choice "
      "away (the class of 5.8)",
      "window._launchTouched=true" in _seg52, True)
check("the window says which chip is the one that starts",
      "starts here" in _panel52, True)
# Since 2026-09-23 the pick lives in window._picked, set on "change": a
# render puts it back from there, and with no pick shows the head of the
# role's chain - what starts - instead of the first model on the list.
# -> DECISIONS.md 8.26
check("and the drop-down keeps what was picked across a render",
      ("var pick=(window._picked||{})[role];" in _panel52,
       "if(pick&&models.indexOf(pick)>=0)sel.value=pick;" in _panel52),
      (True, True))

print("\n53. a live pid is not a live session - the stuck window of 18:27:46")
print("    2026-08-30. rotate_executor opened a replacement at 18:27:46,")
print("    pid 12472. No SessionStart came from it until 21:47:23 - three")
print("    hours nineteen minutes on a startup dialog nobody could see,")
print("    because the window is born minimised. check_sessions said so")
print("    once, at 18:38:03, and set gave_up on the record. Nothing else")
print("    read that field: already_up asked pid_alive, got True, and")
print("    answered 'its window is still running', so clinch() passed its")
print("    all(alive) test and reported a pair waiting on itself - fifteen")
print("    times between 18:44 and 21:34, never once naming the real fact")
print("   a real process, a real reg_pid, a real check tick. The pid has to")
print("   be genuinely alive or pid_alive is not being asked anything")
STUCK = os.path.join(TMP, "stuck-window")
os.makedirs(STUCK, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, STUCK: {}}})
_sleeper = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(600)"])
_k53 = "%s|executor" % canon(STUCK)
try:
    check("the stand-in window really is running",
          daemon.sessions.pid_alive(_sleeper.pid), True)
    daemon.reg_pid(STUCK, "executor", _sleeper.pid, model_req="opus")
    check("and reg_pid wrote it unregistered, as every launch path does",
          (daemon.STATE["pids"][_k53]).get("registered"), False)

    print("   the pair as it stood at 18:38: planner answering, executor")
    print("   retired by the rotation, and only the pid left to speak for it")
    with daemon._lock:
        daemon.STATE.setdefault("sessions", {})["planner:stuckpln"] = {
            "role": "planner", "path": canon(STUCK),
            "session_id": "stuckpln-0000", "model": "Opus 5",
            "state": "idle", "last_seen": daemon.now(),
            "seen_at": time.time()}
        daemon.STATE["sessions"]["executor:stuckexe"] = {
            "role": "executor", "path": canon(STUCK),
            "session_id": "stuckexe-0000", "model": "Opus 5",
            "state": "ended", "last_seen": daemon.now(),
            "seen_at": time.time() - 12000}
        daemon.STATE.setdefault("loops", {})[canon(STUCK)] = {
            "active": True, "iteration": 406}
        daemon.STATE.setdefault("stop_seen", {})[_k53] = time.time() - 4000
        daemon.save_state()
    # THE WINDOW'S AGE MOVES BY THE DAEMON'S CLOCK, NOT BY A FALSE PAST ON
    # ITS RECORD. This put the record 700 s back, over a process born
    # seconds ago - a launch record older than the process it names, which
    # no writer produces and the one definition reads as a number passed to
    # somebody else (8.46). reg_pid's own time stands; startup_grace is set
    # to 10 s - it is also the reminder's interval, so the steps between
    # the first tick and the "not repeated" one must fit inside it, and at
    # 2 s under a loaded acceptance they did not - and the record is let
    # grow 10 s old for real.
    _grace53 = daemon.CFG["thresholds"].get("startup_grace")
    daemon.CFG["thresholds"]["startup_grace"] = 10
    _at53 = float(daemon.STATE["pids"][_k53].get("at") or 0)
    until(lambda: time.time() - _at53 > 10.05, 15)


    def _tick53():
        # THE TICK SEES THIS PAIR'S RECORD ALONE, as case 127's does. At a
        # 10 s grace every other fixture's window that never registered is
        # judged too - alpha's planner, opened seconds earlier, was
        # announced into this case's list - where 600 s had hidden them.
        with daemon._lock:
            _all = dict(daemon.STATE.get("pids") or {})
            daemon.STATE["pids"] = {k: v for k, v in _all.items()
                                    if k == _k53}
        try:
            daemon.check_sessions(0)
        finally:
            with daemon._lock:
                _mine = (daemon.STATE.get("pids") or {}).get(_k53)
                _all.pop(_k53, None)
                if _mine is not None:
                    _all[_k53] = _mine
                daemon.STATE["pids"] = _all
                daemon.save_state()

    check("the executor is NOT called alive on a pid alone",
          bool(daemon.already_up(STUCK, "executor")), False)
    check("the planner still is, on its own record",
          bool(daemon.already_up(STUCK, "planner")), True)

    print("   the real tick, not a snapshot of what a tick would have seen")
    _told53 = []
    _realn53, daemon.notify = daemon.notify, \
        lambda kind, text, **kw: _told53.append((kind, text))
    _started53 = daemon.STATE.get("started_at")
    try:
        with daemon._lock:
            daemon.STATE["started_at"] = time.time() - 9000
            daemon.save_state()
        _tick53()
        check("the watchdog named the stuck window",
              any(k == "session_died" and "never started" in t
                  for k, t in _told53), True)
        check("and wrote down that it has told once",
              (daemon.STATE["pids"][_k53]).get("told_n"), 1)

        print("   THIS is the failure shape: with the pair read as two live")
        print("   halves, tier 1 calls it a clinch and wakes the wrong thing")
        _sit53 = daemon.situation(STUCK)
        check("clinch says nothing about a half that never came up",
              daemon.clinch(STUCK, _sit53), None)
        print("   the control - flip the one bit back and the old report")
        print("   returns, so the check could have failed")
        with daemon._lock:
            daemon.STATE["pids"][_k53]["registered"] = True
            daemon.save_state()
        _sitc = daemon.situation(STUCK)
        _clc = daemon.clinch(STUCK, _sitc)
        check("with registered=True it is a clinch again",
              (_clc or {}).get("why"), "report_never_arrived")
        check("and that is the line the owner read fifteen times",
              "no report reached the planner" in (_clc or {}).get("said", ""),
              True)
        with daemon._lock:
            daemon.STATE["pids"][_k53]["registered"] = False
            daemon.save_state()

        print("   and nothing opens a second window over the stuck one -")
        print("   launch_guard refuses while that pid is alive, which it did")
        print("   NOT do before: its refusal lapsed at startup_grace")
        _lg = daemon.launch_guard(STUCK, "executor")
        check("a second window is refused", bool(_lg), True)
        check("and the refusal names why", "startup dialog" in (_lg or ""),
              True)

        print("   said again, further apart each time - it used to be said")
        print("   once, ever, and the window sat there for three hours")
        _told53[:] = []
        _tick53()
        check("not repeated before the gap is up", _told53, [])
        with daemon._lock:
            daemon.STATE["pids"][_k53]["told_at"] = time.time() - 1300
            daemon.save_state()
        _tick53()
        check("repeated once the gap is up",
              any(k == "session_died" for k, t in _told53), True)
        check("and the count grew, so the next gap is wider",
              (daemon.STATE["pids"][_k53]).get("told_n"), 2)
    finally:
        daemon.notify = _realn53
        with daemon._lock:
            daemon.STATE["started_at"] = _started53
            daemon.save_state()

    print("   and when a person finally answers the dialog, the record is")
    print("   clean again - gave_up outlived its window in the live state")
    print("   of 2026-08-30, on a session that had come up")
    _r53 = post("/event", {"hook_event_name": "SessionStart", "cwd": STUCK,
                           "role": "executor", "session_id": "stuck-new-1"})
    check("the SessionStart went in", _r53.get("status"), 200)
    check("the record is registered", (daemon.STATE["pids"][_k53]).get(
        "registered"), True)
    check("gave_up went with it", "gave_up" in daemon.STATE["pids"][_k53],
          False)
    check("and so did the cadence", "told_n" in daemon.STATE["pids"][_k53],
          False)
    check("the executor is alive again",
          bool(daemon.already_up(STUCK, "executor")), True)
finally:
    # Rule 9: the stand-in process goes in the turn that made it.
    _sleeper.kill()
    _sleeper.wait(timeout=10)
    if "_grace53" in globals():
        if _grace53 is None:
            daemon.CFG["thresholds"].pop("startup_grace", None)
        else:
            daemon.CFG["thresholds"]["startup_grace"] = _grace53


print("\n54. a reader of state.json does not crash the daemon")
print("    2026-08-30 22:00:26. A plain /status POST went touch_session ->")
print("    remember_session -> save_state and died on WinError 5 replacing")
print("    state.json - a whole crash bundle out of a status line redraw,")
print("    and that save lost. Nothing was wrong with the write: on Windows")
print("    os.replace fails while ANY other process holds the destination")
print("    open, and CPython's open() for reading does exactly that. The")
print("    reader that afternoon was this bridge's own investigation")
print("   two halves, because one is not enough. A retry wins against a")
print("   reader that lets go; nothing wins against a handle held open, so")
print("   the write is allowed to LOSE rather than come back up the stack")
_sp54 = os.path.join(TMP, "atomic-probe.json")
daemon.store._write_atomic(_sp54, {"n": 1})
_held54 = None
_real54 = daemon.store.STATE_PATH
try:
    print("   half one: a reader that lets go loses to the retry")
    _slow = []

    def _one_shot_reader(path):
        fh = open(path, "r", encoding="utf-8")
        _slow.append(fh)
        threading.Timer(0.12, fh.close).start()

    _one_shot_reader(_sp54)
    daemon.store._write_atomic(_sp54, {"n": 2})
    with open(_sp54, encoding="utf-8") as fh:
        check("the write went through once the reader let go",
              json.load(fh).get("n"), 2)

    print("   half two: a handle held for the whole window. The save loses")
    print("   - and losing must not reach the caller, because the caller is")
    print("   a hook posting /status")
    _held54 = open(_sp54, "r", encoding="utf-8")
    daemon.store.STATE_PATH = _sp54
    _n54 = daemon.store._state_write_fails[0]
    try:
        daemon.store.save_state({"n": 3})
        check("save_state did not raise with the file held", True, True)
    except OSError as exc:
        check("save_state did not raise with the file held", repr(exc), True)
    check("and it counted the loss instead of hiding it",
          daemon.store._state_write_fails[0] > _n54, True)
    print("   the complete data is on disk beside it, so nothing serialised")
    print("   was lost even though the replace never happened")
    with open(_sp54 + ".tmp", encoding="utf-8") as fh:
        check("the .tmp holds what the save was carrying",
              json.load(fh).get("n"), 3)
    print("   and the next save carries it, which is why losing one is")
    print("   survivable for STATE and would not be for config.json")
    _held54.close()
    daemon.store.save_state({"n": 4})
    with open(_sp54, encoding="utf-8") as fh:
        check("the next save landed", json.load(fh).get("n"), 4)
    check("and the run of failures was reset by it",
          daemon.store._state_write_fails[0], 0)
finally:
    daemon.store.STATE_PATH = _real54
    try:
        if _held54:
            _held54.close()
    except Exception:
        pass
    for _fh in _slow:
        try:
            _fh.close()
        except Exception:
            pass

print("   the failure shape, said out loud: without the retry half one")
print("   raises PermissionError; without save_state absorbing it, half two")
print("   is the 22:00:26 crash bundle again. Both are Windows-only at")
print("   runtime, so the wiring is asserted on every platform too")
_src54 = inspect.getsource(daemon.store._write_atomic)
check("_write_atomic goes through the retry", "_replace_with_retry" in _src54,
      True)
_ret54 = inspect.getsource(daemon.store._replace_with_retry)
check("which retries the two Windows codes that mean 'somebody has it open'",
      "(5, 32)" in _ret54, True)
check("and raises on anything else rather than hiding it",
      "raise" in _ret54, True)
check("bounded, so a file that is genuinely locked is still reported",
      2 <= daemon.store.REPLACE_TRIES <= 20, True)
_ss54 = inspect.getsource(daemon.store.save_state)
check("only state.json is allowed to lose a write",
      "_write_atomic(STATE_PATH, state)" in _ss54, True)
_sc54 = inspect.getsource(daemon.store.save_config)
check("config.json still raises, because it is written rarely",
      "except OSError" in _sc54, False)


print("\n55. a prompt-too-long with no PreCompact is the cure starting")
print("    2026-08-30 18:27:43. The API answered `prompt is too long:")
print("    1000815 tokens > 1000000 maximum` and NO PreCompact ever came -")
print("    not before, not after. compaction_pending is written by the")
print("    PreCompact branch and by nothing else, so the only witness")
print("    wait_for_compaction knew about could not exist; it returned")
print("    False and handle_wall_hit ran in the same second, killing a")
print("    session that had compacted at 998 851 the day before, 2-3")
print("    seconds into its own recovery. Rule 30 in its least obvious")
print("    form: not a forged witness, but one the event makes impossible")


def _ovf_pair(tag, size=1000815):
    """A fresh project with a live pair, a size on record and the loop on."""
    d = os.path.join(TMP, tag)
    os.makedirs(d, exist_ok=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}, d: {}}})
    post_rc("/loop", {"action": "start", "project": d})
    for role, sid in (("executor", tag + "-ex"), ("planner", tag + "-pl")):
        post_rc("/event", {"hook_event_name": "SessionStart", "role": role,
                           "session_id": sid, "project_dir": d, "cwd": d})
        post_rc("/status", {"role": role, "payload": {
            "session_id": sid,
            "workspace": {"current_dir": d, "project_dir": d},
            "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
            "context_window": {"context_window_size": 1000000,
                               "used_percentage": 99.0,
                               "current_usage": {
                                   "input_tokens": 10,
                                   "cache_creation_input_tokens": 90,
                                   "cache_read_input_tokens": 999_000,
                                   "output_tokens": 4000}}}})
    return d, canon(d), "%s|executor" % canon(d)


def _too_long(d, sid, size=1000815):
    """The death exactly as the client sends it - the numbers live in
    `error_details`, which is not one of ERROR_KEYS, so the journal line for
    the real one said `invalid_request` and the diagnosis sat one field
    away."""
    return post("/event", {
        "hook_event_name": "StopFailure", "cwd": d, "role": "executor",
        "session_id": sid, "error": "invalid_request",
        "error_details": ('400 {"type":"error","error":{"type":'
                          '"invalid_request_error","message":"prompt is too '
                          'long: %d tokens > 1000000 maximum"}}' % size)})


print("   gate 1: the death goes in as a real POST, with no PreCompact")
print("   FAILURE WOULD LOOK LIKE: a launch appears in the stub's log, or")
print("   compact_wait stays empty - that is the 18:27:43 kill, unchanged")
OVF, OVFC, OVFK = _ovf_pair("overflow-1")
_before55 = len(launches())
_r55 = _too_long(OVF, "overflow-1-ex")
check("the bridge took the StopFailure", _r55.get("status"), 200)
check("no PreCompact was ever recorded for this pair",
      (daemon.STATE.get("compactions") or {}).get(OVFK), None)
check("nothing was rotated", len(launches()), _before55)
_w55 = (daemon.STATE.get("compact_wait") or {}).get(OVFK) or {}
check("the wait was written down", bool(_w55), True)
check("and it says which kind of wait it is", _w55.get("via"),
      "prompt_too_long")
check("at the size the API REFUSED, not the size drawn before the turn",
      _w55.get("tokens"), 1000815)
print("   and the real tick changes nothing while the wait is young")
daemon.assess(OVF)
check("still no rotation after a real assess() tick", len(launches()),
      _before55)
check("and the wait is still standing",
      bool((daemon.STATE.get("compact_wait") or {}).get(OVFK)), True)

print("   gate 2: the summary lands - a smaller reading arrives")
print("   FAILURE WOULD LOOK LIKE: a launch in the log, or")
print("   compaction_failed_at returning a number for a compaction that")
print("   worked - which would then poison the ceiling for ever")
post_rc("/status", {"role": "executor", "payload": {
    "session_id": "overflow-1-ex",
    "workspace": {"current_dir": OVF, "project_dir": OVF},
    "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
    "context_window": {"context_window_size": 1000000,
                       "used_percentage": 11.0,
                       "current_usage": {"input_tokens": 10,
                                         "cache_creation_input_tokens": 90,
                                         "cache_read_input_tokens": 108_777,
                                         "output_tokens": 4000}}}})
daemon.assess(OVF)
check("the wait was dropped", (daemon.STATE.get("compact_wait") or {}).get(
    OVFK), None)
check("nothing was replaced", len(launches()), _before55)
check("and NOTHING was recorded as a failure",
      daemon.compaction_failed_at(OVF, "executor"), None)

print("   gate 3: the summary never lands. Only now is it a failure, and")
print("   THIS is the half that teaches the ceiling (A2)")
print("   FAILURE WOULD LOOK LIKE: no launch at all - the session left to")
print("   die at the wall - or compaction_failed_at still None, which is")
print("   today's live state: the bridge learned nothing from a real death")
OVF2, OVF2C, OVF2K = _ovf_pair("overflow-2")
_before3 = len(launches())
_too_long(OVF2, "overflow-2-ex")
check("the wait was written", bool((daemon.STATE.get("compact_wait") or {})
                                   .get(OVF2K)), True)
# A START WITNESS, which this case did not need until 2026-09-02. A wait
# that times out is only a failed COMPACTION if a compaction ever began,
# and the bridge's own timer is not evidence that one did: on the night of
# 2026-09-02 nothing began at all and the bridge still wrote the ceiling a
# number (-> compaction_failure_evidence, case 66 gate (e)). The client
# does announce these - the pair in that incident has PreCompacts on
# record for every compaction it really ran - so this is the ordinary
# shape, and gate 1 above still ran with no PreCompact anywhere.
post_rc("/event", {"hook_event_name": "PreCompact", "role": "executor",
                   "session_id": "overflow-2-ex", "project_dir": OVF2,
                   "cwd": OVF2})
with daemon._lock:
    daemon.STATE["compact_wait"][OVF2K]["at"] = \
        time.time() - daemon.COMPACT_RECOVERY_SEC - 5
    daemon.save_state()
daemon.assess(OVF2)
check("the failure is on record now",
      daemon.compaction_failed_at(OVF2, "executor"), 1000815)
print("   rotate_executor runs in a thread and sleeps two seconds before it")
print("   launches, so the replacement is waited for rather than assumed")
check("and the session was replaced",
      until(lambda: len(launches()) > _before3, 20), True)
_n3 = len(launches())
daemon.assess(OVF2)
check("a second tick does not replace it again",
      until(lambda: len(launches()) > _n3, 4), False)
check("because the wait is gone", (daemon.STATE.get("compact_wait") or {})
      .get(OVF2K), None)

print("   A2, proven on its own: the recorded failure is what closes the")
print("   ceiling. These are the live numbers of 2026-08-30 - one")
print("   successful compaction at 998 851 with a floor of 108 877, and")
print("   the refusal at 1 000 815")
with daemon._lock:
    daemon.STATE.setdefault("compactions", {})[OVF2K] = [
        {"at": "2026-08-29 17:01", "tokens": 998851, "after": 108877,
         "session": "overflow-2-ex"}]
    daemon.save_state()
daemon.note_turn_cost(OVF2, "executor", 200274, "overflow-2-ex")
_prov = daemon.compaction_survivable(OVF2, "executor")
_fail = daemon.compaction_failed_at(OVF2, "executor")
_top = daemon.compaction_too_big(OVF2, "executor", 1000000)
check("the success below the failure still counts", _prov, 998851)
_ordA, _wideA = (daemon.turn_ordinary(OVF2, "executor")[0],
                 daemon.turn_widest(OVF2, "executor")[0])
check("and the ceiling is the planner's arithmetic", _top,
      min(_prov + _ordA, _fail - _wideA))
check("which puts the compaction point ABOVE the ceiling - rule 1a's test",
      998851 > _top, True)
print("   the control for THAT: with the failure removed, the same numbers")
print("   leave the point below the ceiling and rule 1a stays silent")
with daemon._lock:
    (daemon.STATE.get("compact_failed") or {}).pop(OVF2K, None)
    daemon.save_state()
check("no failure on record, no early rotation",
      998851 > daemon.compaction_too_big(OVF2, "executor", 1000000), False)

print("   gate 4: the control - a PreCompact DID arrive. This is the")
print("   planner's 03:43 on 2026-08-30, which survived, and it must go on")
print("   behaving exactly as it did")
print("   FAILURE WOULD LOOK LIKE: via reading prompt_too_long, or the")
print("   recorded size being the refused one instead of the carried one")
OVF3, OVF3C, OVF3K = _ovf_pair("overflow-3")
_before4 = len(launches())
post_rc("/event", {"hook_event_name": "PreCompact", "role": "executor",
                   "session_id": "overflow-3-ex", "project_dir": OVF3,
                   "cwd": OVF3})
_too_long(OVF3, "overflow-3-ex")
_w4 = (daemon.STATE.get("compact_wait") or {}).get(OVF3K) or {}
check("the announced path is the one that was taken", _w4.get("via"),
      "precompact")
check("and it carries the size the PreCompact recorded, not the refusal",
      _w4.get("tokens"), 999100)
check("nothing was rotated", len(launches()), _before4)
print("   and it still lands the way it did on 2026-08-30 at 03:45:48")
post_rc("/status", {"role": "executor", "payload": {
    "session_id": "overflow-3-ex",
    "workspace": {"current_dir": OVF3, "project_dir": OVF3},
    "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
    "context_window": {"context_window_size": 1000000,
                       "used_percentage": 7.0,
                       "current_usage": {"input_tokens": 10,
                                         "cache_creation_input_tokens": 90,
                                         "cache_read_input_tokens": 66_409,
                                         "output_tokens": 4000}}}})
daemon.assess(OVF3)
check("the wait was dropped", (daemon.STATE.get("compact_wait") or {}).get(
    OVF3K), None)
check("nothing was replaced", len(launches()), _before4)
check("and no failure was invented", daemon.compaction_failed_at(
    OVF3, "executor"), None)

print("   and the doorway is still a doorway: an invalid_request that is")
print("   NOT an overflow is handled at once, as it always was")
print("   FAILURE WOULD LOOK LIKE: a wait record - a broken request left")
print("   sitting for ten minutes before anyone looked at it")
check("a malformed request names no overflow",
      daemon.overflow_said({"error": "invalid_request",
                            "error_details": "400 tool schema is wrong"}),
      None)
check("the client's own sentence does", daemon.overflow_said(
    {"error_details": "prompt is too long: 1000815 tokens > 1000000 maximum"}),
    (1000815, 1000000))
check("and a rate limit was never in this branch at all",
      "rate" in inspect.getsource(daemon.handle_event), True)


print("\n56. how big a turn is, is MEASURED - per pair, and it is two numbers")
print("    LARGEST_TURN_SEEN = 200274 was a literal dated 2026-08-20 and")
print("    called 'the largest single turn this bridge has ever measured'.")
print("    Measured 2026-08-31 over 826 turns in 57 sessions: one pair's")
print("    max 432 609, another's 532 910, and 523 857 of those was")
print("    measured on 2026-08-20 itself. It was never the largest turn")
print("    seen - it was the turn that killed one session - and it was")
print("    used with two opposite meanings in the same function")
print("   the costs go in the way real ones do: statuses and Stop hooks")
print("   through /event, so the recording is the code that records")


def _turn_run(tag, costs, start=200000):
    """A pair that really takes turns. Loop off, so the Stop hook records
    the cost and then goes home instead of blocking on a review."""
    d = os.path.join(TMP, tag)
    os.makedirs(d, exist_ok=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}, d: {}}})
    sid = tag + "-ex"
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": sid, "project_dir": d, "cwd": d})
    size = start
    for i, cost in enumerate([0] + list(costs)):
        size += cost
        post_rc("/status", {"role": "executor", "payload": {
            "session_id": sid,
            "workspace": {"current_dir": d, "project_dir": d},
            "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
            "context_window": {"context_window_size": 1000000,
                               "used_percentage": size / 10000.0,
                               "current_usage": {
                                   "input_tokens": 0,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": size,
                                   "output_tokens": 100}}}})
        post_rc("/event", {"hook_event_name": "Stop", "role": "executor",
                           "session_id": sid, "project_dir": d, "cwd": d,
                           "last_assistant_message": "%s turn %d" % (tag, i)})
    return d, canon(d), "%s|executor" % canon(d), sid


_COSTS = [10000, 20000, 30000, 40000, 50000, 60000, 70000, 80000, 90000,
          400000]
TW1, TW1C, TW1K, TW1S = _turn_run("turns-one", _COSTS)
_rec56 = (daemon.STATE.get("turns") or {}).get(TW1K) or {}
check("every turn was recorded, through the real hook",
      _rec56.get("sample"), _COSTS)
check("and the total counted", _rec56.get("n"), len(_COSTS))
print("   attributed, not floating: the session that produced the biggest")
print("   one is written down beside it. That is the whole of")
print("   ANALYSIS-compaction-point.md - one session's size recorded as")
print("   another's measurement - and it must not happen in a new place")
check("the biggest is kept with the session that made it",
      (_rec56.get("biggest"), _rec56.get("biggest_session")),
      (400000, TW1S))

print("   TWO figures off one sample, because the old constant answered two")
print("   questions with one number: `proven + L` wants an ORDINARY turn,")
print("   `fail - L` wants the WIDEST one. On one real pair's numbers")
print("   `proven + max` is 1 531 761 - above the window, so that branch")
print("   could never fire - and `fail - max` is 467 905, which calls a")
print("   session carrying half its window doomed")
check("widest is the widest", daemon.turn_widest(TW1, "executor"),
      (400000, "measured"))
check("ordinary is not - the freak turn is the top decile",
      daemon.turn_ordinary(TW1, "executor"), (90000, "measured"))

print("   THE SUBSTITUTION CHECK. A pair with no turns of its own borrows,")
print("   and the borrowing is VISIBLE. Failure here looks like taking")
print("   somebody else's maximum and calling it measured")
TW2 = os.path.join(TMP, "turns-two")
os.makedirs(TW2, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, TW1: {}, TW2: {}}})
_w2, _s2 = daemon.turn_widest(TW2, "executor")
check("a pair with nothing of its own gets a number", bool(_w2), True)
check("and it is NOT called measured", _s2, "fallback")
check("nor is the planner of a pair that only measures its executor",
      daemon.turn_widest(TW1, "planner")[1], "fallback")
print("   and the moment it has its own, it stops borrowing")
TW3, TW3C, TW3K, TW3S = _turn_run("turns-three", [5000, 6000, 7000])
check("its own numbers, said to be its own",
      daemon.turn_widest(TW3, "executor"), (7000, "measured"))
check("and they are its own and not the loud neighbour's",
      daemon.turn_widest(TW3, "executor")[0] < 400000, True)

print("   THE COLD START. STATE['turns'] fills from Stop hooks, so for a")
print("   while after every restart it is empty for every pair - and empty")
print("   means rule 1b grants no exception at all. The pair's own answer is")
print("   on disk the whole time: its session record carries its last turn")
print("   costs. Without this the daemon is stricter for its first minutes")
print("   than it is an hour later, which is a difference nobody asked for")
_keep56 = daemon.STATE["turns"].pop(TW1K)
# by best_session, not by a hand-built key: a session record is keyed by
# "<role>:<sid8>" and TW1S is longer than eight characters, so the hand-built
# one silently found nothing and the check read as "no costs on the record"
_costs56 = (daemon.best_session(TW1, "executor") or {}).get("turn_costs") or []
check("the session record has the pair's turns on it", bool(_costs56), True)
check("so an empty sample is not an empty answer",
      daemon.turn_widest(TW1, "executor"), (max(_costs56), "measured"))
check("and it is its own record, not the neighbour's history",
      daemon.turn_widest(TW1, "executor")[0]
      != daemon.turn_widest(TW2, "executor")[0], True)
daemon.STATE["turns"][TW1K] = _keep56
check("the recorded sample wins the moment there is one",
      daemon.turn_widest(TW1, "executor"), (400000, "measured"))

print("   in the inventory, on the day it was written - a container that is")
print("   not in STATE_PATHS is invisible to a project move AND to")
print("   /forget-project (cases 49 and 50)")
check("the inventory names it, as a pair key",
      daemon.STATE_PATHS.get("turns"), "pair")
_fc56, _fr56 = post_rc("/forget-project", {"path": TW3})
check("forgetting it is accepted - ok, not merely a 200: the refusal "
      "for a live pair answers 200 as well",
      (_fc56, _fr56.get("ok")), (200, True))
check("and its turns went with it",
      (daemon.STATE.get("turns") or {}).get(TW3K), None)
check("while the other pair's are untouched",
      bool((daemon.STATE.get("turns") or {}).get(TW1K)), True)

print("   consumer 1 of 4 - compaction_point's sample filter. The claim is")
print("   'these two cannot be overshoots of the same threshold', and it is")
print("   a claim about turn sizes, so it is made with the pair's own")
# Oldest first: the anchor is the NEWEST sample (that is compaction_point's
# whole reasoning - the newest was written under the configuration in force)
_far = [700100, 996305]
check("a distant sample is dropped when a turn cannot span the gap",
      daemon.compaction_point(_far, 100000), 996305)
check("and kept when it can", daemon.compaction_point(_far, 400000), 700100)
check("with nothing measured the claim is not made at all",
      daemon.compaction_point(_far, None), 700100)

print("   consumers 2 and 3 - the two branches of compaction_too_big, now")
print("   with two different numbers in them")
with daemon._lock:
    daemon.STATE.setdefault("compactions", {})[TW1K] = [
        {"at": "2026-08-29 17:01", "tokens": 600000, "after": 100000,
         "session": TW1S}]
    daemon.save_state()
check("with no failure it is proven + ORDINARY",
      daemon.compaction_too_big_why(TW1, "executor", 1000000)["proven"]
      is not None
      and 600000 + 90000 == 690000, True)
print("   ...but 690k is BELOW window - RESERVED_TOKENS, and a proven")
print("   compaction may only RAISE this line. 5.32 put the branch here to")
print("   lift it over an unmeasured reserve; written as a replacement it")
print("   cut the other way too, and on 2026-09-02 a success at 475k set a")
print("   559k ceiling on a 1M window and replaced a healthy executor (5.45)")
check("so the answer is the reserve line, the higher of the two",
      daemon.compaction_too_big(TW1, "executor", 1000000),
      1000000 - daemon.RESERVED_TOKENS)
check("and it says which of the two it used",
      daemon.compaction_too_big_why(TW1, "executor",
                                    1000000)["source"],
      "window minus the 33k compaction reserve")
print("   and the branch still lifts, which is what it is for: a pair whose")
print("   proven size is already near the window gets the higher line")
with daemon._lock:
    daemon.STATE["compactions"][TW1K] = [
        {"at": "2026-08-29 17:01", "tokens": 950000, "after": 100000,
         "session": TW1S}]
    daemon.save_state()
check("proven + ordinary when THAT is the larger",
      daemon.compaction_too_big(TW1, "executor", 1000000), 950000 + 90000)
check("and it says so",
      "compacting at and surviving" in
      daemon.compaction_too_big_why(TW1, "executor", 1000000)["source"], True)
with daemon._lock:
    daemon.STATE["compactions"][TW1K] = [
        {"at": "2026-08-29 17:01", "tokens": 600000, "after": 100000,
         "session": TW1S}]
    daemon.save_state()
daemon.note_compaction_failed(TW1, "executor", 900000)
check("and with one it is capped by failure - WIDEST",
      daemon.compaction_too_big(TW1, "executor", 1000000),
      min(600000 + 90000, 900000 - 400000))
print("   the difference is the point: one number for both would have to be")
print("   90000 in one branch and 400000 in the other on this same pair")
check("the two figures are not the same number",
      daemon.turn_ordinary(TW1, "executor")[0]
      != daemon.turn_widest(TW1, "executor")[0], True)
_why56 = daemon.compaction_too_big_why(TW1, "executor", 1000000)
check("and the provenance travels with the answer",
      (_why56["turn_widest_source"], _why56["turn_ordinary_source"]),
      ("measured", "measured"))

print("   consumer 4 - rule 1b's 'an overshoot is at most one turn wide'.")
print("   With the literal, a pair whose turns are 30k was granted 200k")
_1b = inspect.getsource(daemon.plan_for)
check("1b asks this pair, not a constant",
      "_wide, _wide_src = turn_widest(path, _rrole)" in _1b, True)
check("and grants no exception when nothing has been measured",
      "bool(_wide) and compact < wall" in _1b, True)
check("the literal is gone from the code",
      hasattr(daemon, "LARGEST_TURN_SEEN"), False)


print("\n57. the journal writes the diagnosis, not only the conclusion")
print("    2026-08-30 18:27:43. The line a person reads said")
print("    `invalid_request` - a category - while `prompt is too long:")
print("    1000815 tokens > 1000000 maximum` sat in the SAME payload under")
print("    error_details, which ERROR_KEYS does not list. §5.37 already")
print("    carries the rule 'never let an edge path record only its")
print("    conclusion'; the journal was on the wrong side of it, and an")
print("    afternoon went on proving from a JSON file what one line could")
print("    have said")
JRN, JRNC, JRNK, JRNS = _turn_run("journal-diag", [1000])
_before57 = len(store.recent_events(200, project=JRNC))
post("/event", {
    "hook_event_name": "StopFailure", "cwd": JRN, "role": "executor",
    "session_id": JRNS, "error": "invalid_request",
    "error_details": ('400 {"type":"error","error":{"type":'
                      '"invalid_request_error","message":"prompt is too '
                      'long: 1000815 tokens > 1000000 maximum"}}')})
_lines57 = [e.get("text", "") for e in store.recent_events(200,
                                                           project=JRNC)
            if "stopped with an error" in e.get("text", "")]
check("the death is in the journal", len(_lines57) >= 1, True)
_l57 = _lines57[-1]
check("and the line carries the numbers", "1000815 tokens > 1000000 maximum"
      in _l57, True)
check("the conclusion is still in front of it, so every existing reader",
      _l57.split("stopped with an error: ")[1].startswith("invalid_request"),
      True)
print("   THE CHECK CAN FAIL: the old line was the conclusion and nothing")
print("   else. If the diagnosis stops being appended, this goes red")
check("the old form - conclusion alone - is no longer what is written",
      _l57.strip().endswith("invalid_request [error]"), False)

print("   and a reason that already says everything is not made to say it")
print("   twice, so a fuller client does not get a doubled line")
_dup57 = {"hook_event_name": "StopFailure",
          "error": "prompt is too long: 1000815 tokens > 1000000 maximum"}
_r57, _w57, _k57 = daemon.stopfail_reason(_dup57, JRN, "executor")
_d57 = daemon._payload_detail(_dup57)
check("the diagnosis is there to add", "1000815" in _d57, True)
check("but it is already said, so the line does not say it twice",
      _d57.lower() in _r57.lower(), True)
print("   nor is a payload without a diagnosis given one")
_r58, _w58, _ = daemon.stopfail_reason(
    {"hook_event_name": "StopFailure", "error": "rate limit reached"},
    JRN, "executor")
check("the old shape is untouched where there is nothing to add",
      (_r58, _w58), ("rate limit reached", "error"))

print("   AND THE DIAGNOSIS DOES NOT GO IN THE STRING A DECISION READS.")
print("   The first version of this fix appended it to `reason` - and")
print("   `etype`, which chooses between the compaction doorway and the")
print("   rate handling, is built from `reason`. So a rate limit whose")
print("   detail merely mentioned a context window would have taken the")
print("   compaction doorway. The ask was the JOURNAL LINE; widening what")
print("   a decision reads was a side effect nobody ordered")
print("   FAILURE WOULD LOOK LIKE: stopfail_reason returning the two")
print("   joined, so `rate` stops being the first word etype sees")
_mix57 = {"hook_event_name": "StopFailure", "error": "rate limit reached",
          "error_details": ('500 {"error":{"message":"the context window '
                            'service is unavailable"}}')}
_rm57, _wm57, _ = daemon.stopfail_reason(_mix57, JRN, "executor")
check("the conclusion comes back alone, which is what etype is built from",
      (_rm57, _wm57), ("rate limit reached", "error"))
_et57 = (_mix57.get("error_type") or _rm57 or "").lower()
check("so the rate handling is still what this routes to",
      ("invalid" in _et57 or "context" in _et57, "rate" in _et57),
      (False, True))
check("and the diagnosis was there all along, for the line",
      "context window" in daemon._payload_detail(_mix57), True)
# the CALL, with its bracket: the function names itself in a comment there,
# saying where the diagnosis is added instead, and a comment is not a call
check("structurally: stopfail_reason does not call it",
      "_payload_detail(" in inspect.getsource(daemon.stopfail_reason), False)
check("while the journal line does",
      "_payload_detail(event)" in inspect.getsource(daemon.handle_event), True)
check("and etype really is built from what it returns",
      'or reason or ""' in inspect.getsource(daemon.handle_event), True)


print("\n58. a commit that does not happen SAYS SO")
print("    Measured 2026-08-31: 56 commits in this repository and not one")
print("    from the loop; both pre-relayout reflogs in the 2026-08-19 backup")
print("    hold a single entry, `bridge: baseline`. Two days of work sat in")
print("    a working tree and nothing had said it was unsaved. The cause was")
print("    the second gate of git_commit_iteration - `git rev-parse")
print("    --git-dir` with cwd set to the PROJECT path, where the repository")
print("    is a folder BELOW it: rc=128, returned quietly. A witness nobody")
print("    asks (rule 30), turned on the thing keeping the record")


def _git_lines(pathc):
    return [e.get("text", "") for e in store.recent_events(300, project=pathc)
            if e.get("kind") == "git" and "NOT COMMITTING" in e.get("text", "")]


def _verdict_turn(proj, tag, feedback="Checked: seen.txt\nfine"):
    """One real iteration: a blocking Stop hook, then a real verdict."""
    with open(os.path.join(proj, "seen.txt"), "w", encoding="utf-8") as fh:
        fh.write("read by the planner" + chr(10))
    out = {}

    def _turn():
        out["r"] = stop_hook(proj, "executor", tag + "-ex", tag + " did work")

    # The loop has to be ON, or the Stop hook writes no report and there is
    # no verdict to reach the commit with. Started here rather than once at
    # the top, because a `stop` verdict elsewhere in the suite would switch
    # it back off between the gates below.
    post("/loop", {"action": "start", "project": proj})
    t = threading.Thread(target=_turn)
    t.start()
    check("the pair really is waiting for a verdict",
          until(lambda: canon(proj) in daemon.PENDING), True)
    post("/verdict", {"project": proj, "verdict": "continue",
                      "feedback": feedback}, secret=True)
    t.join(40)
    return out.get("r")


print("   gate 1: a project that is NOT a repository. Real Stop hook, real")
print("   verdict, the order the daemon actually runs them in")
print("   FAILURE WOULD LOOK LIKE: no line at all - today's behaviour, and")
print("   exactly what let two days go unsaved")
G1 = os.path.join(TMP, "git-none")
os.makedirs(G1, exist_ok=True)
G1C = canon(G1)
post("/config", {"projects": {A: {}, B: {}, C: {}, G1: {}}})
register(G1, "planner", "g1-pl")
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "g1-ex", "project_dir": G1, "cwd": G1})
check("the setting is on for it - the default", store.project_config(
      daemon.CFG, G1).get("commit_each_iteration"), True)
check("and git agrees the folder is not a repository",
      daemon._git(["rev-parse", "--git-dir"], G1, timeout=8)[0] != 0, True)
_verdict_turn(G1, "g1")
_l58 = _git_lines(G1C)
check("the refusal to commit is in the journal", len(_l58), 1)
# Indexed only after the guard: on the old behaviour _l58 is empty, and a
# suite that raises there reports a traceback instead of a FAIL line (5.9).
check("and it names the reason a person can act on",
      "not a git repository" in (_l58[0] if _l58 else ""), True)
check("at warn, not buried in the log level",
      [e.get("level") for e in store.recent_events(300, project=G1C)
       if e.get("kind") == "git"][:1], ["warn"])

print("   gate 2: ONCE per project, not once per verdict. The reason does")
print("   not change between iterations, and a line each time is a log")
print("   where a report was wanted")
_verdict_turn(G1, "g1b")
check("a second iteration adds nothing", len(_git_lines(G1C)), 1)
check("and the latch is keyed by project", bool(
      (daemon.STATE.get("git_told") or {}).get(G1C)), True)

print("   gate 3: a REAL repository with nothing to commit stays silent.")
print("   A verdict on a report that changed no file is the ordinary case")
print("   FAILURE WOULD LOOK LIKE: that ordinary case reported as a failure")
print("   - the new line becoming the noise it exists to prevent")
G2 = os.path.join(TMP, "git-real")
os.makedirs(G2, exist_ok=True)
G2C = canon(G2)
daemon._git(["init"], G2, timeout=20)
# No at-sign: check_public refuses ANY address in a shipped file, and
# it is right not to keep a list of the fake-looking ones. git does
# not validate the format, so an identity without one commits fine.
daemon._git(["config", "user.email", "suite"], G2)
daemon._git(["config", "user.name", "suite"], G2)
# WITHOUT THIS THE SILENT CASE CANNOT BE REACHED: the bridge writes
# bridge-logs/ into the project at every iteration, so a watched folder is
# never clean and every verdict always has something to commit. Ignoring it
# is what makes "the executor changed nothing" a real state here.
with open(os.path.join(G2, ".gitignore"), "w", encoding="utf-8") as _fh:
    _fh.write("bridge-logs/" + chr(10))
with open(os.path.join(G2, "first.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("baseline" + chr(10))
daemon._git(["add", "-A"], G2)
daemon._git(["commit", "-m", "baseline"], G2)
_base58 = daemon._git(["rev-list", "--count", "HEAD"], G2)[1]
check("the fixture really is a repository with one commit", _base58, "1")
post("/config", {"projects": {A: {}, B: {}, C: {}, G1: {}, G2: {}}})
register(G2, "planner", "g2-pl")
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "g2-ex", "project_dir": G2, "cwd": G2})
# seen.txt is written by _verdict_turn, so the tree is NOT clean; commit it
# first, then take the silent-case reading on a genuinely clean tree.
_verdict_turn(G2, "g2a")
check("that iteration was committed", daemon._git(
      ["log", "-1", "--format=%s"], G2)[1].startswith("bridge: iteration"),
      True)
_before58 = daemon._git(["rev-list", "--count", "HEAD"], G2)[1]
_verdict_turn(G2, "g2b")
check("a verdict with nothing to commit says nothing", _git_lines(G2C), [])
check("and writes no empty commit", daemon._git(
      ["rev-list", "--count", "HEAD"], G2)[1], _before58)

print("   gate 4: and when there IS something, it really commits. Without")
print("   this the whole case could pass on a function that only ever")
print("   complains")
with open(os.path.join(G2, "work.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("the executor changed a file" + chr(10))
_verdict_turn(G2, "g2c")
check("a new commit exists", int(daemon._git(
      ["rev-list", "--count", "HEAD"], G2)[1]) > int(_before58), True)
check("with the loop's own message", "bridge: iteration" in daemon._git(
      ["log", "-1", "--format=%s"], G2)[1], True)
check("and the file it was about is in it", "work.txt" in daemon._git(
      ["show", "--name-only", "--format=", "HEAD"], G2)[1], True)
check("still nothing in the journal about this project", _git_lines(G2C), [])

print("   in the inventory, on the day it was written (cases 49 and 50)")
check("the inventory names the latch", daemon.STATE_PATHS.get("git_told"),
      "path")
print("   and a live pair is refused by name first - the answer is 200 with")
print("   ok:false in it, so a case that checks only the status code cannot")
print("   fail. That was this check, until it was")
_fc58, _fr58 = post_rc("/forget-project", {"path": G1})
check("a live pair is refused, and says why",
      (_fc58, _fr58.get("ok"), bool(_fr58.get("live"))), (200, False, True))
check("and nothing was taken while it was refused",
      (daemon.STATE.get("git_told") or {}).get(G1C), "not-a-repo")
for _r58 in ("executor", "planner"):
    post_rc("/event", {"hook_event_name": "SessionEnd", "role": _r58,
                       "session_id": "g1-%s" % _r58[:2],
                       "project_dir": G1, "cwd": G1})
_fc58, _fr58 = post_rc("/forget-project", {"path": G1})
check("with the windows gone it is accepted", (_fc58, _fr58.get("ok")),
      (200, True))
check("and the latch went with it",
      (daemon.STATE.get("git_told") or {}).get(G1C), None)
check("named in what the removal reports it took",
      "git_told" in json.dumps(_fr58), True)


print("\n59. where the repository is, is DECLARED - and only inside the project")
print("    Case 58 showed the bridge saying it cannot commit. This is the")
print("    other half: the project names the folder, in projects[path]")
print("    ['repo'], relative to itself. Searching for a .git up or down the")
print("    tree was refused - that is the bridge choosing a repository")
print("    nobody named, and `git add -A` in somebody else's folder is not a")
print("    mistake anyone notices the same day. Same shape as checks, modes")
print("    and moved_from: stated, never inferred")

R = os.path.join(TMP, "declared")
RSUB = os.path.join(R, "sub")
os.makedirs(RSUB, exist_ok=True)
RC_ = canon(R)
daemon._git(["init"], RSUB, timeout=20)
daemon._git(["config", "user.email", "suite"], RSUB)
daemon._git(["config", "user.name", "suite"], RSUB)
with open(os.path.join(RSUB, ".gitignore"), "w", encoding="utf-8") as _fh:
    _fh.write("bridge-logs/" + chr(10))
with open(os.path.join(RSUB, "a.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("baseline" + chr(10))
daemon._git(["add", "-A"], RSUB)
daemon._git(["commit", "-m", "baseline"], RSUB)


def _repo_set(val):
    """Declare it through the real endpoint the panel writes settings with."""
    post("/config", {"projects": {A: {}, B: {}, C: {},
                                 R: ({"repo": val} if val is not None else {})}})


def _commits():
    return daemon._git(["rev-list", "--count", "HEAD"], RSUB)[1]


def _git_lines59():
    return [e.get("text", "") for e in store.recent_events(300, project=RC_)
            if e.get("kind") == "git" and "NOT COMMITTING" in e.get("text", "")]


_repo_set(None)
register(R, "planner", "r-pl")
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "r-ex", "project_dir": R, "cwd": R})
_n59 = _commits()
check("the repository really is the subfolder, with one commit", _n59, "1")

print("   with NO field: today's behaviour exactly - it does not commit and")
print("   it says why. Nothing is guessed, the subfolder is not found")
print("   FAILURE WOULD LOOK LIKE: a commit appearing in sub/ anyway, which")
print("   is the bridge having gone looking")
_verdict_turn(R, "r-none")
check("no commit was made", _commits(), _n59)
check("and the silence is broken, once", len(_git_lines59()), 1)
check("naming the setting rather than leaving a person guessing",
      any('"repo"' in l for l in _git_lines59()), True)

print("   DECLARED: the same project, the same folder, one setting")
print("   FAILURE WOULD LOOK LIKE: no new commit - the declaration read but")
print("   not used - or a commit in the project root instead of sub/")
_repo_set("sub")
check("the declaration is what repo_dir returns",
      daemon.repo_dir(R), (os.path.normpath(RSUB), None))
with open(os.path.join(R, "in-root.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("this file is NOT in the repository" + chr(10))
with open(os.path.join(RSUB, "work.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("this file is" + chr(10))
_verdict_turn(R, "r-good")
check("now it commits", int(_commits()) > int(_n59), True)
check("with the loop's own message", "bridge: iteration" in daemon._git(
      ["log", "-1", "--format=%s"], RSUB)[1], True)
check("the file inside the repository is in it", "work.txt" in daemon._git(
      ["show", "--name-only", "--format=", "HEAD"], RSUB)[1], True)
check("and the one outside it is not - the commit happened in sub/, not R",
      "in-root.txt" in daemon._git(
          ["show", "--name-only", "--format=", "HEAD"], RSUB)[1], False)
check("a working commit clears the latch", bool(
      (daemon.STATE.get("git_told") or {}).get(RC_)), False)

print("   A DECLARATION THAT POINTS NOWHERE IS A REFUSAL WITH A SENTENCE -")
print("   not silence, and not an exception either")
print("   FAILURE WOULD LOOK LIKE: nothing in the journal, or a traceback")
print("   swallowed by the except that used to be a bare pass")
_repo_set("no-such-folder")
_n59b = _commits()
_verdict_turn(R, "r-missing")
check("it refuses", _commits(), _n59b)
_l59 = _git_lines59()
check("and says so", len(_l59), 2)
# Searched, not indexed: recent_events hands back the newest FIRST, so
# [-1] is the OLDEST line and this check was reading the wrong one.
check("naming what was declared and where it looked",
      any("no-such-folder" in l and "does not exist" in l for l in _l59),
      True)

print("   AND THE DECLARATION IS BOUNDED. `..` would put `git add -A` in the")
print("   folder ABOVE the project - which for this bridge is the owner's own")
print("   folder of unrelated work, the one CLAUDE.md says nothing may ever")
print("   be written to")
print("   FAILURE WOULD LOOK LIKE: repo_dir returning the parent - a field")
print("   that hands out the filesystem instead of naming a subfolder")
_repo_set("..")
check("refused, and the reason is that it leaves the project",
      (daemon.repo_dir(R)[0], "outside the project" in daemon.repo_dir(R)[1]),
      (None, True))
_verdict_turn(R, "r-escape")
check("no commit", _commits(), _n59b)
check("and a SECOND sentence, because a different mistake is a different "
      "thing to tell somebody", len(_git_lines59()), 3)
_repo_set(os.path.join(TMP, "somewhere-else"))
check("naming an absolute path is refused as well",
      (daemon.repo_dir(R)[0], "absolute" in daemon.repo_dir(R)[1]),
      (None, True))
print("   and nothing here ever searched: with no field it did not find sub/")
print("   even though sub/ was there the whole time")


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
print("\n60. a verdict's WORDS reach the executor - for every verdict")
print("    that carries a judgement, not only for `continue`")
print("    The body used to be built inside the branch that catches")
print("    'anything not stop/done/wait', and the only verdict left in it")
print("    is `continue`. So a `done` verdict's text was built nowhere and")
print("    sent nowhere. Measured on this project's own executor")
print("    transcript, reports 46-70: all 8 `continue` verdicts arrived,")
print("    all 17 `done` and `stop` ones did not - a perfect correlation")
print("    with the verdict type and none at all with time.")
print("    WHAT THE WITNESS IS. Not the journal: it records what the bridge")
print("    did with the REPORT and never mentions the body, which is why")
print("    nothing complained for weeks. The channel below is a real HTTP")
print("    server and records what it was HANDED - one step past 'the")
print("    daemon called deliver'. A suite cannot see inside a real")
print("    session, and that limit is named rather than papered over: the")
print("    live half of this is a verdict whose text shows up in an")
print("    executor's transcript.")

_vp = A
_vkey = canon(_vp)


def _bodies60():
    return json.dumps(DELIVERED.get((_vkey, "executor"), []),
                      ensure_ascii=False)


def _verdict_reached60(marker, tries=40):
    """Did THIS text reach the executor's channel? Waits, then answers."""
    for _ in range(tries):
        if marker in _bodies60():
            return True
        time.sleep(0.25)
    return False


with open(os.path.join(_vp, "seen.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("read by the planner" + chr(10))
post("/loop", {"action": "start", "project": _vp, "reset": True})

for _v60, _marker60, _want60, _why60 in (
        ("continue", "WORDS-CONTINUE-60", True,
         "the one that always worked"),
        ("done", "WORDS-DONE-60 " + ("and here is the next piece. " * 40), True,
         "THE DEFECT: since rule 34 this is where the next piece is written."
         " The feedback is long on purpose - a `done` that asks for nothing"
         " is held instead of delivered now, and that is case 61"),
        ("stop", "WORDS-STOP-60", False,
         "the run is over - nothing to act on, and nothing to move"),
        ("wait", "WORDS-WAIT-60", False,
         "nothing is being judged; the pair is parked, not working")):
    del DELIVERED[(_vkey, "executor")][:]
    _t60 = threading.Thread(
        target=lambda: stop_hook(_vp, "executor", "ex-alpha",
                                 "a piece of work, report body"),
        daemon=True)
    _t60.start()
    check("a report is waiting for the %s verdict" % _v60,
          until(lambda: daemon.PENDING.get(_vkey)), True)
    post("/verdict", {"project": _vp, "verdict": _v60,
                      "feedback": "Checked: seen.txt\n%s" % _marker60},
         secret=True)
    _t60.join(30)
    _got60 = _verdict_reached60(_marker60, 40 if _want60 else 12)
    check("%s: the words %s reach the executor - %s"
          % (_v60, "DO" if _want60 else "do NOT", _why60),
          _got60, _want60)
    if _v60 in ("done", "stop"):
        post("/loop", {"action": "start", "project": _vp, "reset": False})

print("   and a task held from mid-turn rides in the SAME message, so a")
print("   `done` costs one wake and not two racing 1.5 s timers")
del DELIVERED[(_vkey, "executor")][:]
daemon.note_task_sent(_vp, "HELD-WORK-60", mid_turn=True)
_t60 = threading.Thread(
    target=lambda: stop_hook(_vp, "executor", "ex-alpha", "another piece"),
    daemon=True)
_t60.start()
check("the report is waiting", until(lambda: daemon.PENDING.get(_vkey)), True)
post("/verdict", {"project": _vp, "verdict": "done",
                  "feedback": "Checked: seen.txt\nWORDS-BOTH-60"},
     secret=True)
_t60.join(30)
check("the held work came back", _verdict_reached60("HELD-WORK-60"), True)
check("and the verdict's words came with it",
      "WORDS-BOTH-60" in _bodies60(), True)
_msgs60 = [d for d in DELIVERED.get((_vkey, "executor"), [])
           if "WORDS-BOTH-60" in json.dumps(d, ensure_ascii=False)
           or "HELD-WORK-60" in json.dumps(d, ensure_ascii=False)]
check("in ONE message, not two", len(_msgs60), 1)
post("/loop", {"action": "start", "project": _vp, "reset": False})

print("   and the loop-start notice is narrowed the same way: the planner")
print("   learns nothing from being told what it just pressed itself")
_pk60 = (_vkey, "planner")
post("/loop", {"action": "stop", "project": _vp})
del DELIVERED[_pk60][:]
post("/loop", {"action": "start", "project": _vp, "by": "planner"})
time.sleep(0.5)
check("its own start says nothing to it",
      [d for d in DELIVERED[_pk60] if "The loop is on" in json.dumps(d)], [])
print("   but a start it did NOT make is the only way it learns reports")
print("   are coming, so no mark means TELL - the refusal opens outward")
post("/loop", {"action": "stop", "project": _vp})
del DELIVERED[_pk60][:]
post("/loop", {"action": "start", "project": _vp})
check("a start with no mark is told",
      until(lambda: [d for d in DELIVERED[_pk60]
                     if "The loop is on" in json.dumps(d)]) and True, True)
post("/loop", {"action": "stop", "project": _vp})
del DELIVERED[_pk60][:]
post("/loop", {"action": "start", "project": _vp, "by": "panel"})
check("and so is one from the panel",
      until(lambda: [d for d in DELIVERED[_pk60]
                     if "The loop is on" in json.dumps(d)]) and True, True)

print("\n61. a `done` that asks for nothing is HELD, not spent on a wake")
print("    - and held is not dropped, which is the whole difficulty")
print("    Rule 34 on this project's numbers: an acknowledgement wakes the")
print("    executor, the executor ends a turn, every turn end fires the")
print("    Stop hook, every Stop hook is a report, and the report wakes the")
print("    planner. A ROUND TRIP for the word 'accepted'. Of 433 `done`")
print("    verdicts read from the planners' own transcripts, 239 (55%) have")
print("    under 1000 characters of prose once the gate's own Checked block")
print("    is taken out by prose_of, and 382 of 433 are too long for the")
print("    idle damper to call the exchange trivial - nothing else caught")
print("    them.")
print("    WHY THE CASE IS SHAPED THIS WAY. The defect this pair spent four")
print("    turns finding was content that went nowhere while every record")
print("    said it had. So it is not enough to check that nothing was sent:")
print("    every branch below also checks WHERE THE WORDS WENT INSTEAD, and")
print("    the two release paths - riding with a wake, and going alone at")
print("    the limit - are exercised separately, because a hold with only")
print("    the first is a drop that waits.")

_hp = A
_hk = canon(_hp)


def _held61():
    return daemon.held_record(_hk)


def _seen61():
    return json.dumps(DELIVERED.get((_hk, "executor"), []), ensure_ascii=False)


def _wait61(marker, want, tries=32):
    """Positive: answer the moment it arrives. Negative: WAIT THE WHOLE TIME.

    Written first as `if (marker in seen) == want`, which for a negative
    returns on the very first look - before the 1.5 s delivery timer has
    even fired - so it answered "it did not arrive" without waiting for
    anything and could not fail. Caught by sabotage: with the hold ripped
    out, "NOTHING reached the executor" stayed green while nine other
    checks went red. A check that cannot show the difference is not a gate
    (rule 19), and a negative one has to outlive what it is denying.
    """
    for _ in range(tries):
        if want and marker in _seen61():
            return True
        time.sleep(0.25)
    return marker in _seen61()


def _done61(feedback):
    """One full turn answered with `done`, the real way round."""
    t = threading.Thread(
        target=lambda: stop_hook(_hp, "executor", "ex-alpha", "a piece"),
        daemon=True)
    t.start()
    if not until(lambda: daemon.PENDING.get(_hk)):
        return False
    post("/verdict", {"project": _hp, "verdict": "done",
                      "feedback": feedback}, secret=True)
    t.join(30)
    post("/loop", {"action": "start", "project": _hp, "reset": False})
    return True


daemon.STATE.pop("held_verdict", None)
post("/loop", {"action": "start", "project": _hp, "reset": True})

print("   (a) it is held, and nothing is sent")
del DELIVERED[(_hk, "executor")][:]
check("a report was answered", _done61("Checked: seen.txt\nACK-61"), True)
check("NOTHING reached the executor - no wake was spent",
      _wait61("ACK-61", False, 24), False)
check("and the words are being held, not dropped",
      "ACK-61" in json.dumps(_held61(), ensure_ascii=False), True)
check("the held text says it is late, in its own first line",
      daemon.HELD_VERDICT_HEAD in (_held61().get("body") or ""), True)
check("held, counted", int(_held61().get("n") or 0), 1)
# Visible, not merely recorded. "Held" is only different from "dropped" if
# somebody can see it without reading the journal, so the number the panel
# draws from is checked here rather than assumed.
check("and the panel's own row carries it",
      int((daemon.pairs_view().get(_hk) or {}).get("held_chars") or 0) > 0,
      True)

print("   (b) it rides with the next thing the executor is woken for,")
print("       in ONE message - the ride is free, a second message is not")
del DELIVERED[(_hk, "executor")][:]
daemon.deliver(_hp, "executor", "NEW-WORK-61", {"kind": "task"})
check("the task arrived", _wait61("NEW-WORK-61", True), True)
check("and the held words came with it", "ACK-61" in _seen61(), True)
_msgs61 = [d for d in DELIVERED.get((_hk, "executor"), [])
           if "ACK-61" in json.dumps(d, ensure_ascii=False)
           or "NEW-WORK-61" in json.dumps(d, ensure_ascii=False)]
check("in ONE message, not two", len(_msgs61), 1)
check("the hold is empty now", (_held61().get("body") or ""), "")
check("and the ride is counted", int(_held61().get("ridden") or 0), 1)

print("   (c) a `continue` is NEVER held. The executor is blocked on it and")
print("       waiting for those words by name; holding one would stop the")
print("       work to save a wake, which is the trade rule 34 refuses")
del DELIVERED[(_hk, "executor")][:]
_t61 = threading.Thread(
    target=lambda: stop_hook(_hp, "executor", "ex-alpha", "a piece"),
    daemon=True)
_t61.start()
check("a report is waiting", until(lambda: daemon.PENDING.get(_hk)), True)
post("/verdict", {"project": _hp, "verdict": "continue",
                  "feedback": "Checked: seen.txt\nGO-ON-61"}, secret=True)
_t61.join(30)
check("a short `continue` is delivered at once", _wait61("GO-ON-61", True), True)
check("and nothing of it is held", (_held61().get("body") or ""), "")

print("   (d) a `done` that DOES carry work is delivered at once. The test")
print("       is the prose, not the whole body: the Checked block is the")
print("       planner's evidence, eats a median 192 characters, and moves")
print("       57% of `done` over the line where 45% belong - which is why")
print("       carries_work measures prose_of() and not len()")
del DELIVERED[(_hk, "executor")][:]
check("a report was answered",
      _done61("Checked: seen.txt\nWORK-61 " + ("do the next thing. " * 70)),
      True)
check("a `done` carrying work goes straight through",
      _wait61("WORK-61", True), True)
check("and nothing of it is held", (_held61().get("body") or ""), "")

print("   (e) THE LIMIT. Nothing was sent to the executor, so the words go")
print("       on their own. Held with no limit is dropped with extra steps.")
print("       3600 s: over 332 work-free done/stop verdicts, timed against")
print("       the EXECUTOR'S OWN Stop hook, 298 (89.8%) were woken inside")
print("       it and 34 were not - p50 392 s, p75 1160 s, p90 3629 s. And")
print("       3600 is already this file's INFLIGHT_MAX_SEC: one idea of")
print("       'too long to still be real', not two, and closer to the p90")
print("       than any four-digit number deserves to be trusted to.")
del DELIVERED[(_hk, "executor")][:]
check("a report was answered", _done61("Checked: seen.txt\nLATE-61"), True)
check("held again", "LATE-61" in json.dumps(_held61(), ensure_ascii=False),
      True)
check("nothing has been sent", _wait61("LATE-61", False, 24), False)
# Guarded, and the guard is the point. In a flat script an unguarded
# `STATE["held_verdict"][_hk]` three lines after a check that just said
# "nothing is held" raises, and the raise takes every block below it with
# it - silently, with no FAIL summary. Measured on a copy with the hold
# removed: the case said FAIL, then died on the KeyError and (f) never
# ran. One FAIL is recorded for the whole block instead.
_rec61 = (daemon.STATE.get("held_verdict") or {}).get(_hk)
if _rec61:
    _rec61["since"] = time.time() - daemon.HELD_VERDICT_MAX_SEC - 1
    daemon.release_stale_verdicts()
else:
    check("there is something held for the limit to release", False, True)
check("past the limit it goes on its own", _wait61("LATE-61", True), True)
check("the hold is empty", (_held61().get("body") or ""), "")
check("and it is counted as having travelled alone",
      int(_held61().get("alone") or 0), 1)

print("   (f) a delivery that FAILS puts the words back. A ride that fell")
print("       through must not be the way content disappears.")
del DELIVERED[(_hk, "executor")][:]
check("a report was answered", _done61("Checked: seen.txt\nKEEP-61"), True)
check("held", "KEEP-61" in json.dumps(_held61(), ensure_ascii=False), True)
_open61 = daemon.urllib.request.urlopen


def _fail61(*a, **k):
    raise IOError("the channel took the connection and dropped it")


daemon.urllib.request.urlopen = _fail61
try:
    daemon.deliver(_hp, "executor", "LOST-61", {"kind": "task"})
finally:
    daemon.urllib.request.urlopen = _open61
check("the delivery failed", "LOST-61" in _seen61(), False)
check("and the held words are still held, not gone with it",
      "KEEP-61" in json.dumps(_held61(), ensure_ascii=False), True)
del DELIVERED[(_hk, "executor")][:]
daemon.deliver(_hp, "executor", "AFTER-61", {"kind": "task"})
check("they ride the next one instead", _wait61("KEEP-61", True), True)

print("   (g) and the same predicate decides the NUDGE. A verdict that")
print("       carried the work needs no task asked for - the work is in it.")
print("       Measured on 371 firings of the 60 s form: 130 (35%) go. The")
print("       case the branch lives for keeps its own - of 195 acceptances")
print("       with no task after them, 82 carried the work themselves and")
print("       113 carried nothing, and all 113 still fire.")
print("       The old witness, last_task, was measured on a world where a")
print("       `done` delivered no words at all, so work in a verdict could")
print("       not physically arrive. Rule 33: that world ended the same")
print("       afternoon, and the measurement under it expired with it.")

_pk61 = (_hk, "planner")
_NUDGE_WORDS = "waiting for its next piece of work"


def _nudged61(want, tries=24):
    """Positive: answer as soon as it lands. Negative: OUTLIVE THE TIMER.

    24 x 0.25 s is twelve times the delay the branch is set to below, and
    that delay is not assumed - the positive check runs first and proves
    the nudge does fire at it. A negative that answers before the timer it
    denies is the defect sabotage found in this very case.
    """
    for _ in range(tries):
        if want and _NUDGE_WORDS in json.dumps(DELIVERED.get(_pk61, []),
                                               ensure_ascii=False):
            return True
        time.sleep(0.25)
    return _NUDGE_WORDS in json.dumps(DELIVERED.get(_pk61, []),
                                      ensure_ascii=False)


_nudge_was = daemon.NUDGE_AFTER_VERDICT_SEC
daemon.NUDGE_AFTER_VERDICT_SEC = 0.5
try:
    del DELIVERED[_pk61][:]
    check("a report was answered", _done61("Checked: seen.txt\nQUIET-61"),
          True)
    check("a `done` with nothing in it DOES ask for the next piece - this "
          "is what the branch is for", _nudged61(True), True)
    del DELIVERED[_pk61][:]
    check("a report was answered",
          _done61("Checked: seen.txt\nWORKY-61 " + ("the next piece. " * 80)),
          True)
    check("a `done` that carried the work is not asked for it",
          _nudged61(False), False)
finally:
    daemon.NUDGE_AFTER_VERDICT_SEC = _nudge_was

daemon.STATE.pop("held_verdict", None)
post("/loop", {"action": "start", "project": _hp, "reset": False})

print("\n62. a long turn is not a deadlock - and a deadlock still is one")
print("    THE WITNESS THAT LIED. last_movement() counts a finished Stop")
print("    and a task going out, and deliberately nothing else - right")
print("    about status lines and heartbeats, wrong about a turn. The")
print("    executor is writing and will not fire Stop until it finishes,")
print("    so last_movement reports the START of the turn and every turn")
print("    longer than clinch_grace reads as a deadlock. Measured on this")
print("    project 2026-08-31: three announcements for one pair, and at")
print("    each one the executor's transcript held a living entry 0s, 5s")
print("    and 51s earlier - all inside stall_grace.")
print("    BOTH SIDES ARE CHECKED, because a tier that stops lying by")
print("    also stopping catching is worse than the one it replaced. And")
print("    the witness fails CLOSED - no transcript is not evidence of")
print("    work - which is why it is not executor_is_working(), whose")
print("    fail-open is right where IT is used and wrong here.")
print("    IN THE REAL ORDER (5.33): project A has been driven through the")
print("    real endpoints for sixty blocks; one more real turn and a real")
print("    verdict go through them here, then the real deciding tick. No")
print("    hand-built snapshot of STATE - that shape was accepted twice")
print("    and came back live both times.")

_cp = A
_ck = canon(_cp)
_grace_was = dict(daemon.CFG.get("thresholds") or {})

# Both halves announce themselves the way hook.py does, because the ladder
# asks how long the executor has been silent and that is read from the
# session record's seen_at - no record, no answer, and assess() stands down
# above the branch this case is about.
# A status line as well as a SessionStart: best_session takes only records
# that carry telemetry, so a session the status line has never described is
# invisible to the ladder however many hooks it has fired.
for _r62, _s62 in (("executor", "ex-alpha"), ("planner", "pl-alpha")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r62,
                       "session_id": _s62, "project_dir": _cp, "cwd": _cp})
    post_rc("/status", {"role": _r62, "payload": {
        "session_id": _s62,
        "workspace": {"current_dir": _cp, "project_dir": _cp},
        "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
        "context_window": {"context_window_size": 1000000,
                           "used_percentage": 12.0,
                           "current_usage": {
                               "input_tokens": 10,
                               "cache_creation_input_tokens": 90,
                               "cache_read_input_tokens": 120_000,
                               "output_tokens": 400}}}})

# One more real turn, through the endpoints: the Stop hook writes
# stop_seen, and a `done` carrying work leaves no verdict in flight.
post("/loop", {"action": "start", "project": _cp, "reset": False})
_t62 = threading.Thread(
    target=lambda: stop_hook(_cp, "executor", "ex-alpha", "a piece of work"),
    daemon=True)
_t62.start()
check("a report is waiting", until(lambda: daemon.PENDING.get(_ck)), True)
post("/verdict", {"project": _cp, "verdict": "done",
                  "feedback": "Checked: seen.txt\nCLINCH-62 "
                              + ("carry on with the next thing. " * 50)},
     secret=True)
_t62.join(30)
post("/loop", {"action": "start", "project": _cp, "reset": False})

# The only things moved are two clocks, and both go through /config - the
# events themselves all really happened, which is the part 5.33 is about.
# `silence_minutes` matters as much as the grace: assess() stands down on
# "the executor answered recently" long before it reaches clinch(), and a
# case that never reaches the branch it is about proves nothing.
_projs62 = json.loads(json.dumps(daemon.CFG.get("projects") or {}))
_pk62 = _cp if _cp in _projs62 else _ck
_projs62.setdefault(_pk62, {})["silence_minutes"] = 0.03      # 1.8 s
post("/config", {"projects": _projs62,
                 "thresholds": dict(_grace_was, clinch_grace=1)})
time.sleep(2.4)


def _saw62(res):
    return "waiting on each other" in json.dumps(res or {}, ensure_ascii=False)


# Proof the case can REACH the branch it is about: assess() stands down
# above clinch() on both of these, and a case that never gets there is
# green for the wrong reason.
_sit62 = daemon.situation(_cp)
note("silence the ladder wants (s)",
     float(store.project_config(daemon.CFG, _ck).get("silence_minutes", 8)) * 60)
note("silence the executor has (s)",
     (_sit62["roles"]["executor"] or {}).get("silent_for"))
note("in flight / reviewing / verdict",
     (_sit62["inflight"], _sit62["reviewing"], _sit62["verdict_in_flight"]))

print("   side one: nothing is writing, so the pair really is stuck")
_res62a = daemon.assess(_cp)
note("what assess saw with no transcript", _res62a)
check("tier 1 still names a genuine clinch", _saw62(_res62a), True)

print("   side two: the same pair, the same instant, except that the")
print("   executor's transcript is being written")
_tdir62 = os.path.join(TMP, "clinch-transcripts")
os.makedirs(_tdir62, exist_ok=True)
_tp62 = os.path.join(_tdir62, "ex-alpha.jsonl")
_real62 = sessions.transcript_of
sessions.transcript_of = (lambda sid, path=None:
                          _tp62 if sid == "ex-alpha" else _real62(sid, path))
try:
    with open(_tp62, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(time.time() - 5))
                         + ".000Z",
            "message": {"content": "still working"}}) + chr(10))
    daemon.STATE.pop("assessed", None)
    _res62b = daemon.assess(_cp)
    note("what assess saw with a live transcript", _res62b)
    check("a working executor is NOT called a deadlock", _saw62(_res62b),
          False)
    print("   and the witness is positive evidence, not the absence of it:")
    print("   an entry older than stall_grace proves nothing, so the clinch")
    print("   comes back - which is what stops this being a quieter tier")
    with open(_tp62, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(time.time() - 4000))
                         + ".000Z",
            "message": {"content": "long ago"}}) + chr(10))
    daemon.STATE.pop("assessed", None)
    check("an old entry is not evidence of work", _saw62(daemon.assess(_cp)),
          True)
    print("   and the death's own record is not evidence either - the api")
    print("   error is exactly what a dying turn writes (5.38)")
    with open(_tp62, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({
            "type": "assistant", "isApiErrorMessage": True,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(time.time() - 5))
                         + ".000Z",
            "message": {"content": "API Error"}}) + chr(10))
    daemon.STATE.pop("assessed", None)
    check("a dying turn's own record does not stand the watchdog down",
          _saw62(daemon.assess(_cp)), True)
finally:
    sessions.transcript_of = _real62
    _back62 = json.loads(json.dumps(daemon.CFG.get("projects") or {}))
    (_back62.get(_pk62) or {}).pop("silence_minutes", None)
    post("/config", {"projects": _back62, "thresholds": _grace_was})

print("\n63. a dead session's channel may not vouch for its replacement")
print("    THE SECOND CONSUMER OF A CLOSED CLASS. On 2026-08-28")
print("    mark_registered stopped taking a channel registration as proof")
print("    that a replacement had come up: a handover STOPS the old window,")
print("    its channel process answers for up to 45 s, so the corpse of the")
print("    window that failed to be replaced was clearing the streak that")
print("    counts the failure. Only that consumer was hardened. The other")
print("    one - already_up() -> ensure_record(), which writes the record")
print("    the panel and the planning read - was left on the old witness")
print("    and told the same lie three days later.")
print("    LIVE INSTANCE, 2026-08-31, this project: handover decided")
print("    19:43:09, window opened 19:43:12, and at 19:43:32 the journal")
print("    says 'Noticed a live executor window (its channel is answering)")
print("    - adding it to the panel'. That window did not reach SessionStart")
print("    until 20:22:51 - 39 minutes - and its transcript file did not")
print("    exist before then, which is what proves the channel was not its.")
print("    BOTH SIDES ARE CHECKED, because a guard that stops lying by also")
print("    refusing every honest adoption is worse than the bug.")

_p63 = B
_k63 = canon(_p63)

# The pair starts with no executor record at all, and that is ASSERTED
# rather than assumed: ensure_record() returns early when a live record
# already exists, so without this the case would pass with the guard
# deleted - green for the wrong reason (rule 19).
with daemon._lock:
    for _key in [k for k, v in (daemon.STATE.get("sessions") or {}).items()
                 if canon(v.get("path") or "") == _k63
                 and v.get("role") == "executor"]:
        daemon.STATE["sessions"].pop(_key, None)
daemon.STATE["handover"] = {}


def _rec63():
    """Every live executor record this pair has, by key."""
    return sorted(k for k, v in (daemon.STATE.get("sessions") or {}).items()
                  if canon(v.get("path") or "") == _k63
                  and v.get("role") == "executor"
                  and v.get("state") not in ("ended", "died"))


check("the pair starts with no executor record to hide behind", _rec63(), [])

# The corpse: a real channel server, registered through the real endpoint,
# exactly as the OLD session's channel.py did. Nothing removes it at a
# handover and there is no reaper (-> DECISIONS.md 5.3), so it is still
# answering when the replacement is launched. It names its window as
# channel.py does, and that window is GONE - which is what makes it a
# corpse's channel rather than a live half the handover must not touch
# (8.36, replaced_window).
register(_p63, "executor", "ex-beta-old", ppid=dead_pid())
check("the old session's channel answers",
      daemon.channel_alive(_p63, "executor") is not None, True)

_before63 = len(launches())
_r63 = post("/handover", {"project": _p63, "role": "executor",
                          "reason": "the case that closes the second consumer"})
check("the handover was accepted for the executor", _r63.get("roles"),
      ["executor"])
check("a replacement window was opened",
      until(lambda: len(launches()) > _before63, 30), True)

# CHANGED DELIBERATELY 2026-09-04 (X1b), and it is a change of FIXTURE, not
# of claim. The handover used to stop the old window right here and retire
# its records with it, which is what emptied this list; since the order was
# reversed the session being replaced keeps working - and keeps its record
# - until its replacement reports for duty, so the list no longer empties
# itself. It is emptied here instead, for the reason written above the
# first check of this side: ensure_record returns early when a live record
# exists, so with one present this side would pass with the guard deleted.
# Side three has always done exactly this, and now both sides say why.
with daemon._lock:
    for _key in [k for k, v in (daemon.STATE.get("sessions") or {}).items()
                 if canon(v.get("path") or "") == _k63
                 and v.get("role") == "executor"]:
        daemon.STATE["sessions"].pop(_key, None)
    daemon.save_state()
check("and no executor record is left to hide behind", _rec63(), [])

# THE CONTROL. The false witness has to be genuinely present, or "no record
# was written" proves nothing: it would also be the answer if the channel
# had quietly stopped answering and there were no lie left to tell.
check("and the false witness IS present - already_up still says so",
      daemon.already_up(_p63, "executor"), daemon.WITNESS_CHANNEL)
check("while the handover is still waiting for exactly this role",
      daemon.handover_awaits(_p63, "executor"), True)
check("and not for the other half, which is untouched",
      daemon.handover_awaits(_p63, "planner"), False)

# The replacement never comes up: no SessionStart, no registration - which
# is the whole incident. reconcile() is the real caller, run in the real
# order after the real endpoint, not a hand-built snapshot (5.33).
daemon.reconcile()
check("NO record is written from the dead session's channel", _rec63(), [])
check("so the panel does not show an executor that is not there",
      [r for r in (get("/state")["state"].get("pairs") or [])
       if canon(r.get("path") or "") == _k63
       and r.get("role") == "executor" and r.get("live")], [])

print("   side two: the same pair, the same channel, once the handover is")
print("   no longer waiting for it - the guard must RELEASE, and it is")
print("   expire_handover that does it, not the case reaching into STATE")
# BOTH waits, because since 2026-09-02 there are two and they are
# independent: the handover record, and the launch record reg_pid writes on
# every launch - which is the only one the ROTATION path leaves, and the
# whole reason the guard missed 01:20:54. Ageing one and not the other
# would prove the release of one path and quietly leave the other shut.
with daemon._lock:
    # GUARDED: with no handover record the checks above have already said
    # so, and a KeyError here took every block below it (2026-09-26).
    ((daemon.STATE.get("handover") or {}).get(_k63) or {})["at"] = (
        time.time()
        - float(daemon.CFG["thresholds"].get("handover_grace", 600)) - 5)
    _pid63 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k63)
    if _pid63:
        _pid63["at"] = (
            time.time()
            - float(daemon.CFG["thresholds"].get("startup_grace", 600)) - 5)
daemon.reconcile()
check("the stale handover expired", (daemon.STATE.get("handover") or
                                     {}).get(_k63), None)
check("and now the very same witness DOES adopt the window",
      len(_rec63()), 1)
check("recorded as noticed by the channel, which is what it was",
      [(daemon.STATE["sessions"][k].get("seen_by")) for k in _rec63()],
      [daemon.WITNESS_CHANNEL])

print("   side three: THE ROTATION PATH, which is how 2026-09-02 happened.")
print("   handover() writes STATE['handover']; rotate_executor - what")
print("   handle_wall_hit calls, and what replaced that executor at")
print("   01:20:54 - writes no such record, so handover_awaits answered")
print("   False and this guard never ran. Ten seconds after the window")
print("   opened the journal said 'Noticed a live executor window (its")
print("   channel is answering)'; it reached SessionStart at 04:46:00.")
print("   Driven the real way: a real StopFailure, then the real tick.")
_dR, _cR, _kR = _ovf_pair("rot-witness", 1000561)


def _recR():
    # _cR, the canonical PATH. _kR is the pair key "<path>|executor", and
    # comparing a path against that matched nothing at all - which made
    # every negative check below green by construction, the exact shape
    # sabotage found in case 61.
    return sorted(k for k, v in (daemon.STATE.get("sessions") or {}).items()
                  if canon(v.get("path") or "") == _cR
                  and v.get("role") == "executor"
                  and v.get("state") not in ("ended", "died"))


# The corpse, registered through the real endpoint exactly as the old
# session's channel.py does. Nothing removes it at a rotation either.
register(_dR, "executor", "rot-witness-ex-old")
_too_long(_dR, "rot-witness-ex", 1000561)
post_rc("/event", {"hook_event_name": "PreCompact", "role": "executor",
                   "session_id": "rot-witness-ex", "project_dir": _dR,
                   "cwd": _dR})
_bR = len(launches())
_recw63 = daemon.COMPACT_RECOVERY_SEC
try:
    daemon.COMPACT_RECOVERY_SEC = 0
    daemon.assess(_dR)
finally:
    daemon.COMPACT_RECOVERY_SEC = _recw63
check("the wall handling replaced the executor",
      until(lambda: len(launches()) > _bR, 30), True)
check("and this is the ROTATION path - there is no handover record at all",
      (daemon.STATE.get("handover") or {}).get(_cR), None)
with daemon._lock:
    for _k in [k for k, v in (daemon.STATE.get("sessions") or {}).items()
               if canon(v.get("path") or "") == _cR
               and v.get("role") == "executor"]:
        daemon.STATE["sessions"].pop(_k, None)
check("the pair has no executor record to hide behind", _recR(), [])
# THE CONTROL, as on side one: the lie has to be available, or "no record
# was written" is also what a silent channel would produce.
check("the false witness IS present - already_up still says so",
      daemon.already_up(_dR, "executor"), daemon.WITNESS_CHANNEL)
check("and ONE predicate sees the rotation's wait as well",
      daemon.handover_awaits(_dR, "executor"), True)
daemon.reconcile()
check("NO record is written from the rotated-out session's channel",
      _recR(), [])
print("   and it releases: the launch record is what says a replacement is")
print("   still awaited on this path, so ageing it past startup_grace is")
print("   what ends the wait - not the case reaching into the guard.")
with daemon._lock:
    _pr63 = (daemon.STATE.get("pids") or {}).get(_kR)
    if _pr63:
        _pr63["at"] = (
            time.time()
            - float(daemon.CFG["thresholds"].get("startup_grace", 600)) - 5)
check("the wait is over", daemon.handover_awaits(_dR, "executor"), False)
daemon.reconcile()
check("and now the very same witness DOES adopt the window", len(_recR()), 1)

post("/session", {"action": "stop", "project": _p63, "role": "executor"})
daemon.STATE["handover"] = {}

print("\n64. the record and the receiver must agree about the nudge")
print("    A RECORD THAT WAS WRONG IN THE FLATTERING DIRECTION. The branch")
print("    that stayed quiet journalled a line; the branch that woke the")
print("    planner journalled nothing. So the record could only ever show")
print("    this nudge quieter than it was, and nobody goes looking behind")
print("    good news. Measured 2026-08-31: the planner RECEIVED five nudges")
print("    after 15:10:50 - iterations 70, 71, 74, 76 and 77 - and the")
print("    journal held not one line about any of them. The only witness")
print("    was the receiving side, because none existed on this side.")
print("    Report 78 read the record and wrote 'the nudge has been silent")
print("    since 16:25' into a shift handoff, which is exactly the belief a")
print("    handoff exists to stop carrying into tomorrow.")
print("    So this case asserts the two sides AGREE - what the planner got,")
print("    and what the bridge wrote down about giving it - and it is the")
print("    RECORD half that is new. Case 61 already proved the delivery.")

_k64 = _hk
daemon.STATE.pop("nudge_tally", None)
daemon.STATE.pop("held_verdict", None)
post("/loop", {"action": "start", "project": _hp, "reset": False})


def _tally64():
    return dict((daemon.STATE.get("nudge_tally") or {}).get(_k64)
                or {"sent": 0, "held": 0, "failed": 0, "last": ""})


check("the tally starts at nothing, so a rise cannot be left over",
      (_tally64().get("sent"), _tally64().get("held")), (0, 0))

_nudge_was64 = daemon.NUDGE_AFTER_VERDICT_SEC
try:
    print("   side one: it fires - and the record has to say it fired")
    daemon.NUDGE_AFTER_VERDICT_SEC = 0.5
    del DELIVERED[_pk61][:]
    check("a report was answered with nothing in it",
          _done61("Checked: seen.txt\nRECORD-64A"), True)
    check("the planner really was woken - the RECEIVER side, as in case 61",
          _nudged61(True), True)
    check("and the record agrees: one firing, not none",
          until(lambda: _tally64().get("sent") == 1, 20), True)
    check("written down as sent, which is what happened",
          _tally64().get("last"), "sent")
    check("and nothing was miscounted as held", _tally64().get("held"), 0)

    print("   side two: it holds - the record must not call that a firing,")
    print("   or the tally tomorrow's measurement reads is worthless")
    daemon.NUDGE_AFTER_VERDICT_SEC = 4.0
    del DELIVERED[_pk61][:]
    check("a second report was answered",
          _done61("Checked: seen.txt\nRECORD-64B"), True)
    # The planner sends the work itself, INSIDE the nudge's delay - which is
    # the only way this branch is reached, and the real order it happens in.
    post("/task", {"project": _hp,
                   "instructions": "the planner sent it without being asked"},
         secret=True)
    check("the nudge held, because the task was already on its way",
          until(lambda: _tally64().get("held") == 1, 30), True)
    check("the planner was NOT woken a second time", _nudged61(False), False)
    check("and the firing count did not move", _tally64().get("sent"), 1)
finally:
    daemon.NUDGE_AFTER_VERDICT_SEC = _nudge_was64

print("\n65. a compaction the bridge stood aside for is not interrupted")
print("   THE INCIDENT (a live executor, 2026-09-02). At 01:10:31 the API")
print("   refused the turn as too long - 1000561 against 1000000 - and on")
print("   this client that refusal is what STARTS the compaction (5.37), so")
print("   the bridge wrote compact_wait and said in the journal that the")
print("   session was 'left alone to finish it'. It then woke that session")
print("   THREE TIMES into the recovery it was waiting for: revive at")
print("   01:13:50 and at 01:16:51, an idle nudge at 01:19:51. The turn those")
print("   insertions started ran 412s and died at 01:20:43; no fifth")
print("   compact_boundary was ever written to the transcript. compact_wait")
print("   was read by nobody except the two functions that maintain it.")
print("   Real order, real endpoints (5.33): the death goes in as a POST and")
print("   the tick is the real assess(), never a hand-built snapshot.")


def _got65(c):
    return json.dumps(DELIVERED.get((c, "executor"), []), ensure_ascii=False)


def _settled65():
    """Join what a tick may have started, before denying that it did."""
    waited = []
    for t in threading.enumerate():
        if t is threading.current_thread() or not t.is_alive():
            continue
        if (t.name or "").startswith(("handover", "rotate")):
            waited.append(t.name)
            t.join(60)
    return waited


def _quiet65(c, seconds=3.0):
    """NEGATIVE: wait the whole time, then answer.

    The shape sabotage found in case 61: `if (arrived) == want` answers a
    negative on its FIRST look, before anything could have arrived, so it
    is green whatever the code does. A negative check has to outlive what
    it denies.
    """
    end = time.time() + seconds
    while time.time() < end:
        if _got65(c) != "[]":
            return False
        time.sleep(0.25)
    return _got65(c) == "[]"


def _pair65(tag):
    """A pair at the ceiling with a channel that records what reaches it."""
    d, c, k = _ovf_pair(tag, 1000561)
    register(d, "executor", tag + "-ex")
    DELIVERED.setdefault((c, "executor"), []).clear()
    return d, c, k


def compaction_waiting_via_state(key):
    return (daemon.STATE.get("compact_wait") or {}).get(key)


def _holds65(c):
    """The journal lines this pair's hold wrote, newest last."""
    return [r for r in daemon.store.recent_events(400)
            if "stood aside" in (r.get("text") or "")
            and os.path.basename(c) in (r.get("path") or "").lower()]


_thr65 = dict(daemon.CFG.get("thresholds") or {})
try:
    post("/config", {"thresholds": dict(_thr65, stopfail_grace=0)})

    print("   gate (a) THE CONTROL, and it comes first on purpose: with no")
    print("   wait standing, this exact fixture DOES deliver. Without this,")
    print("   the silence in (b) would prove nothing - it would be green on")
    print("   a fixture that never delivers anything to anybody.")
    print("   FAILURE WOULD LOOK LIKE: an empty channel here.")
    _dA, _cA, _kA = _pair65("hold-ctl")
    _too_long(_dA, "hold-ctl-ex", 1000561)
    check("the wait was written for the control too",
          bool((daemon.STATE.get("compact_wait") or {}).get(_kA)), True)
    # The independent variable, and the ONLY one: the record itself. Same
    # death, same grace, same channel, same tick.
    with daemon._lock:
        (daemon.STATE.get("compact_wait") or {}).pop(_kA, None)
    daemon.assess(_dA)
    _settled65()
    check("something reached the executor once the wait was gone",
          until(lambda: _got65(_cA) != "[]", 10), True)

    print("   gate (b) THE SUBJECT: the wait stands, so nothing is inserted")
    print("   FAILURE WOULD LOOK LIKE: the revive's text in the channel -")
    print("   which is 01:13:50, exactly.")
    _dB, _cB, _kB = _pair65("hold-sub")
    _too_long(_dB, "hold-sub-ex", 1000561)
    check("the wait is standing for the subject",
          bool((daemon.STATE.get("compact_wait") or {}).get(_kB)), True)
    daemon.assess(_dB)
    _settled65()
    check("NOTHING reached the executor while its compaction was awaited",
          _quiet65(_cB), True)
    check("and the wait was not consumed by the tick",
          bool((daemon.STATE.get("compact_wait") or {}).get(_kB)), True)

    print("   gate (c) one line per decision, not one per tick")
    print("   FAILURE WOULD LOOK LIKE: three lines for three ticks, which is")
    print("   a journal that drowns the incident it is recording.")
    _n65 = len(_holds65(_cB))
    daemon.assess(_dB)
    daemon.assess(_dB)
    _settled65()
    check("still nothing delivered after three ticks in all",
          _quiet65(_cB, 2.0), True)
    check("and the hold said so exactly once", len(_holds65(_cB)) - _n65, 0)
    check("one line stands for the whole episode", _n65, 1)

    print("   gate (d) THE RELEASE: when the compaction lands, work resumes")
    print("   FAILURE WOULD LOOK LIKE: a gate that never opens - a pair")
    print("   silenced for good by one refused turn.")
    post_rc("/status", {"role": "executor", "payload": {
        "session_id": "hold-sub-ex",
        "workspace": {"current_dir": _dB, "project_dir": _dB},
        "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
        "context_window": {"context_window_size": 1000000,
                           "used_percentage": 7.0,
                           "current_usage": {
                               "input_tokens": 10,
                               "cache_creation_input_tokens": 90,
                               "cache_read_input_tokens": 70_000,
                               "output_tokens": 400}}}})
    daemon.assess(_dB)
    _settled65()
    check("the wait was dropped once the summary landed",
          (daemon.STATE.get("compact_wait") or {}).get(_kB), None)
    check("and the executor is reachable again",
          until(lambda: _got65(_cB) != "[]", 10), True)

    print("   gate (e) THE CONTROL for the third consumer: the idle nudge in")
    print("   assess() is a different insertion from the revive, and gate (c)")
    print("   proved only the revive was reached - ONE hold line, and the")
    print("   revive's. So it gets its own control and its own subject.")
    print("   FAILURE WOULD LOOK LIKE: assess() stopping on a higher rung, in")
    print("   which case (f) below would be green without the gate existing.")
    _dE, _cE, _kE = _pair65("nudge-ctl")
    post("/config", {"projects": dict(daemon.CFG["projects"],
                                      **{_dE: {"silence_minutes": 0}})})
    daemon.assess(_dE)
    _settled65()
    _aE = (daemon.STATE.get("assessed") or {}).get(_dE) or {}
    check("with nothing to wait for, the nudge is what assess() decides",
          _aE.get("did"), "sent it its state")
    check("and it really reached the executor",
          until(lambda: _got65(_cE) != "[]", 10), True)

    print("   gate (f) THE SUBJECT: same rung, wait standing, nudge withheld")
    _dF, _cF, _kF = _pair65("nudge-sub")
    post("/config", {"projects": dict(daemon.CFG["projects"],
                                      **{_dF: {"silence_minutes": 0}})})
    _too_long(_dF, "nudge-sub-ex", 1000561)
    check("the wait is standing", bool(compaction_waiting_via_state(_kF)), True)
    daemon.assess(_dF)
    _settled65()
    _aF = (daemon.STATE.get("assessed") or {}).get(_dF) or {}
    check("assess() named the compaction as the reason it did nothing",
          _aF.get("saw"), "an idle executor whose compaction is still running")
    check("it sent nothing", _aF.get("did"), "nothing")
    check("and NOTHING reached the executor by any route", _quiet65(_cF), True)
finally:
    post("/config", {"thresholds": _thr65})

print("\n66. a failed compaction is not evidence if the bridge interfered")
print("   THE INCIDENT (2026-09-02). The wait opened at 01:10:31, the bridge")
print("   woke the session three times inside it, the turn that started died")
print("   at 01:20:43, and at 01:20:51 the bridge wrote compact_failed=1000561")
print("   against the pair. Nothing had begun: no PreCompact, no fifth")
print("   compact_boundary. That record then WAS the pair's ceiling -")
print("   compaction_too_big reads it as failed_at - turn_widest, so at")
print("   09:57:11 a session was replaced 'past the 890k wall (measured")
print("   here)'. note_compaction_failed has said since it was written that")
print("   'only a compaction nobody interfered with gets to testify'; until")
print("   now that was a sentence with nothing under it (rule 24).")


def _failed66(k):
    return (daemon.STATE.get("compact_failed") or {}).get(k)


def _pair66(tag, precompact=True):
    """A pair at the ceiling whose turn was refused as too long."""
    d, c, k = _ovf_pair(tag, 1000561)
    register(d, "executor", tag + "-ex")
    DELIVERED.setdefault((c, "executor"), []).clear()
    _too_long(d, tag + "-ex", 1000561)
    if precompact:
        # A start witness that is the CLIENT's, not ours: the bridge is
        # judging a wait it opened itself, so a witness it produces would
        # be an echo of the event (rule 30).
        post_rc("/event", {"hook_event_name": "PreCompact", "role": "executor",
                           "session_id": tag + "-ex", "project_dir": d,
                           "cwd": d})
    return d, c, k


_rec_was66 = daemon.COMPACT_RECOVERY_SEC
try:
    print("   gate (a) THE CONTROL: a clean wait DOES get recorded, with")
    print("   provenance. Without this every silence below would be green on")
    print("   a gate that simply never records anything.")
    print("   FAILURE WOULD LOOK LIKE: no record here.")
    _dA, _cA, _kA = _pair66("prov-clean")
    check("no failure on record yet", _failed66(_kA), None)
    _bA = len(launches())
    daemon.COMPACT_RECOVERY_SEC = 0
    daemon.assess(_dA)
    _settled65()
    _fA = _failed66(_kA) or {}
    check("the clean failure was recorded", _fA.get("tokens"), 1000561)
    check("and it carries what confirms it",
          "PreCompact" in ((_fA.get("why") or {}).get("began") or ""), True)
    check("the provenance names the size it is about",
          (_fA.get("why") or {}).get("tokens"), 1000561)
    # rotate_executor sleeps two seconds in its thread before it launches,
    # so this is waited for rather than assumed - the same allowance case 55
    # makes. A POSITIVE check may answer the moment it arrives.
    check("and the session was replaced",
          until(lambda: len(launches()) > _bA, 20), True)

    print("   gate (b) a DELIVERY into the wait disqualifies the measurement")
    print("   FAILURE WOULD LOOK LIKE: a compact_failed record - which is")
    print("   exactly the 01:20:51 write, unchanged.")
    daemon.COMPACT_RECOVERY_SEC = _rec_was66
    _dB, _cB, _kB = _pair66("prov-deliver")
    post("/task", {"project": _dB, "instructions": "something to do"},
         secret=True)
    check("the task really reached it",
          until(lambda: _got65(_cB) != "[]", 10), True)
    _bB = len(launches())
    daemon.COMPACT_RECOVERY_SEC = 0
    daemon.assess(_dB)
    _settled65()
    check("NOTHING was written to the ceiling", _failed66(_kB), None)
    check("but the session was still replaced - it IS at the ceiling",
          until(lambda: len(launches()) > _bB, 20), True)
    check("and the journal says why it refused to measure",
          any("nobody interfered with" in (r.get("text") or "")
              for r in daemon.store.recent_events(200)), True)

    print("   gate (c) a SECOND death inside the wait disqualifies it")
    print("   FAILURE WOULD LOOK LIKE: 01:20:43 counted as a compaction")
    print("   result, when what it was is a network error.")
    daemon.COMPACT_RECOVERY_SEC = _rec_was66
    _dC, _cC, _kC = _pair66("prov-death")
    post("/event", {"hook_event_name": "StopFailure", "cwd": _dC,
                    "role": "executor", "session_id": "prov-death-ex",
                    "error": "server_error",
                    "last_assistant_message": "API Error: Connection refused"})
    _bC = len(launches())
    daemon.COMPACT_RECOVERY_SEC = 0
    daemon.assess(_dC)
    _settled65()
    check("a network death is not a compaction result", _failed66(_kC), None)
    check("the session was replaced all the same",
          until(lambda: len(launches()) > _bC, 20), True)

    print("   gate (d) and the refusal that OPENS a wait is not itself")
    print("   interference - the self-alibi shape of 5.33, refused by the")
    print("   order note_stopfail runs in rather than by a special case.")
    print("   FAILURE WOULD LOOK LIKE: gate (a) going red, since its own")
    print("   too-long death would have disqualified its own wait.")
    check("gate (a) recorded, so the opening death did not taint it",
          (_failed66(_kA) or {}).get("tokens"), 1000561)

    print("   gate (e) NOTHING BEGAN: no PreCompact and no compact_boundary")
    print("   FAILURE WOULD LOOK LIKE: 'it did not land' written about a")
    print("   compaction that never took off - the 01:20:51 record exactly.")
    daemon.COMPACT_RECOVERY_SEC = _rec_was66
    _dE2, _cE2, _kE2 = _pair66("prov-nostart", precompact=False)
    _bE2 = len(launches())
    daemon.COMPACT_RECOVERY_SEC = 0
    daemon.assess(_dE2)
    _settled65()
    check("no start, so no measurement", _failed66(_kE2), None)
    check("the session was replaced all the same",
          until(lambda: len(launches()) > _bE2, 20), True)
    check("and the refusal says nothing ever started",
          any("no compaction ever started" in (r.get("text") or "")
              for r in daemon.store.recent_events(200)), True)
finally:
    daemon.COMPACT_RECOVERY_SEC = _rec_was66

print("   gate (f) the dated migration drops the night's record and only it")
print("   FAILURE WOULD LOOK LIKE: the neighbour going too, which would make")
print("   this a sweep of the ceiling rather than one dated repair.")
with daemon._lock:
    daemon.STATE.setdefault("compact_failed", {})["mig|executor"] = {
        "tokens": 1000561, "at": 1788301251.4076195, "last": 1000561}
    daemon.STATE["compact_failed"]["keep|executor"] = {
        "tokens": 1000561, "at": 1788301999.0, "last": 1000561}
    daemon.STATE["compact_failed"]["keep2|executor"] = {
        "tokens": 990000, "at": 1788301251.4076195, "last": 990000}
_gone66 = daemon.migrate_unearned_failure()
check("the night's record is gone", _failed66("mig|executor"), None)
check("it was named in what the migration returned",
      "mig|executor" in _gone66, True)
check("a record of the same size at another second is untouched",
      (_failed66("keep|executor") or {}).get("tokens"), 1000561)
check("a record of another size in the same second is untouched",
      (_failed66("keep2|executor") or {}).get("tokens"), 990000)
check("and it says so at warn, naming the pair",
      any("Dropped the failed-compaction record of 2026-09-02" in
          (r.get("text") or "") for r in daemon.store.recent_events(200)),
      True)
with daemon._lock:
    daemon.STATE["compact_failed"].pop("keep|executor", None)
    daemon.STATE["compact_failed"].pop("keep2|executor", None)

print("\n67. a window that takes a report and never opens a turn")
print("    2026-09-02, a planner on another project this bridge watches.")
print("    Report 822 was written to")
print("    its channel at 13:32:09 and DEQUEUED 8 ms later - the client had")
print("    it - and no turn opened for 68 minutes. Every delivery answered")
print("    `written`, so chan_backlog stayed empty and unread_channel, whose")
print("    whole job is to name a window that is not reading, never fired.")
print("    `written` is set where THIS process writes its own stdout, so it")
print("    is a witness the act of asking produces (rule 30). The witness")
print("    that can answer is the planner's own transcript. -> 5.46")
print("    Real order on the throwaway daemon (5.33): a real Stop hook, a")
print("    real report, a real channel that accepts and answers nothing.")

EPSILON = os.path.join(TMP, "epsilon")
os.makedirs(EPSILON, exist_ok=True)
_pk67 = canon(EPSILON)
post("/config", {"projects": {A: {}, B: {}, C: {}, DELTA: {}, EPSILON: {}}})
check("the pair's project joins the watch list",
      _pk67 in (daemon.CFG.get("projects") or {}), True)

_tdir67 = os.path.join(TMP, "deaf-transcripts")
os.makedirs(_tdir67, exist_ok=True)
_tp67 = os.path.join(_tdir67, "pl-eps.jsonl")
_real67 = sessions.transcript_of
sessions.transcript_of = (lambda sid, path=None:
                          _tp67 if sid == "pl-eps" else _real67(sid, path))


def _wrote67(ago):
    """The planner's transcript, with its last turn `ago` seconds back."""
    with open(_tp67, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(time.time() - ago))
                         + ".000Z",
            "message": {"content": "a turn"}}) + chr(10))


_PAD67 = (' The report is written long on purpose: under IDLE_TURN_CHARS the idle damper calls the exchange empty and holds the third Stop hook instead of making a report, which is right and is not what this case is about.')


def _deaf67():
    return (daemon.STATE.get("deaf") or {}).get("%s|planner" % _pk67) or {}


def _report67(text):
    """One executor turn, ending in a report nobody answers.

    The planner's channel is the ordinary stand: it takes the POST and
    returns 200, which is exactly what the real channel process did all
    through the incident. Nothing posts a verdict, so run_review waits
    channel_silence_warn and takes the branch under test.
    """
    out = {}

    def run():
        out.update(stop_hook(EPSILON, "executor", "ex-eps", text) or {})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(90)
    return out


try:
    register(EPSILON, "executor", "ex-eps")
    register(EPSILON, "planner", "pl-eps")
    post("/loop", {"project": EPSILON, "action": "start"}, secret=True)
    with daemon._lock:
        for _role, _sid in (("executor", "ex-eps"), ("planner", "pl-eps")):
            daemon.STATE.setdefault("sessions", {})[
                "%s:%s" % (_role, _sid[:8])] = {
                "role": _role, "path": _pk67, "session_id": _sid,
                "model": "Opus 5", "window": 1000000,
                "window_observed": True, "context_tokens": 120000,
                "state": "idle", "last_seen": daemon.now(),
                "seen_at": time.time()}
            daemon.STATE.setdefault("last_session", {})[
                "%s|%s" % (_pk67, _role)] = _sid
        daemon.save_state()

    print("\n   (a) the control FIRST, so the case can tell the two apart:")
    print("   a planner whose window IS opening turns must not be accused.")
    print("   It has to write its turn AFTER the report goes out, because")
    print("   that is what a live planner does and it is what the witness")
    print("   asks - a turn written before the report answers nothing")
    _wrote67(4000)
    _tg_before = len(tg_texts())
    _alive67 = threading.Timer(2.0, _wrote67, args=(0,))
    _alive67.daemon = True
    _alive67.start()
    _report67("report while the planner is alive." + _PAD67)
    _alive67.join(10)
    check("a moving planner is not recorded as deaf", _deaf67(), {})
    check("and the pair is not held",
          bool((daemon.STATE.get("paused") or {}).get(_pk67)), False)

    print("\n   (b) now the incident: the same delivery, the same 200, and a")
    print("   window whose transcript has recorded no turn since before it")
    _wrote67(4000)
    _report67("report 1 into a window that is not opening turns." + _PAD67)
    check("the report was recorded as taken but unanswered",
          _deaf67().get("n"), 1)
    check("and the transcript WAS readable, so this is a fact about the "
          "window and not about our eyesight", _deaf67().get("readable"),
          True)
    check("it says so in the journal, at warn",
          any(r.get("level") == "warn"
              and "no turn since the report went out" in (r.get("text") or "")
              for r in daemon.store.recent_events(300)), True)
    print("   and the sentence that used to go out here was a GUESS - it")
    print("   named a known client bug that was not what was happening")
    check("the old guess is not sent any more",
          any("inbound channel messages are being dropped" in x
              for x in tg_texts()[_tg_before:]), False)
    check("the pair is NOT held on the first one",
          bool((daemon.STATE.get("paused") or {}).get(_pk67)), False)

    print("\n   (c) the second one holds the pair - two, not the silence")
    print("   counter's three, because three is 65 minutes and 65 minutes is")
    print("   what the incident was")
    _wrote67(4000)
    _report67("report 2 into the same window." + _PAD67)
    check("two in a row", _deaf67().get("n"), 2)
    _held67 = (daemon.STATE.get("paused") or {}).get(_pk67) or {}
    check("the pair is held", bool(_held67), True)
    note("the reason the pair is held", _held67.get("why"))
    check("and the reason names the window, not the channel",
          "taken 2 reports off its channel" in (_held67.get("why") or "")
          and "channel is not the problem" in (_held67.get("why") or ""),
          True)
    print("   NOTHING was re-delivered into it, and that is measured, not")
    print("   assumed: on the day, the two reports written into the stalled")
    print("   window were enqueued at 11:40:14.223Z and REMOVED at")
    print("   11:40:33.379Z having never become `user` records at all")
    _sent67 = [d for d in DELIVERED.get((_pk67, "planner"), [])
               if (d.get("meta") or {}).get("kind") == "report"]
    check("three reports were made and three were delivered - not one of "
          "them twice", (len(_sent67),
                         len(set(json.dumps(d.get("content"),
                                            ensure_ascii=False)
                                 for d in _sent67))), (3, 3))

    print("\n   (c2) the witness is anchored on WHEN THE REPORT WENT OUT,")
    print("   not on 'warn_after seconds ago'. The first draft used the")
    print("   latter and (a) caught it - a planner whose turn was written a")
    print("   second before the report was accused of never opening one.")
    print("   With (a) written the way a live planner behaves - the turn")
    print("   comes AFTER the report - the two anchors differ only by the")
    print("   wait's own overshoot, well under a second, so behaviour cannot")
    print("   separate them without a race. Pinned by source instead, which")
    print("   is honest about being a weaker check than a red one")
    _src67 = inspect.getsource(daemon.run_review)
    check("the delivery stamps its own time",
          "sent_at = time.time()" in _src67, True)
    check("and the witness is asked against that, not against a window",
          "planner_took_report(path, sent_at)" in _src67, True)

    print("\n   (c3) fail-closed is not fail-ACCUSING. With no transcript to")
    print("   read, 'that window is not opening turns' is a claim the bridge")
    print("   cannot make, so it counts and journals and does NOT hold. The")
    print("   silence counter is what covers that case and always did.")
    print("   test_handover's own silence case found this: it stubs")
    print("   deliver_ex and has no transcript, so every report looked deaf")
    # Both counters back to zero, not just the hold: by now the ordinary
    # silence counter is past its own limit from the reports above, and a
    # pair held for THAT never reaches run_review's delivery at all.
    daemon.clear_silence(_pk67, "epsilon")
    daemon.resume_project(_pk67)
    with daemon._lock:
        daemon.STATE.get("deaf", {}).pop("%s|planner" % _pk67, None)
        daemon.save_state()
    sessions.transcript_of = lambda sid, path=None: None
    _report67("report into a window with no transcript to read." + _PAD67)
    _report67("second such report." + _PAD67)
    check("it is still counted, and honestly",
          (_deaf67().get("n"), _deaf67().get("readable")), (2, False))
    check("but the pair is NOT held on an absence of evidence",
          bool((daemon.STATE.get("paused") or {}).get(_pk67)), False)
    check("and the journal says why it did not",
          any("not something the bridge can say" in (r.get("text") or "")
              for r in daemon.store.recent_events(300)), True)
    sessions.transcript_of = (lambda sid, path=None:
                              _tp67 if sid == "pl-eps" else _real67(sid, path))

    print("\n   (d) the planner comes back: one verdict clears it")
    _wrote67(1)
    daemon.clear_deaf(EPSILON)
    check("the record is gone", _deaf67(), {})
    check("and a second clear is honest about having done nothing",
          daemon.clear_deaf(EPSILON), False)
finally:
    sessions.transcript_of = _real67
    post("/loop", {"project": EPSILON, "action": "stop"}, secret=True)
    daemon.resume_project(_pk67)
    post("/config", {"projects": {A: {}, B: {}, C: {}, DELTA: {}}})

print("\n68. a background command is not over when its call returns")
print("    A project this bridge watches, 2026-09-03. Its executor")
print("    launched a long chain of work in the background")
print("    with run_in_background at 10:48:22; the bridge journalled")
print("    \"Finished in 0s\" at 10:48:23 and the job ran 10 111 s. From")
print("    that second inflight_live was empty, so assess() called the")
print("    executor idle and sent it its state 26 times that day - each a")
print("    delivery, a report and a verdict, none of them about the work.")
print("    Meanwhile the `wait` branch read the RAW PROCTRACK, which held a")
print("    41-hour-old leak, so it answered the opposite: something IS")
print("    running, and the pair was never called in. Two witnesses of one")
print("    question, disagreeing, and both wrong.")
print("    Real order throughout (5.33): the actual PreToolUse, the actual")
print("    PostToolUse, then the deciding call - never a hand-built STATE.")

_p68 = os.path.join(TMP, "bg-blind")
os.makedirs(_p68, exist_ok=True)
_k68 = canon(_p68)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p68: {}}})
post_rc("/loop", {"action": "start", "project": _p68})
_s68 = "bg68-ex"
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68})
post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                   "session_id": "bg68-pl", "project_dir": _p68, "cwd": _p68})
register(_p68, "executor", _s68)
register(_p68, "planner", "bg68-pl")

# The transcript the client would be writing. Two records matter and this
# case writes exactly the two the real one had: the tool_use that launched
# the job, and - later - the notification that ends it. Everything the
# bridge concludes below it concludes by reading these.
_t68 = os.path.join(TMP, "bg68.jsonl")


def _tw68(rows):
    with open(_t68, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


_CMD68 = "py tools/night_chain.py 2>&1 | tail -60"
_TID68 = "toolu_68BACKGROUNDCHAIN"
_tw68([{"type": "user", "message": {"content": "start"},
        "timestamp": "2026-09-03T07:00:00.000Z"}])
daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t68 if sid == _s68 else "")

print("   (a) the record outlives the call that started it")
print("   FAILURE WOULD LOOK LIKE: PostToolUse empties inflight_live, so a")
print("   pair with a three-hour job running reads as idle and is nudged.")
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68,
                   "tool_name": "Bash",
                   "tool_input": {"command": _CMD68,
                                  "run_in_background": True}})
check("the launch was tracked", bool(daemon.inflight_live(_p68)), True)
_tw68([{"type": "assistant", "timestamp": "2026-09-03T07:48:22.639Z",
        "message": {"content": [
            {"type": "tool_use", "id": _TID68, "name": "Bash",
             "input": {"command": _CMD68, "run_in_background": True}}]}}])
post_rc("/event", {"hook_event_name": "PostToolUse", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68,
                   "tool_name": "Bash",
                   "tool_input": {"command": _CMD68,
                                  "run_in_background": True}})
check("and its own PostToolUse does NOT end it",
      bool(daemon.inflight_live(_p68)), True)
check("so the pair reads as busy, not idle",
      bool(daemon.situation(_k68)["inflight"]), True)
check("and assess() says so instead of nudging an idle executor",
      "still running" in (daemon.assess(_k68).get("saw") or ""), True)
print("   an ORDINARY command is untouched: its PostToolUse still ends it")
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68,
                   "tool_name": "Bash",
                   "tool_input": {"command": "gradle build one"}})
check("the foreground build is tracked too",
      len(daemon.inflight_live(_p68)), 2)
post_rc("/event", {"hook_event_name": "PostToolUse", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68,
                   "tool_name": "Bash",
                   "tool_input": {"command": "gradle build one"}})
check("and it is gone when its call returns, exactly as before",
      len(daemon.inflight_live(_p68)), 1)

print("   the client's own notice is what ends the background one - a")
print("   witness the launch cannot have produced (rule 30)")
# getattr, because on the code BEFORE this change there is no witness at
# all - and a red proof has to be a FAIL line, not a traceback that takes
# the rest of the case with it. This one cannot be red-first in the useful
# sense: there is nothing there to be red about, the same limit case 70c
# has on the branch.
_bgf68 = getattr(daemon, "bg_finished", None)
# And the record has to be FETCHED safely, not subscripted on faith: under
# the sabotage that removes it there is nothing at index 0, and an
# IndexError three characters after a check that has already spoken takes
# every block below it down in silence.
_recs68 = list((daemon.STATE.get("inflight", {}).get(_k68) or {}).values())
check("before the notice, nothing has ended it",
      _bgf68(_p68, "executor", _recs68[0])[0]
      if (_bgf68 and _recs68) else "no live background record to ask about",
      False)
_tw68([{"type": "user", "timestamp": "2026-09-03T10:36:53.493Z",
        "message": {"content":
                    "<task-notification>\n<task-id>bk68</task-id>\n"
                    "<tool-use-id>" + _TID68 + "</tool-use-id>\n"
                    "<status>completed</status>\n</task-notification>"}}])
if hasattr(daemon, "check_background"):
    daemon.check_background()
check("the notice ends it", daemon.inflight_live(_p68), [])
check("and the pair is idle again, honestly this time",
      list(daemon.situation(_k68)["inflight"]), [])

print("   (b) ONE witness of \"something is running\", and it ages")
print("   FAILURE WOULD LOOK LIKE: the wait branch reads the raw PROCTRACK,")
print("   a leaked record keeps it True for ever, and needs_you - the only")
print("   thing that tells a person the pair is parked on them - never")
print("   fires. On the pair this came from: two days, thirty-odd waits.")
with daemon._lock:
    daemon.STATE.setdefault("inflight", {})[_k68] = {
        "SCR=": {"cmd": "SCR= a shell variable, taken for a command",
                               "started": time.time() - 41 * 3600}}
    daemon.STATE.get("waiting_on_you", {}).pop(_k68, None)
    daemon.save_state()
daemon.PROCTRACK.pop(_k68, None)
daemon.reseed_proctrack()
check("a record older than any real command is not handed back",
      daemon.PROCTRACK.get(_k68) or {}, {})
check("and it is not \"running\" either", bool(daemon.inflight_live(_p68)),
      False)
_t68w = threading.Thread(
    target=stop_hook, args=(_p68, "executor", _s68, "the leak case"),
    daemon=True)
_t68w.start()
check("a report is waiting", until(lambda: bool(_k68 in daemon.PENDING), 25), True)
post("/verdict", {"project": _p68, "verdict": "wait",
                  "feedback": "Waiting."}, secret=True)
_t68w.join(40)
check("with nothing really running, the person is called - once",
      until(lambda: bool((daemon.STATE.get("waiting_on_you")
                          or {}).get(_k68)), 20), True)

print("   CONTROL: with a LIVE background job the same wait is silent,")
print("   which is the answer that must NOT change")
with daemon._lock:
    daemon.STATE.get("waiting_on_you", {}).pop(_k68, None)
    daemon.STATE["inflight"][_k68] = {}
    daemon.save_state()
daemon.PROCTRACK.pop(_k68, None)
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68,
                   "tool_name": "Bash",
                   "tool_input": {"command": "py tools/second_chain.py",
                                  "run_in_background": True}})
_t68c = threading.Thread(
    target=stop_hook, args=(_p68, "executor", _s68, "the control"),
    daemon=True)
_t68c.start()
check("a second report is waiting", until(lambda: bool(_k68 in daemon.PENDING), 25),
      True)
post("/verdict", {"project": _p68, "verdict": "wait",
                  "feedback": "Waiting."}, secret=True)
_t68c.join(40)
check("nobody is called about a pair whose job is really running",
      bool((daemon.STATE.get("waiting_on_you") or {}).get(_k68)), False)

print("   (c) a wait that carries words hands them back on the hook")
print("   FAILURE WOULD LOOK LIKE: the planner writes the next piece into a")
print("   wait and the executor never sees a character - which is what")
print("   happened at 15:02 on 2026-09-03, until the owner asked why")
print("   nobody was doing anything and the same words were sent as a task.")
_out68 = {}


def _turn68(text, tag):
    _out68[tag] = stop_hook(_p68, "executor", _s68, text)


_WORK68 = ("Checked it myself: the marker is in place. " + (
    "Put the lesson in section 5 in one line; add an idempotency marker "
    "to the sandbox patches and run two of them back to back. ") * 3)
_ACK68 = "Accepted. Waiting for the numbers."
_t68a = threading.Thread(target=_turn68, args=(_WORK68, "work"), daemon=True)
_t68a.start()
check("the report for the work-carrying wait is waiting",
      until(lambda: bool(_k68 in daemon.PENDING), 25), True)
post("/verdict", {"project": _p68, "verdict": "wait", "feedback": _WORK68},
     secret=True)
_t68a.join(40)
check("its words came back on the Stop hook the executor was blocked on",
      "idempotency marker" in json.dumps(_out68.get("work"),
                                        ensure_ascii=False), True)
check("with the preface that says they may be work",
      "do not end the turn just to say you are waiting"
      in json.dumps(_out68.get("work"), ensure_ascii=False), True)
print("   and a bare acknowledgement is NOT worth a turn: it stays silent")
_t68b = threading.Thread(target=_turn68, args=(_ACK68, "ack"), daemon=True)
_t68b.start()
check("the report for the acknowledgement is waiting",
      until(lambda: bool(_k68 in daemon.PENDING), 25), True)
post("/verdict", {"project": _p68, "verdict": "wait", "feedback": _ACK68},
     secret=True)
_t68b.join(40)
# The envelope carries a status too, so assert on the thing this check is
# about - and on the KEY as well as the value, or "missing" and "None" would
# read the same and the check could not fail.
check("nothing was handed back for it",
      ("hook_output" in (_out68.get("ack") or {}),
       (_out68.get("ack") or {}).get("hook_output")), (True, None))
print("   and with NOTHING running even a long wait hands nothing back -")
print("   there is no work to do while waiting for a person")
with daemon._lock:
    daemon.STATE["inflight"][_k68] = {}
    daemon.STATE.get("waiting_on_you", {}).pop(_k68, None)
    daemon.save_state()
daemon.PROCTRACK.pop(_k68, None)
_t68d = threading.Thread(target=_turn68, args=(_WORK68, "parked"),
                         daemon=True)
_t68d.start()
check("the third report is waiting", until(lambda: bool(_k68 in daemon.PENDING), 25),
      True)
post("/verdict", {"project": _p68, "verdict": "wait", "feedback": _WORK68},
     secret=True)
_t68d.join(40)
check("a pair parked on a person is called, not handed prose",
      ((_out68.get("parked") or {}).get("hook_output"),
       bool((daemon.STATE.get("waiting_on_you") or {}).get(_k68))),
      (None, True))

print("   (d) and the PLANNER is told what its wait costs, for nothing")
print("   2026-09-03, the owner asked three times in one day why both")
print("   halves were standing still. Every time the same shape: the")
print("   executor had started a run in the background and ended its turn")
print("   to wait for it, and the planner answered wait with no work in it.")
print("   FAILURE WOULD LOOK LIKE: the planner gets \"recorded\" and nothing")
print("   else, and finds out an hour later from a person.")
print("   It rides on the verdict tool's OWN answer, which the planner is")
print("   already waiting for - no delivery, no wake, no round trip.")
with daemon._lock:
    daemon.STATE["inflight"][_k68] = {}
    daemon.STATE.get("waiting_on_you", {}).pop(_k68, None)
    daemon.STATE.get("last_task", {}).pop(_k68, None)
    daemon.save_state()
daemon.PROCTRACK.pop(_k68, None)
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _s68, "project_dir": _p68, "cwd": _p68,
                   "tool_name": "Bash",
                   "tool_input": {"command": "py tools/walker_probe.py",
                                  "run_in_background": True}})
check("a background run is going", bool(daemon.inflight_live(_p68)), True)
_t68e = threading.Thread(
    target=_turn68, args=("started the walker in the background", "idle"),
    daemon=True)
_t68e.start()
check("its report is waiting", until(lambda: _k68 in daemon.PENDING, 25), True)
_r68e = post("/verdict", {"project": _p68, "verdict": "wait",
                          "feedback": "Waiting for the walker."}, secret=True)
_t68e.join(40)
check("the planner is told the executor will now sit there",
      "engine-free work with task now" in json.dumps(_r68e), True)

print("   and NOT told it when it has already handed work over: a task")
print("   newer than the executor's last finished turn IS work in hand")
# THE REAL ORDER, and the first draft of this block had it wrong twice: the
# task has to go out while the report is still waiting, because a Stop
# stamps stop_seen and would put the turn back in front of the task; and the
# verdict has to answer a REPORT, because a /verdict with nothing waiting
# takes a different branch of the handler and never reaches the note at all.
_t68g = threading.Thread(
    target=_turn68, args=("the walker is still going", "inhand"), daemon=True)
_t68g.start()
check("a second report is waiting", until(lambda: _k68 in daemon.PENDING, 25),
      True)
post_rc("/task", {"project": _p68,
                  "instructions": "three things that need no engine"})
check("the task is on the record, and newer than the last finished turn",
      until(lambda: float((daemon.STATE.get("last_task") or {}).get(_k68)
                          or 0)
            > float((daemon.STATE.get("stop_seen") or {})
                    .get(_k68 + "|executor") or 0), 20), True)
_r68f = post("/verdict", {"project": _p68, "verdict": "wait",
                          "feedback": "Waiting for the walker."}, secret=True)
_t68g.join(40)
check("no note this time - there is nothing to warn about",
      "engine-free work with task now" in json.dumps(_r68f), False)
print("   and with NO background run there is no note either, whatever")
print("   else is true: this is about a wait that means hours, not a wait")
print("   that means a person")
with daemon._lock:
    daemon.STATE["inflight"][_k68] = {}
    daemon.STATE.get("last_task", {}).pop(_k68, None)
    daemon.save_state()
daemon.PROCTRACK.pop(_k68, None)
_wn68 = getattr(daemon, "wait_note", None)
check("wait_note says nothing without a background run",
      _wn68(_p68) if _wn68 else "there is no wait_note in this daemon", "")
print("   the channel is the thin edge: it carries the note, it does not")
print("   decide it - one place knows what a wait costs")
check("channel.py hands the daemon's note on",
      'out.get("note")' in inspect.getsource(_chan), True)

print("\n69. a background record does not outlive the window that started it")
print("    The hole case 68 opened. A bg record now lives until the client's")
print("    notice or BG_MAX_SEC (8 h) - but the notice is written into the")
print("    SESSION that launched the job, and the job is a child of that")
print("    window. Replace the window, kill it or close it and the notice")
print("    can never come, so the record stands for eight hours as an alibi")
print("    for inflight, clinch() and stalled(). That is a new silence in")
print("    place of the old one, which is not a repair.")
print("    Real order (5.33): the actual PreToolUse, PostToolUse, then the")
print("    actual SessionStart of the replacement - no hand-built STATE.")

_p69 = os.path.join(TMP, "bg-orphan")
os.makedirs(_p69, exist_ok=True)
_k69 = canon(_p69)
_q69 = os.path.join(TMP, "bg-orphan-other")
os.makedirs(_q69, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p69: {}, _q69: {}}})
post_rc("/loop", {"action": "start", "project": _p69})


def _up69(project, role, sid):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": role,
                       "session_id": sid, "project_dir": project,
                       "cwd": project})


def _bg69(project, role, sid, cmd):
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": role,
                       "session_id": sid, "project_dir": project,
                       "cwd": project, "tool_name": "Bash",
                       "tool_input": {"command": cmd,
                                      "run_in_background": True}})
    post_rc("/event", {"hook_event_name": "PostToolUse", "role": role,
                       "session_id": sid, "project_dir": project,
                       "cwd": project, "tool_name": "Bash",
                       "tool_input": {"command": cmd,
                                      "run_in_background": True}})


_up69(_p69, "executor", "ex1-orph")
_up69(_p69, "planner", "pl1-orph")
_up69(_q69, "executor", "oth1-orph")
_bg69(_p69, "executor", "ex1-orph", "py tools/long_chain.py")
check("the record survived its own PostToolUse, as case 68 requires",
      len(daemon.inflight_live(_p69)), 1)
check("and the pair reads as busy on it",
      bool(daemon.situation(_k69)["inflight"]), True)

print("   FAILURE WOULD LOOK LIKE: the replacement comes up and the record")
print("   of the window that is gone stays, so the pair stays 'busy' for")
print("   eight hours on a job nobody can hear from again.")
_up69(_p69, "executor", "ex2-orph")
check("a new session for the same half drops it",
      until(lambda: len(daemon.inflight_live(_p69)) == 0, 20), True)
check("and the pair is free again",
      bool(daemon.situation(_k69)["inflight"]), False)

print("   CONTROLS - three things that must NOT drop it, and each is a way")
print("   the last one could have been right by accident")
_bg69(_p69, "executor", "ex2-orph", "py tools/second_chain.py")
check("a fresh record for the live session", len(daemon.inflight_live(_p69)),
      1)
_up69(_p69, "planner", "pl2-orph")
check("the OTHER half coming up leaves it alone",
      len(daemon.inflight_live(_p69)), 1)
_up69(_q69, "executor", "oth2-orph")
check("and so does another project's executor",
      len(daemon.inflight_live(_p69)), 1)
print("   and the same session starting again - a resume - is not a")
print("   replacement: the window is the same one, the job is still in it")
_up69(_p69, "executor", "ex2-orph")
check("its own SessionStart leaves its own record alone",
      len(daemon.inflight_live(_p69)), 1)

print("   A DEAD TURN IS NOT A DEAD JOB, and this is the one that would be")
print("   easy to add and wrong: the process belongs to the window, not to")
print("   the turn, so the window is still standing there running it.")
daemon.note_stopfail(_p69, "executor", "api error", True)
check("a stopfail does not touch the record",
      len(daemon.inflight_live(_p69)), 1)
check("and the pair is still busy, which is the truth",
      bool(daemon.situation(_k69)["inflight"]), True)

print("   SessionEnd is the other witness, and the one that is supposed to")
print("   arrive - it just usually does not, because taskkill fires none")
post_rc("/event", {"hook_event_name": "SessionEnd", "role": "executor",
                   "session_id": "ex2-orph", "project_dir": _p69,
                   "cwd": _p69})
check("the session ending drops what it was running",
      until(lambda: len(daemon.inflight_live(_p69)) == 0, 20), True)
_hl69 = [r for r in daemon.store.recent_events(400, project=_p69)
         if "bg record dropped" in (r.get("text") or "")]
check("and the journal says whose session it was and why",
      (len(_hl69) >= 2,
       # The line carries sid[:8], which is what a journal line should
       # show, and the fixture's ids differ inside those eight.
       any("ex2-orph" in (r.get("text") or "") for r in _hl69),
       any("the session ended" in (r.get("text") or "") for r in _hl69),
       any("a new session started" in (r.get("text") or "") for r in _hl69)),
      (True, True, True, True))
post("/loop", {"project": _p69, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n70. three things the bridge lost track of while a pair stood still")
print("    All three from one evening, 2026-09-03, and all three were found")
print("    by watching a real pair rather than by reading the code.")

_p70 = os.path.join(TMP, "stood-still")
os.makedirs(_p70, exist_ok=True)
_k70 = canon(_p70)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p70: {}}})
post_rc("/loop", {"action": "start", "project": _p70})
for _r, _s in (("executor", "ss-ex-1"), ("planner", "ss-pl-1")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p70, "cwd": _p70})
    register(_p70, _r, _s)

print("   (a) a task that arrived mid-turn has to BE recorded as one")
print("   FAILURE WOULD LOOK LIKE: the executor is working, the task is")
print("   delivered, nothing is held, and the next `done` asks the planner")
print("   for a new piece while the old one sits unanswered. That is what")
print("   happened at 18:31:51, and it was read as a restart losing the")
print("   record - `tasks_open` is in STATE and on disk, so it survives")
print("   one. The record was never made: looks_busy() asks whether the")
print("   LAST entry is an unreturned tool, so BETWEEN two tool calls it")
print("   says 'not busy' of a turn in full flow.")
_t70 = os.path.join(TMP, "ss-ex.jsonl")


def _wrote70(when):
    """A transcript of a turn that is writing - and nothing else."""
    with open(_t70, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": when,
            "message": {"content": [{"type": "text", "text": "working"}]}})
            + "\n")


_wrote70(time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                       time.gmtime(time.time() - 5)))
daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t70 if sid == "ss-ex-1" else "")
check("a turn that is writing, with no tool pending, IS mid-turn",
      daemon.task_arrived_mid_turn(_p70), True)
print("   and a turn that has genuinely stopped writing is not")
_wrote70(time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                       time.gmtime(time.time() - 4000)))
check("an executor silent for over an hour is not mid-turn",
      daemon.task_arrived_mid_turn(_p70), False)
print("   THE WHOLE WAY ROUND, through the endpoints: task while writing,")
print("   then a report, then `done` - the task comes back rather than the")
print("   planner being asked for another")
_wrote70(time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                       time.gmtime(time.time() - 5)))
with daemon._lock:
    daemon.STATE["tasks_open"] = {}
    daemon.save_state()
post_rc("/task", {"project": _p70, "instructions": "MIDTURN-PIECE"})
# /task answers before it acts - the delivery runs in a thread - so this
# waits for the record rather than reading straight after the POST.
check("the task was held because the executor was working",
      until(lambda: len((daemon.STATE.get("tasks_open") or {}).get(_k70)
                        or []) == 1, 25), True)
print("   and it is on DISK, so a restart cannot lose it - what was")
print("   actually broken was the recording, not the keeping")
def _ondisk70():
    """What a restart would read back - state.json, not memory."""
    try:
        with open(os.path.join(os.environ["BRIDGE_DATA"], "state.json"),
                  encoding="utf-8") as fh:
            return len((json.load(fh).get("tasks_open") or {}).get(_k70)
                       or [])
    except (OSError, ValueError):
        return -1


check("state.json carries the held task across a restart",
      until(lambda: _ondisk70() == 1, 25), True)
check("and the container is named in the inventory, so it moves and is "
      "dropped with the project", "tasks_open" in daemon.STATE_PATHS, True)

print("   (b) a hold the BRIDGE put on knows its own kind")
print("   FAILURE WOULD LOOK LIKE: clear_silence recognising its own hold")
print("   by the words in it - and the deaf hold, whose words are")
print("   different, outliving the window it was about. On 2026-09-03 that")
print("   was 16:55:03 to 19:07:51, lifted by a person.")
def _hold70(why, by):
    """Put a bridge hold on, on EITHER side of this change.

    Harness, like _waiter and _spin: pause_project grew a `by` argument
    here, and calling the new signature against the old module dies with a
    TypeError instead of failing on behaviour. No case asserts through it.
    """
    if "by" in inspect.signature(daemon.pause_project).parameters:
        return daemon.pause_project(_p70, why, by=by)
    return daemon.pause_project(_p70, why)


_hold70("the planner's window has taken 2 reports off its channel "
        "without opening a turn", "deaf")
with daemon._lock:
    daemon.STATE.setdefault("deaf", {})["%s|planner" % _k70] = {
        "n": 2, "at": time.time(), "sid": "ss-pl-1", "readable": True}
    daemon.save_state()
check("the pair is held", bool((daemon.STATE.get("paused") or {}).get(_k70)),
      True)
check("and the hold says which kind it is, rather than only why",
      ((daemon.STATE.get("paused") or {}).get(_k70) or {}).get("by"), "deaf")
print("   the replacement window comes up - the real SessionStart")
post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                   "session_id": "ss-pl-2", "project_dir": _p70,
                   "cwd": _p70})
check("the deaf hold is lifted by the window being replaced",
      until(lambda: not (daemon.STATE.get("paused") or {}).get(_k70), 20),
      True)
check("and the complaint about that window is spent",
      ("%s|planner" % _k70) in (daemon.STATE.get("deaf") or {}), False)
print("   CONTROLS: a hold a PERSON put on is not touched, and the same")
print("   window saying hello twice is not a replacement")
post("/cmd", {"cmd": "pause", "project": _p70})
post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                   "session_id": "ss-pl-3", "project_dir": _p70,
                   "cwd": _p70})
check("a human's pause survives a planner starting",
      bool((daemon.STATE.get("paused") or {}).get(_k70)), True)
post("/cmd", {"cmd": "resume", "project": _p70})
_hold70("deaf again", "deaf")
with daemon._lock:
    daemon.STATE.setdefault("deaf", {})["%s|planner" % _k70] = {
        "n": 2, "at": time.time(), "sid": "ss-pl-4", "readable": True}
    daemon.save_state()
post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                   "session_id": "ss-pl-4", "project_dir": _p70,
                   "cwd": _p70})
check("the SAME window starting again is not a replacement",
      bool((daemon.STATE.get("paused") or {}).get(_k70)), True)
with daemon._lock:
    (daemon.STATE.get("paused") or {}).pop(_k70, None)
    (daemon.STATE.get("deaf") or {}).pop("%s|planner" % _k70, None)
    daemon.save_state()

print("   (c) the planner that comes back is TOLD what it missed")
print("   FAILURE WOULD LOOK LIKE: the count going into the journal only,")
print("   so a replacement window comes up knowing nothing about the")
print("   reports in the inbox and no way to find out but being told.")
with daemon._lock:
    daemon.STATE["missed_reports"] = {}
    daemon.save_state()
# getattr, because on the code before this change there is no recorder at
# all - and a red proof has to be a FAIL line, not a traceback that takes
# the rest of the case with it.
_nmr70 = getattr(daemon, "note_missed_report", None)
if _nmr70:
    _nmr70(_p70, 41, os.path.join("inbox", "041-report.md"))
    _nmr70(_p70, 42, os.path.join("inbox", "042-report.md"))
_port70 = open_channel(_p70, "planner")
post("/channel/register", {"project": _p70, "port": _port70,
                           "pid": os.getpid(), "role": "planner"},
     secret=True)
daemon.remember_session(_p70, "planner", "ss-pl-4")
_seen70 = DELIVERED.setdefault((_k70, "planner"), [])
_before70 = len(_seen70)
daemon.deliver_ex(_p70, "planner", "REPORT-AFTER-THE-HOLD",
                  {"kind": "report"})
check("something reached the planner",
      until(lambda: len(_seen70) > _before70, 20), True)
_got70 = json.dumps(_seen70[-1:], ensure_ascii=False) if _seen70 else ""
check("and it names both reports it never saw, by file",
      ("041-report.md" in _got70, "042-report.md" in _got70,
       "REPORT-AFTER-THE-HOLD" in _got70), (True, True, True))
print("   CONTROL: with nothing missed the next delivery carries no such")
print("   line - a header that is always there is not a message")
_b2 = len(_seen70)
daemon.deliver_ex(_p70, "planner", "PLAIN-REPORT", {"kind": "report"})
check("the second delivery went too",
      until(lambda: len(_seen70) > _b2, 20), True)
check("and says nothing about an inbox",
      "went to bridge-logs" in json.dumps(_seen70[-1:],
                                          ensure_ascii=False), False)
print("   (d) the deaf COUNT belongs to the window it was counted against")
print("   FAILURE WOULD LOOK LIKE: two reports counted against window A,")
print("   window B replaces it, ONE unanswered report there, and the pair")
print("   is held at three - B gets four minutes instead of two reports.")
print("   That is 2026-09-03: the hold at 19:16:40 stood on a count two")
print("   thirds of which belonged to a window that was already gone.")
with daemon._lock:
    daemon.STATE["deaf"] = {"%s|planner" % _k70: {
        "n": 2, "at": time.time() - 300, "last_report": 10,
        "readable": True, "sid": "ss-pl-A"}}
    (daemon.STATE.get("paused") or {}).pop(_k70, None)
    daemon.save_state()
daemon.remember_session(_p70, "planner", "ss-pl-A")
check("the count stands against window A", (daemon.STATE.get("deaf") or {}).get(
    "%s|planner" % _k70, {}).get("n"), 2)
post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                   "session_id": "ss-pl-B", "project_dir": _p70,
                   "cwd": _p70})
check("window B coming up spends the complaint about A",
      until(lambda: ("%s|planner" % _k70) not in (daemon.STATE.get("deaf")
                                                  or {}), 20), True)
print("   so one unanswered report at B counts as ONE, not three, and")
print("   holds nothing - DEAF_REPORTS_BEFORE_HOLD is 2")
_r70d = daemon.note_deaf_planner(_p70, "stood-still", 11, True)
check("B's first unanswered report counts one and holds nothing",
      ((daemon.STATE.get("deaf") or {}).get("%s|planner" % _k70, {}).get("n"),
       _r70d, bool((daemon.STATE.get("paused") or {}).get(_k70))),
      (1, False, False))
with daemon._lock:
    daemon.STATE["deaf"] = {}
    daemon.save_state()

print("   (e) and a hold ends when the window it named starts working")
print("   FAILURE WOULD LOOK LIKE: the window wakes up and nothing")
print("   notices, because assess() returns on `paused` before it looks at")
print("   anything. On 2026-09-03 the planner came back at 19:46 and the")
print("   pair stayed held on a complaint that had stopped being true.")
_t70p = os.path.join(TMP, "ss-pl.jsonl")
_prev70 = daemon.sessions.transcript_of


def _pl_wrote70(when):
    with open(_t70p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": when,
            "message": {"content": [{"type": "text", "text": "thinking"}]}})
            + "\n")


daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t70p if sid == "ss-pl-B" else
                                 (_t70 if sid == "ss-ex-1" else ""))
daemon.remember_session(_p70, "planner", "ss-pl-B")
_since70 = time.time() - 120
_pl_wrote70(time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                          time.gmtime(_since70 - 60)))
_hold70("the planner's window has taken 2 reports off its channel without "
        "opening a turn", "deaf")
with daemon._lock:
    daemon.STATE.setdefault("deaf", {})["%s|planner" % _k70] = {
        "n": 2, "at": _since70, "last_report": 12, "readable": True,
        "sid": "ss-pl-B"}
    daemon.save_state()
# getattr, because before this change there is no such release at all -
# the red proof has to be a FAIL line, not a traceback.
_dho70 = getattr(daemon, "deaf_hold_over", lambda p: "no deaf_hold_over")
check("held, and the window has written nothing since the complaint",
      (bool((daemon.STATE.get("paused") or {}).get(_k70)), _dho70(_p70)),
      (True, False))
print("   the window opens a turn - the transcript is the witness, and it")
print("   fails CLOSED: an unreadable one leaves the hold standing")
_pl_wrote70(time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
daemon.assess(_p70)
check("the deaf hold is lifted once that window works again",
      until(lambda: not (daemon.STATE.get("paused") or {}).get(_k70), 20),
      True)
_hl70e = [r for r in daemon.store.recent_events(400, project=_p70)
          if "the deaf hold is lifted" in (r.get("text") or "")]
check("and the journal names the window and when it opened a turn",
      len(_hl70e) >= 1, True)
print("   CONTROL: a hold a PERSON put on is not lifted by any of this")
post("/cmd", {"cmd": "pause", "project": _p70})
with daemon._lock:
    daemon.STATE.setdefault("deaf", {})["%s|planner" % _k70] = {
        "n": 2, "at": _since70, "last_report": 13, "readable": True,
        "sid": "ss-pl-B"}
    daemon.save_state()
check("a human's pause survives a working planner",
      (_dho70(_p70), bool((daemon.STATE.get("paused") or {}).get(_k70))),
      (False, True))
post("/cmd", {"cmd": "resume", "project": _p70})
with daemon._lock:
    daemon.STATE["deaf"] = {}
    daemon.save_state()
daemon.sessions.transcript_of = _prev70

post("/loop", {"project": _p70, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n71. the restart gate, and the word that ends a background job")
print("    Both from 2026-09-03, and both found by the loop tripping over")
print("    itself: a restart that refused to run, and a record that would")
print("    not end for eight hours after the job behind it was killed.")

from bridgecore import relayout as _rl71                       # noqa: E402

_p71 = os.path.join(TMP, "relayout-gate")
os.makedirs(_p71, exist_ok=True)
_k71 = canon(_p71)
_n71 = daemon.project_name(_p71)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p71: {}}})
_s71 = "gate71-ex"
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": _s71, "project_dir": _p71, "cwd": _p71})
register(_p71, "executor", _s71)

_t71 = os.path.join(TMP, "gate71.jsonl")
_prev71 = daemon.sessions.transcript_of


def _tw71(rows):
    with open(_t71, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


_tw71([{"type": "user", "message": {"content": "start"},
        "timestamp": "2026-09-03T19:00:00.000Z"}])
daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t71 if sid == _s71 else _prev71(sid, cwd))


def _bg71(cmd):
    """Launch a background command the way the client does: PreToolUse."""
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _s71, "project_dir": _p71, "cwd": _p71,
                       "tool_name": "Bash",
                       "tool_input": {"command": cmd,
                                      "run_in_background": True}})


def _fg71(cmd):
    """An ORDINARY command: tracked, and something is waiting on it.

    The difference from _bg71 is the whole of the 2026-09-04 rule - a
    background job outlives its own PostToolUse and nothing blocks on it,
    so the restart gate stopped counting those. A foreground one is work
    a restart would interrupt, and still holds the gate.
    """
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _s71, "project_dir": _p71, "cwd": _p71,
                       "tool_name": "Bash",
                       "tool_input": {"command": cmd}})


def _clear71():
    """Take the records away by hand - a bg record has no other end yet."""
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(_k71, None)
        daemon.save_state()
    daemon.PROCTRACK.pop(_k71, None)


def _gate71():
    """THE REAL GATE, over real HTTP, against this suite's own daemon.

    Not busy_now's internals and not a hand-built /state: the same GET the
    restart makes, answered by the same handler, so what is measured is
    what a person typing `relayout --now` would get.
    """
    verdict, lines = _rl71.busy_now(port=PORT)
    mine = [ln for ln in lines if ln.startswith(_n71 + ":")]
    others = [ln for ln in lines if not ln.startswith(_n71 + ":")
              and "not counting" not in ln and ln != "every pair is idle"]
    return verdict, mine, others


print("   (a) a gate may not count ITSELF")
print("   FAILURE WOULD LOOK LIKE: `relayout --now` started in the")
print("   background is tracked like every other Bash tool, so the NEXT")
print("   restart is refused - by its own ancestor - and nothing lifts")
print("   that but the record ageing out eight hours later. That is")
print("   2026-09-03, three refusals in a row, all naming one record.")
_clear71()
# THE GATE READS EVERY PAIR, so a neighbour left busy by an earlier case
# would decide this one - and the two answers this case is about are
# "quiet" and "busy" for the whole daemon. The five things the gate looks
# at are emptied for every pair but this one, and the precondition below
# says out loud that it worked; if it ever stops working the FAIL names
# the fixture rather than quietly measuring the wrong thing.
_clear71()
with daemon._lock:
    for _c71 in ("inflight", "awaiting", "handover"):
        for _op71 in list(daemon.STATE.get(_c71) or {}):
            if _op71 != _k71:
                (daemon.STATE.get(_c71) or {}).pop(_op71, None)
    for _op71 in list(daemon.STATE.get("compact_wait") or {}):
        if not _op71.startswith(_k71 + "|"):
            (daemon.STATE.get("compact_wait") or {}).pop(_op71, None)
    daemon.save_state()
for _op71 in [_q for _q in list(daemon.PROCTRACK) if _q != _k71]:
    daemon.PROCTRACK.pop(_op71, None)
for _op71 in [_q for _q in list(daemon.PENDING) if _q != _k71]:
    daemon.PENDING.pop(_op71, None)
_v71, _m71, _o71 = _gate71()
check("PRECONDITION: no other pair in this daemon is holding the gate",
      _o71, [])
print("   FIRST that the gate is alive and can see this pair at all - an")
print("   ordinary FOREGROUND command, and it refuses by name")
_fg71("pytest tests/render_all.py -q")
check("an ordinary foreground command is tracked",
      len(daemon.inflight_live(_p71)), 1)
_v71, _m71, _o71 = _gate71()
check("and the gate refuses, naming this pair",
      ([ln for ln in _m71 if "still running" in ln] != [], _v71),
      (True, "busy"))
print("   AND THE RULE THAT CHANGED THIS CASE: a BACKGROUND job is not")
print("   work this gate protects. Nothing waits on one - it outlives its")
print("   own PostToolUse, the stuck watch skips it for that reason, and")
print("   reseed_proctrack hands it back two seconds after the daemon")
print("   returns. 63 of them held a restart for five minutes on")
print("   2026-09-04 and cost nothing when it went ahead anyway.")
_clear71()
_bg71("py tools/nightly_render.py --all")
check("a background job is tracked exactly like any other",
      len(daemon.inflight_live(_p71)), 1)
_v71, _m71, _o71 = _gate71()
check("but the gate does not stop for it",
      ([ln for ln in _m71 if "still running" in ln], _v71), ([], "quiet"))
check("and says what it excluded, rather than going quiet in silence",
      len([ln for ln in _m71 if "background command" in ln]), 1)
note("what it says", ([ln for ln in _m71 if "background" in ln] or [""])[0])
_clear71()
_fg71("pytest tests/render_all.py -q")
print("   now the same thing, except the command IS the restart")
_clear71()
_bg71("nohup py -m bridgecore.relayout --now --wait 900 > relayout.log 2>&1 &")
check("the restart is tracked exactly like any other command",
      len(daemon.inflight_live(_p71)), 1)
_v71, _m71, _o71 = _gate71()
check("the gate does NOT stop for a restart of its own",
      ([ln for ln in _m71 if "still running" in ln], _v71), ([], "quiet"))
check("and it SAYS what it excluded, rather than ignoring it quietly",
      len([ln for ln in _m71 if "not counting" in ln]), 1)
print("   CONTROL: the exclusion is one command by name, not a hole. Any")
print("   other background job still holds the gate, and the line saying")
print("   what was excluded stands BESIDE the refusal rather than in place")
print("   of it: excluded-and-still-busy and excluded-and-now-quiet have")
print("   to read differently.")
_fg71("pytest tests/render_all.py -q")
check("two records, one of them mine",
      len(daemon.inflight_live(_p71)), 2)
_v71, _m71, _o71 = _gate71()
check("the other job still refuses the restart",
      ([ln for ln in _m71 if "still running" in ln] != [], _v71),
      (True, "busy"))
check("and the exclusion is named beside the refusal",
      len([ln for ln in _m71 if "not counting" in ln]), 1)
print("   AND THE TWO EXCLUSIONS ARE A UNION, NOT A SUM. The restart of")
print("   its own is itself a background job, so counting both and")
print("   subtracting both removed that one record twice and took this")
print("   foreground command with it - the gate would have said quiet and")
print("   stopped the daemon under live work.")
check("one record excluded once: the foreground job still holds it",
      _rl71.count_inflight({
          "inflight": 2,
          "inflight_cmds": ["nohup py -m bridgecore.relayout --now &",
                            "py tools/nightly_render.py --all"],
          "inflight_bg_cmds": ["nohup py -m bridgecore.relayout --now &"],
      }), (1, 1))
check("and its own restart is named once, not twice",
      len([ln for ln in _m71 if "background command" in ln]), 0)
print("   and the line about what was EXCLUDED is never printed as the")
print("   reason the gate is waiting - it reads exactly like a line about")
print("   something running, and there are three places that pick one")
print("   line out of the list (the verdict, the waiting message, and the")
print("   `why` a caller records), so they share one definition")
_said71 = []
_rl71.wait_until_quiet(port=PORT, seconds=2, out=_said71.append)
check("the waiting message names what is RUNNING, not what was excluded",
      ([l for l in _said71 if "still running" in l] != [],
       [l for l in _said71 if "not counting" in l]), (True, []))
_clear71()

print("   (b) the client's NOTICE ends the job; the word inside it is only")
print("   a word")
print("   FAILURE WOULD LOOK LIKE: a status this bridge has not seen")
print("   before standing in front of a witness that has already answered,")
print("   so the record ages out over BG_MAX_SEC instead - eight hours of")
print("   a pair reading busy after its job ended. The list of statuses is")
print("   a guess about the client's vocabulary; the notification is the")
print("   fact. Measured this evening: a background relayout KILLED from")
print("   outside still produced a notice, and it said `completed` - the")
print("   client reports that the task is over, not how it died.")
_CMD71 = "py tools/night_chain.py --lane one"
_CTL71 = "py tools/night_chain.py --lane two"
_TID71 = "toolu_71ENDEDWITHASTRANGEWORD"
_CTID71 = "toolu_71CONTROLSTILLRUNNING"
_bg71(_CMD71)
_tw71([{"type": "assistant", "timestamp": "2026-09-03T19:10:00.000Z",
        "message": {"content": [
            {"type": "tool_use", "id": _TID71, "name": "Bash",
             "input": {"command": _CMD71, "run_in_background": True}}]}}])
_bg71(_CTL71)
_tw71([{"type": "assistant", "timestamp": "2026-09-03T19:10:05.000Z",
        "message": {"content": [
            {"type": "tool_use", "id": _CTID71, "name": "Bash",
             "input": {"command": _CTL71, "run_in_background": True}}]}}])
check("both jobs are running", len(daemon.inflight_live(_p71)), 2)
daemon.check_background()
check("and nothing has said either of them ended, so both stand",
      len(daemon.inflight_live(_p71)), 2)
# THE SHAPE THE REAL ONE HAD, and it is not the shape case 68 writes.
# A notice that arrives while a turn is running is ENQUEUED, and the client
# records that as a row with no `message` field at all: measured 2026-09-03
# at 17:27:46.276Z, `{"type": "queue-operation", "operation": "enqueue",
# "content": "<task-notification>..."}`, with an `attachment` twin. So the
# two shapes are pinned by two cases - case 68 the idle one, this the
# queued one - and a rewrite of bg_finished that parses each row and reads
# message.content reddens here instead of quietly running to the ceiling.
_tw71([{"type": "queue-operation", "operation": "enqueue",
        "timestamp": "2026-09-03T19:25:00.000Z",
        "content": "<task-notification>\n<task-id>bk71</task-id>\n"
                   "<tool-use-id>" + _TID71 + "</tool-use-id>\n"
                   "<status>ended</status>\n</task-notification>"}])
daemon.check_background()
_left71 = [(m or {}).get("cmd") or "" for m in daemon.inflight_live(_p71)]
check("a word nobody has seen before still ends the job it names",
      [c for c in _left71 if c.startswith(_CMD71)], [])
check("CONTROL: and only the job it names - the other one is untouched",
      len([c for c in _left71 if c.startswith(_CTL71)]), 1)
_j71 = [r for r in daemon.store.recent_events(400, project=_p71)
        if "Background command finished" in (r.get("text") or "")]
check("the journal says so, and says the word was an unfamiliar one",
      ("ended" in (_j71[0].get("text") or "") and
       "has not seen before" in (_j71[0].get("text") or ""))
      if _j71 else "no journal line was written at all", True)
print("   AND THE CONTROL IS A CONTROL: it can be ended, so its surviving")
print("   above is a fact about the id and not a fixture that never fires")
_tw71([{"type": "user", "timestamp": "2026-09-03T19:26:00.000Z",
        "message": {"content":
                    "<task-notification>\n<task-id>bk71b</task-id>\n"
                    "<tool-use-id>" + _CTID71 + "</tool-use-id>\n"
                    "<status>completed</status>\n</task-notification>"}}])
daemon.check_background()
check("its own notice ends it too", daemon.inflight_live(_p71), [])
_j71b = [r for r in daemon.store.recent_events(400, project=_p71)
         if "Background command finished" in (r.get("text") or "")]
check("and a status this bridge DOES know is not called unfamiliar",
      "has not seen before" in (_j71b[0].get("text") or "")
      if _j71b else "no journal line was written at all", False)

_clear71()
daemon.sessions.transcript_of = _prev71
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n72. the pair that blocked itself, and the three things that let it")
print("    2026-09-03, on a watched project: 23:50:05 a background job,")
print("    and six seconds later, 23:50:11, an")
print("    `until grep ...; do sleep 45; done` in the FOREGROUND to watch it,")
print("    and the executor's own turn blocked 2 h 45 min while the")
print("    engine-free work it had been handed stood untouched.")

_p72 = os.path.join(TMP, "self-blocked")
os.makedirs(_p72, exist_ok=True)
_k72 = canon(_p72)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p72: {}}})
post_rc("/loop", {"action": "start", "project": _p72})
for _r, _s in (("executor", "sb72-ex"), ("planner", "sb72-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p72, "cwd": _p72})
    register(_p72, _r, _s)

print("   (a) the rule forbade ONE way of waiting, so the waiting moved")
print("   FAILURE WOULD LOOK LIKE: `not a reason to end the turn` read as")
print("   permission to wait INSIDE it. Both ways have to be named.")
_line72 = daemon.RULES_ROLE_LINE.get("task") or ""
check("the line forbids ending the turn to wait",
      "ending the turn" in _line72, True)
check("and the foreground polling loop, by name",
      "until" in _line72 and "sleep" in _line72, True)
check("and it still says what to do instead",
      "engine-free" in _line72, True)
_deliv72 = daemon.rules_for_delivery("task", "sb72-ex")
check("and it rides on a real task delivery, not only in the constant",
      _line72 in _deliv72, True)
print("   the same line is on a verdict, and NOT on a report to the planner")
check("verdict carries it, report carries the planner's own line",
      (_line72 in daemon.rules_for_delivery("verdict", "sb72-ex"),
       _line72 in daemon.rules_for_delivery("executor report", "sb72-pl")),
      (True, False))

print("   (b) every notify() is written down, so `was a person rung?` has")
print("   an answer that is not somebody else's chat")
print("   FAILURE WOULD LOOK LIKE: 2026-09-03, asked at 00:07:06, step two")
print("   due at 00:17:06, and NOTHING in the journal either way - the only")
print("   record of the call was the thing the call produced (rule 30).")
_cmd72 = "gradle build the-long-one"
tg_reset()
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": "sb72-ex", "project_dir": _p72, "cwd": _p72,
                   "tool_name": "Bash", "tool_input": {"command": _cmd72}})
check("the foreground command is tracked",
      until(lambda: len(daemon.inflight_live(_p72)) == 1, 20), True)
_sig72 = [s for s in (daemon.PROCTRACK.get(_k72) or {})]
check("and it is NOT a background record", bool(_sig72) and not (
    daemon.PROCTRACK[_k72][_sig72[0]].get("bg")), True)
print("   the clock moves past stuck_limit - step one asks the PAIR, and")
print("   the human is not troubled at all")
_m72 = daemon.PROCTRACK[_k72][_sig72[0]]
# stuck_limit(None) is 900, not the 311 floor: the floor is the absolute
# minimum, and with no history the answer is fifteen minutes.
_m72["started"] = time.time() - 1000
daemon.check_processes()
check("step one asked the pair rather than a person",
      bool(_m72.get("asked_pair")), True)
check("and nothing went to the chat for it", tg_texts(), [])
print("   the grace passes and the thing is still running: NOW a person.")
print("   THIS IS THE ANSWER TO 00:17 - it fires, so the live call did go")
print("   out and was simply never written down.")
_m72["flagged"] = time.time() - 700          # over stuck_planner_grace (600)
daemon.check_processes()
check("step two rang a person", bool(_m72.get("told_human")), True)
check("the message reached the chat recorder",
      any("has been running" in t for t in tg_texts()), True)
_j72 = [r for r in daemon.store.recent_events(400, project=_p72)
        if "notify process_stuck" in (r.get("text") or "")]
check("and the journal now says so, with kind, level and destination",
      (len(_j72) >= 1,
       "telegram" in (_j72[0].get("text") if _j72 else "")), (True, True))
print("   CONTROL: a repeat of the SAME fact is suppressed, and the journal")
print("   says THAT too - `suppressed` and `sent` must not look alike")
_same72 = "%s: the very same fact, said twice" % daemon.project_name(_p72)
daemon.notify("process_stuck", _same72, path=_p72)
daemon.notify("process_stuck", _same72, path=_p72)
_j72b = [r.get("text") or "" for r in daemon.store.recent_events(400,
                                                                project=_p72)
         if "the very same fact" in (r.get("text") or "")]
check("both attempts are written down - one sent, one suppressed",
      (len(_j72b),
       len([t for t in _j72b if "suppressed" in t]),
       len([t for t in _j72b if "-> telegram" in t])), (2, 1, 1))
check("and only ONE of them reached the chat",
      len([t for t in tg_texts() if "the very same fact" in t]), 1)
print("   CONTROL: a kind that never reaches the chat is written as such,")
print("   not silently dropped")
daemon.notify("iteration_done", "a kind that is not in TELEGRAM_KINDS",
              path=_p72)
_j72c = [r for r in daemon.store.recent_events(400, project=_p72)
         if "notify iteration_done" in (r.get("text") or "")]
check("a log-only kind still leaves a line",
      (len(_j72c) >= 1,
       "log only" in (_j72c[0].get("text") if _j72c else "")), (True, True))

print("   (c) a command still running is not a leaked record")
print("   FAILURE WOULD LOOK LIKE: 01:02:30, `longer than any real one has")
print("   ever taken here. Treating it as a leaked record`, said of a call")
print("   that was open in a living session with the turn blocked on it.")
_t72 = os.path.join(TMP, "sb72-ex.jsonl")
_prev72 = daemon.sessions.transcript_of


def _write72(closed):
    """A transcript whose last entry is a tool that has (not) returned."""
    rows = [{"type": "user", "timestamp": "2026-09-03T20:50:00.000Z",
             "message": {"content": "go"}},
            {"type": "assistant", "timestamp": "2026-09-03T20:50:05.000Z",
             "message": {"content": [{"type": "tool_use", "id": "t72",
                                      "name": "Bash",
                                      "input": {"command": _cmd72}}]}}]
    if closed:
        rows.append({"type": "user", "timestamp": "2026-09-03T20:50:09.000Z",
                     "message": {"content": [{"type": "tool_result",
                                              "tool_use_id": "t72",
                                              "content": "done"}]}})
    with open(_t72, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t72 if sid == "sb72-ex" else "")
_write72(False)
_old72 = {"cmd": _cmd72, "session": "sb72-ex",
          "started": time.time() - daemon.INFLIGHT_MAX_SEC - 60}
check("an hour-old FOREGROUND record whose call is still open is not a leak",
      daemon.record_expired(dict(_old72)), False)
print("   CONTROL: the same age, the same session, but the call CLOSED -")
print("   that is the leak the ageing exists for, and it still ages")
_write72(True)
check("a closed call at the same age is still a leak",
      daemon.record_expired(dict(_old72)), True)
print("   CONTROL: a session the bridge cannot find is a leak too - the")
print("   exception is POSITIVE evidence only, and fails closed")
check("no session id: expired, exactly as before",
      daemon.record_expired({"cmd": _cmd72,
                             "started": time.time()
                             - daemon.INFLIGHT_MAX_SEC - 60}), True)
check("a session with no transcript: the same",
      daemon.record_expired({"cmd": _cmd72, "session": "sb72-nobody",
                             "started": time.time()
                             - daemon.INFLIGHT_MAX_SEC - 60}), True)
print("   and a transcript that RAISES on being read is the same answer -")
print("   this control exists because the two above never reach the except")
print("   branch at all: they answer False by falling through, so a version")
print("   that failed OPEN on an exception passed all of them. Found by")
print("   sabotaging exactly that, which is what sabotage is for.")


def _boom72(sid, cwd=None):
    raise IOError("the transcript cannot be read")


daemon.sessions.transcript_of = _boom72
check("an unreadable transcript: expired, because it fails CLOSED",
      daemon.record_expired(dict(_old72)), True)
daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t72 if sid == "sb72-ex" else "")
print("   CONTROL: a BACKGROUND record is untouched by any of this - it")
print("   keeps BG_MAX_SEC, and nothing rings for it")
check("a bg record an hour old is still live, as it always was",
      daemon.record_expired({"cmd": _cmd72, "bg": True, "session": "sb72-ex",
                             "started": time.time()
                             - daemon.INFLIGHT_MAX_SEC - 60}), False)
check("and a bg record past ITS ceiling is expired",
      daemon.record_expired({"cmd": _cmd72, "bg": True, "session": "sb72-ex",
                             "started": time.time()
                             - daemon.BG_MAX_SEC - 60}), True)
print("   and the pair blocking itself is told about ONCE, not per tick")
_write72(False)
tg_reset()
with daemon._lock:
    daemon.STATE.setdefault("inflight", {})[_k72] = {}
    daemon.save_state()
daemon.PROCTRACK[_k72] = {"gradle": dict(
    _old72, flagged=time.time() - 700, asked_pair=True, told_human=True)}
daemon.check_processes()
check("a person is told the half is blocking itself",
      any("blocking itself" in t for t in tg_texts()), True)
_n72 = len([t for t in tg_texts() if "blocking itself" in t])
daemon.check_processes()
daemon.check_processes()
check("and two more ticks add nothing - the latch is on the record",
      len([t for t in tg_texts() if "blocking itself" in t]), _n72)
check("the journal carries it at warn, where a leak used to be claimed",
      any("blocking itself" in (r.get("text") or "")
          for r in daemon.store.recent_events(400, project=_p72)), True)

daemon.PROCTRACK.pop(_k72, None)
daemon.sessions.transcript_of = _prev72
post("/loop", {"project": _p72, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n73. six hours without an executor, and the journal that drowned")
print("    2026-09-04: a handover decided at 04:02:19, the replacement")
print("    opened at 04:02:22 and never came up - it was waiting for Enter")
print("    on a prompt nobody could see - and from 05:28 the decision was")
print("    re-taken every minute, about 300 pairs of journal lines.")

_p73 = os.path.join(TMP, "never-came-up")
os.makedirs(_p73, exist_ok=True)
_k73 = canon(_p73)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p73: {}}})
post_rc("/loop", {"action": "start", "project": _p73})
for _r, _s in (("executor", "nc73-ex"), ("planner", "nc73-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p73, "cwd": _p73})
    register(_p73, _r, _s)

print("   (a) one decision, one record, then silence until the fact changes")
print("   FAILURE WOULD LOOK LIKE: the notify beside it latched for an hour")
print("   while both journal lines fired every tick, so the phone stayed")
print("   quiet and the journal buried the one line that mattered.")


# DRIVEN AT THE LATCH, not through a whole assess() tick: reaching the
# handover branch needs a session aged past `quiet` and a plan_for that
# says handover, and the tick-level path is being rebuilt in the piece that
# follows this one - a case that pins the old shape end to end would have to
# be rewritten with it. What is pinned here is the thing that was missing:
# the same decision, recognised as one already written down.
_fp73 = {"do": "handover", "why": "it has compacted 5 times"}
with daemon._lock:
    (daemon.STATE.get("handover_said") or {}).pop("%s|executor" % _k73, None)
    daemon.save_state()
check("the first decision is new, so it is written down",
      daemon.handover_decision_new(_p73, "executor", _fp73,
                                   "the replacement never registered"), True)
check("and the same fact twice more is not",
      (daemon.handover_decision_new(_p73, "executor", _fp73,
                                    "the replacement never registered"),
       daemon.handover_decision_new(_p73, "executor", _fp73,
                                    "the replacement never registered")),
      (False, False))
print("   the block changes - that IS a new fact, and it is said at once")
check("a different reason is a new decision",
      daemon.handover_decision_new(_p73, "executor", _fp73,
                                   "the window will not stop"), True)
print("   and so is a different reason for the handover itself")
check("a different plan is a new decision too",
      daemon.handover_decision_new(_p73, "executor",
                                   {"do": "handover", "why": "past the wall"},
                                   "the window will not stop"), True)
print("   CONTROL: a handover that actually runs forgets the fingerprint,")
print("   because the next decision after a replacement is a new one")
daemon.handover_decision_forget(_p73, "executor")
check("after a real handover the same fact is written down again",
      daemon.handover_decision_new(_p73, "executor",
                                   {"do": "handover", "why": "past the wall"},
                                   "the window will not stop"), True)
check("and the fingerprint is in the inventory, so it moves and is dropped "
      "with the project", "handover_said" in daemon.STATE_PATHS, True)

print("   (b) a window that has not come up is ASKED what it is asking")
print("   FAILURE WOULD LOOK LIKE: 04:02:22 to 10:13, six hours, and the")
print("   only thing the bridge could say was `never came up` - which sends")
print("   the reader to the window, where the answer was on the screen.")
_seen73 = {"screen": "", "answered": 0}
_scr73 = daemon.sessions.console_screen
_ans73 = daemon.sessions.console_answer
daemon.sessions.console_screen = (lambda pid, timeout=20:
                                  _seen73["screen"] if pid == 4321 else "")


def _answer73(pid, timeout=20):
    # counted PER PID: check_sessions walks every project's pids, and the
    # suite has several by now - a bare counter would be answering somebody
    # else's window and calling it this one's.
    if pid == 4321:
        _seen73["answered"] += 1
    return True


daemon.sessions.console_answer = _answer73

_seen73["screen"] = ("  WARNING: Loading development channels\n\n"
                     "  Channels: server:bridge\n\n"
                     "  ❯ 1. I am using this for local development\n"
                     "    2. Exit\n\n  Enter to confirm · Esc to cancel")
check("the known prompt is answered with Enter",
      (daemon.answer_window_prompt(_p73, "executor", 4321),
       _seen73["answered"]), ("channels", 1))
check("and the journal says which prompt was answered",
      any("development-channels prompt" in (r.get("text") or "")
          for r in daemon.store.recent_events(200, project=_p73)), True)

print("   the TRUST dialog means install.trust_folder did not take, and it")
print("   is not answered blind: its default is `No, exit`")
tg_reset()
_seen73["answered"] = 0
_seen73["screen"] = (" Accessing workspace:\n\n Quick safety check: Is this "
                     "a project you created or one you trust?\n\n"
                     " ❯ No, exit\n   Yes, I trust this folder")
check("the trust screen is NOT answered with a key",
      (daemon.answer_window_prompt(_p73, "executor", 4321),
       _seen73["answered"]), ("trust", 0))
check("a person is told, and the journal names the key that should be set",
      (any("trust dialog" in t for t in tg_texts()),
       any("hasTrustDialogAccepted" in (r.get("text") or "")
           for r in daemon.store.recent_events(200, project=_p73))),
      (True, True))

print("   an unknown screen goes into the journal VERBATIM and to a person")
print("   in 300 characters - a screen nobody has seen is exactly what a")
print("   human should read for themselves")
tg_reset()
_seen73["answered"] = 0
_seen73["screen"] = "SOMETHING NOBODY HAS SEEN BEFORE: press F7 to continue"
check("an unknown screen is not answered either",
      (daemon.answer_window_prompt(_p73, "executor", 4321),
       _seen73["answered"]), ("unknown", 0))
check("and both the journal and the chat carry its words",
      (any("press F7 to continue" in (r.get("text") or "")
           for r in daemon.store.recent_events(200, project=_p73)),
       any("press F7 to continue" in t for t in tg_texts())), (True, True))

print("   CONTROL: an unreadable console is said to be unreadable, not")
print("   guessed at")
_seen73["answered"] = 0
_seen73["screen"] = "   \n  "
check("nothing is answered when nothing could be read",
      (daemon.answer_window_prompt(_p73, "executor", 4321),
       _seen73["answered"]), ("unreadable", 0))

print("   CONTROL, AND THE DANGEROUS ONE: a window that HAS come up never")
print("   gets a key. Enter in a live session means `send what is typed`,")
print("   and what is typed there is whatever last touched that window.")
_seen73["answered"] = 0
_seen73["screen"] = "  WARNING: Loading development channels"
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % _k73] = {
        "pid": 4321, "at": time.time() - 300, "registered": True}
    daemon.STATE["started_at"] = time.time() - 9999
    daemon.save_state()
daemon.check_sessions(0)
check("a registered window is left alone",
      _seen73["answered"], 0)
print("   and one that has NOT registered is asked, once and only once")
with daemon._lock:
    daemon.STATE["pids"]["%s|executor" % _k73] = {
        "pid": 4321, "at": time.time() - 300, "registered": False}
    daemon.save_state()
daemon.check_sessions(0)
_once73 = _seen73["answered"]
daemon.check_sessions(0)
daemon.check_sessions(0)
check("asked once, and two more ticks add nothing",
      (_once73, _seen73["answered"]), (1, 1))
check("the latch is on the pid record, so it dies with the window",
      bool((daemon.STATE.get("pids") or {})
           .get("%s|executor" % _k73, {}).get("screen_asked")), True)

daemon.sessions.console_screen = _scr73
daemon.sessions.console_answer = _ans73
with daemon._lock:
    (daemon.STATE.get("pids") or {}).pop("%s|executor" % _k73, None)
    daemon.save_state()
post("/loop", {"project": _p73, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n74. the new window comes up BEFORE the old one is stopped")
print("    2026-09-04 04:02:19: the executor was stopped, its replacement")
print("    opened three seconds later and never came up, and the pair had")
print("    no executor for six hours. Stopping first puts the pair on the")
print("    floor before the risky half has even begun. The order is the")
print("    other way round now, which buys a minute in which BOTH windows")
print("    of one role are alive - and the three checks below are that")
print("    minute: who holds the channel seat, what may be delivered into")
print("    it, and what the old window's channel is allowed to vouch for.")

_p74 = os.path.join(TMP, "swap-order")
os.makedirs(_p74, exist_ok=True)
_k74 = canon(_p74)
_OLDWIN74, _OLDCHAN74 = 741001, 741002
post("/config", {"projects": {A: {}, B: {}, C: {}, _p74: {}}})
post_rc("/loop", {"action": "start", "project": _p74})
for _r, _s in (("executor", "sw74-ex"), ("planner", "sw74-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p74, "cwd": _p74})
register(_p74, "planner", "sw74-pl")
# The executor's channel is registered by hand rather than through
# register(), because these checks are ABOUT pid and ppid and the helper
# sends this process's own.
_export74 = open_channel(_p74, "executor")
post("/channel/register", {"project": _p74, "role": "executor",
                           "port": _export74, "pid": _OLDCHAN74,
                           "ppid": _OLDWIN74, "session_id": "sw74-ex"},
     secret=True)
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % _k74] = {
        "pid": _OLDWIN74, "at": time.time() - 3600, "registered": True,
        "registered_via": "session"}
    daemon.save_state()
check("the old window holds the channel seat before any of this",
      (daemon.channel_for(_p74, "executor") or {}).get("pid"), _OLDCHAN74)

# sessions.stop is the one thing that must not happen yet, so it is
# RECORDED rather than performed: the window pids here are invented, and a
# real taskkill would go hunting for whatever owns 741001 on this machine.
_stopped74 = []
_stop74, _alive74 = daemon.sessions.stop, daemon.sessions.pid_alive


_gone74 = set()


def _fakestop74(project, role, pid=None, wait=None, tree=True):
    _stopped74.append((role, pid))
    _gone74.add(int(pid or 0))
    return True


def _alive74stub(pid):
    """The old window is ALIVE until it is stopped, and nothing else is.

    It used to answer False for everything, including the window this case
    then asserted had been "stopped - in that order". The fixture was
    telling the daemon that window was dead while requiring a line that
    claims it was stopped - and the daemon printed the claim because it
    never asked. That is the live defect of 2026-09-05 16:12:22 (pid 12116,
    dead since 14:31:08) standing in the suite as an assertion.
    -> DECISIONS.md 8.17

    AND THE SECOND PAIR'S OLD WINDOW TOO (74 b): the case says it "was
    never touched" and "is still working", and since 8.36 a handover whose
    old window is dead and whose channel names none is refused - so the
    stub has to tell the daemon what the case asserts.
    """
    # AND THE NEWCOMER THAT NEVER CAME UP (8.46): a window that did not come
    # up is one sitting on its dialog - alive - and it answered dead here,
    # so the one definition had no window to vouch for when it was closed.
    return (int(pid or 0) in (_OLDWIN74, globals().get("_OLDWIN74B"),
                              globals().get("_new74b"))
            and int(pid or 0) not in _gone74)


daemon.sessions.stop = _fakestop74
daemon.sessions.pid_alive = _alive74stub


# GUARDED, so this case can be run against the code it repairs. On the
# pre-2026-09-04 daemon there is no such predicate at all, and a bare
# daemon.handover_swapping would raise AttributeError - a traceback that
# kills the flat script and reports nothing, where what is wanted is a FAIL
# line saying the swap window is not guarded. Same principle as
# read_or_fail, and as the _MARK guards in test_wall_handover.
_swapping74 = getattr(daemon, "handover_swapping", lambda p, r: False)


def _live74(role="executor", proj=None):
    """The session ids of this pair's LIVE records of one role.

    best_session() is the wrong instrument here and was the first thing
    tried: it skips any record with neither a model nor a context size,
    which is exactly what a bare SessionStart writes - so it answered {}
    for a session that had just come up, and `{}.get("state") not in
    ("ended", "died")` is True, green for the wrong reason.
    """
    return sorted((v.get("session_id") or "")
                  for v in (daemon.STATE.get("sessions") or {}).values()
                  if canon(v.get("path") or "") == canon(proj or _p74)
                  and v.get("role") == role
                  and v.get("state") not in ("ended", "died"))


def _j74(sub, proj=None):
    """Lines in a project's own journal that contain `sub`, in order.

    The project's file and not the central one: two of these checks are
    about the ORDER of two lines, and the central journal carries every
    other pair in the suite in between.
    """
    _f = os.path.join(proj or _p74, "bridge-logs",
                      time.strftime("%Y-%m-%d"), "events.jsonl")
    if not os.path.isfile(_f):
        return []
    out = []
    with open(_f, encoding="utf-8") as fh:
        for _i, _line in enumerate(fh):
            try:
                _row = json.loads(_line)
            except ValueError:
                continue
            if sub in (_row.get("text") or ""):
                _row["n"] = _i
                out.append(_row)
    return out


_before74 = len(launches())
_r74 = daemon.handover(_p74, "five compactions", ("executor",))
check("the handover went ahead", _r74.get("ok"), True)
check("and a window was opened for it",
      until(lambda: len(launches()) - _before74 == 1, 30), True)
check("NOTHING was stopped to make room for it", _stopped74, [])
check("the old executor is still a live record", "sw74-ex" in _live74(),
      True)
check("the journal says which way round it did it",
      bool(_j74("opening the new executor BEFORE stopping the old one")),
      True)
check("and does NOT yet say the old one was stopped",
      bool(_j74("was stopped - in that order")), False)
_newwin74 = ((daemon.STATE.get("pids") or {})
             .get("%s|executor" % _k74) or {}).get("pid")
check("the pid record already names the new window",
      bool(_newwin74) and _newwin74 != _OLDWIN74, True)

print("   (a) the channel seat, while two windows of one role are alive")
print("   parentage decides it before age does (5.19), and reg_pid wrote")
print("   the new window's pid at launch - so the newcomer wins the moment")
print("   it registers, and the one it replaces can never take it back.")
_NEWCHAN74 = 741003
check("until the new channel registers the seat is still the old one's",
      (daemon.channel_for(_p74, "executor") or {}).get("pid"), _OLDCHAN74)
post("/channel/register", {"project": _p74, "role": "executor",
                           "port": open_channel(_p74, "executor"),
                           "pid": _NEWCHAN74, "ppid": _newwin74,
                           "session_id": "sw74-ex2"}, secret=True)
check("the new window's own channel takes it at once",
      (daemon.channel_for(_p74, "executor") or {}).get("pid"), _NEWCHAN74)
post("/channel/register", {"project": _p74, "role": "executor",
                           "port": _export74, "pid": _OLDCHAN74,
                           "ppid": _OLDWIN74, "session_id": "sw74-ex"},
     secret=True)
check("and the replaced window's heartbeat may not take it back",
      (daemon.channel_for(_p74, "executor") or {}).get("pid"), _NEWCHAN74)
# The record is now KEYED by the contender rather than holding one at a
# time, so this asserts the same thing one layer in. The shape changed
# because the old one could not survive two channels alternating: any
# winning registration popped the whole record, so the streak was wiped
# every few seconds and the five-refusal warning of 5.43 had never once
# fired. -> DECISIONS.md 8.16
check("the refusal is on record, against the contender that made it",
      sorted(((daemon.STATE.get("chan_refused") or {})
              .get("%s|executor" % _k74) or {})), [str(_OLDCHAN74)])

print("   AND THE SEAT HOLDER'S OWN HEARTBEAT MUST NOT WIPE IT. Found by")
print("   SABOTAGE on 2026-09-05, not by running the suite: with the whole")
print("   record popped again - the exact pre-c1539ec line - test_multipair")
print("   stayed GREEN. The shape was asserted (the key above) and the")
print("   BEHAVIOUR it was changed for was not, so the fix had no gate at")
print("   all (rule 24) and no check here could have failed (rule 19).")
print("   channel.py re-registers every 45 s, so a winning registration")
print("   lands between two refusals; live, 2026-09-05, refusal 15:23:40")
print("   and win 15:23:49, 17 refusal lines that day and ZERO entries in")
print("   the state. -> DECISIONS.md 8.16, 8.17")
_n74 = (((daemon.STATE.get("chan_refused") or {})
         .get("%s|executor" % _k74) or {}).get(str(_OLDCHAN74)) or {}).get("n")
post("/channel/register", {"project": _p74, "role": "executor",
                           "port": _export74, "pid": _NEWCHAN74,
                           "ppid": _newwin74, "session_id": "sw74-ex2"},
     secret=True)
check("the holder's heartbeat keeps the seat",
      (daemon.channel_for(_p74, "executor") or {}).get("pid"), _NEWCHAN74)
check("and the OTHER contender's streak survives that win",
      sorted(((daemon.STATE.get("chan_refused") or {})
              .get("%s|executor" % _k74) or {})), [str(_OLDCHAN74)])
check("with the count it had before that win, which is what the warning "
      "of 5.43 needs to reach its threshold at all",
      (((daemon.STATE.get("chan_refused") or {})
        .get("%s|executor" % _k74) or {}).get(str(_OLDCHAN74)) or {})
      .get("n"), _n74)
check("and that count is a real one, not two Nones matching",
      bool(_n74), True)

print("   (b) and nothing at all is delivered into that minute")
print("   the seat above is only half of it: between the launch and the")
print("   registration the record still names the window being replaced,")
print("   and work put there goes with it.")
check("handover_swapping says the swap is open",
      _swapping74(_p74, "executor"), True)
check("so a delivery is refused, in the same word an absent channel gets",
      daemon.deliver_ex(_p74, "executor", "in the swap minute",
                        {"kind": "task"}), (False, "absent"))
DELIVERED[(_k74, "executor")] = []
post("/task", {"project": _p74,
               "instructions": "a task that arrives mid-swap"}, secret=True)
check("a real task through the real endpoint never lands",
      until(lambda: bool(_j74("never reached the executor")), 30), True)
check("and the window being replaced received nothing",
      DELIVERED.get((_k74, "executor")), [])
check("it went to the inbox instead, which is where work waits",
      bool(_j74("Written to")), True)
print("   CONTROL: the half that is NOT being swapped is untouched")
check("the planner still takes a delivery",
      daemon.deliver_ex(_p74, "planner", "a note for the planner",
                        {"kind": "info"})[0], True)

print("   (c) and the old window's channel may not vouch for the new one")
print("   5.44 was about a CORPSE's channel answering for 45 s. This is")
print("   worse: the window is genuinely alive and answers for as long as")
print("   the swap takes. ensure_record's second gate - a live record")
print("   already exists - hides the guard here, so the records are retired")
print("   first: that is the state a bridge restarted mid-swap is in, and")
print("   the state the guard is for.")
daemon.retire_sessions(_p74, "executor")
check("handover_awaits still says this role is being swapped",
      daemon.handover_awaits(_p74, "executor"), True)
check("so a channel that answers writes no record",
      daemon.ensure_record(_p74, "executor", daemon.WITNESS_CHANNEL), False)
check("and none appeared",
      # THIS PAIR's key, not any: ensure_record keys the record by
      # pair_id, and several projects in this suite already carry one -
      # a bare startswith() is green on somebody else's record and red
      # for a reason that has nothing to do with the swap.
      "executor:seen:%s" % daemon.pair_id(_p74)
      in (daemon.STATE.get("sessions") or {}), False)
print("   CONTROL: the planner is not being replaced, so its channel does")
daemon.retire_sessions(_p74, "planner")
check("it writes one",
      daemon.ensure_record(_p74, "planner", daemon.WITNESS_CHANNEL), True)

print("   the replacement reports for duty - and NOW the old one goes")
_stopped74[:] = []
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "sw74-ex2", "project_dir": _p74,
                   "cwd": _p74})
check("the old window is stopped, once, by pid",
      until(lambda: _stopped74 == [("executor", _OLDWIN74)], 20), True)
# WAIT ON THE LINE THIS CHECK READS, not on the one before it. The
# fixture records sessions.stop from inside stop_the_replaced, and the
# journal line comes several statements later - after retire_sessions and
# prune_sessions, both of which touch disk. So the `until` above returns
# while the line is still unwritten, and this check went red once in three
# runs for nothing in the product. Waiting on one fact and reading another
# is the same defect this suite tests the daemon for.


def _ordered74():
    o = _j74("opening the new executor BEFORE stopping the old one")
    s = _j74("was stopped - in that order")
    return bool(o) and bool(s) and o[0]["n"] < s[0]["n"]


check("and the journal has the two lines in that order",
      until(_ordered74, 20), True)
check("the swap is over", _swapping74(_p74, "executor"), False)
check("and the new session was not retired along with the old window",
      _live74(), ["sw74-ex2"])

print("   CONTROL, AND THE WHOLE REASON FOR THE ORDER: the new window never")
print("   comes up. Under the old order that is the six hours; under this")
print("   one the old window was never touched, so the pair loses nothing.")
_p74b = os.path.join(TMP, "swap-never-came-up")
os.makedirs(_p74b, exist_ok=True)
_k74b = canon(_p74b)
_OLDWIN74B = 742001
post("/config", {"projects": {A: {}, B: {}, C: {}, _p74: {}, _p74b: {}}})
post_rc("/loop", {"action": "start", "project": _p74b})
for _r, _s in (("executor", "nc74-ex"), ("planner", "nc74-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p74b, "cwd": _p74b})
    register(_p74b, _r, _s)
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % _k74b] = {
        "pid": _OLDWIN74B, "at": time.time() - 3600, "registered": True,
        "registered_via": "session"}
    daemon.save_state()
# The console is stubbed for the reason case 73 stubs it: this window is
# about to sit unregistered past WINDOW_ASK_SEC, and the real reader
# attaches to a console - which here would be the suite's own.
_scr74 = daemon.sessions.console_screen
_ans74 = daemon.sessions.console_answer
daemon.sessions.console_screen = lambda pid, timeout=20: ""
daemon.sessions.console_answer = lambda pid, timeout=20: False
_stopped74[:] = []
_r74b = daemon.handover(_p74b, "five compactions", ("executor",))
check("the handover started", _r74b.get("ok"), True)
_new74b = ((daemon.STATE.get("pids") or {})
           .get("%s|executor" % _k74b) or {}).get("pid")
with daemon._lock:
    daemon.STATE["started_at"] = time.time() - 9999
    daemon.save_state()
# ITS AGE MOVES BY THE DAEMON'S CLOCK (8.46): this put the newcomer's
# record 700 s back over a process born a moment ago. reg_pid's time
# stands; startup_grace drops to 2 s for the tick and the record grows 2 s
# old for real.
_grace74 = daemon.CFG["thresholds"].get("startup_grace")
daemon.CFG["thresholds"]["startup_grace"] = 2
_at74 = float(((daemon.STATE.get("pids") or {}).get(
    "%s|executor" % _k74b) or {}).get("at") or 0)
until(lambda: time.time() - _at74 > 2.05, 5)
daemon.check_sessions(0)
if _grace74 is None:
    daemon.CFG["thresholds"].pop("startup_grace", None)
else:
    daemon.CFG["thresholds"]["startup_grace"] = _grace74
check("the one that never came up is the one that is closed",
      _stopped74, [("executor", _new74b)])
check("the old window was never touched",
      any(p == _OLDWIN74B for _, p in _stopped74), False)
check("its record is back, exactly as it was",
      ((daemon.STATE.get("pids") or {})
       .get("%s|executor" % _k74b) or {}).get("pid"), _OLDWIN74B)
check("the handover is not left half-open",
      bool((daemon.STATE.get("handover") or {}).get(_k74b)), False)
check("the journal says the old one is still working",
      bool(_j74("was never stopped and is still working", _p74b)), True)
daemon.check_sessions(0)
daemon.check_sessions(0)
check("and says it ONCE, not once a tick",
      len(_j74("was never stopped and is still working", _p74b)), 1)

daemon.sessions.stop = _stop74
daemon.sessions.pid_alive = _alive74
daemon.sessions.console_screen = _scr74
daemon.sessions.console_answer = _ans74
post("/loop", {"project": _p74, "action": "stop"}, secret=True)
post("/loop", {"project": _p74b, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n75. the planner may not assert the state of a pair having opened nothing")
print("    2026-09-04: the bridge's own planner told the owner that a")
print("    watched project's executor 'lost nothing but its context',")
print("    without opening that")
print("    project's handoff file. The file was on disk and its mtime - 15:00")
print("    the day before - refuted the sentence by itself. What the claim")
print("    stood on was the bridge's own line 'a fresh session with the")
print("    handoff is what comes next', which is rule 30 turned on us: the")
print("    event produced its own witness.")
print("    THE OWNER ASKED FOR A STRUCTURE, NOT A PROMISE - so this is a")
print("    gate on the planner's Stop hook, not a sentence in the canon.")

_p75 = os.path.join(TMP, "claim-gate")
os.makedirs(_p75, exist_ok=True)
_k75 = canon(_p75)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p75: {}}})
post_rc("/loop", {"action": "start", "project": _p75})
for _r, _s in (("executor", "cg75-ex"), ("planner", "cg75-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p75, "cwd": _p75})
    register(_p75, _r, _s)


def _j75(sub):
    """This project's own journal lines containing `sub`, in order."""
    return _j74(sub, _p75)


# THE WORDS ARE NOT WRITTEN HERE. They are Russian, this suite is published,
# and check_public refuses Cyrillic in a published file whether it is
# written as characters or as \uXXXX escapes - the same split as case 47 and
# test_handover's case 104. They are read from the loaded list instead,
# which also makes the empty-list contract below testable rather than
# assumed.
_marks75 = list(getattr(daemon, "CLAIM_MARKS", []) or [])
_free75 = list(getattr(daemon, "CLAIM_NOT_CHECKED", []) or [])
# READ FROM THE FILE when the constant is not there, so this case can be
# run against the code it repairs and go RED rather than skip itself. With
# the words in hand every check below drives a real Stop through the real
# endpoint and fails on the answer, which is what red-first means.
_hf75 = getattr(daemon, "HINTS_FILE", "")
_onfile75 = {}
if _hf75 and os.path.isfile(_hf75):
    try:
        _onfile75 = (json.loads(read_or_fail(_hf75, "the hint file"))
                     or {}).get("claims") or {}
    except ValueError:
        _onfile75 = {}
check("the marker list loaded into the daemon",
      bool(_marks75), bool(_onfile75.get("marks")))
check("and so did the words that mark a sentence unchecked",
      bool(_free75), bool(_onfile75.get("not_checked")))
_marks75 = _marks75 or list(_onfile75.get("marks") or [])
_free75 = _free75 or list(_onfile75.get("not_checked") or [])
_witness75 = getattr(daemon, "claim_witness", lambda p, r: (0, []))

if not _marks75:
    # THE PUBLIC CHECKOUT, and it is a contract rather than a hole: with no
    # hints.local.json there are no markers, and a gate that cannot fire
    # must be SAID not to fire rather than quietly pass for a working one.
    print("   (no marker list - this is a public checkout; the gate is off")
    print("    by design, and that is what is asserted here)")
    check("with no list the gate never fires",
          getattr(daemon, "claim_gate",
                  lambda p, r, m: (True,))(_p75, "planner", "x")[0], True)
else:
    _claim75 = "The pair is fine: %s. Nothing else to report." % _marks75[0]

    def _stop75(sid, msg, role="planner"):
        return post_rc("/event", {"hook_event_name": "Stop", "role": role,
                                  "session_id": sid, "project_dir": _p75,
                                  "cwd": _p75,
                                  "last_assistant_message": msg})

    def _opened75(sid, tool="Read", what="C:/x/y.md"):
        # THE PAYLOAD THE CLIENT REALLY SENDS, recorded 2026-09-05 from a
        # headless `claude -p` in plan mode against a recorder of our own:
        # the four keys this case used to send plus prompt_id,
        # permission_mode, effort, tool_use_id and transcript_path. None of
        # them changes the answer, and that is the point - the case is now
        # driven by the shape that arrives rather than by a shape written
        # here. -> DECISIONS.md 8.10 part I, where it is written up
        return post_rc("/event", {"hook_event_name": "PreToolUse",
                                  "role": "planner", "session_id": sid,
                                  "project_dir": _p75, "cwd": _p75,
                                  "transcript_path": "",
                                  "prompt_id": "p-%s" % sid,
                                  "permission_mode": "plan",
                                  "effort": {"level": "high"},
                                  "tool_use_id": "toolu_%s" % sid,
                                  "tool_name": tool,
                                  "tool_input": {"file_path": what}})

    print("   the turn that asserts and opened nothing does not close")
    _code, _r75 = _stop75("cg75-pl", _claim75)
    _out75 = (_r75 or {}).get("hook_output") or {}
    check("the Stop hook is blocked", _out75.get("decision"), "block")
    check("and the reason names the marker it fired on",
          _marks75[0] in (_out75.get("reason") or ""), True)
    check("and both ways out, so it is not a dead end",
          all(w in (_out75.get("reason") or "").lower()
              for w in ("open", "unchecked")), True)
    note("the reason", (_out75.get("reason") or "")[:150])
    check("the journal says what was said and what was opened",
          bool(_j75("planner said:")), True)
    check("and that this one was blocked",
          "BLOCKED" in ((_j75("planner said:") or [{}])[-1].get("text") or ""),
          True)

    print("   CONTROL: the same sentence with the witness opened first")
    _opened75("cg75-pl", "Read", os.path.join(_p75, "handoff.md"))
    check("the registry has it", _witness75(_p75, "planner")[0], 1)
    _code, _r75 = _stop75("cg75-pl", _claim75)
    check("the turn closes", ((_r75 or {}).get("hook_output") or {})
          .get("decision"), None)
    _line75 = (_j75("planner said:") or [{}])[-1].get("text") or ""
    check("and the journal names what was opened", "opened: 1" in _line75,
          True)
    note("the journal line", _line75[:160])

    print("   CONTROL: the registry belongs to ONE turn and is dropped at it")
    check("nothing carried over from the turn before",
          _witness75(_p75, "planner")[0], 0)
    _code, _r75 = _stop75("cg75-pl", _claim75)
    check("so the very same sentence is blocked again",
          ((_r75 or {}).get("hook_output") or {}).get("decision"), "block")

    print("   CONTROL: the honest mark is an exit, and a visible one")
    _code, _r75 = _stop75("cg75-pl", "%s (%s)" % (_claim75, _free75[0]))
    check("a sentence marked unchecked closes the turn",
          ((_r75 or {}).get("hook_output") or {}).get("decision"), None)

    print("   EVERY MARKER ON THE FILE, ALONE. The list is measured, and a")
    print("   marker the daemon does not carry is a measurement thrown away:")
    print("   the two added 2026-09-12 were the only two hits of the class")
    print("   in 409 real turns. The daemon's list must be the file's list,")
    print("   and each entry must fire on its own with nothing opened.")
    check("the daemon carries exactly the file's list, in order",
          list(getattr(daemon, "CLAIM_MARKS", []) or []),
          list(_onfile75.get("marks") or []))
    _fired75 = []
    for _i, _mk in enumerate(_onfile75.get("marks") or []):
        _code, _r = _stop75("cg75-each-%d" % _i,
                            "Status for you: %s. That is all." % _mk)
        _fired75.append(((_r or {}).get("hook_output") or {})
                        .get("decision") == "block")
    check("every marker on the file fires alone (count of the ones that "
          "did not)", _fired75.count(False), 0)
    check("and that is a real count, not an empty loop",
          len(_fired75) >= 2, True)

    print("   CONTROL: a marker INSIDE a longer word is not a marker")
    print("   S5.35: a plain `in` read a flat denial as a question and rang")
    print("   a person 45 times in one day. hint_hit wants a whole word, and")
    print("   without this check nothing here could tell the two apart - the")
    print("   sabotage `substring-match` reddened not one line.")
    # Built from the marker itself rather than written out: the suite is
    # published and carries no Cyrillic. Gluing one of the marker's own
    # letters on either end makes a longer word that `in` still finds and
    # a whole-word match does not.
    _glued75 = "%s%s" % (_marks75[0], _marks75[0][0])
    _code, _r75 = _stop75("cg75-pl", "All well: %s. Carry on." % _glued75)
    check("a marker with a letter glued to its end does not fire",
          ((_r75 or {}).get("hook_output") or {}).get("decision"), None)
    _glued75 = "%s%s" % (_marks75[0][-1], _marks75[0])
    _code, _r75 = _stop75("cg75-pl", "All well: %s. Carry on." % _glued75)
    check("nor one with a letter glued to its front",
          ((_r75 or {}).get("hook_output") or {}).get("decision"), None)

    print("   CONTROL: no marker, no gate - it catches a class, not all")
    _code, _r75 = _stop75("cg75-pl", "Report accepted. Next piece is X4.")
    check("an ordinary turn is untouched",
          ((_r75 or {}).get("hook_output") or {}).get("decision"), None)

    print("   AND THE LINE MUST REPORT THE REGISTRY, NOT THE GATE'S RETURN.")
    print("   claim_gate answers (ok, why, mark, n, first) and until")
    print("   2026-09-05 it read the registry ONLY on the paths where a")
    print("   marker had fired; every other exit returned a literal 0, and")
    print("   the journal printed that as `opened: 0`. So the one record a")
    print("   person can audit said the planner had opened nothing on 61")
    print("   turns out of 61, while its own transcript showed 44 Reads,")
    print("   Greps and Globs inside those same turns. It was read as a")
    print("   broken registry and reported as one. A number computed on")
    print("   only some paths is a number nobody can read.")
    print("   -> DECISIONS.md 8.10")
    _opened75("cg75-pl", "Read", os.path.join(_p75, "audit.md"))
    check("the registry took it", _witness75(_p75, "planner")[0], 1)
    _code, _r75 = _stop75("cg75-pl", "Report accepted. Next piece is X4.")
    _line75 = (_j75("planner said:") or [{}])[-1].get("text") or ""
    check("an unmarked turn still says what it opened",
          "opened: 1" in _line75, True)
    note("the line", _line75[:160])

    print("   CONTROL: a turn that opened nothing still says zero, so the")
    print("   number is a reading and not a constant the other way round")
    _code, _r75 = _stop75("cg75-pl", "Report accepted. Next piece is X5.")
    _line75 = (_j75("planner said:") or [{}])[-1].get("text") or ""
    check("nothing opened, nothing claimed", "opened: 0" in _line75, True)

    print("   CONTROL: the executor has its own gates and is not this one's")
    _code, _r75 = _stop75("cg75-ex", _claim75, role="executor")
    check("the same words from the executor are not blocked here",
          ((_r75 or {}).get("hook_output") or {}).get("decision"), None)

    print("   CONTROL: a reading in ANOTHER project vouches for nothing")
    _opened75("cg75-pl", "Read", os.path.join(_p75, "again.md"))
    post_rc("/event", {"hook_event_name": "Stop", "role": "planner",
                       "session_id": "pl-a", "project_dir": A, "cwd": A,
                       "last_assistant_message": "unrelated"})
    check("this pair's registry survived the other pair's Stop",
          _witness75(_p75, "planner")[0], 1)
    check("and the other pair's own registry is empty",
          _witness75(A, "planner")[0], 0)
    _stop75("cg75-pl", _claim75)

check("the registry is in the inventory, so it moves and is dropped with "
      "the project", "claim_witness" in daemon.STATE_PATHS, True)
post("/loop", {"project": _p75, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n76. a handover record that was dropped must not come back")
print("    X1b's race, found by test_wake_sim seed 1 on 2026-09-04 AFTER the")
print("    same seed had passed once. handover() wrote stop_after after the")
print("    launch with `.get(path) or {}` and an unconditional store-back, so")
print("    when the replacement registered first - which is what a window")
print("    that comes up quickly does - resume_after_handover had already")
print("    dropped the record and that line put a NEW one back holding only")
print("    stop_after and old_recs: no waiting, no roles, no at.")
print("    WHAT IT COSTS: situation()['handover'] is True for ever, and")
print("    clinch(), stalled() and assess() all stand down on it - the whole")
print("    three-tier watchdog off for that project (5.24's class) - and")
print("    stop_the_replaced had already run and found no pid, so the window")
print("    being replaced is never stopped at all.")
print("    NO SLEEP ANYWHERE HERE: the order is forced with an Event, so the")
print("    case proves the order rather than winning a race.")

_p76 = os.path.join(TMP, "handover-resurrect")
os.makedirs(_p76, exist_ok=True)
_k76 = canon(_p76)
_OLDWIN76 = 761001
post("/config", {"projects": {A: {}, B: {}, C: {}, _p76: {}}})
post_rc("/loop", {"action": "start", "project": _p76})
for _r, _s in (("executor", "hr76-ex"), ("planner", "hr76-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p76, "cwd": _p76})
    register(_p76, _r, _s)
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % _k76] = {
        "pid": _OLDWIN76, "at": time.time() - 3600, "registered": True,
        "registered_via": "session"}
    daemon.save_state()

_stopped76 = []
_stop76o, _alive76o = daemon.sessions.stop, daemon.sessions.pid_alive
_launch76o = daemon.sessions.launch
_inlaunch76 = threading.Event()
_release76 = threading.Event()


def _stop76(project, role, pid=None, wait=None, tree=True):
    _stopped76.append((role, pid))
    _gone76.add(int(pid or 0))
    return True


# THE OLD WINDOW IS ALIVE UNTIL IT IS STOPPED, as 74's stub says: the case
# asserts it is stopped "by the pid that was written down", and since 8.36
# a handover does not start over a window it cannot find alive while the
# half's channel names none. -> DECISIONS.md 8.36
_gone76 = set()


def _launch76(project, role, **kw):
    """Hold handover() inside the launch, so the replacement can register
    while it is still there. This is the window the race lives in, and an
    Event is what makes it a fact instead of a coin toss."""
    _inlaunch76.set()
    _release76.wait(30)
    return 762002


daemon.sessions.stop = _stop76
daemon.sessions.pid_alive = (lambda pid: int(pid or 0) == _OLDWIN76
                             and int(pid or 0) not in _gone76)
daemon.sessions.launch = _launch76

_r76 = {}
_t76 = threading.Thread(
    target=lambda: _r76.setdefault("out",
                                   daemon.handover(_p76, "the race",
                                                   ("executor",))),
    name="handover:case76", daemon=True)
_t76.start()
check("handover reached the launch", _inlaunch76.wait(30), True)
check("and wrote down what to stop BEFORE it got there",
      sorted(((daemon.STATE.get("handover") or {}).get(_k76) or {})
             .get("stop_after") or {}), ["executor"])

print("   the replacement registers while handover() is still launching")
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "hr76-ex2", "project_dir": _p76,
                   "cwd": _p76})
check("so the old window is stopped, by the pid that was written down",
      until(lambda: _stopped76 == [("executor", _OLDWIN76)], 20), True)
# WAITED ON, not asserted the instant the stop is seen. resume_after_handover
# stops the old window and THEN drops the record, so the two are not one
# moment - and a negative check placed between them is green or red by
# timing. It passed for two acceptance runs and failed on the third, which
# is the only warning this class ever gives.
check("and the handover record is gone",
      until(lambda: not (daemon.STATE.get("handover") or {}).get(_k76), 20),
      True)

print("   and now handover() finishes - it must not put the record back")
_release76.set()
_t76.join(30)
check("the handover thread finished", _t76.is_alive(), False)
check("the record STAYS gone", (daemon.STATE.get("handover") or {})
      .get(_k76), None)
check("so the pair does not read as mid-handover for ever",
      daemon.situation(_p76).get("handover"), False)
print("   which is what keeps the watchdog armed: all three tiers stand")
print("   down on sit['handover'], so a record nothing clears is all three")
print("   of them off at once")
check("and tier 1 is not standing down on a handover that is not happening",
      "handover" in ((daemon.clinch(_p76, daemon.situation(_p76))
                     or {}).get("why") or ""), False)

daemon.sessions.stop = _stop76o
daemon.sessions.pid_alive = _alive76o
daemon.sessions.launch = _launch76o
post("/loop", {"project": _p76, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n77. an executor that died is raised without asking, and the planner")
print("    writes its handoff from the journal while it starts up")
print("    The owner, 2026-09-04: if the executor fell and did not write a")
print("    handoff, it must be raised in a new window WITHOUT QUESTIONS, its")
print("    init runs, and the planner meanwhile writes it a handoff from the")
print("    log and checks that nothing was lost.")
print("    WHAT WAS THERE: the automatic restart sat behind a project key")
print("    that is in no defaults and in no project's config, so it had")
print("    never run once - a dead window rang a person and waited.")

_p77 = os.path.join(TMP, "died-no-handoff")
os.makedirs(_p77, exist_ok=True)
_k77 = canon(_p77)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p77: {}}})
post_rc("/loop", {"action": "start", "project": _p77})
for _r, _s in (("executor", "dn77-ex"), ("planner", "dn77-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p77, "cwd": _p77})
    register(_p77, _r, _s)
DELIVERED[(_k77, "planner")] = []
DELIVERED[(_k77, "executor")] = []


def _j77(sub):
    return _j74(sub, _p77)


_launched77 = []
_l77o = daemon.sessions.launch
_alive77o = daemon.sessions.pid_alive


def _launch77(project, role, **kw):
    _launched77.append({"role": role, "resume": kw.get("resume_id"),
                        "prompt": kw.get("prompt")})
    return 771001


daemon.sessions.launch = _launch77
daemon.sessions.pid_alive = lambda pid: False

# The real path: the record the process watch hands to handle_session_death
# when a window's process is gone. Not a hand-built STATE snapshot - the
# same call the watcher makes, with the same record (5.33).
_rec77 = daemon.best_session(_p77, "executor") or {}
daemon.handle_session_death(_p77, "executor", _rec77)

check("a window was raised for it, without anybody being asked",
      until(lambda: [l for l in _launched77 if l["role"] == "executor"], 20),
      True)
# GUARDED, because everything below reads it. Under the sabotage that
# takes the default away - and on the code before this piece - no window
# is raised at all, and [-1] on an empty list raises and takes the rest of
# the file with it, in silence. That is the class CLAUDE.md names twice
# and it was written here anyway; one FAIL per claim instead.
_l77 = ([l for l in _launched77 if l["role"] == "executor"] or
        [{"resume": "no window was raised", "prompt": ""}])[-1]
check("and it is a NEW session, not the dead one resumed",
      _l77["resume"], None)
check("whose first act is /init, as the client's own prompt argument",
      _l77["prompt"], getattr(daemon, "INIT_PROMPT", None))
check("the bridge knows an init is running, so work can wait for it",
      bool(getattr(daemon, "init_pending", lambda p: {})(_p77)), True)

print("   the planner is told in the same moment, WITH THE MTIME IN IT")
check("a message reached the planner",
      until(lambda: bool(DELIVERED.get((_k77, "planner"))), 20), True)
_msg77 = " ".join(body_of(d.get("content") or "")
                  for d in DELIVERED.get((_k77, "planner"), []))
check("it asks for the handoff to be written from the journal",
      "WRITE IT A HANDOFF FROM THE JOURNAL" in _msg77, True)
check("it names the dialogue and the index to build it from",
      ("dialogue.md" in _msg77 and "INDEX.md" in _msg77), True)
check("it says what state the handoff file is actually in, not that it is "
      "fine", ("does not exist" in _msg77 or "has not changed since" in _msg77
               or "was written at" in _msg77), True)
check("and it asks for the tasks to be checked, by name",
      "NOT ONE TASK IS LOST" in _msg77, True)
note("what the planner was told", _msg77[:200])

print("   the handoff the planner then sends is HELD until /init is over")
DELIVERED[(_k77, "executor")] = []
_hold77 = post("/task", {"project": _p77,
                         "instructions": "HANDOFF FROM THE JOURNAL: "
                                         "you were on piece 5"}, secret=True)
check("the executor is given nothing while it is still starting up",
      until(lambda: bool(DELIVERED.get((_k77, "executor"))), 6), False)
check("but the bridge is holding it, not dropping it",
      bool((daemon.STATE.get("after_init") or {}).get(_k77)), True)

print("   ...and handed over in ONE message the moment the init ends")
post_rc("/event", {"hook_event_name": "Stop", "role": "executor",
                   "session_id": "dn77-ex2", "project_dir": _p77,
                   "cwd": _p77,
                   "last_assistant_message": "init done"})
check("it arrives after the first Stop",
      until(lambda: any("HANDOFF FROM THE JOURNAL" in (d.get("content") or "")
                        for d in DELIVERED.get((_k77, "executor"), [])), 25),
      True)
check("in one delivery, not several",
      len([d for d in DELIVERED.get((_k77, "executor"), [])
           if "HANDOFF FROM THE JOURNAL" in (d.get("content") or "")]), 1)
check("and the journal says so, in those words",
      bool(_j77("handed to the new executor after its init")), True)
check("the hold is over", bool((daemon.STATE.get("after_init") or {})
                               .get(_k77)), False)

print("   CONTROL: with no init pending, a task goes straight through")
DELIVERED[(_k77, "executor")] = []
post("/task", {"project": _p77, "instructions": "an ordinary next piece"},
     secret=True)
check("delivered without being held",
      until(lambda: any("an ordinary next piece" in (d.get("content") or "")
                        for d in DELIVERED.get((_k77, "executor"), [])), 25),
      True)

print("   CONTROL: an /init that never ends does not strand the handoff")
print("   held is only different from dropped if something releases it -")
print("   the same argument HELD_VERDICT_MAX_SEC exists for")
DELIVERED[(_k77, "executor")] = []
# GUARDED like every other name this piece introduces: on the code before
# V none of these exist, and a bare attribute access raises and takes the
# rest of the file with it. The control then fails on its own line, which
# is what a reader can act on.
_note77 = getattr(daemon, "note_init_pending", lambda p, s: None)
_strand77 = getattr(daemon, "release_stranded_init_holds", lambda: None)
_hold_max77 = getattr(daemon, "INIT_HOLD_MAX_SEC", 3600)
_note77(_p77, "never-comes-up-77")
post("/task", {"project": _p77, "instructions": "stranded by a dead init"},
     secret=True)
check("it is held first",
      until(lambda: bool((daemon.STATE.get("after_init") or {}).get(_k77)),
            20), True)
with daemon._lock:
    _rec77 = (daemon.STATE.get("init_pending") or {}).get(_k77)
    if _rec77:
        _rec77["at"] = time.time() - _hold_max77 - 5
    daemon.save_state()
_strand77()
check("and handed over anyway once the ceiling passes",
      until(lambda: any("stranded by a dead init" in (d.get("content") or "")
                        for d in DELIVERED.get((_k77, "executor"), [])), 20),
      True)
check("with the journal saying why, not silently",
      bool(_j77("has not finished in")), True)
check("and nothing left holding", bool((daemon.STATE.get("after_init") or {})
                                       .get(_k77)), False)

print("   CONTROL: the two containers are in the inventory, so they move")
print("   with a project and are dropped with it")
check("both named", [k for k in ("init_pending", "after_init")
                     if k in daemon.STATE_PATHS],
      ["init_pending", "after_init"])

daemon.sessions.launch = _l77o
daemon.sessions.pid_alive = _alive77o
post("/loop", {"project": _p77, "action": "stop"}, secret=True)
post("/config", {"projects": {A: {}, B: {}, C: {}}})

# install is not imported at the top of this file, and daemon does not
# re-export it - it imports it lazily, inside the functions that need
# it - so it is imported here, where it is used.
from bridgecore import install as _inst78              # noqa: E402
import io as _io78                                    # noqa: E402

print("\n78. a pair that starts blind, and a window that will not answer")
print("    2026-09-04, a project carried from another machine: all eight")
print("    hooks in its .claude/settings.json named an interpreter that is")
print("    not on this one, and for nearly an hour every report the")
print("    executor finished went nowhere. marks_missing recognises a")
print("    bridge hook by its ARGS and never looked at the command, so the")
print("    marks read as whole and the window was launched blind.")

_p78 = os.path.join(TMP, "carried-project")
os.makedirs(os.path.join(_p78, ".claude"), exist_ok=True)
_k78 = canon(_p78)
_DEAD78 = os.path.join(TMP, "no-such-place", "python.exe")


def need78(mod, name, blank):
    """The mechanism under test, or one FAIL and a stand-in for it.

    Every name below is new in this piece, so a package without it is
    exactly what the red-first run has in its hands. Resolving them by
    attribute would end that run in an AttributeError - which reddens
    nothing, says nothing about behaviour, and kills every check after
    it. One FAIL per missing mechanism, then something that answers the
    way a function that does nothing would, so each dependent check
    fails on its own line where a person can read which ones. The same
    principle as read_or_fail.
    """
    fn = getattr(mod, name, None)
    if fn is not None:
        return fn
    check("the package has %s.%s" % (mod.__name__.rsplit(".", 1)[-1], name),
          False, True)
    return lambda *a, **k: blank


_hookcmds78 = need78(_inst78, "hook_commands", [])
_repair78 = need78(_inst78, "repair_hook_python", (None, ""))
_nudgefn78 = need78(daemon, "nudge_deaf_window", "")
_idle78 = need78(daemon, "idle_notice_since", None)


def _settings78(cmd):
    """A settings.json whose bridge hooks name `cmd` - the shape a project
    arrives with when it is copied off another machine."""
    cfg = {"hooks": {ev: [{"hooks": [{"type": "command", "command": cmd,
                                      "args": ["-m", "bridgecore.hook"],
                                      "timeout": 30}]}]
                     for ev in _inst78.EVENTS}}
    _io78.open(os.path.join(_p78, ".claude", "settings.json"), "w",
            encoding="utf-8").write(json.dumps(cfg, indent=2))


def _cmds78():
    c = json.loads(read_or_fail(os.path.join(_p78, ".claude",
                                             "settings.json"),
                                "the project's settings.json") or "{}")
    return sorted(set(_hookcmds78(c)))


_settings78(_DEAD78)
_missing78 = _inst78.marks_missing(_p78)
check("a hook naming an interpreter that is not here is a MISSING mark",
      any(_DEAD78 in m for m in _missing78), True)
check("and the mark names the file it is missing from",
      any("settings.json" in m and _DEAD78 in m for m in _missing78), True)
note("what it says", [m for m in _missing78 if _DEAD78 in m][:1])

print("   and it is REPAIRED, not merely reported")
_was78, _where78 = _repair78(_p78, sys.executable)
check("the dead interpreter is named as what was replaced", _was78, _DEAD78)
check("every bridge hook now points at one that exists",
      _cmds78(), [sys.executable])
check("with the file as it was kept beside it",
      os.path.isfile(os.path.join(_p78, ".claude",
                                  "settings.json.before-bridge-python")),
      True)
check("and the mark is no longer missing",
      any(_DEAD78 in m for m in _inst78.marks_missing(_p78)), False)

print("   AND THE REPAIR IS ON install()'s OWN PATH")
print("   The direct call above proves the function; this proves the")
print("   WIRING, which is the half that was missing on 2026-09-04 -")
print("   ensure_marks runs install() at every launch, and a repair that")
print("   only a test calls would have left the pair just as blind.")
_settings78(_DEAD78)
_inst78.install(_p78, "executor", python=sys.executable, statusline=False)
check("install repaired the dead interpreter on its way in",
      _cmds78(), [sys.executable])
check("and the mark it merged is usable",
      any(_DEAD78 in m for m in _inst78.marks_missing(_p78)), False)

print("   CONTROL: an interpreter that IS here is not touched")
_settings78(sys.executable)
check("nothing was replaced",
      _repair78(_p78, sys.executable)[0], None)
check("and the command is the one that was there", _cmds78(),
      [sys.executable])
print("   CONTROL: `py` is a NAME, not a path, and is left alone")
print("   install writes it deliberately for a machine where the absolute")
print("   path moves; resolving it the way the OS would is the whole test")
_settings78("py")
check("py is not called missing",
      any("py" in m and "settings.json: the bridge hooks" in m
          for m in _inst78.marks_missing(_p78)), False)
check("and py is not rewritten",
      _repair78(_p78, sys.executable)[0], None)

print("\n   (b) A WINDOW THAT TOOK A DELIVERY AND OPENED NO TURN")
print("   Measured first, and the measurement changed the design: across 28")
print("   long silences an idle notice was present before 8 of them, so its")
print("   ABSENCE is not a condition of anything - it is recorded and")
print("   reported. What decides is the screen, which is the only witness")
print("   that can tell a stalled client from a busy one.")

_p78b = os.path.join(TMP, "deaf-window")
os.makedirs(_p78b, exist_ok=True)
_k78b = canon(_p78b)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p78b: {}}})
post_rc("/loop", {"action": "start", "project": _p78b})
for _r, _s in (("executor", "dw78-ex"), ("planner", "dw78-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _s, "project_dir": _p78b, "cwd": _p78b})
    register(_p78b, _r, _s)
_PID78 = 780001
# A LIVE planner window, as the case says - declared so (8.46). It was an
# invented number no process holds, recorded 600 s back, and its screen
# was read and keyed without the daemon ever asking whether it lived.
DECLARED_ALIVE.add(_PID78)
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|planner" % _k78b] = {
        "pid": _PID78, "at": time.time(), "registered": True,
        "registered_via": "session"}
    daemon.save_state()

_screen78 = {"text": "", "keys": 0}
_scr78, _ans78 = daemon.sessions.console_screen, daemon.sessions.console_answer
daemon.sessions.console_screen = (lambda p, timeout=20:
                                  _screen78["text"] if p == _PID78 else "")


def _key78(p, timeout=20):
    if p == _PID78:
        _screen78["keys"] += 1
    return True


daemon.sessions.console_answer = _key78


def _j78(sub):
    return _j74(sub, _p78b)


try:
    # ONE DELIVERY, ONE TIME, AND THEY GO FORWARD. The latch is keyed on
    # when the delivery happened - that is what makes it one key per
    # delivery rather than one per tick - so a fixture handing later calls
    # EARLIER times is describing deliveries that travelled backwards, and
    # every one of them is refused as "that one again". Mine did, and four
    # of the reds it produced were about the fixture, not the gate.
    _t78 = time.time() - 900

    def _nudge78(screen, when):
        _screen78["text"] = screen
        return _nudgefn78(_p78b, "planner", when, "a report")

    print("   an EMPTY prompt is the one case a key is safe in")
    check("it is nudged", _nudge78("some earlier output\n\n> ", _t78),
          "nudged")
    check("with exactly one key", _screen78["keys"], 1)
    check("and the journal says what was done and why",
          bool(_j78("nudged planner window pid %d with Enter" % _PID78)),
          True)
    # A NUDGE NOBODY MEASURED IS A NUDGE NOBODY CAN JUDGE, and it types
    # into somebody's window - so the thread that reports back a minute
    # later is part of the mechanism, not a nicety. It sleeps
    # NUDGE_TURN_WINDOW_SEC, so it is still alive here; it is a daemon
    # thread and holds nothing up.
    check("and the key is MEASURED - something is watching for the turn",
          any(t.name == "nudge:measure" for t in threading.enumerate()),
          True)
    note("the line", (_j78("nudged planner window") or [{}])[-1]
         .get("text", "")[:150])

    print("   ONE KEY PER DELIVERY, not one per tick")
    check("the same delivery is refused, twice over",
          [_nudge78("some earlier output\n\n> ", _t78),
           _nudge78("some earlier output\n\n> ", _t78)], ["", ""])
    check("and no second key was sent", _screen78["keys"], 1)

    print("   CONTROL: a screen with TYPED TEXT is never touched")
    _screen78["keys"] = 0
    check("it is left alone, and says so",
          _nudge78("some output\n\n> py -m bridgecore.relayout --now",
                   _t78 + 60), "left alone")
    check("no key is sent", _screen78["keys"], 0)
    check("and the screen goes into the journal verbatim",
          bool(_j78("bridgecore.relayout --now")), True)

    print("   CONTROL: a DIALOG is never touched either - Enter would")
    print("   choose whatever happens to be selected, not wake anything")
    _screen78["keys"] = 0
    check("no key is sent to a dialog",
          (_nudge78("Do you trust the files in this folder?\n"
                    "\u276f 1. Yes, proceed\n  2. No, exit", _t78 + 120),
           _screen78["keys"]), ("left alone", 0))

    print("   CONTROL: a numbered choice with no arrow is still a dialog")
    _screen78["keys"] = 0
    check("nor to one without the marker",
          (_nudge78("  1. Yes, proceed\n  2. No, exit", _t78 + 180),
           _screen78["keys"]), ("left alone", 0))

    print("   AND A WINDOW THAT IS WORKING IS NOT AT ITS PROMPT, whatever")
    print("   its box says. The first live key, 2026-09-05 00:30:42, went")
    print("   into a planner whose turn was already open: the box WAS bare,")
    print("   and the client was drawing its spinner three rows above it.")
    print("   The two forms below are taken off live screens rather than")
    print("   invented - the working one ends in an ellipsis and carries a")
    print("   duration in brackets, and its glyph ANIMATES (U+2722, U+273D")
    print("   in two captures six seconds apart), so nothing may key off the")
    print("   glyph; the finished one reads `for <duration> - done <clock>`")
    print("   and is not a spinner at all.")
    print("   -> DECISIONS.md 8.10")
    _screen78["keys"] = 0
    _spin78 = ("some earlier output\n"
               "\u2722 Perusing\u2026 (7m 29s \u00b7 \u2193 8.4k tokens)\n> ")
    check("a window drawing its spinner is not keyed",
          (_nudge78(_spin78, _t78 + 240), _screen78["keys"]), ("working", 0))
    check("and the journal quotes what it saw", bool(_j78("Perusing")), True)
    check("and it does NOT say the window opened no turn",
          any("opened no turn" in (r.get("text") or "")
              for r in (_j78("Perusing") or [])), False)

    print("   CONTROL: the same screen with the OTHER glyph, because the")
    print("   glyph is the one thing on that line that changes by itself")
    _screen78["keys"] = 0
    check("still not keyed",
          (_nudge78(_spin78.replace("\u2722", "\u273d"), _t78 + 300),
           _screen78["keys"]), ("working", 0))

    print("   CONTROL: a FINISHED marker is not a spinner, and a window that")
    print("   has just finished a turn is exactly the one worth keying")
    _screen78["keys"] = 0
    _done78 = ("some earlier output\n"
               "\u273b Churned for 39s \u00b7 done 1:26\n> ")
    check("it is nudged", _nudge78(_done78, _t78 + 360), "nudged")
    check("with one key", _screen78["keys"], 1)

    print("   AND THE SCREEN IS WRITTEN DOWN ON THE KEYING BRANCH TOO. Two")
    print("   keys went into a live window on 2026-09-05 and there is")
    print("   nothing to look at afterwards: only the branch that declined")
    print("   to key ever wrote a screen into the journal.")
    check("the tail of what was keyed is in the journal",
          bool(_j78("Churned for 39s")), True)

    print("   the idle notice is MEASURED, not obeyed")
    check("the bridge can say whether one came, either way",
          _idle78(_p78b, time.time() - 3600) in (True, False), True)
    # AND THE ANSWER IS KEPT. It is asked while the silence is happening
    # because afterwards nobody can recover it, so the record is the whole
    # point of asking - and a record nothing reads back is one a later
    # edit drops without a sound.
    check("and the answer is written into the record",
          "idle_notice" in ((daemon.STATE.get("nudged") or {})
                            .get("%s|planner" % _k78b) or {}), True)
    check("the latch is in the inventory, so it moves and is dropped with "
          "the project", "nudged" in daemon.STATE_PATHS, True)
finally:
    daemon.sessions.console_screen = _scr78
    daemon.sessions.console_answer = _ans78
    DECLARED_ALIVE.discard(_PID78)
    with daemon._lock:
        (daemon.STATE.get("pids") or {}).pop("%s|planner" % _k78b, None)
        daemon.save_state()
    post("/loop", {"project": _p78b, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n79. a suite may not open a real client window")
print("    2026-09-04: the owner saw a heap of Claude Code windows. The")
print("    live daemon had opened none of them - seventeen were sitting")
print("    with the fixture's own launch line, `claude ... --model opus")
print("    /init`, each under a python of mine that had already exited.")
print("    Every suite stubs build_command; the rule was kept by each of")
print("    them REMEMBERING, which is rule 24 exactly. The gate is in")
print("    launch() now, at the one place a process is actually started.")

# NOTHING HERE MAY REALLY SPAWN, not even to prove the old behaviour.
# Popen is recorded instead - so on a package without the gate this case
# shows the launch going through, and on one with it the launch is
# refused, and neither answer costs a window.
_pop79 = daemon.sessions.subprocess.Popen
_spawned79 = []


class _Proc79(object):
    pid = 790001


def _popen79(cmd, **kw):
    """Record a SESSION launch; let everything else really run.

    sessions.subprocess IS the subprocess module, so replacing Popen on it
    replaces it for the whole process - and install(), which launch() calls
    on its way past, runs subprocess.run underneath. Only a command
    carrying the client's own flag is caught.
    """
    c = [cmd] if isinstance(cmd, str) else list(cmd)
    if "--remote-control" not in c:
        return _pop79(cmd, **kw)
    _spawned79.append(c)
    return _Proc79()


_p79 = os.path.join(TMP, "real-client-guard")
os.makedirs(_p79, exist_ok=True)
_build79 = daemon.sessions.build_command
_gate79 = getattr(daemon.sessions, "real_client_refused", None)
if _gate79 is None:
    check("the package has sessions.real_client_refused", False, True)

    def _gate79(cmd):
        return ""

try:
    daemon.sessions.subprocess.Popen = _popen79
    daemon.sessions.build_command = _real_build      # the REAL command line

    print("   the fixture's own launch line is refused, by name")
    _why79 = ""
    try:
        daemon.sessions.launch(_p79, "executor", model="opus", prompt="/init")
    except RuntimeError as exc:
        _why79 = str(exc)
    except Exception as exc:                          # noqa: BLE001
        _why79 = "WRONG KIND: %r" % exc
    check("launch refuses", bool(_why79) and "REAL client" in _why79, True)
    check("and NOTHING was spawned", _spawned79, [])
    check("and it says what to do instead",
          "BRIDGE_REAL_CLIENT" in _why79 and "build_command" in _why79, True)
    note("what it says", _why79[:170])

    print("   CONTROL: a STUBBED command is never refused - the gate looks")
    print("   at the executable, so every suite that stubs is untouched")
    check("the stub goes through", _gate79([sys.executable, "x.py", "-a"]), "")
    daemon.sessions.build_command = _stub_build
    daemon.sessions.launch(_p79, "executor", model="opus", prompt="/init")
    check("and it really launched", len(_spawned79), 1)
    check("with the stub in front", _spawned79[0][0], sys.executable)

    print("   CONTROL: BRIDGE_REAL_CLIENT=1 is the way to mean it")
    _spawned79[:] = []
    daemon.sessions.build_command = _real_build
    os.environ["BRIDGE_REAL_CLIENT"] = "1"
    try:
        daemon.sessions.launch(_p79, "executor", model="opus", prompt="/init")
        _flag79 = ""
    except RuntimeError as exc:
        _flag79 = str(exc)
    finally:
        os.environ.pop("BRIDGE_REAL_CLIENT", None)
    check("with the flag it goes through", _flag79, "")
    check("and that is the ONLY way it does", len(_spawned79), 1)
    check("the real client is what it would have been",
          os.path.splitext(os.path.basename(
              (_spawned79 or [[""]])[0][0]))[0].lower(), "claude")

    print("   CONTROL: the bridge's OWN data folder is never a test")
    # A REAL data folder: derived, never a literal absolute path
    # (check_public refuses those in a published file), and never
    # under the temp folder WHEREVER THIS SUITE IS COPIED TO. The
    # first version of this used the suite's own directory, which
    # is exactly right in the repository and exactly wrong in a
    # sabotage copy - those live under temp, so the gate refused
    # the control and every sabotage file carried a red line about
    # nothing. The drive root is under nothing; the gate compares
    # paths and never opens them, so it need not exist.
    _was79 = os.environ.get("BRIDGE_DATA")
    os.environ["BRIDGE_DATA"] = os.path.join(
        os.path.abspath(os.sep), "bridge-data-not-a-test")
    check("a real data folder is not refused",
          _gate79(["claude", "--remote-control"]), "")
    os.environ["BRIDGE_DATA"] = _was79 or ""
    check("and the temp one is",
          bool(_gate79(["claude", "--remote-control"])), True)
finally:
    daemon.sessions.subprocess.Popen = _pop79
    daemon.sessions.build_command = _build79


print("\n80. four things the bridge said that were not so")
print("    All four were found by reading what the bridge wrote while")
print("    somebody watched it: a clinch counting quiet nobody owed, a")
print("    live window with no record of which half it is, a restart gate")
print("    held by jobs nothing waits on, and a lost report counted by")
print("    nobody. -> DECISIONS.md 8.10 parts D and G")

# relayout is not imported at the top of this file and daemon does not
# re-export it, so it is imported here, where the gate is tested.
from bridgecore import relayout as _relay80             # noqa: E402

_need80 = []


def have80(mod, name, blank):
    """The mechanism, or one FAIL and a stand-in - as case 78 does."""
    fn = getattr(mod, name, None)
    if fn is not None:
        return fn
    check("the package has %s.%s" % (mod.__name__.rsplit(".", 1)[-1], name),
          False, True)
    _need80.append(name)
    return lambda *a, **k: blank


_p80 = os.path.join(TMP, "four-corrections")
os.makedirs(_p80, exist_ok=True)
_k80 = canon(_p80)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p80: {}}})

print("   (a) A CLINCH MAY NOT COUNT QUIET SPENT WITH THE LOOP OFF")
print("   2026-09-04 18:59:59, sixteen seconds after the loop came back:")
print("   'idle with work owed and nothing in flight for 51m' - and for")
print("   fifty-one of those minutes the loop was off, so nothing was owed.")
have80(daemon, "note_loop_started", None)
post_rc("/loop", {"action": "start", "project": _p80})
_, _lp80 = daemon.loop_state(_p80)
check("starting the loop writes down when",
      bool(float((_lp80 or {}).get("started_at") or 0)), True)
# The pair looks deadlocked by every other test: both idle, nothing in
# flight, work owed - and the only thing standing between it and a false
# announcement is that the loop came back on a moment ago.
_sit80 = {"loop": True, "paused": False, "reviewing": False,
          "verdict_in_flight": False, "handover": False, "inflight": 0,
          "roles": {r: {"alive": True, "tail": []} for r in ("executor",
                                                             "planner")}}
_mv80 = daemon.last_movement
_wrote80 = daemon.executor_wrote_recently
daemon.last_movement = lambda p: time.time() - 3600      # an hour of quiet
daemon.executor_wrote_recently = lambda p, q: False
try:
    with daemon._lock:
        _lp80["started_at"] = time.time() - 16           # ...but on for 16 s
        daemon.save_state()
    check("an hour of quiet, the loop on for sixteen seconds: no clinch",
          daemon.clinch(_p80, _sit80, grace=900), None)
    print("   CONTROL: the same quiet with the loop long on IS a clinch -")
    print("   the fix must not have simply switched tier 1 off")
    with daemon._lock:
        _lp80["started_at"] = time.time() - 7200
        daemon.save_state()
    _c80 = daemon.clinch(_p80, _sit80, grace=900)
    check("it still catches a real one", bool(_c80), True)
    note("what it says", (_c80 or {}).get("said"))
    print("   CONTROL: a loop with no stamp at all reads as it always did")
    with daemon._lock:
        _lp80.pop("started_at", None)
        daemon.save_state()
    check("no stamp, no change in behaviour",
          bool(daemon.clinch(_p80, _sit80, grace=900)), True)
finally:
    daemon.last_movement = _mv80
    daemon.executor_wrote_recently = _wrote80

print("   and the stamp is written at EVERY door the loop opens by, not")
print("   only the panel's - a task re-arms it too, and a case that only")
print("   reached one of the three would let a sabotage on the others")
print("   redden nothing (that is how named-not-repaired was found)")
post_rc("/loop", {"action": "stop", "project": _p80})
with daemon._lock:
    _, _lp80b = daemon.loop_state(_p80)
    _lp80b.pop("started_at", None)
    daemon.save_state()
post_rc("/task", {"project": _p80, "instructions": "a small piece of work"})
_, _lp80c = daemon.loop_state(_p80)
check("a task re-arming the loop stamps it too",
      bool(float((_lp80c or {}).get("started_at") or 0)), True)

print("   (b) A SESSION ENDING IS NOT A WINDOW CLOSING")
print("   A carried project's executor was alive with its channel")
print("   answering and no pids record at all - SessionEnd took it at")
print("   17:39:42 and no SessionStart put it back. Every reader of")
print("   pid_of fails its own way on that, one by touching a window")
print("   that is not its own.")
_alive80 = daemon.sessions.pid_alive
_livepids80 = {800001}
daemon.sessions.pid_alive = lambda pid: int(pid) in _livepids80
try:
    for _r, _pid in (("executor", 800001), ("planner", 800002)):
        post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                           "session_id": "fc80-%s" % _r, "project_dir": _p80,
                           "cwd": _p80})
        with daemon._lock:
            daemon.STATE.setdefault("pids", {})["%s|%s" % (_k80, _r)] = {
                "pid": _pid, "at": time.time(), "registered": True,
                "registered_via": "session"}
            daemon.save_state()
    post_rc("/event", {"hook_event_name": "SessionEnd", "role": "executor",
                       "session_id": "fc80-executor", "project_dir": _p80,
                       "cwd": _p80})
    _rec80 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k80) or {}
    check("the window is still known after its session ended",
          _rec80.get("pid"), 800001)
    check("and the record says the session ended",
          bool(_rec80.get("session_ended_at")), True)
    check("so pid_of can still answer", daemon.pid_of(_p80, "executor"),
          800001)
    print("   CONTROL: a window that really is gone is forgotten")
    post_rc("/event", {"hook_event_name": "SessionEnd", "role": "planner",
                       "session_id": "fc80-planner", "project_dir": _p80,
                       "cwd": _p80})
    check("a dead window's record is dropped, as before",
          (daemon.STATE.get("pids") or {}).get("%s|planner" % _k80), None)
finally:
    daemon.sessions.pid_alive = _alive80

print("   (c) THE RESTART GATE COUNTED JOBS NOBODY WAITS ON")
print("   63 of 70 tracked commands were bg: records; the gate refused")
print("   for five minutes, the restart was forced, and the next journal")
print("   line was 'Picked up 70 tracked commands' - it protected nothing.")
# THE REAL SHAPE: the daemon sends the commands and which of them are
# background, because two exclusions over one set are a union and counts
# alone cannot express that.
_bg80 = {"inflight": 3,
         "inflight_cmds": ["py render.py", "sleep 60", "pytest -q"],
         "inflight_bg_cmds": ["sleep 60", "py render.py"]}
check("background jobs do not count as work",
      _relay80.count_inflight(_bg80)[0], 1)
check("and the gate's own command is excluded too, once",
      _relay80.count_inflight({
          "inflight": 3,
          "inflight_cmds": ["nohup py -m bridgecore.relayout --now &",
                            "sleep 60", "pytest -q"],
          "inflight_bg_cmds": ["nohup py -m bridgecore.relayout --now &",
                               "sleep 60"]}),
      (1, 1))
print("   and the DAEMON is the half that can see it: the bg: marker is")
print("   in the record's key, and /state sends only the command strings,")
print("   so without this the gate has nothing to subtract")
with daemon._lock:
    _inf80 = daemon.STATE.setdefault("inflight", {}).setdefault(_k80, {})
    # `bg` on the RECORD is what makes a job a background one - the same
    # field record_expired reads for BG_MAX_SEC. The key's `bg:` prefix
    # never leaves the daemon, which is why the first version of this
    # counted zero every time.
    _inf80["bg:one80"] = {"cmd": "sleep 1", "started": time.time(),
                          "bg": True}
    _inf80["bg:two80"] = {"cmd": "sleep 2", "started": time.time(),
                          "bg": True}
    _inf80["plain80"] = {"cmd": "sleep 3", "started": time.time()}
    daemon.save_state()
try:
    _st80 = get("/state?project=%s" % _p80)
    _b80 = ((_st80.get("pairs") or {}).get(_k80) or {}).get("busy") or {}
    check("the daemon counts the tracked commands", _b80.get("inflight"), 3)
    check("and says how many of them nobody waits on",
          _b80.get("inflight_bg"), 2)
    check("and names them, so the two exclusions can be a union",
          sorted(_b80.get("inflight_bg_cmds") or []), ["sleep 1", "sleep 2"])
    check("so the gate, reading only that, refuses on one command",
          _relay80.count_inflight(_b80)[0], 1)
finally:
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(_k80, None)
        daemon.save_state()

print("   CONTROL: an older daemon sends no such list at all, and then")
print("   nothing is excluded - the behaviour before this piece, which")
print("   errs towards refusing")
check("no command list means nothing subtracted",
      _relay80.count_inflight({"inflight": 3})[0], 3)
print("   CONTROL: a pair held ONLY by background jobs is not a refusal,")
print("   and a real command still is")
check("all background: quiet",
      _relay80.blocking_lines(
          ["P: not counting 5 background command(s) - nothing waits on "
           "those"]), [])
check("a real one still refuses",
      len(_relay80.blocking_lines(["P: 2 tracked command(s) still running"])),
      1)

print("   (d) A REPORT WHOSE WAITER DIES IS COUNTED BY NOBODY")
print("   We forced a restart believing the returning planner would be")
print("   told by the 'N reports had gone unanswered' line. It reads")
print("   STATE['unanswered'], which only note_silence fed - and that runs")
print("   on the review timeout, which a restart never reaches.")
_missed80 = have80(daemon, "note_waiter_lost", None)
with daemon._lock:
    (daemon.STATE.get("unanswered") or {}).pop(_k80, None)
    daemon.save_state()
# THROUGH THE WALK shutdown() CALLS, not through the leaf: a sabotage
# that removes the call has to redden something. PENDING is the daemon's
# own dict, and a waiter in it is exactly what a stop destroys.
_walk80 = have80(daemon, "note_waiters_lost", 0)
daemon.PENDING[_k80] = {"event": threading.Event(), "verdict": None,
                        "feedback": "", "content": "",
                        "meta": {"kind": "report", "report": "42"},
                        "made": time.time()}
try:
    check("the walk counts what the stop is about to destroy",
          _walk80("the bridge was stopped while it was waiting") >= 1, True)
finally:
    daemon.PENDING.pop(_k80, None)
check("the lost report is counted",
      (daemon.STATE.get("unanswered") or {}).get(_k80), 1)
check("and the journal names it and where it is",
      bool(_j74("Report 42 will not be answered", _p80)), True)
note("the line", (_j74("will not be answered", _p80) or [{}])[-1]
     .get("text", "")[:150])
print("   and THAT is what makes the returning planner's line true")
check("clear_silence hands back what was missed",
      daemon.clear_silence(_p80, "Four"), 1)
check("and the counter is spent, not left to double",
      (daemon.STATE.get("unanswered") or {}).get(_k80, 0), 0)
print("   CONTROL: counting a miss must NOT pause the pair - something")
print("   took the waiter away, the planner did not desert it")
check("the pair is not held", bool((daemon.STATE.get("paused") or {})
                                   .get(_k80)), False)

post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n81. the bridge does not run its own suites for somebody else")
print("    2026-09-04, the owner asked why a pair kept hanging.")
print("    Its planner called check on every report and run_check ran THIS")
print("    bridge's acceptance for it, never asking check_kinds(path) -")
print("    empty for that project, and deliberately: these suites test this")
print("    bridge. Seven runs in a day, three to seven minutes each, and")
print("    through every one of them the executor's Stop hook waited for a")
print("    verdict while the planner's turn hung on the tool. Both windows")
print("    look frozen. Report-to-verdict for that pair: median 440 s,")
print("    max 2953 s. -> DECISIONS.md 8.13")

_p81 = os.path.join(TMP, "not-our-project")
os.makedirs(_p81, exist_ok=True)
_k81 = canon(_p81)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p81: {}}})
check("PRECONDITION: this project names no checks",
      daemon.check_kinds(_p81), [])

# WHAT A RUN LOOKS LIKE FROM OUTSIDE: it copies a tree, spawns processes
# and writes a folder. None of those may happen here, and each is watched
# separately - "it was fast" is not the claim, "it did nothing" is.
_copied81 = []
_ran81 = []
_copy81o = daemon._check_copy
_runone81o = daemon._run_one
daemon._check_copy = lambda dst: _copied81.append(dst)
daemon._run_one = lambda *a, **k: (_ran81.append(a[0]), (0, ""))[1]
_t81 = time.time()
try:
    _out81 = daemon.run_check(_p81)
finally:
    daemon._check_copy = _copy81o
    daemon._run_one = _runone81o
_took81 = time.time() - _t81

check("it answers ok, and says it skipped",
      (_out81.get("ok"), _out81.get("skipped")), (True, True))
check("and the sentence names the setting and what to do instead",
      ("projects[path].checks is empty" in (_out81.get("why") or "")
       and "accept by what you opened" in (_out81.get("why") or "")), True)
note("what it says", (_out81.get("why") or "")[:120])
check("NOTHING was copied", _copied81, [])
check("NOTHING was run", _ran81, [])
check("and no artefacts folder was made",
      os.path.isdir(os.path.join(_p81, "test-results")), False)
check("and it took no time worth measuring", _took81 < 1.0, True)
check("and it did not take the one check seat, so a real check for a"
      " real project is not blocked by this one",
      bool(daemon.CHECK_RUNNING.get(_k81)), False)

print("   CONTROL: a project that DOES name its checks still runs them")
post("/config", {"projects": {A: {}, B: {}, C: {},
                              _p81: {"checks": ["suites"]}}})
check("now it names one", daemon.check_kinds(_p81), ["suites"])
_copied81[:] = []
_ran81[:] = []
daemon._check_copy = lambda dst: _copied81.append(dst)
daemon._run_one = lambda *a, **k: (_ran81.append(a[0]), (0, ""))[1]
try:
    _out81b = daemon.run_check(_p81, suite="cases")
finally:
    daemon._check_copy = _copy81o
    daemon._run_one = _runone81o
    daemon.CHECK_RUNNING.pop(_k81, None)
check("the tree IS copied for it", len(_copied81), 1)
check("and something IS run", len(_ran81) > 0, True)
check("and it is not reported as skipped", _out81b.get("skipped"), None)

print("   THE ELEVENTH COMMAND, 2026-09-12, the owner's decision: the")
print("   planner's check knows test_recovery_sim.py by the name the file")
print("   derives from - recovery_sim, the way wake_sim is not wake - and")
print("   hands back the exit the suite returned, not a default")
_ran81 = []
_copied81 = []
daemon._check_copy = lambda dst: _copied81.append(dst)
daemon._run_one = lambda *a, **k: (_ran81.append(a[0]), (7, "seven"))[1]
try:
    _out81d = daemon.run_check(_p81, suite="recovery_sim")
finally:
    daemon._check_copy = _copy81o
    daemon._run_one = _runone81o
    daemon.CHECK_RUNNING.pop(_k81, None)
check("the suite is known - not refused, not skipped",
      (_out81d.get("refused"), _out81d.get("skipped")), (None, None))
check("and it is the recovery suite's own file that was run",
      any("test_recovery_sim.py" in " ".join(str(x) for x in c)
          for c in _ran81), True)
_rows81d = [r for r in (_out81d.get("rows") or [])
            if r.get("what") == "test_recovery_sim.py"]
check("with one row for it", len(_rows81d), 1)
check("carrying the exit the run returned, not a default",
      (_rows81d[0] if _rows81d else {}).get("exit"), 7)
check("and the full run lists it too, derived from CHECK_SUITES",
      "recovery_sim" in daemon.CHECK_SUITES, True)

print("   CONTROL: the refusal for an unknown suite still comes FIRST -")
print("   naming a suite that does not exist is a mistake worth saying so")
print("   about, whether or not the project names any checks")
post("/config", {"projects": {A: {}, B: {}, C: {}, _p81: {}}})
_out81c = daemon.run_check(_p81, suite="no-such-suite")
check("an unknown suite is still refused by name",
      (_out81c.get("refused"),
       "no suite called" in (_out81c.get("why") or "")),
      (True, True))

post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n82. the console helpers are RUN, not stubbed")
print("   2026-09-04: nudge_deaf_window was reached ten times in one day and")
print("   never journalled either of its two loud lines, because")
print("   console_screen answered '' every single time. _SCREEN_SRC is a")
print("   triple-quoted string holding the helper's own Python source, and")
print("   it was NOT raw - so the two-character escape written inside it")
print("   became a real newline and the child was handed an unterminated")
print("   string literal. It died of SyntaxError before reading anything,")
print("   console_screen catches everything and returns '', and the")
print("   caller's own 'could not read the screen' line is therefore")
print("   unreachable: a broken helper and a blank screen are one answer.")
print("   _ANSWER_SRC had the same defect, so no Enter was ever sent either.")
print("   Case 78 STUBS console_screen, which is why it pinned the decision")
print("   and never the helper; the probe that measured the technique keeps")
print("   its own copy of that line, where nothing nests it and it works.")
print("   So this case runs the helpers. -> DECISIONS.md 8.10")

# (a) THE CHEAP HALF, and it alone would have caught this on the day the
# helpers were written: the source has to be a program before anything
# else about it can be true.
for _n82 in ("_SCREEN_SRC", "_ANSWER_SRC"):
    _src82 = getattr(sessions, _n82, None)
    if not isinstance(_src82, str) or not _src82.strip():
        _why82 = "there is no such source"
    else:
        try:
            compile(_src82, "<%s>" % _n82, "exec")
            _why82 = ""
        except SyntaxError as _e82:
            _why82 = "line %s: %s" % (_e82.lineno, _e82.msg)
    check("sessions.%s compiles" % _n82, _why82, "")

# (b) THE HALF THAT MATTERS, against a real console. A stub can only ever
# say that the decision above it is right.
if os.name != "nt":
    print("   (the live half is Windows-only - console_screen returns '' by")
    print("    its own first line elsewhere, and that is not this defect)")
else:
    print("   a child in its own console, born minimised and without focus")
    print("   (rule 29: SW_SHOWMINNOACTIVE, the same as our own windows), which")
    print("   prints a known line and then waits on its console for a key")
    _dir82 = tempfile.mkdtemp(prefix="screen82-", dir=TMP)
    _mark82 = "BRIDGE-CONSOLE-PROBE-%d" % os.getpid()
    _ready82 = os.path.join(_dir82, "ready.txt")
    _proof82 = os.path.join(_dir82, "proof.txt")
    # It writes `ready` only AFTER printing, so waiting on that file is
    # waiting on the fact rather than on a guess about how long a Python
    # takes to start. stdin is deliberately NOT redirected: the point is
    # that the key lands in the CONSOLE and the child reads it from there.
    _CHILD82 = (
        "import io, sys\n"
        "mark, ready, proof = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "print(mark)\n"
        "sys.stdout.flush()\n"
        "io.open(ready, 'w').write('up')\n"
        "line = sys.stdin.readline()\n"
        "io.open(proof, 'w').write(repr(line))\n")
    _si82 = subprocess.STARTUPINFO()
    _si82.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _si82.wShowWindow = 7                      # SW_SHOWMINNOACTIVE
    _kid82 = subprocess.Popen(
        [sys.executable, "-c", _CHILD82, _mark82, _ready82, _proof82],
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=_si82,
        cwd=_dir82)
    try:
        _end82 = time.time() + 30
        while time.time() < _end82 and not os.path.isfile(_ready82):
            time.sleep(0.1)
        check("the child came up in a console of its own",
              os.path.isfile(_ready82), True)

        _scr82 = sessions.console_screen(_kid82.pid)
        check("console_screen brought back a screen at all",
              bool((_scr82 or "").strip()), True)
        check("and the line the child printed is on it",
              _mark82 in (_scr82 or ""), True)

        _sent82 = sessions.console_answer(_kid82.pid)
        check("console_answer says the key was accepted", _sent82, True)
        # THE WITNESS IS THE CHILD, never console_answer's own return value
        # (rule 30): the thing being tested must not be the thing vouching
        # for itself. The child writes the file only once readline() has
        # returned, so the file IS the key arriving.
        _end82 = time.time() + 15
        while time.time() < _end82 and not os.path.isfile(_proof82):
            time.sleep(0.1)
        _got82 = read_or_fail(_proof82, "what the child read from its console") \
            if os.path.isfile(_proof82) else ""
        check("and the child's own stdin received a line",
              bool(_got82) and "n" in _got82, True)
    finally:
        # A suite leaves no window behind (SS8.11), and pre-fix the child is
        # still sitting at its readline when we get here.
        sessions.terminate_and_wait(_kid82.pid)



print("\n83. where the prompt IS on this client's screen")
print("   Piece 1 repaired the helper, and the decision above it still")
print("   answered 'not a bare prompt' for every live window, so the one")
print("   Enter this branch exists for still could not be sent. Two")
print("   independent reasons, both measured on four live screens:")
print("   prompt_is_empty read lines[-1], and the client draws a status")
print("   footer UNDER its input box - a rule, the prompt, a rule, then the")
print("   model/ctx line and the mode line; and its dialog test refused any")
print("   tail holding U+276F, which is this client's own prompt character.")
print("   The fixtures below are the four live screens of 2026-09-04 in")
print("   ASCII - the real ones are Russian, so they live in the private")
print("   suite (test_cases case 7), the same split as case 47 and")
print("   test_handover case 104. -> DECISIONS.md 8.10")

_RULE83 = "─" * 72
_P83 = "❯"


def _screen83(*rows):
    return "\n".join(rows) + "\n"


# The shape every live window had: conversation, a titled rule, the input
# line, a plain rule, then two status lines.
_BARE83 = _screen83(
    "  the executor answered and the turn ended",
    "  ⎿ Tip: Use /btw to ask a quick side question",
    "─" * 40 + " Executor " + "─",
    _P83,
    _RULE83,
    "  Opus 5  ctx ###....... 31%  5h 9%",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · 1 agent")

# THE ONE THAT MATTERS MOST. A window whose SCROLLBACK holds a prompt
# character with text after it, and whose actual input line is bare: this
# was live on 2026-09-04, and any rule that looks for the prompt anywhere
# but its own place gets this one backwards.
_SCROLL83 = _screen83(
    _P83 + " how far along is it now?",
    "  52 %",
    "  reading 34 files... (ctrl+o to expand)",
    "─" * 40 + " Planner " + "─",
    _P83,
    _RULE83,
    "  Fable 5.1  ctx ########.. 78%  5h 8%",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · 1 agent")

# Somebody has typed into the box and not sent it. Enter here SENDS that,
# so it is never keyed - and the words go into the journal.
_TYPED83 = _screen83(
    "  the wave came back and the sheets are up",
    "─" * 40 + " Planner " + "─",
    _P83 + " ok, waiting for the rest of the batch",
    _RULE83,
    "  Fable 5.1  ctx ###....... 33%  5h 4%",
    "  ⏵⏵ auto mode on (shift+tab to cycle)")

# A dialog. Enter CHOOSES whatever is selected, so it is never keyed.
_DIALOG83 = _screen83(
    "  Do you want to proceed?",
    _P83 + " 1. Yes",
    "  2. Yes, and don't ask again",
    "  3. No, and tell Claude what to do differently")

# No rule anywhere: an older client, or something that is not this client
# at all. The old reading is what is left, and it must still work.
_OLD83 = _screen83("  some output",
                   "  more output",
                   _P83)
_OLDBUSY83 = _screen83("  some output",
                       "  a command is still running")

for _name83, _scr83, _want83 in (
        ("a bare prompt under the client's own footer", _BARE83, True),
        ("a bare prompt with a used one in the scrollback", _SCROLL83, True),
        ("text typed into the box", _TYPED83, False),
        ("a dialog with numbered choices", _DIALOG83, False),
        ("no rule at all, bare prompt last (older client)", _OLD83, True),
        ("no rule at all, and the last line is not a prompt",
         _OLDBUSY83, False)):
    _got83, _last83 = daemon.prompt_is_empty(_scr83)
    check("prompt_is_empty: %s" % _name83, _got83, _want83)

# WHAT GOES IN THE JOURNAL. The second value is what the branch quotes to
# a person, so for a window that is left alone it has to be the input
# line - quoting the mode line tells them nothing about why.
check("and what it hands back to be quoted is the prompt line",
      daemon.prompt_is_empty(_TYPED83)[1],
      _P83 + " ok, waiting for the rest of the batch")
check("even when a rule is the last thing before the footer",
      daemon.prompt_is_empty(_BARE83)[1], _P83)



print("\n84. a turn opened in the SAME SECOND as the delivery")
print("   2026-09-05, the first live nudge: a planner took a report at")
print("   00:26:42.171, dequeued it at .179 and wrote its `user` entry at")
print("   .191 - the turn was open 20 ms after the delivery. Four minutes")
print("   later the bridge asked whether that window had opened a turn,")
print("   was told no, and keyed Enter into a window that was already")
print("   mid-turn. The witness counts `user` entries and that one was")
print("   there; what it could not do was place it AFTER the delivery,")
print("   because _entry_epoch read ts[:19] - whole seconds - while `when`")
print("   is a real time.time() and the test is strictly greater. So a turn")
print("   opened inside the delivery's own second reads as older than the")
print("   delivery. -> DECISIONS.md 8.10")

_dir84 = tempfile.mkdtemp(prefix="samesec84-", dir=TMP)
_T84 = 1788557202.0                      # a whole second, for arithmetic
_iso84 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(_T84))


def _tr84(*rows):
    """A transcript file holding exactly these rows."""
    p = os.path.join(_dir84, "t%d.jsonl" % len(os.listdir(_dir84)))
    with open(p, "w", encoding="utf-8", newline="") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


_user84 = {"type": "user", "timestamp": "%s.191Z" % _iso84,
           "message": {"role": "user", "content": "a report"}}
_p84 = _tr84(_user84)

check("the entry's own stamp keeps its milliseconds",
      round(daemon._entry_epoch(_user84) - _T84, 3), 0.191)

print("   the delivery is mid-second and the turn opens after it")
check("a turn 20 ms after the delivery counts as after it",
      daemon.transcript_moved_after(_p84, _T84 + 0.150) > 0, True)
check("and what it hands back is the entry's own time",
      round(daemon.transcript_moved_after(_p84, _T84 + 0.150) - _T84, 3), 0.191)

print("   CONTROL: it must not become true for everything, or the witness")
print("   stops being one - a delivery LATER in the same second is still")
print("   later than the turn")
check("a delivery after the entry is not moved", 
      daemon.transcript_moved_after(_p84, _T84 + 0.500), 0.0)
check("nor is one a whole second later",
      daemon.transcript_moved_after(_p84, _T84 + 1.0), 0.0)

print("   CONTROL: the answer it always gave for a clearly older delivery")
check("a second before the entry is still moved",
      daemon.transcript_moved_after(_p84, _T84 - 1.0) > 0, True)

print("   CONTROL: a stamp with no fraction at all still reads, because")
print("   not every row the client writes carries one")
_bare84 = {"type": "assistant", "timestamp": "%sZ" % _iso84,
           "message": {"role": "assistant"}}
check("a whole-second stamp is still an epoch",
      daemon._entry_epoch(_bare84), _T84)
check("and it is still counted",
      daemon.transcript_moved_after(_tr84(_bare84), _T84 - 1.0) > 0, True)

print("   CONTROL: S5.38 survives - the death writes the transcript too, and")
print("   its own record must still not be an alibi even in the same second")
_died84 = {"type": "assistant", "timestamp": "%s.900Z" % _iso84,
           "isApiErrorMessage": True,
           "message": {"role": "assistant",
                       "content": "API Error: Connection lost mid-response"}}
check("an api-error record in the delivery's second is still no witness",
      daemon.transcript_moved_after(_tr84(_died84), _T84 + 0.150), 0.0)

print("   CONTROL: a malformed stamp answers 0 rather than raising")
check("nonsense in, zero out",
      daemon._entry_epoch({"type": "user", "timestamp": "not a time at all"}),
      0.0)
check("and so does a fraction that is not one",
      daemon._entry_epoch({"type": "user",
                           "timestamp": "%s.xyzZ" % _iso84}), _T84)



print("\n85. a clean stop is not a crash")
print("   Three reasons share one exit at startup and only two of them are")
print("   a loss: the power went, or somebody killed the window. The third")
print("   - shut down properly while a loop happened to be on - is what")
print("   every restart through the gate looks like, and it rang the owner")
print("   with the word `crash` twice on the night of 2026-09-04, at")
print("   22:26:25 and 00:05:57, for two restarts that lost nothing.")
print("   It is not silence either: farewell() already sends a message on")
print("   the way OUT, naming the windows left running, so the person has")
print("   been told once by the half that knows. -> DECISIONS.md 8.10")

_report85 = need78(daemon, "report_startup", "")
_told85 = []
_realn85, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _told85.append((kind, text[:60]))
try:
    for _r85, _wantring in (("reboot", True), ("killed", True),
                            ("shutdown_mid_run", False)):
        del _told85[:]
        _before85 = len(daemon.store.recent_events(300))
        _out85 = _report85(_r85, True)
        _rows85 = daemon.store.recent_events(300)
        _said85 = [r for r in _rows85 if r.get("kind") == "recovery"
                   and daemon.REASONS[_r85][:40] in (r.get("text") or "")]
        check("%s: it is written down either way" % _r85,
              bool(_said85), True)
        check("%s: a person is rung only for a real loss" % _r85,
              [k for k, _ in _told85] == ["crash"], _wantring)
        check("%s: and the reason is handed back" % _r85, _out85, _r85)

    print("   CONTROL: the line for a clean stop is not a SOUND either - the")
    print("   chat is a phone and the journal is the log")
    _quiet85 = [r for r in daemon.store.recent_events(300)
                if r.get("kind") == "recovery"
                and daemon.REASONS["shutdown_mid_run"][:40] in (r.get("text") or "")]
    check("it is logged, not sounded",
          sorted({r.get("level") for r in _quiet85}), ["log"])

    print("   CONTROL: no reason at all still says something, both ways")
    del _told85[:]
    check("a clean start with nothing owed says so",
          (_report85(None, True), [k for k, _ in _told85]),
          ("", []))
    check("and an unclean one with no stakes says THAT",
          any("Previous stop was not clean" in (r.get("text") or "")
              for r in [_report85(None, False)] and
              daemon.store.recent_events(300)), True)
finally:
    daemon.notify = _realn85



print("\n86. a background command that ended leaves the half FREE")
print("   PreToolUse records `waiting on a process` for a tracked command,")
print("   and PostToolUse put it back to idle - but only when it had POPPED")
print("   a record, and a background one survives its own PostToolUse by")
print("   design (8.6). What really ends it is check_background, on the")
print("   client's own notice, and that did not touch the state at all.")
print("   Measured 2026-09-05 from inside the recovery simulation: the")
print("   record was gone from inflight_live and from PROCTRACK, and the")
print("   half still read `waiting on a process` - which tool_in_flight")
print("   answers `busy` to, and which stalled() and clinch() take as an")
print("   alibi. A half that ran a background command and then froze was")
print("   excused until its next Stop, and a frozen half has no next Stop.")
print("   -> DECISIONS.md 8.10")

_p86 = os.path.join(TMP, "bgstate")
os.makedirs(_p86, exist_ok=True)
_k86 = canon(_p86)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p86: {}}})
_sid86 = "bg86-ex"
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86})
# THE SHAPE statusline.py ACTUALLY POSTS - flat keys are ignored, and a
# case that sends them has no session record to ask, so `tool_in_flight`
# answers False because there is nobody to answer about. That is the same
# accident this case exists to catch, and it caught me first.
post("/status", {"role": "executor", "payload": {
    "session_id": _sid86,
    "workspace": {"current_dir": _p86, "project_dir": _p86},
    "model": {"display_name": "Opus 5 (1M context)", "id": "claude-opus-5"},
    "context_window": {
        "context_window_size": 1000000, "used_percentage": 12.0,
        "current_usage": {"input_tokens": 10,
                          "cache_creation_input_tokens": 90,
                          "cache_read_input_tokens": 119900,
                          "output_tokens": 4000}}}})
check("the pair has a session record to answer about",
      bool((daemon.best_session(_p86, "executor") or {}).get("session_id")),
      True)
_cmd86 = "py -m tool --long"

print("   the real order: PreToolUse with run_in_background, then its own")
print("   PostToolUse, then the client's notice - never a hand-built state")
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86,
                   "tool_name": "Bash",
                   "tool_input": {"command": _cmd86,
                                  "run_in_background": True}})
check("while it runs the half is busy",
      daemon.tool_in_flight(_p86, "executor"), True)
post_rc("/event", {"hook_event_name": "PostToolUse", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86,
                   "tool_name": "Bash",
                   "tool_input": {"command": _cmd86,
                                  "run_in_background": True}})
check("its own PostToolUse does NOT end it - that is the bg record",
      bool(daemon.inflight_live(_p86)), True)

print("   now the client says it finished, which is the only thing that can")
_rec86 = None
for _sig86, _meta86 in (daemon.PROCTRACK.get(_k86) or {}).items():
    if isinstance(_meta86, dict) and _meta86.get("bg"):
        _rec86 = (_sig86, _meta86)
        break
check("there is a background record to end", bool(_rec86), True)
if _rec86:
    _tid86 = "toolu_bg86"
    _tr86 = os.path.join(TMP, "tr-bg86.jsonl")
    with open(_tr86, "w", encoding="utf-8") as _fh86:
        _fh86.write(json.dumps({
            "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": _tid86, "name": "Bash",
                 "input": {"command": _cmd86, "run_in_background": True}}]}})
                    + "\n")
        _fh86.write(json.dumps({
            "type": "queue-operation", "operation": "enqueue",
            "content": "<task-notification>%s <status>completed</status> "
                       "(exit code 0)</task-notification>" % _tid86}) + "\n")
    _prevof86 = daemon.sessions.transcript_of
    daemon.sessions.transcript_of = (
        lambda sid, path=None, _t=_tr86, _s=_sid86, _p=_prevof86:
        _t if sid == _s else _p(sid))
    try:
        _rec86[1]["tpos"] = 0
        daemon.check_background()
        check("the record is gone", bool(daemon.inflight_live(_p86)), False)
        check("AND the half is free - the state went back with it",
              daemon.tool_in_flight(_p86, "executor"), False)
        check("and the record says idle, not merely nothing",
              (daemon.best_session(_p86, "executor") or {})
              .get("state"), "idle")
        check("the closing line says so, so a person can see it",
              any("idle" in (r.get("text") or "")
                  and "Background command finished" in (r.get("text") or "")
                  for r in daemon.store.recent_events(200, project=_p86)),
              True)
    finally:
        daemon.sessions.transcript_of = _prevof86

print("   CONTROL: a SECOND tracked command still running keeps it busy -")
print("   the old PostToolUse said idle whatever else was going on")
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86,
                   "tool_name": "Bash",
                   "tool_input": {"command": "make -j4"}})
post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86,
                   "tool_name": "Bash",
                   "tool_input": {"command": "pytest -q tests"}})
post_rc("/event", {"hook_event_name": "PostToolUse", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86,
                   "tool_name": "Bash",
                   "tool_input": {"command": "make -j4"}})
check("one of two ended, and the half is still busy",
      daemon.tool_in_flight(_p86, "executor"), True)
post_rc("/event", {"hook_event_name": "PostToolUse", "role": "executor",
                   "session_id": _sid86, "project_dir": _p86, "cwd": _p86,
                   "tool_name": "Bash",
                   "tool_input": {"command": "pytest -q tests"}})
check("and free once the second ends too",
      daemon.tool_in_flight(_p86, "executor"), False)
post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n87. the console of a live window is not touched unless the config "
      "says so")
print("   2026-09-05: the owner's windows began freezing - the picture")
print("   stopped while the model kept working, tool calls and all, and a")
print("   keystroke revived it without interrupting the turn. The times")
print("   follow the times THIS BRIDGE started reading consoles for real,")
print("   which was 2026-09-04 at 23:16, when the helpers first compiled.")
print("   Whether AttachConsole is the cause is NOT settled by this case.")
print("   What this case settles is that the bridge can stop: nudge_console")
print("   is false in the defaults, so a bridge that never heard of the key")
print("   reads no screen and sends no key at all.")

_p87 = os.path.join(TMP, "console-switch")
os.makedirs(_p87, exist_ok=True)
_k87 = canon(_p87)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p87: {}}})
post_rc("/loop", {"action": "start", "project": _p87})
for _r87, _s87 in (("executor", "cs87-ex"), ("planner", "cs87-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r87,
                       "session_id": _s87, "project_dir": _p87, "cwd": _p87})
    register(_p87, _r87, _s87)
_PID87 = 870001
# a LIVE planner window, declared so (8.46) - see case 78
DECLARED_ALIVE.add(_PID87)
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|planner" % _k87] = {
        "pid": _PID87, "at": time.time(), "registered": True,
        "registered_via": "session"}
    daemon.save_state()

_seen87 = {"reads": 0, "keys": 0}
_scr87o, _ans87o = daemon.sessions.console_screen, daemon.sessions.console_answer


def _read87(p, timeout=20):
    _seen87["reads"] += 1
    return "some earlier output\n\n> "


def _key87(p, timeout=20):
    _seen87["keys"] += 1
    return True


def _j87(sub):
    return _j74(sub, _p87)


daemon.sessions.console_screen = _read87
daemon.sessions.console_answer = _key87
_answerfn87 = need78(daemon, "answer_window_prompt", "")
_was87 = daemon.CFG.get("nudge_console")

try:
    print("   OFF is the default, and off means nothing is touched")
    daemon.CFG["nudge_console"] = False
    _seen87["reads"] = _seen87["keys"] = 0
    _out87 = _nudgefn78(_p87, "planner", time.time() - 900, "a report")
    check("the console is NOT read", _seen87["reads"], 0)
    check("and no key is sent", _seen87["keys"], 0)
    check("and it says so rather than answering as if it had looked",
          _out87, "console off")
    check("and the journal names the config",
          bool(_j87("screen read disabled by config")), True)

    print("   the OTHER path that attaches is the same switch, not a second")
    print("   one - answer_window_prompt reads the console of a window that")
    print("   has not come up, and it is the same physical act")
    _seen87["reads"] = 0
    check("answer_window_prompt does not attach either",
          _answerfn87(_p87, "executor", _PID87), "console off")
    check("nothing was read there either", _seen87["reads"], 0)

    print("   AND IT IS A SWITCH, NOT A REMOVAL - rule 10. With it on, the")
    print("   mechanism is exactly what it was, or this case would be")
    print("   pinning an amputation and calling it a fix.")
    daemon.CFG["nudge_console"] = True
    _seen87["reads"] = _seen87["keys"] = 0
    _out87b = _nudgefn78(_p87, "planner", time.time() - 800, "a report")
    check("with the switch on the screen is read again",
          _seen87["reads"] >= 1, True)
    check("and the key goes, exactly one", _seen87["keys"], 1)
    check("and the answer is the nudge's own word", _out87b, "nudged")

    print("   A GATE NOBODY CAN FLIP IS NO GATE - rule 24. It has to be")
    print("   reachable from /config, which copies only the keys it knows.")
    daemon.CFG["nudge_console"] = False
    post("/config", {"nudge_console": True})
    check("/config carries it into the running daemon",
          daemon.CFG.get("nudge_console"), True)
    check("it is in the key inventory /config copies from",
          "nudge_console" in daemon.CONFIG_KEYS, True)
    # getattr, not store.DEFAULTS: the red-first run of this case ended in
    # an AttributeError on a name that does not exist, which reddens
    # nothing and kills every block below it. That is the whole reason
    # need78 is in this file.
    # THE SHIPPED DEFAULT IS ON, and that is a decision with a reason
    # rather than an oversight: the freeze this key was written in one
    # hour of was NOT caused by reading a console (the cause was the
    # host's selection mode, caught in a window title), and switching a
    # repair off for a cause it did not have costs the repair. What the
    # key buys is the ability to stop in one move, which is what was
    # missing when it was suspected.
    check("the shipped default is ON, deliberately",
          (getattr(store, "DEFAULT_CONFIG", None) or {}).get(
              "nudge_console", "missing"), True)
finally:
    daemon.sessions.console_screen = _scr87o
    daemon.sessions.console_answer = _ans87o
    DECLARED_ALIVE.discard(_PID87)
    daemon.CFG["nudge_console"] = _was87
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n88. QuickEdit is turned OFF on the console of a window we own")
print("   2026-09-05, and it cost weeks. The owner's windows kept freezing:")
print("   the picture stopped, the model carried on, tool calls kept going,")
print("   and a keystroke revived it without interrupting the turn. The")
print("   legacy console host blocks the application in WriteConsole while")
print("   a SELECTION is up, and Esc clears the selection and is eaten by")
print("   the host - which is why no interruption ever reached a transcript.")
print("   Caught from outside with nothing attached: the frozen window's")
print("   TITLE carried the host's own word for a selection, and for 78 s")
print("   that title never changed while two controls changed five times.")
print("   THE READING THAT EXCLUDED IT WAS BLIND. 5.18 measured 0x0208 and")
print("   read it as 'QuickEdit is off'. 0x0208 has ENABLE_EXTENDED_FLAGS")
print("   (0x80) CLEAR, and with that bit clear the word reports neither")
print("   QuickEdit nor Insert: they come from the console's defaults, and")
print("   this machine's HKCU/Console/QuickEdit is 1. -> DECISIONS.md 8.14")

_QE88 = 0x0040          # ENABLE_QUICK_EDIT_MODE
_EX88 = 0x0080          # ENABLE_EXTENDED_FLAGS
_CLIENT88 = 0x0208      # exactly what 5.18 measured on the live windows

print("   the arithmetic first, because it IS the defect and it cannot rot:")
print("   on a mode with 0x80 clear, clearing 0x40 is a no-op")
check("the August edit changes nothing on the client's mode",
      _CLIENT88 & ~_QE88, _CLIENT88)
check("and the repaired arithmetic does change it",
      (_CLIENT88 | _EX88) & ~_QE88, 0x0288)

_quiet88 = need78(sessions, "console_quiet_edit", {})

print("   WHO DOES THIS REACH, AND WHEN (rule 31). A mode set once at")
print("   launch lasts until the client next starts a session - and a")
print("   compaction restart, a /clear and a fork are all SessionStarts.")
print("   So the call is on the SessionStart branch, every time.")
_src88d = inspect.getsource(daemon)
check("the daemon has the caller that journals both modes",
      callable(getattr(daemon, "quiet_console_for", None)), True)
check("and the SessionStart branch is where it is called",
      'args=(path, role, "SessionStart")' in _src88d, True)
check("and it is gated on the same switch as every other console touch",
      'if not CFG.get("nudge_console")' in
      inspect.getsource(getattr(daemon, "quiet_console_for", lambda: None)),
      True)

print("   AND THE LINE CARRIES BOTH NUMBERS. A line saying the mode was")
print("   turned off is the setter reporting its own intention; before and")
print("   after, each read back with GetConsoleMode, is a measurement")
print("   (rule 30). Written because the sabotage that DELETES that line")
print("   first reddened nothing at all - which meant the case was pinning")
print("   the arithmetic and leaving the witness unguarded.")
_p88j = os.path.join(TMP, "quiet-journal")
os.makedirs(_p88j, exist_ok=True)
_k88j = canon(_p88j)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p88j: {}}})
# a LIVE planner window, declared so (8.46) - see case 78
DECLARED_ALIVE.add(880001)
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|planner" % _k88j] = {
        "pid": 880001, "at": time.time(), "registered": True,
        "registered_via": "session"}
    daemon.save_state()
_qe88o = daemon.sessions.console_quiet_edit
daemon.sessions.console_quiet_edit = (
    lambda pid, apply=True, mode=None, timeout=20:
    {"pid": pid, "before": 0x0208, "after": 0x0288, "ok": True})
_wasq88 = daemon.CFG.get("nudge_console")
_qc88 = need78(daemon, "quiet_console_for", "")
try:
    daemon.CFG["nudge_console"] = True
    check("the caller reports that it set the mode",
          _qc88(_p88j, "planner", "case 88"), "set")
    _l88 = _j74("console QuickEdit off", _p88j)
    check("and the journal carries the line at all", bool(_l88), True)
    _t88 = (_l88 or [{}])[-1].get("text", "")
    check("with the mode it changed FROM", "0x0208" in _t88, True)
    check("and the mode it changed TO", "0x0288" in _t88, True)
    check("and the pid, so a person can tell which window",
          "880001" in _t88, True)
    print("   AND IT RUNS ON A REAL SessionStart, not only in the source.")
    print("   The branch starts a THREAD, so the thread is joined before")
    print("   anything is asserted - a check that runs before the thread")
    print("   does is green by construction, which is the shape CLAUDE.md")
    print("   refuses under 'a NEGATIVE check must outlive what it denies'.")
    _seen88s = []
    daemon.sessions.console_quiet_edit = (
        lambda pid, apply=True, mode=None, timeout=20:
        (_seen88s.append(pid) or {"pid": pid, "before": 0x0208,
                                  "after": 0x0288, "ok": True}))
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                       "session_id": "qe88-pl", "project_dir": _p88j,
                       "cwd": _p88j})
    for _th88 in list(threading.enumerate()):
        if _th88.name.startswith("quiet-edit"):
            _th88.join(timeout=20)
    check("a real SessionStart reached the console, by pid",
          _seen88s, [880001])

    print("   and this touch is under the same switch as the others")
    daemon.CFG["nudge_console"] = False
    _seen88s[:] = []
    check("with the switch off no console is touched here either",
          _qc88(_p88j, "planner", "case 88"), "console off")
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                       "session_id": "qe88-pl2", "project_dir": _p88j,
                       "cwd": _p88j})
    for _th88 in list(threading.enumerate()):
        if _th88.name.startswith("quiet-edit"):
            _th88.join(timeout=20)
    check("and a SessionStart under the switch touches nothing",
          _seen88s, [])
finally:
    daemon.sessions.console_quiet_edit = _qe88o
    DECLARED_ALIVE.discard(880001)
    daemon.CFG["nudge_console"] = _wasq88
    post("/config", {"projects": {A: {}, B: {}, C: {}}})

if os.name != "nt":
    print("   (the live half is Windows-only - skipped)")
else:
    print("   AND NOW ON A REAL CONSOLE THIS SUITE OWNS - never a pair")
    print("   window. Born with CREATE_NEW_CONSOLE and SW_SHOWMINNOACTIVE")
    print("   (rule 29) and waited on at the end (terminate_and_wait).")
    _kid88 = None
    try:
        _src88 = os.path.join(TMP, "qe88_child.py")
        _lines88 = ["import sys, time",
                    "for i in range(400):",
                    "    sys.stdout.write('tick %d' % i)",
                    "    sys.stdout.write(chr(10))",
                    "    sys.stdout.flush()",
                    "    time.sleep(1)"]
        with open(_src88, "w", encoding="utf-8") as _fh88:
            _fh88.write(chr(10).join(_lines88) + chr(10))
        _si88 = subprocess.STARTUPINFO()
        _si88.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si88.wShowWindow = 7                      # SW_SHOWMINNOACTIVE
        _kid88 = subprocess.Popen(
            [sys.executable, _src88],
            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=_si88)
        time.sleep(2.0)

        _r88 = _quiet88(_kid88.pid, apply=False)
        check("a console's mode can be READ without changing it",
              isinstance((_r88 or {}).get("before"), int), True)
        note("this console starts at", hex((_r88 or {}).get("before") or 0))

        print("   BE THE CLIENT: force it into 0x0208 and show the reading")
        print("   is BLIND - the flag reads clear because it is not reported")
        _b88 = _quiet88(_kid88.pid, mode=_CLIENT88)
        check("the console is now in the mode 5.18 measured",
              (_b88 or {}).get("after"), _CLIENT88)
        check("QuickEdit reads as clear there",
              bool(((_b88 or {}).get("after") or 0) & _QE88), False)
        check("and EXTENDED is clear too, which is what makes it blind",
              bool(((_b88 or {}).get("after") or 0) & _EX88), False)

        print("   THE FIX, on exactly that console")
        _f88 = _quiet88(_kid88.pid)
        check("EXTENDED is set, so the word now reports the flag",
              bool(((_f88 or {}).get("after") or 0) & _EX88), True)
        check("and QuickEdit is off - reported, and meant",
              bool(((_f88 or {}).get("after") or 0) & _QE88), False)
        check("which is 0x0288, the number the live windows got",
              (_f88 or {}).get("after"), 0x0288)
        check("and it says what it changed FROM, or the journal line is a "
              "claim and not a measurement",
              (_f88 or {}).get("before"), _CLIENT88)

        print("   AND IT STICKS - read back by a fresh child that sets")
        print("   nothing, because a setter reporting its own success is")
        print("   the witness rule 30 refuses")
        _a88 = _quiet88(_kid88.pid, apply=False)
        check("the mode is still 0x0288 when nobody is setting it",
              (_a88 or {}).get("before"), 0x0288)
    finally:
        if _kid88 is not None:
            try:
                sessions.terminate_and_wait(_kid88.pid)
            except Exception:
                pass


print("\n89. a CHANNEL cannot vouch for a window that never came up")
print("   2026-09-05, found live on two pairs at once. A handover opens a")
print("   window; the window stops on the client's development-channels")
print("   consent dialog and never reaches SessionStart. Within 45 s the")
print("   REPLACED window's channel process heartbeats (5.3), the daemon")
print("   marks the new record registered - and check_sessions, which reads")
print("   the bare `registered` flag, skips the whole 'never came up' block")
print("   for the rest of that window's life. So answer_window_prompt, whose")
print("   entire job is to answer that dialog, had never run ONCE in two")
print("   days: `screen_asked` was None on every record in the live state.")
print("   Two windows sat on that dialog for 6.8 hours; a third was doing it")
print("   on another pair while this case was being written.")
print("   5.44 named this witness and fixed it in ensure_record - the test")
print("   is registered_via == 'session', the WINDOW's own word. This site")
print("   kept the weak one. -> DECISIONS.md 8.15")

_p89 = os.path.join(TMP, "never-came-up")
os.makedirs(_p89, exist_ok=True)
_k89 = canon(_p89)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p89: {}}})
_PID89 = 890001
_seen89 = {"screen": 0, "keys": 0}
_scr89o, _ans89o = daemon.sessions.console_screen, daemon.sessions.console_answer
_DIALOG89 = ("WARNING: Loading development channels\n"
             "  Channels: server:bridge\n"
             "  1. I am using this for local development\n"
             "  2. Exit\n"
             "  Enter to confirm - Esc to cancel\n")


def _read89(p, timeout=20):
    _seen89["screen"] += 1
    return _DIALOG89


def _key89(p, timeout=20):
    _seen89["keys"] += 1
    return True


def _j89(sub):
    return _j74(sub, _p89)


def _seed89(via):
    """One pid record, this project only, older than the 90 s grace."""
    with daemon._lock:
        daemon.STATE["pids"] = {"%s|executor" % _k89: {
            "pid": _PID89, "at": time.time() - 200, "registered": bool(via),
            "registered_via": via}}
        daemon.STATE["started_at"] = time.time() - 1000
        daemon.save_state()
    _seen89["screen"] = _seen89["keys"] = 0


_pids89 = dict(daemon.STATE.get("pids") or {})
_started89 = daemon.STATE.get("started_at")
_was89 = daemon.CFG.get("nudge_console")
try:
    daemon.sessions.console_screen = _read89
    daemon.sessions.console_answer = _key89
    daemon.CFG["nudge_console"] = True

    print("   THE CONTROL FIRST: a window that really came up is left alone,")
    print("   or this case would be pinning 'ask every window every tick'")
    _seed89("session")
    daemon.check_sessions(90)
    check("a session-registered window is not asked", _seen89["screen"], 0)
    check("and no key is sent to it", _seen89["keys"], 0)

    print("   AND NOW THE CORPSE'S HEARTBEAT, through the real endpoint")
    _seed89("")
    register(_p89, "executor", "nc89-ex")
    _rec89 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k89) or {}
    check("the channel marked the record registered, as it always has",
          _rec89.get("registered"), True)
    check("but the witness is NAMED, and it is not the window",
          _rec89.get("registered_via"), "channel")

    print("   the deciding tick - the real one, not a snapshot (5.33)")
    _seen89["screen"] = _seen89["keys"] = 0
    daemon.check_sessions(90)
    check("the screen is read anyway, because no WINDOW ever said it came up",
          _seen89["screen"] >= 1, True)
    check("the dialog is answered, exactly once", _seen89["keys"], 1)
    # NOT "development-channels": that string is in this project's journal
    # for other reasons, and the check passed on the pre-fix run where
    # nothing had been answered at all. The phrase asserted has to be the
    # one only answer_window_prompt writes.
    check("and the journal names the prompt that was answered",
          bool(_j89("answered it with Enter")), True)
    check("and the record remembers it asked, so a second tick does not "
          "key a screen that has moved on",
          ((daemon.STATE.get("pids") or {}).get("%s|executor" % _k89)
           or {}).get("screen_asked"), True)
    _seen89["keys"] = 0
    daemon.check_sessions(90)
    check("the second tick sends nothing", _seen89["keys"], 0)
    print("   AND THE HOLD THAT COUNTS THESE FAILURES HAS A WAY OUT NOW.")
    print("   It is cleared in exactly two places - a SessionStart for the")
    print("   role it waited for, and the success path of")
    print("   resume_after_handover - and BOTH need a new window to come up,")
    print("   which the hold itself prevents. Live on 2026-09-05: armed at")
    print("   08:02, still there at 14:30 having survived a restart, with")
    print("   nothing left in the world that could lift it.")
    _hold89 = need78(daemon, "expire_handover_hold", False)
    _mig89 = need78(daemon, "migrate_handover_holds", None)
    _book89 = dict(daemon.STATE.get("handover_failed") or {})
    try:
        print("   a hold younger than the hour stands")
        with daemon._lock:
            daemon.STATE["handover_failed"] = {
                _k89: {"n": 2, "at": time.time() - 60, "roles": ["executor"]}}
            daemon.save_state()
        check("it is still held", bool(daemon.handover_blocked(_p89)), True)
        check("and nothing was expired", _hold89(_p89), False)

        print("   past HANDOVER_HOLD_SEC it lifts, and says so")
        with daemon._lock:
            daemon.STATE["handover_failed"] = {
                _k89: {"n": 2, "at": time.time() - 4000,
                       "roles": ["executor"]}}
            daemon.save_state()
        check("the constant is an hour", daemon.HANDOVER_HOLD_SEC, 3600)
        check("asking whether the pair is blocked lifts it",
              daemon.handover_blocked(_p89), None)
        check("the record is gone",
              (daemon.STATE.get("handover_failed") or {}).get(_k89), None)
        check("and the journal says it expired, with the age",
              bool(_j89("handover hold expired")), True)

        print("   the STARTUP migration is the fact, not the clock: a hold")
        print("   whose stuck windows are gone has lost its evidence")
        with daemon._lock:
            daemon.STATE["handover_failed"] = {
                _k89: {"n": 2, "at": time.time() - 60, "roles": ["executor"]}}
            daemon.STATE["pids"] = {"%s|executor" % _k89: {
                "pid": _PID89, "at": time.time() - 200, "registered": True,
                "registered_via": "channel"}}
            daemon.save_state()
        _mig89()
        check("a dead stuck window drops the hold",
              (daemon.STATE.get("handover_failed") or {}).get(_k89), None)
        check("and says why", bool(_j89("the evidence for it is too")), True)

        print("   and the control: a hold whose window is STILL SITTING")
        print("   there keeps it, or the migration is an ageing in disguise")
        with daemon._lock:
            daemon.STATE["handover_failed"] = {
                _k89: {"n": 2, "at": time.time() - 60, "roles": ["executor"]}}
            daemon.STATE["pids"] = {"%s|executor" % _k89: {
                "pid": os.getpid(), "at": time.time() - 200,
                "registered": True, "registered_via": "channel"}}
            daemon.save_state()
        _mig89()
        check("the hold stands while the window does",
              bool((daemon.STATE.get("handover_failed") or {}).get(_k89)),
              True)
    finally:
        with daemon._lock:
            daemon.STATE["handover_failed"] = _book89
            daemon.save_state()
    print("   AND ANSWERING THE DIALOG MUST NOT BE UNDONE IN THE SAME TICK.")
    print("   2026-09-05 15:11:02, live on the second pair, two journal")
    print("   lines with the SAME second: 'answered it with Enter (accepted)'")
    print("   and 'executor window never came up: waited 37 min ... Clearing")
    print("   it'. The Enter woke the window; the very next statement in the")
    print("   same tick undid the swap, because `wait` was still 37 minutes.")
    print("   The window came up two seconds later into a pair that no longer")
    print("   expected it: two executors, the channel seat with the empty new")
    print("   one, and the old window's handoff never written.")
    print("   So the clock restarts FROM THE ENTER, and the tick ends there.")
    _seen89["screen"] = _seen89["keys"] = 0
    _GRACE90 = float((daemon.CFG.get("thresholds") or {}).get(
        "startup_grace", 600))
    with daemon._lock:
        daemon.STATE["pids"] = {"%s|executor" % _k89: {
            "pid": _PID89, "at": time.time() - _GRACE90 - 500,
            "registered": False, "registered_via": ""}}
        daemon.STATE.setdefault("handover", {})[_k89] = {
            "stop_after": {"executor": 890777}, "at": time.time() - 100}
        daemon.save_state()
    daemon.check_sessions(90)
    check("the dialog was answered", _seen89["keys"], 1)
    check("and the swap was NOT undone in the same tick",
          ((daemon.STATE.get("handover") or {}).get(_k89) or {})
          .get("stop_after", {}).get("executor"), 890777)
    _rec90 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k89) or {}
    check("the record remembers WHEN it was answered",
          isinstance(_rec90.get("answered_at"), float), True)

    print("   a second tick, still inside the grace from the Enter, waits")
    daemon.check_sessions(90)
    check("still not undone",
          ((daemon.STATE.get("handover") or {}).get(_k89) or {})
          .get("stop_after", {}).get("executor"), 890777)

    print("   THE CONTROL: the clock RESTARTED, it did not stop. Past the")
    print("   grace from the Enter with still no SessionStart, the swap is")
    print("   undone exactly as it always was - or this fix would be")
    print("   'never give up' wearing a repair's name")
    # GUARDED, because the red run of this case died here with a KeyError:
    # on the pre-fix code the tick above had already undone the swap and
    # dropped the record, so the subscript raised and took every block
    # below it with no FAIL list at all - the exact shape CLAUDE.md names
    # under "assert non-empty, then subscript it anyway".
    _r90 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k89)
    check("the record is still there to age - if it is gone the tick "
          "already undid the swap, which is the defect",
          bool(_r90), True)
    if _r90:
        with daemon._lock:
            _r90["answered_at"] = time.time() - _GRACE90 - 10
            daemon.save_state()
    daemon.check_sessions(90)
    check("now it is undone",
          ((daemon.STATE.get("handover") or {}).get(_k89) or {})
          .get("stop_after", {}).get("executor"), None)
finally:
    daemon.sessions.console_screen = _scr89o
    daemon.sessions.console_answer = _ans89o
    daemon.CFG["nudge_console"] = _was89
    with daemon._lock:
        (daemon.STATE.get("handover") or {}).pop(_k89, None)
        daemon.save_state()


print("\n90. three windows on one pair, and the seat with the empty one")
print("   The owner, 2026-09-05: three executor windows at once is a fault.")
print("   The journal shows it took 18 minutes - 14:04:47 a replacement")
print("   opens, 14:15:11 'never came up ... Clearing it', 14:15:16 a")
print("   handover is due again, 14:33:28 a second replacement opens, with")
print("   the first still sitting there. Clearing the record IS what permits")
print("   the next window, and nothing closed the one that did not come up.")
print("   -> DECISIONS.md 8.16")

_p91 = os.path.join(TMP, "three-windows")
os.makedirs(_p91, exist_ok=True)
_k91 = canon(_p91)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p91: {}}})
_close91 = need78(daemon, "close_stray_replacement", 0)
_repair91 = need78(daemon, "repair_orphaned_swap", False)


def _j91(sub):
    return _j74(sub, _p91)


def _child91():
    """A live process of our own to stand for a window that never came up."""
    src = os.path.join(TMP, "stray91.py")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("import time" + chr(10) + "time.sleep(300)" + chr(10))
    return subprocess.Popen([sys.executable, src],
                            creationflags=getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0))


_kids91 = []
_chan91 = dict(daemon.STATE.get("channels") or {})
try:
    print("   (a) a stray IS closed, and the record says which and when")
    _k = _child91()
    _kids91.append(_k)
    _meta91 = {"pid": _k.pid, "at": time.time() - 900, "registered": True,
               "registered_via": "channel", "why": "handover"}
    check("it is closed, and the pid comes back",
          _close91(_p91, "executor", _meta91), _k.pid)
    check("and it is really gone", sessions.pid_alive(_k.pid), False)
    check("with a line naming the pid and when it was opened",
          bool(_j91("replacement window closed: pid %d" % _k.pid)), True)

    print("   (a2) CONTROL, and it is the one that was missing: a record")
    print("   with NO launch mark is not touched at all. The first version")
    print("   asked only 'is there no evidence it came up', which is true of")
    print("   any live pid that reached a pids record - and case 63 puts")
    print("   real processes of its own in there. It killed the suite: the")
    print("   run died at 637 checks with that branch on and reached 1368")
    print("   with it off. A repair that ends a process must establish whose")
    print("   it is, so the test is POSITIVE - the bridge opened this one.")
    _k = _child91()
    _kids91.append(_k)
    check("a record with no launch mark is left alone",
          _close91(_p91, "executor",
                   {"pid": _k.pid, "at": time.time() - 900,
                    "registered": True, "registered_via": "channel"}), 0)
    check("and that process is still running", sessions.pid_alive(_k.pid),
          True)
    check("and reg_pid really writes the mark, or this guard would close "
          "nothing for ever",
          "why" in inspect.getsource(daemon.reg_pid), True)

    print("   (b) CONTROL: a window that came up is not touched. Without")
    print("   this the repair would be 'close the newest window', which is")
    print("   the thing the owner's rule forbids outright.")
    _k = _child91()
    _kids91.append(_k)
    check("a session-registered window is left alone",
          _close91(_p91, "executor",
                   {"pid": _k.pid, "at": time.time() - 900,
                    "registered": True, "registered_via": "session",
                    "why": "handover"}), 0)
    check("and it is still running", sessions.pid_alive(_k.pid), True)

    print("   (c) CONTROL: nor is one that has a channel of its own - that")
    print("   is a session by another name, whatever the record says")
    with daemon._lock:
        daemon.STATE.setdefault("channels", {})["%s|executor" % _k91] = {
            "pid": 910001, "ppid": _k.pid, "port": 1}
        daemon.save_state()
    check("a window with its own channel is left alone",
          _close91(_p91, "executor",
                   {"pid": _k.pid, "at": time.time() - 900,
                    "registered": True, "registered_via": "channel",
                    "why": "handover"}), 0)
    check("and it is still running too", sessions.pid_alive(_k.pid), True)

    print("   (d) THE SEAT GOES BACK. repair_orphaned_swap puts the pids")
    print("   record on the old window - the seat follows it through the")
    print("   parentage test - and writes an `orphaned` record, NOT")
    print("   `stop_after`, because handover_swapping reads that one and")
    print("   would stop the old window being fed as well.")
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})["%s|executor" % _k91] = {
            "pid": 910777, "at": time.time() - 100, "registered": True,
            "registered_via": "session"}
        (daemon.STATE.get("handover") or {}).pop(_k91, None)
        daemon.save_state()
    check("the repair reports it did something",
          _repair91(_p91, "executor", 910666, 910777), True)
    _rec91 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k91) or {}
    check("the seat record is back on the OLD window", _rec91.get("pid"),
          910666)
    check("and it counts as up, so it is not treated as a stray",
          daemon.window_up(_rec91), True)
    # ITS OWN RECORD since 8.36, not a key inside STATE["handover"], where
    # expire_handover read its missing `at` as 1970 and counted a failure.
    _h91 = (daemon.STATE.get("handover") or {}).get(_k91) or {}
    _o91 = (daemon.STATE.get("orphaned_swap") or {}).get(
        "%s|executor" % _k91) or {}
    check("an orphaned-swap record was written, with a clock of its own",
          (_o91.get("old"), bool(_o91.get("at"))), (910666, True))
    check("and nothing in the handover record at all - so no stop_after, "
          "which would starve the old window", _h91, {})
    check("and the journal names both pids",
          bool(_j91("orphaned swap repaired: old pid 910666")), True)
finally:
    for _k in _kids91:
        try:
            sessions.terminate_and_wait(_k.pid)
        except Exception:
            pass
    with daemon._lock:
        daemon.STATE["channels"] = _chan91
        (daemon.STATE.get("handover") or {}).pop(_k91, None)
        (daemon.STATE.get("orphaned_swap") or {}).pop("%s|executor" % _k91,
                                                      None)
        (daemon.STATE.get("pids") or {}).pop("%s|executor" % _k91, None)
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})
    with daemon._lock:
        daemon.STATE["pids"] = _pids89
        if _started89 is not None:
            daemon.STATE["started_at"] = _started89
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})
print("\n91. a SessionEnd is about the session that ended, not about the")
print("    window on record")
print("    2026-09-05 16:14:53: the OLD executor's session ended two minutes")
print("    AFTER its replacement had registered, and this branch answered")
print("    about the replacement - it read pid 33280, found it alive and")
print("    journalled 'its window is still alive' about a window the event")
print("    was not about, having first dropped THAT window's channel and rc")
print("    link. It cost nothing that day because a channel re-registers")
print("    every 45 s; the other order - the ending session being the")
print("    current one while the record names a corpse - is the one that")
print("    loses a live window's record. -> DECISIONS.md 8.17")

_p91 = os.path.join(TMP, "whose-session-ended")
os.makedirs(_p91, exist_ok=True)
_k91b = canon(_p91)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p91: {}}})
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "s91-old", "project_dir": _p91, "cwd": _p91})
register(_p91, "executor", "s91-old")
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": "s91-new", "project_dir": _p91, "cwd": _p91})
register(_p91, "executor", "s91-new")
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % _k91b] = {
        "pid": os.getpid(), "at": time.time() - 60, "registered": True,
        "registered_via": "session"}
    daemon.STATE.setdefault("rc", {})["%s|executor" % _k91b] = {
        "url": "https://claude.ai/code/s91"}
    daemon.save_state()
check("the current session on record is the new one",
      daemon.last_session_id(_p91, "executor"), "s91-new")

print("    the OLD session ends. Its window is not this half's window.")
post_rc("/event", {"hook_event_name": "SessionEnd", "role": "executor",
                   "session_id": "s91-old", "project_dir": _p91, "cwd": _p91})
_rec91 = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k91b) or {}
check("the pid record of the LIVE half is left alone",
      (bool(_rec91), _rec91.get("pid")), (True, os.getpid()))
check("and is not marked as having ended",
      "session_ended_at" in _rec91, False)
check("the live half keeps its rc link",
      bool((daemon.STATE.get("rc") or {}).get("%s|executor" % _k91b)), True)
check("and its channel registration",
      bool((daemon.STATE.get("channels") or {}).get("%s|executor" % _k91b)),
      True)
check("and the journal says which session ended and which is on record",
      any("is not the one on record" in (_t.get("text") or "")
          for _t in _j74("", _p91)), True)

print("    THE CONTROL: the CURRENT session ends, and the branch does its")
print("    old work exactly as before - or this is 'never clean up' wearing")
print("    a repair's name")
post_rc("/event", {"hook_event_name": "SessionEnd", "role": "executor",
                   "session_id": "s91-new", "project_dir": _p91, "cwd": _p91})
_rec91b = (daemon.STATE.get("pids") or {}).get("%s|executor" % _k91b) or {}
check("the record is kept, because the window is alive",
      bool(_rec91b), True)
check("and NOW it is marked as ended",
      bool(_rec91b.get("session_ended_at")), True)
check("and the rc link is dropped",
      bool((daemon.STATE.get("rc") or {}).get("%s|executor" % _k91b)), False)
with daemon._lock:
    (daemon.STATE.get("pids") or {}).pop("%s|executor" % _k91b, None)
    daemon.save_state()
post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n92. the swap that stopped a corpse, and said it had stopped a")
print("    window")
print("    2026-09-05 16:12:22, live: 'the one it replaces (pid 12116) was")
print("    stopped'. 12116 had been closed by hand at 14:31:08 - an hour and")
print("    39 minutes earlier - as a stray replacement that never came up,")
print("    and the pids record was never given back, so the live old window")
print("    (16812) was never touched by anything and the pair had two")
print("    executors. The stop being a no-op is half of it; the journal")
print("    saying it had happened is what kept it invisible.")
print("    -> DECISIONS.md 8.17")

_p92 = os.path.join(TMP, "stopped-a-corpse")
os.makedirs(_p92, exist_ok=True)
_k92 = canon(_p92)
_DEAD92, _LEFT92, _NEW92 = 921001, 921002, 921003
post("/config", {"projects": {A: {}, B: {}, C: {}, _p92: {}}})
_leftover92 = need78(daemon, "leftover_windows", [])
_opened92 = need78(daemon, "opened_by_bridge", None)
# The person being told is half the claim, so it is recorded rather than
# asserted by its absence.
_notes92 = []
_notify92o = daemon.notify
def _notify92(*a, **kw):
    _notes92.append(a[1] if len(a) > 1 else (kw.get("text") or ""))
    return _notify92o(*a, **kw)


daemon.notify = _notify92
_alive92o = daemon.sessions.pid_alive
_stop92o = daemon.sessions.stop
_term92o = daemon.sessions.terminate_and_wait
_ended92 = []
# A WINDOW THAT IS STOPPED IS THEN GONE, or the control below would fire
# refuse_replacement on a pid the stub keeps answering "alive" for - the
# fixture contradicting the thing it is meant to be a control for.
_dead92 = set()
daemon.sessions.pid_alive = (lambda pid: int(pid or 0) == _LEFT92
                             and int(pid or 0) not in _dead92)


def _stop92(project, role, pid=None, wait=None, tree=True):
    _dead92.add(int(pid or 0))
    return True


daemon.sessions.stop = _stop92
daemon.sessions.terminate_and_wait = lambda pid, *a, **k: _ended92.append(pid)
try:
    for _r, _s in (("executor", "s92-ex"), ("planner", "s92-pl")):
        post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                           "session_id": _s, "project_dir": _p92,
                           "cwd": _p92})
        register(_p92, _r, _s)
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})["%s|executor" % _k92] = {
            "pid": _NEW92, "at": time.time(), "registered": True,
            "registered_via": "session"}
        daemon.STATE.setdefault("chan_refused", {})["%s|executor" % _k92] = {
            "92099": {"pid": 92099, "ppid": _LEFT92, "n": 7,
                      "since": time.time() - 600, "told": True}}
        daemon.STATE.setdefault("handover", {})[_k92] = {
            "at": time.time(), "reason": "the wall", "waiting": ["executor"],
            "roles": ["executor"], "iteration": 3,
            "stop_after": {"executor": _DEAD92},
            "old_recs": {"executor": {"pid": _DEAD92}}}
        daemon.save_state()

    print("    the leftover is found from the one signal that repeats: a")
    print("    refused registration carries the window's pid every 45 s")
    check("a live window of this half that is not the one on record",
          _leftover92(_p92, "executor", _NEW92), [_LEFT92])
    check("and the half's own window is never in that list",
          _leftover92(_p92, "executor", _LEFT92), [])

    print("    and the swap completes THE REAL WAY - the replacement's own")
    print("    SessionStart, which is where resume_after_handover runs. Not a")
    print("    direct call: this class of bug has been accepted twice on a")
    print("    hand-built snapshot and came back live both times.")
    _before92 = len(_j74("", _p92))
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": "s92-ex2", "project_dir": _p92,
                       "cwd": _p92})

    def _txt92(since):
        return [(_r.get("text") or "")
                for _r in _j74("", _p92)[since:]]

    check("the journal says the window was ALREADY GONE",
          until(lambda: any("ALREADY GONE" in _t
                            for _t in _txt92(_before92)), 30), True)
    _lines92 = _txt92(_before92)
    check("and does NOT claim it stopped anything",
          any("was stopped - in that order" in _t for _t in _lines92), False)
    # WAITED FOR: since 8.36 the leftover is named by close_leftovers in a
    # line of its own, written after the one above
    check("it names the live leftover",
          until(lambda: any(str(_LEFT92) in _t
                            for _t in _txt92(_before92)), 20), True)
    print("    AND A WINDOW THE BRIDGE DID NOT OPEN IS NOT CLOSED. There is")
    print("    no row for it in window_log - 5.43's second window, opened by")
    print("    hand in the app, has none - so it is named to a person and")
    print("    left alone. Asserted AFTER the line above, which is what makes")
    print("    this deny something that has already happened.")
    check("nothing was ended", _ended92, [])
    check("and a person was told instead",
          any("will not close it" in (_n or "")
              for _n in _notes92), True)

    check("the control swap is finished with the record too",
          until(lambda: not (daemon.STATE.get("handover") or {}).get(_k92),
                20), True)
    print("    THE OTHER HALF, and it is the owner's word: close the old")
    print("    window once the new one is up and working, so they do not")
    print("    multiply. A leftover the BRIDGE opened has a row in")
    print("    window_log saying so, and that row is the whole difference.")
    # AND ITS TURN IS OVER, read from its OWN session (8.36): a leftover is
    # never closed mid-turn, and one whose turn cannot be read is not closed
    # at all. So the window is linked to its session, as hook.py links it,
    # and that session's transcript ends with the client's turn-end record.
    _TR92 = os.path.join(TMP, "s92-left.jsonl")
    with open(_TR92, "w", encoding="utf-8") as _f92:
        _f92.write(json.dumps({"type": "system", "subtype": "turn_duration",
                               "timestamp": time.strftime(
                                   "%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime())}) + chr(10))
    _tof92o = daemon.sessions.transcript_of
    daemon.sessions.transcript_of = (
        lambda sid, cwd=None: _TR92 if sid == "s92-left"
        else _tof92o(sid, cwd))
    with daemon._lock:
        daemon.STATE.setdefault("window_sessions", {}).setdefault(
            "%s|executor" % _k92, {})[str(_LEFT92)] = {
                "sid": "s92-left", "at": time.time()}
        daemon.STATE.setdefault("window_log", []).append(
            {"pid": _LEFT92, "path": _k92, "role": "executor",
             "why": "handover", "at": time.time() - 300})
        daemon.STATE.setdefault("handover", {})[_k92] = {
            "at": time.time(), "reason": "the wall", "waiting": [],
            "roles": ["executor"], "iteration": 4,
            "stop_after": {"executor": _DEAD92},
            "old_recs": {"executor": {"pid": _DEAD92}}}
        daemon.save_state()
    _before92c = len(_j74("", _p92))
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": "s92-ex4", "project_dir": _p92,
                       "cwd": _p92})
    check("now it IS closed, and the journal says by whose word",
          until(lambda: any("by the owner's word" in _t
                            for _t in _txt92(_before92c)), 30), True)
    check("and it was closed by the owning call, not a kill",
          _LEFT92 in _ended92, True)

    print("    THE CONTROL: a recorded pid that IS alive is stopped and said")
    print("    to be, exactly as before")
    # WAITED FOR, not assumed: resume_after_handover journals the line above
    # and drops the handover record several statements later, in a thread.
    # Writing the control's record into that gap gets it popped by the run
    # that is still finishing, and then nothing happens at all - which is
    # exactly what the first run of this case showed.
    check("the first swap is finished with the record",
          until(lambda: not (daemon.STATE.get("handover") or {}).get(_k92),
                20), True)
    with daemon._lock:
        daemon.STATE.setdefault("handover", {})[_k92] = {
            "at": time.time(), "reason": "the wall", "waiting": [],
            "roles": ["executor"], "iteration": 3,
            "stop_after": {"executor": _LEFT92},
            "old_recs": {"executor": {"pid": _LEFT92}}}
        daemon.save_state()
    _before92b = len(_j74("", _p92))
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": "s92-ex3", "project_dir": _p92,
                       "cwd": _p92})
    check("the ordinary line is back",
          until(lambda: any("was stopped - in that order" in _t
                            for _t in _txt92(_before92b)), 30), True)
    _lines92b = _txt92(_before92b)
    check("and nothing says it was already gone",
          any("ALREADY GONE" in _t for _t in _lines92b), False)
finally:
    if "_tof92o" in globals():
        daemon.sessions.transcript_of = _tof92o
    daemon.sessions.pid_alive = _alive92o
    daemon.sessions.stop = _stop92o
    daemon.sessions.terminate_and_wait = _term92o
    daemon.notify = _notify92o
    with daemon._lock:
        daemon.STATE["window_log"] = [
            _r for _r in (daemon.STATE.get("window_log") or [])
            if _r.get("path") != _k92]
        (daemon.STATE.get("handover") or {}).pop(_k92, None)
        (daemon.STATE.get("pids") or {}).pop("%s|executor" % _k92, None)
        (daemon.STATE.get("chan_refused") or {}).pop("%s|executor" % _k92,
                                                     None)
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n93. a clean stop is not a crash, in the SEED either")
print("    QUIET_REASONS knows the third reason - shut down properly while")
print("    a loop happened to be on, which is what every restart through")
print("    the gate looks like - and the CHAT was taught it on 2026-09-04.")
print("    The seed was not, and it is the first thing a session reads: on")
print("    2026-09-05 the journal says 'Bridge stopped cleanly.' at 16:09:30")
print("    and 'The bridge was shut down normally, but a loop was still")
print("    running' at 16:09:40, and the window that came up at 16:12:22 was")
print("    told the machine had stopped without shutting down cleanly.")
print("    -> DECISIONS.md 8.17")

_p93 = os.path.join(TMP, "clean-stop-seed")
os.makedirs(_p93, exist_ok=True)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p93: {}}})
_mode93 = daemon.STATE.get("mode")
_reason93 = daemon.STATE.get("recovered_reason")
_quiet93 = getattr(daemon, "QUIET_REASONS", ())
check("the quiet reason is the one a gated restart produces",
      "shutdown_mid_run" in _quiet93, True)
try:
    for _n, (_why, _clean, _crash) in enumerate((
            ("shutdown_mid_run", True, False),
            ("killed", False, True))):
        with daemon._lock:
            daemon.STATE["mode"] = "recovered"
            daemon.STATE["recovered_reason"] = _why
            daemon.save_state()
        _code93, _out93 = post_rc("/event",
                                  {"hook_event_name": "SessionStart",
                                   "role": "executor",
                                   "session_id": "s93-%d" % _n,
                                   "project_dir": _p93, "cwd": _p93})
        # GUARDED. A body that is not a dict - which is what a refusal or a
        # changed endpoint hands back - used to raise here and take every
        # block below it with no FAIL list at all, the shape CLAUDE.md names
        # under "assert non-empty, then subscript it anyway". It did exactly
        # that on the first run of this case.
        _ctx93 = ((((_out93 if isinstance(_out93, dict) else {})
                    .get("hook_output") or {})
                   .get("hookSpecificOutput") or {})
                  .get("additionalContext") or "")
        check("the SessionStart was answered", _code93, 200)
        check("%s: the seed says it shut down properly" % _why,
              "shut down properly" in _ctx93, _clean)
        check("%s: the seed calls it an unclean stop" % _why,
              "without shutting down cleanly" in _ctx93, _crash)
        check("%s: either way the tree is to be checked" % _why,
              "check the working tree" in _ctx93, True)
finally:
    with daemon._lock:
        if _mode93 is None:
            daemon.STATE.pop("mode", None)
        else:
            daemon.STATE["mode"] = _mode93
        if _reason93 is None:
            daemon.STATE.pop("recovered_reason", None)
        else:
            daemon.STATE["recovered_reason"] = _reason93
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n94. the seat goes back to the window that is WORKING - and only")
print("    when all three conditions say so")
print("    ARMED 2026-09-05 by the planner's decision on the owner's word:")
print("    close the old window once the new one is up and working, so")
print("    they do not multiply. repair_orphaned_swap existed, was covered")
print("    by case 90 and was called from nowhere, because its first")
print("    version fired on 'the refused channel's parent is alive and is")
print("    not the pid on record' - equally true of 5.43's second window,")
print("    opened by hand in the app, where the seat must NOT move. Each")
print("    condition is denied on its own below; the (a) control IS the")
print("    5.43 case. -> DECISIONS.md 8.17")

_p94 = os.path.join(TMP, "orphaned-swap")
os.makedirs(_p94, exist_ok=True)
_k94 = canon(_p94)
_OLD94, _NEW94w, _CHAN94 = 941001, 941002, 941003
_evid94 = need78(daemon, "orphaned_swap_evidence", (False, "absent"))
post("/config", {"projects": {A: {}, B: {}, C: {}, _p94: {}}})
_alive94o = daemon.sessions.pid_alive
_tof94o = daemon.sessions.transcript_of
_TR94 = os.path.join(TMP, "s94-old.jsonl")


def _tof94(sid, cwd=None):
    """The old session's transcript, inside the suite's own temp folder.

    Pointed here rather than at ~/.claude/projects: the case writes the
    file, and the REAL transcript_moved_after reads it, so condition (c)
    is exercised rather than stubbed away.
    """
    return {"s94-old": _TR94, "s94-new": _TR94N}.get(sid)


_TR94N = os.path.join(TMP, "s94-new.jsonl")


def _write94(kind, when):
    """One entry of `kind`, stamped `when`. `system` is what a window that
    is NOT working still emits - that is the negative sample."""
    with open(_TR94, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({
            "type": kind, "timestamp":
            time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(when)),
            "message": {"role": "assistant", "content": "still working"}})
            + "\n")


def _newcomer94(turn_ended_at=None):
    """The newcomer's OWN transcript. Since 8.36 whether it finished a turn
    is read here - its turn-end marker - not off a session record that the
    other window's hooks can re-create."""
    rows = [{"type": "system", "subtype": "bridge_status",
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                        time.gmtime(_reg94))}]
    if turn_ended_at:
        rows.append({"type": "system", "subtype": "turn_duration",
                     "timestamp": time.strftime(
                         "%Y-%m-%dT%H:%M:%S.000Z",
                         time.gmtime(turn_ended_at))})
    with open(_TR94N, "w", encoding="utf-8") as _fh:
        for _r in rows:
            _fh.write(json.dumps(_r) + "\n")


# The CONTENDERS are alive too, or note_channel_refused prunes them as
# dead on the very next call and the count can never reach its gate - the
# book drops any contender pid that is not running.
_LIVE94 = (_OLD94, _NEW94w, _CHAN94, _CHAN94 + 1)
daemon.sessions.pid_alive = lambda pid: int(pid or 0) in _LIVE94
daemon.sessions.transcript_of = _tof94
try:
    # WITH window_pid, as hook.py sends it (15.1): it is what ties each
    # session to its window, and since 8.36 the evidence asks each window
    # about its OWN session.
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": "s94-old", "project_dir": _p94,
                       "cwd": _p94, "window_pid": _OLD94})
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": "s94-new", "project_dir": _p94,
                       "cwd": _p94, "window_pid": _NEW94w})
    _reg94 = time.time() - 120
    _write94("assistant", _reg94 + 60)
    _newcomer94()

    def _set94(**kw):
        """The newcomer's pid record, as reg_pid + mark_registered leave it."""
        rec = {"pid": _NEW94w, "at": _reg94 - 10, "registered": True,
               "registered_via": "session", "registered_at": _reg94,
               "why": "handover"}
        rec.update(kw)
        with daemon._lock:
            daemon.STATE.setdefault("pids", {})["%s|executor" % _k94] = rec
            (daemon.STATE.get("handover") or {}).pop(_k94, None)
            (daemon.STATE.get("orphaned_swap") or {}).pop(
                "%s|executor" % _k94, None)
            daemon.save_state()
        _newcomer94()

    _set94()
    check("with all three, the evidence says yes",
          _evid94(_p94, "executor", _OLD94, _NEW94w)[0], True)

    print("    8.36: AND THE ANSWER DOES NOT DEPEND ON WHICH WINDOW SPOKE LAST.")
    print("    2026-09-26 it flipped for twenty minutes, then passed on the")
    print("    witnesses swapped, because the newcomer's session was read")
    print("    off `last_session`. The old window speaks last here - a real")
    print("    hook, with its window_pid - and nothing may change")
    post_rc("/event", {"hook_event_name": "Notification", "role": "executor",
                       "session_id": "s94-old", "project_dir": _p94,
                       "cwd": _p94, "window_pid": _OLD94,
                       "notification_type": "idle_prompt",
                       "message": "waiting"})
    check("last_session now names the OLD window's session",
          daemon.last_session_id(_p94, "executor"), "s94-old")
    _l94 = _evid94(_p94, "executor", _OLD94, _NEW94w)
    check("and the evidence still says yes, naming each window's own "
          "session", (_l94[0], "s94-old" in (_l94[1] or "")
                      and "s94-new" in (_l94[1] or "")), (True, True))
    print("    a window with NO link is not evidence either way")
    with daemon._lock:
        _ws94 = (daemon.STATE.get("window_sessions") or {}).get(
            "%s|executor" % _k94) or {}
        _saved94 = _ws94.pop(str(_NEW94w), None)
        daemon.save_state()
    _n94 = _evid94(_p94, "executor", _OLD94, _NEW94w)
    check("refused, and it says the newcomer's session cannot be named",
          (_n94[0], "cannot be established" in (_n94[1] or "")),
          (False, True))
    with daemon._lock:
        if _saved94 is not None:
            _ws94[str(_NEW94w)] = _saved94
        daemon.save_state()

    print("    (a) THE 5.43 CONTROL: a window opened by hand has no `why` in")
    print("    its record, and its seat is not the bridge's to move")
    _set94(why="")
    _a94 = _evid94(_p94, "executor", _OLD94, _NEW94w)
    check("refused", _a94[0], False)
    check("and it says which condition failed",
          "not opened by the bridge" in (_a94[1] or ""), True)

    print("    (b) a newcomer that has finished a turn is WORKING, not empty -")
    print("    read from ITS transcript's turn end since 8.36")
    _set94()
    _newcomer94(turn_ended_at=_reg94 + 30)
    _b94 = _evid94(_p94, "executor", _OLD94, _NEW94w)
    check("refused", _b94[0], False)
    check("and it says why", "already finished a turn" in (_b94[1] or ""),
          True)

    print("    (c) the old window must be alive AND still writing. A `system`")
    print("    row is what a window that is NOT working still emits - the")
    print("    same distinction the death of a turn taught us (5.38)")
    _set94()
    _write94("system", _reg94 + 60)
    _c94 = _evid94(_p94, "executor", _OLD94, _NEW94w)
    check("refused on a transcript that only bookkeeping touched",
          _c94[0], False)
    _write94("assistant", _reg94 - 60)
    check("and refused when the writing is OLDER than the newcomer",
          _evid94(_p94, "executor", _OLD94, _NEW94w)[0], False)
    _write94("assistant", _reg94 + 60)
    daemon.sessions.pid_alive = lambda pid: int(pid or 0) == _NEW94w
    check("and refused when the old window is not alive",
          _evid94(_p94, "executor", _OLD94, _NEW94w)[0], False)
    daemon.sessions.pid_alive = lambda pid: int(pid or 0) in _LIVE94

    print("    and now the repair itself, through the refusal that carries")
    print("    the evidence every 45 s")
    _set94()
    _before94 = len(_j74("", _p94))
    # FIVE OF THEM, because the check is gated on the count: one refusal
    # is a leftover heartbeating and says nothing, a contender that keeps
    # coming back every 45 s is something alive starting it. That gate is
    # CHANNEL_REFUSE_TELL, and driving it rather than lowering it is what
    # makes this case about the live cadence.
    for _i94 in range(daemon.CHANNEL_REFUSE_TELL):
        daemon.note_channel_refused(_p94, "executor", _CHAN94, _OLD94,
                                    _NEW94w)
    _t94 = [(_r.get("text") or "") for _r in _j74("", _p94)[_before94:]]
    check("the seat is back with the window that has the thread",
          ((daemon.STATE.get("pids") or {}).get("%s|executor" % _k94)
           or {}).get("pid"), _OLD94)
    # 8.36: its OWN record with its own clock, and nothing in
    # STATE["handover"] - so never a `stop_after` either.
    check("the record is an orphaned swap of its own, never `stop_after`",
          (bool(((daemon.STATE.get("orphaned_swap") or {})
                 .get("%s|executor" % _k94) or {}).get("at")),
           ((daemon.STATE.get("handover") or {}).get(_k94) or {})),
          (True, {}))
    check("and the line says what was OBSERVED, not 2026-09-05's story",
          (any("Observed: pid %s (session s94-old" % _OLD94 in _t
               for _t in _t94),
           any("came up after its handover record had already been "
               "cleared" in _t for _t in _t94)), (True, False))
    check("so deliver_ex does not call this half absent",
          daemon.handover_swapping(_p94, "executor")
          if hasattr(daemon, "handover_swapping") else False, False)
    check("the journal says what it did",
          any("orphaned swap repaired" in _t for _t in _t94), True)
    check("and the old window is asked for its handoff - nothing is killed",
          bool(daemon.handover_pending_for(_p94, "executor")), True)

    print("    THE CONTROL ON THE ARMING: with (a) failing, the same call")
    print("    moves nothing and says so")
    with daemon._lock:
        (daemon.STATE.get("handover") or {}).pop(_k94, None)
        (daemon.STATE.get("handover_pending") or {}).pop(
            "%s|executor" % _k94, None)
        daemon.save_state()
    _set94(why="")
    _before94b = len(_j74("", _p94))
    for _i94 in range(daemon.CHANNEL_REFUSE_TELL):
        daemon.note_channel_refused(_p94, "executor", _CHAN94 + 1, _OLD94,
                                    _NEW94w)
    _t94b = [(_r.get("text") or "") for _r in _j74("", _p94)[_before94b:]]
    check("the seat stays with the newcomer",
          ((daemon.STATE.get("pids") or {}).get("%s|executor" % _k94)
           or {}).get("pid"), _NEW94w)
    check("and the line says NOT acted on, with the reason",
          any("NOT acted on" in _t and "not opened by the bridge" in _t
              for _t in _t94b), True)
finally:
    daemon.sessions.pid_alive = _alive94o
    daemon.sessions.transcript_of = _tof94o
    with daemon._lock:
        (daemon.STATE.get("handover") or {}).pop(_k94, None)
        (daemon.STATE.get("orphaned_swap") or {}).pop("%s|executor" % _k94,
                                                      None)
        (daemon.STATE.get("handover_pending") or {}).pop(
            "%s|executor" % _k94, None)
        (daemon.STATE.get("pids") or {}).pop("%s|executor" % _k94, None)
        (daemon.STATE.get("chan_refused") or {}).pop("%s|executor" % _k94,
                                                     None)
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n95. a replacement that would NOT close blocks the next one")
print("    close_stray_replacement ends a window that never came up before")
print("    the record is cleared - and it can fail. It returned the pid or")
print("    0 and BOTH callers threw the answer away, so a window that would")
print("    not close was announced in a line nobody reads and the next")
print("    replacement was permitted straight over it. That is the owner's")
print("    three windows arriving by a second road.")
print("    THE PATH MATTERS, and it is only one of the two: where a swap")
print("    times out, the OLD window's record goes back into `pids`, and")
print("    launch_guard reads only that - so the newcomer still sitting on")
print("    its dialog stops being visible to the one thing that refuses a")
print("    second window. On the other path the record stays, carries")
print("    `gave_up`, and the guard still sees the live pid.")
print("    -> DECISIONS.md 8.17")

_p95 = os.path.join(TMP, "would-not-close")
os.makedirs(_p95, exist_ok=True)
_k95 = canon(_p95)
_STUCK95, _OLD95 = 951001, 951002
_stuckfn95 = need78(daemon, "stuck_window", None)
_note95 = need78(daemon, "note_stuck_window", None)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p95: {}}})
_alive95o = daemon.sessions.pid_alive
_term95o = daemon.sessions.terminate_and_wait
_stop95o = daemon.sessions.stop
_dead95 = set()
_refuses95 = []


def _alive95(pid):
    return int(pid or 0) in (_STUCK95, _OLD95) and int(pid or 0) not in _dead95


def _term95(pid, *a, **k):
    """The window REFUSES to close - which is the whole case."""
    _refuses95.append(pid)


daemon.sessions.pid_alive = _alive95
daemon.sessions.terminate_and_wait = _term95
daemon.sessions.stop = lambda project, role, pid=None, wait=None, tree=True: True


def _arm95():
    """A replacement launched by the bridge that never came up, past the
    grace, with the swap on record - the timed-out path."""
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})["%s|executor" % _k95] = {
            "pid": _STUCK95, "at": time.time() - 5000, "registered": False,
            "why": "handover"}
        daemon.STATE.setdefault("handover", {})[_k95] = {
            "at": time.time() - 5000, "reason": "the wall",
            "waiting": ["executor"], "roles": ["executor"], "iteration": 2,
            "stop_after": {"executor": _STUCK95},
            "old_recs": {"executor": {"pid": _OLD95, "registered": True,
                                      "registered_via": "session"}}}
        (daemon.STATE.get("stuck_windows") or {}).pop("%s|executor" % _k95,
                                                      None)
        daemon.save_state()


try:
    _arm95()
    _before95 = len(_j74("", _p95))
    daemon.check_sessions(95)
    _t95 = [(_r.get("text") or "") for _r in _j74("", _p95)[_before95:]]
    check("the journal says the close did not work",
          any("did NOT close" in _t for _t in _t95), True)
    check("the window that would not close is on record",
          (_stuckfn95(_p95, "executor") or {}).get("pid"), _STUCK95)
    _g95 = daemon.launch_guard(_p95, "executor")
    check("and no second replacement may be opened over it",
          bool(_g95) and str(_STUCK95) in _g95, True)
    note("what the guard says", _g95)
    print("    the old window was given its record back all the same - the")
    print("    pair keeps working, which is the point of the timeout")
    check("the seat is the old window's again",
          ((daemon.STATE.get("pids") or {}).get("%s|executor" % _k95)
           or {}).get("pid"), _OLD95)

    print("    THE EXIT IS THE FACT, not a clock: the moment that process is")
    print("    gone the record has lost its evidence. A hold whose only exit")
    print("    is the thing it forbids is what 8.15 cost.")
    _dead95.add(_STUCK95)
    check("the record clears itself", _stuckfn95(_p95, "executor"), None)
    _g95b = daemon.launch_guard(_p95, "executor")
    check("and the refusal lifts",
          bool(_g95b) and str(_STUCK95) in _g95b, False)
    check("and it says so once, in the journal",
          any("may be opened again" in (_r.get("text") or "")
              for _r in _j74("", _p95)), True)

    print("    AND THE OTHER PATH, which is where the close's OWN record is")
    print("    the only one there is. With no swap on record")
    print("    handover_swap_timed_out never runs, so nothing writes a second")
    print("    time - and this section is what tells the two write sites")
    print("    apart. Found by SABOTAGE: with the close's write removed the")
    print("    suite stayed GREEN, because the section above was still")
    print("    covered by the other layer. A sabotage that changes nothing")
    print("    is a fix with no gate (rule 24) and a check that could not")
    print("    fail (rule 19).")
    _dead95.clear()
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})["%s|executor" % _k95] = {
            "pid": _STUCK95, "at": time.time() - 5000, "registered": False,
            "why": "handover"}
        (daemon.STATE.get("handover") or {}).pop(_k95, None)
        (daemon.STATE.get("stuck_windows") or {}).pop("%s|executor" % _k95,
                                                      None)
        daemon.save_state()
    _before95b = len(_j74("", _p95))
    daemon.check_sessions(95)
    check("the close failed here too",
          any("did NOT close" in (_r.get("text") or "")
              for _r in _j74("", _p95)[_before95b:]), True)
    check("and THIS path recorded it, with nothing else to do so",
          (_stuckfn95(_p95, "executor") or {}).get("pid"), _STUCK95)
    print("    the guard refuses here for TWO reasons at once - the pids")
    print("    record still names the live pid on this path - so the record")
    print("    above is what this section is really asserting")

    print("    AND THE SECOND WRITE LAYER, which is reached only when the")
    print("    close DECLINES. close_stray_replacement asks for positive")
    print("    evidence that the bridge opened this window - `why` from the")
    print("    launch record - and a record without it (an older one, or a")
    print("    rotate that wrote none) makes it return 0 without trying. The")
    print("    swap is undone all the same, sessions.stop is called all the")
    print("    same, and if THAT does not reach the window either, the only")
    print("    thing left to write the record is handover_swap_timed_out.")
    print("    Found by SABOTAGE: with that layer removed the suite stayed")
    print("    GREEN, because the section above is covered by the close's")
    print("    own write. Two write sites, two sections.")
    _dead95.clear()
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})["%s|executor" % _k95] = {
            "pid": _STUCK95, "at": time.time() - 5000, "registered": False}
        daemon.STATE.setdefault("handover", {})[_k95] = {
            "at": time.time() - 5000, "reason": "the wall",
            "waiting": ["executor"], "roles": ["executor"], "iteration": 3,
            "stop_after": {"executor": _STUCK95},
            "old_recs": {"executor": {"pid": _OLD95, "registered": True,
                                      "registered_via": "session"}}}
        (daemon.STATE.get("stuck_windows") or {}).pop("%s|executor" % _k95,
                                                      None)
        daemon.save_state()
    _refuses95[:] = []
    _before95c = len(_j74("", _p95))
    daemon.check_sessions(95)
    check("the close declined - no positive evidence that we opened it",
          _refuses95, [])
    check("so nothing said it did not close, because it never tried",
          any("did NOT close" in (_r.get("text") or "")
              for _r in _j74("", _p95)[_before95c:]), False)
    check("and the swap timeout is what recorded the window it could not "
          "stop", (_stuckfn95(_p95, "executor") or {}).get("pid"), _STUCK95)
    check("so the next replacement is refused by that record alone",
          str(_STUCK95) in (daemon.launch_guard(_p95, "executor") or ""),
          True)

    print("    THE CONTROL: a close that WORKS leaves no record and refuses")
    print("    nothing - or this is 'never replace anything' wearing a")
    print("    repair's name")
    _dead95.clear()
    _arm95()
    with daemon._lock:
        (daemon.STATE.get("stuck_windows") or {}).pop("%s|executor" % _k95,
                                                      None)
        daemon.save_state()
    _term95o2 = daemon.sessions.terminate_and_wait
    daemon.sessions.terminate_and_wait = lambda pid, *a, **k: _dead95.add(
        int(pid or 0))
    daemon.check_sessions(95)
    check("nothing is on record", _stuckfn95(_p95, "executor"), None)
    _g95c = daemon.launch_guard(_p95, "executor")
    check("and the guard does not name it",
          bool(_g95c) and str(_STUCK95) in _g95c, False)
    daemon.sessions.terminate_and_wait = _term95o2
finally:
    daemon.sessions.pid_alive = _alive95o
    daemon.sessions.terminate_and_wait = _term95o
    daemon.sessions.stop = _stop95o
    with daemon._lock:
        (daemon.STATE.get("stuck_windows") or {}).pop("%s|executor" % _k95,
                                                      None)
        (daemon.STATE.get("handover") or {}).pop(_k95, None)
        (daemon.STATE.get("pids") or {}).pop("%s|executor" % _k95, None)
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})

def _status96(project, role, sid, tokens):
    """A status line carrying `tokens` of input context, as the client posts
    it. The window is stated at 1M so plan_for reads a real wall."""
    post_rc("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": project, "project_dir": project},
        "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
        "context_window": {"context_window_size": 1000000,
                           "used_percentage": tokens / 10000.0,
                           "current_usage": {
                               "input_tokens": 10,
                               "cache_creation_input_tokens": 90,
                               "cache_read_input_tokens": int(tokens) - 100,
                               "output_tokens": 4000}}}})


def _compact96(project, role, sid, before, after=None):
    """One compaction as the client shows it: a reading at `before`, a
    PreCompact, and - when it LANDS - a smaller reading at `after`. With
    after=None the announcement is all there is, which is what a rate
    limit at 811k looked like on 2026-09-07."""
    _status96(project, role, sid, before)
    post_rc("/event", {"hook_event_name": "PreCompact", "role": role,
                       "session_id": sid, "project_dir": project,
                       "cwd": project})
    if after is not None:
        _status96(project, role, sid, after)


print("\n97. a compaction counts when it LANDED, not when it was announced")
print("    2026-09-07 08:02-11:04, a watched pair: under a rate limit the client")
print("    fired PreCompact every three minutes at 811k and never shrank -")
print("    the count went 5 -> 20 in an hour on nothing. 09-11 12:58 the")
print("    owner resumed the same session from the resume tab; 13:06")
print("    'compacted 20 times', replaced eight minutes after it started.")
print("    A PreCompact is the client ANNOUNCING; the floor written at the")
print("    Stop, or the first smaller reading, is it having happened.")
print("    -> DECISIONS.md 8.18")

_p97 = os.path.join(TMP, "announced-not-landed")
os.makedirs(_p97, exist_ok=True)
_k97 = canon(_p97)
_S97 = "s97-ex"
post("/config", {"projects": {A: {}, B: {}, C: {}, _p97: {}}})
post_rc("/loop", {"action": "start", "project": _p97})
for _r, _sid in (("executor", _S97), ("planner", "s97-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _p97, "cwd": _p97})
    register(_p97, _r, _sid)
try:
    for _i in range(5):
        _compact96(_p97, "executor", _S97, 990000)
    check("five announced and none landed: the count is ZERO",
          daemon.compactions_done(_p97, "executor"), 0)
    _sess97 = daemon.STATE["sessions"]["executor:%s" % _S97[:8]]
    check("and announcements alone do not make a handover",
          daemon.plan_for(_sess97, _p97)["do"] == "handover", False)
    check("the records are there - they are simply not counted",
          len((daemon.STATE.get("compactions") or {})
              .get("%s|executor" % _k97) or []), 5)

    print("    the turn ends with the size unchanged - and the Stop says so,")
    print("    once, because a count that quietly stops moving is the kind")
    print("    of thing nobody goes looking for")
    with open(os.path.join(_p97, "seen97.txt"), "w", encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    _t97 = threading.Thread(
        target=lambda: stop_hook(_p97, "executor", _S97,
                                 "piece done, still at 990k"),
        daemon=True)
    _t97.start()
    check("the Stop journalled the announcement that did not land",
          until(lambda: any("compaction attempted at 990k, not landed"
                            in (_r.get("text") or "")
                            for _r in _j74("", _p97)), 20), True)
    post("/verdict", {"project": _p97, "verdict": "continue",
                      "feedback": "Checked: seen97.txt\ncarry on"},
         secret=True)
    _t97.join(30)
    check("and the executor was released", _t97.is_alive(), False)
    check("still not counted", daemon.compactions_done(_p97, "executor"), 0)

    print("    THE CONTROL: five that LAND - the summary comes back smaller -")
    print("    and the wall is reached exactly as before")
    for _i in range(5):
        _compact96(_p97, "executor", _S97, 990000, 300000 + _i * 10000)
    check("five landed", daemon.compactions_done(_p97, "executor"), 5)
    _sess97 = daemon.STATE["sessions"]["executor:%s" % _S97[:8]]
    _status96(_p97, "executor", _S97, 400000)
    check("and NOW the plan is the fifth-compaction handover",
          daemon.plan_for(_sess97, _p97)["do"], "handover")
finally:
    post_rc("/loop", {"action": "stop", "project": _p97})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n96. the planner's wall is decided on the PLANNER's facts, above")
print("    every exit that is about the executor")
print("    The planner's branch stood below five early exits of assess(),")
print("    three of them the executor's - a command running for it, its")
print("    tail looking busy, its silence under the quiet - and a fourth,")
print("    verdict_in_flight, that lasts the executor's whole TURN. On")
print("    that pair the executor is busy nearly always, so the planner")
print("    compacted 8 times on 2026-09-12 with its handoff written 186")
print("    times and was never replaced. What holds it now is its own:")
print("    an unwritten handoff, its own open turn, a handover under way.")
print("    -> DECISIONS.md 8.18")

_p96 = os.path.join(TMP, "planner-wall-own-facts")
os.makedirs(_p96, exist_ok=True)
_k96 = canon(_p96)
_E96, _P96 = "s96-ex", "s96-pl"
_MARK96 = getattr(daemon, "HANDOFF_MARK", "HANDOFF WRITTEN:")
_turnopen96 = need78(daemon, "planner_turn_open", False)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p96: {}}})
post_rc("/loop", {"action": "start", "project": _p96})
for _r, _sid in (("executor", _E96), ("planner", _P96)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _p96, "cwd": _p96})
    register(_p96, _r, _sid)
# The planner's window is ALIVE, as a real one is: since 8.36 a handover
# names the window it replaces before it opens one.
stand_in(_p96, "planner")
_tof96o = daemon.sessions.transcript_of
_TR96 = os.path.join(TMP, "s96-pl.jsonl")
_tof96 = {"on": False}


def _transcript96(sid, cwd=None):
    return _TR96 if (_tof96["on"] and sid == _P96) else None


daemon.sessions.transcript_of = _transcript96
try:
    print("    the planner reaches its wall: five landed compactions")
    for _i in range(5):
        _compact96(_p96, "planner", _P96, 990000, 300000 + _i * 10000)
    _status96(_p96, "planner", _P96, 400000)
    _plsess = daemon.STATE["sessions"]["planner:%s" % _P96[:8]]
    check("its plan is handover", daemon.plan_for(_plsess, _p96)["do"],
          "handover")

    print("    and writes its handoff, on its own Stop hooks: the first one")
    print("    is asked, the second one answers")
    stop_hook(_p96, "planner", _P96, "a review, the first after the wall")
    _pf96 = (daemon.handover_pending_for(_p96, "planner") or {}).get("file")
    check("the demand is on record and names a file", bool(_pf96), True)
    if _pf96:
        try:
            os.makedirs(os.path.dirname(_pf96), exist_ok=True)
        except OSError:
            pass
        with open(_pf96, "w", encoding="utf-8") as _f:
            _f.write("# the planner's thread" + chr(10))
    stop_hook(_p96, "planner", _P96, "%s %s" % (_MARK96, _pf96 or "?"))
    check("written, and nothing holds the replacement any more",
          daemon.planner_wall_holds(_p96), False)

    print("    THE EXECUTOR IS BUSY in every way assess() used to read as")
    print("    'not now': a background command tracked, a report in PENDING")
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _E96, "project_dir": _p96, "cwd": _p96,
                       "tool_name": "Bash",
                       "tool_input": {"command": "py long_build.py",
                                      "run_in_background": True}})
    check("the executor has a command in flight",
          bool(daemon.inflight_live(_p96)), True)
    _t96 = threading.Thread(
        target=lambda: stop_hook(_p96, "executor", _E96,
                                 "report 1, waiting on the planner"),
        daemon=True)
    _t96.start()
    check("and a report sitting in PENDING",
          until(lambda: bool(daemon.PENDING.get(_k96)), 20), True)

    print("    THE CONTROL FIRST: the planner's OWN turn is open - its")
    print("    transcript written just now - and that DOES hold it")
    with open(_TR96, "w", encoding="utf-8") as _f:
        _f.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime()),
            "message": {"role": "assistant", "content": "reviewing"}})
            + chr(10))
    _tof96["on"] = True
    # bool(), not "> 0": since 8.36 the witness answers with the sentence
    # that names it, and a string compared with 0 raises.
    check("the witness sees the open turn", bool(_turnopen96(_p96, 180)),
          True)
    _before96 = len(launches())
    _res96 = daemon.assess(_p96)
    for _th in threading.enumerate():
        if _th.name.startswith("handover"):
            _th.join(30)
    check("assess held the planner for its own open turn",
          "its own turn is open" in json.dumps(_res96), True)
    check("and opened nothing", len(launches()) - _before96, 0)

    print("    now the planner is idle at its prompt - and the executor is")
    print("    STILL busy, exactly as above")
    _tof96["on"] = False
    check("the executor is still busy", bool(daemon.inflight_live(_p96))
          and bool(daemon.PENDING.get(_k96)), True)
    _before96 = len(launches())
    _res96 = daemon.assess(_p96)
    check("assess decided on the planner's replacement",
          "handing over the planner" in json.dumps(_res96), True)
    check("and a planner window was opened",
          until(lambda: len(launches()) - _before96 == 1, 30), True)
    _lr96 = launches()[-1] if len(launches()) > _before96 else {}
    check("for the planner, not the executor", _lr96.get("role"), "planner")
    check("the handover record names the planner alone",
          ((daemon.STATE.get("handover") or {}).get(_k96) or {})
          .get("roles"), ["planner"])
    print("    the replacement registers, and its own SessionStart drops the")
    print("    demand its predecessor answered - a different sid")
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "planner",
                       "session_id": "s96-pl2", "project_dir": _p96,
                       "cwd": _p96})
    check("the written record is gone with the session that wrote it",
          bool(daemon.handover_pending_for(_p96, "planner")), False)
finally:
    daemon.sessions.transcript_of = _tof96o
    with open(os.path.join(_p96, "seen96.txt"), "w", encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    post("/verdict", {"project": _p96, "verdict": "continue",
                      "feedback": "Checked: seen96.txt\ncarry on"},
         secret=True)
    _t96.join(30)
    for _th in threading.enumerate():
        if _th.name.startswith("handover"):
            _th.join(30)
    with daemon._lock:
        (daemon.STATE.get("handover") or {}).pop(_k96, None)
        (daemon.STATE.get("handover_pending") or {}).pop(
            "%s|planner" % _k96, None)
        daemon.save_state()
    post_rc("/loop", {"action": "stop", "project": _p96})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n98. the tool_use line is on disk BEFORE the PreToolUse hook, and the")
print("    background record still closes")
print("    Measured 2026-09-12 on this pair's own transcript: three records")
print("    that never closed, the tool_use line beginning 3 303, 3 021 and")
print("    5 518 bytes BEFORE the watermark PreToolUse had recorded, and a")
print("    forward read that could therefore never find it. The id stayed")
print("    empty, the second pass never ran, the record lived towards")
print("    BG_MAX_SEC, and inflight_live called the half busy: a pair stood")
print("    50 minutes on a `wait` with nothing running, and needs_you never")
print("    rang. Real order throughout (5.33): the line, the hook, the")
print("    notice, the deciding tick - never a hand-built record.")
print("    -> DECISIONS.md 8.20")

_p98 = os.path.join(TMP, "bg-line-before-hook")
os.makedirs(_p98, exist_ok=True)
_k98 = canon(_p98)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p98: {}}})
post_rc("/loop", {"action": "start", "project": _p98})
_s98 = "bg98-ex"
for _r, _sid in (("executor", _s98), ("planner", "bg98-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _p98, "cwd": _p98})
    register(_p98, _r, _sid)
_t98 = os.path.join(TMP, "bg98.jsonl")
_tof98o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t98 if sid == _s98 else "")
_notes98 = []
_notify98o = daemon.notify


def _notify98(*a, **kw):
    _notes98.append((a[0] if a else kw.get("kind"),
                     a[1] if len(a) > 1 else kw.get("text", ""),
                     kw.get("path")))
    return _notify98o(*a, **kw)


daemon.notify = _notify98


def _tw98(rows):
    with open(_t98, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _line98(cmd, tid):
    return {"type": "assistant", "timestamp": "2026-09-12T18:40:05.000Z",
            "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "Bash",
                 "input": {"command": cmd, "run_in_background": True}}]}}


def _notice98(tid):
    return {"type": "queue-operation", "operation": "enqueue",
            "content": "<task-notification>\n<task-id>x</task-id>\n"
                       "<tool-use-id>%s</tool-use-id>\n<status>completed"
                       "</status>\n</task-notification>" % tid}


def _bg98():
    return [(k, v) for k, v in
            ((daemon.STATE.get("inflight") or {}).get(_k98) or {}).items()
            if str(k).startswith("bg:")]


def _rec98(cmd):
    # Each section asks after ITS OWN record: a section that asked whether
    # the pair's whole list was empty was red whenever an EARLIER section's
    # record had leaked, and the sabotages' red sets overlapped for it.
    for _k, _v in _bg98():
        if (_v.get("cmd") or "") == cmd:
            return _v
    return None


try:
    _tw98([{"type": "user", "message": {"content": "start"},
            "timestamp": "2026-09-12T18:40:00.000Z"}])

    print("   (a) THE MEASURED ORDER, an older client that sends no id: the")
    print("   line is written, THEN the hook runs")
    _CMD98a = "O=/x/green1 && py test_multipair.py > $O/g.txt 2>&1"
    _TID98a = "toolu_98LINEBEFOREHOOK"
    _tw98([_line98(_CMD98a, _TID98a)])
    _size98 = os.path.getsize(_t98)
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _s98, "project_dir": _p98, "cwd": _p98,
                       "tool_name": "Bash",
                       "tool_input": {"command": _CMD98a,
                                      "run_in_background": True}})
    _rec98a = _rec98(_CMD98a) or {}
    check("the record was made", bool(_rec98a), True)
    check("with the watermark AFTER the line - the shape of the leak",
          int(_rec98a.get("tpos") or 0) >= _size98, True)
    check("and no id, because this client sent none",
          _rec98a.get("tid") or "", "")
    _tw98([_notice98(_TID98a)])
    daemon.check_background()
    check("the deciding tick closed it all the same",
          _rec98(_CMD98a), None)
    check("and the pair reads as idle, honestly",
          bool(daemon.inflight_live(_p98)), False)
    check("the journal says the id was recovered from the transcript",
          any("recovered the id of a background record" in
              (_r.get("text") or "") for _r in _j74("", _p98)), True)
    print("   and a `wait` on a pair with nothing running RINGS - asked of")
    print("   the receiver, the notify call, not of a flag")
    with open(os.path.join(_p98, "seen98.txt"), "w", encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    _t98w = threading.Thread(target=stop_hook,
                             args=(_p98, "executor", _s98, "the leak case"),
                             daemon=True)
    _t98w.start()
    check("a report is waiting",
          until(lambda: bool(_k98 in daemon.PENDING), 25), True)
    del _notes98[:]
    post("/verdict", {"project": _p98, "verdict": "wait",
                      "feedback": "Checked: seen98.txt\nWaiting."},
         secret=True)
    _t98w.join(40)
    check("needs_you reached the receiver, about this pair",
          until(lambda: any(k == "needs_you" and (p is None or
                                                  canon(p) == _k98)
                            for k, _t, p in _notes98), 20), True)

    print("   (b) THIS client sends tool_use_id with the hook, and then")
    print("   there is nothing to scan for - proven on a transcript that")
    print("   never receives the tool_use line at all")
    _CMD98b = "py tools/long_build.py --seed 7"
    _TID98b = "toolu_98FROMPAYLOAD"
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _s98, "project_dir": _p98, "cwd": _p98,
                       "tool_name": "Bash", "tool_use_id": _TID98b,
                       "tool_input": {"command": _CMD98b,
                                      "run_in_background": True}})
    check("the record carries the id from the payload at once",
          (_rec98(_CMD98b) or {}).get("tid"), _TID98b)
    _tw98([_notice98(_TID98b)])
    daemon.check_background()
    check("and closes on the notice with no tool_use line anywhere",
          _rec98(_CMD98b), None)

    print("   (c) a notice BEHIND the tail. A record that leaked for hours")
    print("   has its notice buried under everything the session wrote")
    print("   after it - measured 2026-09-13, five notices at 1.6-2.6 MB of")
    print("   a 4.7 MB file, the 2 MB tail starting at 2.6 MB. The sweep")
    print("   from the watermark finds it, once, and remembers where it")
    print("   stopped. The id comes with the hook here, so the only thing")
    print("   under test is where the notice is looked for.")
    _CMD98c = "py tools/overnight.py"
    _TID98c = "toolu_98BEHINDTHETAIL"
    _tw98([_line98(_CMD98c, _TID98c)])
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _s98, "project_dir": _p98, "cwd": _p98,
                       "tool_name": "Bash", "tool_use_id": _TID98c,
                       "tool_input": {"command": _CMD98c,
                                      "run_in_background": True}})
    _tw98([_notice98(_TID98c)])
    _filler = {"type": "assistant", "timestamp": "2026-09-12T19:00:00.000Z",
               "message": {"content": [{"type": "text",
                                        "text": "x" * 4000}]}}
    _tw98([_filler] * 600)                     # ~2.4 MB after the notice
    check("PRECONDITION: the notice is behind the 2 MB tail",
          os.path.getsize(_t98) - 2 * 1024 * 1024 > 0, True)
    daemon.check_background()
    check("the sweep found it and closed the record",
          _rec98(_CMD98c), None)
finally:
    daemon.sessions.transcript_of = _tof98o
    daemon.notify = _notify98o
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(_k98, None)
        daemon.save_state()
    daemon.PROCTRACK.pop(_k98, None)
    post_rc("/loop", {"action": "stop", "project": _p98})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})

print("\n99. a task delivered onto a blocked Stop hook is NOT 'mid-turn', and a")
print("    task booked mid-turn is settled by the RECEIVER - the transcript")
print("    2026-09-12/13, four `done` verdicts in a row handed a task over")
print("    again ('had not been taken up - handing it over again') and all")
print("    four tasks had been taken up and executed already: the book was")
print("    filled by PENDING (a report awaiting its verdict - the turn is")
print("    OVER, the next one reads the queue first) and emptied by nothing")
print("    but the next done. Report 341 was a turn spent on the piece-4")
print("    specification from 00:24:11, executed at 01:49. Real order:")
print("    /task, the recording channel, the Stop hook, the verdict, and")
print("    the transcript as the witness. -> DECISIONS.md 8.22")
_p99 = os.path.join(TMP, "task-book")
os.makedirs(_p99, exist_ok=True)
_k99 = canon(_p99)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p99: {}}})
post_rc("/loop", {"action": "start", "project": _p99})
_s99 = "book99-ex"
for _r, _sid in (("executor", _s99), ("planner", "book99-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _p99, "cwd": _p99})
    register(_p99, _r, _sid)
_t99 = os.path.join(TMP, "book99.jsonl")
_tof99o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (lambda sid, cwd=None:
                                 _t99 if sid == _s99 else "")
_REHAND = "It is still the work in hand"


def _now99():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def _tw99(rows):
    with open(_t99, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _assistant99(text="working"):
    return {"type": "assistant", "timestamp": _now99(),
            "message": {"content": [{"type": "text", "text": text}]}}


def _envelope99(text):
    # THE FORM THE CLIENT WRITES, built from what was actually delivered:
    # the recording channel keeps the body the bridge POSTed - content AND
    # meta - and daemon.channel_envelope renders the tag, so this fixture
    # cannot drift from the delivery it stands for. Until 2026-09-27 it was
    # a literal of the witness's own constant, without the from="planner"
    # every planner task carries since 2026-09-25: this case stayed green
    # on a witness that had not found one live booked task. -> 8.41
    for _d in reversed(_exec99()):
        if text in (_d.get("content") or ""):
            return "%s\n%s\n</channel>" % (
                daemon.channel_envelope(_d.get("meta") or {}), _d["content"])
    return "(no delivery carried %r - nothing to write)" % text


def _arrival99(text):
    # THE IDLE-WINDOW SHAPE: a task that lands on a window between turns
    # (or on a blocked Stop hook) is written as a `user` row when the turn
    # starts. Measured on this pair's transcript, rows 2900/2911 for the
    # task of 00:24:11.
    return {"type": "user", "isMeta": True, "timestamp": _now99(),
            "message": {"role": "user", "content": _envelope99(text)}}


def _absorbed99(text):
    # THE MID-TURN SHAPE - the case the book exists for. The client writes
    # `enqueue`, then `remove` with its own word for what happened, then an
    # `attachment` carrying the prompt; NO `user` row, ever. Rows 3607 /
    # 3616 / 3619 for the task of 02:40:32, which the first witness could
    # not see (it asked for a `user` row) and so never settled. Same
    # structure, no personal data.
    env = _envelope99(text)
    return [{"type": "queue-operation", "operation": "enqueue",
             "timestamp": _now99(), "content": env},
            {"type": "queue-operation", "operation": "remove",
             "timestamp": _now99(), "content": env,
             "reason": "absorbed_mid_turn"},
            {"type": "attachment", "timestamp": _now99(),
             "attachment": {"type": "queued_command", "prompt": env,
                            "commandMode": "prompt",
                            "origin": {"kind": "channel", "server": "bridge"},
                            "isMeta": True}}]


def _book99():
    return list((daemon.STATE.get("tasks_open") or {}).get(_k99) or [])


def _exec99():
    return DELIVERED.get((_k99, "executor")) or []


def _tasks99(text):
    return [d for d in _exec99()
            if text in (d.get("content") or d.get("text") or json.dumps(d))]


def _rehands99():
    return [d for d in _exec99() if _REHAND in json.dumps(d, ensure_ascii=False)]


def _rehand_lines99():
    return sum(1 for r in _j74("", _p99)
               if "handing it over again" in (r.get("text") or ""))


def _task99(text):
    post("/task", {"project": _p99, "instructions": text}, secret=True)
    return until(lambda: bool(_tasks99(text)), 20)


def _done99(text):
    with open(os.path.join(_p99, "seen99.txt"), "w", encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    post("/verdict", {"project": _p99, "verdict": "done",
                      "feedback": "Checked: seen99.txt\n%s" % text},
         secret=True)


try:
    _tw99([_assistant99("first turn")])
    print("   (c) THE CONTROL, the case the book exists for (5.13): a task")
    print("   arrives while the turn is really running, the turn never reads")
    print("   it, and the done hands it over again - ONCE, naming it")
    _T99c = "task-99c: build the thing the turn never saw"
    DELIVERED[(_k99, "executor")] = []
    check("the executor was writing just now, so this counts as mid-turn",
          bool(daemon.executor_wrote_recently(_p99, 180)), True)
    check("the task reached the executor's channel", _task99(_T99c), True)
    # the booking is written AFTER the delivery returns, on the delivery
    # thread - so the book is asked with the same short wait the negative
    # in (a) is given, and both sides measure the same thing
    check("and is on the book, with a watermark",
          until(lambda: bool(_book99()), 3) and
          [(_T99c in (b.get("text") or ""), "tpos" in b) for b in _book99()],
          [(True, True)])
    _th = threading.Thread(target=stop_hook,
                           args=(_p99, "executor", _s99, "report c"), daemon=True)
    _th.start()
    check("the report is pending", until(lambda: _k99 in daemon.PENDING, 25), True)
    _done99("accepted c")
    _th.join(40)
    check("the done handed the unseen task over again - the control fires",
          until(lambda: len(_rehands99()) == 1, 6), True)
    check("and the journal names the task and why",
          any("handing it over again" in (r.get("text") or "")
              and "its text is not in the executor's transcript" in (r.get("text") or "")
              and "delivered " in (r.get("text") or "")
              for r in _j74("", _p99)), True)
    check("the book is empty after one re-hand - never a third time", _book99(), [])
    _n_c = len(_rehands99())

    print("   (a) a task delivered while a REPORT IS PENDING - the turn is over,")
    print("   the executor stands on its blocked Stop hook, the next turn reads")
    print("   the task first. The measured shape of 00:24:11")
    _T99a = "task-99a: the next piece, delivered onto the blocked hook"
    DELIVERED[(_k99, "executor")] = []
    _th = threading.Thread(target=stop_hook,
                           args=(_p99, "executor", _s99, "report a"), daemon=True)
    _th.start()
    check("PRECONDITION: the report is pending",
          until(lambda: _k99 in daemon.PENDING, 25), True)
    check("the task reached the executor's channel while it was pending",
          _task99(_T99a), True)
    check("it is NOT booked as mid-turn (the booking's own 3 s window)",
          until(lambda: bool(_book99()), 3), False)
    _jl_before = _rehand_lines99()
    _rh_before = len(_rehands99())
    _done99("accepted a")
    _th.join(40)
    # the next turn takes it: the client writes the arrival, the turn goes on
    _tw99([_arrival99(_T99a), _assistant99("working on a")])
    check("the receiver got the task exactly once",
          until(lambda: len(_tasks99(_T99a)) == 1, 5) and len(_tasks99(_T99a)),
          1)
    check("nothing was handed over again (the control's own 6 s window)",
          until(lambda: len(_rehands99()) > _rh_before, 6), False)
    check("and no journal line says it was", _rehand_lines99() - _jl_before, 0)

    print("   (b) a task that really arrived mid-turn and the turn ABSORBED it -")
    print("   the client's own shape for that: enqueue, remove (absorbed_mid_turn),")
    print("   an attachment carrying the prompt, and the turn going on. No user row")
    _T99b = "task-99b: mid-turn, absorbed by the turn as an attachment"
    DELIVERED[(_k99, "executor")] = []
    _tw99([_assistant99("still going")])
    check("mid-turn by the transcript", bool(daemon.executor_wrote_recently(_p99, 180)), True)
    check("the task reached the channel", _task99(_T99b), True)
    check("and was booked",
          until(lambda: bool(_book99()), 3) and
          [_T99b in (b.get("text") or "") for b in _book99()], [True])
    print("   THE RECEIVER'S FORM (8.41): the entry below is rendered from what")
    print("   the channel was handed, and has to open the way a live executor")
    print("   transcript does - the literal is the one found there on disk")
    _LIVE99 = '<channel source="bridge" kind="task" from="planner">'
    check("the fixture writes the envelope the live delivery produces",
          _envelope99(_T99b).startswith(_LIVE99 + "\n"), True)
    _re99 = getattr(daemon, "TASK_ENVELOPE_RE", None)
    check("the witness knows a task by both envelopes found on disk, and "
          "nothing else by them",
          [bool(_re99 and _re99.search(_e)) for _e in (
              '<channel source="bridge" kind="task">', _LIVE99,
              '<channel source="bridge" kind="verdict">',
              '<channel source="bridge" kind="tasks">')],
          [True, True, False, False])
    _tw99(_absorbed99(_T99b) + [_assistant99("took it, working"),
                                _assistant99("finishing")])
    _th = threading.Thread(target=stop_hook,
                           args=(_p99, "executor", _s99, "report b"), daemon=True)
    _th.start()
    check("the report is pending", until(lambda: _k99 in daemon.PENDING, 25), True)
    check("the Stop settled the book against the transcript (attachment shape)",
          until(lambda: _book99() == [], 10), True)
    check("and said so, naming the attachment shape",
          any("was taken up by the turn that followed" in (r.get("text") or "")
              and "absorbed mid-turn" in (r.get("text") or "")
              and _T99b[:20] in (r.get("text") or "")
              for r in _j74("", _p99)), True)
    _jl_before = _rehand_lines99()
    _rh_before = len(_rehands99())
    _done99("accepted b")
    _th.join(40)
    check("the done handed nothing over again (attachment shape)",
          until(lambda: len(_rehands99()) > _rh_before, 6), False)
    check("the receiver got the task exactly once (attachment shape)", len(_tasks99(_T99b)), 1)
    check("and no journal line says it was handed over again (attachment shape)",
          _rehand_lines99() - _jl_before, 0)

    print("   (b2) the same, in the idle-window shape - a `user` row: what a task")
    print("   booked by the old code, or one that landed between turns while the")
    print("   180 s grace still called the executor busy, looks like")
    _T99b2 = "task-99b2: booked, and read as a user row by the next turn"
    DELIVERED[(_k99, "executor")] = []
    _tw99([_assistant99("still going")])
    check("mid-turn by the transcript", bool(daemon.executor_wrote_recently(_p99, 180)), True)
    check("the task reached the channel", _task99(_T99b2), True)
    check("and was booked",
          until(lambda: bool(_book99()), 3) and
          [_T99b2 in (b.get("text") or "") for b in _book99()], [True])
    _tw99([_arrival99(_T99b2), _assistant99("took it, working")])
    _th = threading.Thread(target=stop_hook,
                           args=(_p99, "executor", _s99, "report b2"), daemon=True)
    _th.start()
    check("the report is pending", until(lambda: _k99 in daemon.PENDING, 25), True)
    check("the Stop settled the book against the transcript (user shape)",
          until(lambda: _book99() == [], 10), True)
    check("and said so, naming the user shape",
          any("was taken up by the turn that followed" in (r.get("text") or "")
              and "a channel entry at" in (r.get("text") or "")
              and _T99b2[:20] in (r.get("text") or "")
              for r in _j74("", _p99)), True)
    _jl_before = _rehand_lines99()
    _rh_before = len(_rehands99())
    _done99("accepted b2")
    _th.join(40)
    check("the done handed nothing over again (user shape)",
          until(lambda: len(_rehands99()) > _rh_before, 6), False)
    check("the receiver got the task exactly once (user shape)", len(_tasks99(_T99b2)), 1)
    check("and no journal line says it was handed over again (user shape)",
          _rehand_lines99() - _jl_before, 0)

    print("   (d) an `enqueue` alone is NOT taken: the row the client writes at")
    print("   delivery, before any turn has read it, must not settle the book")
    _T99d = "task-99d: enqueued, never read"
    DELIVERED[(_k99, "executor")] = []
    _tw99([_assistant99("still going")])
    check("the task reached the channel", _task99(_T99d), True)
    check("and was booked", until(lambda: bool(_book99()), 3), True)
    _tw99([_absorbed99(_T99d)[0], _assistant99("unrelated work goes on")])
    _th = threading.Thread(target=stop_hook,
                           args=(_p99, "executor", _s99, "report d"), daemon=True)
    _th.start()
    check("the report is pending", until(lambda: _k99 in daemon.PENDING, 25), True)
    check("the enqueue row settled nothing - the book still holds it",
          until(lambda: _book99() == [], 3), False)
    _done99("accepted d")
    _th.join(40)
    check("and the done handed it over, once",
          until(lambda: len(_rehands99()) == 1, 6), True)
finally:
    daemon.sessions.transcript_of = _tof99o
    with daemon._lock:
        (daemon.STATE.get("tasks_open") or {}).pop(_k99, None)
        daemon.save_state()
    post_rc("/loop", {"action": "stop", "project": _p99})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n100. a model whose allowance is spent is HELD at once, said ONCE and")
print("     never revived - and a death the revive itself caused backs off")
print("     instead of spinning")
print("    2026-09-23 08:47-10:31, a watched project's planner: 24 StopFailures,")
print("    every one filed as 'rate_limit', with the client's own sentence one")
print("    field away - You've reached your <model> limit. The report was")
print("    handed back 21 times, every hand-back died within two seconds, every")
print("    death wrote a fresh record, so every line read 'attempt 1 of 3';")
print("    eleven copies of one message went to the chat, and the pair was")
print("    held only by the silence counter, after three reports of twenty")
print("    minutes. On 2026-09-07 the same reset ran 328 hand-backs, all of")
print("    them 'attempt 1'. Real order: the status lines, a Stop hook")
print("    blocked on its report, a StopFailure POSTed in the kept payload's")
print("    own shape, then the real check_lost_turn tick. Every check below")
print("    compares one mechanism's output with its own input, and every part")
print("    sets up its own preconditions, so a broken mechanism reddens its")
print("    own checks and not its neighbours'. -> DECISIONS.md 8.23")
_p100 = os.path.join(TMP, "model-limit")
_q100 = os.path.join(TMP, "rate-limit")
for _d in (_p100, _q100):
    os.makedirs(_d, exist_ok=True)
_k100, _kq100 = canon(_p100), canon(_q100)
_PK100, _EK100 = "%s|planner" % _k100, "%s|executor" % _k100
_QK100 = "%s|planner" % _kq100
# One model per chain, so a spent executor has nowhere to drop to and the
# hold is what is under test, not the older rotation to the next model.
post("/config", {"projects": {A: {}, B: {}, C: {}, _q100: {}, _p100: {
    "chains": {"executor": ["opus"], "planner": ["fable"]}}}})
_ex100, _pl100, _exq100, _plq100 = ("limit100-ex", "limit100-pl",
                                    "rate100-ex", "rate100-pl")
for _proj, _pair in ((_p100, (_ex100, _pl100)), (_q100, (_exq100, _plq100))):
    post_rc("/loop", {"action": "start", "project": _proj})
    for _r, _sid in zip(("executor", "planner"), _pair):
        post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                           "session_id": _sid, "project_dir": _proj,
                           "cwd": _proj})
        register(_proj, _r, _sid)
daemon.CFG["telegram"] = {"token": "test-token", "chat_id": "42",
                          "pinned_message_id": 0}
_thr100 = dict(daemon.CFG.get("thresholds") or {})
# Long enough that a Stop hook still standing at a check below is standing
# on the REVIEW and on nothing else. The grace a dead turn is given is
# SHORT and really waited out, rather than the record being moved into the
# past: moving it back 400 s makes every Stop of the last 400 s read as
# movement after the death, and the first draft of this case was green on
# the old daemon for exactly that reason.
# And the idle damper off: these reports are short on purpose and no verdict
# comes back between most of them, which is the empty exchange the damper
# exists to hold - the third one here was held by it, on the first green run,
# as it should have been. Same switch test_wall_handover uses, for the same
# reason.
_GRACE100 = 2.0
daemon.CFG["thresholds"].update({"review_timeout": 120,
                                 "channel_silence_warn": 110,
                                 "stopfail_grace": _GRACE100,
                                 "idle_hold": 0})
_SAID100 = ("You've reached your Fable limit. Run /usage-credits to "
            "continue or switch models with /model.")
_SAIDX100 = ("You've reached your Opus limit. Run /usage-credits to "
             "continue or switch models with /model.")
_API100 = "429 " + json.dumps(
    {"type": "error",
     "error": {"type": "rate_limit_error",
               "message": "This request would exceed your account's rate "
                          "limit. Please try again later."},
     "request_id": "req_test100"}, separators=(",", ":"))
_HOOK100 = {}


def _status100(proj, role, sid, display, mid):
    post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": proj, "project_dir": proj},
        "model": {"display_name": display, "id": mid},
        "context_window": {
            "context_window_size": 1000000, "used_percentage": 20.0,
            "current_usage": {"input_tokens": 10,
                              "cache_creation_input_tokens": 90,
                              "cache_read_input_tokens": 199900,
                              "output_tokens": 100}}}})


def _death100(proj, role, sid, said):
    """A StopFailure with the keys of the payload the bridge kept on the
    day, in its order - the client's fields, then the two hook.py adds.
    Nothing personal: every path is this suite's own. `said` None is the
    same payload with the client's sentence taken out."""
    body = {"session_id": sid,
            "transcript_path": os.path.join(TMP, "%s.jsonl" % sid),
            "cwd": proj, "prompt_id": "prompt-%s" % sid,
            "effort": {"level": "max"},
            "hook_event_name": "StopFailure", "error": "rate_limit",
            "error_details": _API100}
    if said is not None:
        body["last_assistant_message"] = said
    body.update({"project_dir": proj, "role": role})
    return post("/event", body)


def _to100(proj, role):
    return DELIVERED.get((canon(proj), role)) or []


def _chat100(said):
    return [t for t in tg_texts() if said in t]


def _kept100(seen=None):
    rows = ((daemon.STATE.get("held") or {}).get(_k100)
            or {}).get("reports") or []
    return [r.get("n") for r in rows
            if seen is None or bool(r.get("seen")) == seen]


def _nums100(items):
    return sorted(sum((re.findall(r"Executor report (\d+):",
                                  body_of((d or {}).get("content") or ""))
                       for d in items), []))


def _pending100(proj):
    return str((((daemon.PENDING.get(canon(proj)) or {}).get("meta") or {})
                .get("report")))


def _iter100(proj):
    return int(((daemon.STATE.get("loops") or {}).get(canon(proj))
                or {}).get("iteration") or 0)


def _pass100(key):
    """The grace, really waited out - past the record's own stamp, which
    after a backoff is in the future, that being the point of a backoff.
    And the chat's memory of what it said is aged by the three minutes the
    real storm had between deaths, or the repeat window would hide a second
    message the real clock let through."""
    with daemon._lock:
        for _fp in list(daemon.STATE.get("said") or {}):
            if _fp.startswith(key.rsplit("|", 1)[0] + "|"):
                daemon.STATE["said"][_fp] -= 400
        daemon.save_state()
        rec = (daemon.STATE.get("stopfail") or {}).get(key) or {}
        due = float(rec.get("at") or time.time()) + _GRACE100
    time.sleep(max(0.0, due - time.time()) + 0.1)


def _clean100():
    """A part's own preconditions: no limit on record, no dead turn, no
    hold - set here rather than inherited, so a mechanism a sabotage broke
    in one part cannot redden the checks of the next."""
    with daemon._lock:
        for _k in (_PK100, _EK100):
            (daemon.STATE.get("model_limit") or {}).pop(_k, None)
            (daemon.STATE.get("stopfail") or {}).pop(_k, None)
        (daemon.STATE.get("held") or {}).pop(_k100, None)
        daemon.save_state()
    if (daemon.STATE.get("paused") or {}).get(_k100):
        daemon.resume_project(_p100)


def _stop100(proj, sid, text, tag):
    _t0 = time.time()
    _out = stop_hook(proj, "executor", sid, text)
    _HOOK100[tag] = {"out": _out, "secs": time.time() - _t0}


def _hook100(proj, sid, text, tag):
    _th = threading.Thread(target=_stop100, args=(proj, sid, text, tag),
                           daemon=True)
    _th.start()
    return _th


def _answer100(proj, text):
    with open(os.path.join(proj, "seen100.txt"), "w", encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    post("/verdict", {"project": proj, "verdict": "done",
                      "feedback": "Checked: seen100.txt\n%s" % text},
         secret=True)


try:
    _status100(_p100, "executor", _ex100, "Opus 5 (1M context)",
               "claude-opus-5")
    _status100(_p100, "planner", _pl100, "Fable 5.1", "claude-fable-5-1")
    print("   (a) the report is out and the executor's Stop hook waits on it")
    _it100 = _iter100(_p100)
    _hook100(_p100, _ex100, "report one: the map script is ready", "a")
    check("the report reached the planner and waits for its verdict",
          until(lambda: _pending100(_p100) == str(_it100 + 1), 20), True)
    _n1 = str(_it100 + 1)
    tg_reset()
    print("   the planner's turn dies with the client's sentence in it")
    _death100(_p100, "planner", _pl100, _SAID100)
    check("the StopFailure line quotes the client's sentence word for word",
          any(_SAID100 in (r.get("text") or "")
              for r in _j74("stopped with an error", _p100)), True)
    _hold100 = (daemon.STATE.get("paused") or {}).get(_k100) or {}
    check("the pair is held at once, by the model limit (planner)",
          _hold100.get("by"), "model_limit")
    check("and the hold names the role, the model and the sentence",
          [_x in (_hold100.get("why") or "")
           for _x in ("planner", "Fable", _SAID100)], [True, True, True])
    check("the journal says PAIR HELD, with the sentence",
          any(_SAID100 in (r.get("text") or "")
              for r in _j74("PAIR HELD", _p100)), True)
    check("ONE message reaches the chat, with the sentence (planner)",
          until(lambda: len(_chat100(_SAID100)) >= 1, 5)
          and len(_chat100(_SAID100)), 1)
    _m100 = (_chat100(_SAID100) or [""])[0]
    check("it names the role, the model and the three ways it ends",
          ["planner" in _m100, "Fable" in _m100, "/model" in _m100,
           "chain" in _m100, "reset" in _m100], [True] * 5)
    check("the executor's Stop hook is let go, not held for the review",
          until(lambda: "a" in _HOOK100, 10), True)
    check("and the journal says why it was let go",
          until(lambda: bool(_j74("is not being reviewed now", _p100)), 5),
          True)
    check("the report it was waiting on is kept, as one the window has seen",
          _kept100(seen=True), [_n1])

    print("   the tick, past the grace. The old path handed the report back")
    print("   here, into a window that dies on every turn - twice, below")
    _base100 = len(_to100(_p100, "planner"))
    _said100 = len(_chat100(_SAID100))
    _dl100 = len(_j74("of this episode", _p100))
    _revs100 = []
    for _round in range(2):
        _pass100(_PK100)
        daemon.check_lost_turn(_p100)
        # read AT the tick: the next death is what used to wipe it
        _revs100.append(((daemon.STATE.get("stopfail") or {}).get(_PK100)
                         or {}).get("revives"))
        # what a hand-back into that window meets
        _death100(_p100, "planner", _pl100, _SAID100)
    note("hand-backs into the spent window over two ticks",
         len(_to100(_p100, "planner")) - _base100)
    note("the revive count, read at each tick", _revs100)
    check("no report is handed back into the spent window",
          len(_to100(_p100, "planner")) - _base100, 0)
    check("and no revive is counted at either tick (planner)", _revs100,
          [None, None])
    # held_for_limit's own line, by its own tail: a turn_lost line
    # embeds revive_lost_turn's answer, which says "stood aside" too
    _st100 = len(_j74("and a turn handed to it now dies at once", _p100))
    check("the tick says why it stood aside", _st100 >= 1, True)
    check("and says it once, not once per tick", _st100 <= 1, True)
    check("and no second message, three deaths and two ticks later",
          len(_chat100(_SAID100)) - _said100, 0)
    check("each later death is written down, and nobody is told",
          len(_j74("of this episode", _p100)) - _dl100, 2)
    check("revive_lost_turn itself stands aside, whoever calls it",
          daemon.revive_lost_turn(_p100, "planner"),
          "stood aside - its model has no allowance left")

    print("   (b) a turn that ends while the planner is out: made into a")
    print("   report and kept - its Stop hook is not held on it")
    _base100 = len(_to100(_p100, "planner"))
    _n2 = str(_iter100(_p100) + 1)
    _hook100(_p100, _ex100, "report two: done while the planner was out", "b")
    check("a Stop hook is not held on a report nobody can read",
          until(lambda: "b" in _HOOK100, 10), True)
    check("that report is kept as well, as one the window never saw",
          _n2 in _kept100(seen=False), True)
    check("and a line says it was made and kept",
          len(_j74("Report %s made and kept" % _n2, _p100)), 1)
    check("and it is not handed to the window that cannot read it",
          len(_to100(_p100, "planner")) - _base100, 0)

    print("   (c) the planner's model is switched in its own window: the")
    print("   status line says so, and that ends it - once")
    _was100 = sorted(_kept100())
    _base100 = len(_to100(_p100, "planner"))
    _status100(_p100, "planner", _pl100, "Opus 5 (1M context)",
               "claude-opus-5")
    check("a status line on another model family lifts the hold",
          ((daemon.STATE.get("paused") or {}).get(_k100) or {}).get("by"),
          None)
    _new100 = _to100(_p100, "planner")[_base100:]
    check("what was kept went to the planner once, and exactly that",
          (len(_new100), _nums100(_new100)), (1, _was100))
    check("and a line says the limit is over and what went over",
          len(_j74("the model limit is over", _p100)), 1)
    _base100 = len(_to100(_p100, "planner"))
    _status100(_p100, "planner", _pl100, "Opus 5 (1M context)",
               "claude-opus-5")
    check("a second status line hands nothing over again",
          len(_to100(_p100, "planner")) - _base100, 0)

    print("   (c2) the same, ended by a finished turn instead: the report the")
    print("   window died on is in its own conversation and is NOT sent")
    print("   again; one it never saw is")
    _clean100()
    _status100(_p100, "planner", _pl100, "Fable 5.1", "claude-fable-5-1")
    _it100 = _iter100(_p100)
    _hook100(_p100, _ex100, "report three: the one it died on", "c2a")
    check("the next report reached the planner",
          until(lambda: _pending100(_p100) == str(_it100 + 1), 20), True)
    _n3, _n4 = str(_it100 + 1), str(_it100 + 2)
    _death100(_p100, "planner", _pl100, _SAID100)
    until(lambda: "c2a" in _HOOK100, 10)
    _hook100(_p100, _ex100, "report four: one it never saw", "c2b")
    until(lambda: "c2b" in _HOOK100, 10)
    check("the report it died on is kept as seen", _kept100(seen=True),
          [_n3])
    check("the report made while it was out is kept as unseen",
          _kept100(seen=False), [_n4])
    _seen100, _unseen100 = _kept100(seen=True), _kept100(seen=False)
    _base100 = len(_to100(_p100, "planner"))
    post("/event", {"hook_event_name": "Stop", "role": "planner",
                    "session_id": _pl100, "project_dir": _p100,
                    "cwd": _p100,
                    "last_assistant_message": "Back on another model."})
    _got100 = _nums100(_to100(_p100, "planner")[_base100:])
    check("nothing the window had already seen is sent again",
          [n for n in _got100 if n in _seen100], [])
    check("everything it never saw is sent",
          [n for n in _unseen100 if n not in _got100], [])

    print("   (c3) the window that comes up on another model may not have")
    print("   its channel yet: what waited is queued for it, not lost")
    _clean100()
    _status100(_p100, "planner", _pl100, "Fable 5.1", "claude-fable-5-1")
    _death100(_p100, "planner", _pl100, _SAID100)
    _hook100(_p100, _ex100, "report five: kept for a channel not yet up",
             "c3")
    until(lambda: "c3" in _HOOK100, 10)
    _was100 = sorted(_kept100())
    _sk = socket.socket()
    _sk.bind(("127.0.0.1", 0))
    _dead100 = _sk.getsockname()[1]
    _sk.close()
    post("/channel/register", {"project": _p100, "port": _dead100,
                               "pid": os.getpid(), "role": "planner"},
         secret=True)
    daemon.QUEUED.pop(_k100, None)
    _status100(_p100, "planner", _pl100, "Opus 5 (1M context)",
               "claude-opus-5")
    _q100rows = [json.loads(_i) for _i in daemon.QUEUED.get(_k100) or []]
    note("kept when the limit ended", _was100)
    # the queue against what was KEPT, not against a count of it - whether
    # anything was kept is the keeping's own check, in (b) and (c2)
    check("with no channel to take them, what was kept is queued, not lost",
          _nums100(_q100rows), _was100)
    _base100 = len(_to100(_p100, "planner"))
    register(_p100, "planner", _pl100)
    check("and they go over when the channel comes back",
          until(lambda: _nums100(_to100(_p100, "planner")[_base100:])
                == _was100, 5), True)

    print("   (d) the other role: the executor's model runs out - the same")
    print("   hold, the same single message, and its first finished turn")
    print("   ends it")
    _clean100()
    tg_reset()
    _base100 = len(_to100(_p100, "executor"))
    _death100(_p100, "executor", _ex100, _SAIDX100)
    check("the executor's spent model holds the pair too",
          ((daemon.STATE.get("paused") or {}).get(_k100) or {}).get("by"),
          "model_limit")
    check("ONE message reaches the chat, with the sentence (executor)",
          until(lambda: len(_chat100(_SAIDX100)) >= 1, 5)
          and len(_chat100(_SAIDX100)), 1)
    _said100 = len(_chat100(_SAIDX100))
    for _round in range(2):
        _pass100(_EK100)
        daemon.check_lost_turn(_p100)
        _death100(_p100, "executor", _ex100, _SAIDX100)
    check("nothing is handed back to an executor whose model is spent",
          len(_to100(_p100, "executor")) - _base100, 0)
    check("and no second message about it",
          len(_chat100(_SAIDX100)) - _said100, 0)
    _base100 = len(_to100(_p100, "planner"))
    _hook100(_p100, _ex100, "report six: back on another model", "d")
    check("its first finished turn ends the hold",
          until(lambda: not (daemon.STATE.get("model_limit") or {})
                .get(_EK100), 10), True)
    check("and that turn is reviewed as usual - its report is delivered",
          until(lambda: any("report six" in body_of(d.get("content") or "")
                            for d in _to100(_p100, "planner")[_base100:]),
                20), True)
    _answer100(_p100, "accepted six")
    until(lambda: "d" in _HOOK100, 20)

    print("   (d2) the executor, idle at its prompt, gets another model in its")
    print("   window: the status line ends the hold, and the turn it died in")
    print("   is handed back to it once")
    _clean100()
    _death100(_p100, "executor", _ex100, _SAIDX100)
    _base100 = len(_to100(_p100, "executor"))
    _status100(_p100, "executor", _ex100, "Sonnet 5", "claude-sonnet-5")
    check("a status line on another family ends the executor's hold",
          (daemon.STATE.get("model_limit") or {}).get(_EK100), None)
    check("and the turn it died in is handed back to it, once",
          until(lambda: len(_to100(_p100, "executor")) - _base100 == 1, 5)
          and len(_to100(_p100, "executor")) - _base100, 1)
    _status100(_p100, "executor", _ex100, "Opus 5 (1M context)",
               "claude-opus-5")
    check("the path inventory carries the record, so a moved project takes "
          "it along and a removed one drops it",
          "model_limit" in daemon.PAIR_KEYED, True)

    print("   (e) THE CONTROL: the same payload with the client's sentence")
    print("   taken out - an instant rate limit. Not held, and the old repair")
    print("   still runs; but a death the hand-back itself causes carries the")
    print("   count on, so the next attempt is the second, and later")
    _status100(_q100, "planner", _plq100, "Fable 5.1", "claude-fable-5-1")
    # the planner had finished turns before this one, as it always has
    post("/event", {"hook_event_name": "Stop", "role": "planner",
                    "session_id": _plq100, "project_dir": _q100,
                    "cwd": _q100,
                    "last_assistant_message": "Nothing to say yet."})
    _it100 = _iter100(_q100)
    _hook100(_q100, _exq100, "report one of the control", "e")
    check("the control report waits for its verdict",
          until(lambda: _pending100(_q100) == str(_it100 + 1), 20), True)
    _nq = str(_it100 + 1)
    _death100(_q100, "planner", _plq100, None)
    check("an instant rate limit is not held",
          ((daemon.STATE.get("paused") or {}).get(_kq100) or {}).get("by"),
          None)
    def _lines100():
        return [r.get("text") or ""
                for r in _j74("back to the planner", _q100)]

    def _rec100():
        return dict((daemon.STATE.get("stopfail") or {}).get(_QK100) or {})

    _base100 = len(_to100(_q100, "planner"))
    _pass100(_QK100)
    daemon.check_lost_turn(_q100)
    check("the tick hands the report back, as it always did",
          len(_to100(_q100, "planner")) - _base100, 1)
    _lq100 = _lines100()
    check("and the line names the report it handed back",
          bool(_lq100) and ("report %s back" % _nq) in _lq100[-1], True)
    _death100(_q100, "planner", _plq100, None)
    check("a death the hand-back caused carries its count on",
          _rec100().get("revives"), 1)
    check("and what was tried goes with it", len(_rec100().get("tried")
                                                 or []), 1)
    _pass100(_QK100)
    daemon.check_lost_turn(_q100)
    _lq100 = _lines100()
    check("so the next hand-back is attempt 2 of 3",
          bool(_lq100) and "attempt 2 of 3" in _lq100[-1], True)
    print("   and after that hand-back the next look is pushed out; a death")
    print("   now must not pull it back to one grace after itself")
    _sched100 = float(_rec100().get("at") or 0)
    _death100(_q100, "planner", _plq100, None)
    _r3 = _rec100()
    check("a death does not pull the next look before the backoff set it",
          float(_r3.get("at") or 0) >= _sched100 > 0, True)
    _died100 = float(_r3.get("died") or 0)
    _pass100(_QK100)
    daemon.check_lost_turn(_q100)
    _lq100 = _lines100()
    check("and the line names the time of the death, not of the schedule",
          bool(_lq100) and bool(_died100) and time.strftime(
              "%H:%M:%S", time.localtime(_died100)) in _lq100[-1], True)
    _answer100(_q100, "accepted the control")
    until(lambda: "e" in _HOOK100, 20)
finally:
    daemon.CFG["thresholds"] = _thr100
    for _proj in (_p100, _q100):
        post_rc("/loop", {"action": "stop", "project": _proj})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n101. a tracked command's record ends at its OWN call's end - a")
print("     failure, a success or the end of the turn it ran in - and a")
print("     background job's when the Stop's own list stops naming it")
print("    17-23.09 on the watched projects: 36 foreground records never")
print("    closed, every one of them a FAILED call. The client ends a failed")
print("    call with PostToolUseFailure and nothing else, and the bridge did")
print("    not listen for it, so each record held its pair busy for up to an")
print("    hour and put 'decide whether it is stuck' to it about a command")
print("    that had ended. And the key was the command's first word: 7 failed")
print("    records were 'closed' by some other command's PostToolUse, a")
print("    background launch's included. Real order: the PreToolUse, then the")
print("    call's own end or the Stop, then the real tick. Every check compares")
print("    one mechanism's output with its own input; where that input is")
print("    made by ANOTHER mechanism the check is asked only once it exists,")
print("    and says so when it does not, so a broken mechanism reddens its own")
print("    checks and not its neighbours'. -> DECISIONS.md 8.24")
_p101 = os.path.join(TMP, "failed-calls")
_s101 = os.path.join(TMP, "turn-ends")
_n101 = os.path.join(TMP, "no-ids")
_i101 = os.path.join(TMP, "install-101")
for _d in (_p101, _s101, _n101, os.path.join(_i101, ".claude")):
    os.makedirs(_d, exist_ok=True)
_k101, _ks101, _kn101 = canon(_p101), canon(_s101), canon(_n101)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p101: {}, _s101: {},
                              _n101: {}}})
# The first eight characters of a session id are its record's key, so
# every id here differs within them.
_FX101, _FP101 = "fa101-ex", "fa101-pl"
_SA101, _SB101, _SP101 = "tb101-ex", "tb101-e2", "tb101-pl"
_NX101, _NP101 = "nc101-ex", "nc101-pl"
for _proj, _ex, _pl in ((_p101, _FX101, _FP101), (_s101, _SA101, _SP101),
                        (_n101, _NX101, _NP101)):
    for _r, _sid in (("executor", _ex), ("planner", _pl)):
        post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                           "session_id": _sid, "project_dir": _proj,
                           "cwd": _proj})
        register(_proj, _r, _sid)
# The second window of the same half, as during a handover: it comes up
# before any record here exists, the order a real overlap has.
post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                   "session_id": _SB101, "project_dir": _s101,
                   "cwd": _s101})
_SKIP101 = []


def _ev101(proj, name, sid, **extra):
    body = {"hook_event_name": name, "role": "executor", "session_id": sid,
            "project_dir": proj, "cwd": proj,
            "transcript_path": os.path.join(TMP, "%s.jsonl" % sid),
            "prompt_id": "prompt-%s" % sid, "permission_mode": "default"}
    body.update(extra)
    return post_rc("/event", body)


def _tin101(cmd, bg):
    tin = {"command": cmd, "description": "run it"}
    if bg:
        tin["run_in_background"] = True
    return tin


def _pre101(proj, sid, cmd, tid, bg=False, agent=""):
    """PreToolUse as the client sends it; `tid` "" is an older client."""
    extra = {"tool_name": "Bash", "tool_input": _tin101(cmd, bg)}
    if tid:
        extra["tool_use_id"] = tid
    if agent:
        extra.update({"agent_id": agent, "agent_type": "general-purpose"})
    return _ev101(proj, "PreToolUse", sid, **extra)


def _post101(proj, sid, cmd, tid, bg=False, bgid=""):
    """PostToolUse in the recorded shape; a launch's names its job."""
    resp = {"stdout": "", "stderr": "", "interrupted": False,
            "isImage": False, "noOutputExpected": False}
    if bgid:
        resp["backgroundTaskId"] = bgid
    extra = {"tool_name": "Bash", "tool_input": _tin101(cmd, bg),
             "tool_response": resp, "duration_ms": 1200}
    if tid:
        extra["tool_use_id"] = tid
    return _ev101(proj, "PostToolUse", sid, **extra)


def _fail101(proj, sid, cmd, tid, error="Exit code 3"):
    """PostToolUseFailure with the fields a real one carried (2.1.280)."""
    return _ev101(proj, "PostToolUseFailure", sid, tool_name="Bash",
                  tool_input=_tin101(cmd, False), tool_use_id=tid,
                  error=error, is_interrupt=False, duration_ms=4200)


def _stop101(proj, sid, tasks=None):
    """A Stop with nothing to review. `tasks` None leaves the field out."""
    extra = {"stop_hook_active": False, "last_assistant_message": "",
             "session_crons": []}
    if tasks is not None:
        extra["background_tasks"] = tasks
    return _ev101(proj, "Stop", sid, **extra)


def _open101(proj, cmd):
    """This command's records among those every watchdog reads."""
    return [dict(m) for m in (daemon.inflight_live(proj) or [])
            if (m or {}).get("cmd") == cmd]


def _ids101(proj, cmd):
    return [m.get("bgid") or "" for m in _open101(proj, cmd)]


def _asked101(key, since):
    """The stuck questions the pair's planner received after `since`."""
    got = DELIVERED.get((key, "planner"), [])[since:]
    return [json.dumps(b, ensure_ascii=False) for b in got
            if "Decide whether it is stuck" in json.dumps(b)]


def _age101(key, secs):
    """The clock moves on for this project's open records, just before the
    watch looks and after every event of the part has been posted - the
    start of a call is read by nothing else in between."""
    for _m in (daemon.PROCTRACK.get(key) or {}).values():
        _m["started"] = float(_m.get("started") or time.time()) - secs


def _ask101(ready, why, name, got, want):
    """A check whose input another mechanism makes, asked once it exists.
    When it does not, the mechanism that should have made it has already
    said so in its own check above; this one says it was not asked."""
    if ready:
        check(name, got, want)
    else:
        _SKIP101.append(name)
        print("  ..   not asked: %s - %s" % (name, why))


_OLD_AGE101 = daemon.stuck_limit(None) + 60
try:
    print("   (i) the installer subscribes the failure's own event - merged,")
    print("       the project's own hooks kept")
    _set101 = os.path.join(_i101, ".claude", "settings.json")
    _GUARD101 = "py tools/their_guard.py"
    _FLOG101 = "py tools/their_failure_log.py"
    with open(_set101, "w", encoding="utf-8") as _fh:
        json.dump({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": _GUARD101}]}],
            "PostToolUseFailure": [{"matcher": "", "hooks": [
                {"type": "command", "command": _FLOG101}]}]}}, _fh)
    from bridgecore import install as _inst101
    _inst101.install(_i101, "executor", python=sys.executable,
                     statusline=False)
    _cfg101 = json.loads(read_or_fail(_set101, "the installed settings")
                         or "{}")

    def _hooks101(ev):
        return [h for g in ((_cfg101.get("hooks") or {}).get(ev) or [])
                for h in (g.get("hooks") or [])]

    check("(i) PostToolUseFailure carries the bridge's hook",
          len([h for h in _hooks101("PostToolUseFailure")
               if list(h.get("args") or []) == ["-m", "bridgecore.hook"]]),
          1)
    check("CONTROL (i) and the project's own hooks are where they were",
          [any(h.get("command") == _GUARD101
               for h in _hooks101("PreToolUse")),
           any(h.get("command") == _FLOG101
               for h in _hooks101("PostToolUseFailure"))], [True, True])

    print("   (a) a failed call ends on PostToolUseFailure: the record, the")
    print("       half and the watch all hear it")
    _CA = "godot --headless --script res://tests/run_all.gd"
    _TA = "toolu_101a_fail"
    _pre101(_p101, _FX101, _CA, _TA)
    check("PRECONDITION: the call is tracked", len(_open101(_p101, _CA)), 1)
    check("PRECONDITION: and while it runs the half is busy",
          daemon.tool_in_flight(_p101, "executor"), True)
    _fail101(_p101, _FX101, _CA, _TA)
    _closed_a = not _open101(_p101, _CA)
    check("(a) the failed call's record is gone", _closed_a, True)
    check("(a) and the half is not called busy any more",
          daemon.tool_in_flight(_p101, "executor"), False)
    _la = [r.get("text") or "" for r in _j74("Failed after", _p101)]
    _ask101(_closed_a, "the record was not closed, see above",
            "(a) the line says what failed, after how long and why",
            bool(_la) and _CA in _la[-1] and "(Exit code 3)" in _la[-1]
            and bool(re.search(r"Failed after \d+s: ", _la[-1])), True)
    _fl = [h for (_pk, _sg), _hs in list(daemon.DURATIONS.items())
           if _pk == _k101 for h in _hs
           if isinstance(h, (tuple, list)) and len(h) > 1 and h[1]]
    _ask101(_closed_a, "the record was not closed, see above",
            "(a) its seconds went into this project's history, marked "
            "failed", len(_fl), 1)
    print("   the real tick, with the clock past the stuck limit: nobody is")
    print("   asked about a command that has ended")
    _age101(_k101, _OLD_AGE101)
    _q0 = len(DELIVERED.get((_k101, "planner"), []))
    daemon.check_processes()
    check("(a) and the watch asks nobody whether it is stuck",
          len(_asked101(_k101, _q0)), 0)

    print("   CONTROL (a): a call that succeeds ends on its own PostToolUse,")
    print("   exactly as before")
    _CO = "godot --headless --script res://tests/smoke.gd"
    _TO = "toolu_101a_ok"
    _pre101(_p101, _FX101, _CO, _TO)
    _sig_o = [m.get("sig") for m in _open101(_p101, _CO)] == ["godot"]
    check("(a) the record keeps its command's first word, for the history",
          _sig_o, True)
    _h0 = len(daemon.DURATIONS.get((_k101, "godot"), []))
    _post101(_p101, _FX101, _CO, _TO)
    _closed_o = not _open101(_p101, _CO)
    check("CONTROL (a) a successful call's record is gone", _closed_o, True)
    _lo = [r.get("text") or "" for r in _j74("Finished in", _p101)]
    _ask101(_closed_o, "the record was not closed, see above",
            "CONTROL (a) with the line it always wrote",
            bool(_lo) and _CO in _lo[-1], True)
    _ask101(_closed_o and _sig_o,
            "the record was not closed, or did not keep its first word",
            "(a) and one more run under the command's first word",
            len(daemon.DURATIONS.get((_k101, "godot"), [])) - _h0, 1)

    print("   (u) 'usually' says how many of the runs it averages failed")
    for _i in range(4):
        _c = "npm test -- --shard=%d" % _i
        _pre101(_p101, _FX101, _c, "toolu_101u_%d" % _i)
        _post101(_p101, _FX101, _c, "toolu_101u_%d" % _i)
    _pre101(_p101, _FX101, "npm test -- --shard=9", "toolu_101u_9")
    _fail101(_p101, _FX101, "npm test -- --shard=9", "toolu_101u_9",
             error="Exit code 1")
    _hu = list(daemon.DURATIONS.get((_k101, "npm"), []))
    _ready_u = len(_hu) == 5 and len(
        [h for h in _hu if isinstance(h, (tuple, list)) and h[1]]) == 1
    note("the history the question will average", _hu)
    _CU, _TU = "npm test -- --runInBand", "toolu_101u_long"
    _pre101(_p101, _FX101, _CU, _TU)
    _age101(_k101, _OLD_AGE101)
    _q0 = len(DELIVERED.get((_k101, "planner"), []))
    daemon.check_processes()
    _qu = _asked101(_k101, _q0)
    _why_u = "the history is not five runs with one failure, see (a)"
    _ask101(_ready_u, _why_u,
            "(u) the question averages the command's own history",
            bool(_qu) and "over 5 runs" in _qu[-1], True)
    _ready_u2 = _ready_u and bool(_qu) and "over 5 runs" in _qu[-1]
    _why_u2 = "the question did not average that history, see above"
    _ask101(_ready_u2, _why_u2,
            "(u) and says how many of those runs failed",
            "1 of 5 failed" in _qu[-1] if _qu else False, True)
    _lu = [r.get("text") or ""
           for r in _j74("asked the pair to decide", _p101)]
    _ask101(_ready_u2, _why_u2, "(u) the journal line carries the same mark",
            bool(_lu) and "1 of 5 failed" in _lu[-1], True)
    _post101(_p101, _FX101, _CU, _TU)

    print("   (b) two calls with one first word are two records, and each")
    print("       ends on its own call's end")
    _CB1, _TB1 = "pytest -q tests/unit", "toolu_101b_one"
    _CB2, _TB2 = "pytest -q tests/slow", "toolu_101b_two"
    _pre101(_s101, _SA101, _CB1, _TB1)
    _pre101(_s101, _SA101, _CB2, _TB2)
    _two_b = [len(_open101(_s101, _CB1)), len(_open101(_s101, _CB2))]
    check("(b) two calls, two records", _two_b, [1, 1])
    _sig_b = [m.get("sig") for m in (_open101(_s101, _CB1)
                                     + _open101(_s101, _CB2))]
    _hb0 = len(daemon.DURATIONS.get((_ks101, "pytest"), []))
    _post101(_s101, _SA101, _CB2, _TB2)
    check("(b) the second call's PostToolUse ends the second call",
          _open101(_s101, _CB2), [])
    check("(b) and not the first, which is still running",
          len(_open101(_s101, _CB1)), 1)
    _post101(_s101, _SA101, _CB1, _TB1)
    check("(b) the first ends on its own PostToolUse",
          _open101(_s101, _CB1), [])
    _ask101(_two_b == [1, 1] and _sig_b == ["pytest", "pytest"]
            and not _open101(_s101, _CB1) and not _open101(_s101, _CB2),
            "the two calls were not two records that kept their first word "
            "and both closed, see above",
            "(b) the history keeps both runs under the command's first word",
            len(daemon.DURATIONS.get((_ks101, "pytest"), [])) - _hb0, 2)

    print("   (c) the end of a turn ends that session's foreground calls -")
    print("       and only that session's")
    _CCA, _TCA = "gradle build --offline", "toolu_101c_a"
    _CCB, _TCB = "dotnet build -c Release", "toolu_101c_b"
    _pre101(_s101, _SA101, _CCA, _TCA)
    _pre101(_s101, _SB101, _CCB, _TCB)
    check("PRECONDITION: both windows' calls are tracked",
          [len(_open101(_s101, _CCA)), len(_open101(_s101, _CCB))], [1, 1])
    _stop101(_s101, _SB101)
    check("(c) the other window's Stop leaves this window's call open",
          len(_open101(_s101, _CCA)), 1)
    check("(c) and ends its own", _open101(_s101, _CCB), [])
    _stop101(_s101, _SA101)
    _closed_c = not _open101(_s101, _CCA)
    check("(c) this window's Stop ends its call", _closed_c, True)
    check("(c) and the half is not called busy any more",
          daemon.tool_in_flight(_s101, "executor"), False)
    _lc = [r.get("text") or ""
           for r in _j74("Closed at the end of the turn", _s101)]
    _ask101(_closed_c, "the record was not closed, see above",
            "(c) the line names the call and says why it was closed",
            any(_CCA in t and "a turn that has ended" in t for t in _lc),
            True)
    _age101(_ks101, _OLD_AGE101)
    _q0 = len(DELIVERED.get((_ks101, "planner"), []))
    daemon.check_processes()
    check("(c) and the real tick asks nobody whether it is stuck",
          len(_asked101(_ks101, _q0)), 0)

    print("   (c2) a BACKGROUND SUBAGENT's call carries the parent's session")
    print("        and outlives the parent's turn - measured: a headless")
    print("        probe had its Stop at 1.5 s and the subagent's call running")
    print("        from 2.3 s to 28.6 s. It ends only when the Stop's own list")
    print("        no longer names its agent")
    _CG, _TG, _AG = ("pytest -q tests/integration", "toolu_101c_agent",
                     "a101agent0000000")
    _pre101(_s101, _SA101, _CG, _TG, agent=_AG)
    _agent_g = [m.get("agent") for m in _open101(_s101, _CG)] == [_AG]
    check("(c2) the call's record names the subagent it belongs to",
          _agent_g, True)
    _stop101(_s101, _SA101, tasks=[{
        "id": _AG, "type": "subagent", "status": "running",
        "description": "probe", "agent_type": "general-purpose"}])
    _why_g = "the record does not name its subagent, see above"
    _ask101(_agent_g, _why_g,
            "(c2) the parent's Stop leaves it open while its agent is listed",
            len(_open101(_s101, _CG)), 1)
    _stop101(_s101, _SA101)
    _ask101(_agent_g, _why_g,
            "(c2) and a Stop with no list concludes nothing about it",
            len(_open101(_s101, _CG)), 1)
    _stop101(_s101, _SA101, tasks=[])
    check("(c2) a Stop whose list no longer names the agent ends it",
          _open101(_s101, _CG), [])

    print("   (b2) a background launch's PostToolUse ends no foreground")
    print("        record, and hands its own record the client's job id")
    _CF, _TF = "make -j4 all", "toolu_101b2_fg"
    _CW, _TW, _BW = "make watch", "toolu_101b2_bg", "bgtask101"
    _pre101(_s101, _SA101, _CF, _TF)
    _pre101(_s101, _SA101, _CW, _TW, bg=True)
    _post101(_s101, _SA101, _CW, _TW, bg=True, bgid=_BW)
    check("(b2) the foreground call still runs after the launch returned",
          len(_open101(_s101, _CF)), 1)
    check("(b2) and the launch's record carries the job's id",
          _ids101(_s101, _CW), [_BW])
    _post101(_s101, _SA101, _CF, _TF)
    check("CONTROL (b2) the foreground call ends on its own PostToolUse",
          _open101(_s101, _CF), [])

    print("   (b3) an OLDER client sends no ids: the first word is the key,")
    print("        as before - and a launch still ends nothing")
    _pre101(_n101, _NX101, "make -j4 all", "")
    _pre101(_n101, _NX101, "make watch", "", bg=True)
    _post101(_n101, _NX101, "make watch", "", bg=True)
    check("(b3) the foreground call still runs after the launch returned",
          len(_open101(_n101, "make -j4 all")), 1)
    _post101(_n101, _NX101, "make -j4 all", "")
    check("CONTROL (b3) and ends on its own PostToolUse by the first word",
          _open101(_n101, "make -j4 all"), [])

    print("   (d) a background job ends when the Stop's own list no longer")
    print("       names it - and only then, and only in its own session")
    _CJ2, _TJ2, _BJ2 = "gradle bootRun", "toolu_101d_two", "bgtask102"
    _CJ0, _TJ0 = "npm test -- --watch", "toolu_101d_noid"
    _CJ3, _TJ3, _BJ3 = "make serve", "toolu_101d_other", "bgtask103"
    _pre101(_s101, _SA101, _CJ2, _TJ2, bg=True)
    _post101(_s101, _SA101, _CJ2, _TJ2, bg=True, bgid=_BJ2)
    _pre101(_s101, _SA101, _CJ0, _TJ0, bg=True)
    _post101(_s101, _SA101, _CJ0, _TJ0, bg=True)
    _pre101(_s101, _SB101, _CJ3, _TJ3, bg=True)
    _post101(_s101, _SB101, _CJ3, _TJ3, bg=True, bgid=_BJ3)
    _stop101(_s101, _SA101)
    _stays_d = len(_open101(_s101, _CJ0)) == 1
    check("(d) a Stop does not end a background job by itself", _stays_d,
          True)
    _ask101(_stays_d, "the Stop ended a background job on its own, see "
            "above", "CONTROL (d) and with no list, nothing is concluded",
            [len(_open101(_s101, _CW)), len(_open101(_s101, _CJ2))], [1, 1])
    _had_d = dict((_c, _ids101(_s101, _c)) for _c in (_CW, _CJ2, _CJ3))
    _stop101(_s101, _SA101, tasks=[{
        "id": _BJ2, "type": "shell", "status": "running",
        "description": _CJ2, "command": _CJ2}])
    _why_d = "the job was not running with its id before this Stop"
    _ask101(_had_d[_CW] == [_BW], _why_d,
            "(d) the job the list no longer names has ended",
            _open101(_s101, _CW), [])
    _ask101(_had_d[_CJ2] == [_BJ2], _why_d,
            "CONTROL (d) the job the list names still runs",
            len(_open101(_s101, _CJ2)), 1)
    _ask101(_had_d[_CJ3] == [_BJ3], _why_d,
            "(d) another window's job is left to that window's own list",
            len(_open101(_s101, _CJ3)), 1)
    _ld = [r.get("text") or "" for r in _j74(
        "the Stop's own list of running tasks no longer names it", _s101)]
    _ask101(_had_d[_CW] == [_BW] and not _open101(_s101, _CW),
            "the job did not end on the list, see above",
            "(d) the line says why, and names the job",
            any(_BW in t and _CW in t for t in _ld), True)
    _stop101(_s101, _SB101, tasks=[])
    _ask101(_had_d[_CJ3] == [_BJ3], _why_d,
            "(d) that window's own Stop, whose list no longer names it, "
            "ends it", _open101(_s101, _CJ3), [])

    print("   (r) a restart hands each record back with what it is looked up")
    print("       by - the reseeded copy against the one on disk")
    _FIELDS101 = ("sig", "tid", "bgid", "agent")
    with daemon._lock:
        _disk101 = dict(((daemon.STATE.get("inflight") or {})
                         .get(_ks101) or {}))
    daemon.PROCTRACK.pop(_ks101, None)
    daemon.reseed_proctrack()
    _back101 = dict(daemon.PROCTRACK.get(_ks101) or {})
    _ask101(bool(_disk101), "nothing was left on disk to reseed",
            "(r) every reseeded record carries what the one on disk did",
            dict((_k, [(_back101.get(_k) or {}).get(_f) or ""
                       for _f in _FIELDS101]) for _k in _disk101),
            dict((_k, [(_disk101[_k] or {}).get(_f) or ""
                       for _f in _FIELDS101]) for _k in _disk101))
    note("records reseeded", sorted((_m or {}).get("cmd")
                                    for _m in _disk101.values()))
    note("checks not asked in this run", len(_SKIP101),
         "0 when every mechanism works; each one names its reason above")
finally:
    for _k in (_k101, _ks101, _kn101):
        with daemon._lock:
            (daemon.STATE.get("inflight") or {}).pop(_k, None)
            daemon.save_state()
        daemon.PROCTRACK.pop(_k, None)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n102. a start never silently swaps the model a person picked, both")
print("     halves start from one request read at the click, every opening")
print("     line names its model and mode, and /state reads one snapshot")
print("    2026-08-30: the owner chose opus for both halves in the panel and")
print("    got a fable planner - the drop-down fed two buttons, the start sent")
print("    the chain's head, and no line a person could read named the model")
print("    a window was opened on. 'start both' was two starts 1.2 s apart,")
print("    each re-reading a chain the 2.5 s tick could have put back. And")
print("    /state walked the live session dict outside the lock with work per")
print("    element, so a window registering mid-walk cost the panel its")
print("    answer - measured, 28 failures in 584 calls under a writing thread")
print("    and 0 over a copy. Real endpoints; the race is FORCED at the one")
print("    moment it needs, by a record that registers another one while it")
print("    is being read, so the red is deterministic. A check whose input")
print("    another mechanism makes is asked only once that input exists.")
print("    -> DECISIONS.md 8.25")
_pa102 = os.path.join(TMP, "picked")
_pb102 = os.path.join(TMP, "both-halves")
_pg102 = os.path.join(TMP, "snapshot")
for _d in (_pa102, _pb102, _pg102):
    os.makedirs(_d, exist_ok=True)
_ka102, _kb102, _kg102 = canon(_pa102), canon(_pb102), canon(_pg102)
post("/config", {"projects": {
    A: {}, B: {}, C: {},
    _pa102: {"chains": {"executor": ["opus"], "planner": ["fable"]}},
    _pb102: {"chains": {"executor": ["fable"], "planner": ["fable"]}},
    _pg102: {}}})
for _r, _sid in (("executor", "sg102-ex"), ("planner", "sg102-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _pg102,
                       "cwd": _pg102})
_SKIP102 = []


def _ask102(ready, why, name, got, want):
    """A check whose input another mechanism makes, asked once it exists."""
    if ready:
        check(name, got, want)
    else:
        _SKIP102.append(name)
        print("  ..   not asked: %s - %s" % (name, why))


def _rows102(proj, role):
    return [r for r in launches()
            if canon(r.get("cwd") or "") == canon(proj)
            and r.get("role") == role]


def _flag102(row, flag):
    a = list((row or {}).get("argv") or [])
    return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) \
        else None


def _opened102(proj, role):
    """How many windows note_launch has counted - its synchronous record."""
    return len(((daemon.STATE.get("launches") or {})
                .get("%s|%s" % (canon(proj), role))) or [])


def _get102(path):
    """GET that hands back (status, body) and never raises: a /state that
    fails is a FAIL line here, not the end of the suite."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path),
                                    timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:
        return repr(exc), None


class _Trap102(dict):
    """A record that, when it is read from inside `where` through `via`,
    registers one more entry into `into` - what a window coming up does
    to STATE while somebody is walking it. Fires once."""

    def __init__(self, data, into, add_key, add_val, where, via="get"):
        dict.__init__(self, data)
        self.t_into, self.t_key, self.t_val = into, add_key, add_val
        self.t_where, self.t_via, self.fired = where, via, False

    def _maybe(self):
        if self.fired:
            return
        f = sys._getframe(2)
        while f is not None and f.f_code.co_name.startswith("<"):
            f = f.f_back
        if f is not None and f.f_code.co_name == self.t_where:
            self.fired = True
            self.t_into[self.t_key] = self.t_val

    def get(self, key, default=None):
        if self.t_via == "get":
            self._maybe()
        return dict.get(self, key, default)

    def items(self):
        if self.t_via == "items":
            self._maybe()
        return dict.items(self)


def _session102(sid):
    return {"path": _pg102, "role": "executor", "session_id": sid,
            "state": "idle", "last_seen": time.strftime("%H:%M:%S"),
            "seen_at": time.time()}


def _plant102(container, key, rec):
    with daemon._lock:
        daemon.STATE.setdefault(container, {})[key] = rec


def _unplant102(container, *keys):
    with daemon._lock:
        for _k in keys:
            (daemon.STATE.get(container) or {}).pop(_k, None)
        daemon.save_state()


def _call102(fn, *a):
    """(result, error) - a helper that raises is a FAIL line, not a crash."""
    try:
        return fn(*a), ""
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


# The source, read once, for the gates that are about where code stands.
_dsrc102 = read_or_fail(os.path.join(os.path.dirname(os.path.abspath(
    daemon.__file__)), "daemon.py"), "daemon.py")
_panel102 = read_or_fail(os.path.join(os.path.dirname(os.path.abspath(
    daemon.__file__)), "panel.html"), "panel.html")
try:
    _tree102 = ast.parse(_dsrc102)
except SyntaxError:
    _tree102 = ast.parse("")
_fns102 = {n.name: n for n in ast.walk(_tree102)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _names102(node):
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}


def _live_iters102(node):
    """Loops and comprehensions whose iterable reads STATE or CFG itself."""
    bad = []
    for n in ast.walk(node):
        its = []
        if isinstance(n, (ast.For, ast.AsyncFor)):
            its = [n.iter]
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp,
                            ast.GeneratorExp)):
            its = [g.iter for g in n.generators]
        for it in its:
            if _names102(it) & {"STATE", "CFG"}:
                bad.append(n.lineno)
    return bad


def _lock_withs102(node):
    return [w for w in ast.walk(node) if isinstance(w, ast.With)
            and any(isinstance(i.context_expr, ast.Name)
                    and i.context_expr.id == "_lock" for i in w.items)]


def _calls102(node, name):
    """Calls to `name` (a bare name, or an attribute's last part) in node."""
    out = []
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            f = c.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                    isinstance(f, ast.Attribute) and f.attr == name):
                out.append(c)
    return out


# the /state branch of do_GET, and the reply dict it sends
_state_if102 = None
for _n in ast.walk(_fns102.get("do_GET") or ast.parse("")):
    if isinstance(_n, ast.If) and "/state" in ast.dump(_n.test):
        _state_if102 = _n
        break
_reply102 = {}
if _state_if102 is not None:
    for _c in _calls102(_state_if102, "_send"):
        if len(_c.args) > 1 and isinstance(_c.args[1], ast.Dict):
            for _k, _v in zip(_c.args[1].keys, _c.args[1].values):
                if isinstance(_k, ast.Constant):
                    _reply102[_k.value] = _v

try:
    print("   (c) every call of note_launch names the model and the mode it")
    print("       opens the window on - a gate per call site, so a new site")
    print("       that forgets is red on its own line")
    _sites102 = {}
    for _fname, _fn in _fns102.items():
        for _c in _calls102(_fn, "note_launch"):
            if isinstance(_c.func, ast.Name):
                _sites102.setdefault(_fname, []).append(
                    {k.arg for k in _c.keywords} >= {"model", "mode"})
    note("note_launch is called from", sorted(_sites102))
    for _fname in sorted(_sites102):
        check("(c) note_launch in %s names model and mode" % _fname,
              all(_sites102[_fname]), True)
    _named_hs = all(_sites102.get("handle_session") or [False])

    print("   (a) a pick nobody applied refuses the start, naming both")
    print("       models - the daemon decides, the panel carries the fact")
    _n0 = _opened102(_pa102, "planner")
    _st, _r = post_rc("/session", {
        "action": "launch", "project": _pa102, "role": "planner",
        "model": "fable", "mode": "plan", "picked": "opus"})
    _err = (_r or {}).get("error") or ""
    _refused = (_r or {}).get("ok") is False
    check("(a) the start is refused", _refused, True)
    check("(a) and nothing was opened for it - note_launch never ran",
          _opened102(_pa102, "planner") - _n0, 0)
    _ask102(_refused, "the start was not refused, see above",
            "(a) the refusal names the pick, the chain's head and the way "
            "out", "the drop-down says opus" in _err and "starts with fable"
            in _err and "'start with'" in _err and "Nothing was started"
            in _err, True)
    _lr = [r for r in _j74("Start refused", _pa102)]
    _ask102(_refused, "the start was not refused, see above",
            "(a) the journal says so at warn, with both models",
            bool(_lr) and _lr[-1].get("level") == "warn"
            and "opus" in (_lr[-1].get("text") or "")
            and "fable" in (_lr[-1].get("text") or ""), True)
    print("   CONTROL (a): the pick IS the head, and no pick at all - both")
    print("   start exactly as before")
    _st, _r1 = post_rc("/session", {
        "action": "launch", "project": _pa102, "role": "planner",
        "model": "opus", "mode": "plan", "picked": "opus"})
    _st, _r2 = post_rc("/session", {
        "action": "launch", "project": _pa102, "role": "executor",
        "model": "opus", "mode": "bypassPermissions"})
    check("CONTROL (a) a pick equal to the head starts",
          (_r1 or {}).get("ok"), True)
    check("CONTROL (a) and a start with no pick starts",
          (_r2 or {}).get("ok"), True)
    check("CONTROL (a) the stub saw both windows start on opus",
          until(lambda: [_flag102(r, "--model") for r in
                         _rows102(_pa102, "planner")][-1:] == ["opus"]
                and [_flag102(r, "--model") for r in
                     _rows102(_pa102, "executor")][-1:] == ["opus"], 30),
          True)
    check("(a) and no window was ever started on the refused head",
          [_flag102(r, "--model") for r in _rows102(_pa102, "planner")
           if _flag102(r, "--model") == "fable"], [])

    print("   (c) the opening line and the launch log name what was opened")
    _lo = [r.get("text") or "" for r in _j74("Opening a planner window",
                                               _pa102)]
    _ask102(_named_hs, "the panel's start does not pass the model and mode, "
            "see the gate above",
            "(c) the line reads 'on opus (mode plan)'",
            bool(_lo) and "Opening a planner window on opus (mode plan): "
            "you pressed start in the panel" in _lo[-1], True)
    _ll = [e for e in (daemon.STATE.get("launch_log") or [])
           if e.get("role") == "planner"
           and e.get("project") == daemon.project_name(_pa102)]
    _ask102(_named_hs, "the panel's start does not pass the model and mode, "
            "see the gate above",
            "(c) and the launch log carries the same two fields",
            [(_ll[-1].get("model"), _ll[-1].get("mode"))] if _ll else [],
            [("opus", "plan")])

    print("   (a) the panel carries the pick, per project, and lets go of it")
    print("       when a button applies it")
    check("(a) panel: a project switch clears the picks with the latch",
          "if(CUR!==window._launchProj){window._launchProj=CUR;"
          "window._launchTouched=false;window._picked={}}" in _panel102, True)
    check("(a) panel: a change in the drop-down records the pick",
          bool(re.search(r'addEventListener\("change",function\(\)\{\s*'
                         r'window\._picked=window\._picked\|\|\{\};'
                         r'window\._picked\[role\]=this\.value',
                         _panel102)), True)
    check("(a) panel: 'add as fallback' applies the pick",
          bool(re.search(r'\[data-add\]"\)\.forEach\(function\(b\)\{b\.'
                         r'onclick=function\(\)\{\s*var role=b\.dataset\.'
                         r'add[^\n]*\n\s*if\(window\._picked\)delete '
                         r'window\._picked\[role\];', _panel102)), True)
    check("(a) panel: 'start with' applies the pick",
          bool(re.search(r'var role=b\.dataset\.first[^\n]*\n\s*if\(!sel'
                         r'\.value\)return;\s*\n\s*if\(window\._picked\)'
                         r'delete window\._picked\[role\];', _panel102)),
          True)
    check("(a) panel: a single start sends the pick",
          # `confirm` since 8.29: the daemon asks before a second window
          bool(re.search(r'function launchRole\(role,confirm\)\{[\s\S]{0,400}'
                         r'var picked=\(window\._picked\|\|\{\}\)\[role\]'
                         r'\|\|null;[\s\S]{0,400}picked:picked,'
                         r'confirm:!!confirm\}',
                         _panel102)), True)

    print("   (b) 'start both' is ONE request: both picks checked first, the")
    print("       config written once, each half started on the head of the")
    print("       chain it was SENT - whatever lands in the config between")
    print("       the two starts")
    _saves102 = []
    _real_save102 = daemon.store.save_config

    def _count_save102(cfg):
        _saves102.append(time.time())
        return _real_save102(cfg)

    daemon.store.save_config = _count_save102
    _real_hs102 = daemon.handle_session
    _stale102 = {"on": False, "sent": 0}

    def _hs102(body):
        # THE TICK, AT THE ONE MOMENT IT MATTERS: a /config with the old
        # chains lands after the executor's start has RETURNED and before
        # anything is read for the planner's - through the real endpoint,
        # from the daemon's own request thread, so the interleaving is not a
        # race of the test's. (The first form sent it at the start of the
        # planner's own call, which is AFTER launch_both has built that
        # call's arguments - so a launch_both that read the planner's head
        # from the config stayed green. Sabotage found it.)
        out = _real_hs102(body)
        if _stale102["on"] and body.get("action") == "launch" \
                and body.get("role") == "executor":
            _stale102["on"] = False
            post("/config", {"projects": {
                A: {}, B: {}, C: {},
                _pa102: {"chains": {"executor": ["opus"],
                                    "planner": ["fable"]}},
                _pb102: {"chains": {"executor": ["fable"],
                                    "planner": ["fable"]}},
                _pg102: {}}})
            _stale102["sent"] += 1
        return out

    daemon.handle_session = _hs102
    try:
        _BOTH102 = {"executor": ["opus"], "planner": ["opus", "fable"]}
        _MODES102 = {"executor": "bypassPermissions", "planner": "plan"}
        _s0 = len(_saves102)
        _e0, _p0 = _opened102(_pb102, "executor"), _opened102(_pb102,
                                                              "planner")
        _st, _rb = post_rc("/session", {
            "action": "launch_both", "project": _pb102,
            "chains": _BOTH102, "modes": _MODES102,
            "readonly_planner": True,
            "picked": {"executor": None, "planner": "sonnet"}})
        _eb = (_rb or {}).get("error") or ""
        _refb = (_rb or {}).get("ok") is False \
            and "drop-down" in _eb
        check("(b) a pick nobody applied refuses BOTH halves", _refb, True)
        _ask102(_refb, "the start was not refused, see above",
                "(b) the refusal names the half, the pick and the head",
                "planner: the drop-down says sonnet, but the chain starts "
                "with opus" in _eb, True)
        _ask102(_refb, "the start was not refused, see above",
                "(b) and neither half was opened, nor the config written",
                [_opened102(_pb102, "executor") - _e0,
                 _opened102(_pb102, "planner") - _p0,
                 len(_saves102) - _s0], [0, 0, 0])

        _s0 = len(_saves102)
        _stale102["on"] = True
        _st, _rb = post_rc("/session", {
            "action": "launch_both", "project": _pb102,
            "chains": _BOTH102, "modes": _MODES102,
            "readonly_planner": True,
            "picked": {"executor": "opus", "planner": None}})
        _stale102["on"] = False
        _startedb = (_rb or {}).get("started") == ["executor", "planner"]
        check("(b) both halves start from the one request", _startedb, True)
        check("PRECONDITION: the stale config landed between the starts",
              _stale102["sent"], 1)
        _nsave102 = len(_saves102) - _s0 - _stale102["sent"]
        _ask102(_startedb, "the two halves did not start, see above",
                "(b) the start writes the chains it was sent to the config",
                _nsave102 >= 1, True)
        _ask102(_startedb, "the two halves did not start, see above",
                "(b) and writes them once, not once per half",
                _nsave102 <= 1, True)
        _ask102(_startedb, "the two halves did not start, see above",
                "(b) the stub saw both halves start on opus, the head it "
                "was sent, stale config or not",
                until(lambda: [_flag102(r, "--model") for r in
                               _rows102(_pb102, "executor")][-1:] == ["opus"]
                      and [_flag102(r, "--model") for r in
                           _rows102(_pb102, "planner")][-1:] == ["opus"], 30),
                True)
        _ask102(_startedb, "the two halves did not start, see above",
                "(b) each in the mode it was sent",
                [_flag102((_rows102(_pb102, "executor") or [{}])[-1],
                          "--permission-mode"),
                 _flag102((_rows102(_pb102, "planner") or [{}])[-1],
                          "--permission-mode")],
                ["bypassPermissions", "plan"])
    finally:
        daemon.handle_session = _real_hs102
        daemon.store.save_config = _real_save102
    note("config writes during the start, the stale one it had to survive "
         "included", len(_saves102) - _s0)
    check("(b) panel: 'start both' sends one request, no second start on a "
          "timer", 'action:"launch_both"' in _panel102
          and 'setTimeout(function(){launchRole("planner")},1200)'
          not in _panel102, True)

    print("   (d) /state and every helper it calls read a snapshot taken")
    print("       under the lock - forced: a record registers another")
    print("       while it is being read")
    for _key, _fnname in (("plans", "plan_for"), ("life", "life_view"),
                          ("walls", "wall_view")):
        _real102 = getattr(daemon, _fnname)
        _added = "executor:%s" % ("add-" + _key)[:8]
        _fired = {"n": 0}

        def _w102(*a, _real=_real102, _added=_added, _fired=_fired, **kw):
            if not _fired["n"] and sys._getframe(1).f_code.co_name \
                    == "do_GET":
                _fired["n"] = 1
                daemon.STATE["sessions"][_added] = _session102(_added)
            return _real(*a, **kw)

        setattr(daemon, _fnname, _w102)
        try:
            _st, _body = _get102("/state")
        finally:
            setattr(daemon, _fnname, _real102)
            _unplant102("sessions", _added)
        check("(d) /state answers while a window registers during its "
              "'%s'" % _key, [_st, _fired["n"]], [200, 1])
    _loop_key = canon(os.path.join(TMP, "caps-trap"))
    _plant102("loops", _loop_key, _Trap102(
        {"active": False, "iteration": 3}, daemon.STATE["loops"],
        canon(os.path.join(TMP, "caps-added")), {"active": False},
        "do_GET"))
    try:
        _st, _body = _get102("/state")
        _firedc = daemon.STATE["loops"][_loop_key].fired
    finally:
        _unplant102("loops", _loop_key, canon(os.path.join(TMP,
                                                           "caps-added")))
    # Not asked whether the record FIRED: on the snapshot the loop is read
    # from a copy, so the record that registers another is never walked at
    # all - which is the repair. The red run is what shows it fires.
    note("the loop record was walked live", _firedc)
    check("(d) /state answers while a loop registers during its 'caps'",
          _st, 200)

    _real_lv102 = daemon.life_view
    _addp = "executor:addpairs"[:17]
    _firedp = {"n": 0}

    def _lvp102(*a, **kw):
        if not _firedp["n"] and sys._getframe(1).f_code.co_name \
                == "pairs_view":
            _firedp["n"] = 1
            daemon.STATE["sessions"][_addp] = _session102(_addp)
        return _real_lv102(*a, **kw)

    daemon.life_view = _lvp102
    try:
        _st, _body = _get102("/state")
    finally:
        daemon.life_view = _real_lv102
        _unplant102("sessions", _addp)
    check("PRECONDITION: a window registered while the pairs were read",
          _firedp["n"], 1)
    _ask102(_st == 200, "/state did not answer, see above",
            "(d) the state returned is the snapshot: a window that "
            "registered after it was taken is not in it",
            _addp in (((_body or {}).get("state") or {}).get("sessions")
                      or {}), False)

    print("   the helpers, each read by itself with a record that registers")
    print("   another while that helper walks the dict")
    for _hname, _args in (("live_sessions", (_pg102,)), ("pair_paths", ())):
        _tk = "executor:trap-%s" % _hname[:4]
        _ak = "executor:add-%s" % _hname[:4]
        _plant102("sessions", _tk, _Trap102(
            _session102(_tk), daemon.STATE["sessions"], _ak,
            _session102(_ak), _hname))
        try:
            _res, _exc = _call102(getattr(daemon, _hname), *_args)
            _firedh = daemon.STATE["sessions"][_tk].fired
        finally:
            _unplant102("sessions", _tk, _ak)
        check("(d) %s answers while a window registers mid-walk" % _hname,
              [_exc, _firedh], ["", True])
    with daemon._lock:
        _down_was = dict(daemon.STATE.get("down") or {})
        _mode_was = daemon.STATE.get("mode")
        daemon.STATE["down"] = {}
        daemon.STATE["mode"] = "running"
    _lk = canon(os.path.join(TMP, "head-trap"))
    _la = canon(os.path.join(TMP, "head-added"))
    _plant102("loops", _lk, _Trap102(
        {"active": True}, daemon.STATE["loops"], _la, {"active": False},
        "status_headline"))
    try:
        _res, _exc = _call102(daemon.status_headline)
        _firedl = daemon.STATE["loops"][_lk].fired
    finally:
        _unplant102("loops", _lk, _la)
    check("(d) the headline answers while a loop registers mid-walk",
          [_exc, _firedl], ["", True])
    # A path with no folder behind it: while this record stands, nothing
    # that restarts a downed half can open a window for it.
    _dnone = canon(os.path.join(TMP, "down-trap-no-folder"))
    _dk, _dd = "%s|executor" % _dnone, "%s|planner" % _dnone
    _plant102("down", _dk, _Trap102(
        {"why": "a test record", "giveup": False}, daemon.STATE["down"],
        _dd, {"why": "added mid-walk"}, "status_headline"))
    try:
        _res, _exc = _call102(daemon.status_headline)
        _firedd = daemon.STATE["down"][_dk].fired
    finally:
        with daemon._lock:
            daemon.STATE["down"] = _down_was
            daemon.STATE["mode"] = _mode_was
            daemon.save_state()
    check("(d) the headline answers while a window goes down mid-walk",
          [_exc, _firedd], ["", True])

    print("   the crash bundle: the snapshot under the lock, the file after")
    _ck = "executor:trap-crash"[:17]
    _ca = "executor:add-crash"[:17]
    _plant102("sessions", _ck, _Trap102(
        _session102(_ck), daemon.STATE["sessions"], _ca, _session102(_ca),
        "_iterencode_dict", via="items"))
    try:
        _cd = daemon.crash_bundle("a test traceback for case 102")
        _ctext = read_or_fail(os.path.join(_cd, "state.json"),
                              "the bundle's state.json")
        try:
            _cstate = json.loads(_ctext) if _ctext else None
        except ValueError:
            _cstate = None
    finally:
        _unplant102("sessions", _ck, _ca)
    check("(d) the crash bundle's state.json is whole, with the record "
          "that was being read in it",
          _ck in ((_cstate or {}).get("sessions") or {}), True)

    print("   the standing gates - where the code stands, per site")
    for _key in ("caps", "plans", "life", "walls", "moved_from", "canon"):
        _node = _reply102.get(_key)
        check("(d) census: /state '%s' walks the snapshot, not STATE or CFG"
              % _key, _live_iters102(_node) if _node is not None
              else ["no such key"], [])
    _sn = _reply102.get("state")
    check("(d) census: /state sends the snapshot as its 'state'",
          isinstance(_sn, ast.Name) and _sn.id != "STATE", True)
    for _fname in ("live_sessions", "pair_paths", "status_headline"):
        _fn = _fns102.get(_fname)
        check("(d) census: %s walks a copy, not STATE or CFG" % _fname,
              _live_iters102(_fn) if _fn is not None else ["missing"], [])
    _withs = _lock_withs102(_state_if102) if _state_if102 is not None \
        else []
    _snap_under = any(
        any(c.args and isinstance(c.args[0], ast.Name)
            and c.args[0].id == "STATE" for c in _calls102(w, "dumps"))
        for w in _withs)
    check("(d) the /state snapshot is taken under the lock", _snap_under,
          True)
    _io_in_lock = sorted({n for w in _withs for n in (
        "plan_for", "life_view", "wall_view", "pairs_view",
        "status_headline", "recent_events", "load_calibration",
        "load_profiles", "load_models") if _calls102(w, n)})
    check("(d) and nothing that reads files runs under that lock",
          _io_in_lock, [])
    _cb = _fns102.get("crash_bundle")
    _cb_withs = _lock_withs102(_cb) if _cb is not None else []
    check("(d) crash_bundle writes its files outside the lock",
          sorted({c.lineno for w in _cb_withs for c in _calls102(w, "open")}),
          [])
    check("(d) census: crash_bundle writes the snapshot, not STATE",
          [c.lineno for c in (_calls102(_cb, "dump") if _cb is not None
                              else [])
           if c.args and "STATE" in _names102(c.args[0])], [])
    note("checks not asked in this run", len(_SKIP102),
         "0 when every mechanism works; each one names its reason above")
finally:
    with daemon._lock:
        for _k in (_ka102, _kb102, _kg102):
            (daemon.STATE.get("inflight") or {}).pop(_k, None)
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n103. a window asking a PERSON - a permission dialog or a question -")
print("     is named at once, in ONE message per dialog that says what to")
print("     press; the bridge presses nothing, and the dialog's end is one")
print("     line; the panel says what plan mode does and shows what starts")
print("    2026-09-23: a planner stood 62 minutes on the verdict tool's")
print("    dialog in plan mode with two reports queued behind it. The owner")
print("    got the notice's bare sentence and then, at 240 s, 'the model has")
print("    not answered' - which was false. All 30 such notices in the")
print("    journals read 'Claude needs your permission' and nothing more, and")
print("    6 of the 9 on a planner were AskUserQuestion, not a permission -")
print("    so what is asking is read from the half's own open call. Real")
print("    endpoints in the real order: PreToolUse, then the notice, then")
print("    what ends the dialog. A check whose input another mechanism makes")
print("    is asked only once that input exists. -> DECISIONS.md 8.26")
_pp103 = os.path.join(TMP, "asks")
_pq103 = os.path.join(TMP, "asks-idle")
_pr103 = os.path.join(TMP, "asks-screen")
for _d in (_pp103, _pq103, _pr103):
    os.makedirs(_d, exist_ok=True)
_kp103, _kq103, _kr103 = canon(_pp103), canon(_pq103), canon(_pr103)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pp103: {}, _pq103: {},
                              _pr103: {}}})
_SKIP103 = []
# The chat's own repeat filter would swallow a second identical message,
# and then a broken latch would look like a working one. Off for the case,
# so the only thing that can keep a dialog to one message is the latch.
_rep103 = (daemon.CFG.get("thresholds") or {}).get("notify_repeat_sec")
daemon.CFG.setdefault("thresholds", {})["notify_repeat_sec"] = 0
_nudge103 = daemon.CFG.get("nudge_console")
_scr103, _key103 = daemon.sessions.console_screen, \
    daemon.sessions.console_answer
_tr103 = sessions.transcript_of
_tdir103 = os.path.join(TMP, "asks-transcripts")
os.makedirs(_tdir103, exist_ok=True)
sessions.transcript_of = (lambda sid, path=None:
                          os.path.join(_tdir103, sid + ".jsonl")
                          if sid in ("pl-asks", "pl-scr")
                          else _tr103(sid, path))
# The daemon's names this case reads, through a fallback: on a daemon that
# does not have them the red is a FAIL line, never a traceback that takes
# the summary with it.
_label103 = getattr(daemon, "tool_label", None) or (lambda n: n)
_prompt103 = getattr(daemon, "permission_prompt", None) or (lambda s: None)
_answers103 = getattr(daemon, "ask_answered_by", None)


def _clock103(t):
    return time.strftime("%H:%M:%S", time.localtime(float(t or 0)))
_PAD103 = (" The report is written long on purpose: under IDLE_TURN_CHARS the "
           "idle damper would call the exchange empty and hold the Stop hook "
           "instead of making a report, which is right and is not what this "
           "case is about.")
_H, _MID, _MARK, _ELL = chr(0x2500), chr(0x00b7), chr(0x276f), chr(0x2026)
_RULE = _H * 60


def _titled103(title):
    return _H * 3 + " " + title + " " + _H * 40


# The client's own strings (2.1.280), never a live screen: none has been
# recorded yet, and the first live dialog is what the journal will keep.
_F1 = "\n".join([  # the one MEASURED form: the footer taken for the input line
    "  reading the report", _titled103("Tool use"),
    "   bridge - verdict (MCP)",
    "   Esc to cancel " + _MID + " Tab to amend", _RULE,
    "  Opus 5.5 | ctx 41%", "  plan mode on (shift+tab to cycle)"])
_F2 = "\n".join([  # the question and its choices, no footer
    "  Do you want to proceed?", "  " + _MARK + " 1. Yes",
    "    2. Yes, and don't ask again for bridge - verdict commands in "
    "c:\\path\\to\\project",
    "    3. No, and tell Claude what to do differently (esc)"])
_F3 = "\n".join([  # an empty prompt box
    "  the last answer", _RULE, "  " + _MARK + " ", _RULE,
    "  Opus 5.5 | ctx 12%", "  bypass permissions on (shift+tab to cycle)"])
_F4 = "\n".join([  # a turn that is open
    "  " + chr(0x2722) + " Perusing" + _ELL + " (7m 29s " + _MID + " "
    + chr(0x2193) + " 8.4k tokens)", _RULE, "  " + _MARK + " ", _RULE,
    "  Opus 5.5 | ctx 12%", "  bypass permissions on (shift+tab to cycle)"])
_F5 = "\n".join([  # a dialog of another kind
    _titled103("Accessing workspace"),
    "  Do you trust the files in this folder?", "  c:\\path\\to\\project",
    "  " + _MARK + " 1. Yes, proceed", "    2. No, exit",
    "  Enter to confirm " + _MID + " Esc to exit"])
_F6 = "\n".join([  # text somebody typed
    "  the last answer", _RULE,
    "  " + _MARK + " please carry on with the next piece", _RULE,
    "  Opus 5.5 | ctx 12%", "  bypass permissions on (shift+tab to cycle)"])
_F7 = "\n".join([  # the whole dialog, footer and question both
    "  reading the report", _titled103("Tool use"),
    "   bridge - verdict (MCP)", "   Do you want to proceed?",
    "   " + _MARK + " 1. Yes",
    "     2. Yes, and don't ask again for bridge - verdict commands in "
    "c:\\path\\to\\project",
    "     3. No, and tell Claude what to do differently (esc)",
    "   Esc to cancel " + _MID + " Tab to amend", _RULE,
    "  Opus 5.5 | ctx 41%", "  plan mode on (shift+tab to cycle)"])


def _ask103(ready, why, name, got, want):
    """A check whose input another mechanism makes, asked once it exists."""
    if ready:
        check(name, got, want)
    else:
        _SKIP103.append(name)
        print("  ..   not asked: %s - %s" % (name, why))


def _ev103(proj, role, sid, name, **kw):
    """One hook event, exactly as hook.py posts it."""
    body = {"hook_event_name": name, "role": role, "session_id": sid,
            "project_dir": proj, "cwd": proj}
    body.update(kw)
    return post("/event", body)


def _pre103(proj, role, sid, tid, tool, tin=None, agent=None):
    kw = {"tool_name": tool, "tool_input": tin or {}, "tool_use_id": tid}
    if agent:
        kw["agent_id"] = agent
    return _ev103(proj, role, sid, "PreToolUse", **kw)


def _post103(proj, role, sid, tid, tool, failed=False, agent=None):
    kw = {"tool_name": tool, "tool_input": {}, "tool_use_id": tid}
    if agent:
        kw["agent_id"] = agent
    if failed:
        kw["error"] = "The user doesn't want to proceed with this tool use."
    return _ev103(proj, role, sid,
                  "PostToolUseFailure" if failed else "PostToolUse", **kw)


def _notice103(proj, role, sid, ntype="permission_prompt",
               message="Claude needs your permission"):
    kw = {"message": message}
    if ntype is not None:
        kw["notification_type"] = ntype
    return _ev103(proj, role, sid, "Notification", **kw)


def _rec103(key, role):
    with daemon._lock:
        return dict((daemon.STATE.get("asks") or {})
                    .get("%s|%s" % (key, role)) or {})


def _book103(key, role):
    with daemon._lock:
        return dict(getattr(daemon, "OPEN_CALLS", {})
                    .get("%s|%s" % (key, role)) or {})


def _lines103(proj, needle):
    """This pair's journal lines holding `needle`, oldest first."""
    return [r.get("text") or "" for r in
            daemon.store.recent_events(100000, project=proj)[::-1]
            if needle in (r.get("text") or "")
            and r.get("path") == canon(proj)]


def _answered103(proj, role):
    return _lines103(proj, "%s permission answered after" % role)


def _reports103(key):
    return [d for d in DELIVERED.get((key, "planner"), [])
            if (d.get("meta") or {}).get("kind") == "report"]


def _turn103(proj, ex_sid, text):
    """One executor turn ending in a report nobody has answered yet."""
    key = canon(proj)
    before = len(_reports103(key))
    out = {}
    t = threading.Thread(target=lambda: out.update(
        stop_hook(proj, "executor", ex_sid, text) or {}), daemon=True)
    t.start()
    ok = until(lambda: len(_reports103(key)) > before, 15)
    n = str((_reports103(key)[-1].get("meta") or {}).get("report")) if ok \
        else "?"
    return t, ok, n


def _wrote103(sid, ago):
    """A planner's transcript: one entry, `ago` seconds back - the verdict
    tool's call, as the client writes it before its dialog."""
    with open(os.path.join(_tdir103, sid + ".jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(time.time() - ago))
                         + ".000Z",
            "message": {"content": [{"type": "tool_use", "id": "toolu_v103",
                                     "name": "mcp__bridge__verdict",
                                     "input": {}}]}}) + chr(10))


def _said103(since):
    """What went to the chat since `since`: the notices, not the pinned
    status block, which refresh_pin rewrites on a clock of its own."""
    return [t for t in tg_texts()[since:] if not t.startswith("bridge - ")]


def _rich103(m):
    return "window (pid " in (m or "")


try:
    for _proj, _key, _ex, _pl, _pids in (
            (_pp103, _kp103, "ex-asks", "pl-asks", (42104, 42103)),
            (_pr103, _kr103, "ex-scr", "pl-scr", (42106, 42105))):
        register(_proj, "executor", _ex)
        register(_proj, "planner", _pl)
        post("/loop", {"project": _proj, "action": "start"}, secret=True)
        with daemon._lock:
            for _role, _sid, _pid in (("executor", _ex, _pids[0]),
                                      ("planner", _pl, _pids[1])):
                daemon.STATE.setdefault("sessions", {})[
                    "%s:%s" % (_role, _sid[:8])] = {
                    "role": _role, "path": _key, "session_id": _sid,
                    "model": "Opus 5", "window": 1000000,
                    "window_observed": True, "context_tokens": 120000,
                    "state": "idle", "last_seen": daemon.now(),
                    "seen_at": time.time()}
                daemon.STATE.setdefault("last_session", {})[
                    "%s|%s" % (_key, _role)] = _sid
                # a record with its time, as every writer makes one, and the
                # planner windows declared alive, as the case treats them -
                # their screens are read and keyed (8.46)
                daemon.STATE.setdefault("pids", {})[
                    "%s|%s" % (_key, _role)] = {"pid": _pid,
                                                "at": time.time()}
                if _role == "planner":
                    DECLARED_ALIVE.add(_pid)
            daemon.save_state()

    print("\n   (a) the incident, in its order: a report goes to the planner,")
    print("   the planner calls the verdict tool, the client draws its")
    print("   dialog and sends the notice")
    _wrote103("pl-asks", 4000)
    _tg_a = len(tg_texts())
    _ta, _ok_a, _n_a = _turn103(_pp103, "ex-asks",
                                "report one, while the planner is asked for "
                                "permission." + _PAD103)
    check("(a) the report reached the planner's channel", _ok_a, True)
    _pre103(_pp103, "planner", "pl-asks", "toolu_v103",
            "mcp__bridge__verdict", {"verdict": "wait", "feedback": "held"})
    _wrote103("pl-asks", -1.5)   # its tool_use is written: the turn looks open
    _notice103(_pp103, "planner", "pl-asks")
    _ra = _rec103(_kp103, "planner")
    check("(a) the notice opens a dialog record", bool(_ra), True)
    _ask103(bool(_ra), "no dialog record",
            "(a) the dialog is pinned to the call that is waiting",
            (_ra.get("call"), _ra.get("tool")),
            ("toolu_v103", "mcp__bridge__verdict"))
    _ma = _said103(_tg_a)
    _ask103(bool(_ra), "no dialog record", "(a) one message, at once",
            len(_ma), 1)
    _m = _ma[0] if _ma else ""
    note("the messages", _ma)
    _ask103(bool(_ma), "no message went",
            "(a) the message is the dialog's own, naming the report "
            "waiting behind it",
            _rich103(_m) and ("report %s is waiting behind it for its "
                              "verdict" % _n_a) in _m, True)
    _ask103(_rich103(_m), "the message is not the dialog's own",
            "(a) it names the window's pid, what to press, and that the "
            "bridge presses nothing",
            ("planner window (pid 42103)" in _m,
             "1 allows it once, 2 allows it and stops asking about it here, "
             "Esc refuses it" in _m, "The bridge presses nothing" in _m),
            (True, True, True))
    _ask103(_rich103(_m) and bool(_ra.get("tool")),
            "the message is not the dialog's own, or its call was not named",
            "(a) it says a permission is asked, since when, and for which "
            "tool as its dialog names it",
            ("asking for permission since %s" % _clock103(_ra.get("at"))
             in _m, "(to use bridge - verdict (MCP))" in _m), (True, True))
    check("(a) the notice's line carries notification_type verbatim",
          any("(notification_type permission_prompt)" in x
              for x in _lines103(_pp103, "/ planner is ")), True)
    print("   the client sends the notice again for the same dialog")
    _notice103(_pp103, "planner", "pl-asks")
    _ask103(len(_ma) == 1, "the first message did not go",
            "(a) a second notice about the same dialog sends nothing",
            len(_said103(_tg_a)), 1)
    _tg_a2 = len(tg_texts())
    print("   240 s on (10 s in this suite) the report is still unanswered -")
    print("   the branch that said 'the model has not answered'")
    _l240a = "Report %s reached the planner's channel process, but its " \
             "window is" % _n_a
    _old103 = ("has not answered", "has not taken the prompt",
               "could not be read")
    until(lambda: _lines103(_pp103, _l240a) or any(
        o in x for x in _said103(_tg_a) for o in _old103), 25)
    _ask103(bool(_ra.get("tool")), "its call was not named",
            "(a) at 240 s it says its window is asking for permission, and "
            "since when",
            any("its window is asking for permission since %s"
                % _clock103(_ra.get("at")) in x
                for x in _lines103(_pp103, _l240a)), True)
    _ask103(len(_ma) == 1, "the first message did not go",
            "(a) and at 240 s nothing new is sent - nothing about a model "
            "that has not answered", _said103(_tg_a2), [])
    print("   a person presses 1: the verdict tool runs")
    _was_a = len(_answered103(_pp103, "planner"))
    _st_a, _bv_a = post_rc("/verdict", {"project": _pp103, "verdict": "wait",
                                        "feedback": "held: the dialog was "
                                                    "answered."})
    check("(a) the verdict is taken", (_st_a, (_bv_a or {}).get("ok")),
          (200, True))
    _ta.join(30)
    _aa = _answered103(_pp103, "planner")[_was_a:]
    note("the line", _aa[-1] if _aa else "")
    _ask103(bool(_ra), "no dialog record",
            "(a) the verdict closes the dialog: one line, by the verdict",
            ["by the verdict reaching the bridge (/verdict wait)" in x
             for x in _aa], [True])
    _ask103(bool(_ra), "no dialog record", "(a) and its record is gone",
            _rec103(_kp103, "planner"), {})
    _post103(_pp103, "planner", "pl-asks", "toolu_v103",
             "mcp__bridge__verdict")
    _ask103(bool(_ra), "no dialog record",
            "(a) the tool's own return after it writes no second line",
            len(_answered103(_pp103, "planner")[_was_a:]), 1)

    print("\n   (c) the same notice for a QUESTION: the planner asks the owner")
    print("   with AskUserQuestion while report 2 waits behind it")
    _tg_c = len(tg_texts())
    _tc, _ok_c, _n_c = _turn103(_pp103, "ex-asks",
                                "report two, while the planner asks its "
                                "owner a question." + _PAD103)
    check("(c) the report reached the planner's channel", _ok_c, True)
    _pre103(_pp103, "planner", "pl-asks", "toolu_q103", "AskUserQuestion",
            {"questions": [{"question": "Which way?"}]})
    _notice103(_pp103, "planner", "pl-asks")
    _rc = _rec103(_kp103, "planner")
    check("(c) the notice opens a dialog record", bool(_rc), True)
    _ask103(bool(_rc), "no dialog record", "(c) it is pinned to the question",
            (_rc.get("call"), _rc.get("tool")),
            ("toolu_q103", "AskUserQuestion"))
    _mc = _said103(_tg_c)
    _ask103(bool(_rc), "no dialog record", "(c) one message, at once",
            len(_mc), 1)
    _m = _mc[0] if _mc else ""
    note("the message", _m)
    _ask103(bool(_mc), "no message went",
            "(c) the message is the dialog's own, naming the report "
            "waiting behind it",
            _rich103(_m) and ("report %s is waiting behind it" % _n_c) in _m,
            True)
    _q103 = _rich103(_m) and _rc.get("tool") == "AskUserQuestion"
    _ask103(_q103, "not the dialog's own message about a named question",
            "(c) it says a question is being asked, since when - and not "
            "that a permission is",
            ("asking you a question since %s (AskUserQuestion)"
             % _clock103(_rc.get("at")) in _m, "permission" in _m),
            (True, False))
    _ask103(_q103, "not the dialog's own message about a named question",
            "(c) it says how a question is answered",
            "Answer it in that window: pick one of its choices" in _m, True)
    print("   a verdict arrives - a second window of this half can send one")
    print("   (5.43) - and the question is still on the screen")
    _st_c, _bv_c = post_rc("/verdict", {"project": _pp103, "verdict": "wait",
                                        "feedback": "held."})
    check("(c) the verdict is taken", (_st_c, (_bv_c or {}).get("ok")),
          (200, True))
    _tc.join(30)
    _cmp103 = bool(_answers103) and _answers103(
        {"tool": "AskUserQuestion"}, tool="mcp__bridge__verdict") is False
    _ask103(_rc.get("tool") == "AskUserQuestion" and _cmp103,
            "the dialog was not named a question, or no tool is compared",
            "(c) a verdict does not close a question",
            _rec103(_kp103, "planner").get("call"), "toolu_q103")
    _open_c = bool(_rec103(_kp103, "planner"))
    _was_c = len(_answered103(_pp103, "planner"))
    _post103(_pp103, "planner", "pl-asks", "toolu_q103", "AskUserQuestion",
             failed=True)
    _ask103(_open_c, "the dialog was already closed",
            "(c) the question's own call failing closes it: one line, by it",
            [("by its own call failing (PostToolUseFailure, AskUserQuestion)"
              in x) for x in _answered103(_pp103, "planner")[_was_c:]],
            [True])

    print("\n   (h) no notice at all - a client that sends none, or a hook")
    print("   that failed - and the screen is what finds the dialog")
    _wrote103("pl-scr", 4000)
    daemon.CFG["nudge_console"] = True
    _keys103 = []
    daemon.sessions.console_screen = lambda pid, timeout=20: _F7
    daemon.sessions.console_answer = \
        lambda pid, timeout=20: _keys103.append(pid) or True
    _tg_h = len(tg_texts())
    _th, _ok_h, _n_h = _turn103(_pr103, "ex-scr",
                                "report one, into a window drawing a "
                                "dialog." + _PAD103)
    check("(h) the report reached the planner's channel", _ok_h, True)
    _l240h = "Report %s reached the planner's channel process, but its " \
             "window is" % _n_h
    until(lambda: _lines103(_pr103, _l240h) or any(
        o in x for x in _said103(_tg_h) for o in _old103), 25)
    _rh = _rec103(_kr103, "planner")
    check("(h) the screen's dialog opens a record, named from the screen",
          (_rh.get("via"), _rh.get("tool")),
          ("screen", "bridge - verdict (MCP)"))
    check("(h) and no key goes into it", _keys103, [])
    _ask103(bool(_rh), "the screen opened no record",
            "(h) the screen goes into the journal whole",
            any("The screen:" in x and "Esc to cancel" in x
                and "No, and tell Claude" in x
                for x in _lines103(_pr103, "opened no turn for")), True)
    _mh = _said103(_tg_h)
    note("the messages", _mh)
    _ask103(bool(_rh), "the screen opened no record",
            "(h) one message, the dialog's own, naming the report behind it",
            [_rich103(x) and ("report %s is waiting behind it" % _n_h) in x
             and "asking for permission since" in x for x in _mh], [True])
    _ask103(bool(_rh), "the screen opened no record",
            "(h) at 240 s it says its window is asking for permission",
            any("its window is asking for permission since" in x
                for x in _lines103(_pr103, _l240h)), True)
    _ask103(bool(_rh), "the screen opened no record",
            "(h) and the window is not counted as deaf",
            (daemon.STATE.get("deaf") or {}).get("%s|planner" % _kr103), None)
    _post103(_pr103, "planner", "pl-scr", "toolu_r103", "Read")
    _ask103(bool(_rh) and not _rh.get("call"),
            "the screen opened no record, or it was pinned to a call",
            "(h) a Read returning does not close the dialog the screen named",
            _rec103(_kr103, "planner").get("via"), "screen")
    _open_h = bool(_rec103(_kr103, "planner"))
    _same103 = _label103("mcp__bridge__verdict") == \
        "bridge - verdict (MCP)"
    _was_h = len(_answered103(_pr103, "planner"))
    _st_h, _bv_h = post_rc("/verdict", {"project": _pr103, "verdict": "wait",
                                        "feedback": "held."})
    _th.join(30)
    _ask103(_open_h and _same103,
            "the dialog was already closed, or the tool's two names differ",
            "(h) the verdict closes the dialog the screen named",
            [("by the verdict reaching the bridge" in x)
             for x in _answered103(_pr103, "planner")[_was_h:]], [True])
    daemon.sessions.console_screen, daemon.sessions.console_answer = \
        _scr103, _key103
    daemon.CFG["nudge_console"] = _nudge103

    print("\n   (b) the executor, loop on: two Bash calls open, the notice")
    print("   known only by its type, the dialog about the newest call")
    _tg_b = len(tg_texts())
    _pre103(_pp103, "executor", "ex-asks", "toolu_x103", "Bash",
            {"command": "git status"})
    _pre103(_pp103, "executor", "ex-asks", "toolu_b103", "Bash",
            {"command": "git push origin main"})
    _notice103(_pp103, "executor", "ex-asks",
               message="Claude is asking before it runs a command")
    _rb = _rec103(_kp103, "executor")
    check("(b) a notice known only by its type opens a dialog record",
          bool(_rb), True)
    _ask103(bool(_rb), "no dialog record",
            "(b) it is pinned to the newest open call",
            (_rb.get("call"), _rb.get("tool")), ("toolu_b103", "Bash"))
    _mb = _said103(_tg_b)
    _ask103(bool(_rb), "no dialog record", "(b) one message, at once",
            len(_mb), 1)
    _m = _mb[0] if _mb else ""
    note("the message", _m)
    _ask103(bool(_mb), "no message went",
            "(b) the message is the dialog's own, saying the loop waits on it",
            _rich103(_m) and "the loop is on and the executor's turn stands "
                             "on it" in _m, True)
    _ask103(_rich103(_m) and bool(_rb.get("tool")),
            "the message is not the dialog's own, or its call was not named",
            "(b) it names the window and the tool",
            ("executor window (pid 42104)" in _m,
             "asking for permission since %s (to use Bash)"
             % _clock103(_rb.get("at")) in _m), (True, True))
    _post103(_pp103, "executor", "ex-asks", "toolu_x103", "Bash")
    _ask103(bool(_rb.get("call")), "the dialog was not pinned to a call",
            "(b) another Bash call returning does not close it",
            _rec103(_kp103, "executor").get("call"), "toolu_b103")
    check("(b) a call that returned is off the book",
          "toolu_x103" in _book103(_kp103, "executor"), False)
    _open_b = bool(_rec103(_kp103, "executor"))
    _post103(_pp103, "executor", "ex-asks", "toolu_b103", "Bash")
    _ab = _answered103(_pp103, "executor")
    note("the line", _ab[-1] if _ab else "")
    _ask103(_open_b, "the dialog was already closed",
            "(b) its own call returning closes it: one line, by that call",
            ["by its own call returning (PostToolUse, Bash)" in x
             for x in _ab], [True])

    print("\n   (s) a background subagent's call asks, and the main turn ends")
    print("   while its dialog is still up")
    _pre103(_pq103, "executor", "ex-s103", "toolu_g103", "Bash",
            {"command": "git push"}, agent="ag103")
    _notice103(_pq103, "executor", "ex-s103", ntype=None)
    _rs = _rec103(_kq103, "executor")
    _ask103(bool(_rs), "no dialog record",
            "(s) it is pinned to the subagent's call",
            (_rs.get("call"), _rs.get("agent")), ("toolu_g103", "ag103"))
    _ev103(_pq103, "executor", "ex-s103", "Stop",
           last_assistant_message="the main turn ends")
    _ask103(bool(_rs.get("agent")), "the dialog was not pinned to a subagent",
            "(s) the main turn ending does not close a subagent's dialog",
            _rec103(_kq103, "executor").get("call"), "toolu_g103")
    _open_s = bool(_rec103(_kq103, "executor"))
    _post103(_pq103, "executor", "ex-s103", "toolu_g103", "Bash",
             agent="ag103")
    _ask103(_open_s, "the dialog was already closed",
            "(s) its own call returning closes it",
            ["by its own call returning (PostToolUse, Bash)" in x
             for x in _answered103(_pq103, "executor")], [True])

    print("\n   (d) nothing waits behind it - the loop is off - and the notice")
    print("   is known only by its words: the ordinary line is its message")
    _tg_d = len(tg_texts())
    _pre103(_pq103, "executor", "ex-d103", "toolu_d103", "Bash",
            {"command": "git push"})
    _notice103(_pq103, "executor", "ex-d103", ntype=None)
    _rd = _rec103(_kq103, "executor")
    check("(d) a notice known only by its words opens a dialog record",
          bool(_rd), True)
    _md = _said103(_tg_d)
    _ask103(bool(_rd), "no dialog record",
            "(d) the ordinary needs-you line is its one message",
            [("asks-idle / executor needs you: Claude needs your permission"
              in x) for x in _md], [True])
    _ask103(bool(_rd), "no dialog record", "(d) and it takes the latch",
            bool(_rec103(_kq103, "executor").get("told")), True)
    _notice103(_pq103, "executor", "ex-d103", ntype=None)
    _ask103(bool(_rd) and len(_md) == 1, "no record, or no single message",
            "(d) a second notice about the same dialog sends nothing",
            len(_said103(_tg_d)), 1)
    print("   the client draws its NEXT dialog, about another call - the")
    print("   one on record is over, because it draws one at a time")
    _pre103(_pq103, "executor", "ex-d103", "toolu_d2103", "Bash",
            {"command": "git push --tags"})
    _tg_d2 = len(tg_texts())
    _was_d1 = len(_answered103(_pq103, "executor"))
    _notice103(_pq103, "executor", "ex-d103", ntype=None)
    _ask103(bool(_rd.get("call")), "the dialog was not pinned to a call",
            "(d) a notice about another call closes the one on record, "
            "with its line",
            ([("by the next dialog of this half being drawn" in x)
              for x in _answered103(_pq103, "executor")[_was_d1:]],
             _rec103(_kq103, "executor").get("call")), ([True], "toolu_d2103"))
    _ask103(bool(_rd.get("call")), "the dialog was not pinned to a call",
            "(d) and the new dialog has its own message",
            len(_said103(_tg_d2)), 1)
    _pre103(_pq103, "executor", "ex-d103", "toolu_y103", "Read")
    _open_d = bool(_rec103(_kq103, "executor"))
    _was_d2 = len(_answered103(_pq103, "executor"))
    _ev103(_pq103, "executor", "ex-d103", "Stop",
           last_assistant_message="the turn ends")
    _ask103(_open_d, "the dialog was already closed",
            "(d) the turn ending closes it: one line, by the turn ending",
            [("by the turn ending (Stop)" in x)
             for x in _answered103(_pq103, "executor")[_was_d2:]], [True])
    check("(d) a turn that ended leaves nothing of its own open",
          sorted(t for t, c in _book103(_kq103, "executor").items()
                 if c.get("sid") == "ex-d103"), [])

    print("\n   (e) what else ends a dialog - each once, and not a stranger")
    for _sid, _name, _kw, _by in (
            ("ex-e1", "StopFailure", {"error_type": "server_error",
                                      "error": "Internal server error"},
             "by the turn dying (StopFailure)"),
            ("ex-e2", "SessionEnd", {"reason": "other"},
             "by the session ending (SessionEnd)")):
        _pre103(_pq103, "executor", _sid, "toolu_%s" % _sid, "Bash",
                {"command": "git push"})
        _notice103(_pq103, "executor", _sid, ntype=None)
        _open_e = _rec103(_kq103, "executor").get("sid") == _sid
        _was_e = len(_answered103(_pq103, "executor"))
        _ev103(_pq103, "executor", _sid, _name, **_kw)
        _ask103(_open_e, "no dialog of this session on record",
                "(e) %s closes it: one line, by it"
                % _by[len("by "):].split(" (")[0],
                [(_by in x) for x in _answered103(_pq103, "executor")[_was_e:]],
                [True])
    _pre103(_pq103, "executor", "ex-e3", "toolu_e3", "Bash",
            {"command": "git push"})
    _notice103(_pq103, "executor", "ex-e3", ntype=None)
    _re3 = _rec103(_kq103, "executor")
    print("   another window of this half comes up (a handover's minute)")
    _ev103(_pq103, "executor", "ex-e4", "SessionStart", source="startup")
    _ask103(_re3.get("sid") == "ex-e3", "no dialog of the old session",
            "(e) another session starting does not close it",
            _rec103(_kq103, "executor").get("sid"), "ex-e3")
    _open_e3 = _rec103(_kq103, "executor").get("sid") == "ex-e3"
    _was_e3 = len(_answered103(_pq103, "executor"))
    print("   and ITS notice arrives with no call of its own on the book -")
    print("   its PreToolUse went to a daemon that has since restarted")
    _notice103(_pq103, "executor", "ex-e4", ntype=None)
    _ask103(_open_e3, "no dialog of the old session was open",
            "(e) a notice from another session closes the one on record, "
            "saying nothing saw it answered",
            [("another session of this half asking" in x
              and "nothing saw this one answered" in x)
             for x in _answered103(_pq103, "executor")[_was_e3:]], [True])
    _open_e4 = _rec103(_kq103, "executor").get("sid") == "ex-e4"
    _was_e4 = len(_answered103(_pq103, "executor"))
    _ev103(_pq103, "executor", "ex-e4", "SessionStart", source="resume")
    _ask103(_open_e4, "the new session's dialog is not the one on record",
            "(e) its own session starting again closes it",
            [("by the session starting again (SessionStart)" in x)
             for x in _answered103(_pq103, "executor")[_was_e4:]], [True])

    print("\n   (j) the idle notice is not a dialog, and keeps its old path")
    _before_j = _rec103(_kq103, "executor")
    _notice103(_pq103, "executor", "ex-j103", ntype="idle_prompt",
               message="Claude is waiting for your input")
    check("(j) an idle notice opens no dialog record and changes none",
          _rec103(_kq103, "executor"), _before_j)
    check("(j) its line carries notification_type verbatim",
          any("is idle at the prompt (notification_type idle_prompt)" in x
              for x in _lines103(_pq103, "is idle at the prompt")), True)

    print("\n   (f) the screen: which of the client's screens is a dialog")
    _pp = _prompt103
    _f1, _f2 = _pp(_F1), _pp(_F2)
    check("(f) the footer the prompt line took, 2026-09-23, is a dialog",
          ((_f1 or {}).get("hint"), (_f1 or {}).get("tool")),
          (True, "bridge - verdict (MCP)"))
    check("(f) the question over its choices is a dialog, footer or not",
          ((_f2 or {}).get("question"), len((_f2 or {}).get("options") or [])),
          (True, 3))
    check("(f) not a dialog: an empty prompt, an open turn, another "
          "dialog, typed text",
          [_pp(x) for x in (_F3, _F4, _F5, _F6)], [None, None, None, None])
    check("(f) the whole dialog: its tool and its three choices",
          (_pp(_F7) is not None, (_pp(_F7) or {}).get("tool"),
           len((_pp(_F7) or {}).get("options") or [])),
          (True, "bridge - verdict (MCP)", 3))
    check("(f) a tool is named as its dialog names it",
          (_label103("mcp__bridge__verdict"), _label103("Bash")),
          ("bridge - verdict (MCP)", "Bash"))
    _src_n = inspect.getsource(daemon.nudge_deaf_window)
    _i = [_src_n.find(x) for x in ("spinner_line(screen)",
                                   "permission_prompt(screen)",
                                   "if not empty:")]
    check("(f) the window asks it after the spinner and before the empty "
          "prompt decides", -1 not in _i and _i == sorted(_i), True)

    print("\n   (i) the panel: what plan mode does, and what starts")
    _panel103 = read_or_fail(os.path.join(os.path.dirname(os.path.abspath(
        daemon.__file__)), "panel.html"), "panel.html")
    check("(i) the launch window no longer claims no mode asks, and says "
          "what plan mode does",
          ("pre-approved in every mode" in _panel103,
           "In plan mode the client can still stop and ask a person before "
           "the verdict tool runs" in _panel103,
           "it never presses anything in it" in _panel103),
          (False, True, True))
    check("(i) the drop-down shows the person's pick, else its own chain's "
          "head",
          bool(re.search(
              r"var pick=\(window\._picked\|\|\{\}\)\[role\];"
              r"[\s\S]{0,400}if\(pick&&models\.indexOf\(pick\)>=0\)"
              r"sel\.value=pick;\s*else if\(head\)sel\.value=head;",
              _panel103))
          and "head=(CHAINS[role]||[])[0]" in _panel103, True)
    note("checks not asked in this run", len(_SKIP103),
         "0 when every mechanism works; each one names its reason above")
finally:
    sessions.transcript_of = _tr103
    daemon.sessions.console_screen, daemon.sessions.console_answer = \
        _scr103, _key103
    daemon.CFG["nudge_console"] = _nudge103
    if _rep103 is None:
        daemon.CFG["thresholds"].pop("notify_repeat_sec", None)
    else:
        daemon.CFG["thresholds"]["notify_repeat_sec"] = _rep103
    with daemon._lock:
        for _k in (_kp103, _kq103, _kr103):
            for _r in ("executor", "planner"):
                (daemon.STATE.get("asks") or {}).pop("%s|%s" % (_k, _r), None)
                (daemon.STATE.get("pids") or {}).pop("%s|%s" % (_k, _r), None)
        daemon.save_state()
    DECLARED_ALIVE.difference_update({42103, 42105})
    for _proj in (_pp103, _pr103):
        post("/loop", {"project": _proj, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n104. an ACCOUNT whose allowance is spent holds the whole bridge at")
print("     once, is said ONCE, is never revived, keeps what comes due, and")
print("     ends at the reset time it named or at the first finished turn")
print("    2026-09-23 20:47 to 2026-09-25 03:00, this bridge's own executor:")
print("    You've hit your weekly limit - resets 3am (Etc/GMT-3), filed by the")
print("    client as rate_limit with no error_details. The bridge read the")
print("    category and not the sentence: 287 deaths, 188 hand-backs, 62")
print("    'tried 3 times' each with a crash message, 94 clinch wakes - and")
print("    456 Telegram messages on 2026-09-24 alone (355 limit_low, 101")
print("    crash). The client names the reset's date while it is a day or")
print("    more away and drops it after; both forms are here. Real order:")
print("    status lines, a Stop hook blocked on its report, StopFailures in")
print("    the kept payload's shape, the real check_lost_turn tick after a")
print("    grace really waited out. Each part sets up its own preconditions,")
print("    so a broken mechanism reddens its own checks and not the next")
print("    part's. -> DECISIONS.md 8.28")
import datetime as _dt104                                 # noqa: E402
import json as _json104                                   # noqa: E402
_pa104 = os.path.join(TMP, "acct-p")
_qa104 = os.path.join(TMP, "acct-q")
for _d in (_pa104, _qa104):
    os.makedirs(_d, exist_ok=True)
_kp104, _kq104 = canon(_pa104), canon(_qa104)
_EQ104 = "%s|executor" % _kq104
# One model per chain: a spent window has nowhere to drop to, so what is
# under test is the hold and not the older rotation to the next model.
_ONE104 = {"chains": {"executor": ["opus"], "planner": ["fable"]}}
post("/config", {"projects": {A: {}, B: {}, C: {}, _pa104: dict(_ONE104),
                              _qa104: dict(_ONE104)}})
_exp104, _plp104, _exq104, _plq104 = ("acct104-pe", "acct104-pp",
                                      "acct104-qe", "acct104-qp")
for _proj, _pair in ((_pa104, (_exp104, _plp104)),
                     (_qa104, (_exq104, _plq104))):
    post_rc("/loop", {"action": "start", "project": _proj})
    for _r, _sid in zip(("executor", "planner"), _pair):
        post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                           "session_id": _sid, "project_dir": _proj,
                           "cwd": _proj})
        register(_proj, _r, _sid)
daemon.CFG["telegram"] = {"token": "test-token", "chat_id": "42",
                          "pinned_message_id": 0}
_thr104 = dict(daemon.CFG.get("thresholds") or {})
# The grace is short and really waited out. The repeat filter is off, so
# that only the bridge's own latches stand between a death and a message:
# with it on, a message three minutes apart - the real storm's spacing -
# would be let through and a message two seconds apart would not, and the
# case would be testing the filter instead of the latch.
_GRACE104 = 2.0
daemon.CFG["thresholds"].update({"review_timeout": 120,
                                 "channel_silence_warn": 110,
                                 "stopfail_grace": _GRACE104,
                                 "idle_hold": 0, "notify_repeat_sec": 0})
_TZ104 = _dt104.timezone(_dt104.timedelta(hours=3))
_MON104 = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec")
_SAID104 = "You've hit your weekly limit %s resets 3am (Etc/GMT-3)" % chr(183)
# The 07.09 form, with its date - four days ahead, as it was on the day,
# so the case does not depend on the calendar it runs on.
_ahead104 = (_dt104.datetime.fromtimestamp(time.time(), _TZ104)
             + _dt104.timedelta(days=4))
_DATED104 = ("You've hit your weekly limit %s resets %s %d, 3am (Etc/GMT-3)"
             % (chr(183), _MON104[_ahead104.month - 1], _ahead104.day))
_UNTIL104 = _ahead104.replace(hour=3, minute=0, second=0,
                              microsecond=0).timestamp()
_MODEL104 = ("You've reached your Fable limit. Run /usage-credits to "
             "continue or switch models with /model.")
_RATE104 = "no model left in the chain"
_CRASH104 = "picked it back up"
_HOOK104 = {}
_SKIP104 = []


def _ask104(ready, why, name, got, want):
    """A check whose input another mechanism makes, asked once it exists."""
    if ready:
        check(name, got, want)
    else:
        _SKIP104.append(name)
        print("  ..   not asked: %s - %s" % (name, why))


def _next3am104(t):
    """The next 03:00 at UTC+3 after t - worked out here, independently of
    the bridge's own reading of the sentence."""
    here = _dt104.datetime.fromtimestamp(t, _TZ104)
    cand = here.replace(hour=3, minute=0, second=0, microsecond=0)
    if cand <= here:
        cand += _dt104.timedelta(days=1)
    return cand.timestamp()


def _status104(proj, role, sid, five=None, model=None):
    exe = role == "executor"
    display, mid = model or (("Opus 5 (1M context)", "claude-opus-5") if exe
                             else ("Fable 5.1", "claude-fable-5-1"))
    payload = {
        "session_id": sid,
        "workspace": {"current_dir": proj, "project_dir": proj},
        "model": {"display_name": display, "id": mid},
        "context_window": {
            "context_window_size": 1000000, "used_percentage": 20.0,
            "current_usage": {"input_tokens": 10,
                              "cache_creation_input_tokens": 90,
                              "cache_read_input_tokens": 199900,
                              "output_tokens": 100}}}
    if five is not None:
        payload["rate_limits"] = {"five_hour": {"used_percentage": five,
                                                "resets_at": "later"}}
    post("/status", {"role": role, "payload": payload})


def _death104(proj, role, sid, said):
    """A StopFailure in the shape of the payloads kept on the day - error
    rate_limit and NO error_details, which an account's limit never had.
    `said` None is the same payload with no sentence at all."""
    body = {"session_id": sid,
            "transcript_path": os.path.join(TMP, "%s.jsonl" % sid),
            "cwd": proj, "prompt_id": "prompt-%s" % sid,
            "effort": {"level": "max"},
            "hook_event_name": "StopFailure", "error": "rate_limit"}
    if said is not None:
        body["last_assistant_message"] = said
    body.update({"project_dir": proj, "role": role})
    return post("/event", body)


def _acct104():
    return dict(daemon.STATE.get("account_limit") or {})


def _pause104():
    """(the bridge's mode, the kind of limit its pause is for)."""
    pl = daemon.STATE.get("paused_by_limit")
    kind = pl.get("kind") if isinstance(pl, dict) else (
        "five_hour" if pl else None)
    return daemon.STATE.get("mode"), kind


def _kept104(proj, what):
    return list(((daemon.STATE.get("held") or {}).get(canon(proj))
                 or {}).get(what) or [])


def _to104(proj, role):
    return list(DELIVERED.get((canon(proj), role)) or [])


def _chat104(sub):
    return [t for t in tg_texts() if sub in t]


def _lines104(sub, proj=None):
    """Journal lines containing `sub`: the project's own file, or the
    central day for a line about the whole bridge."""
    if proj:
        f = os.path.join(proj, "bridge-logs", time.strftime("%Y-%m-%d"),
                         "events.jsonl")
    else:
        f = os.path.join(daemon.store.day_dir(), "events.jsonl")
    out = []
    if os.path.isfile(f):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    t = _json104.loads(line).get("text") or ""
                except Exception:
                    continue
                if sub in t:
                    out.append(t)
    return out


def _reports104(items):
    return sorted(sum((re.findall(r"Executor report (\d+):",
                                  body_of((d or {}).get("content") or ""))
                       for d in items), []))


def _pending104(proj):
    return str((((daemon.PENDING.get(canon(proj)) or {}).get("meta") or {})
                .get("report")))


def _iter104(proj):
    return int(((daemon.STATE.get("loops") or {}).get(canon(proj))
                or {}).get("iteration") or 0)


def _pass104(key):
    """The grace, really waited out - past the record's own stamp."""
    with daemon._lock:
        rec = (daemon.STATE.get("stopfail") or {}).get(key) or {}
        due = float(rec.get("at") or time.time()) + _GRACE104
    time.sleep(max(0.0, due - time.time()) + 0.1)


def _clean104():
    """A part's own preconditions: no limit of either scope, no dead turn,
    no latch, no hold, the bridge running."""
    with daemon._lock:
        daemon.STATE.pop("account_limit", None)
        daemon.STATE.pop("held", None)
        daemon.STATE.pop("paused_by_limit", None)
        daemon.STATE["mode"] = "running"
        for _k in (_kp104, _kq104):
            for _r in ("executor", "planner"):
                for _c in ("model_limit", "stopfail", "rate_told"):
                    (daemon.STATE.get(_c) or {}).pop("%s|%s" % (_k, _r),
                                                     None)
        daemon.save_state()
    for _p in (_pa104, _qa104):
        if (daemon.STATE.get("paused") or {}).get(canon(_p)):
            daemon.resume_project(_p)


def _stop104(proj, sid, text, tag):
    _t0 = time.time()
    _out = stop_hook(proj, "executor", sid, text)
    _HOOK104[tag] = {"out": _out, "secs": time.time() - _t0}


def _hook104(proj, sid, text, tag):
    _th = threading.Thread(target=_stop104, args=(proj, sid, text, tag),
                           daemon=True)
    _th.start()
    return _th


def _answer104(proj, text):
    with open(os.path.join(proj, "seen104.txt"), "w", encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    post("/verdict", {"project": proj, "verdict": "done",
                      "feedback": "Checked: seen104.txt\n%s" % text},
         secret=True)


def _pstop104(proj, sid, text):
    """The planner finishes a turn - a Stop that reviews nothing."""
    post("/event", {"hook_event_name": "Stop", "role": "planner",
                    "session_id": sid, "project_dir": proj, "cwd": proj,
                    "last_assistant_message": text})


try:
    _clean104()
    note("the bridge's mode as the case starts", daemon.STATE.get("mode"),
         "running is the precondition")
    for _p, _pair in ((_pa104, (_exp104, _plp104)),
                      (_qa104, (_exq104, _plq104))):
        _status104(_p, "executor", _pair[0])
        _status104(_p, "planner", _pair[1])

    print("   (a) P's report is out and its executor's Stop hook waits on it")
    _it104 = _iter104(_pa104)
    _hook104(_pa104, _exp104, "report one: the level loads", "a")
    check("(a) the report reached P's planner and waits for its verdict",
          until(lambda: _pending104(_pa104) == str(_it104 + 1), 20), True)
    _n104 = str(_it104 + 1)
    tg_reset()

    print("   (b) P's planner dies with the account's sentence - the form of")
    print("   24-25.09, no date, because the reset is less than a day away")
    _tb104 = time.time()
    _death104(_pa104, "planner", _plp104, _SAID104)
    _rec104 = _acct104()
    check("(b) the account's sentence opens ONE record for the whole bridge",
          (_rec104.get("kind"), _rec104.get("said")), ("weekly", _SAID104))
    _ask104(bool(_rec104), "there is no record to read it from",
            "(b) its reset is the next 03:00 at UTC+3, read from a sentence "
            "that names no date",
            abs(float(_rec104.get("until") or 0) - _next3am104(_tb104)) < 2,
            True)
    check("(b) the whole bridge is paused, and the pause names its kind",
          _pause104(), ("paused", "weekly"))
    _pb104 = _pause104() == ("paused", "weekly")
    check("(b) P's executor, blocked on its report, is let go at once",
          until(lambda: "a" in _HOOK104, 10), True)
    check("(b) the report is kept for P's planner, not dropped",
          [str(r.get("n")) for r in _kept104(_pa104, "reports")], [_n104])
    check("(b) ONE message reaches the chat, with the client's sentence",
          until(lambda: len(_chat104(_SAID104)) >= 1, 5)
          and len(_chat104(_SAID104)), 1)
    _m104 = (_chat104(_SAID104) or [""])[0]
    print("   the message: %s" % _m104[:500])
    _ask104(bool(_m104), "no message to read",
            "(b) it carries no pair's colour - it is about the account",
            [m for m in getattr(daemon, "PAIR_MARKS", ()) if m in _m104], [])
    _ask104(bool(_m104) and bool(_rec104.get("until")),
            "no message, or no reset time to look for in it",
            "(b) and it says when it resets, on this machine's clock",
            time.strftime("%H:%M", time.localtime(
                float(_rec104.get("until") or 0))) in _m104, True)

    print("   (c) Q's executor dies with the same sentence: one account, one")
    print("   episode")
    _said104 = len(_chat104(_SAID104))
    _death104(_qa104, "executor", _exq104, _SAID104)
    check("(c) a second window's death is the same episode - one record, "
          "two deaths", _acct104().get("deaths"), 2)
    _ask104(_said104 == 1, "the first message never went",
            "(c) and nobody is told again", len(_chat104(_SAID104)), 1)

    print("   (d) the real tick, the grace really waited out, both pairs")
    _bp104 = len(_to104(_pa104, "planner"))
    _bq104 = len(_to104(_qa104, "executor"))
    _pass104("%s|planner" % _kp104)
    _pass104(_EQ104)
    daemon.check_lost_turn(_pa104)
    daemon.check_lost_turn(_qa104)
    check("(d) nothing is handed back into a window of a spent account",
          (len(_to104(_pa104, "planner")) - _bp104,
           len(_to104(_qa104, "executor")) - _bq104), (0, 0))
    check("(d) no hand-back is even attempted",
          len(_lines104("(attempt 1 of ", _pa104))
          + len(_lines104("(attempt 1 of ", _qa104)), 0)
    check("(d) and no 'rate limit hit' message goes out per death",
          len(_chat104(_RATE104)), 0)
    # The revive has callers of its own besides this tick, so its own
    # stand-aside is asked directly - behind the tick's it could not fail.
    _bp104 = len(_to104(_pa104, "planner"))
    _rv104 = daemon.revive_lost_turn(_pa104, "planner")
    check("(d) the revive itself stands aside too, for its other callers",
          (str(_rv104).startswith("stood aside"),
           len(_to104(_pa104, "planner")) - _bp104), (True, 0))

    print("   (e) Q's planner hands its executor a task")
    _bq104 = len(_to104(_qa104, "executor"))
    _rt104 = post("/task", {"project": _qa104, "instructions":
                            "task104: the next map, under the limit"},
                  secret=True) or {}
    # A negative about a delivery made on a thread: wait for the DECISION,
    # whichever it is - kept, or delivered - before saying which it was.
    until(lambda: _kept104(_qa104, "tasks")
          or len(_to104(_qa104, "executor")) > _bq104, 8)
    check("(e) the task is kept for Q's executor",
          [t.get("text") for t in _kept104(_qa104, "tasks")],
          ["task104: the next map, under the limit"])
    check("(e) ...and NOT handed into a spent account's window",
          len(_to104(_qa104, "executor")) - _bq104, 0)
    print("   the planner's answer: %s" % _rt104.get("why"))
    check("(e) the planner is told it is held, and why",
          (_rt104.get("delivered"), "account" in str(_rt104.get("why"))),
          (False, True))

    print("   (f) the bridge's own wake-ups, into a spent account's windows")
    _kn104 = len(_kept104(_qa104, "tasks"))
    _f1 = daemon.deliver_ex(_qa104, "executor", "you have been idle - a "
                            "state report", {"kind": "task"})
    check("(f) a wake-up the bridge itself sends is refused",
          tuple(_f1), (False, "limit"))
    check("(f) ...and not kept: nobody asked for it to arrive later",
          len(_kept104(_qa104, "tasks")) - _kn104, 0)
    _f2 = daemon.deliver_ex(_pa104, "planner", "a nudge for the next task",
                            {"kind": "info"})
    check("(f) nothing at all goes into the planner's window either",
          tuple(_f2), (False, "limit"))
    # The delivery's own hold, asked directly: the /task answer holds the
    # task first, and behind it this one could not fail. It is the second
    # level for a limit that starts while a task is on its way.
    _kn104 = len(_kept104(_qa104, "tasks"))
    _f3 = daemon.deliver_ex(_qa104, "executor", "Task from the planner:"
                            "\n\ntask104b: kept at the delivery itself",
                            dict(daemon.PLANNER_TASK_META))
    check("(f) a planner's task that reaches the delivery itself is kept "
          "there too", (tuple(_f3), len(_kept104(_qa104, "tasks")) - _kn104),
          ((True, "held-for-limit"), 1))
    # The nudge for a task, run as the timer runs it: it goes through
    # deliver(), which answers only yes or no - so without its own look at
    # the limit, a refusal would be counted as a delivery that FAILED.
    _nt104 = dict((daemon.STATE.get("nudge_tally") or {}).get(_kp104) or {})
    _bpn104 = len(_to104(_pa104, "planner"))
    daemon.nudge_for_task(_pa104, 104, time.time())
    _nt104b = dict((daemon.STATE.get("nudge_tally") or {}).get(_kp104) or {})
    check("(f) the nudge for a task is not sent into a spent planner's "
          "window, and is counted as held, not failed",
          (int(_nt104b.get("held") or 0) - int(_nt104.get("held") or 0),
           int(_nt104b.get("failed") or 0) - int(_nt104.get("failed") or 0),
           len(_to104(_pa104, "planner")) - _bpn104), (1, 0, 0))

    print("   (i) a five-hour reading of 20% while the WEEKLY limit holds")
    _wk104 = _pause104()[1] == "weekly"
    _status104(_qa104, "executor", _exq104, five=20.0)
    _ask104(_wk104, "there is no account pause to protect",
            "(i) it does not lift the account's pause - that lift is the "
            "five-hour pause's own", _pause104()[1], "weekly")

    print("   (g) the first finished turn of ANY window ends it - here Q's")
    print("   planner, a window that held nothing")
    _had104 = bool(_acct104())
    _pk104 = _pause104()
    _kr104 = [str(r.get("n")) for r in _kept104(_pa104, "reports")]
    _kt104 = [t.get("text") for t in _kept104(_qa104, "tasks")]
    _bpp104 = len(_to104(_pa104, "planner"))
    _bqe104 = len(_to104(_qa104, "executor"))
    _pstop104(_qa104, _plq104, "back after the reset")
    _end104 = _had104 and until(lambda: not _acct104(), 5)
    _ask104(_had104, "there was no record to end",
            "(g) a finished turn of any window ends the account's limit",
            _end104, True)
    _ask104(_end104 and _pk104[0] == "paused" and bool(_pk104[1]),
            "no end, or no pause of the limit's standing before it",
            "(g) and lifts the pause it put on the bridge",
            _pause104(), ("running", None))
    # What goes over is asked only once the end has happened and only
    # about what was kept - so a broken end, or a broken keep, reddens its
    # own line and not these.
    _ask104(_end104 and bool(_kr104), "no end, or no report kept",
            "(g) P's kept report goes to P's planner, once",
            until(lambda: _reports104(_to104(_pa104, "planner")[_bpp104:]),
                  5) and _reports104(_to104(_pa104, "planner")[_bpp104:]),
            _kr104)
    _ask104(_end104 and bool(_kt104), "no end, or no task kept",
            "(g) Q's kept tasks go to Q's executor, each once",
            until(lambda: len(_to104(_qa104, "executor")) > _bqe104, 5)
            and [sum(t in body_of(d.get("content") or "")
                     for d in _to104(_qa104, "executor")[_bqe104:])
                 for t in _kt104], [1] * len(_kt104))
    _ask104(_end104, "no end to write down",
            "(g) one line says it is over, what ended it and what went over",
            until(lambda: _lines104("account limit is over"), 5)
            and len(_lines104("account limit is over")), 1)

    print("   the line: %s" % (_lines104("account limit is over") or [""])[-1][:400])
    print("   (h) the 07.09 form, with a date: the reset time passing ends it.")
    print("   The bridge is in 'recovered' mode, as it is after every restart")
    print("   through the gate with a loop on - running, with an unacknowledged")
    print("   notice - and the limit must hold it all the same")
    _clean104()
    tg_reset()
    with daemon._lock:
        daemon.STATE["mode"] = "recovered"
        daemon.save_state()
    _death104(_pa104, "executor", _exp104, _DATED104)
    _ask104(bool(_acct104()) and _pb104,
            "no record, or the pause never engaged even from 'running'",
            "(h) a bridge in 'recovered' mode is paused by the limit too",
            _pause104(), ("paused", "weekly"))
    _rec104 = _acct104()
    check("(h) the dated form opens the record too", bool(_rec104), True)
    _ask104(bool(_rec104), "there is no record to read it from",
            "(h) its reset is that date, at 03:00 UTC+3",
            abs(float(_rec104.get("until") or 0) - _UNTIL104) < 2, True)
    print("   the reset time is moved into the past BY HAND: no measured form")
    print("   carries minutes, so a reset two seconds away cannot be written")
    print("   as a sentence. The shift stands for a different sentence - the")
    print("   parser's output - not for the moment of a death, which other")
    print("   witnesses read")
    if _rec104:
        with daemon._lock:
            daemon.STATE["account_limit"]["until"] = time.time() - 1
            daemon.save_state()
    _bh104 = len(_to104(_pa104, "executor"))
    daemon.check_lost_turn(_pa104)
    _endh104 = bool(_rec104) and not _acct104()
    _ask104(bool(_rec104), "there was no record to end",
            "(h) the next tick ends it", _endh104, True)
    _ask104(_endh104, "the tick did not end it",
            "(h) and lifts the bridge's pause",
            (_pause104()[0] != "paused", _pause104()[1]), (True, None))
    _ask104(_endh104 and _pause104()[0] != "paused", "the pause stayed on",
            "(h) ...giving back the mode it found", _pause104()[0],
            "recovered")
    _ask104(_endh104, "the tick did not end it",
            "(h) the executor that died in it is handed its turn back, once",
            len(_to104(_pa104, "executor")) - _bh104, 1)
    _pass104("%s|executor" % _kp104)
    daemon.check_lost_turn(_pa104)
    _ask104(_endh104, "the tick did not end it",
            "(h) and a later tick is not a second answer to the same death",
            len(_to104(_pa104, "executor")) - _bh104, 1)

    print("   (m) the 07.09 monthly spend form: its own kind, and the weekly")
    print("   reset it names")
    _clean104()
    _MONTHLY104 = ("You've hit your monthly spend limit %s raise it at "
                   "claude.ai/settings/usage?from=cc_cli_limit_message %s "
                   "your weekly limit resets %s %d, 3am (Etc/GMT-3)"
                   % (chr(183), chr(183), _MON104[_ahead104.month - 1],
                      _ahead104.day))
    _death104(_qa104, "planner", _plq104, _MONTHLY104)
    _rec104 = _acct104()
    check("(m) the monthly spend form is its own kind",
          _rec104.get("kind"), "monthly spend")
    _ask104(bool(_rec104), "there is no record to read it from",
            "(m) ...with the weekly reset the sentence names",
            abs(float(_rec104.get("until") or 0) - _UNTIL104) < 2, True)

    print("   (n) a planner's task for an executor whose MODEL is spent is")
    print("   kept as well, and goes over once when that limit ends")
    _clean104()
    _death104(_qa104, "executor", _exq104,
              "You've reached your Opus limit. Run /usage-credits to "
              "continue or switch models with /model.")
    _bn104 = len(_to104(_qa104, "executor"))
    post("/task", {"project": _qa104, "instructions":
                   "task104n: for an executor whose model is spent"},
         secret=True)
    # Kept in the one container every hold keeps in since 8.29,
    # STATE["held"][path] - not in the model's own record any more.
    def _heldt104():
        return ((daemon.STATE.get("held") or {}).get(canon(_qa104))
                or {}).get("tasks") or []

    until(lambda: _heldt104()
          or len(_to104(_qa104, "executor")) > _bn104, 8)
    check("(n) the task waits in the hold, not in the window",
          ([t.get("text") for t in _heldt104()],
           len(_to104(_qa104, "executor")) - _bn104),
          (["task104n: for an executor whose model is spent"], 0))
    _heldn104 = bool(_heldt104())
    _status104(_qa104, "executor", _exq104,
               model=("Sonnet 5", "claude-sonnet-5"))
    _ask104(_heldn104, "no task was kept to go over",
            "(n) a status line on another model ends it, and the task goes "
            "over once",
            until(lambda: sum("task104n" in body_of(d.get("content") or "")
                              for d in _to104(_qa104, "executor")[_bn104:])
                  >= 1, 5)
            and sum("task104n" in body_of(d.get("content") or "")
                    for d in _to104(_qa104, "executor")[_bn104:]), 1)
    _status104(_qa104, "executor", _exq104)

    print("   (j) CONTROL: a MODEL's sentence holds its own pair, not the")
    print("   bridge - piece 11 is untouched")
    _clean104()
    _death104(_pa104, "planner", _plp104, _MODEL104)
    check("(j) a model's sentence holds P by the model limit, and nothing "
          "else",
          (((daemon.STATE.get("paused") or {}).get(_kp104) or {}).get("by"),
           bool((daemon.STATE.get("paused") or {}).get(_kq104)),
           _pause104(), bool(_acct104())),
          ("model_limit", False, ("running", None), False))

    print("   (k) ADDITION B: a rate limit with no sentence is said ONCE per")
    print("   episode, and a finished turn ends the episode")
    _clean104()
    tg_reset()
    _death104(_qa104, "planner", _plq104, None)
    check("(k) the first death of the episode says so",
          until(lambda: len(_chat104(_RATE104)) >= 1, 5)
          and len(_chat104(_RATE104)), 1)
    _first104 = len(_chat104(_RATE104)) == 1
    _death104(_qa104, "planner", _plq104, None)
    _again104 = until(lambda: len(_chat104(_RATE104)) > 1, 3)
    _ask104(_first104, "the first message never went",
            "(k) the second death of the episode does not say it again",
            (_again104, len(_chat104(_RATE104))), (False, 1))
    _ask104(_first104, "the first message never went",
            "(k) it goes into the pair's journal instead",
            len(_lines104("still rate-limited", _qa104)) >= 1, True)
    _pstop104(_qa104, _plq104, "a turn that finished")
    _death104(_qa104, "planner", _plq104, None)
    _ask104(_first104 and not _again104,
            "the episode was never kept to one message",
            "(k) after a finished turn it is a new episode, and is told",
            until(lambda: len(_chat104(_RATE104)) >= 2, 5)
            and len(_chat104(_RATE104)), 2)

    print("   (l) ADDITION A: once a person has been told, the next death")
    print("   opens no new round of hand-backs until a turn finishes")
    _clean104()
    tg_reset()
    _death104(_qa104, "executor", _exq104, None)
    # the hand-backs the bridge allows itself, then the tick that tells
    for _i in range(daemon.LOST_TURN_TRIES + 1):
        _pass104(_EQ104)
        daemon.check_lost_turn(_qa104)
    _told104 = bool(((daemon.STATE.get("stopfail") or {}).get(_EQ104) or {})
                    .get("told"))
    check("(l) three hand-backs, then a person is told - as before",
          (_told104, until(lambda: len(_chat104(_CRASH104)) >= 1, 5)
           and len(_chat104(_CRASH104))), (True, 1))
    _att104 = len(_lines104("(attempt 1 of ", _qa104))
    _death104(_qa104, "executor", _exq104, None)
    _pass104(_EQ104)
    daemon.check_lost_turn(_qa104)
    _ask104(_told104, "no person was told, so there is nothing to carry",
            "(l) the next death, with no turn finished, opens no new round",
            len(_lines104("(attempt 1 of ", _qa104)) - _att104, 0)
    _ask104(_told104, "no person was told, so there is nothing to carry",
            "(l) ...and is still the one fault the person was told about",
            bool(((daemon.STATE.get("stopfail") or {}).get(_EQ104) or {})
                 .get("told")), True)
    _hook104(_qa104, _exq104, "report: a turn that finished", "l")
    until(lambda: _pending104(_qa104) not in ("None", ""), 20)
    _answer104(_qa104, "accepted")
    until(lambda: "l" in _HOOK104, 20)
    _att104 = len(_lines104("(attempt 1 of ", _qa104))
    _death104(_qa104, "executor", _exq104, None)
    _pass104(_EQ104)
    daemon.check_lost_turn(_qa104)
    _ask104(_told104, "no person was told, so there is nothing to end",
            "(l) after a finished turn, a death is new again: hand-backs "
            "start over", len(_lines104("(attempt 1 of ", _qa104)) - _att104,
            1)
    note("checks not asked in this run", len(_SKIP104),
         "0 when every mechanism works; each one names its reason above")
finally:
    daemon.CFG["thresholds"] = _thr104
    _clean104()
    for _proj in (_pa104, _qa104):
        post("/loop", {"project": _proj, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n105. a report made while its pair is HELD is kept and handed over")
print("     once the hold lifts; and a late wait wakes nobody")
print("    2026-09-25, this bridge: report 380 went unanswered, the pair was")
print("    held for silence at 06:16:38, and four reports after it - the last")
print("    a whole piece's report - each got one line, 'Paused - report")
print("    held', and nothing else: the branch held nothing. The planner's")
print("    verdict lifted the hold at 10:20:00 and nothing was handed over.")
print("    In the same second, and at :06 and :10, three late 'wait' verdicts")
print("    with no words went into the executor's window as messages - three")
print("    wakes for nothing. Real order: three reports time out, the pair is")
print("    held, the executor's Stop, a verdict, the real tick.")
print("    -> DECISIONS.md 8.29")
import json as _json105                                   # noqa: E402
_pr105 = os.path.join(TMP, "held-r")
os.makedirs(_pr105, exist_ok=True)
_kr105 = canon(_pr105)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pr105: {
    "chains": {"executor": ["opus"], "planner": ["fable"]}}}})
_ex105, _pl105 = "held105-ex", "held105-pl"
post_rc("/loop", {"action": "start", "project": _pr105})
for _r, _sid in (("executor", _ex105), ("planner", _pl105)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _pr105,
                       "cwd": _pr105})
    register(_pr105, _r, _sid)
_thr105 = dict(daemon.CFG.get("thresholds") or {})
# Short reviews, so three reports can go unanswered in seconds; the silence
# limit is the live one, three; the idle damper off, since these reports are
# short on purpose and would be held as empty exchanges.
daemon.CFG["thresholds"].update({"review_timeout": 2,
                                 "channel_silence_warn": 100,
                                 "silence_limit": 3, "idle_hold": 0})
_SKIP105 = []


def _ask105(ready, why, name, got, want):
    if ready:
        check(name, got, want)
    else:
        _SKIP105.append(name)
        print("  ..   not asked: %s - %s" % (name, why))


def _to105(role):
    return list(DELIVERED.get((_kr105, role)) or [])


def _nums105(items):
    return sorted(sum((re.findall(r"Executor report (\d+):",
                                  body_of((d or {}).get("content") or ""))
                       for d in items), []))


def _kept105():
    return [str(r.get("n")) for r in
            ((daemon.STATE.get("held") or {}).get(_kr105) or {})
            .get("reports") or []]


def _iter105():
    return int(((daemon.STATE.get("loops") or {}).get(_kr105) or {})
               .get("iteration") or 0)


def _lines105(sub):
    f = os.path.join(_pr105, "bridge-logs", time.strftime("%Y-%m-%d"),
                     "events.jsonl")
    out = []
    if os.path.isfile(f):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    t = _json105.loads(line).get("text") or ""
                except Exception:
                    continue
                if sub in t:
                    out.append(t)
    return out


def _late105(verdict, feedback):
    return post("/verdict", {"project": _pr105, "verdict": verdict,
                             "feedback": feedback}, secret=True) or {}


try:
    print("   (a) three reports go unanswered - the pair is held for silence")
    for _i in range(3):
        stop_hook(_pr105, "executor", _ex105, "report %d: no answer" % _i)
    _held105 = ((daemon.STATE.get("paused") or {}).get(_kr105) or {})
    check("(a) three reports with no answer hold the pair, for silence",
          _held105.get("by"), "silence")

    print("   (b) the executor finishes a turn while the pair is held")
    _it105 = _iter105()
    _bp105 = len(_to105("planner"))
    _t0 = time.time()
    stop_hook(_pr105, "executor", _ex105, "report four: THE WORK OF THE TURN")
    _n105 = str(_it105 + 1)
    check("(b) its Stop hook is not held on a pair nobody reviews",
          time.time() - _t0 < 10, True)
    check("(b) the turn is made into a report and KEPT, not dropped",
          _kept105(), [_n105])
    check("(b) ...and not handed to the planner while the pair is held",
          len(_to105("planner")) - _bp105, 0)
    _ask105(bool(_kept105()), "nothing was kept",
            "(b) a line says it was made and kept, and why",
            [("Report %s made and kept" % _n105) in t and "held" in t
             for t in _lines105("made and kept")][-1:], [True])

    print("   (c) the planner answers - a late wait, no words - and the hold")
    print("   is off")
    _be105 = len(_to105("executor"))
    _r105 = _late105("wait", "")
    check("(c) a verdict lifts the silence hold",
          bool((daemon.STATE.get("paused") or {}).get(_kr105)), False)

    print("   (d) the real tick hands the kept report over, once")
    _bp105 = len(_to105("planner"))
    daemon.assess(_pr105)
    _ask105(bool(_kept105()) or bool(_nums105(_to105("planner")[_bp105:])),
            "nothing was kept to hand over",
            "(d) the kept report reaches the planner, once, with its number",
            until(lambda: _nums105(_to105("planner")[_bp105:]), 5)
            and _nums105(_to105("planner")[_bp105:]), [_n105])
    _ask105(bool(_nums105(_to105("planner")[_bp105:])),
            "nothing was handed over",
            "(d) and it is no longer kept", _kept105(), [])
    daemon.assess(_pr105)
    _ask105(bool(_nums105(_to105("planner")[_bp105:])),
            "nothing was handed over",
            "(d) a second tick does not hand it over again",
            _nums105(_to105("planner")[_bp105:]), [_n105])

    print("   (e) 15.8: that late wait, with no words, woke nobody")
    # The delivery is made on a thread when it is made at all; the reply
    # says which, and the recording channel is asked after a real wait.
    time.sleep(2.0)
    check("(e) a late wait with nothing to act on is not delivered",
          (_r105.get("delivered"), len(_to105("executor")) - _be105),
          (False, 0))

    print("   (f) CONTROL: a late wait WITH words while something runs goes")
    print("   in, as it would ride the hook")
    post("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                    "session_id": _ex105, "project_dir": _pr105,
                    "cwd": _pr105, "tool_name": "Bash",
                    "tool_use_id": "toolu_held105bg",
                    "tool_input": {"command": "sleep 600",
                                   "run_in_background": True}})
    _run105 = bool(daemon.inflight_live(_pr105))
    _be105 = len(_to105("executor"))
    _words105 = ("While the long run finishes, look at the second map: "
                 "the corridor on the east side ends in a wall where the "
                 "plan has a door, and the room behind it is never reached. "
                 "Find which rule drops the door, fix it at that rule, and "
                 "rerun only the map check - not the whole suite - when the "
                 "long run is done.")
    _r2_105 = _late105("wait", _words105)
    _ask105(_run105, "nothing is running, so the control cannot be set up",
            "(f) a late wait with words while something runs is delivered",
            until(lambda: len(_to105("executor")) > _be105, 8)
            and len(_to105("executor")) - _be105, 1)

    print("   (g) CONTROL: a late continue is delivered as before")
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(_kr105, None)
        daemon.save_state()
    with open(os.path.join(_pr105, "seen105.txt"), "w",
              encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    _be105 = len(_to105("executor"))
    _late105("continue", "Checked: seen105.txt\nGo on with the east door.")
    check("(g) a late continue is delivered",
          until(lambda: len(_to105("executor")) > _be105, 8)
          and len(_to105("executor")) - _be105, 1)
    note("checks not asked in this run", len(_SKIP105),
         "0 when every mechanism works; each one names its reason above")
finally:
    daemon.CFG["thresholds"] = _thr105
    with daemon._lock:
        (daemon.STATE.get("held") or {}).pop(_kr105, None)
        (daemon.STATE.get("inflight") or {}).pop(_kr105, None)
        (daemon.STATE.get("paused") or {}).pop(_kr105, None)
        daemon.save_state()
    post("/loop", {"project": _pr105, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n106. the silence alarm does not ring while the planner waits on its")
print("     own check - the run the bridge makes for it")
print("    2026-09-25 03:28:24: the planner ended a turn with its check open;")
print("    at 03:30:01 the alarm sent report 373 out the fallback way and told")
print("    the owner 'its turn IS open and the model has not answered'; the")
print("    check passed at 03:41:36. 73 of the bridge's 76 alarms in September")
print("    rang while such a check ran. Real order: a report delivered, a")
print("    real /check whose heavy parts are stubbed - the seat is taken and")
print("    freed by the bridge's own code - the alarm's time passing.")
print("    -> DECISIONS.md 8.29")
_pc106 = os.path.join(TMP, "check-wait")
os.makedirs(_pc106, exist_ok=True)
_kc106 = canon(_pc106)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pc106: {
    "checks": ["suites"]}}})
_ex106, _pl106 = "check106-ex", "check106-pl"
post_rc("/loop", {"action": "start", "project": _pc106})
for _r, _sid in (("executor", _ex106), ("planner", _pl106)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _pc106,
                       "cwd": _pc106})
    register(_pc106, _r, _sid)
daemon.CFG["telegram"] = {"token": "test-token", "chat_id": "42",
                          "pinned_message_id": 0}
_thr106 = dict(daemon.CFG.get("thresholds") or {})
daemon.CFG["thresholds"].update({"review_timeout": 60,
                                 "channel_silence_warn": 2, "idle_hold": 0,
                                 "notify_repeat_sec": 0})
_stubs106 = (daemon._check_copy, daemon._run_one, daemon._check_package)
_RUN106 = 4.0


def _run_one106(cmd, cwd, env, out_path):
    time.sleep(_RUN106)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("stubbed" + chr(10))
    return 0, "ok"


daemon._check_copy = lambda dst: os.makedirs(dst, exist_ok=True)
daemon._run_one = _run_one106
daemon._check_package = lambda work, env, artefacts: (0, "ok")
_SKIP106 = []


def _ask106(ready, why, name, got, want):
    if ready:
        check(name, got, want)
    else:
        _SKIP106.append(name)
        print("  ..   not asked: %s - %s" % (name, why))


def _alarm106():
    return [t for t in tg_texts()
            if "reached the planner's channel process" in t
            and "check-wait" in t.lower()]


def _running106():
    return bool(daemon.CHECK_RUNNING.get(_kc106))


def _verdict106(text):
    with open(os.path.join(_pc106, "seen106.txt"), "w",
              encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    post("/verdict", {"project": _pc106, "verdict": "wait",
                      "feedback": text}, secret=True)


try:
    tg_reset()
    print("   (a) the report is out; the planner runs its check at once")
    _h106 = threading.Thread(target=stop_hook, args=(
        _pc106, "executor", _ex106, "report one: the walls"), daemon=True)
    _h106.start()
    until(lambda: bool(daemon.PENDING.get(_kc106)), 10)
    _c106 = threading.Thread(target=post, args=(
        "/check", {"project": _pc106, "suite": "handover"}),
        kwargs={"secret": True}, daemon=True)
    _c106.start()
    check("(a) the check is running - the bridge's own seat is taken",
          until(_running106, 5), True)
    # past the alarm's two seconds, while the check still runs
    time.sleep(3.5)
    _still106 = _running106()
    _ask106(_still106, "the check ended too early to ask this",
            "(a) nobody is called while the check runs",
            len(_alarm106()), 0)
    _ask106(_still106, "the check ended too early to ask this",
            "(a) the journal says why the alarm is quiet",
            any("waiting on its own check" in (r.get("text") or "")
                for r in store.recent_events(200, project=_kc106)), True)

    print("   (b) CONTROL: the check is over and the planner does not answer")
    print("   - the alarm speaks, once, as it always did")
    until(lambda: not _running106(), 30)
    check("(b) the alarm rings after the check, once",
          until(lambda: len(_alarm106()) >= 1, 10) and len(_alarm106()), 1)
    _verdict106("")
    _h106.join(15)

    print("   (c) CONTROL: no check at all - the alarm rings at its time")
    tg_reset()
    _h106 = threading.Thread(target=stop_hook, args=(
        _pc106, "executor", _ex106, "report two: the doors"), daemon=True)
    _h106.start()
    check("(c) with no check running the alarm rings as before",
          until(lambda: len(_alarm106()) >= 1, 10) and len(_alarm106()), 1)
    _verdict106("")
    _h106.join(15)
    note("checks not asked in this run", len(_SKIP106),
         "0 when every mechanism works; each one names its reason above")
finally:
    daemon._check_copy, daemon._run_one, daemon._check_package = _stubs106
    daemon.CFG["thresholds"] = _thr106
    post("/loop", {"project": _pc106, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n107. 'you have been idle and nothing is waiting on you' is not sent")
print("     to an executor that was just handed a task")
print("    2026-09-25: a Stop hook held on a review from 03:26:01 to 03:41:43,")
print("    a task at 03:41:51, and at 03:42:09 the idle nudge - silence was")
print("    counted from the hook's POST. 11 of September's 276 idle nudges")
print("    came 0-27 s after a task. And 2026-09-23 12:48:09: the task and")
print("    the nudge in the same second - decided before the task, sent after")
print("    it. Real order: status line, a Stop hook held past the quiet")
print("    threshold, a verdict, a real /task, the real tick; the same-second")
print("    form forced deterministically at the one moment between the")
print("    decision and the delivery. -> DECISIONS.md 8.29")
_pi107 = os.path.join(TMP, "idle-task")
os.makedirs(_pi107, exist_ok=True)
_ki107 = canon(_pi107)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pi107: {
    "silence_minutes": 0.05}}})
_ex107, _pl107 = "idle107-ex", "idle107-pl"
post_rc("/loop", {"action": "start", "project": _pi107})
for _r, _sid in (("executor", _ex107), ("planner", _pl107)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _pi107,
                       "cwd": _pi107})
    register(_pi107, _r, _sid)
_thr107 = dict(daemon.CFG.get("thresholds") or {})
daemon.CFG["thresholds"].update({"review_timeout": 60,
                                 "channel_silence_warn": 50, "idle_hold": 0})
_hfc107 = daemon.hold_for_compaction
_STATE107 = "you have been idle and nothing is waiting on you"


def _states107(since):
    return sum(_STATE107 in body_of(d.get("content") or "")
               for d in (DELIVERED.get((_ki107, "executor")) or [])[since:])


def _tasks107(since, tag):
    return sum(tag in body_of(d.get("content") or "")
               for d in (DELIVERED.get((_ki107, "executor")) or [])[since:])


def _deliv107():
    return len(DELIVERED.get((_ki107, "executor")) or [])


def _clear_acted107():
    with daemon._lock:
        for _k in list(daemon.STATE):
            if _k.startswith("acted:") and _ki107 in _k.lower():
                daemon.STATE.pop(_k, None)
        daemon.save_state()


def _task107(text):
    _before = float((daemon.STATE.get("last_task") or {}).get(_ki107) or 0)
    post("/task", {"project": _pi107, "instructions": text}, secret=True)
    until(lambda: float((daemon.STATE.get("last_task") or {})
                        .get(_ki107) or 0) > _before, 8)


def _status107(role, sid):
    exe = role == "executor"
    post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": _pi107, "project_dir": _pi107},
        "model": {"display_name": "Opus 5 (1M context)" if exe
                  else "Fable 5.1",
                  "id": "claude-opus-5" if exe else "claude-fable-5-1"},
        "context_window": {
            "context_window_size": 1000000, "used_percentage": 20.0,
            "current_usage": {"input_tokens": 10,
                              "cache_creation_input_tokens": 90,
                              "cache_read_input_tokens": 199900,
                              "output_tokens": 100}}}})


try:
    _status107("executor", _ex107)
    _status107("planner", _pl107)
    print("   (a) the 03:42 form: a hook held past the quiet threshold, then")
    print("   a verdict, then a task, then the tick")
    _h107 = threading.Thread(target=stop_hook, args=(
        _pi107, "executor", _ex107, "report one: the stairs"), daemon=True)
    _h107.start()
    until(lambda: bool(daemon.PENDING.get(_ki107)), 10)
    time.sleep(4.0)                 # the quiet threshold is 3 s
    with open(os.path.join(_pi107, "seen107.txt"), "w",
              encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    # `done`, as on the day: a continue would set `awaiting` and stand
    # every tier down until the executor's next Stop.
    post("/verdict", {"project": _pi107, "verdict": "done",
                      "feedback": "Checked: seen107.txt\nAccepted."},
         secret=True)
    _h107.join(15)
    _b107 = _deliv107()
    _task107("task107a: the roof tiles")
    _clear_acted107()
    daemon.assess(_pi107)
    check("(a) the task went in",
          _tasks107(_b107, "task107a"), 1)
    check("(a) no idle nudge follows a task handed over seconds ago",
          _states107(_b107), 0)

    print("   (b) the same-second form: the task goes out between the")
    print("   decision and the delivery")
    time.sleep(4.0)                 # the task of (a) is past the threshold

    def _hfc_task107(path, role, who):
        if who == "the idle nudge" and canon(path) == _ki107:
            _task107("task107b: the gutters")
        return _hfc107(path, role, who)

    daemon.hold_for_compaction = _hfc_task107
    _b107 = _deliv107()
    _clear_acted107()
    daemon.assess(_pi107)
    daemon.hold_for_compaction = _hfc107
    _went107 = _tasks107(_b107, "task107b") == 1
    check("(b) the task went in at the moment between", _went107, True)
    check("(b) and the nudge decided before it is not sent after it",
          _states107(_b107), 0)
    _ask107 = _went107 and _states107(_b107) == 0
    if _ask107:
        check("(b) ...and leaves no latch that would silence the next real "
              "nudge",
              [k for k in daemon.STATE if str(k).startswith("acted:nudge:")
               and _ki107 in str(k).lower()], [])
    else:
        print("  ..   not asked: (b) ...and leaves no latch - the nudge was "
              "not stood down here")

    print("   (c) CONTROL: no task at all - the idle nudge goes as before")
    time.sleep(4.0)
    _b107 = _deliv107()
    _clear_acted107()
    daemon.assess(_pi107)
    check("(c) with no task the idle nudge still goes",
          until(lambda: _states107(_b107) >= 1, 5) and _states107(_b107), 1)
finally:
    daemon.hold_for_compaction = _hfc107
    daemon.CFG["thresholds"] = _thr107
    post("/loop", {"project": _pi107, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n108. every window the bridge opens says why; and a start pressed")
print("     while that role's window is up is asked, not done")
print("    2026-09-23, a watched project: the bridge opened a planner at")
print("    10:30:23 (no planner channel for report 3666), and a start pressed")
print("    in the panel 61 s later opened a second one; nothing said so. Four")
print("    of the six ways a window is opened wrote no reason into")
print("    window_log, so 103 warnings about the seat read 'why=''' for the")
print("    next hour. Real order: POST /session, the stub window alive,")
print("    POST /session again. -> DECISIONS.md 8.29")
import ast as _ast108                                     # noqa: E402
_pw108 = os.path.join(TMP, "why-ask")
os.makedirs(_pw108, exist_ok=True)
_kw108 = canon(_pw108)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pw108: {}}})


def _launch108(role, confirm=False):
    return post("/session", {"action": "launch", "project": _pw108,
                             "role": role, "confirm": confirm}) or {}


def _log108():
    return [r for r in (daemon.STATE.get("window_log") or [])
            if r.get("path") == _kw108]


try:
    print("   (a) a start from the panel writes its reason")
    _n108 = len(launches())
    _r108 = _launch108("planner")
    check("(a) the window started", bool(_r108.get("ok")), True)
    check("(a) window_log says why it was opened",
          (_log108()[-1:] or [{}])[0].get("why"),
          "you pressed start in the panel")
    _src108 = inspect.getsource(daemon)
    _bare108 = [n.lineno for n in _ast108.walk(_ast108.parse(_src108))
                if isinstance(n, _ast108.Call)
                and getattr(n.func, "id", "") == "reg_pid"
                and not any(k.arg == "why" for k in n.keywords)]
    check("(a) every reg_pid call in the daemon passes why", _bare108, [])

    print("   (b) a second start of the same role, while its window is up")
    until(lambda: len(launches()) > _n108, 10)
    _n108 = len(launches())
    _l108 = len(_log108())
    _r108 = _launch108("planner")
    check("(b) it is asked, not done",
          (_r108.get("ok"), _r108.get("ask")), (False, True))
    _up108 = str(_r108.get("error") or "")
    _ask108 = bool(_r108.get("ask"))
    if _ask108:
        check("(b) the question names the window, what opened it and when",
              ["pid %s" % ((daemon.STATE.get("pids") or {}).get(
                  "%s|planner" % _kw108) or {}).get("pid") in _up108,
               "you pressed start in the panel" in _up108,
               "opened at " in _up108], [True, True, True])
    else:
        print("  ..   not asked: (b) the question names the window - there "
              "was no question")
    time.sleep(1.0)
    check("(b) and nothing was started", (len(launches()) - _n108,
                                          len(_log108()) - _l108), (0, 0))

    print("   (c) the same request with confirm starts it")
    _r108 = _launch108("planner", confirm=True)
    check("(c) with confirm it starts", bool(_r108.get("ok")), True)
    until(lambda: len(launches()) > _n108, 10)

    print("   (d) start both, with the planner up: asked, both halves, before")
    print("   either starts")
    _n108 = len(launches())
    _l108 = len(_log108())
    _r108 = post("/session", {"action": "launch_both", "project": _pw108,
                              "chains": {"executor": ["opus"],
                                         "planner": ["fable"]}}) or {}
    time.sleep(1.0)
    check("(d) start both is asked, and nothing starts",
          (_r108.get("ask"), _r108.get("started"),
           len(launches()) - _n108, len(_log108()) - _l108),
          (True, [], 0, 0))
finally:
    for _r in ("planner", "executor"):
        post("/session", {"action": "stop", "project": _pw108, "role": _r})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n109. a Stop hook the client cancelled lets its review go: no silence")
print("     is counted, and the verdict still reaches the executor")
print("    2026-09-23, a watched project's executor transcript: twice the")
print("    client cancelled its own Stop hook ('hook_cancelled', after 684 s")
print("    and 137 s) and killed hook.py, while the daemon waited on for a")
print("    verdict nobody could deliver - and a timeout there is counted as the")
print("    planner's silence, three of which hold the pair. Real order: a Stop")
print("    hook POSTed on a raw socket, the report delivered, the socket")
print("    closed as the client closes it, the review's time passing.")
print("    -> DECISIONS.md 8.29")
import socket as _so109                                   # noqa: E402
_ph109 = os.path.join(TMP, "hook-gone")
os.makedirs(_ph109, exist_ok=True)
_kh109 = canon(_ph109)
post("/config", {"projects": {A: {}, B: {}, C: {}, _ph109: {}}})
_ex109, _pl109 = "gone109-ex", "gone109-pl"
post_rc("/loop", {"action": "start", "project": _ph109})
for _r, _sid in (("executor", _ex109), ("planner", _pl109)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _ph109,
                       "cwd": _ph109})
    register(_ph109, _r, _sid)
_thr109 = dict(daemon.CFG.get("thresholds") or {})
_TIMEOUT109 = 12
daemon.CFG["thresholds"].update({"review_timeout": _TIMEOUT109,
                                 "channel_silence_warn": 100,
                                 "silence_limit": 3, "idle_hold": 0})
_tr109 = sessions.transcript_of
_tp109 = os.path.join(TMP, "gone109-ex.jsonl")
sessions.transcript_of = (lambda sid: _tp109 if sid == _ex109
                          else _tr109(sid))
_SKIP109 = []
# A DELIVERY THAT TAKES TIME, forced, because that is what once made this
# case red in a full run: the client stamps its cancel row to the whole
# second, the hook is cancelled while the report is still being delivered,
# and a `since` taken AFTER the delivery called the true row older than the
# review. The boundary is when the review began (8.35); with it measured
# from the delivery this fails every time instead of one run in several.
_dx109 = daemon.deliver_ex


def _slow109(path, role, *a, **k):
    if canon(path) == _kh109 and role == "planner":
        time.sleep(2.5)
    return _dx109(path, role, *a, **k)


daemon.deliver_ex = _slow109


def _unanswered109():
    return int((daemon.STATE.get("unanswered") or {}).get(_kh109) or 0)


def _raw_stop109(text):
    body = json.dumps({"hook_event_name": "Stop", "role": "executor",
                       "session_id": _ex109, "project_dir": _ph109,
                       "cwd": _ph109,
                       "last_assistant_message": text}).encode("utf-8")
    s = _so109.create_connection(("127.0.0.1", PORT), timeout=5)
    s.sendall(b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
              b"Content-Type: application/json\r\n"
              b"Content-Length: %d\r\n\r\n" % len(body) + body)
    return s


def _lines109(sub):
    f = os.path.join(_ph109, "bridge-logs", time.strftime("%Y-%m-%d"),
                     "events.jsonl")
    out = []
    if os.path.isfile(f):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    t = json.loads(line).get("text") or ""
                except Exception:
                    continue
                if sub in t:
                    out.append(t)
    return out


try:
    print("   (a) the report is out, its Stop hook on a raw socket")
    _u109 = _unanswered109()
    _sock109 = _raw_stop109("report one: the lighthouse")
    check("(a) the report waits for its verdict",
          until(lambda: bool(daemon.PENDING.get(_kh109)), 10), True)

    print("   (b) the client cancels the hook - its transcript says so, and")
    print("   the socket closes")
    with open(_tp109, "w", encoding="utf-8") as _f:
        _f.write(json.dumps({
            "type": "attachment",
            "attachment": {"type": "hook_cancelled", "hookName": "Stop",
                           "hookEvent": "Stop", "durationMs": 684486,
                           "timedOut": False, "timeoutMs": 1800000},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime())}) + chr(10))
    _sock109.close()
    check("(b) the review is let go within seconds, not at its timeout",
          until(lambda: not daemon.PENDING.get(_kh109), 8), True)
    _gone109 = not daemon.PENDING.get(_kh109)
    if _gone109:
        check("(b) the line names the client's own word, from its transcript",
              any("the client cancelled the Stop hook after 684 s" in t
                  for t in _lines109("nobody is waiting on this review")),
              True)
    else:
        _SKIP109.append("(b) the line")
        print("  ..   not asked: (b) the line - the review was not let go")

    print("   (c) the review's timeout passes: it was not the planner's")
    print("   silence")
    time.sleep(_TIMEOUT109 + 3)
    check("(c) no silence is counted for a hook nobody waits on",
          _unanswered109() - _u109, 0)

    print("   (d) the planner answers: the verdict finds no waiter and goes to")
    print("   the executor by the channel, once")
    with open(os.path.join(_ph109, "seen109.txt"), "w",
              encoding="utf-8") as _f:
        _f.write("read" + chr(10))
    _be109 = len(DELIVERED.get((_kh109, "executor")) or [])
    post("/verdict", {"project": _ph109, "verdict": "continue",
                      "feedback": "Checked: seen109.txt\nOn to the keeper's "
                                  "house."}, secret=True)
    check("(d) the verdict reaches the executor, once",
          until(lambda: len(DELIVERED.get((_kh109, "executor")) or [])
                > _be109, 8)
          and sum("keeper's house" in body_of(d.get("content") or "")
                  for d in (DELIVERED.get((_kh109, "executor")) or [])
                  [_be109:]), 1)

    print("   (e) CONTROL: a hook that stays is not let go - it comes back")
    print("   when its verdict does, and no line calls it gone")
    _out109 = {}

    def _stay109():
        stop_hook(_ph109, "executor", _ex109, "report two: the harbour")
        _out109["at"] = time.time()

    _n2109 = "Report %d:" % (int(((daemon.STATE.get("loops") or {})
                                  .get(_kh109) or {}).get("iteration") or 0)
                             + 1)
    _t109 = threading.Thread(target=_stay109, daemon=True)
    _t109.start()
    until(lambda: bool(daemon.PENDING.get(_kh109)), 10)
    time.sleep(4.0)       # two of the watcher's looks at a hook still there
    _tv109 = time.time()
    post("/verdict", {"project": _ph109, "verdict": "continue",
                      "feedback": "Checked: seen109.txt\nThe harbour wall "
                                  "next."}, secret=True)
    _t109.join(15)
    check("(e) a hook that is still there waits for its verdict",
          float(_out109.get("at") or 0) >= _tv109, True)
    check("(e) and nothing called it gone",
          [t for t in _lines109("nobody is waiting on this review")
           if t.startswith(_n2109)], [])
    note("checks not asked in this run", len(_SKIP109),
         "0 when every mechanism works; each one names its reason above")
finally:
    sessions.transcript_of = _tr109
    daemon.deliver_ex = _dx109
    daemon.CFG["thresholds"] = _thr109
    post("/loop", {"project": _ph109, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n110. a deaf hold is lifted only by a turn in the window it named")
print("    2026-09-23, a watched project, J:2167: 'planner window 3724 opened")
print("    a turn at 11:49:51 - the deaf hold is lifted'. The turn was in")
print("    window 22824, in its linked session: the witness was the newest planner")
print("    record of all, the line printed the window opened last. Real")
print("    order: two planner windows come up (their hooks say which window")
print("    each is), the channel seat is window A's, the hold, a turn in B,")
print("    the real tick. -> DECISIONS.md 8.29")
_pd110 = os.path.join(TMP, "deaf-which")
os.makedirs(_pd110, exist_ok=True)
_kd110 = canon(_pd110)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pd110: {}}})
_WA110, _WB110 = 41101, 41102
_sA110, _sB110, _sX110 = "a110dwpl-1", "b110dwpl-2", "x110dwex-3"
_tr110 = sessions.transcript_of
_tp110 = {s: os.path.join(TMP, s + ".jsonl") for s in (_sA110, _sB110)}
sessions.transcript_of = (lambda sid, cwd=None: _tp110.get(sid)
                          or _tr110(sid))
_SKIP110 = []


def _turn110(sid, when):
    with open(_tp110[sid], "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime(when)),
            "message": {"content": [{"type": "text", "text": "reading"}]}})
            + chr(10))


def _held110():
    return ((daemon.STATE.get("paused") or {}).get(_kd110) or {}).get("by")


def _said110(sub):
    return [r.get("text") or "" for r in
            daemon.store.recent_events(400, project=_pd110)
            if sub in (r.get("text") or "")]


def _deaf110():
    """The report branch's own call, twice - DEAF_REPORTS_BEFORE_HOLD."""
    with daemon._lock:
        (daemon.STATE.get("deaf") or {}).pop("%s|planner" % _kd110, None)
        daemon.save_state()
    for _i in range(daemon.DEAF_REPORTS_BEFORE_HOLD):
        daemon.note_deaf_planner(_pd110, "deaf-which", 20 + _i, True)


def _seat110(ppid):
    post("/channel/register", {"project": _pd110, "role": "planner",
                               "port": open_channel(_pd110, "planner"),
                               "pid": os.getpid(), "ppid": ppid},
         secret=True)


try:
    for _t in (_tp110.values()):
        open(_t, "w").close()
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": _sX110, "project_dir": _pd110,
                       "cwd": _pd110, "window_pid": 41100})
    for _s, _w in ((_sA110, _WA110), (_sB110, _WB110)):
        post_rc("/event", {"hook_event_name": "SessionStart",
                           "role": "planner", "session_id": _s,
                           "project_dir": _pd110, "cwd": _pd110,
                           "window_pid": _w})
    _seat110(_WA110)
    print("   (a) the complaint is about the window that holds the seat")
    _deaf110()
    _rec110 = (daemon.STATE.get("deaf") or {}).get("%s|planner" % _kd110) \
        or {}
    check("(a) the pair is held, and the record names window A's session",
          (_held110(), _rec110.get("sid"), _rec110.get("pid")),
          ("deaf", _sA110, _WA110))

    print("   (b) a turn opens in window B - the real tick")
    _turn110(_sB110, time.time() + 1)
    daemon.assess(_pd110)
    check("(b) the hold stands: B is not the window that was deaf",
          _held110(), "deaf")
    check("(b) and one line names B's turn and A as the deaf one",
          len([t for t in _said110("the deaf hold stands")
               if str(_WB110) in t and str(_WA110) in t]), 1)
    daemon.assess(_pd110)
    check("(b) said once, not every tick",
          len(_said110("the deaf hold stands")), 1)

    print("   (c) CONTROL: a turn in window A lifts it, and the line names A")
    if _held110() != "deaf":
        _SKIP110.append("(c)")
        print("  ..   not asked: (c) - the hold was already gone")
    else:
        _turn110(_sA110, time.time() + 1)
        daemon.assess(_pd110)
        check("(c) a turn in the window it named lifts the hold",
              until(lambda: _held110() is None, 10), True)
        check("(c) and the line names that window and its session",
              len([t for t in _said110("the deaf hold is lifted")
                   if ("planner window %s (%s)" % (_WA110, _sA110[:8]))
                   in t]), 1)

    print("   (d) the seat's window is one no hook has named: the hold")
    print("   stands, whatever turns open elsewhere")
    _seat110(41109)
    open(_tp110[_sA110], "w").close()
    open(_tp110[_sB110], "w").close()
    _deaf110()
    _rec110 = (daemon.STATE.get("deaf") or {}).get("%s|planner" % _kd110) \
        or {}
    check("(d) the record says it is not linked",
          (_held110(), _rec110.get("linked")), ("deaf", False))
    _turn110(_sA110, time.time() + 1)
    _turn110(_sB110, time.time() + 1)
    daemon.assess(_pd110)
    check("(d) the hold stands, and says why",
          (_held110(), len(_said110("which session is in planner window "
                                    "41109 is not known"))), ("deaf", 1))

    print("   (e) hook.py finds the window PAST the shell a live client runs")
    print("   it through: measured the day this shipped, a live session")
    print("   reported four different parents, none of them its window. A")
    print("   child started through cmd.exe must name THIS process by its")
    print("   executable, and a name nobody has must answer 0, not a guess")
    if os.name == "nt":
        _root110 = os.path.dirname(os.path.dirname(os.path.abspath(
            daemon.__file__)))
        _code110 = ("import sys; sys.path.insert(0, %r); "
                    "from bridgecore import hook; "
                    "print(hook.client_pid(%r), hook.client_pid('no-such-"
                    "client'))" % (_root110, os.path.basename(
                        sys.executable).lower()[:6]))
        _cp110 = subprocess.run(["cmd.exe", "/c", sys.executable, "-c",
                                 _code110], capture_output=True, text=True,
                                timeout=60)
        check("(e) the walk passes the shell and names this process; an "
              "absent client is 0",
              (_cp110.stdout or "").split(), [str(os.getpid()), "0"])
    else:
        print("  ..   not asked: (e) - the walk is Windows-only")
    note("checks not asked in this run", len(_SKIP110),
         "0 when every mechanism works; each one names its reason above")
finally:
    sessions.transcript_of = _tr110
    with daemon._lock:
        (daemon.STATE.get("deaf") or {}).pop("%s|planner" % _kd110, None)
        daemon.save_state()
    daemon.resume_project(_pd110)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n111. the planner's own check stops the review's clock; nobody is told")
print("     the planner is silent while it runs; the hook watcher ends with")
print("     its review; a clinch says 'no report reached the planner' only")
print("     when that is true")
print("    2026-09-25: report 399 at 18:56:19, the planner's check from")
print("    18:56:32 to 19:17:02 - and at 19:16:21 the review timed out")
print("    mid-check: a needs_you 'its turn IS open and the model has not")
print("    answered', silence counted, a line that the hook's connection had")
print("    closed, and at 19:17:07 a clinch saying no report had reached the")
print("    planner - two seconds before its verdict. Real order: the Stop")
print("    hook, the report out, the planner's check taking the real seat,")
print("    the review's timeout passing under it, the verdict after it.")
print("    -> DECISIONS.md 8.33")
_pc111 = os.path.join(TMP, "check-clock")
os.makedirs(_pc111, exist_ok=True)
_kc111 = canon(_pc111)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pc111: {}}})
_ex111, _pl111 = "cc111-ex", "cc111-pl"
post_rc("/loop", {"action": "start", "project": _pc111})
for _r, _sid in (("executor", _ex111), ("planner", _pl111)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _pc111,
                       "cwd": _pc111})
    register(_pc111, _r, _sid)
_thr111 = dict(daemon.CFG.get("thresholds") or {})
_TO111 = 10
daemon.CFG["thresholds"].update({"review_timeout": _TO111,
                                 "channel_silence_warn": 3,
                                 "silence_limit": 3, "idle_hold": 0,
                                 "clinch_grace": 1})
with open(os.path.join(_pc111, "seen111.txt"), "w", encoding="utf-8") as _f:
    _f.write("read" + chr(10))
_SK111 = []


def _unans111():
    return int((daemon.STATE.get("unanswered") or {}).get(_kc111) or 0)


def _lines111(sub, since):
    f = os.path.join(_pc111, "bridge-logs", time.strftime("%Y-%m-%d"),
                     "events.jsonl")
    out = []
    if os.path.isfile(f):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("at") or "") >= since and sub in (r.get("text")
                                                           or ""):
                    out.append(r.get("text") or "")
    return out


def _stamp111():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


try:
    print("   (a) the report goes out; the planner starts its own check")
    _u111 = _unans111()
    _t0 = _stamp111()
    _out111 = {}

    def _turn111():
        _out111["r"] = stop_hook(_pc111, "executor", _ex111,
                                 "report one: the lighthouse")
        _out111["at"] = time.time()

    _th111 = threading.Thread(target=_turn111, daemon=True)
    _th111.start()
    _pend111 = until(lambda: bool(daemon.PENDING.get(_kc111)), 10)
    check("(a) the report waits for its verdict", _pend111, True)
    _seat111 = daemon.check_seat(_pc111)
    check("(a) the check takes the real seat", bool(_seat111[0]), True)

    print("   (b) the review's timeout passes while the check runs")
    time.sleep(_TO111 + 6)
    check("(b) the review is still open - the check stopped its clock",
          bool(daemon.PENDING.get(_kc111)), True)
    # ANY alarm, not two phrasings of one: the fallback branch words the
    # same silence three ways depending on what the transcript shows, and
    # a sabotage that let it fire under a different wording stayed green
    # while this asked only for the 19:16 sentences.
    check("(b) nobody was told the planner is silent, in any words",
          (_lines111("notify needs_you", _t0),
           _lines111("sent it out the fallback way", _t0)), ([], []))
    check("(b) no silence was counted", _unans111() - _u111, 0)
    check("(b) a clinch does not call the pair idle while the check runs",
          daemon.clinch(_pc111, daemon.situation(_pc111), grace=1), None)
    check("(b) and the tick knows the planner is at work",
          daemon.assess(_pc111).get("saw"),
          "the planner is reviewing a report")

    print("   (c) the check ends, the verdict comes: the review ends on it")
    with daemon._check_lock:
        daemon.CHECK_RUNNING.pop(_kc111, None)
    _tv111 = time.time()
    post("/verdict", {"project": _pc111, "verdict": "continue",
                      "feedback": "Checked: seen111.txt\nThe keeper's house "
                                  "next."}, secret=True)
    _th111.join(20)
    check("(c) the hook came back after the verdict, not at a timeout",
          float(_out111.get("at") or 0) >= _tv111, True)
    time.sleep(3.0)   # two of the watcher's looks after the reply went out
    check("(c) and no line says its connection closed with nobody waiting",
          _lines111("nobody is waiting on this review", _t0), [])
    check("(c) still no silence", _unans111() - _u111, 0)

    print("   (d) CONTROL: no check - the review times out as it always did,")
    print("   and the clinch after it says the report DID reach the planner")
    _u111 = _unans111()
    _t1 = _stamp111()
    _out111.clear()
    _th111 = threading.Thread(target=_turn111, daemon=True)
    _th111.start()
    until(lambda: bool(daemon.PENDING.get(_kc111)), 10)
    _th111.join(_TO111 + 20)
    check("(d) without a check the review times out and silence is counted",
          _unans111() - _u111, 1)
    time.sleep(3.0)
    check("(d) and the watcher did not call a timed-out hook gone",
          _lines111("nobody is waiting on this review", _t1), [])
    _found111 = daemon.clinch(_pc111, daemon.situation(_pc111), grace=1) or {}
    check("(d) the clinch names the half that owes the answer, truthfully",
          (_found111.get("why"), _found111.get("wake"),
           "reached the planner at" in (_found111.get("said") or "")),
          ("report_unanswered", "planner", True))

    print("   (e) a check running after the review has ended is the planner")
    print("   at work: the tick and the clinch stand down on it")
    daemon.check_seat(_pc111)
    check("(e) the tick says the planner is running its own check",
          daemon.assess(_pc111).get("saw"),
          "the planner is running its own check")
    check("(e) and the clinch finds nothing to call",
          daemon.clinch(_pc111, daemon.situation(_pc111), grace=1), None)
    with daemon._check_lock:
        daemon.CHECK_RUNNING.pop(_kc111, None)

    print("   (f) a check that starts LATE - after the alarm - stops the")
    print("   clock for the rest of the review too")
    _u111 = _unans111()
    _out111.clear()
    _th111 = threading.Thread(target=_turn111, daemon=True)
    _th111.start()
    until(lambda: bool(daemon.PENDING.get(_kc111)), 10)
    time.sleep(3 + 2)            # past channel_silence_warn: the alarm branch
    daemon.check_seat(_pc111)
    time.sleep(_TO111)           # the review's own time runs out under it
    check("(f) the review is still open under a late check",
          bool(daemon.PENDING.get(_kc111)), True)
    check("(f) and no silence is counted", _unans111() - _u111, 0)
    with daemon._check_lock:
        daemon.CHECK_RUNNING.pop(_kc111, None)
    post("/verdict", {"project": _pc111, "verdict": "continue",
                      "feedback": "Checked: seen111.txt\nOn."}, secret=True)
    _th111.join(20)

    print("   (g) a check past CHECK_HOLD_SEC is not believed: a hung run may")
    print("   not hold the tick for ever")
    with daemon._check_lock:
        # getattr: the red run's module has no such name, and a raise here
        # would take the summary with it instead of failing on a line
        daemon.CHECK_RUNNING[_kc111] = (
            time.time() - getattr(daemon, "CHECK_HOLD_SEC",
                                  daemon.CHECK_TIMEOUT * 2) - 5)
    check("(g) the tick does not stand down on a stale check",
          daemon.assess(_pc111).get("saw")
          != "the planner is running its own check", True)
    print("  ..   checks not asked in this run: %d" % len(_SK111))
finally:
    with daemon._check_lock:
        daemon.CHECK_RUNNING.pop(_kc111, None)
    daemon.CFG["thresholds"] = _thr111
    post("/loop", {"project": _pc111, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n112. the idle nudge is not sent while the planner said wait and the")
print("     executor's own Monitor is open - the client wakes it by itself")
print("    17-25.09: 33 of 112 idle nudges fired while the executor's own")
print("    Monitor was open after a wait - open 902 s at the median - and each")
print("    opened a turn and a report the Monitor's next event would have")
print("    opened anyway. The bridge tracks Bash jobs only; a Monitor has no")
print("    record, so nothing stood the nudge down. Real order: a report and")
print("    the planner's wait, then the executor idle past the quiet with its")
print("    Monitor open, then the real tick. -> DECISIONS.md 8.34")
_pn113 = os.path.join(TMP, "nudge-monitor")
os.makedirs(_pn113, exist_ok=True)
_kn113 = canon(_pn113)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pn113: {
    "silence_minutes": 0.05}}})
_ex113, _pl113 = "mon113-ex", "mon113-pl"
post_rc("/loop", {"action": "start", "project": _pn113})
for _r, _sid in (("executor", _ex113), ("planner", _pl113)):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _pn113,
                       "cwd": _pn113})
    register(_pn113, _r, _sid)
_thr113 = dict(daemon.CFG.get("thresholds") or {})
daemon.CFG["thresholds"].update({"review_timeout": 60,
                                 "channel_silence_warn": 50, "idle_hold": 0})
_tp113 = os.path.join(TMP, "mon113-ex.jsonl")
_tr113 = sessions.transcript_of
sessions.transcript_of = (lambda sid, cwd=None: _tp113 if sid == _ex113
                          else _tr113(sid))
with open(os.path.join(_pn113, "seen113.txt"), "w", encoding="utf-8") as _f:
    _f.write("read" + chr(10))
_NUDGE113 = "you have been idle and nothing is waiting on you"


def _z113(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def _window113(open_for, timeout_ms=3600000, persistent=False, ended=False):
    """The executor's transcript: a Monitor started `open_for` seconds ago
    - its tool_use and the result naming its task - then the turn's end;
    and, when `ended`, the Monitor's own end notice after it."""
    t0 = time.time() - open_for
    rows = [{"type": "assistant", "timestamp": _z113(t0), "message": {
                "content": [{"type": "tool_use", "id": "mon", "name":
                             "Monitor", "input": {"description": "watch"}}]}},
            {"type": "user", "timestamp": _z113(t0),
             "toolUseResult": {"taskId": "mon113", "timeoutMs": timeout_ms,
                               "persistent": persistent},
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": "mon"}]}},
            {"type": "assistant", "timestamp": _z113(t0 + 1), "message": {
                "content": [{"type": "text", "text": "watching"}],
                "stop_reason": "end_turn"}}]
    if ended:
        rows.append({"type": "user", "origin": {"kind": "task-notification"},
                     "timestamp": _z113(time.time() - 5), "message": {
                         "content": "<task-notification>\n<task-id>mon113"
                         "</task-id>\n<status>completed</status>\n<summary>"
                         "Monitor \"watch\" stream ended</summary>\n"
                         "</task-notification>"}})
        rows.append({"type": "assistant", "timestamp": _z113(time.time() - 4),
                     "message": {"content": [{"type": "text",
                                              "text": "it ended"}],
                                 "stop_reason": "end_turn"}})
    with open(_tp113, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + chr(10))


def _status113(role, sid):
    post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": _pn113, "project_dir": _pn113},
        "model": {"display_name": "Opus 5 (1M context)",
                  "id": "claude-opus-5"},
        "context_window": {
            "context_window_size": 1000000, "used_percentage": 20.0,
            "current_usage": {"input_tokens": 10,
                              "cache_creation_input_tokens": 90,
                              "cache_read_input_tokens": 199900,
                              "output_tokens": 100}}}})


def _report113(verdict):
    """A real report, answered with `verdict`."""
    th = threading.Thread(target=stop_hook, args=(
        _pn113, "executor", _ex113, "the run is going"), daemon=True)
    th.start()
    until(lambda: bool(daemon.PENDING.get(_kn113)), 10)
    post("/verdict", {"project": _pn113, "verdict": verdict,
                      "feedback": "Checked: seen113.txt\nWaiting for the "
                                  "run."}, secret=True)
    th.join(15)


def _nudges113(since):
    return sum(_NUDGE113 in body_of(d.get("content") or "")
               for d in (DELIVERED.get((_kn113, "executor")) or [])[since:])


def _tick113():
    """Past the 3 s quiet, the latch cleared, the real tick. Returns the
    nudges it sent and what it saw."""
    with daemon._lock:
        for _k in list(daemon.STATE):
            if _k.startswith("acted:") and _kn113 in _k.lower():
                daemon.STATE.pop(_k, None)
        daemon.save_state()
    _status113("executor", _ex113)
    _status113("planner", _pl113)
    time.sleep(4.0)
    b = len(DELIVERED.get((_kn113, "executor")) or [])
    saw = daemon.assess(_pn113).get("saw") or ""
    until(lambda: _nudges113(b) >= 1, 3)
    return _nudges113(b), saw


try:
    print("   (a) a report, and the planner's wait")
    _window113(10)
    _report113("wait")
    print("   (b) the form of the 33: the Monitor open 15 minutes, the")
    print("   executor idle past the quiet")
    _window113(900)
    _n113, _saw113 = _tick113()
    check("(b) no idle nudge while its own Monitor is open after a wait",
          _n113, 0)
    check("(b) and the tick names the Monitor it is leaving it to",
          "its own Monitor" in _saw113, True)

    print("   (c) CONTROL: the Monitor's end notice has come - the nudge goes")
    _window113(900, ended=True)
    _n113, _ = _tick113()
    check("(c) a Monitor that ended stands nothing down", _n113, 1)

    print("   (d) CONTROL: a Monitor past its own timeout is over too")
    _window113(900, timeout_ms=600000)
    _n113, _ = _tick113()
    check("(d) a timed-out Monitor stands nothing down", _n113, 1)

    print("   (e) CONTROL: the planner's last word was done - the nudge")
    print("   goes, Monitor or not")
    _window113(10)
    # done, not continue: a continue sets `awaiting`, and assess stands
    # every tier down until the next Stop - no nudge could be asked about
    _report113("done")
    _window113(900)
    _n113, _ = _tick113()
    check("(e) after a verdict other than wait the nudge goes as before",
          _n113, 1)
finally:
    sessions.transcript_of = _tr113
    daemon.CFG["thresholds"] = _thr113
    post("/loop", {"project": _pn113, "action": "stop"}, secret=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n113. a foreground record of a session that is gone is closed - at its")
print("     end, at a new session of its half, by the sweep, and at start")
print("    a watched project's inflight held a `pkill ...` of an executor session from")
print("    21.09 23:53:22: its window died mid-call, no PostToolUse ever came,")
print("    and only background records were ever closed for a session that is")
print("    gone. Real order: SessionStart, a real PreToolUse, then the event")
print("    that says the session is over, or the sweep. -> DECISIONS.md 8.35")
import subprocess as _sp113                                # noqa: E402
_pf113 = os.path.join(TMP, "gone-fg")
os.makedirs(_pf113, exist_ok=True)
_kf113 = canon(_pf113)
post("/config", {"projects": {A: {}, B: {}, C: {}, _pf113: {}}})


def _start113(sid, window=None):
    ev = {"hook_event_name": "SessionStart", "role": "executor",
          "session_id": sid, "project_dir": _pf113, "cwd": _pf113}
    if window:
        ev["window_pid"] = window
    post_rc("/event", ev)


def _run113(sid, cmd, tid):
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": sid, "project_dir": _pf113,
                       "cwd": _pf113, "tool_name": "Bash",
                       "tool_use_id": tid, "tool_input": {"command": cmd}})


def _recs113():
    return {k: dict(m) for k, m in ((daemon.STATE.get("inflight") or {})
                                    .get(_kf113) or {}).items()}


def _has113(tid):
    return any((m.get("tid") or "") == tid for m in _recs113().values())


def _said113(sub):
    return [r.get("text") or "" for r in
            daemon.store.recent_events(400, project=_pf113)
            if sub in (r.get("text") or "")]


try:
    print("   (a) the session's own SessionEnd closes its foreground record")
    _start113("a113gone-1")
    _run113("a113gone-1", "make build", "toolu_a113")
    check("(a) the command is on record", _has113("toolu_a113"), True)
    post_rc("/event", {"hook_event_name": "SessionEnd", "role": "executor",
                       "session_id": "a113gone-1", "project_dir": _pf113,
                       "cwd": _pf113, "reason": "other"})
    check("(a) its session ended: the record is closed",
          until(lambda: not _has113("toolu_a113"), 5), True)
    check("(a) with one line saying why",
          len([t for t in _said113("record dropped") if "a113gone" in t]), 1)

    print("   (b) a different session of the half starting closes the old")
    print("   session's record")
    _start113("b113gone-2")
    _run113("b113gone-2", "make build", "toolu_b113")
    _start113("c113gone-3")
    check("(b) the half moved on: the old session's record is closed",
          until(lambda: not _has113("toolu_b113"), 5), True)

    print("   (c) the sweep: a window the bridge knows, dead, closes its")
    print("   session's record")
    _win113 = _sp113.Popen([sys.executable, "-c",
                            "import time; time.sleep(60)"])
    post("/channel/register", {"project": _pf113, "role": "executor",
                               "port": open_channel(_pf113, "executor"),
                               "pid": os.getpid(), "ppid": _win113.pid},
         secret=True)
    _start113("d113gone-4", window=_win113.pid)
    _run113("d113gone-4", "make build", "toolu_d113")
    daemon.check_processes()
    check("(c) CONTROL: while its window lives the record stays",
          _has113("toolu_d113"), True)
    daemon.sessions.terminate_and_wait(_win113.pid)
    daemon.check_processes()
    check("(c) its window died: the sweep closes it",
          _has113("toolu_d113"), False)
    check("(c) and says which window",
          len([t for t in _said113("Command record closed")
               if "is gone" in t and str(_win113.pid) in t]), 1)

    print("   (d) the live form, at start: a record of a session the role")
    print("   ledger knows, whose half has moved on, and one no book knows")
    with daemon._lock:
        daemon.STATE.setdefault("session_roles", {})["e113gone-5"] = {
            "role": "executor", "path": _kf113, "project": "gone-fg",
            "at": time.time() - 90000}
        daemon.STATE.setdefault("inflight", {}).setdefault(_kf113, {})[
            "pkill"] = {"cmd": "pkill -f night.sh", "session": "e113gone-5",
                        "started": time.time() - 90000, "tid": "toolu_e113"}
        daemon.STATE["inflight"][_kf113]["unknown113"] = {
            "cmd": "make build", "session": "f113gone-nobody",
            "started": time.time() - 90000, "tid": "toolu_f113"}
        daemon.save_state()
    getattr(daemon, "migrate_gone_session_records", lambda: None)()
    check("(d) the moved-on session's record is closed at start",
          _has113("toolu_e113"), False)
    check("(d) and the one no book knows",
          _has113("toolu_f113"), False)
    check("(d) each said once, with its reason",
          (len([t for t in _said113("At start: Command record closed")
                if "moved on" in t]),
           len([t for t in _said113("At start: Command record closed")
                if "no record of the bridge knows" in t])), (1, 1))

    print("   (e) CONTROL: the half's current session keeps its record, and a")
    print("   background record is left to its own rules")
    # a session of its own: by now c113 has been moved on from, by d113
    _start113("g113gone-7")
    _run113("g113gone-7", "make build", "toolu_g113")
    # a BACKGROUND record of a session that is gone - the moved-on one of
    # (d): its own rules (bg_session_gone, BG_MAX_SEC, 8.31) decide, and
    # the sweep and the migration, which are foreground-only, must not
    with daemon._lock:
        daemon.STATE.setdefault("inflight", {}).setdefault(_kf113, {})[
            "bg:h113"] = {"cmd": "make all", "session": "e113gone-5",
                          "bg": True, "started": time.time(),
                          "tid": "toolu_h113"}
        daemon.save_state()
    daemon.check_processes()
    getattr(daemon, "migrate_gone_session_records", lambda: None)()
    check("(e) the current session's record stays", _has113("toolu_g113"),
          True)
    check("(e) a background record is not taken by the foreground rule",
          _has113("toolu_h113"), True)

    print("   (f) a dead pid the ledger saw is not a dead WINDOW unless the")
    print("   bridge knew it as one - 15.1's first days left shell pids there")
    _sh113 = _sp113.Popen([sys.executable, "-c", "pass"])
    _sh113.wait(10)
    with daemon._lock:
        daemon.STATE.setdefault("window_sessions", {}).setdefault(
            "%s|executor" % _kf113, {})[str(_sh113.pid)] = {
                "sid": "g113gone-7", "at": time.time()}
        daemon.save_state()
    daemon.check_processes()
    check("(f) the current session's record stays over a dead shell pid",
          _has113("toolu_g113"), True)
    check("(f) and the migration runs from main(), at every start",
          "migrate_gone_session_records()" in inspect.getsource(daemon.main),
          True)
finally:
    try:
        _win113.kill()
    except Exception:
        pass
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(_kf113, None)
        daemon.save_state()
    daemon.PROCTRACK.pop(_kf113, None)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


def calls_in_main(name):
    """Is `name` CALLED in daemon.main - a real Call node, not the text.

    A substring of inspect.getsource is satisfied by a comment: the sabotage
    that commented a migration out of main() stayed green on exactly that
    (piece 23, T4). -> DECISIONS.md 8.37"""
    import ast as _ast
    import textwrap as _tw
    try:
        tree = _ast.parse(_tw.dedent(inspect.getsource(daemon.main)))
    except (OSError, SyntaxError, TypeError):
        return False
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Call):
            f = n.func
            if (isinstance(f, _ast.Name) and f.id == name) or (
                    isinstance(f, _ast.Attribute) and f.attr == name):
                return True
    return False



print("\n114. a handover names the window it replaces BEFORE it opens one")
print("    2026-09-26 17:13:25, a watched pair's planner: the record was empty -")
print("    it had named 3724, the second window of a double start on 09-23,")
print("    and that window had gone - while pid 22824 had been the planner")
print("    for three days, holding the seat, linked to the session being")
print("    replaced. The replacement opened with nothing to stop, and")
print("    17:15:16 said 'Planner handover complete' with 22824 still")
print("    working. Real order: the double start, the second window closing,")
print("    the first speaking, the panel's button, the new SessionStart.")
print("    -> DECISIONS.md 8.36")

_LONG_PY = os.path.join(BIN, "claude_stub_long.py")
with open(_LONG_PY, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys, time\n"
        "row = {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "       'role': os.environ.get('BRIDGE_ROLE')}\n"
        "open(%r, 'a', encoding='utf-8').write("
        "json.dumps(row, ensure_ascii=False) + '\\n')\n"
        "time.sleep(900)\n" % LAUNCHES)
_W22 = []                       # every window these cases opened


def _long_build22(*a, **kw):
    cmd = _real_build(*a, **kw)
    return [sys.executable, _LONG_PY] + cmd[1:]


def _open22(project, role):
    """A window the BRIDGE opens: the real /session launch, with a stub that
    stays up. Returns its pid, off the record reg_pid wrote."""
    n = len(launches())
    _prev = sessions.build_command
    sessions.build_command = _long_build22
    try:
        # `confirm`: the panel's "start another anyway?" - how the second
        # window of 2026-09-23's double start was opened
        post("/session", {"action": "launch", "project": project,
                          "role": role, "confirm": True})
    finally:
        sessions.build_command = _prev
    until(lambda: len(launches()) > n, 30)
    pid = ((daemon.STATE.get("pids") or {}).get(
        "%s|%s" % (canon(project), role)) or {}).get("pid")
    if pid:
        _W22.append(pid)
    return pid


def _proc22():
    """A live process to stand for a channel.py - a contender must be
    running, or note_channel_refused prunes it before it can count."""
    p = subprocess.Popen([sys.executable, "-c",
                          "import time" + chr(10) + "time.sleep(900)"],
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
    STAND_INS.append(p)
    return p.pid


def _chan22(project, role, window, tag, cpid=None):
    """The channel.py of window `window`, registered through the real
    endpoint with its own pid and its parent - as channel.py does. Its
    deliveries are recorded under (project, role#tag). `cpid` is the SAME
    channel.py coming back: it re-registers every 45 s with one pid, and
    refusals are counted per contender."""
    port = open_channel(project, "%s#%s" % (role, tag))
    return post("/channel/register", {"project": project, "role": role,
                                      "port": port, "pid": cpid or _proc22(),
                                      "ppid": window}, secret=True)


def _ev22(project, role, sid, name, window, **extra):
    body = {"hook_event_name": name, "role": role, "session_id": sid,
            "project_dir": project, "cwd": project, "window_pid": window}
    body.update(extra)
    return post_rc("/event", body)


_TR22 = {}


def _tr22(sid):
    return _TR22.setdefault(sid, os.path.join(TMP, "t22-%s.jsonl" % sid))


def _tw22(sid, kind, ago=0.0, subtype=None, content="work"):
    """One transcript line, stamped `ago` seconds back. The client writes
    them in this order; the stamp is when the block was begun."""
    row = {"type": kind, "timestamp": time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))}
    if subtype:
        row["subtype"] = subtype
    if kind in ("user", "assistant"):
        row["message"] = {"role": kind, "content": content}
    with open(_tr22(sid), "a", encoding="utf-8") as _fh:
        _fh.write(json.dumps(row) + "\n")


_tof22o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (
    lambda sid, cwd=None: _TR22.get(sid) if sid in _TR22 else None)


def _j22(sub, proj):
    _f = os.path.join(proj, "bridge-logs", time.strftime("%Y-%m-%d"),
                      "events.jsonl")
    if not os.path.isfile(_f):
        return []
    with open(_f, encoding="utf-8") as _fh:
        return [json.loads(l).get("text") or "" for l in _fh
                if l.strip() and sub in l]


def _settle22():
    """Join the threads a decision starts, so a NEGATIVE check is read after
    the thing it denies could have happened (5.9, case 61).

    Not orphan-handoff. It decides nothing - it is a delivery, and it waits
    up to two minutes for a channel that case 115 registers only AFTER its
    settle, so joining it sat out the whole 40 s on every run: 46.3 s for
    that case on both 5c707bc and b0e2a6c. No negative check reads it
    before a positive one has seen its one delivery. -> DECISIONS.md 8.39"""
    for _th in threading.enumerate():
        if _th.name == "orphan-handoff":
            continue
        if any(w in _th.name for w in ("handover", "orphan", "rotate",
                                       "restart", "revive", "launch")):
            _th.join(40)


def _opened22(project, since, role="planner"):
    """Windows the bridge opened for this half since `since`, off the ledger
    reg_pid writes BEFORE the launching call returns. Not launches(): the
    stub writes that row after Popen has returned, so a count taken from it
    raced the very launch it denied - the red run of these cases caught
    117 (a) green with a window opening under it. -> DECISIONS.md 8.36"""
    return len([r for r in (daemon.STATE.get("window_log") or [])
                if isinstance(r, dict) and r.get("role") == role
                and canon(r.get("path") or "") == canon(project)
                and float(r.get("at") or 0) >= since])


def _status22(project, role, sid, tokens):
    """A status line carrying `tokens` of input context, as the client posts
    it (the shape of case 96's). The window is stated at 1M."""
    post_rc("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": project, "project_dir": project},
        "model": {"display_name": "Opus 5", "id": "claude-opus-5"},
        "context_window": {"context_window_size": 1000000,
                           "used_percentage": tokens / 10000.0,
                           "current_usage": {
                               "input_tokens": 10,
                               "cache_creation_input_tokens": 90,
                               "cache_read_input_tokens": int(tokens) - 100,
                               "output_tokens": 4000}}}})


def _wall22(project, sid):
    """Five compactions that LANDED, as the client shows them: a reading,
    a PreCompact, a smaller reading - the planner is at its wall."""
    for _i in range(5):
        _status22(project, "planner", sid, 990000)
        post_rc("/event", {"hook_event_name": "PreCompact", "role": "planner",
                           "session_id": sid, "project_dir": project,
                           "cwd": project})
        _status22(project, "planner", sid, 300000 + _i * 10000)
    _status22(project, "planner", sid, 400000)


_p114 = os.path.join(TMP, "names-its-window")
os.makedirs(_p114, exist_ok=True)
_k114 = canon(_p114)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p114: {}}})
try:
    _W1 = _open22(_p114, "planner")
    _ev22(_p114, "planner", "s114-a", "SessionStart", _W1)
    _chan22(_p114, "planner", _W1, "w1")
    print("    the double start: a second window, and the record names it")
    _W2 = _open22(_p114, "planner")
    _ev22(_p114, "planner", "s114-b", "SessionStart", _W2)
    check("the record names the second window", daemon.pid_of(_p114,
                                                              "planner"), _W2)
    print("    ...which closes: its SessionEnd, after the window has gone")
    sessions.terminate_and_wait(_W2)
    _ev22(_p114, "planner", "s114-b", "SessionEnd", _W2)
    _ev22(_p114, "planner", "s114-a", "Notification", _W1,
          notification_type="idle_prompt", message="waiting")
    check("the record names no window - 17:13:25's state",
          daemon.pid_of(_p114, "planner"), None)
    check("and the window that IS the planner is alive",
          sessions.pid_alive(_W1), True)
    _b114 = len(launches())
    post("/handover", {"project": _p114, "role": "planner",
                       "reason": "case 114"})
    _settle22()
    check("a replacement was opened",
          until(lambda: len(launches()) > _b114, 30), True)
    _W3 = daemon.pid_of(_p114, "planner")
    _W22.append(_W3)
    check("and the handover names the window it replaces",
          (((daemon.STATE.get("handover") or {}).get(_k114) or {})
           .get("stop_after") or {}).get("planner"), _W1)
    _ev22(_p114, "planner", "s114-c", "SessionStart", _W3)
    check("the window it replaces is stopped when the new one reports",
          until(lambda: not sessions.pid_alive(_W1), 60), True)
    # `until` on the LINE: it is written after the stop, and a read the
    # moment the window died raced it - found by the parallel sabotage runs
    check("and 'complete' is written, and not 'NOT complete'",
          (until(lambda: bool(_j22("Planner handover complete", _p114)),
                 20),
           bool(_j22("handover is NOT complete", _p114))), (True, False))

    print("    (b) THE REFUSAL: the half is alive and its window is not the")
    print("    bridge's - a window opened by hand (5.43) holds the seat")
    _p114b = os.path.join(TMP, "cannot-name-its-window")
    os.makedirs(_p114b, exist_ok=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}, _p114: {},
                                  _p114b: {}}})
    _X114 = _proc22()
    _ev22(_p114b, "planner", "s114-x", "SessionStart", _X114)
    _chan22(_p114b, "planner", _X114, "x")
    _t114b = time.time()
    post("/handover", {"project": _p114b, "role": "planner",
                       "reason": "case 114 b"})
    _settle22()
    check("the decision is on record - held, with the reason",
          any("cannot be handed over" in _t and "not opened by the bridge"
              in _t for _t in _j22("Handover held", _p114b)), True)
    check("NO window was opened", _opened22(_p114b, _t114b), 0)
    check("and the window nobody asked the bridge about is untouched",
          sessions.pid_alive(_X114), True)
finally:
    with daemon._lock:
        for _k in (_k114, canon(os.path.join(TMP, "cannot-name-its-window"))):
            (daemon.STATE.get("handover") or {}).pop(_k, None)
        daemon.save_state()


print("\n115. an orphaned swap has its own clock, and is FINISHED to the")
print("    window already waiting - no third window")
print("    2026-09-26: 17:38:23 the seat was repaired back to the old")
print("    planner; 17:38:28 'The handover started 29840558 min ago never")
print("    finished', a failure counted and a person rung, about that")
print("    repair's own record - it had no `at`. The old window wrote its")
print("    handoff at 17:42:55, and at 18:13:34 a second handover opened a")
print("    THIRD window while the newcomer of 17:13:25 sat waiting.")
print("    -> DECISIONS.md 8.36")
_p115 = os.path.join(TMP, "orphan-finished")
os.makedirs(_p115, exist_ok=True)
_k115 = canon(_p115)
_kp115 = "%s|planner" % _k115
post("/config", {"projects": {A: {}, B: {}, C: {}, _p115: {}}})
post_rc("/loop", {"action": "start", "project": _p115})
_rw115 = getattr(daemon, "replaced_window", None)
try:
    _WO = _open22(_p115, "planner")
    _ev22(_p115, "planner", "s115-old", "SessionStart", _WO)
    _chan22(_p115, "planner", _WO, "old")
    _tw22("s115-old", "system", 600, "bridge_status")
    _wall22(_p115, "s115-old")
    print("    THE PRECONDITION, forced: the 22.1 defect - a handover that")
    print("    did not know its window. replaced_window now refuses that, so")
    print("    it is stood in for, for this one call, with what pid_of said")
    with daemon._lock:
        (daemon.STATE.get("pids") or {}).pop(_kp115, None)
        daemon.save_state()
    daemon.replaced_window = (lambda path, role:
                              (0, "the 22.1 defect, forced", True))
    _b115 = len(launches())
    _pb115 = sessions.build_command
    sessions.build_command = _long_build22      # the replacement stays up
    try:
        post("/handover", {"project": _p115, "role": "planner",
                           "reason": "case 115"})
        _settle22()
    finally:
        sessions.build_command = _pb115
    daemon.replaced_window = _rw115
    until(lambda: len(launches()) > _b115, 30)
    _WN = daemon.pid_of(_p115, "planner")
    _W22.append(_WN)
    _tw22("s115-new", "system", 0, "bridge_status")
    _ev22(_p115, "planner", "s115-new", "SessionStart", _WN)
    _chan22(_p115, "planner", _WN, "new")
    check("the newcomer holds the seat, the old window still works",
          (daemon.pid_of(_p115, "planner") == _WN,
           sessions.pid_alive(_WO)), (True, True))
    print("    the old window goes on writing, and its channel keeps coming")
    print("    back every 45 s - refused, five times")
    time.sleep(1.2)
    _tw22("s115-old", "user", 0, content="a report")
    _tw22("s115-old", "assistant", 0, content="reviewing it")
    print("    the newcomer takes a report and opens a turn it does not end -")
    print("    29064 at 17:35:05, then blocked on a permission prompt - and")
    print("    the OLD window fires a hook last, as 22824 did at 17:38:08:")
    print("    the order in which the old check read the two windows swapped")
    _tw22("s115-new", "user", 0, content="report 4081")
    _tw22("s115-new", "assistant", 0, content="calling the verdict tool")
    _ev22(_p115, "planner", "s115-old", "PreToolUse", _WO, tool_name="Read",
          tool_input={"file_path": "dialogue.md"})
    _cp115 = _proc22()
    for _i in range(daemon.CHANNEL_REFUSE_TELL):
        _chan22(_p115, "planner", _WO, "old%d" % _i, cpid=_cp115)
    check("the seat is back with the old window",
          daemon.pid_of(_p115, "planner"), _WO)
    print("    the old window's channel comes back as it does every 45 s,")
    print("    and - its window on record again - takes the seat")
    _chan22(_p115, "planner", _WO, "old-seat")
    check("and the line says what was OBSERVED",
          (any("Observed: pid %s (session s115-old" % _WO in _t
               for _t in _j22("orphaned swap repaired", _p115)),
           any("had already been cleared" in _t
               for _t in _j22("orphaned swap repaired", _p115))),
          (True, False))

    print("    (22.2) THE NEXT TICK: no clock read as 1970, no failure, no")
    print("    person rung - and the record survives it")
    tg_reset()
    daemon.assess(_p115)
    _settle22()
    check("no 'never finished'", _j22("never finished", _p115), [])
    check("no failed handover counted",
          (daemon.STATE.get("handover_failed") or {}).get(_k115), None)
    check("nobody told a handover stalled",
          [t for t in tg_texts() if "handover stalled" in t], [])
    check("the orphaned swap is still on record, with its own clock",
          bool(((daemon.STATE.get("orphaned_swap") or {}).get(_kp115)
                or {}).get("at")), True)
    print("    and an OLDER DAEMON's record, the shape of 17:38:23 - a key")
    print("    inside STATE['handover'] with no `at` - is moved, not counted")
    with daemon._lock:
        daemon.STATE.setdefault("handover", {})[_k115] = {"orphaned": {
            "role": "executor", "old": 1, "new": 2, "at": time.time()}}
        daemon.save_state()
    daemon.expire_handover(_p115)
    check("not counted as a failure",
          (daemon.STATE.get("handover_failed") or {}).get(_k115), None)
    check("moved to its own record",
          ((daemon.STATE.get("orphaned_swap") or {}).get(
              "%s|executor" % _k115) or {}).get("old"), 1)
    check("and the handover record holding it is gone",
          (daemon.STATE.get("handover") or {}).get(_k115), None)
    with daemon._lock:
        (daemon.STATE.get("orphaned_swap") or {}).pop("%s|executor" % _k115,
                                                      None)
        daemon.save_state()

    print("    (22.3) while the newcomer waits, no other window opens")
    check("launch_guard names the waiting replacement",
          str(_WN) in (daemon.launch_guard(_p115, "planner") or ""), True)

    print("    the old window writes its handoff on its own Stop, and its")
    print("    turn ends")
    _pf115 = (daemon.handover_pending_for(_p115, "planner") or {}).get("file")
    check("the repair asked the old window for its handoff", bool(_pf115),
          True)
    if _pf115:
        os.makedirs(os.path.dirname(_pf115), exist_ok=True)
        with open(_pf115, "w", encoding="utf-8") as _f:
            _f.write("# the old planner's thread" + chr(10))
    stop_hook(_p115, "planner", "s115-old",
              "HANDOFF WRITTEN: %s" % (_pf115 or "?"))
    _tw22("s115-old", "system", 0, "turn_duration")
    check("written, by the session it was asked of",
          (((daemon.handover_pending_for(_p115, "planner") or {})
            .get("written") or {}).get("sid")), "s115-old")
    _t115 = time.time()
    daemon.assess(_p115)
    _settle22()
    check("NO window was opened", _opened22(_p115, _t115), 0)
    check("the old window was stopped",
          until(lambda: not sessions.pid_alive(_WO), 60), True)
    check("the record is the newcomer's, and it is alive",
          (daemon.pid_of(_p115, "planner"), sessions.pid_alive(_WN)),
          (_WN, True))
    check("the orphaned swap is over",
          (daemon.STATE.get("orphaned_swap") or {}).get(_kp115), None)
    print("    the newcomer's channel takes the seat, and is handed the file")
    _chan22(_p115, "planner", _WN, "new2")
    check("the handoff reached the newcomer's own channel",
          until(lambda: bool(_pf115) and any(
              _pf115 in str(_d.get("content") or "") for _d in
              DELIVERED.get((_k115, "planner#new2"), [])), 40), True)
    check("and not to the window that was stopped",
          any(_pf115 and _pf115 in str(_d.get("content") or "") for _d in
              DELIVERED.get((_k115, "planner#old-seat"), [])), False)
finally:
    daemon.replaced_window = _rw115
    with daemon._lock:
        for _c in ("handover", "orphaned_swap", "handover_pending"):
            for _k in (_k115, _kp115):
                (daemon.STATE.get(_c) or {}).pop(_k, None)
        daemon.save_state()
    post_rc("/loop", {"action": "stop", "project": _p115})


print("\n116. an old window left beside the new one is closed - in BOTH")
print("    branches of the swap, and never in the middle of its turn")
print("    stop_the_replaced cleaned up the half's leftovers only when the")
print("    window being replaced was already gone; at 18:15:17 it was")
print("    alive, was stopped, and the other leftover - the newcomer of")
print("    17:13:25 - was never looked at. A leftover mid-turn is closed")
print("    when that turn ends, read from its OWN session. -> DECISIONS 8.36")
_p116 = os.path.join(TMP, "leftover-after-turn")
os.makedirs(_p116, exist_ok=True)
_k116 = canon(_p116)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p116: {}}})
_pids116 = dict(daemon.STATE.get("pids") or {})
_started116 = daemon.STATE.get("started_at")
try:
    _WA = _open22(_p116, "planner")
    _ev22(_p116, "planner", "s116-a", "SessionStart", _WA)
    _chan22(_p116, "planner", _WA, "a")
    _WB = _open22(_p116, "planner")
    _ev22(_p116, "planner", "s116-b", "SessionStart", _WB)
    _chan22(_p116, "planner", _WB, "b")
    print("    the first window is a leftover now: its channel is refused")
    _chan22(_p116, "planner", _WA, "a2")
    check("its refusal is on the book",
          _WA in daemon.leftover_windows(_p116, "planner", _WB), True)
    _tw22("s116-a", "system", 120, "turn_duration")
    _tw22("s116-a", "user", 60, content="a report, mid-turn")
    _b116 = len(launches())
    _pb116 = sessions.build_command
    sessions.build_command = _long_build22      # the replacement stays up
    try:
        post("/handover", {"project": _p116, "role": "planner",
                           "reason": "case 116"})
        _settle22()
    finally:
        sessions.build_command = _pb116
    until(lambda: len(launches()) > _b116, 30)
    _WC = daemon.pid_of(_p116, "planner")
    _W22.append(_WC)
    _ev22(_p116, "planner", "s116-c", "SessionStart", _WC)
    check("the window replaced was stopped - the was-alive branch",
          until(lambda: not sessions.pid_alive(_WB), 60), True)
    # `until` on the line for the same reason as 114's: close_leftovers
    # runs after the stop the check above waited for
    check("the leftover was looked at, and NOT closed mid-turn",
          (until(lambda: any(str(_WA) in _t
                             for _t in _j22("is mid-turn", _p116)), 20),
           sessions.pid_alive(_WA)), (True, True))
    print("    its turn ends; the next tick closes it")
    _tw22("s116-a", "system", 0, "turn_duration")
    with daemon._lock:
        daemon.STATE["pids"] = {k: v for k, v in
                                (daemon.STATE.get("pids") or {}).items()
                                if k.startswith(_k116)}
        daemon.STATE["started_at"] = time.time() - 3600
        daemon.save_state()
    daemon.check_sessions(float(daemon.CFG["thresholds"].get(
        "startup_grace", 600)))
    check("closed at the end of its turn",
          until(lambda: not sessions.pid_alive(_WA), 60), True)
    check("and the line says so",
          any(str(_WA) in _t for _t in _j22("old planner window closed",
                                            _p116)), True)
finally:
    with daemon._lock:
        _mine116 = {k: v for k, v in (daemon.STATE.get("pids") or {}).items()
                    if k.startswith(_k116)}
        daemon.STATE["pids"] = _pids116
        if _started116 is not None:
            daemon.STATE["started_at"] = _started116
        (daemon.STATE.get("handover") or {}).pop(_k116, None)
        (daemon.STATE.get("leftovers") or {}).pop("%s|planner" % _k116, None)
        daemon.save_state()


print("\n117. the planner's wall waits for the end of ITS turn, and asks THIS")
print("    session for its handoff")
print("    2026-09-26 17:13:25: 'its transcript written inside 180 s'")
print("    answered no about a planner three minutes into one long task -")
print("    the client writes an entry when the block is COMPLETE. And no")
print("    planner had been asked since 09-18: a demand of 09-12, never")
print("    answered and never tied to a session, stood as 'asked and")
print("    expired' for every planner after it. -> DECISIONS.md 8.36")
_p117 = os.path.join(TMP, "composing-planner")
os.makedirs(_p117, exist_ok=True)
_k117 = canon(_p117)
_kp117 = "%s|planner" % _k117
post("/config", {"projects": {A: {}, B: {}, C: {}, _p117: {}}})
post_rc("/loop", {"action": "start", "project": _p117})
try:
    _WP = _open22(_p117, "planner")
    _ev22(_p117, "planner", "s117", "SessionStart", _WP)
    _chan22(_p117, "planner", _WP, "p")
    _tw22("s117", "system", 900, "bridge_status")
    _wall22(_p117, "s117")
    print("    (a) THE RECORD OF 2026-09-12 as it stood: asked of no named")
    print("    session, never answered, told, two hours old")
    _hf117 = daemon.project_handoff_file(_p117, "planner")[0]
    with daemon._lock:
        daemon.STATE.setdefault("handover_pending", {})[_kp117] = {
            "at": time.time() - 7200, "why": "an old wall", "file": _hf117,
            "mtime": 0, "size": 0, "told": time.time() - 3600}
        daemon.save_state()
    _tw22("s117", "system", 600, "turn_duration")
    _tw22("s117", "user", 300, content="report 7")
    _tw22("s117", "assistant", 280, content="reading")
    _tw22("s117", "user", 250, content="a tool result")
    _t117 = time.time()
    daemon.assess(_p117)
    _settle22()
    check("NO window was opened", _opened22(_p117, _t117), 0)
    check("THIS session is asked, with the ceiling from now",
          (((daemon.handover_pending_for(_p117, "planner") or {})
            .get("sid")),
           time.time() - float((daemon.handover_pending_for(
               _p117, "planner") or {}).get("at") or 0) < 60),
          ("s117", True))
    print("    (b) it writes its handoff - and a NEW turn opens and composes,")
    print("    its last entry older than stall_grace")
    if _hf117:
        os.makedirs(os.path.dirname(_hf117), exist_ok=True)
        with open(_hf117, "w", encoding="utf-8") as _f:
            _f.write("# the thread" + chr(10))
    stop_hook(_p117, "planner", "s117", "HANDOFF WRITTEN: %s" % _hf117)
    check("written", bool((daemon.handover_pending_for(_p117, "planner")
                           or {}).get("written")), True)
    _tw22("s117", "system", 0, "turn_duration")
    _tw22("s117", "user", 240, content="report 8")
    _tw22("s117", "assistant", 220, content="composing a long task")
    _t117 = time.time()
    _r117 = daemon.assess(_p117)
    _settle22()
    check("held for its own open turn", "its own turn is open"
          in json.dumps(_r117), True)
    check("NO window was opened", _opened22(_p117, _t117), 0)
    print("    (c) THE CONTROL: the turn ends, and the same tick replaces it")
    _tw22("s117", "system", 0, "turn_duration")
    _t117 = time.time()
    _r117 = daemon.assess(_p117)
    _settle22()
    check("replaced", "handing over the planner" in json.dumps(_r117), True)
    check("one window", until(lambda: _opened22(_p117, _t117) == 1, 30),
          True)
    _W22.append(daemon.pid_of(_p117, "planner"))
finally:
    with daemon._lock:
        for _c in ("handover", "handover_pending"):
            for _k in (_k117, _kp117):
                (daemon.STATE.get(_c) or {}).pop(_k, None)
        daemon.save_state()
    post_rc("/loop", {"action": "stop", "project": _p117})


print("    (d) A DEAD WINDOW'S TURN HOLDS NOTHING. A window closed mid-turn")
print("    leaves no turn end - 7236's session ends on a verdict call at")
print("    15:28:01Z - and the record may still name it; its markers read")
print("    'open' for ever, and must not hold the wall")
_p117d = os.path.join(TMP, "dead-window-open-turn")
os.makedirs(_p117d, exist_ok=True)
_k117d = canon(_p117d)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p117d: {}}})
post_rc("/loop", {"action": "start", "project": _p117d})
try:
    _WQ = _open22(_p117d, "planner")
    _ev22(_p117d, "planner", "s117d", "SessionStart", _WQ)
    _chan22(_p117d, "planner", _WQ, "q")
    _tw22("s117d", "system", 900, "bridge_status")
    _wall22(_p117d, "s117d")
    stop_hook(_p117d, "planner", "s117d", "a review at the wall")
    _hf117d = (daemon.handover_pending_for(_p117d, "planner") or {}).get(
        "file") or ""
    if _hf117d:
        os.makedirs(os.path.dirname(_hf117d), exist_ok=True)
        with open(_hf117d, "w", encoding="utf-8") as _f:
            _f.write("# the thread" + chr(10))
    stop_hook(_p117d, "planner", "s117d", "HANDOFF WRITTEN: %s" % _hf117d)
    check("(d) its handoff is written",
          bool((daemon.handover_pending_for(_p117d, "planner") or {})
               .get("written")), True)
    _tw22("s117d", "system", 0, "turn_duration")
    _tw22("s117d", "user", 240, content="report 9")
    _tw22("s117d", "assistant", 220, content="calling the verdict tool")
    sessions.terminate_and_wait(_WQ)
    check("(d) the window is gone and its transcript still reads 'open'",
          (sessions.pid_alive(_WQ),
           getattr(daemon, "transcript_turn_state",
                   lambda tp: "?")(_tr22("s117d"))), (False, "open"))
    check("(d) and the record still names it",
          daemon.pid_of(_p117d, "planner"), _WQ)
    _t117d = time.time()
    _r117d = daemon.assess(_p117d)
    _settle22()
    check("(d) the wall is NOT held by the dead window's turn",
          "its own turn is open" in json.dumps(_r117d), False)
    check("(d) and the planner is replaced",
          until(lambda: _opened22(_p117d, _t117d) == 1, 30), True)
    _W22.append(daemon.pid_of(_p117d, "planner"))
finally:
    with daemon._lock:
        for _c in ("handover", "handover_pending"):
            for _k in (_k117d, "%s|planner" % _k117d):
                (daemon.STATE.get(_c) or {}).pop(_k, None)
        daemon.save_state()
    post_rc("/loop", {"action": "stop", "project": _p117d})


print("\n118. the record of which window a half is follows the window that")
print("    holds its seat - when every witness agrees, and never by a guess")
print("    2026-09-26 18:41:22: window 7236, the one ON RECORD, closed and")
print("    its session ended; because 29064 had redrawn a status line five")
print("    seconds earlier the line said 'an older window of this half',")
print("    and from 18:41:34 29064's channel was counted as 7236's pulse,")
print("    so the dead record was never judged. -> DECISIONS.md 8.36")
_p118 = os.path.join(TMP, "record-follows-the-seat")
os.makedirs(_p118, exist_ok=True)
_k118 = canon(_p118)
_kp118 = "%s|planner" % _k118
post("/config", {"projects": {A: {}, B: {}, C: {}, _p118: {}}})
_pids118 = dict(daemon.STATE.get("pids") or {})
_started118 = daemon.STATE.get("started_at")
try:
    _WD = _open22(_p118, "planner")
    _ev22(_p118, "planner", "s118-d", "SessionStart", _WD)
    _chan22(_p118, "planner", _WD, "d")
    _WE = _open22(_p118, "planner")
    _ev22(_p118, "planner", "s118-e", "SessionStart", _WE)
    _chan22(_p118, "planner", _WE, "e")
    with daemon._lock:
        daemon.STATE.setdefault("rc", {})[_kp118] = {
            "url": "rc-link-of-the-closed-window-118", "slug": ""}
        daemon.save_state()
    print("    the other window speaks last - a real hook with its window")
    _ev22(_p118, "planner", "s118-d", "Notification", _WD,
          notification_type="idle_prompt", message="waiting")
    check("last_session names the window NOT on record",
          daemon.last_session_id(_p118, "planner"), "s118-d")
    print("    the window on record closes, and its session ends")
    sessions.terminate_and_wait(_WE)
    _ev22(_p118, "planner", "s118-e", "SessionEnd", _WE)
    check("the end is judged by its window - not 'an older window'",
          [t for t in _j22("an older window of this half", _p118)
           if "s118-e" in t], [])
    check("and the record no longer names the closed window",
          daemon.pid_of(_p118, "planner") == _WE, False)
    print("    the other window's channel takes the seat; the next tick")
    _chan22(_p118, "planner", _WD, "d2")
    with daemon._lock:
        daemon.STATE["pids"] = {k: v for k, v in
                                (daemon.STATE.get("pids") or {}).items()
                                if k.startswith(_k118)}
        daemon.STATE["started_at"] = time.time() - 3600
        daemon.save_state()
    _t118 = time.time()
    daemon.check_sessions(float(daemon.CFG["thresholds"].get(
        "startup_grace", 600)))
    _settle22()
    check("the record moves to the window holding the seat",
          daemon.pid_of(_p118, "planner"), _WD)
    check("with a line saying which witnesses agreed",
          any("The record moves to it" in _t and str(_WD) in _t
              for _t in _j22("holds the channel seat", _p118)), True)
    check("the dead window's link is gone",
          (daemon.STATE.get("rc") or {}).get(_kp118), None)
    check("and NO window was opened", _opened22(_p118, _t118), 0)

    print("    (b) THE WITNESSES DISAGREE: the record names a closed window,")
    print("    the seat's window runs a session that is not the one on")
    print("    record - nothing moves, and it is said once")
    _dead118 = dead_pid()
    with daemon._lock:
        daemon.STATE["pids"][_kp118] = {"pid": _dead118,
                                        "at": time.time() - 3600,
                                        "registered": True,
                                        "registered_via": "session"}
        daemon.STATE.setdefault("last_session", {})[_kp118] = "s118-other"
        daemon.save_state()
    daemon.check_sessions(600)
    daemon.check_sessions(600)
    check("not moved", daemon.pid_of(_p118, "planner"), _dead118)
    check("said once, with the reason",
          len([t for t in _j22("record was NOT moved", _p118)
               if "disagree" in t]), 1)
    check("and no death was handled for a half whose window holds the seat",
          (daemon.STATE.get("down") or {}).get(_kp118), None)

    print("    (c) AT START: the live state of 2026-09-26 - the record on a")
    print("    closed window, the seat with the window the bridge opened,")
    print("    linked to the session on record - moved by the migration")
    with daemon._lock:
        daemon.STATE["last_session"][_kp118] = "s118-d"
        daemon.save_state()
    getattr(daemon, "reconcile_window_records",
            lambda at_start=False: None)(at_start=True)
    check("moved", daemon.pid_of(_p118, "planner"), _WD)
    check("with an 'At start:' line",
          any(_t.startswith("At start:") for _t in
              _j22("The record moves to it", _p118)), True)
    check("and main() runs it at every start",
          calls_in_main("reconcile_window_records"), True)
finally:
    with daemon._lock:
        daemon.STATE["pids"] = _pids118
        if _started118 is not None:
            daemon.STATE["started_at"] = _started118
        for _c in ("rc", "down", "adopt_told", "last_session"):
            (daemon.STATE.get(_c) or {}).pop(_kp118, None)
        daemon.save_state()
    daemon.sessions.transcript_of = _tof22o
    for _w in _W22:
        try:
            sessions.terminate_and_wait(_w)
        except Exception:
            pass
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n119. a command record ends when ITS call ends - a call a hook refused")
print("    included - and a session busy with another call is no alibi")
print("    2026-09-26 20:36:17 and 20:48:37: a watched project's own")
print("    PreToolUse guard refused two heredoc commands. The client wrote")
print("    their results ('PreToolUse:Bash hook error ... refused') and sent")
print("    no PostToolUse and no PostToolUseFailure - a refused call never")
print("    ran - so the records the bridge had opened lived on; and past the")
print("    hour command_still_open asked `looks_busy`, 'is ANY tool of this")
print("    session running', which a `sleep 285` answered yes. The restart")
print("    gate waited 23 minutes. Real order: the hooks, the transcript, the")
print("    tick. -> DECISIONS.md 8.37")
_p119 = os.path.join(TMP, "refused-call")
os.makedirs(_p119, exist_ok=True)
_k119 = canon(_p119)
_S119 = "s119-ex"
_TR119 = os.path.join(TMP, "s119-ex.jsonl")
post("/config", {"projects": {A: {}, B: {}, C: {}, _p119: {}}})
_tof119o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (
    lambda sid, cwd=None: _TR119 if sid == _S119 else _tof119o(sid, cwd))
# THE LIVE FORM OF THE COMMANDS, prefix and all: the bridge tracks a
# foreground call whose first line names a build tool, and every command
# of that project begins `cd /c/projects/godot/...` - which is why each
# of its calls is tracked at all.
_CD119 = "cd /c/projects/godot/a_game; "
_REFUSED119 = ("PreToolUse:Bash hook error: [guard.cmd no_heredoc.py]: "
               "[no_heredoc] heredoc in a shell command is refused. Refused.")
_grace119 = daemon.CFG.get("thresholds", {}).get("call_end_grace")


def _call119(tid, cmd, bg=False, result=None, error=False):
    """What the client writes for one Bash call: its tool_use, and - when
    the call is over - its tool_result carrying the same id."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    rows = [{"type": "assistant", "timestamp": ts, "message": {
        "role": "assistant", "content": [{
            "type": "tool_use", "id": tid, "name": "Bash",
            "input": {"command": cmd, "run_in_background": bg}}]}}]
    if result is not None:
        rows.append({"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": [{
                "tool_use_id": tid, "type": "tool_result",
                "content": result, "is_error": error}]}})
    # COMPACT, as the client writes its transcript - `"id":"toolu_…"`,
    # no space after the colon.
    with open(_TR119, "a", encoding="utf-8") as _fh:
        for _r in rows:
            _fh.write(json.dumps(_r, ensure_ascii=False,
                                 separators=(",", ":")) + "\n")


def _pre119(tid, cmd, bg=False):
    post_rc("/event", {"hook_event_name": "PreToolUse", "role": "executor",
                       "session_id": _S119, "project_dir": _p119,
                       "cwd": _p119, "tool_name": "Bash",
                       "tool_use_id": tid,
                       "tool_input": {"command": cmd,
                                      "run_in_background": bg}})


def _live119():
    return sorted(str((m or {}).get("tid") or "")
                  for m in daemon.inflight_live(_p119))


try:
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": _S119, "project_dir": _p119,
                       "cwd": _p119})
    print("    a heredoc the project's guard refuses: PreToolUse reaches the")
    print("    bridge, the call never runs, its result is the refusal")
    _pre119("toolu_119refused",
            _CD119 + "cat > /dev/null <<'X'" + chr(10) + "X")
    _call119("toolu_119refused",
             _CD119 + "cat > /dev/null <<'X'" + chr(10) + "X",
             result=_REFUSED119, error=True)
    print("    a background launch refused the same way - it starts no job")
    _pre119("toolu_119bgrefused", "until [ -f done ]; do sleep 5; done",
            bg=True)
    _call119("toolu_119bgrefused", "until [ -f done ]; do sleep 5; done",
             bg=True, result=_REFUSED119, error=True)
    print("    CONTROL: a background launch that STARTED a job - its end is")
    print("    the job's notice, not this result")
    _pre119("toolu_119bgjob", "py long_render.py", bg=True)
    _call119("toolu_119bgjob", "py long_render.py", bg=True,
             result="Command running in background with ID: b119job.")
    print("    and the same session is now inside another call - `sleep 285`")
    _pre119("toolu_119sleep", _CD119 + "sleep 285")
    _call119("toolu_119sleep", _CD119 + "sleep 285")
    check("four records are open before the tick",
          _live119(), sorted(["toolu_119refused", "toolu_119bgrefused",
                              "toolu_119bgjob", "toolu_119sleep"]))
    _m119 = dict(((daemon.STATE.get("inflight") or {}).get(_k119) or {})
                 .get("toolu_119refused") or {})
    _s119 = dict(((daemon.STATE.get("inflight") or {}).get(_k119) or {})
                 .get("toolu_119sleep") or {})
    print("    command_still_open answers about ITS call, not the session's")
    check("the refused call is not open, though the session is busy",
          daemon.command_still_open(dict(_m119)), False)
    check("CONTROL: the call that IS running is open",
          daemon.command_still_open(dict(_s119)), True)
    print("    the tick, with the grace a PostToolUse is given set to 0")
    post("/config", {"thresholds": dict(daemon.CFG["thresholds"],
                                        call_end_grace=0)})
    daemon.check_processes()
    check("the refused call's record is closed",
          "toolu_119refused" in _live119(), False)
    check("the refused background launch's record is closed",
          "toolu_119bgrefused" in _live119(), False)
    check("CONTROL: the job that started keeps its record",
          "toolu_119bgjob" in _live119(), True)
    check("CONTROL: the running call keeps its record",
          "toolu_119sleep" in _live119(), True)
    check("each closure is said, with the refusal as its witness",
          len([t for t in _j22("Command record closed: its", _p119)
               if "it was refused" in t]), 2)
    check("the pair's busy count is the running call and the job alone",
          (((get("/state").get("pairs") or {}).get(_k119) or {})
           .get("busy") or {}).get("inflight"), 2)
    print("    the running call ends the ordinary way - its PostToolUse")
    post_rc("/event", {"hook_event_name": "PostToolUse", "role": "executor",
                       "session_id": _S119, "project_dir": _p119,
                       "cwd": _p119, "tool_name": "Bash",
                       "tool_use_id": "toolu_119sleep",
                       "tool_input": {"command": _CD119 + "sleep 285"}})
    check("closed by its own PostToolUse", "toolu_119sleep" in _live119(),
          False)
    print("    AT START: a record the last daemon left for a refused call")
    _pre119("toolu_119atstart",
            _CD119 + "cat > /dev/null <<'Y'" + chr(10) + "Y")
    _call119("toolu_119atstart",
             _CD119 + "cat > /dev/null <<'Y'" + chr(10) + "Y",
             result=_REFUSED119, error=True)
    getattr(daemon, "migrate_ended_calls", lambda: 0)()
    check("closed at start", "toolu_119atstart" in _live119(), False)
    check("with an 'At start:' line",
          any(t.startswith("At start: Command record closed: its call")
              for t in _j22("Command record closed", _p119)), True)
    check("and main() runs it at every start",
          calls_in_main("migrate_ended_calls"), True)
finally:
    daemon.sessions.transcript_of = _tof119o
    post("/config", {"thresholds": dict(
        daemon.CFG["thresholds"],
        call_end_grace=_grace119 if _grace119 is not None else 30)})
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(_k119, None)
        daemon.save_state()
    daemon.PROCTRACK.pop(_k119, None)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n120. a task sent while the report waited for its verdict IS the next")
print("    piece - the planner is not asked for one")
print("    2026-09-26, this bridge's own journal: a task at 18:55:50, `done` on")
print("    report 417 at 18:56:29, and at 18:57:29 'asked the planner for one")
print("    - and the asking cost a planner wake'. The task was counted only if")
print("    it came AFTER the verdict. -> DECISIONS.md 8.38")
_p120 = os.path.join(TMP, "task-before-verdict")
os.makedirs(_p120, exist_ok=True)
_k120 = canon(_p120)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p120: {}}})
post_rc("/loop", {"action": "start", "project": _p120})
for _r, _sid in (("executor", "s120-ex"), ("planner", "s120-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _p120,
                       "cwd": _p120})
    register(_p120, _r, _sid)
with open(os.path.join(_p120, "seen120.txt"), "w", encoding="utf-8") as _f:
    _f.write("read" + chr(10))
_nw120 = daemon.NUDGE_AFTER_VERDICT_SEC
daemon.NUDGE_AFTER_VERDICT_SEC = 0.5


def _nudges120():
    return [t for t in _j22("accepted", _p120)
            if "had already sent the next task" in t
            or "was asked for one" in t]


try:
    print("    the executor ends a turn; its report waits for a verdict")
    _t120 = threading.Thread(target=lambda: stop_hook(
        _p120, "executor", "s120-ex", "report one: the first piece is done"),
        daemon=True)
    _t120.start()
    check("the report is waiting", until(lambda: bool(
        daemon.PENDING.get(_k120)), 20), True)
    print("    the planner sends the next task WHILE the report waits, then")
    print("    accepts the report")
    post("/task", {"project": _p120,
                   "instructions": "the next piece: the second map"},
         secret=True)
    post("/verdict", {"project": _p120, "verdict": "done",
                      "feedback": "Checked: seen120.txt" + chr(10)
                      + "accepted"}, secret=True)
    _t120.join(30)
    check("the decision is written down",
          until(lambda: bool(_nudges120()), 15), True)
    check("it counted the task: nothing to ask for",
          any("had already sent the next task" in t for t in _nudges120()),
          True)
    check("and the planner was NOT asked",
          any("was asked for one" in t for t in _nudges120()), False)
    print("    CONTROL: the next report is accepted with no task at all - and")
    print("    the planner IS asked, as before")
    _b120 = len(_nudges120())
    _t120b = threading.Thread(target=lambda: stop_hook(
        _p120, "executor", "s120-ex", "report two: the second map is done"),
        daemon=True)
    _t120b.start()
    until(lambda: bool(daemon.PENDING.get(_k120)), 20)
    post("/verdict", {"project": _p120, "verdict": "done",
                      "feedback": "Checked: seen120.txt" + chr(10)
                      + "accepted"}, secret=True)
    _t120b.join(30)
    check("asked for the next piece",
          until(lambda: any("was asked for one" in t
                            for t in _nudges120()[_b120:]), 15), True)
finally:
    daemon.NUDGE_AFTER_VERDICT_SEC = _nw120
    post_rc("/loop", {"action": "stop", "project": _p120})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n121. a permission dialog is closed by the window that asked - not by")
print("    a verdict another window of the half sent")
print("    2026-09-26: 'planner permission answered after 2.2 min ... by the")
print("    verdict reaching the bridge' at 17:38:23 - the verdict was the old")
print("    window's, the dialog the newcomer's; and again at 18:41:17 the")
print("    other way round. /verdict carries no identity. -> DECISIONS.md 8.38")
_p121 = os.path.join(TMP, "ask-of-another-window")
os.makedirs(_p121, exist_ok=True)
_k121 = canon(_p121)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p121: {}}})


def _ask121(p):
    with daemon._lock:
        return dict((daemon.STATE.get("asks") or {})
                    .get("%s|planner" % canon(p)) or {})


try:
    _WA121, _WB121 = _proc22(), _proc22()
    _ev22(_p121, "planner", "s121-a", "SessionStart", _WA121)
    _ev22(_p121, "planner", "s121-b", "SessionStart", _WB121)
    print("    window B asks for permission to use the verdict tool")
    _ev22(_p121, "planner", "s121-b", "Notification", _WB121,
          notification_type="permission_prompt",
          message="Claude needs your permission to use bridge - verdict")
    check("the dialog is open, for B's session",
          _ask121(_p121).get("sid"), "s121-b")
    print("    a verdict reaches the bridge - from A, which /verdict cannot say")
    post("/verdict", {"project": _p121, "verdict": "wait", "feedback": ""},
         secret=True)
    check("B's dialog is NOT closed by it",
          _ask121(_p121).get("sid"), "s121-b")
    check("and no line claims it was answered by that verdict",
          [t for t in _j22("permission answered", _p121)
           if "verdict reaching the bridge" in t], [])
    print("    B's own verdict call returns - its PostToolUse, its session")
    _ev22(_p121, "planner", "s121-b", "PostToolUse", _WB121,
          tool_name="mcp__bridge__verdict", tool_use_id="toolu_121verdict")
    check("closed now, by B's own call",
          (bool(_ask121(_p121)),
           any("its own call returning" in t
               for t in _j22("permission answered", _p121))), (False, True))
    print("    CONTROL: a half with ONE window - the verdict closes it as before")
    _p121b = os.path.join(TMP, "ask-one-window")
    os.makedirs(_p121b, exist_ok=True)
    post("/config", {"projects": {A: {}, B: {}, C: {}, _p121: {},
                                  _p121b: {}}})
    _WC121 = _proc22()
    _ev22(_p121b, "planner", "s121-c", "SessionStart", _WC121)
    _ev22(_p121b, "planner", "s121-c", "Notification", _WC121,
          notification_type="permission_prompt",
          message="Claude needs your permission to use bridge - verdict")
    check("its dialog is open", bool(_ask121(_p121b)), True)
    post("/verdict", {"project": _p121b, "verdict": "wait", "feedback": ""},
         secret=True)
    check("closed by the verdict, which can only be its own",
          (bool(_ask121(_p121b)),
           any("verdict reaching the bridge" in t
               for t in _j22("permission answered", _p121b))), (False, True))
finally:
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n122. 'possible orphaned swap ... NOT acted on' is said once per")
print("    (contender, holder, reason) - not at every refusal")
print("    2026-09-26: more than sixty of them in one pair, one every 45 s,")
print("    each saying the same thing. -> DECISIONS.md 8.38")
_p122 = os.path.join(TMP, "orphan-not-acted")
os.makedirs(_p122, exist_ok=True)
_k122 = canon(_p122)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p122: {}}})


def _not122():
    return [t for t in _j22("possible orphaned swap", _p122)
            if "NOT acted on" in t]


_WN122 = None
try:
    _WN122 = _open22(_p122, "planner")
    _ev22(_p122, "planner", "s122-new", "SessionStart", _WN122)
    _chan22(_p122, "planner", _WN122, "new")
    _WO122, _CP122 = _proc22(), _proc22()
    print("    another live window keeps starting a channel for this half:")
    print("    refused ten times, every one past the fifth asks the question")
    for _i in range(10):
        _chan22(_p122, "planner", _WO122, "old%d" % _i, cpid=_CP122)
    check("one line for ten refusals with one reason", len(_not122()), 1)
    print("    the reason changes - the holder's record is now a replacement")
    print("    the bridge opened - and the line is said again, once")
    with daemon._lock:
        _r122 = (daemon.STATE.get("pids") or {}).get("%s|planner" % _k122)
        if isinstance(_r122, dict):
            _r122["why"] = "handover"
        daemon.save_state()
    for _i in range(5):
        _chan22(_p122, "planner", _WO122, "old-b%d" % _i, cpid=_CP122)
    check("two lines in all, the second with a new reason",
          (len(_not122()), len(set(t.split("NOT acted on - ", 1)[-1]
                                   for t in _not122()))), (2, 2))
finally:
    # The window this case opened. It was not closed until 2026-09-27, and
    # its stub sleeps 900 s holding this suite's stdout - see case 123.
    if _WN122:
        sessions.terminate_and_wait(_WN122)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n123. the suite leaves nothing running behind it")
print("    2026-09-26: case 122 opened a window and closed nothing. Launched")
print("    without a console of its own - this suite sets CREATE_NEW_CONSOLE")
print("    to 0 - its stub shares the suite's console, and so its stdout:")
print("    measured, a grandchild so born holds the pipe for its whole life.")
print("    The planner's check reads a suite through a pipe, so it waited")
print("    for the stub's 900 s and said 'timed out after 1200s' of a suite")
print("    that had passed. Nothing failed, so nothing said why; this case")
print("    turns a leak into a red line instead. -> DECISIONS.md 8.39")
_si123 = {_s.pid for _s in STAND_INS}
_ctl123 = subprocess.Popen([sys.executable, "-c",
                            "import time" + chr(10) + "time.sleep(120)"],
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
_kids123 = sessions.child_pids(os.getpid(), names=())
print("    the control: a process started here, now - the probe must see it,")
print("    or an empty list below would only mean the probe is blind")
check("the probe sees this suite's children, the control among them",
      _kids123 is not None and _ctl123.pid in _kids123, True)
_left123 = [_k for _k in (_kids123 or [])
            if _k != _ctl123.pid and _k not in _si123
            and sessions.pid_alive(_k)]
check("nothing a case started is still running (stand-ins apart - they are "
      "stopped below, by design)", _left123, [])
for _k in _left123 + [_ctl123.pid]:
    try:
        sessions.terminate_and_wait(_k)
    except Exception:
        pass


print("\n124. a permission dialog whose call was REFUSED is over when the")
print("    call's record is closed by the refusal - not at the end of the turn")
print("    2026-09-27: the executor asked at 01:24:07 (to use Bash), the")
print("    client's safety check refused the call, and no Post event came.")
print("    close_ended_calls ended the command's record at 01:26:11 on the")
print("    refusal in the transcript; the dialog about that same call stayed")
print("    open, and the turn read as standing on a person. Real order:")
print("    PreToolUse, Notification, the refusal written, the tick.")
print("    -> DECISIONS.md 8.41")
_p124 = os.path.join(TMP, "refused-ask")
os.makedirs(_p124, exist_ok=True)
_k124 = canon(_p124)
_S124 = "s124-ex"
_TR124 = os.path.join(TMP, "s124-ex.jsonl")
post("/config", {"projects": {A: {}, B: {}, C: {}, _p124: {}}})
_tof124o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (
    lambda sid, cwd=None: _TR124 if sid == _S124 else _tof124o(sid, cwd))
_grace124 = daemon.CFG.get("thresholds", {}).get("call_end_grace")
_REFUSED124 = ("Permission for this command was denied by a built-in Claude "
               "Code safety check, not by the user.")


def _ev124(name, **extra):
    body = {"hook_event_name": name, "role": "executor",
            "session_id": _S124, "project_dir": _p124, "cwd": _p124}
    body.update(extra)
    return post_rc("/event", body)


def _written124(tid, tool, inp, result=None):
    # compact, as the client writes it (see case 119)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    rows = [{"type": "assistant", "timestamp": ts, "message": {
        "role": "assistant", "content": [{"type": "tool_use", "id": tid,
                                          "name": tool, "input": inp}]}}]
    if result is not None:
        rows.append({"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": [{"tool_use_id": tid,
                                         "type": "tool_result",
                                         "content": result,
                                         "is_error": True}]}})
    with open(_TR124, "a", encoding="utf-8") as _fh:
        for _r in rows:
            _fh.write(json.dumps(_r, ensure_ascii=False,
                                 separators=(",", ":")) + "\n")


def _ask124():
    return dict((daemon.STATE.get("asks") or {}).get(
        "%s|executor" % _k124) or {})


def _asklines124():
    return [t for t in _j22("permission answered", _p124)]


try:
    _ev124("SessionStart")
    post("/config", {"thresholds": dict(daemon.CFG["thresholds"],
                                        call_end_grace=0)})
    print("    (a) the call is tracked, the dialog is about it, the client")
    print("    refuses it and sends no Post event")
    _CMD124 = _CD119 + 'rm -rf "$W/$t"'
    _ev124("PreToolUse", tool_name="Bash", tool_use_id="toolu_124refused",
           tool_input={"command": _CMD124})
    _ev124("Notification", notification_type="permission_prompt",
           message="Claude needs your permission to use Bash")
    check("(a) the dialog is open, about that call",
          _ask124().get("call"), "toolu_124refused")
    _written124("toolu_124refused", "Bash", {"command": _CMD124},
                result=_REFUSED124)
    daemon.check_processes()
    check("(a) the command's record is closed on the refusal",
          "toolu_124refused" in [str((m or {}).get("tid") or "")
                                 for m in daemon.inflight_live(_p124)], False)
    check("(a) and the dialog about it is over", _ask124(), {})
    _l124 = _asklines124()
    check("(a) in one line, which names the refusal as its witness",
          (len(_l124), bool(_l124) and "refused: Permission for this command"
           in _l124[-1] and "no Post event" in _l124[-1]), (1, True))

    print("    (b) CONTROL: the dialog is about ANOTHER call - a refused")
    print("    record closing does not end it")
    _CMD124b = _CD119 + 'rm -rf "$D/old"'
    _ev124("PreToolUse", tool_name="Bash", tool_use_id="toolu_124b",
           tool_input={"command": _CMD124b})
    _written124("toolu_124b", "Bash", {"command": _CMD124b},
                result=_REFUSED124)
    _ev124("PreToolUse", tool_name="Edit", tool_use_id="toolu_124edit",
           tool_input={"file_path": os.path.join(_p124, "x.txt")})
    _ev124("Notification", notification_type="permission_prompt",
           message="Claude needs your permission to use Edit")
    check("(b) the dialog is about the newer call",
          _ask124().get("call"), "toolu_124edit")
    daemon.check_processes()
    check("(b) the refused call's record is closed",
          "toolu_124b" in [str((m or {}).get("tid") or "")
                           for m in daemon.inflight_live(_p124)], False)
    check("(b) and the dialog about the OTHER call stays open",
          _ask124().get("call"), "toolu_124edit")
    check("(b) no second 'answered' line", len(_asklines124()), 1)
finally:
    daemon.sessions.transcript_of = _tof124o
    post("/config", {"thresholds": dict(
        daemon.CFG["thresholds"],
        call_end_grace=_grace124 if _grace124 is not None else 30)})
    with daemon._lock:
        for _c in ("inflight", "asks"):
            _box = daemon.STATE.get(_c) or {}
            for _kk in [x for x in _box if str(x).startswith(_k124)]:
                _box.pop(_kk, None)
        daemon.save_state()
    daemon.PROCTRACK.pop(_k124, None)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n125. a booked task is settled by EVERY transcript of the pair's")
print("    executor since the booking - not only the current window's")
print("    another pair, 2026-09-25, two re-hands in its journal: both tasks")
print("    landed in one window and were taken there; the window was")
print("    replaced, and the witness, reading only the new window's")
print("    transcript, handed both over again to the new one. Real order: the")
print("    task, the turn taking it, a new window, its Stop, the done.")
print("    -> DECISIONS.md 8.42")
_p125 = os.path.join(TMP, "window-changed")
os.makedirs(_p125, exist_ok=True)
_k125 = canon(_p125)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p125: {}}})
post_rc("/loop", {"action": "start", "project": _p125})
_SA125, _SB125 = "win125-a", "win125-b"
_TA125 = os.path.join(TMP, "win125-a.jsonl")
_TB125 = os.path.join(TMP, "win125-b.jsonl")
_tof125o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (
    lambda sid, cwd=None: {_SA125: _TA125, _SB125: _TB125}.get(sid)
    or _tof125o(sid, cwd))
for _r, _sid in (("executor", _SA125), ("planner", "win125-pl")):
    post_rc("/event", {"hook_event_name": "SessionStart", "role": _r,
                       "session_id": _sid, "project_dir": _p125,
                       "cwd": _p125})
    register(_p125, _r, _sid)


# This case's own, so it runs without case 99 before it: the stamp the
# client writes, and the words a re-hand carries (hand_back_open_task).
_REHAND125 = "It is still the work in hand"


def _now125():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def _w125(tp, rows):
    with open(tp, "a", encoding="utf-8") as _fh:
        for _r in rows:
            _fh.write(json.dumps(_r, ensure_ascii=False,
                                 separators=(",", ":")) + "\n")


def _asst125(text):
    return {"type": "assistant", "timestamp": _now125(),
            "message": {"content": [{"type": "text", "text": text}]}}


def _exec125():
    return DELIVERED.get((_k125, "executor")) or []


def _taken125(text):
    # the client's mid-turn shape, its envelope rendered from what the
    # recording channel was handed - as case 99 does (8.41)
    for _d in reversed(_exec125()):
        if text in (_d.get("content") or ""):
            env = "%s\n%s\n</channel>" % (
                daemon.channel_envelope(_d.get("meta") or {}), _d["content"])
            return {"type": "attachment", "timestamp": _now125(),
                    "attachment": {"type": "queued_command", "prompt": env,
                                   "commandMode": "prompt", "isMeta": True}}
    return {"type": "system", "content": "no delivery carried %r" % text}


def _book125():
    return list((daemon.STATE.get("tasks_open") or {}).get(_k125) or [])


_T125a = "task-125a: booked in window A, taken there, settled after B came"
_T125b = "task-125b: booked in window A, never read by anybody"
try:
    _w125(_TA125, [_asst125("window A at work")])
    print("    two tasks arrive while window A's turn runs")
    for _t in (_T125a, _T125b):
        post("/task", {"project": _p125, "instructions": _t}, secret=True)
        until(lambda: any(_t in (_d.get("content") or "")
                          for _d in _exec125()), 20)
    check("both are on the book, each with the session it was booked in",
          until(lambda: len(_book125()) == 2, 5)
          and [b.get("sid") for b in _book125()], [_SA125, _SA125])
    print("    window A's turn takes the first one")
    _w125(_TA125, [_taken125(_T125a), _asst125("took it, working")])
    print("    and window A is replaced by window B")
    post_rc("/event", {"hook_event_name": "SessionStart", "role": "executor",
                       "session_id": _SB125, "project_dir": _p125,
                       "cwd": _p125})
    register(_p125, "executor", _SB125)
    _w125(_TB125, [_asst125("window B at work")])
    check("PRECONDITION: the current window is B - its transcript is the "
          "only one the old witness read",
          daemon.transcript_path_for(_p125, "executor"), _TB125)
    _n125 = len(_exec125())
    _th125 = threading.Thread(target=stop_hook, args=(
        _p125, "executor", _SB125, "report from window B"), daemon=True)
    _th125.start()
    check("the report is pending", until(
        lambda: _k125 in daemon.PENDING, 25), True)
    check("the Stop settled the task window A took, naming window A",
          until(lambda: any(
              "was taken up" in t and _T125a[:30] in t and _SA125[:8] in t
              for t in _j22("was taken up", _p125)), 10), True)
    with open(os.path.join(_p125, "seen125.txt"), "w") as _fh:
        _fh.write("read" + chr(10))
    post("/verdict", {"project": _p125, "verdict": "done",
                      "feedback": "Checked: seen125.txt\naccepted"},
         secret=True)
    _th125.join(40)
    _again125 = lambda: [_d.get("content") or "" for _d in _exec125()[_n125:]
                         if _REHAND125 in (_d.get("content") or "")]
    print("    CONTROL: the task nobody read is handed over again - the")
    print("    witness only ever takes positive evidence")
    check("the unread task is handed over again, once",
          until(lambda: len(_again125()) == 1, 8)
          and _T125b in _again125()[0], True)
    check("and the task window A took is NOT",
          any(_T125a in _c for _c in _again125()), False)
    check("the book is empty", _book125(), [])
finally:
    daemon.sessions.transcript_of = _tof125o
    post_rc("/loop", {"action": "stop", "project": _p125})
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n126. a permission dialog about a call with NO tracked record - a")
print("    refused Edit - is over when its call has a result, not at the")
print("    end of the turn")
print("    8.41 closed a refused call's dialog where a tracked command's")
print("    record is ended, so only a tracked Bash command reached it. The")
print("    dialog knows its call and its session; its own call's result is")
print("    asked for by id, tracked or not. Real order: PreToolUse Edit,")
print("    the permission notice, the refusal written with no Post, the")
print("    tick. -> DECISIONS.md 8.42")
_p126 = os.path.join(TMP, "refused-edit")
os.makedirs(_p126, exist_ok=True)
_k126 = canon(_p126)
_S126 = "s126-ex"
_TR126 = os.path.join(TMP, "s126-ex.jsonl")
post("/config", {"projects": {A: {}, B: {}, C: {}, _p126: {}}})
_tof126o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (
    lambda sid, cwd=None: _TR126 if sid == _S126 else _tof126o(sid, cwd))
_grace126 = daemon.CFG.get("thresholds", {}).get("call_end_grace")
_REJECT126 = ("The user doesn't want to proceed with this tool use. The tool "
              "use was rejected (eg. if it was a file edit, the new_string "
              "was NOT written to the file).")


def _ev126(name, **extra):
    body = {"hook_event_name": name, "role": "executor",
            "session_id": _S126, "project_dir": _p126, "cwd": _p126}
    body.update(extra)
    return post_rc("/event", body)


def _ask126():
    return dict((daemon.STATE.get("asks") or {}).get(
        "%s|executor" % _k126) or {})


def _lines126():
    return _j22("permission answered", _p126)


def _edit126(tid, tool="Edit", result=None):
    inp = {"file_path": os.path.join(_p126, "%s.txt" % tid),
           "old_string": "a", "new_string": "b"}
    _ev126("PreToolUse", tool_name=tool, tool_use_id=tid, tool_input=inp)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    rows = [{"type": "assistant", "timestamp": ts, "message": {
        "role": "assistant", "content": [{"type": "tool_use", "id": tid,
                                          "name": tool, "input": inp}]}}]
    if result is not None:
        rows.append({"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": [{"tool_use_id": tid,
                                         "type": "tool_result",
                                         "content": result,
                                         "is_error": True}]}})
    with open(_TR126, "a", encoding="utf-8") as _fh:
        for _r in rows:
            _fh.write(json.dumps(_r, ensure_ascii=False,
                                 separators=(",", ":")) + "\n")


try:
    _ev126("SessionStart")
    post("/config", {"thresholds": dict(daemon.CFG["thresholds"],
                                        call_end_grace=0)})
    print("    (a) an Edit asks, and is refused")
    _edit126("toolu_126edit")
    _ev126("Notification", notification_type="permission_prompt",
           message="Claude needs your permission to use Edit")
    check("(a) the dialog is open, about the Edit",
          _ask126().get("call"), "toolu_126edit")
    check("(a) PRECONDITION: the call has no tracked record",
          [m for m in daemon.inflight_live(_p126)], [])
    with open(_TR126, "a", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"type": "user", "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "message": {
            "role": "user", "content": [{"tool_use_id": "toolu_126edit",
                                         "type": "tool_result",
                                         "content": _REJECT126,
                                         "is_error": True}]}},
                             separators=(",", ":")) + "\n")
    daemon.check_processes()
    check("(a) the dialog is over", _ask126(), {})
    _l126 = _lines126()
    check("(a) in one line, which names the refusal as its witness",
          (len(_l126), bool(_l126) and "refused: The user doesn't want to "
           "proceed" in _l126[-1] and "no Post event" in _l126[-1]),
          (1, True))

    print("    (b) CONTROL: a refused Edit, then a NEWER call that asks - the")
    print("    dialog is about the newer one, and it stays")
    _edit126("toolu_126b", result=_REJECT126)
    _edit126("toolu_126c", tool="Write")
    _ev126("Notification", notification_type="permission_prompt",
           message="Claude needs your permission to use Write")
    check("(b) the dialog is about the newer call",
          _ask126().get("call"), "toolu_126c")
    daemon.check_processes()
    check("(b) and it stays open - its own call has no result",
          _ask126().get("call"), "toolu_126c")
    check("(b) no second 'answered' line", len(_lines126()), 1)
finally:
    daemon.sessions.transcript_of = _tof126o
    post("/config", {"thresholds": dict(
        daemon.CFG["thresholds"],
        call_end_grace=_grace126 if _grace126 is not None else 30)})
    with daemon._lock:
        (daemon.STATE.get("asks") or {}).pop("%s|executor" % _k126, None)
        daemon.save_state()
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n127. an old window left mid-turn for more than an hour asks a person -")
print("    once - and is still closed only when its turn ends")
print("    The owner's word of 2026-09-28, to 'a leftover stuck mid-turn for")
print("    more than an hour: call a person?' - yes. Until then the whole of")
print("    it was one line, 'is mid-turn', for as long as the turn lasted.")
print("    Real order: the handover's leftover deferred mid-turn, the tick")
print("    under the hour, the booking an hour back, the tick, the tick")
print("    again, the turn's end, the tick. -> DECISIONS.md 8.44")
_p127 = os.path.join(TMP, "leftover-stuck")
os.makedirs(_p127, exist_ok=True)
_k127 = canon(_p127)
post("/config", {"projects": {A: {}, B: {}, C: {}, _p127: {}}})
_pids127 = dict(daemon.STATE.get("pids") or {})
_started127 = daemon.STATE.get("started_at")
_tof127o = daemon.sessions.transcript_of
daemon.sessions.transcript_of = (
    lambda sid, cwd=None: _TR22.get(sid) if sid in _TR22 else None)
_scr127o = daemon.sessions.console_screen
_SCREEN127 = ("the old window's last lines" + chr(10)
              + "* Perusing... (61m 12s - esc to interrupt)" + chr(10))
daemon.sessions.console_screen = lambda pid, timeout=20: _SCREEN127
_wins127 = []
# The call itself, as the bridge makes it - kind, text and the pair it is
# about - recorded on its way through, then handed on unchanged. Whether a
# chat is configured is another case's business.
_NOTES127 = []
_notify127o = daemon.notify


def _notify127(kind, text, *a, **k):
    _NOTES127.append((kind, text, k.get("path")))
    return _notify127o(kind, text, *a, **k)


daemon.notify = _notify127


def _tick127():
    with daemon._lock:
        daemon.STATE["pids"] = {k: v for k, v in
                                (daemon.STATE.get("pids") or {}).items()
                                if k.startswith(_k127)}
        daemon.STATE["started_at"] = time.time() - 3600
        daemon.save_state()
    daemon.check_sessions(float(daemon.CFG["thresholds"].get(
        "startup_grace", 600)))


def _calls127():
    return [t for (kd, t, pth) in _NOTES127 if kd == "needs_you"
            and pth and canon(pth) == _k127
            and "in the middle of a turn since" in t]


try:
    _WA127 = _open22(_p127, "planner")
    _wins127.append(_WA127)
    _ev22(_p127, "planner", "s127-a", "SessionStart", _WA127)
    _chan22(_p127, "planner", _WA127, "a")
    _WB127 = _open22(_p127, "planner")
    _wins127.append(_WB127)
    _ev22(_p127, "planner", "s127-b", "SessionStart", _WB127)
    _chan22(_p127, "planner", _WB127, "b")
    print("    the first window's channel is refused - it is a leftover now,")
    print("    and its own session is in the middle of a turn")
    _chan22(_p127, "planner", _WA127, "a2")
    check("it is a leftover",
          _WA127 in daemon.leftover_windows(_p127, "planner", _WB127), True)
    _tw22("s127-a", "system", 120, "turn_duration")
    _tw22("s127-a", "user", 60, content="a report the old window still writes")
    daemon.close_leftovers(_p127, "planner", _WB127)
    check("deferred mid-turn, and not closed",
          (any(str(_WA127) in _t for _t in _j22("is mid-turn", _p127)),
           sessions.pid_alive(_WA127)), (True, True))
    print("    CONTROL: a tick under the hour is silent")
    _tick127()
    check("no call under the hour", _calls127(), [])
    print("    the booking, an hour and a minute ago")
    with daemon._lock:
        ((daemon.STATE.get("leftovers") or {}).get("%s|planner" % _k127)
         or {}).get(str(_WA127), {})["at"] = time.time() - 3660
        daemon.save_state()
    _tick127()
    _c127 = until(lambda: _calls127(), 10) and _calls127()
    check("exactly one call", len(_c127 or []), 1)
    _t127 = (_c127 or [""])[0]
    check("it names the window, its session, since when, its last entry and "
          "its screen",
          [str(_WA127) in _t127, "s127-a" in _t127, "61 min" in _t127,
           "a report the old window still writes" in _t127,
           "Perusing... (61m 12s" in _t127], [True] * 5)
    check("and the journal says the same, once",
          len(_j22("in the middle of a turn since", _p127)), 1)
    _tick127()
    check("a second tick does not ring again", len(_calls127()), 1)
    check("and the window is still not closed", sessions.pid_alive(_WA127),
          True)
    print("    its turn ends; the next tick closes it, one line")
    _tw22("s127-a", "system", 0, "turn_duration")
    _tick127()
    check("closed at the end of its turn",
          until(lambda: not sessions.pid_alive(_WA127), 60), True)
    check("and the line says so",
          sum(1 for _t in _j22("old planner window closed", _p127)
              if str(_WA127) in _t), 1)
finally:
    daemon.notify = _notify127o
    daemon.sessions.transcript_of = _tof127o
    daemon.sessions.console_screen = _scr127o
    with daemon._lock:
        daemon.STATE["pids"] = _pids127
        if _started127 is not None:
            daemon.STATE["started_at"] = _started127
        (daemon.STATE.get("handover") or {}).pop(_k127, None)
        (daemon.STATE.get("leftovers") or {}).pop("%s|planner" % _k127, None)
        daemon.save_state()
    for _w in _wins127:
        if _w:
            sessions.terminate_and_wait(_w)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


print("\n128. a window record vouches only for the process it was written about")
print("    2026-09-28 04:09:28, case 52 of this suite: a start refused with 'a")
print("    planner window is already up: pid 15200' fifteen seconds after case")
print("    10 had stopped and reaped pid 15200. The number was another")
print("    process's by then, and stop() leaves the record where it is - so")
print("    the same number handed to stop() would have been taskkill /T /F")
print("    on that process. The reuse is FORCED here, not hoped for: the")
print("    record names a live process of this case's own, born an hour after")
print("    the record says it was written. Real endpoints, real order.")
print("    -> DECISIONS.md 8.46")
_p128 = os.path.join(TMP, "pid-reuse")
os.makedirs(_p128, exist_ok=True)
_k128 = canon(_p128)
_key128 = "%s|planner" % _k128
post("/config", {"projects": {A: {}, B: {}, C: {}, _p128: {}}})
_pids128 = dict(daemon.STATE.get("pids") or {})
_kids128 = []
# absent on the code this repairs: say so, do not crash the suite
_ra128 = getattr(daemon, "record_alive", None)
_lp128 = getattr(daemon, "live_pid_of", None)


def _stranger128():
    # a process of this case's own - the only kind a record here may name
    _p = subprocess.Popen([sys.executable, "-c",
                           "import time; time.sleep(120)"])
    _kids128.append(_p)
    return _p


def _record128(pid, at, **more):
    _r = {"pid": pid, "at": at, "registered": True,
          "registered_via": "session", "why": "you pressed start in the panel"}
    _r.update(more)
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})[_key128] = _r
        daemon.save_state()
    return _r


def _mine128():
    return [r for r in launches() if canon(r.get("cwd") or "") == _k128]


def _j128(sub):
    _f = os.path.join(_p128, "bridge-logs", time.strftime("%Y-%m-%d"),
                      "events.jsonl")
    try:
        with open(_f, encoding="utf-8") as _fh:
            return [t for t in (json.loads(l).get("text") or ""
                                for l in _fh if l.strip())
                    if sub in t]
    except (OSError, ValueError):
        return []


try:
    print("   (a) the one definition, asked directly")
    _s128 = _stranger128()
    check("record_alive and live_pid_of exist", (callable(_ra128),
                                                 callable(_lp128)),
          (True, True))
    _late128 = _record128(_s128.pid, time.time() - 3600)
    check("a live process born an hour after its record: not that window",
          (_ra128(_late128) if _ra128 else None,
           _lp128(_p128, "planner") if _lp128 else "absent"), (False, None))
    print("   CONTROL (rule 19): the same live process, recorded AFTER it was")
    print("   born, is the window - so the answer above is the birth time,")
    print("   not a probe that stopped seeing anything")
    _true128 = _record128(_s128.pid, time.time())
    check("recorded after its birth: it is that window",
          (_ra128(_true128) if _ra128 else None,
           _lp128(_p128, "planner") if _lp128 else "absent"),
          (True, _s128.pid))
    check("and a record the bridge's stop marked vouches for nothing, even "
          "over a live process",
          _ra128(dict(_true128, stopped_at=time.time())) if _ra128 else None,
          False)

    print("   (b) stop through the endpoint: the record names the stranger,")
    print("   and the bridge holds no window of its own for this half")
    daemon.sessions.PROCS.pop((_k128, "planner"), None)
    _record128(_s128.pid, time.time() - 3600)
    _st128 = post("/session", {"action": "stop", "project": _p128,
                               "role": "planner"})
    check("the stranger is still alive - nobody's taskkill reached it",
          _s128.poll() is None, True)
    check("the stop answers ok: the window on record was already gone",
          _st128.get("ok"), True)
    check("and the record now says it was stopped",
          bool(((daemon.STATE.get("pids") or {}).get(_key128) or {})
               .get("stopped_at")), True)

    print("   (c) the incident: start with the record naming a stranger of its")
    print("   own - (b) on the old code has already killed the first one")
    _c128 = _stranger128()
    _record128(_c128.pid, time.time() - 3600)
    _n128 = len(_mine128())
    _r128 = post("/session", {"action": "launch", "project": _p128,
                              "role": "planner"})
    check("the start is not refused", (_r128.get("ok"),
                                      "already up" in str(_r128)),
          (True, False))
    check("and a window comes up - the stranger is not taken for one",
          until(lambda: len(_mine128()) > _n128, 30), True)
    post("/session", {"action": "stop", "project": _p128, "role": "planner"})
    print("   CONTROL: a record written after its live process was born still")
    print("   refuses a second window - the guard is not switched off. The")
    print("   refusal is the endpoint's own answer, decided before it returns")
    _g128 = _stranger128()
    daemon.sessions.PROCS.pop((_k128, "planner"), None)
    _record128(_g128.pid, time.time())
    _r128b = post("/session", {"action": "launch", "project": _p128,
                               "role": "planner"})
    check("refused, naming the live window",
          (_r128b.get("ok"), "already up" in str(_r128b),
           str(_g128.pid) in str(_r128b)), (False, True, True))

    print("   (d) the window a handover replaces is stopped only while its own")
    print("   record still vouches for its number. stop_after is written at")
    print("   the launch and read at the replacement's SessionStart - up to")
    print("   startup_grace later - and stop_window asked only pid_alive, so a")
    print("   window that died in between left its number to taskkill. The")
    print("   census of 8.46 missed it: this is a copy of the record, not a")
    print("   read of STATE['pids']")
    _d128 = _stranger128()
    _od128 = {"pid": _d128.pid, "at": time.time() - 3600,
              "registered": True, "registered_via": "session"}
    with daemon._lock:
        daemon.STATE.setdefault("handover", {})[_k128] = {
            "roles": ["planner"], "at": time.time(),
            "stop_after": {"planner": _d128.pid},
            "old_recs": {"planner": _od128}}
        daemon.save_state()
    daemon.stop_the_replaced(_p128, "planner")
    check("(d) the stranger under the old record is still alive",
          _d128.poll() is None, True)
    check("(d) and the journal says the number was not handed on",
          any("not handed to taskkill" in t
              for t in _j128("not handed to taskkill")), True)
    print("   CONTROL: the old window's record written after its process was")
    print("   born - it IS that window, and it is stopped")
    _e128 = _stranger128()
    _oe128 = {"pid": _e128.pid, "at": time.time(),
              "registered": True, "registered_via": "session"}
    with daemon._lock:
        daemon.STATE.setdefault("handover", {})[_k128] = {
            "roles": ["planner"], "at": time.time(),
            "stop_after": {"planner": _e128.pid},
            "old_recs": {"planner": _oe128}}
        daemon.save_state()
    daemon.stop_the_replaced(_p128, "planner")
    check("(d) the window its record vouches for is stopped",
          until(lambda: _e128.poll() is not None, 30), True)
finally:
    with daemon._lock:
        daemon.STATE["pids"] = _pids128
        (daemon.STATE.get("handover") or {}).pop(_k128, None)
        daemon.save_state()
    for _kid in _kids128:
        sessions.terminate_and_wait(_kid.pid)
    post("/config", {"projects": {A: {}, B: {}, C: {}}})


for _si in STAND_INS:
    try:
        sessions.terminate_and_wait(_si.pid)
    except Exception:
        pass

SRV.shutdown()
SRV.server_close()

print("\n" + ("-" * 60))
owntemp.finish(TMP, bool(FAILED))
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
