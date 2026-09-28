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

"""Ten seeded runs of the whole recovery block, every situation in each.

The other suites take one mechanism at a time. This one asks the question
none of them can: do these ten things still work when they happen to the
same pair, in an order nobody chose, one after another? The owner asked for
it in those words - "run a full simulation of all these situations 10
times; until it is ten out of ten the work is not accepted".

WHAT IS SIMULATED, and each is a piece of the block:

  1  background      a run_in_background command, its record, the client's
                     own task-notification ending it
  2  wait_words      a `wait` verdict with work behind it hands its words
                     back; an acknowledgement with nothing running does not
  3  deaf_planner    two reports into a window that opens no turn -> the
                     pair is held, naming the cause; a turn lifts it
  4  wall_executor   five compactions -> the handoff is DEMANDED, the turn
                     is not cut, and the replacement comes up before the
                     old window is stopped
  5  wall_planner    the same wall for the planner, its own handoff file,
                     and a V2 seed with no /init
  6  never_came_up   the replacement never registers -> the old window is
                     not touched and the swap is undone
  7  crash_executor  a dead executor -> raised without asking, /init, the
                     planner asked for a handoff from the journal, and that
                     handoff held until the /init ends
  8  crash_planner   a dead planner -> raised without asking and seeded
                     with pointers rather than a handoff
  9  window_asks     a window sitting on a question at startup is answered
                     once; an unknown screen is quoted, not guessed at
 10  claim_gate      a planner turn asserting the state of the pair with an
                     empty witness registry does not close

NOTHING HERE SLEEPS TO WIN A RACE. Where an order matters it is forced -
an Event, a join, or waiting on the FACT through until() - because a suite
that passes on timing is a suite that will one day pass on nothing.

Run:  python test_recovery_sim.py            all ten seeds
      python test_recovery_sim.py 3          one seed, for a bisect
"""
import hashlib
import io
import json
import os
import random
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# first: it reads nothing from the package, and the folder it makes
# is the only one this run may remove (DECISIONS.md 8.43, 8.45)
from bridgecore import owntemp                 # noqa: E402
TMP = owntemp.make("bridge-recovery-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
os.environ["BRIDGE_CLAUDE_JSON"] = os.path.join(TMP, ".claude.json")
# and the user-level settings approve_channel merges into - never the
# real one (DECISIONS.md 8.35)
os.environ["BRIDGE_CLAUDE_SETTINGS"] = os.path.join(TMP, "user-settings.json")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-home")
os.environ["BRIDGE_NO_HOOKS"] = "1"
os.environ["PYTHONUTF8"] = "1"
os.environ["BRIDGE_WATCH_SEC"] = "1"
os.environ["BRIDGE_GRACE_SEC"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgecore import daemon, sessions, store, telegram          # noqa: E402

OUTDIR = os.environ.get("BRIDGE_SIM_OUT", "")

# ---------------------------------------------------------------------------
# the stand-ins

TG = []
telegram.send = lambda cfg, text, level="silent", buttons=None: TG.append(
    (level, text))
telegram.status_message = lambda cfg, text: cfg
telegram.pin_status = lambda cfg, text: cfg
telegram.pin_links = lambda cfg, text: cfg

BIN = os.path.join(TMP, "fakebin")
os.makedirs(BIN, exist_ok=True)
LAUNCHES = os.path.join(TMP, "launches.log")
STUB = os.path.join(BIN, "claude_stub.py")
with io.open(STUB, "w", encoding="utf-8") as fh:
    fh.write(
        u"import json, os, sys, time\n"
        u"row = {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        u"       'role': os.environ.get('BRIDGE_ROLE')}\n"
        u"open(%r, 'a', encoding='utf-8').write("
        u"json.dumps(row, ensure_ascii=False) + '\\n')\n"
        u"time.sleep(30)\n" % LAUNCHES)

_real_build = sessions.build_command


def _stub_build(*a, **kw):
    """The real command line with only the executable swapped."""
    return [sys.executable, STUB] + _real_build(*a, **kw)[1:]


sessions.build_command = _stub_build
sessions.CREATE_NEW_CONSOLE = 0

# Windows are never really stopped here: the pids are the stub's, and what
# every situation asks about is WHETHER and WHEN a stop was ordered, not
# whether a process died. Recorded, not performed - and the recorder is
# read by the situations that care about the order.
STOPS = []
_real_stop = sessions.stop


def _watched_stop(project, role, pid=None, wait=None, tree=True):
    STOPS.append({"project": daemon.norm(project), "role": role, "pid": pid,
                  "at": time.time()})
    _STOPPED.add(int(pid or 0))
    return True


# WHICH WINDOWS ARE ALIVE is the situation's to say, and a window is alive
# until it is stopped. Everything answered dead until 2026-09-26, and the
# situations that assert "the old window is still working" or "ONLY THEN is
# the old window stopped" were telling the daemon it was dead. Since 8.36 a
# handover names the window it replaces - a dead record and a seat whose
# window is dead is a half with nothing to stop - so the fixture has to say
# what the situation asserts. -> DECISIONS.md 8.36
_LIVE_WIN = set()
_STOPPED = set()
sessions.stop = _watched_stop
sessions.pid_alive = (lambda pid: int(pid or 0) in _LIVE_WIN
                      and int(pid or 0) not in _STOPPED)


def launches():
    if not os.path.exists(LAUNCHES):
        return []
    with io.open(LAUNCHES, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


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
    who = (daemon.norm(project), role)
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
                     "session_roles": {}, "started_at": time.time() - 9999})
daemon.CFG["projects"] = {}
daemon.CFG["telegram"] = {"token": "", "chat_id": "", "pinned_message_id": 0}
# Every threshold the block reads, small enough that ten seeds fit in a
# coffee break. They are CONFIG, read at the moment of the decision, which
# is exactly why they can be moved here instead of waiting them out.
daemon.CFG.setdefault("thresholds", {}).update({
    "review_timeout": 8, "channel_silence_warn": 2, "idle_hold": 0,
    "startup_grace": 2, "handover_grace": 2, "stall_grace": 1,
    "undelivered_hold": 2, "restart_settle": 0,
})

SRV = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()

LOG = []


def say(line=""):
    LOG.append(line)
    print(line)
    sys.stdout.flush()


def post(path, payload, secret=False, timeout=30):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Bridge-Secret"] = daemon.SECRET
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RuntimeError("the suite's daemon on %d did not answer (%s)"
                           % (PORT, exc))


def hook(project, name, role, sid, **extra):
    # window_pid as hook.py sends it - its parent, the window (15.1); the
    # channels here register with the same parent, so a half's session and
    # its seat are linked the way a real window's are.
    ev = {"hook_event_name": name, "role": role, "session_id": sid,
          "project_dir": project, "cwd": project, "window_pid": os.getppid()}
    ev.update(extra)
    return post("/event", ev)


def statusline(project, role, sid, tokens, window=1000000):
    """The shape statusline.py actually posts - flat keys are ignored.

    The first draft here invented a payload and every wall check answered
    "unknown", because plan_for had no telemetry to read. Copied from the
    suite that already drives this endpoint rather than guessed at again.
    """
    return post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": project, "project_dir": project},
        "model": {"display_name": "Opus 5 (1M context)",
                  "id": "claude-opus-5"},
        "context_window": {
            "context_window_size": window,
            "used_percentage": round(tokens * 100.0 / window, 1),
            "current_usage": {"input_tokens": 10,
                              "cache_creation_input_tokens": 90,
                              "cache_read_input_tokens": tokens - 100,
                              "output_tokens": 4000}}}})


def until(fn, seconds=12.0):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    try:
        return bool(fn())
    except Exception:
        return False


def journal(project, needle):
    f = os.path.join(project, "bridge-logs", time.strftime("%Y-%m-%d"),
                     "events.jsonl")
    if not os.path.isfile(f):
        return []
    out = []
    with io.open(f, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if needle in (row.get("text") or ""):
                row["n"] = i
                out.append(row)
    return out


def body_of(content):
    text = content or ""
    if "End of the rules" not in text:
        return text
    return text.split("End of the rules", 1)[1].split("=" * 70, 1)[-1]


# ---------------------------------------------------------------------------
# the scenario, which is what the seed decides

SITUATIONS = ["background", "wait_words", "deaf_planner", "wall_executor",
              "wall_planner", "never_came_up", "crash_executor",
              "crash_planner", "window_asks", "claim_gate"]


def scenario(seed):
    """One seed, one order and one set of sizes. Same seed, same run."""
    rnd = random.Random(seed)
    steps = SITUATIONS[:]
    rnd.shuffle(steps)
    return {
        "order": steps,
        # Where the executor stands when the wall is reached. Above the
        # point and below the window, which is the only shape that matters.
        "floor": rnd.choice([600000, 700000, 800000, 900000]),
        # How long a background command claims to have run. Above
        # stuck_limit's floor and below INFLIGHT_MAX_SEC, both read from
        # the code rather than written out here.
        "bg_secs": rnd.choice([400, 900, 1800, 3000]),
        # Which command shape. The noisy ones are tracked because the PATH
        # contains a watched word, which is the real over-match the stuck
        # watch feeds on.
        "cmd": rnd.choice(["py -m tool --long", "make -j4",
                           "pytest -q tests", "npm test"]),
        "gap": rnd.choice([0.0, 0.05, 0.15]),
    }


def fingerprint(plan):
    raw = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# one situation at a time. Each takes the run's context, drives the real
# endpoints, and records its own checks; each leaves the pair usable by the
# next one, whatever order the seed put them in.


class Ctx(object):
    def __init__(self, seed, plan):
        self.seed = seed
        self.plan = plan
        self.proj = os.path.join(TMP, "proj%02d" % seed)
        os.makedirs(self.proj, exist_ok=True)
        self.key = daemon.norm(self.proj)
        self.sid = {"executor": "ex%02d" % seed, "planner": "pl%02d" % seed}
        self.port = {}
        self.n = 0
        self.results = []          # (situation, [failed check names])
        self._fails = []

    def chk(self, name, got, want):
        if got != want:
            self._fails.append("%s (got %r, want %r)" % (name, got, want))
        return got == want

    def start(self):
        self._fails = []

    def finish(self, sit):
        self.results.append((sit, list(self._fails)))
        return not self._fails

    def register(self, role, sid=None):
        """A window's channel comes up, through the real endpoint."""
        sid = sid or self.sid[role]
        self.port[role] = open_channel(self.proj, role)
        post("/channel/register",
             {"project": self.proj, "role": role, "port": self.port[role],
              "pid": os.getpid(), "ppid": os.getppid(), "session_id": sid},
             secret=True)

    def inbox(self, role):
        return DELIVERED.setdefault((self.key, role), [])

    def clear(self, role):
        DELIVERED[(self.key, role)] = []


def finish_turn(ctx, role, msg, verdict="done"):
    """A turn ends, and the verdict comes back - the loop's own shape.

    The Stop hook BLOCKS until a verdict answers it, so it runs on a thread
    and the verdict is posted from here exactly as the planner would. It is
    what records a floor after a compaction, and what decides a handover,
    so the simulation goes through it rather than around it.
    """
    out = {}

    def run():
        out.update(hook(ctx.proj, "Stop", role, ctx.sid[role],
                        last_assistant_message=msg) or {})

    t = threading.Thread(target=run, name="sim:turn", daemon=True)
    t.start()
    if role == "executor" and daemon.loop_state(ctx.proj)[1].get("active"):
        end = time.time() + 20
        while time.time() < end:
            if daemon.PENDING.get(ctx.key):
                post("/verdict", {"project": ctx.proj, "verdict": verdict,
                                  "feedback": OKFB}, secret=True)
                break
            if not t.is_alive():
                break
            time.sleep(0.02)
    t.join(60)
    return out


OKFB = ("Checked: %s\nResidence: bridgecore/daemon.py:handle_event\n"
        "Accepted." % os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "test_recovery_sim.py"))


_TRANSCRIPT_OF0 = sessions.transcript_of


def transcript_for(ctx, role, rows):
    """Write a transcript for this half and point the bridge at it.

    The bridge reads a transcript through sessions.transcript_of, so that
    is what is redirected - not a STATE field - which keeps every reader
    (bg_finished, role_wrote_recently, transcript_frozen) on the same file
    the real ones would use.
    """
    p = os.path.join(TMP, "tr-%s-%s.jsonl" % (role, ctx.sid[role]))
    with io.open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    _prev = sessions.transcript_of

    def _of(sid, _p=p, _role=role, _ctx=ctx, _prev=_prev):
        return _p if sid == _ctx.sid[_role] else _prev(sid)

    sessions.transcript_of = _of
    return p


def a_turn(role, text, when=None):
    """One assistant row of a transcript, stamped now unless told otherwise.

    THE STAMP CARRIES MILLISECONDS, because the client's does
    (`2026-09-04T21:26:42.191Z`) and because the readers compare it with a
    real time.time(). Written to the whole second, a row put down
    microseconds after the record it must be newer than reads as OLDER
    than it: deaf_hold_over wants `wrote > since` where `since` is the
    float that note_deaf_planner just wrote, and a truncated stamp can
    never be greater. That is what held sit_deaf_planner at 0 of 10 - two
    sides of one comparison at different precisions, the same defect
    _entry_epoch carried until 2026-09-05. -> DECISIONS.md 8.10 K
    """
    t = when or time.time()
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text",
                                                          "text": text}]},
            "timestamp": "%s.%03dZ" % (time.strftime("%Y-%m-%dT%H:%M:%S",
                                                     time.gmtime(t)),
                                       int((t % 1) * 1000))}


def reset(ctx):
    """Leave the pair able to take the next situation, whatever ran before.

    EVERY LINE HERE IS EITHER A REAL ENDPOINT OR A RECORD THIS SUITE
    ITSELF WROTE. The loop and the pause go through /loop and /pause
    because that is how a person clears them; the four records popped
    below are ones a situation leaves behind by design - a handover that
    was never completed, a demand that was never met - and in production
    each has its own expiry, which is a clock this suite is not paying.
    """
    post("/loop", {"project": ctx.proj, "action": "start"}, secret=True)
    if daemon.paused_for(ctx.proj):
        # /cmd with cmd=resume, which is the panel's own button. The first
        # draft posted to "/pause" with an action, an endpoint that does
        # not exist - so a project paused by an earlier situation (a dead
        # executor pauses its pair) stayed paused, and every Stop after it
        # was held. That one wrong path was most of the first run's
        # failures.
        post("/cmd", {"project": ctx.proj, "cmd": "resume"}, secret=True)
    ctx.chk("the pair is not left paused for the next situation",
            bool(daemon.paused_for(ctx.proj)), False)
    with daemon._lock:
        for c in ("handover", "seed", "planner_seed", "init_pending",
                  "after_init", "idle_holding"):
            (daemon.STATE.get(c) or {}).pop(ctx.key, None)
        # `deaf` is pair-keyed in STATE_PATHS and was in neither list:
        # sit_deaf_planner popped it under the PATH key, so the record
        # sat under "<path>|planner" and outlived the situation. Found
        # by dumping every container still holding this pair at the
        # start of the next one, not by reading the list.
        for c in ("handover_pending", "handover_said", "claim_witness",
                  "deaf"):
            for r in ("executor", "planner"):
                (daemon.STATE.get(c) or {}).pop("%s|%s" % (ctx.key, r), None)
        # AND THE PAIR HAS TWO WINDOWS ON RECORD, which is what a pair
        # launched by the bridge always has and what several situations
        # need: handover replaces a window it can name. Nothing in this
        # suite wrote one - `register` brings up a CHANNEL, and `pids` is
        # written by reg_pid at a launch - so wall_executor passed only in
        # the seeds where never_came_up happened to run before it and
        # window_asks did not run after (it pops the record in its own
        # finally). Measured across six seeds: `pids` present in both that
        # passed, absent in all four that failed.
        # Marked `registered`, because launch_guard refuses only an
        # UNregistered entry - which is the state never_came_up builds for
        # itself.
        # A WINDOW PID OF ITS OWN, not this process's: a record naming the
        # suite itself is one real stop away from ending the run.
        _LIVE_WIN.clear()
        _STOPPED.clear()
        for r in ("executor", "planner"):
            daemon.STATE.setdefault("pids", {})["%s|%s" % (ctx.key, r)] = {
                "pid": win_pid(ctx, r), "at": time.time(), "registered": True,
                "registered_via": "session"}
        daemon.STATE.pop("acted:planner_handover:%s" % ctx.key, None)
        daemon.STATE.pop("hoheld:%s" % ctx.key, None)
        daemon.save_state()
        # AND NO SITUATION INHERITS ANOTHER'S TRANSCRIPT. transcript_for
        # redirects sessions.transcript_of and never puts it back, so a
        # fixture written by one situation went on answering for the pair
        # in every situation after it. Measured: wall_executor failed in
        # exactly the five seeds where deaf_planner ran before it, and in
        # those the planner's transcript was that situation's leftover
        # file - planner_took_report read it as (0.0, readable=True),
        # which is "the window opened no turn and we can see that", so the
        # deaf branch held the pair, and the handover branch sits behind
        # `if paused_for(path)`. With no fixture the same call answers
        # (0, False) - "could not be read" - and holds nothing.
        sessions.transcript_of = _TRANSCRIPT_OF0
    daemon.PENDING.pop(ctx.key, None)


# --------------------------------------------------------------- 1
def sit_background(ctx):
    """A run_in_background command is not over when its call returns."""
    cmd = ctx.plan["cmd"]
    sig = cmd.split()[0][:40]
    hook(ctx.proj, "PreToolUse", "executor", ctx.sid["executor"],
         tool_name="Bash",
         tool_input={"command": cmd, "run_in_background": True})
    live = daemon.inflight_live(ctx.proj) or []
    ctx.chk("the background call is tracked", bool(live), True)
    ctx.chk("and the pair reads as busy",
            bool(daemon.tool_in_flight(ctx.proj, "executor",
                                       daemon.situation(ctx.proj))), True)
    # ITS OWN PostToolUse MUST NOT END IT. run_in_background hands the tool
    # back at once; the record is keyed "bg:" and this pop cannot reach it.
    hook(ctx.proj, "PostToolUse", "executor", ctx.sid["executor"],
         tool_name="Bash",
         tool_input={"command": cmd, "run_in_background": True})
    ctx.chk("its own PostToolUse does not end it",
            bool(daemon.inflight_live(ctx.proj)), True)
    # The client's own notice, which is the witness the launch cannot make.
    rec = None
    for _sig, meta in (daemon.PROCTRACK.get(ctx.key) or {}).items():
        if isinstance(meta, dict) and meta.get("bg"):
            rec = meta
            break
    ctx.chk("the record says it is a background one", bool(rec), True)
    tid = "toolu_sim%02d" % ctx.seed
    transcript_for(ctx, "executor", [
        a_turn("executor", "starting it"),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Bash",
             "input": {"command": cmd, "run_in_background": True}}]}},
        {"type": "queue-operation", "operation": "enqueue",
         "content": "<task-notification>%s <status>completed</status> "
                    "(exit code 0)</task-notification>" % tid},
    ])
    if rec is not None:
        rec["tpos"] = 0
    daemon.check_background()
    ctx.chk("the client's notice ends it",
            until(lambda: not daemon.inflight_live(ctx.proj)), True)
    ctx.chk("and the pair is free again",
            bool(daemon.tool_in_flight(ctx.proj, "executor",
                                       daemon.situation(ctx.proj))), False)


# --------------------------------------------------------------- 2
def sit_wait_words(ctx):
    """`wait` hands its words back only while something is running."""
    reset(ctx)
    cmd = ctx.plan["cmd"]
    hook(ctx.proj, "PreToolUse", "executor", ctx.sid["executor"],
         tool_name="Bash",
         tool_input={"command": cmd, "run_in_background": True})
    words = ("Hold on - the long run you started is still going, and the "
             "next piece depends on what it prints, so wait for it rather "
             "than starting anything new. " * 3)
    out = {}

    def turn():
        out["r"] = hook(ctx.proj, "Stop", "executor", ctx.sid["executor"],
                        last_assistant_message="the piece is done")

    t = threading.Thread(target=turn, name="sim:wait-turn", daemon=True)
    t.start()
    ok = until(lambda: bool(daemon.PENDING.get(ctx.key)), 10)
    ctx.chk("the report is waiting for a verdict", ok, True)
    post("/verdict", {"project": ctx.proj, "verdict": "wait",
                      "feedback": words}, secret=True)
    t.join(20)
    ctx.chk("the turn came back", t.is_alive(), False)
    got = json.dumps((out.get("r") or {}), ensure_ascii=False)
    ctx.chk("the words rode back on the hook, with work still running",
            "Hold on" in got, True)
    # ...and the acknowledgement, with nothing running, does not.
    for s, m in list((daemon.PROCTRACK.get(ctx.key) or {}).items()):
        daemon.PROCTRACK[ctx.key].pop(s, None)
    with daemon._lock:
        (daemon.STATE.get("inflight") or {}).pop(ctx.key, None)
        daemon.save_state()
    out2 = {}

    def turn2():
        out2["r"] = hook(ctx.proj, "Stop", "executor", ctx.sid["executor"],
                         last_assistant_message="second piece done")

    t2 = threading.Thread(target=turn2, name="sim:wait-turn2", daemon=True)
    t2.start()
    until(lambda: bool(daemon.PENDING.get(ctx.key)), 10)
    post("/verdict", {"project": ctx.proj, "verdict": "wait",
                      "feedback": "ok, noted"}, secret=True)
    t2.join(20)
    ctx.chk("a bare acknowledgement carries nothing back",
            "ok, noted" in json.dumps(out2.get("r") or {},
                                      ensure_ascii=False), False)


# --------------------------------------------------------------- 3
def sit_deaf_planner(ctx):
    """A window that takes a report and opens no turn holds the pair."""
    reset(ctx)
    transcript_for(ctx, "planner",
                   [a_turn("planner", "the last thing I said",
                           time.time() - 3600)])
    with daemon._lock:
        daemon.STATE.setdefault("deaf", {}).pop(
            "%s|planner" % ctx.key, None)
        daemon.save_state()
    took, readable = daemon.planner_took_report(ctx.proj, time.time() - 10)
    ctx.chk("the planner's own transcript says it opened no turn",
            (took, readable), (False, True))
    held = None
    for i in range(daemon.DEAF_REPORTS_BEFORE_HOLD):
        held = daemon.note_deaf_planner(ctx.proj, "sim", i + 1, True)
    ctx.chk("at the second report the pair is held", bool(held), True)
    ctx.chk("and the hold names the cause, not just silence",
            (daemon.STATE.get("paused") or {}).get(ctx.key, {}).get("by"),
            "deaf")
    # A turn in that window lifts it - through the real Stop hook.
    transcript_for(ctx, "planner", [a_turn("planner", "I am here")])
    # ASKED EXACTLY ONCE, because deaf_hold_over LIFTS the hold as it
    # answers: inside it are clear_deaf and resume_project. The old form
    # asked it inside an `until` whose lambda read
    # `not deaf_hold_over(...) or True` - always True, so the wait ended on
    # its first look having already spent the lift, and the assertion after
    # it found nothing left to lift. 0 of 10, every seed, on a mechanism
    # that works: the check consumed the thing it then asserted. The same
    # family as case 74 in test_multipair, and the reason the millisecond
    # stamp above was necessary but not sufficient.
    ctx.chk("a turn in the window clears the hold",
            bool(daemon.deaf_hold_over(ctx.proj)), True)
    ctx.chk("and lifting it resumes the pair, which is the whole point",
            bool(daemon.paused_for(ctx.proj)), False)


# --------------------------------------------------------------- 4
# WHAT A COMPACTION ACTUALLY DOES TO THE NUMBER, measured rather than
# invented. Over the 37 compactions this bridge holds a floor for, the
# size afterwards is 6 % to 43 % of what was carried before it - median
# 19 % - and the smallest drop of the lot is 57 %. This fixture used to
# go floor+60000 -> floor, a drop of 6 to 9 %, which is a compaction no
# client has ever performed: stale_after_compaction (daemon.py:3333)
# lifts its hold only for a reading under 90 % of the old size, so the
# hold never lifted, plan_for stayed on rule 0 - rightly - and every wall
# check below failed on the fixture's own arithmetic rather than on the
# code it was testing. -> DECISIONS.md 8.9, and the X3 run's own
# artefact '03-real-order-and-size' in the test-results folder of that
# day (the path is not written here: this file ships, and a dated log
# path in a published file is what check_public calls a leftover)
SUMMARY_FRACTION = 0.19


def win_pid(ctx, role):
    """The window a situation's half runs in - invented, and odd, so never
    a real Windows pid (those are multiples of four)."""
    return 700001 + ctx.seed * 10 + (0 if role == "executor" else 2)


def sit_wall_executor(ctx):
    """Five compactions: the handoff is demanded, and only then replaced."""
    reset(ctx)
    _LIVE_WIN.add(win_pid(ctx, "executor"))    # alive until it is stopped
    ctx.clear("planner")
    # the size carried INTO each compaction, and what it leaves
    carried = ctx.plan["floor"]
    settled = int(carried * SUMMARY_FRACTION)
    # THE FLOOR IS RECORDED AT THE STOP AFTER A COMPACTION, so each cycle
    # here is a real one: the size climbs, the compaction fires, the
    # summary lands as a smaller reading, and the turn ends through the
    # blocking hook with a verdict answering it.
    for i in range(5):
        statusline(ctx.proj, "executor", ctx.sid["executor"], carried)
        hook(ctx.proj, "PreCompact", "executor", ctx.sid["executor"],
             transcript_path="")
        statusline(ctx.proj, "executor", ctx.sid["executor"], settled)
        if i < 4:
            finish_turn(ctx, "executor", "cycle %d done" % (i + 1))
            statusline(ctx.proj, "executor", ctx.sid["executor"], carried)
    statusline(ctx.proj, "executor", ctx.sid["executor"], settled)
    ctx.chk("five compactions are on record",
            daemon.compactions_done(ctx.proj, "executor"), 5)
    sess = daemon.best_session(ctx.proj, "executor") or {}
    ctx.chk("and the wall is what the plan says",
            daemon.plan_for(sess, ctx.proj).get("do"), "handover")
    before = len(launches())
    out = hook(ctx.proj, "Stop", "executor", ctx.sid["executor"],
               last_assistant_message="a turn ends at the wall")
    ho = out.get("hook_output") or {}
    ctx.chk("the turn is NOT cut", ho.get("continue"), None)
    ctx.chk("and the demand rides back on the hook",
            "HANDOFF" in json.dumps(ho, ensure_ascii=False).upper(), True)
    ctx.chk("nothing was opened yet", len(launches()) - before, 0)
    pend = daemon.handover_pending_for(ctx.proj, "executor")
    ctx.chk("a demand is on record", bool(pend), True)
    # A turn that says nothing about it changes nothing.
    hook(ctx.proj, "Stop", "executor", ctx.sid["executor"],
         last_assistant_message="another turn, no handoff")
    ctx.chk("and a turn without the file replaces nothing",
            len(launches()) - before, 0)
    # Now the file, and the report that names it.
    f = pend.get("file") or os.path.join(ctx.proj, "handoff.md")
    try:
        os.makedirs(os.path.dirname(f), exist_ok=True)
    except OSError:
        pass
    io.open(f, "w", encoding="utf-8").write(
        u"# the thread\n\nwhat the next session needs, seed %d\n" % ctx.seed)
    stops_before = len(STOPS)
    hook(ctx.proj, "Stop", "executor", ctx.sid["executor"],
         last_assistant_message="%s %s\n\nthe cycle is spent"
         % (daemon.HANDOFF_MARK, f))
    ctx.chk("a window is opened for the replacement",
            until(lambda: len(launches()) > before), True)
    ctx.chk("and the old one is NOT stopped to make room",
            [s for s in STOPS[stops_before:] if s["project"] == ctx.key], [])
    ctx.chk("the journal says which way round",
            bool(journal(ctx.proj, "BEFORE stopping the old one")), True)
    ctx.chk("the seed carries the session's own handoff",
            ((daemon.STATE.get("seed") or {}).get(ctx.key) or {})
            .get("own_handoff"), f)
    # the two-window minute
    ctx.chk("deliveries are refused while both are alive",
            daemon.deliver_ex(ctx.proj, "executor", "into the swap",
                              {"kind": "task"}), (False, "absent"))
    ctx.chk("and the old channel may not vouch for the new window",
            daemon.handover_awaits(ctx.proj, "executor"), True)
    # the replacement reports for duty
    new = ctx.sid["executor"] + "b"
    hook(ctx.proj, "SessionStart", "executor", new, transcript_path="")
    ctx.sid["executor"] = new
    ctx.register("executor", new)
    ctx.chk("ONLY THEN is the old window stopped",
            until(lambda: [s for s in STOPS[stops_before:]
                           if s["project"] == ctx.key
                           and s["role"] == "executor"] != []), True)
    ctx.chk("and the swap is over",
            until(lambda: not daemon.handover_swapping(ctx.proj,
                                                       "executor")), True)


# --------------------------------------------------------------- 5
def sit_wall_planner(ctx):
    """The same wall for the planner, and a V2 seed with no /init."""
    reset(ctx)
    # the size carried INTO each compaction, and what it leaves
    carried = ctx.plan["floor"]
    settled = int(carried * SUMMARY_FRACTION)
    for i in range(5):
        statusline(ctx.proj, "planner", ctx.sid["planner"], carried)
        hook(ctx.proj, "PreCompact", "planner", ctx.sid["planner"],
             transcript_path="")
        statusline(ctx.proj, "planner", ctx.sid["planner"], settled)
        if i < 4:
            # A planner's Stop does not block on a verdict - it is the one
            # that GIVES them - so it is fired straight.
            hook(ctx.proj, "Stop", "planner", ctx.sid["planner"],
                 last_assistant_message="review cycle %d" % (i + 1))
            statusline(ctx.proj, "planner", ctx.sid["planner"], carried)
    statusline(ctx.proj, "planner", ctx.sid["planner"], settled)
    ctx.chk("five compactions on the planner",
            daemon.compactions_done(ctx.proj, "planner"), 5)
    out = hook(ctx.proj, "Stop", "planner", ctx.sid["planner"],
               last_assistant_message="a review turn ends at the wall")
    ctx.chk("the planner is asked for its own handoff",
            "-planner.md" in json.dumps(out.get("hook_output") or {},
                                        ensure_ascii=False), True)
    ctx.chk("and assess waits for it rather than replacing",
            daemon.planner_wall_holds(ctx.proj), True)
    f = daemon.project_handoff_file(ctx.proj, "planner")[0]
    try:
        os.makedirs(os.path.dirname(f), exist_ok=True)
    except OSError:
        pass
    io.open(f, "w", encoding="utf-8").write(u"# the review thread\n")
    hook(ctx.proj, "Stop", "planner", ctx.sid["planner"],
         last_assistant_message="%s %s" % (daemon.HANDOFF_MARK, f))
    ctx.chk("the demand is spent once it is written",
            daemon.planner_wall_holds(ctx.proj), False)
    # ...and the seed the replacement is given
    with daemon._lock:
        daemon.STATE.setdefault("planner_seed", {})[ctx.key] = {
            "handoff": "table", "feedback": "", "iteration": 1,
            "reason": "it has compacted 5 times; a fresh session",
            "roles": ["planner"], "at": time.time()}
        daemon.save_state()
    new = ctx.sid["planner"] + "b"
    out = hook(ctx.proj, "SessionStart", "planner", new, transcript_path="")
    body = (((out.get("hook_output") or {}).get("hookSpecificOutput") or {})
            .get("additionalContext") or "")
    ctx.sid["planner"] = new
    ctx.register("planner", new)
    ctx.chk("the replacement is told not to run /init",
            "DO NOT RUN /init" in body, True)
    v2 = body.split("DO NOT RUN /init", 1)[-1]
    ctx.chk("and given the pointers to rebuild from",
            all(w in v2 for w in ("dialogue.md", "INDEX.md", "open tasks")),
            True)
    ctx.chk("and no false promise of a handoff it is not given",
            "with the handoff is what comes next" in v2, False)


# --------------------------------------------------------------- 6
def sit_never_came_up(ctx):
    """The replacement never registers: the old window is not touched."""
    reset(ctx)
    old_pid = 900000 + ctx.seed
    _LIVE_WIN.add(old_pid)            # "is still working", as asserted below
    with daemon._lock:
        daemon.STATE.setdefault("pids", {})["%s|executor" % ctx.key] = {
            "pid": old_pid, "at": time.time() - 3600, "registered": True,
            "registered_via": "session"}
        daemon.save_state()
    stops_before = len(STOPS)
    r = daemon.handover(ctx.proj, "the simulation asked", ("executor",))
    ctx.chk("the handover started", r.get("ok"), True)
    ctx.chk("and the old window was not stopped for it",
            [s for s in STOPS[stops_before:] if s["pid"] == old_pid], [])
    new_pid = ((daemon.STATE.get("pids") or {})
               .get("%s|executor" % ctx.key) or {}).get("pid")
    # A WINDOW THAT NEVER CAME UP IS SITTING ON ITS DIALOG - ALIVE, until
    # it is closed. It answered dead here, and its record was put 600 s
    # back over a process born a moment ago: a launch record older than
    # the process it names, which the one definition reads as a number
    # passed to somebody else (8.46). Declared alive; its own time stands,
    # and this simulation's startup_grace (2 s) passes for real.
    _LIVE_WIN.add(int(new_pid or 0))
    _at6 = float(((daemon.STATE.get("pids") or {})
                  .get("%s|executor" % ctx.key) or {}).get("at") or 0)
    grace = float(daemon.CFG["thresholds"].get("startup_grace", 600))
    until(lambda: time.time() - _at6 > grace + 0.05, grace + 5)
    daemon.check_sessions(0)
    ctx.chk("the newcomer that never came up is the one closed",
            [s["pid"] for s in STOPS[stops_before:]], [new_pid])
    ctx.chk("the old record is back, exactly as it was",
            ((daemon.STATE.get("pids") or {})
             .get("%s|executor" % ctx.key) or {}).get("pid"), old_pid)
    ctx.chk("the journal says the old one is still working",
            bool(journal(ctx.proj, "was never stopped and is still working")),
            True)
    n = len(journal(ctx.proj, "was never stopped and is still working"))
    daemon.check_sessions(0)
    daemon.check_sessions(0)
    ctx.chk("and says it once, not once a tick",
            len(journal(ctx.proj, "was never stopped and is still working")),
            n)


# --------------------------------------------------------------- 7
def sit_crash_executor(ctx):
    """A dead executor is raised without asking, and told from the log."""
    reset(ctx)
    ctx.clear("planner")
    ctx.clear("executor")
    before = len(launches())
    rec = daemon.best_session(ctx.proj, "executor") or {}
    daemon.handle_session_death(ctx.proj, "executor", rec)
    ctx.chk("a window was raised, without anybody being asked",
            until(lambda: len(launches()) > before), True)
    lr = (launches()[-1] if len(launches()) > before
          else {"argv": [], "role": ""})
    ctx.chk("with /init as its first act",
            daemon.INIT_PROMPT in (lr.get("argv") or []), True)
    ctx.chk("and no --resume", "--resume" in (lr.get("argv") or []), False)
    ctx.chk("the planner was asked for a handoff from the journal",
            until(lambda: any("WRITE IT A HANDOFF FROM THE JOURNAL"
                              in body_of(d.get("content") or "")
                              for d in ctx.inbox("planner"))), True)
    ctx.chk("the bridge is holding work for the init",
            bool(daemon.init_pending(ctx.proj)), True)
    ctx.clear("executor")
    post("/task", {"project": ctx.proj,
                   "instructions": "HANDOFF %d: you were on piece 5"
                                   % ctx.seed}, secret=True)
    ctx.chk("and the handoff is held, not delivered into the init",
            until(lambda: bool(ctx.inbox("executor")), 3), False)
    new = ctx.sid["executor"] + "c"
    hook(ctx.proj, "SessionStart", "executor", new, transcript_path="")
    ctx.sid["executor"] = new
    ctx.register("executor", new)
    hook(ctx.proj, "Stop", "executor", new,
         last_assistant_message="init done")
    ctx.chk("it arrives once the init has ended",
            until(lambda: any("HANDOFF %d" % ctx.seed
                              in (d.get("content") or "")
                              for d in ctx.inbox("executor")), 20), True)
    ctx.chk("and the journal says so",
            bool(journal(ctx.proj, "after its init")), True)


# --------------------------------------------------------------- 8
def sit_crash_planner(ctx):
    """A dead planner is raised without asking and seeded with pointers."""
    reset(ctx)
    before = len(launches())
    rec = daemon.best_session(ctx.proj, "planner") or {}
    daemon.handle_session_death(ctx.proj, "planner", rec)
    ctx.chk("a window was raised for it",
            until(lambda: len(launches()) > before), True)
    lr = (launches()[-1] if len(launches()) > before else {"argv": []})
    ctx.chk("and a planner is NOT sent to /init",
            daemon.INIT_PROMPT in (lr.get("argv") or []), False)
    with daemon._lock:
        daemon.STATE.setdefault("planner_seed", {})[ctx.key] = {
            "handoff": "table", "feedback": "", "iteration": 1,
            "reason": "it died", "roles": ["planner"], "at": time.time()}
        daemon.save_state()
    new = ctx.sid["planner"] + "c"
    out = hook(ctx.proj, "SessionStart", "planner", new, transcript_path="")
    body = (((out.get("hook_output") or {}).get("hookSpecificOutput") or {})
            .get("additionalContext") or "")
    ctx.sid["planner"] = new
    ctx.register("planner", new)
    ctx.chk("it is seeded with pointers, not a handoff",
            "DO NOT RUN /init" in body, True)
    v2 = body.split("DO NOT RUN /init", 1)[-1]
    ctx.chk("and told to check no task was lost",
            "not one task has been lost" in v2.lower(), True)


# --------------------------------------------------------------- 9
def sit_window_asks(ctx):
    """A window sitting on a question is answered once - and only that one."""
    reset(ctx)
    seen = {"screen": "", "answered": 0, "pids": []}
    pid = 950000 + ctx.seed
    _scr, _ans = sessions.console_screen, sessions.console_answer
    sessions.console_screen = (lambda p, timeout=20:
                               seen["screen"] if p == pid else "")

    def _answer(p, timeout=20):
        if p == pid:
            seen["answered"] += 1
            seen["pids"].append(p)
        return True

    sessions.console_answer = _answer
    try:
        seen["screen"] = ("  WARNING: Loading development channels\n\n"
                          "  Do you want to proceed?\n  > 1. Yes\n")
        with daemon._lock:
            daemon.STATE.setdefault("pids", {})["%s|executor" % ctx.key] = {
                "pid": pid, "at": time.time() - 300, "registered": False}
            daemon.save_state()
        daemon.check_sessions(0)
        ctx.chk("a window on a known question is answered", seen["answered"],
                1)
        daemon.check_sessions(0)
        ctx.chk("and only once, however many ticks go by",
                seen["answered"], 1)
        # An unknown screen is quoted, never guessed at.
        seen["screen"] = "  SOMETHING NOBODY HAS SEEN BEFORE %d" % ctx.seed
        seen["answered"] = 0
        with daemon._lock:
            daemon.STATE["pids"]["%s|executor" % ctx.key] = {
                "pid": pid, "at": time.time() - 300, "registered": False}
            daemon.save_state()
        daemon.check_sessions(0)
        ctx.chk("an unknown screen is not answered", seen["answered"], 0)
        ctx.chk("and goes into the journal verbatim",
                bool(journal(ctx.proj, "SOMETHING NOBODY HAS SEEN BEFORE")),
                True)
        # THE DANGEROUS ONE: a window that HAS come up is never keyed.
        seen["answered"] = 0
        with daemon._lock:
            daemon.STATE["pids"]["%s|executor" % ctx.key] = {
                "pid": pid, "at": time.time() - 300, "registered": True}
            daemon.save_state()
        daemon.check_sessions(0)
        ctx.chk("a window that came up is never sent a keystroke",
                seen["answered"], 0)
    finally:
        sessions.console_screen, sessions.console_answer = _scr, _ans
        with daemon._lock:
            (daemon.STATE.get("pids") or {}).pop("%s|executor" % ctx.key,
                                                 None)
            daemon.save_state()


# --------------------------------------------------------------- 10
def sit_claim_gate(ctx):
    """A claim about the pair with an empty registry does not close."""
    reset(ctx)
    marks = list(getattr(daemon, "CLAIM_MARKS", []) or [])
    frees = list(getattr(daemon, "CLAIM_NOT_CHECKED", []) or [])
    if not marks:
        ctx.chk("no marker list: the gate is off by design, and says so",
                daemon.claim_gate(ctx.proj, "planner", "anything")[0], True)
        return
    claim = "All good: %s. Nothing else." % marks[0]
    out = hook(ctx.proj, "Stop", "planner", ctx.sid["planner"],
               last_assistant_message=claim)
    ctx.chk("a claim with nothing opened does not close the turn",
            (out.get("hook_output") or {}).get("decision"), "block")
    hook(ctx.proj, "PreToolUse", "planner", ctx.sid["planner"],
         tool_name="Read", tool_input={"file_path": os.path.join(ctx.proj,
                                                                 "x.md")})
    out = hook(ctx.proj, "Stop", "planner", ctx.sid["planner"],
               last_assistant_message=claim)
    ctx.chk("the same words after opening the witness do close it",
            (out.get("hook_output") or {}).get("decision"), None)
    out = hook(ctx.proj, "Stop", "planner", ctx.sid["planner"],
               last_assistant_message="%s (%s)" % (claim, frees[0]))
    ctx.chk("and so does one marked unchecked",
            (out.get("hook_output") or {}).get("decision"), None)
    ctx.chk("the journal carries a line for the turn either way",
            bool(journal(ctx.proj, "planner said:")), True)


RUNNERS = {"background": sit_background, "wait_words": sit_wait_words,
           "deaf_planner": sit_deaf_planner,
           "wall_executor": sit_wall_executor,
           "wall_planner": sit_wall_planner,
           "never_came_up": sit_never_came_up,
           "crash_executor": sit_crash_executor,
           "crash_planner": sit_crash_planner,
           "window_asks": sit_window_asks, "claim_gate": sit_claim_gate}


def run_seed(seed):
    plan = scenario(seed)
    ctx = Ctx(seed, plan)
    say("-" * 70)
    say("SEED %d   fingerprint %s" % (seed, fingerprint(plan)))
    say("   order: %s" % " -> ".join(plan["order"]))
    with daemon._lock:
        daemon.CFG.setdefault("projects", {})[ctx.key] = {}
        daemon.save_state()
    for role in ("executor", "planner"):
        hook(ctx.proj, "SessionStart", role, ctx.sid[role],
             transcript_path="")
        ctx.register(role)
        statusline(ctx.proj, role, ctx.sid[role], 120000)
    post("/loop", {"project": ctx.proj, "action": "start"}, secret=True)

    for sit in plan["order"]:
        ctx.start()
        try:
            RUNNERS[sit](ctx)
        except Exception as exc:
            ctx._fails.append("raised %s: %s" % (type(exc).__name__, exc))
        ok = ctx.finish(sit)
        say("   %-16s %s" % (sit, "PASS" if ok else "FAIL"))
        for f in ctx.results[-1][1]:
            say("        %s" % f)
        # A gap the seed chose, so two situations are not always adjacent
        # in the same way. Never a wait FOR anything - every wait in this
        # file is until() on a fact.
        time.sleep(plan["gap"])
    return ctx


# ---------------------------------------------------------------------------

SEEDS = ([int(a) for a in sys.argv[1:] if a.isdigit()]
         or list(range(1, 11)))

say("throwaway daemon on 127.0.0.1:%d - the real one on 8765 is never "
    "touched" % PORT)
say("seeds: %s" % ", ".join(str(s) for s in SEEDS))
say()

_fps = {s: fingerprint(scenario(s)) for s in SEEDS}
say("0. the seed is real, not decoration")
say("   the same seed gives the same order twice: %s"
    % (fingerprint(scenario(SEEDS[0])) == _fps[SEEDS[0]]))
say("   every seed gives a different one: %s"
    % (len(set(_fps.values())) == len(SEEDS)))
if fingerprint(scenario(SEEDS[0])) != _fps[SEEDS[0]] \
        or len(set(_fps.values())) != len(SEEDS):
    say("   THE SEEDS ARE NOT SEEDS - stopping here, nothing below would "
        "mean anything")
    SRV.shutdown()
    SRV.server_close()
    sys.exit(1)
say()

RESULTS = {}
for _s in SEEDS:
    RESULTS[_s] = run_seed(_s)

say()
say("=" * 70)
say("TEN RUNS, TEN SITUATIONS EACH. A cell is PASS only if every check in")
say("that situation passed in that run.")
say()
_hdr = "%-16s " % "situation" + " ".join("%4d" % s for s in SEEDS)
say(_hdr)
say("-" * len(_hdr))
_bad = 0
for _sit in SITUATIONS:
    _row = []
    for _s in SEEDS:
        _got = [f for (n, f) in RESULTS[_s].results if n == _sit]
        if not _got:
            _row.append("   -")
        elif _got[0]:
            _row.append("FAIL")
            _bad += 1
        else:
            _row.append("  ok")
    say("%-16s " % _sit + " ".join(_row))
say()

_failed_runs = [s for s in SEEDS
                if any(f for (_n, f) in RESULTS[s].results)]
say("runs with no failure: %d of %d" % (len(SEEDS) - len(_failed_runs),
                                        len(SEEDS)))
if _failed_runs:
    say()
    say("WHAT FAILED, in full - a cell in the table is not a diagnosis:")
    for _s in _failed_runs:
        for _n, _f in RESULTS[_s].results:
            for _one in _f:
                say("  seed %-3d %-16s %s" % (_s, _n, _one))

SRV.shutdown()
SRV.server_close()

if OUTDIR:
    os.makedirs(OUTDIR, exist_ok=True)
    with io.open(os.path.join(OUTDIR, "summary.txt"), "w",
                 encoding="utf-8") as fh:
        fh.write("\n".join(LOG))
        fh.write("\nEXIT=%d\n" % (1 if _failed_runs else 0))

# The last seed's stub windows sleep 30 s with their working folder in
# the seed's project, and a folder that is a live process's working
# directory cannot be removed: this run's OWN children are stopped
# first, waited on, and nothing else is touched. -> DECISIONS.md 8.45
for _kid in sessions.child_pids(os.getpid(), names=()) or []:
    sessions.terminate_and_wait(_kid)
owntemp.finish(TMP, bool(_failed_runs))
say("=" * 70)
if _failed_runs:
    say("NOT TEN OF TEN: %d run(s) failed - %s"
        % (len(_failed_runs), ", ".join(str(s) for s in _failed_runs)))
    sys.exit(1)
say("ten of ten: every situation passed in every run")
