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

"""The handover moment, simulated end to end.

The five-compaction wall has never been reached on a real run - open item 6
of 13. test_handover.py asserts the arithmetic; this drives the whole
moment: two fake sessions with real channels, five real PreCompact events
with climbing floors, and then the actual assess() -> handover() ->
launch -> SessionStart -> resume_after_handover chain, checked at every hop
rather than at the end.

Nothing real is touched. Own BRIDGE_DATA and CLAUDE_CONFIG_DIR in a temp
folder, own daemon on its own port, a stub in place of claude, telegram
replaced by a recorder. The live daemon on 8765 is never contacted.

Run:  python test_wall_handover.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# first: it reads nothing from the package, and the folder it makes
# is the only one this run may remove (DECISIONS.md 8.43, 8.45)
from bridgecore import owntemp                 # noqa: E402
TMP = owntemp.make("bridge-wall-test-")
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgecore import daemon, sessions, store, telegram      # noqa: E402

FAILED = []
STEP = [0]


def check(name, got, want):
    ok = got == want
    print("  %-4s %s\n       got %r, want %r" % ("ok" if ok else "FAIL",
                                                 name, got, want))
    if not ok:
        FAILED.append(name)


def note(name, got, why=""):
    print("  ..   %s: %r%s" % (name, got, ("  - " + why) if why else ""))


PROJ = os.path.join(TMP, "proj")
os.makedirs(PROJ, exist_ok=True)
WINDOW = 1000000
MODEL = "Opus 5"

# An accepting verdict has to carry a «Checked:» block naming artefacts the
# daemon can find, so this suite gives it a real one. Every case here is about
# the DELIVERY machinery - floors, handovers, the Stop hook - and a bare
# "done" would now be refused before any of that ran, which would test the
# gate over and over instead of the thing each case is for. The file is real
# and inside the throwaway project, so the path resolves exactly as a
# planner's would.
PROOF = os.path.join(PROJ, "run.log")
with open(PROOF, "w", encoding="utf-8") as fh:
    fh.write("exit 0\n")
OKFB = "Checked: run.log"

# ---------------------------------------------------------------------------
# the stand-ins: a claude that only records, a telegram that only records,
# and one channel per role that records what the bridge delivers to it.

BIN = os.path.join(TMP, "fakebin")
os.makedirs(BIN, exist_ok=True)
LAUNCHES = os.path.join(TMP, "launches.log")
STUB_PY = os.path.join(BIN, "claude_stub.py")
with open(STUB_PY, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys\n"
        "row = {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "       'role': os.environ.get('BRIDGE_ROLE'),\n"
        "       'autocompact': os.environ.get("
        "'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE')}\n"
        "open(%r, 'a', encoding='utf-8').write("
        "json.dumps(row, ensure_ascii=False) + '\\n')\n" % LAUNCHES)

_real_build = sessions.build_command


def _stub_build(*a, **kw):
    """The real command line, with only the executable swapped.

    Every flag under test is still the one sessions.py produces; what
    changes is the name at the front, because a stub cannot be reached by
    name on Windows - CreateProcess appends only .exe, so a .bat on PATH is
    skipped and the real client runs instead. That lesson cost two phantom
    projects in Max's panel.
    """
    cmd = _real_build(*a, **kw)
    return [sys.executable, STUB_PY] + cmd[1:]


sessions.build_command = _stub_build
sessions.CREATE_NEW_CONSOLE = 0        # no console windows for a test

# WRAPPED, NOT REPLACED. Since 2026-09-04 the handover opens the new
# window BEFORE stopping the old one, and WHEN the old one is stopped is
# now a claim this suite makes - so the calls are recorded. The real
# function still runs: a stub would mean the stub processes were never
# killed and the assertion would be about a recorder rather than about
# the bridge.
STOPS = []
STOP_TREE = {}     # pid -> whether its tree was stopped with it (8.31)
_real_stop = sessions.stop


def _watched_stop(project, role, pid=None, wait=None, tree=True):
    STOPS.append((role, pid))
    STOP_TREE[pid] = tree
    # Passed only when False, the one value a caller sends on purpose - so
    # the red run against a sessions.stop that has no `tree` still runs.
    return _real_stop(project, role, pid=pid, wait=wait,
                      **({} if tree else {"tree": False}))


sessions.stop = _watched_stop

TG = []
telegram.send = lambda cfg, text, level="silent", buttons=None: TG.append(
    (level, text))
telegram.status_message = lambda cfg, text: cfg
telegram.pin_status = lambda cfg, text: cfg
telegram.pin_links = lambda cfg, text: cfg


def launches():
    if not os.path.exists(LAUNCHES):
        return []
    with open(LAUNCHES, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


DELIVERED = {"executor": [], "planner": []}


class Chan(BaseHTTPRequestHandler):
    role = "executor"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        DELIVERED[self.role].append(body)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def channel_for_role(role):
    cls = type("Chan_" + role, (Chan,), {"role": role})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------------
# the throwaway daemon

daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "compactions": {}, "mode": "running",
                     "loops": {}, "session_roles": {}})
daemon.CFG.setdefault("projects", {})[PROJ] = {}
daemon.CFG["telegram"] = {"token": "", "chat_id": "", "pinned_message_id": 0}
# The idle damper off. Every exchange in this simulation is two words on
# purpose - "ok", "done" - which is exactly what the damper reads as a pair
# with nothing to do, and it would hold the hook instead of letting the
# handover happen. What is under test here is the handover moment; idling is
# test_multipair's case 21.
daemon.CFG.setdefault("thresholds", {})["idle_hold"] = 0
SRV = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
print("throwaway daemon on 127.0.0.1:%d - the real one on 8765 is not "
      "touched" % PORT)


def post(path, payload, secret=False):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Bridge-Secret"] = daemon.SECRET
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(payload).encode("utf-8"), headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def hook(name, role, sid, proj=PROJ, **extra):
    ev = {"hook_event_name": name, "role": role, "session_id": sid,
          "project_dir": proj, "cwd": proj}
    ev.update(extra)
    return post("/event", ev)


def statusline(role, sid, tokens, window=WINDOW, proj=PROJ):
    return post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": proj, "project_dir": proj},
        "model": {"display_name": MODEL, "id": "claude-opus-5"},
        "context_window": {"context_window_size": window,
                           "used_percentage": round(tokens * 100.0 / window, 1),
                           "current_usage": {"input_tokens": 10,
                                             "cache_creation_input_tokens": 90,
                                             "cache_read_input_tokens":
                                             tokens - 100,
                                             "output_tokens": 4000}}}})


def journal_text():
    rows = []
    for day in sorted(os.listdir(store.LOGS)):
        p = os.path.join(store.LOGS, day, "events.jsonl")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="replace"):
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def journal_has(fragment):
    return [r for r in journal_text() if fragment in (r.get("text") or "")]


def sess_of(role, proj=PROJ):
    for s in (daemon.STATE.get("sessions") or {}).values():
        if s.get("role") == role and daemon.norm(s.get("path")) == \
                daemon.norm(proj) and s.get("state") not in ("ended", "died"):
            return s
    return {}


def finish_turn(role, sid, msg, verdict="done", feedback=OKFB):
    """A turn ends: the Stop hook fires and blocks until it is answered.

    This is the hook the whole loop hangs off, and it is where a floor is
    recorded and where a handover is decided - so the simulation has to go
    through it rather than around it. It blocks, so it runs on a thread and
    the verdict is posted from here, exactly as the planner would.
    """
    out = {}

    def run():
        out.update(hook("Stop", role, sid, last_assistant_message=msg) or {})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    if role == "executor" and daemon.loop_state(PROJ)[1].get("active"):
        for _ in range(100):            # a report may or may not be sent:
            if daemon.PENDING.get(daemon.norm(PROJ)):   # a cut turn sends none
                post("/verdict", {"project": PROJ, "verdict": verdict,
                                  "feedback": feedback}, secret=True)
                break
            if not t.is_alive():
                break
            time.sleep(0.05)
    t.join(90)
    return out


def settled():
    """Let anything assess() started finish, before denying that it did.

    THE CLASS THIS EXISTS FOR. assess() decides a handover and starts it in
    a thread, then returns; `len(launches()) - before == 0` on the next line
    is racing that thread and wins, so it is green whether or not a handover
    was fired. A negative check has to outlive what it denies - the same
    defect sabotage found in test_multipair case 61, where a helper answered
    a negative on its first look, before the 1.5 s delivery timer.

    Joining, not sleeping. A number of seconds would be a guess about how
    long a launch takes, which is the shape 5.38 refused for a transcript;
    join() is exact, and free when there is nothing to join. It works
    because the threads are NAMED - see the comment at the handover site in
    daemon.py.

    Returns what it waited for, so a caller can put it in the record rather
    than trust that it did something.
    """
    waited = []
    for t in threading.enumerate():
        if t is threading.current_thread() or not t.is_alive():
            continue
        if (t.name or "").startswith(("handover", "rotate")):
            waited.append(t.name)
            t.join(60)
    return waited


def backdate(role, minutes=15, proj=PROJ):
    """Make a session look silent, which is what assess() waits for.

    Both fields, because touch_session writes both and nothing in the bridge
    produces a record with only one. Setting just the clock stamp was a
    fixture no code path makes, and on 2026-08-22 it cost a false red: the
    suite passed at 23:49 and failed at 23:59 on nothing but the date rolling
    over, because silence used to be measured off "%H:%M:%S" mapped onto
    today (-> DECISIONS.md 5.27 and rule 6.5). Silence is an EPOCH question.
    """
    s = sess_of(role, proj)
    when = time.time() - minutes * 60
    s["last_seen"] = time.strftime("%H:%M:%S", time.localtime(when))
    s["seen_at"] = when
    daemon.save_state()


def launch_from_panel(project, role, model=None):
    """POST /session launch - and wait for the stub window's OWN record.

    The record in launches() is written by the child process the launch
    starts, after Popen has already returned, so the POST coming back says
    nothing about when it lands. A launch not waited for here lands inside
    whatever check counts launches next: on 2026-09-25 D2's start of the
    poisoned executor was counted by D3's "still no window", a negative
    check about a tick that had opened nothing. The wait is on the fact the
    later checks read - the record - never on a number of seconds.
    -> DECISIONS.md 8.30
    """
    n = len(launches())
    r = post("/session", {"action": "launch", "project": project,
                          "role": role, "model": model})
    end = time.time() + 30
    while len(launches()) <= n and time.time() < end:
        time.sleep(0.05)
    check("the %s window it opened has recorded itself" % role,
          len(launches()) > n, True)
    return r


def bring_up(role, sid, port, tokens, model=None):
    """Start a session the way the panel does, then let it introduce itself.

    Launched through the real /session endpoint rather than faked into
    STATE: that is what records the compaction threshold the bridge passed
    at launch, and without it the session has no compaction point and every
    later number is unknown for the wrong reason.
    """
    launch_from_panel(PROJ, role, model)
    hook("SessionStart", role, sid, transcript_path="")
    post("/channel/register", {"project": PROJ, "role": role, "port": port,
                               "pid": 4242, "session_id": sid}, secret=True)
    statusline(role, sid, tokens)


# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
print("SCENARIO A - the executor reaches the wall")
print("=" * 68)

print("\nA1. a pair comes up, both channels register, both draw a status line")
_, EX_PORT = channel_for_role("executor")
_, PL_PORT = channel_for_role("planner")
EX1, PL1 = "ex-session-1", "pl-session-1"
bring_up("planner", PL1, PL_PORT, 90000)
bring_up("executor", EX1, EX_PORT, 120000)
post("/loop", {"project": PROJ, "action": "start"})
check("the executor is seen as up", bool(daemon.already_up(PROJ, "executor")),
      True)
check("and the planner", bool(daemon.already_up(PROJ, "planner")), True)
check("the loop is on",
      daemon.loop_state(PROJ)[1].get("active"), True)
check("the window was observed, not deduced",
      daemon.wall_view(sess_of("executor"), PROJ)["window"], WINDOW)
# The bridge used to hand every window a threshold and this read it back
# out of wall_view. It hands out none since 2026-09-01 - the owner's
# decision that the bridge does not manage auto-compaction at all - so the
# honest answer before anything has compacted is that the point is NOT
# KNOWN, rather than a percentage of the window dressed up as a fact. It
# becomes known from a measurement, which is what this scenario goes on to
# take. The old arithmetic is kept below because it is still true of
# anyone who sets a threshold of their own; it is just not ours any more.
_wv = daemon.wall_view(sess_of("executor"), PROJ)
check("no compaction point is claimed before one is measured",
      _wv["compact"], None)
check("and it says WHY, instead of naming a number nobody set",
      "not started with a threshold" in (_wv["compact_source"] or ""), True)
check("the window is still observed - the control for the two above",
      _wv["window"], WINDOW)
check("and 70% would still have left a whole turn of headroom",
      WINDOW - min(int(WINDOW * 70 / 100.0), WINDOW - 13000) >= 200274, True)
check("nothing has compacted yet",
      daemon.compactions_done(PROJ, "executor"), 0)
print("    and with no point there is no cycle, so the distance to the")
print("    wall is not offered at all. It used to be offered and merely")
print("    called not sizeable, because the point was a percentage the")
print("    bridge had set. life_view already had this branch written; the")
print("    removal is what made it the one that runs")
_lv = daemon.life_view(sess_of("executor"), PROJ)
check("the distance is not claimed", "sizeable" in _lv, False)
check("and the blank says why", "not known for this window"
      in (_lv.get("why_blank") or ""), True)
check("...the control: life_view still answered about this session",
      _lv.get("budget"), daemon.COMPACTIONS_TO_WALL)

print("\nA2. four ordinary compactions: fire, summarise, finish the turn")
print("    the floor is recorded at the Stop that follows a compaction, so")
print("    the turns are real ones - the loop carries each report and the")
print("    verdict comes back, as it would on a live run")
FLOORS = [280000, 340000, 400000, 470000, 540000]
FIRED = [806000, 807000, 808000, 809000, 810000]
for i in range(4):
    statusline("executor", EX1, FIRED[i])          # the turn crosses the point
    hook("PreCompact", "executor", EX1, transcript_path="")
    statusline("executor", EX1, FLOORS[i])         # the summary lands
    finish_turn("executor", EX1, "piece %d done" % (i + 1))
    statusline("executor", EX1, FLOORS[i] + 40000)  # work resumes
    print("   compaction %d: fired at %dk, floor %dk"
          % (i + 1, FIRED[i] // 1000, FLOORS[i] // 1000))

check("four compactions on this session's record",
      daemon.compactions_done(PROJ, "executor"), 4)
check("four floors measured",
      [f["after"] for f in daemon.floors(PROJ, "executor")], FLOORS[:4])
check("and the climb between them is visible - 60k, 60k, then 70k",
      daemon.floor_rise(PROJ, "executor"), (60000 + 60000 + 70000) // 3)
wv = daemon.wall_view(sess_of("executor"), PROJ)
check("the point is now measured, not the launch setting",
      "seen compacting" in wv["compact_source"], True)
check("and it is the SMALLEST sample, because every one is an overshoot",
      wv["compact"], min(FIRED[:4]))
check("four is still working",
      daemon.plan_for(sess_of("executor"), PROJ)["do"], "working")

print("\nA3. the fifth fires - and its reading is the pre-summary one")
print("    3.2 rule 3, the interaction that broke the first live handover:")
print("    between PreCompact and the next status line the size on record")
print("    describes a conversation that is being replaced by a summary")
statusline("executor", EX1, FIRED[4])
hook("PreCompact", "executor", EX1, transcript_path="")
# CHANGED DELIBERATELY 2026-09-12: a PreCompact is the client ANNOUNCING a
# compaction, and until the summary comes back smaller it is not one. On
# 2026-09-07 a watched pair's client fired PreCompact every three minutes at
# 811k under a rate limit and never shrank; the count went 5 -> 20 in an
# hour and the next resume was replaced eight minutes after it started
# for "compacted 20 times". So the fifth is counted below, when the
# smaller reading lands - here it is still four. -> DECISIONS.md 8.18
check("four compactions LANDED - the fifth is announced, not yet counted",
      daemon.compactions_done(PROJ, "executor"), 4)
pend = sess_of("executor").get("compaction_pending")
check("the reading is marked as in flight", bool(pend), True)
check("and it remembers what it was", pend.get("tokens"), FIRED[4])
plan_while_stale = daemon.plan_for(sess_of("executor"), PROJ)
check("so the plan is the routine one, not a handover",
      plan_while_stale["do"], "compacting")
check("and it says which reading it is refusing to use",
      bool(plan_while_stale.get("stale")), True)
backdate("executor")
before = len(launches())
res = daemon.assess(PROJ)
note("assess while the reading is stale", res)
# THE DECISION FIRST, because it is what the call returns and therefore
# cannot race the thread it would have started. The two effect checks below
# corroborate it; on their own they were green by construction.
check("assess did not decide on a handover", "runway" in json.dumps(res),
      False)
note("anything assess started, waited for", settled())
check("no handover was fired off the stale reading",
      len(launches()) - before, 0)
check("and none is recorded as under way",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), False)

print("\n    the summary lands; the first smaller reading clears the mark")
statusline("executor", EX1, FLOORS[4])
check("the mark is gone",
      bool(sess_of("executor").get("compaction_pending")), False)
check("and NOW it is five - the summary came back smaller",
      daemon.compactions_done(PROJ, "executor"), 5)
plan_now = daemon.plan_for(sess_of("executor"), PROJ)
check("and NOW the plan is handover", plan_now["do"], "handover")
check("for the reason the rule gives",
      "compacted 5 times" in plan_now["why"], True)
check("the planner, meanwhile, is nowhere near it",
      daemon.plan_for(sess_of("planner"), PROJ)["do"], "working")
check("and nothing is blocking it",
      daemon.handover_blocked(PROJ, ("executor",)), None)

print("\nA4. the turn ends - and the bridge ASKS FOR THE HANDOFF")
print("    CHANGED DELIBERATELY 2026-09-04 (X1). What stood here asserted")
print("    that this Stop cut the turn and fired the handover. It did, and")
print("    that is exactly what the piece removed: the session was killed")
print("    where it stood and `handover()` carried only the daemon's own")
print("    ten-line table. On 2026-09-04 04:02 a real executor was replaced")
print("    with its project handoff untouched since the day before, in the")
print("    middle of its work, under a reason line reading `a fresh session")
print("    with the handoff is what comes next` - about a file that did not")
print("    exist. The wall now DEMANDS the handoff and waits for a witness")
print("    on the FILE; the launch assertions move to A4b below, unchanged.")
# Reached through getattr, because the RED proof of this piece runs these
# very checks against a daemon that has none of these names - and a flat
# script that raises there stops, taking every block below it with it, which
# is no proof at all. Same idiom as test_multipair case 68.
_MARK = getattr(daemon, "HANDOFF_MARK", "HANDOFF WRITTEN:")
_pend = getattr(daemon, "handover_pending_for", lambda *a, **k: {})
pl_sess_before = dict(sess_of("planner"))
pl_compactions_before = daemon.compactions_done(PROJ, "planner")
DELIVERED["planner"] = []
before = len(launches())
out = finish_turn("executor", EX1, "the fifth cycle is spent")
ho = (out.get("hook_output") or {})
check("the turn is NOT cut - the session has a handoff to write",
      ho.get("continue"), None)
_ctx = ((ho.get("hookSpecificOutput") or {}).get("additionalContext") or "")
check("and the demand rides back on the same hook return, no extra trip",
      ("HANDED OVER AFTER THIS TURN" in _ctx,
       _MARK in _ctx), (True, True))
# JOINED BEFORE IT IS DENIED - `handover()` runs on a thread, and
# "nothing was opened" asked on the next line would be racing it.
# settled() joins anything named handover*/rotate*, which is why those
# threads carry names.
#
# AND IT STILL CANNOT DISCRIMINATE ON THE OLD CODE, which is said here
# rather than left to be discovered: the handover thread as it stood was
# started WITHOUT a name (the name is part of this change), so settled()
# finds nothing to join there and this check answers 0 by timing. The
# claim it makes is covered by the line below it, "nothing is recorded as
# under way", which reads state instead of a clock and does go red on the
# old code. This one earns its place on the NEW code, where the join is
# real.
note("anything the wall started, waited for", settled())
check("nothing was opened yet", len(launches()) - before, 0)
check("and nothing is recorded as under way",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), False)
check("the pending record names the file and when it was asked for",
      (bool(_pend(PROJ, "executor").get("file")),
       bool(_pend(PROJ, "executor").get("at"))),
      (True, True))
check("the fifth floor was recorded before the decision was taken",
      [f["after"] for f in daemon.floors(PROJ, "executor")], FLOORS)

print("\nA4a. another turn ends WITHOUT the file: nothing is killed")
before = len(launches())
out = finish_turn("executor", EX1, "still working, no handoff yet")
check("no window is opened for a session that has not written its handoff",
      len(launches()) - before, 0)
check("the demand still stands",
      bool(_pend(PROJ, "executor")), True)
check("and it was said ONCE, at warn, not once per turn",
      len([r for r in daemon.store.recent_events(200, project=PROJ)
           if "waits for the executor's handoff" in (r.get("text") or "")]), 1)

print("\nA4a2. it SAYS the handoff is written, and the file has not moved")
print("     THE WITNESS THAT MATTERS (rule 30). A report claiming its own")
print("     handoff exists is the witness the event produces; the file's")
print("     mtime is the one it cannot. Without this a version that took")
print("     the session's word for it passes everything else - which is")
print("     exactly what the sabotage `word-is-enough` showed.")
_hf = (_pend(PROJ, "executor").get("file")
       or os.path.join(PROJ, "bridge-logs", time.strftime("%Y-%m-%d"),
                       "handoff", "001-executor.md"))
before = len(launches())
out = finish_turn("executor", EX1,
                  "%s %s\n\nsaid, but nothing was written" % (_MARK, _hf))
note("anything that Stop started, waited for", settled())
check("a claim with no file behind it replaces nobody",
      len(launches()) - before, 0)
check("and the demand still stands", bool(_pend(PROJ, "executor")), True)

print("\nA4b. the handoff is written - and THIS is the handover moment")
_hf = (_pend(PROJ, "executor").get("file")
       or os.path.join(PROJ, "bridge-logs", time.strftime("%Y-%m-%d"),
                       "handoff", "001-executor.md"))
try:
    os.makedirs(os.path.dirname(_hf), exist_ok=True)
except OSError:
    pass
io.open(_hf, "w", encoding="utf-8").write(
    u"# what the next session needs\n\nthe thread, written by the session "
    u"that is being replaced\n")
# THE FILE THE REPORT NAMES, WHICH IS NOT THE ONE THAT WAS DEMANDED.
# They differ in every real handover - the session writes a shift handoff
# under its own name and says so on the first line - and until 2026-09-05
# this fixture used ONE file for both, so no check here could tell the
# demanded file from the checked one. That is what let the journal line
# print the demanded one for a year: on 2026-09-05 16:10:25 it read "The
# executor wrote its handoff (BRIDGE_HANDOFF.md)" while handoff_written
# had accepted 191-shift.md, and a shift read that as the daemon taking a
# header edit for a handoff. -> DECISIONS.md 8.17
_own = os.path.join(PROJ, "bridge-logs", time.strftime("%Y-%m-%d"),
                    "handoff", "191-shift.md")
io.open(_own, "w", encoding="utf-8").write(
    u"# the shift handoff\n\nMARK-OWN-HANDOFF-A4B - the thread itself\n")
# 8.31, THE FORM OF 2026-09-23. The old window is ALIVE and has a
# background job running, as a real one would: the stub windows of this
# suite exit as soon as they have recorded themselves, so the pid record is
# pointed at a stand-in that starts a shell the way a window starts one,
# and a real PreToolUse registers the job. On that day the wall cut the
# turn before its report was made, and `taskkill /T` took ten background
# jobs down with the window. -> DECISIONS.md 8.31
import subprocess as _sp31                                 # noqa: E402
_WIN31 = ("import subprocess, sys, time\n"
          "c = subprocess.Popen(['cmd.exe', '/c', 'ping -n 600 127.0.0.1 "
          ">nul'], creationflags=0x08000000)\n"
          "print(c.pid, flush=True)\n"
          "time.sleep(600)\n")
_win31, _job31 = None, 0
if os.name == "nt":
    _win31 = _sp31.Popen([sys.executable, "-c", _WIN31], stdout=_sp31.PIPE,
                         text=True, creationflags=0x08000000)
    _job31 = int(_win31.stdout.readline().strip() or 0)
    with daemon._lock:
        daemon.STATE.setdefault("pids", {}).setdefault(
            "%s|executor" % daemon.norm(PROJ), {})["pid"] = _win31.pid
        daemon.save_state()
_BGCMD31 = "py tools/night_chain.py --wall31"
hook("PreToolUse", "executor", EX1, tool_name="Bash",
     tool_use_id="toolu_wall31bg",
     tool_input={"command": _BGCMD31, "run_in_background": True})
# LONGER THAN 600 CHARACTERS, WITH THE MARK PAST THEM. The bridge's own
# table already carries the first 600 of the last feedback (last_feedback,
# under "still open"), so a short verdict would reach the replacement
# without the seed doing anything - the sabotage that removed the seed's
# block stayed green on exactly that. What the seed adds is the WHOLE
# verdict, and that is what is asked here.
_WALLFB31 = ("Checked: run.log\n" + ("The first map is accepted as it stands; "
             "the corridor rule holds on every seed that was run. ") * 8
             + "MARK-WALL-VERDICT-A4B - the second map next, from the "
             "handoff.")
DELIVERED["planner"] = []
DELIVERED["executor"] = []
before = len(launches())
_stops_before = len(STOPS)
_oldpid_ex = daemon.pid_of(PROJ, "executor")
out = finish_turn("executor", EX1,
                  "%s %s\n\nthe fifth cycle is spent"
                  % (_MARK, _own), verdict="continue", feedback=_WALLFB31)
ho = (out.get("hook_output") or {})
check("NOW the turn is cut", ho.get("continue"), False)
print("    8.31: the last turn is a turn - its report went to the planner,")
print("    and its verdict is kept for the replacement")
check("the turn that wrote the handoff was reported to the planner",
      any((d.get("meta") or {}).get("kind") == "report"
          and "the fifth cycle is spent" in (d.get("content") or "")
          for d in DELIVERED["planner"]), True)
check("the cut came after the verdict: the hook says the report was reviewed",
      "last report was reviewed" in (ho.get("stopReason") or ""), True)
check("the verdict was not handed to the window about to go "
      "(nothing is waiting for it to pick up)",
      bool((daemon.STATE.get("awaiting") or {}).get(daemon.norm(PROJ))),
      False)
check("and the session is told its handoff is what goes across",
      "handoff is written" in (ho.get("stopReason") or ""), True)
note("stopReason", ho.get("stopReason"))
check("the demand is spent",
      bool(_pend(PROJ, "executor")), False)
for _ in range(300):
    if len(launches()) > before:
        break
    time.sleep(0.1)
check("exactly one window was opened", len(launches()) - before, 1)
# CHANGED DELIBERATELY 2026-09-04 (X1b), and it is a NEW claim rather
# than a reworded one: until that day this block could say nothing
# about the old window, because the handover had already killed it
# three lines earlier - stop, sleep 2, launch. The order is reversed
# now and this is the half of it that belongs here; the other half is
# in A6, where the replacement reports for duty and only THEN is the
# old one stopped. On 2026-09-04 04:02:19 the old order stopped an
# executor whose replacement never came up, and the pair had no
# executor for six hours. -> DECISIONS.md 8.7
check("and the session being replaced was NOT stopped to make room",
      STOPS[_stops_before:], [])
check("and the seed carries the session's OWN handoff, not only the table",
      ((daemon.STATE.get("seed") or {}).get(daemon.norm(PROJ)) or {})
      .get("own_handoff"), _own)
_seed31 = (daemon.STATE.get("seed") or {}).get(daemon.norm(PROJ)) or {}
check("and the planner's verdict on the last report, for the replacement",
      "MARK-WALL-VERDICT-A4B" in (_seed31.get("verdict_words") or ""), True)
check("and the background job the old window leaves running",
      any("night_chain" in c for c in (_seed31.get("bg_inherited") or [])),
      True)
_wrote = [r.get("text") or ""
          for r in daemon.store.recent_events(300, project=PROJ)
          if "wrote its handoff" in (r.get("text") or "")]
check("and the journal names the file that was CHECKED",
      bool(_wrote) and _own in _wrote[-1], True)
check("and says which one was asked for, since the two differ",
      bool(_wrote) and _hf in _wrote[-1], True)

# GUARDED, because everything below reads it. Under a sabotage that stops
# the handover happening at all there is no new launch, and launches()[-1]
# then hands back an OLDER window whose argv has no --model: a raise three
# lines later that takes the rest of the file with it, silently. One FAIL
# for the lot instead - the same principle as read_or_fail.
_lrs = launches()
lr = (_lrs[-1] if len(_lrs) > before
      else {"argv": [], "role": "", "cwd": "", "autocompact": "?"})


def argof(rec, flag):
    """The value after a flag, or "". `.index()` on a missing flag raises,
    and a raise here kills the flat script - which is how a sabotage that
    stops the launch produced a traceback instead of FAIL lines."""
    av = list((rec or {}).get("argv") or [])
    return av[av.index(flag) + 1] if flag in av and av.index(flag) + 1 < len(av) else ""

check("a window was actually opened to read the argv of",
      len(_lrs) > before, True)
note("the stub's argv", " ".join(lr["argv"]))
check("it is the executor that was launched", lr["role"], "executor")
check("in the project folder", os.path.normcase(lr["cwd"]),
      os.path.normcase(PROJ))
check("with the executor's permission mode",
      # was "auto" until 2026-08-14. The client went 2.1.227 -> 2.1.232 and
      # auto grew strict enough that executors asked permission for every
      # fresh shape of command; dontAsk turned out to deny rather than ask,
      # so the default became bypassPermissions. Read from the config
      # rather than written out again, so this case follows the decision
      # instead of having to be found and edited next time it moves.
      argof(lr, "--permission-mode"),
      store.DEFAULT_CONFIG["role_modes"]["executor"])
check("with the first model of the executor chain",
      argof(lr, "--model"), "opus")
check("with remote control, so it shows up in the app",
      "--remote-control" in lr["argv"], True)
check("and the development-channels flag the channel needs",
      "--dangerously-load-development-channels" in lr["argv"], True)
check("NO compaction threshold is handed to the window it opens",
      lr["autocompact"], None)
check("...and the control: the stub really did record this launch",
      lr["role"], "executor")
check("no --resume: a handover is a NEW session, not the old one",
      "--resume" in lr["argv"], False)

print("\n    the planner was not touched")
check("its record is the same session", sess_of("planner").get("session_id"),
      pl_sess_before.get("session_id"))
check("its state was not retired", sess_of("planner").get("state") not in
      ("ended", "died"), True)
check("its compaction count is untouched",
      daemon.compactions_done(PROJ, "planner"), pl_compactions_before)
check("its channel is still registered",
      bool(daemon.channel_for(PROJ, "planner")), True)
check("and only one window was opened in total, not two",
      len([r for r in launches()[before:]]), 1)

print("\n    the arithmetic was written down before it ran")
hl = (daemon.STATE.get("handover_log") or [])
check("a decision row was kept", bool(hl), True)
# Empty is exactly the case the check above is for, and reading it
# anyway would raise and take every block below this one with it - in
# silence, with no summary. .get() lets each of the eight fail on its
# own line instead.
row = hl[-1] if hl else {}
check("for the executor", row.get("role"), "executor")
check("with the size it was carrying", row.get("used"), FLOORS[-1])
check("the window and where it came from",
      (row.get("window"), "observed" in (row.get("window_source") or "")),
      (WINDOW, True))
check("the compaction point and where it came from",
      (row.get("compact_at"),
       "seen compacting" in (row.get("compact_source") or "")),
      (min(FIRED), True))
check("every floor this session stood on", row.get("floors"), FLOORS)
check("and the climb between them", row.get("floor_rise"), 65000)
check("five of five compactions",
      (row.get("compactions_done"), row.get("budget")), (5, 5))
jrow = journal_has("Handover decided for the executor")
check("and the same arithmetic reached the journal", bool(jrow), True)
note("journal line", (jrow[-1]["text"] if jrow else "")[:240])
check("the panel payload carries it too",
      bool(json.loads(urllib.request.urlopen(
          "http://127.0.0.1:%d/state" % PORT, timeout=10).read().decode()
      )["state"].get("handover_log")), True)

print("\n    the handoff was written, with a title, and seeded")
seed = (daemon.STATE.get("seed") or {}).get(daemon.norm(PROJ)) or {}
check("a seed is waiting for the new window", bool(seed), True)
check("it carries a title", bool(seed.get("title")), True)
check("and the handoff itself", len(seed.get("handoff") or "") > 200, True)
check("which is on disk as well",
      len(store.read_handoff(PROJ)) > 200, True)
note("seed title", seed.get("title"))

print("\nA5. while it is under way, nothing starts a second one")
before = len(launches())
res2 = daemon.assess(PROJ)
# CHANGED DELIBERATELY 2026-09-25 (8.31): since A4b the old window has a
# background job running, as the 23.09 one did, and assess() asks about
# running work BEFORE it asks about a handover - so it stands down one tier
# earlier, on the job. Both reasons are true; the claim of this block is
# that it stands down and starts nothing, and that the handover is known.
check("assess stands down, on the job still running or on the handover",
      res2["saw"] in ("a handover is under way",
                      "something is still running for the executor"), True)
check("and the handover is known to be under way",
      bool(daemon.handover_awaits(PROJ, "executor")), True)
check("and does nothing", res2["did"], "nothing")
check("no second window", len(launches()) - before, 0)
print("    nor does the next turn boundary - the decision is a property of")
print("    the session, not of the moment, so it stays true every Stop")
out2 = finish_turn("executor", EX1, "another turn ends mid-handover")
check("the second Stop did not cut the turn again",
      (out2.get("hook_output") or {}).get("continue"), None)
check("and still no second window", len(launches()) - before, 0)
print("    and the naming never held it up")
check("the handover returned without waiting for a name",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), True)
check("no name request blocked anything (no telegram configured)",
      daemon.NAMEWAIT.get(daemon.norm(PROJ)), None)

print("\nA6. the replacement comes up and the thread is handed to it")
EX2 = "ex-session-2"
DELIVERED["executor"] = []
DELIVERED["planner"] = []
out = hook("SessionStart", "executor", EX2, transcript_path="")
ctx = ((out.get("hook_output") or {}).get("hookSpecificOutput") or {})
check("the new window was seeded at SessionStart",
      "handoff" in (ctx.get("additionalContext") or "").lower()
      or "picking up" in (ctx.get("additionalContext") or "").lower()
      or "rotated session" in (ctx.get("additionalContext") or "").lower(),
      True)
check("and given the title", ctx.get("sessionTitle"), seed.get("title"))
note("seeded context, first line",
     (ctx.get("additionalContext") or "").splitlines()[0][:150])
post("/channel/register", {"project": PROJ, "role": "executor",
                           "port": EX_PORT, "pid": 4343,
                           "session_id": EX2}, secret=True)
statusline("executor", EX2, 30000)
for _ in range(120):
    if DELIVERED["executor"] and DELIVERED["planner"]:
        break
    time.sleep(0.1)
check("the handoff was delivered to the new executor",
      any("picking up where the previous session stopped" in
          (d.get("content") or "") for d in DELIVERED["executor"]), True)
check("as a task, so it starts working",
      any((d.get("meta") or {}).get("kind") == "task"
          for d in DELIVERED["executor"]), True)
# THE WHOLE POINT OF X1, AND IT HAD NEVER ARRIVED. A4b already proved the
# seed CARRIES the file; nothing until 2026-09-05 asked whether the
# replacement was ever told. It was not: the SessionStart handler pops
# STATE["seed"] and starts resume_after_handover three statements later,
# which read the same key and found nothing - so every replaced executor
# there has ever been was told "The previous session left NO handoff of
# its own (no file was named)" while the file sat on disk, named in the
# report the bridge had just accepted. A mechanism can be green and dead;
# what closed the gap was asking the RECEIVER instead of the record.
# -> DECISIONS.md 8.17
check("and it carries the previous session's OWN words, not only the table",
      any("MARK-OWN-HANDOFF-A4B" in (d.get("content") or "")
          for d in DELIVERED["executor"]), True)
check("naming the file they came from",
      any(_own in (d.get("content") or "")
          for d in DELIVERED["executor"]), True)
check("and never says there was no handoff when there was one",
      any("left NO handoff of its own" in (d.get("content") or "")
          for d in DELIVERED["executor"]), False)
check("and the surviving planner was told the hands changed",
      any("has been replaced by a fresh session" in (d.get("content") or "")
          for d in DELIVERED["planner"]), True)
check("told through its channel, as information not a task",
      any((d.get("meta") or {}).get("kind") == "info"
          for d in DELIVERED["planner"]), True)
check("the handover is no longer under way",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), False)
# THE OTHER HALF OF A4b's ORDER CLAIM. One stop, of the pid A4b
# recorded before the handover started, and not one moment before the
# replacement said it was up.
for _ in range(200):
    if STOPS[_stops_before:]:
        break
    time.sleep(0.1)
check("and the window it replaced is stopped only now, once, by pid",
      STOPS[_stops_before:], [("executor", _oldpid_ex)])
note("what was stopped, and when", STOPS[_stops_before:])

print("\n    8.31: the replacement was TOLD the verdict and the job; the old")
print("    client went without its tree, the job outlived it, and its record")
print("    is the new session's until the shell it left is gone")
check("the replacement's task carries the verdict on the last report",
      any("MARK-WALL-VERDICT-A4B" in (d.get("content") or "")
          for d in DELIVERED["executor"]), True)
check("and names the background job it inherits",
      any("night_chain" in (d.get("content") or "")
          for d in DELIVERED["executor"]), True)
_rec31 = {}
for _ in range(100):
    _rec31 = next((m for m in ((daemon.STATE.get("inflight") or {})
                              .get(daemon.norm(PROJ)) or {}).values()
                   if isinstance(m, dict) and m.get("bg")
                   and "night_chain" in (m.get("cmd") or "")), {})
    if _rec31.get("session") == EX2:
        break
    time.sleep(0.1)
check("the job's record was handed to the new session, not dropped",
      (_rec31.get("session"), _rec31.get("from_session")), (EX2, EX1))
if _win31 is not None:
    check("the old client was stopped WITHOUT its tree",
          STOP_TREE.get(_oldpid_ex), False)
    try:
        _win31.wait(20)
    except Exception:
        pass
    check("the old client is gone", _win31.poll() is not None, True)
    check("and the job it started is ALIVE after it",
          bool(daemon.sessions.pid_alive(_job31)), True)
    # read AGAIN: the shells are written onto it by stop_the_replaced,
    # which runs after the SessionStart the loop above waited for
    _rec31 = next((m for m in ((daemon.STATE.get("inflight") or {})
                              .get(daemon.norm(PROJ)) or {}).values()
                   if isinstance(m, dict) and m.get("bg")
                   and "night_chain" in (m.get("cmd") or "")), {})
    check("the record knows the shell that says when the job ends",
          _job31 in (_rec31.get("orphans") or []), True)
    _sp31.run(["taskkill", "/PID", str(_job31), "/T", "/F"],
              capture_output=True)
    daemon.sessions.terminate_and_wait(_job31)
    daemon.check_processes()
    check("the job ends: its record goes once the shell is gone",
          any("night_chain" in (m.get("cmd") or "") for m in
              ((daemon.STATE.get("inflight") or {}).get(daemon.norm(PROJ))
               or {}).values() if isinstance(m, dict)), False)
    check("and the journal says so, naming the window it came from",
          bool(journal_has("Background job ended")), True)
else:
    print("  ..   not asked: the process checks - Windows only")

print("\n    the replacement starts clean, the old session keeps its trail")
check("no compactions inherited",
      daemon.compactions_done(PROJ, "executor"), 0)
check("no floors inherited", daemon.floors(PROJ, "executor"), [])
check("its distance is not sizeable again, for the honest reason",
      daemon.life_view(sess_of("executor"), PROJ)["sizeable"], False)
trail = [h for h in (daemon.STATE.get("compactions") or {}).get(
    "%s|executor" % daemon.norm(PROJ), []) if h.get("session") == EX1]
check("and the old session's five are still on record", len(trail), 5)

print("\n    the loop stayed on and the next turn flows normally")
check("the loop is still on", daemon.loop_state(PROJ)[1].get("active"), True)
DELIVERED["planner"] = []
it_before = daemon.loop_state(PROJ)[1].get("iteration", 0)
threading.Thread(target=lambda: hook(
    "Stop", "executor", EX2, last_assistant_message="Picked up the handoff "
    "and finished the first piece."), daemon=True).start()
for _ in range(150):
    if DELIVERED["planner"]:
        break
    time.sleep(0.1)
check("the report reached the planner",
      any("Executor report" in (d.get("content") or "")
          for d in DELIVERED["planner"]), True)
post("/verdict", {"project": PROJ, "verdict": "done", "feedback": OKFB},
     secret=True)
time.sleep(0.5)
check("and the iteration advanced",
      daemon.loop_state(PROJ)[1].get("iteration", 0) > it_before, True)

print("\nA7. a handover that never finishes unsticks itself after 10 minutes")
def stall_a_handover(age):
    with daemon._lock:
        daemon.STATE.setdefault("handover", {})[daemon.norm(PROJ)] = {
            "at": time.time() - age, "reason": "simulated stall",
            "waiting": ["executor"], "roles": ["executor"], "iteration": 1}
        daemon.save_state()


stall_a_handover(60)
check("a young one is left alone", daemon.expire_handover(PROJ), None)
check("and still counts as under way",
      daemon.assess(PROJ)["saw"], "a handover is under way")
stall_a_handover(700)
gone = daemon.expire_handover(PROJ)
check("past ten minutes it is cleared, naming who never came up",
      gone, ["executor"])
check("the flag is gone",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), False)
check("saying so in the journal", bool(journal_has("never finished")), True)
note("journal line", (journal_has("never finished")[-1]["text"])[:190])
print("    and the system can see the rest of the picture again")
stall_a_handover(700)
backdate("executor")
res3 = daemon.assess(PROJ)
check("assess itself clears it and carries on",
      res3["saw"] != "a handover is under way", True)
note("assess after the expiry", res3)

print("\nA7b. naming never blocks a handover (6)")
print("     it used to wait five minutes for a telegram reply while the old")
print("     session was already stopped and no replacement had started")
NAMEPROJ = os.path.join(TMP, "naming")
os.makedirs(NAMEPROJ, exist_ok=True)
daemon.CFG["projects"][NAMEPROJ] = {}
daemon.CFG["telegram"]["chat_id"] = "123456"      # telegram.send is a recorder
daemon.CFG["thresholds"]["name_timeout"] = 30
TG[:] = []
before = len(launches())
t0 = time.time()
r = daemon.handover(NAMEPROJ, "naming test", ("executor",))
took = time.time() - t0
check("the handover ran", r.get("ok"), True)
check("and returned long before the naming timeout", took < 15, True)
note("handover took", round(took, 1), "seconds, name_timeout is 30")
check("a window was started anyway", r.get("started"), ["executor"])
for _ in range(200):
    if len(launches()) > before:
        break
    time.sleep(0.1)
check("and the process really ran", len(launches()) - before, 1)
check("the suggested name was used at once", bool(r.get("title")), True)
check("while the offer to rename is still open",
      bool(daemon.NAMEWAIT.get(daemon.norm(NAMEPROJ))), True)
check("and it was asked for over telegram",
      any("Reply to this message" in t for _, t in TG), True)
waiter = daemon.NAMEWAIT.get(daemon.norm(NAMEPROJ))
waiter["name"] = "a better name"
waiter["event"].set()
for _ in range(100):
    if (daemon.STATE.get("seed") or {}).get(
            daemon.norm(NAMEPROJ), {}).get("title") == "a better name":
        break
    time.sleep(0.05)
check("a name that arrives in time replaces it in the seed",
      (daemon.STATE.get("seed") or {})[daemon.norm(NAMEPROJ)]["title"],
      "a better name")
daemon.CFG["telegram"]["chat_id"] = ""

print("\nA8. the pending-start guard: no second window while one is starting")
with daemon._lock:
    daemon.STATE.setdefault("pids", {})["%s|executor" % daemon.norm(PROJ)] = {
        "pid": 999999, "at": time.time(), "registered": False}
    daemon.save_state()
why = daemon.handover_blocked(PROJ, ("executor",))
check("a handover is refused while a window is pending", bool(why), True)
check("naming the reason", "has not come up yet" in (why or ""), True)
before = len(launches())
r = daemon.handover(PROJ, "should not run", ("executor",))
check("and handover() itself refuses", r.get("ok"), False)
check("no window was opened", len(launches()) - before, 0)
check("the refusal reached the journal", bool(journal_has("Handover held")),
      True)
with daemon._lock:
    daemon.STATE["pids"]["%s|executor" % daemon.norm(PROJ)]["registered"] = True
    daemon.save_state()

# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
print("SCENARIO B - the planner reaches the wall instead")
print("=" * 68)

print("\nB1. five compactions on the planner this time")
for i, floor in enumerate(FLOORS, 1):
    statusline("planner", PL1, 805000 + i * 1000)
    hook("PreCompact", "planner", PL1, transcript_path="")
    if i < len(FLOORS):
        statusline("planner", PL1, floor)
        statusline("planner", PL1, floor + 20000)
statusline("planner", PL1, FLOORS[-1])
check("five on the planner", daemon.compactions_done(PROJ, "planner"), 5)
check("its plan is handover",
      daemon.plan_for(sess_of("planner"), PROJ)["do"], "handover")
check("the executor is fine and stays fine",
      daemon.plan_for(sess_of("executor"), PROJ)["do"], "working")

print("\nB1a. the wall asks the PLANNER for its handoff too")
print("     2026-09-04, the owner: the five-compaction wall works for the")
print("     planner as well. It always did - plan_for reads the role off")
print("     the session and assess() has its own planner branch - but")
print("     nobody ASKED the planner for a handoff before replacing it.")
print("     It is asked on its own Stop hook, the same way X1 asks the")
print("     executor, with one difference that is the owner's: a planner")
print("     that has not written one by the ceiling is replaced anyway,")
print("     because it can rebuild itself from the logs and an executor")
print("     cannot.")
_pl_demand = hook("Stop", "planner", PL1,
                  last_assistant_message="Report accepted; next piece sent.")
_pl_ctx = (((_pl_demand.get("hook_output") or {})
            .get("hookSpecificOutput") or {}).get("additionalContext") or "")
check("the demand rides back on the planner's own Stop hook",
      "HANDOFF" in _pl_ctx.upper(), True)
check("and it names the file to write, in the numbered handoff namespace",
      "-planner.md" in _pl_ctx, True)
note("what the planner was asked", _pl_ctx.replace("\n", " ")[:170])
check("a demand is on record for the planner",
      bool(daemon.handover_pending_for(PROJ, "planner")), True)
check("and it is NOT the executor's - that one is untouched",
      bool(daemon.handover_pending_for(PROJ, "executor")), False)

print("     while it is unwritten and under the ceiling, assess waits")
backdate("executor")
backdate("planner")
_before_hold = len(launches())
_res_hold = daemon.assess(PROJ)
settled()
check("assess says what it is waiting for",
      _res_hold.get("did"), "waiting for its own handoff first")
check("and opens nothing", len(launches()) - _before_hold, 0)

print("     the planner writes it - and now it may be replaced")
_plf = daemon.project_handoff_file(PROJ, "planner")[0]
try:
    os.makedirs(os.path.dirname(_plf), exist_ok=True)
except OSError:
    pass
io.open(_plf, "w", encoding="utf-8").write(
    u"# the planner's own handoff\n\nwhat this review thread was about\n")
_pl_written = hook("Stop", "planner", PL1,
                   last_assistant_message="%s %s\n\nthread closed"
                   % (_MARK, _plf))
# CHANGED DELIBERATELY 2026-09-12. The record used to be CLEARED here, so
# the planner's very next Stop found no demand and asked again - 186 demands
# over 247 turns on one pair that day, and the planner never replaced. A
# written handoff is a STATE the record keeps (file, time, sid) until a
# different session takes the stool; what "spent" means now is that nothing
# holds the replacement any more. -> DECISIONS.md 8.18
_pend_w = daemon.handover_pending_for(PROJ, "planner")
check("the record is kept, marked written, by this session",
      ((_pend_w.get("written") or {}).get("sid")), PL1)
check("and it names the file it accepted",
      (_pend_w.get("written") or {}).get("file"), _plf)
check("so nothing holds the replacement any more",
      daemon.planner_wall_holds(PROJ), False)
print("     and the planner's NEXT Stop is not asked again - one demand per")
print("     session, nothing on its hook return")
_pl_again = hook("Stop", "planner", PL1,
                 last_assistant_message="another review, nothing to do with "
                 "the wall")
_ctx_again = (((_pl_again.get("hook_output") or {})
               .get("hookSpecificOutput") or {}).get("additionalContext")
              or "")
check("no demand rides on it", _MARK in _ctx_again, False)
check("and the journal did not say 'at the wall' a second time",
      len([r for r in daemon.store.recent_events(300, project=PROJ)
           if "The planner is at the wall" in (r.get("text") or "")]), 1)

print("\nB2. assess replaces the planner alone")
# A8 above left a pending window that never registers, on purpose, and the
# expiry blocks before it counted failed handovers - also on purpose. Both
# are that block's subject, not this one's, and since 2026-08-22 a streak
# of failed handovers holds the next one back (see handover_blocked).
# Cleared here so B2 tests what it is about; production clears it the
# honest way, when a window actually registers.
with daemon._lock:
    (daemon.STATE.get("handover_failed") or {}).pop(daemon.norm(PROJ), None)
    daemon.STATE.setdefault("pids", {}).pop(
        "%s|executor" % daemon.norm(PROJ), None)
    daemon.save_state()
ex_sid_before = sess_of("executor").get("session_id")
ex_compactions_before = daemon.compactions_done(PROJ, "executor")
backdate("executor")
backdate("planner")
# THE PLANNER'S WINDOW IS ALIVE, as A4b's executor is: the stub windows exit
# as soon as they have recorded themselves, and since 8.36 a handover names
# the window it replaces before it opens one - a dead record and a channel
# that names no window is a half that cannot be named, and it is refused.
# The stand-in is what stop_the_replaced stops at the new SessionStart.
_win36 = _sp31.Popen([sys.executable, "-c",
                      "import time" + chr(10) + "time.sleep(600)"],
                     creationflags=0x08000000 if os.name == "nt" else 0)
with daemon._lock:
    daemon.STATE.setdefault("pids", {}).setdefault(
        "%s|planner" % daemon.norm(PROJ), {})["pid"] = _win36.pid
    daemon.save_state()
before = len(launches())
res = daemon.assess(PROJ)
note("assess", res)
check("it saw the planner at the end of its runway",
      res["saw"], "the planner at the end of its runway")
for _ in range(200):
    if len(launches()) > before:
        break
    time.sleep(0.1)
check("one window", len(launches()) - before, 1)
# GUARDED, exactly as A4b is, and for the same reason: with no new
# launch, launches()[-1] hands back an OLDER window whose argv has no
# --model, and every check below reads it. Found by a sabotage, not by
# a run - the unguarded form raised at --disallowedTools and took
# scenarios C and D with it, in silence.
_lrs2 = launches()
lr = (_lrs2[-1] if len(_lrs2) > before
      else {"argv": [], "role": "", "cwd": "", "autocompact": "?"})
note("the stub's argv", " ".join(lr["argv"]))
check("it is the planner", lr["role"], "planner")
check("started in plan mode",
      argof(lr, "--permission-mode"), "plan")
check("with the first model of the planner chain",
      argof(lr, "--model"), "fable")
check("and the editing tools denied outright",
      "--disallowedTools" in lr["argv"], True)
denied = argof(lr, "--disallowedTools")
for tool in ("Edit", "Write", "Bash"):
    check("  %s denied to the reviewer" % tool, tool in denied, True)

print("\n    the executor was left strictly alone")
check("same session id", sess_of("executor").get("session_id"),
      ex_sid_before)
check("still working", sess_of("executor").get("state") not in
      ("ended", "died"), True)
check("compactions untouched", daemon.compactions_done(PROJ, "executor"),
      ex_compactions_before)
check("a planner seed was written, not an executor one",
      (bool((daemon.STATE.get("planner_seed") or {}).get(daemon.norm(PROJ))),
       daemon.norm(PROJ) in (daemon.STATE.get("seed") or {})),
      (True, False))

print("\nB3. the fresh planner is seeded and told it alone was replaced")
PL2 = "pl-session-2"
out = hook("SessionStart", "planner", PL2, transcript_path="")
ctx = ((out.get("hook_output") or {}).get("hookSpecificOutput") or {})
body = ctx.get("additionalContext") or ""
check("it is told it is the planner", "PLANNER" in body, True)
check("that it is continuing a handover", "continuing a handover" in body,
      True)
check("and that the executor was NOT replaced",
      "Only you were replaced" in body, True)
# V2, 2026-09-04. What a replaced planner is given is NOT a handoff and
# NOT an /init: it is the pointers, and the instruction to rebuild its own
# thread from them and check no task was lost. The owner's words: "the
# planner comes back without questions and without init - a planner does
# not need init - and reads the logs, makes itself a handoff as it were".
check("it is told NOT to run /init, in those words",
      "DO NOT RUN /init" in body, True)
# EVERY POINTER IS LOOKED FOR INSIDE THE SEED, not anywhere in the body.
# The sabotage `no-v2-seed` is what showed why: with the whole seed
# removed, "and the index" stayed GREEN, because INDEX.md appears in the
# daemon's own handoff table further up. A check that passes with the
# thing it is about deleted is not a check (rule 19), so the search starts
# where the seed starts.
_v2 = body.split("DO NOT RUN /init", 1)[-1]
check("and given the day's dialogue to rebuild the thread from",
      "dialogue.md" in _v2, True)
check("and the index", "INDEX.md" in _v2, True)
check("and the executor's open tasks", "open tasks" in _v2, True)
check("and told to check that no task was lost",
      "not one task has been lost" in _v2.lower(), True)
check("and that its first act is a verdict or a task, not a question",
      "not a question" in _v2, True)
note("the V2 seed, first line",
     [l for l in body.splitlines() if "relaunched" in l][:1])
DELIVERED["planner"] = []
post("/channel/register", {"project": PROJ, "role": "planner",
                           "port": PL_PORT, "pid": 4444,
                           "session_id": PL2}, secret=True)
for _ in range(100):
    if not (daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ)):
        break
    time.sleep(0.1)
check("the handover completed", bool(journal_has(
    "Planner handover complete - the executor was left alone")), True)
check("the new planner starts with no compactions",
      daemon.compactions_done(PROJ, "planner"), 0)

# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
print("SCENARIO C - the point is unknown, so nothing is decided from it")
print("=" * 68)

print("\nC1. a window the bridge did not start: no threshold, no compaction")
STRANGE = os.path.join(TMP, "stranger")
os.makedirs(STRANGE, exist_ok=True)
daemon.CFG["projects"][STRANGE] = {}
sid = "unknown-1"
with daemon._lock:
    daemon.STATE["sessions"]["executor:%s" % sid[:8]] = {
        "role": "executor", "path": daemon.norm(STRANGE), "session_id": sid,
        "model": MODEL, "window": WINDOW, "window_observed": True,
        "context_tokens": 941000,
        "turn_costs": [60000, 75000, 52000], "state": "idle",
        "last_seen": daemon.now(), "seen_at": time.time()}
    daemon.STATE.setdefault("last_session", {})[
        "%s|executor" % daemon.norm(STRANGE)] = sid
    daemon.save_state()
s = daemon.STATE["sessions"]["executor:%s" % sid[:8]]
wv = daemon.wall_view(s, STRANGE)
check("carrying 941k of a 1M window", (wv["used"], wv["window"]),
      (941000, WINDOW))
check("no compaction point is known", wv["compact"], None)
check("and it says so", "unknown" in wv["compact_source"], True)
check("whether a compaction fires first is unknowable",
      wv["interception_unknown"], True)
lv = daemon.life_view(s, STRANGE)
check("so no cycle, and no distance", lv.get("left"), None)
check("naming the term that is missing",
      "not known" in (lv.get("why_blank") or ""), True)
plan = daemon.plan_for(s, STRANGE)
check("and the bridge does NOT act on it", plan["do"], "working")
check("saying which term it lacks",
      "compaction point not known" in plan["why"], True)
rep = daemon.state_report(STRANGE, "executor", s, "where you stand",
                          "carry on")
check("the state report names it too",
      "neither the cycle nor the distance" in rep, True)
note("the line in the state report that names it",
     [l for l in rep.splitlines() if "neither the cycle" in l][0][:170])
before = len(launches())
backdate("executor")
_res_strange = daemon.assess(STRANGE)
# Same repair as A2's: the synchronous decision, then the effect once
# anything assess() started has been joined.
check("assess did not decide on a handover",
      "runway" in json.dumps(_res_strange), False)
note("anything assess started, waited for", settled())
check("no window was opened for it", len(launches()) - before, 0)

# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
print("SCENARIO D - a point measured under a threshold this window has not")
print("=" * 68)
print("    2026-09-02 16:37:08, this bridge's own executor, and the whole")
print("    incident is in DECISIONS.md 5.45. The pair's calibration held ten")
print("    samples at 469k-475k, every one of them taken between 08-28 and")
print("    09-01 12:23 while the window ran on autoCompactWindow 700000 x")
print("    70%. That regime was removed on 09-01 at 11:24:53. The session")
print("    that came after it ran to 637k on a 1M window without compacting")
print("    once - which is what this client does when nobody sets a")
print("    threshold - and the bridge read its own stale point as a fact")
print("    about today and replaced it mid-task.")
print("    Real order, real endpoints: an old session compacts twice, ends,")
print("    a fresh one climbs past the point it inherits, and the real")
print("    assess() tick decides.")

POISON = os.path.join(TMP, "poisoned")
os.makedirs(POISON, exist_ok=True)
daemon.CFG["projects"][POISON] = {}

print("\nD1. the old regime: two real compactions at 469k and 475k")
print("    driven through the real hooks, so the sample, the point and the")
print("    floor are all written by the code that writes them live")
OLD = "poison-old-1"
hook("SessionStart", "executor", OLD, proj=POISON, transcript_path="")
statusline("executor", OLD, 300000, proj=POISON)
hook("Stop", "executor", OLD, proj=POISON, last_assistant_message="ok")
for fired, floor in ((469955, 86687), (475887, 129076)):
    statusline("executor", OLD, fired, proj=POISON)
    hook("PreCompact", "executor", OLD, proj=POISON)
    statusline("executor", OLD, floor, proj=POISON)
    hook("Stop", "executor", OLD, proj=POISON, last_assistant_message="ok")
_cal = store.calib_get(MODEL.lower(), POISON, WINDOW)
check("both compactions were recorded as samples",
      _cal.get("compact_samples"), [469955, 475887])
check("and the point is the smaller one - every sample is an overshoot",
      _cal.get("compact_at_tokens"), 469955)
check("both left a floor, which is the proof they went through",
      [r.get("after") for r in
       (daemon.STATE.get("compactions") or {}).get(
           "%s|executor" % daemon.norm(POISON), [])][-2:], [86687, 129076])
check("so the largest compaction this pair has SURVIVED is the 475k one",
      daemon.compaction_survivable(POISON, "executor"), 475887)
check("and nothing has ever failed to compact here",
      daemon.compaction_failed_at(POISON, "executor"), None)
hook("SessionEnd", "executor", OLD, proj=POISON)

print("\n    this pair's turns, as note_turn_cost records them live.")
print("    The two figures below are what decide the case, so they are the")
print("    real pair's: widest 129 799 and ordinary (p90) 83 586, off the")
print("    44 turns STATE['turns'] held at 16:37")
for _c in (20000, 25000, 28000, 30000, 32000, 35000, 38000, 40000, 46592,
           83586, 129799):
    daemon.note_turn_cost(POISON, "executor", _c, OLD)
check("widest, measured for this pair",
      daemon.turn_widest(POISON, "executor"), (129799, "measured"))
check("ordinary, measured for this pair",
      daemon.turn_ordinary(POISON, "executor"), (83586, "measured"))

print("\nD2. a fresh session inherits the point and runs past it")
print("    it has compacted nothing itself, so wall_view falls back to the")
print("    calibration entry - which is the stale one")
NEW = "poison-new-1"
_srv_d, _port_d = channel_for_role("executor")
launch_from_panel(POISON, "executor")
hook("SessionStart", "executor", NEW, proj=POISON, transcript_path="")
post("/channel/register", {"project": POISON, "role": "executor",
                           "port": _port_d, "pid": 4242,
                           "session_id": NEW}, secret=True)
statusline("executor", NEW, 637053, proj=POISON)
_s = sess_of("executor", POISON)
check("the fresh session carries 637k of a 1M window",
      (_s.get("context_tokens"), _s.get("window")), (637053, WINDOW))
_wv = daemon.wall_view(_s, POISON)
check("it has compacted nothing itself",
      daemon.compaction_sizes(POISON, "executor"), [])
check("and it is 167k past the point it inherited",
      _wv["used"] - (_wv["compact"] or 0), 167098)
check("which is further than any one turn of this pair has ever been",
      _wv["used"] - (_wv["compact"] or 0) > 129799, True)

print("\n    THE CEILING. A proven compaction is evidence that a session")
print("    SURVIVES that size, so it may raise the line above the reserve")
print("    model - it may not lower it. 475 887 + 83 586 = 559 473 was")
print("    being used as the wall for a 1M window whose reserve line is")
print("    967 000: a success at 475k made the bridge 400k more timid than")
print("    knowing nothing at all would have")
check("the ceiling is at least the reserve line, never below it",
      daemon.compaction_too_big(POISON, "executor", WINDOW),
      WINDOW - daemon.RESERVED_TOKENS)
_why = daemon.compaction_too_big_why(POISON, "executor", WINDOW)
check("and it says where that number came from",
      "reserve" in (_why.get("source") or ""), True)
check("carrying the evidence it did NOT use, so the choice is readable",
      (_why.get("proven"), _why.get("failed_at")), (475887, None))

print("\nD3. the tick: the point is refuted, and the session is left alone")
print("    rule 33 turned on the measurement itself. A session more than one")
print("    of its own widest turns past a measured point, with no compaction")
print("    of its own and no recorded failure, is not a session in trouble -")
print("    it is a point that no longer describes this window")
backdate("executor", proj=POISON)
_before = len(launches())
_res = daemon.assess(POISON)
note("what the tick saw and did", _res)
# THE WORD MATTERS. Written as `"handover" in json.dumps(_res)` this was
# green while the tick was answering "handing over the executor to a fresh
# session" - assess() never uses the noun. A negative check that cannot
# fail is worse than none, so this is the word the branch actually writes,
# the same one scenario C tests on.
check("assess did not decide the runway had ended",
      "runway" in json.dumps(_res), False)
# And the effect, joined rather than waited for: settled() returns what it
# joined, so an empty list is a fact about the threads, not about how long
# this line was prepared to wait. The launch count is the weaker companion
# - a handover can be refused after the thread starts - so the thread is
# the primary and both are kept.
check("and started no handover", settled(), [])
check("and opened no window", len(launches()) - _before, 0)
check("the refutation was recorded for the pair",
      bool((daemon.STATE.get("point_refuted") or {}).get(
          "%s|executor" % daemon.norm(POISON))), True)
# Guarded, because a check that has already spoken must not be able to
# un-speak itself: the FAIL above is the answer, and an unguarded [] here
# would raise and take every block below it in silence.
_ref = (daemon.STATE.get("point_refuted") or {}).get(
    "%s|executor" % daemon.norm(POISON)) or {}
check("with what was refuted, and at what size",
      (_ref.get("point"), _ref.get("used")), (469955, 637053))
check("and it is in the journal at warn",
      bool([r for r in journal_has("no longer describes this window")
            if r.get("level") == "warn"]), True)
note("the journal line", ([r.get("text") for r in
                           journal_has("no longer describes this window")]
                          or [""])[0][:200])

print("\n    from here the pair reads like one that never measured a point")
print("    at all - which is the honest answer, and the one the pair next")
print("    door has been running on all along")
_wv2 = daemon.wall_view(_s, POISON)
check("the point is no longer offered as a number", _wv2["compact"], None)
check("it says it was refuted and why",
      "refuted" in (_wv2.get("compact_source") or "")
      and "may not have" in (_wv2.get("compact_source") or ""), True)
check("and it is no longer a measured point", _wv2["compact_measured"], False)
_plan = daemon.plan_for(_s, POISON)
check("so the plan is working, not handover", _plan["do"], "working")
check("naming what it lacks",
      "compaction point not known" in _plan["why"], True)

print("\n    a second tick changes nothing and says nothing twice")
_n_before = len([r for r in journal_has("no longer describes this window")])
_before = len(launches())
_res2 = daemon.assess(POISON)
check("still no handover", "runway" in json.dumps(_res2), False)
check("and still no handover thread", settled(), [])
check("still no window", len(launches()) - _before, 0)
check("and the journal was not written twice",
      len(journal_has("no longer describes this window")), _n_before)

print("\nD4. a new sample clears the refutation - the point can come back")
print("    the refutation is about evidence going stale, not about the pair")
print("    being exempt. ONE real compaction of this window and the")
print("    measurement is current again - and it does not merely un-refute")
print("    the old number, it REPLACES it: compaction_point anchors on the")
print("    newest sample, so the ten dead ones fall outside one turn of it")
print("    and are dropped. That is 5.29's anchor doing the job it was")
print("    written for, which it cannot do while no new samples arrive")
statusline("executor", NEW, 900000, proj=POISON)
hook("PreCompact", "executor", NEW, proj=POISON)
check("the refutation is gone",
      (daemon.STATE.get("point_refuted") or {}).get(
          "%s|executor" % daemon.norm(POISON)), None)
check("and the journal says the point is back in use",
      bool(journal_has("refuted compaction point is back in use")), True)
_cal4 = store.calib_get(MODEL.lower(), POISON, WINDOW)
check("the old regime's samples are still on file",
      _cal4.get("compact_samples"), [469955, 475887, 900000])
check("but the point is the new one - the old two are further from it "
      "than one turn of this pair, so they cannot describe it",
      _cal4.get("compact_at_tokens"), 900000)
statusline("executor", NEW, 140000, proj=POISON)
_s2 = sess_of("executor", POISON)
_wv3 = daemon.wall_view(_s2, POISON)
check("and the point is a number again, this session's own",
      _wv3["compact_measured"], True)
check("the number itself", _wv3["compact"], 900000)
print("\n    and rule 1a now reckons from the higher line, so a stale low")
print("    'proven' can no longer make it fire: 469k > 559k was true and")
print("    469k > 967k is not")
check("1a does not call this session unable to compact",
      _wv3["compact"] > daemon.compaction_too_big(POISON, "executor", WINDOW),
      False)

print("\n    900 000 is the real number now: autoCompactWindow 1000000 x 90 %")
print("    (DECISIONS 8.2, the owner's decision of 2026-09-02). So the walk")
print("    that matters is UP TO it - nothing may be handed over below a")
print("    point the pair is going to compact at, which is the whole of what")
print("    a threshold is for")
_walk = []
for _used in (200000, 400000, 600000, 700000, 800000, 880000):
    statusline("executor", NEW, _used, proj=POISON)
    _sx = sess_of("executor", POISON)
    _walk.append((_used // 1000, daemon.plan_for(_sx, POISON)["do"]))
check("no handover anywhere below the point",
      sorted({p for _, p in _walk}), ["compacting", "working"])
note("the walk", _walk)
print("    'compacting' from 810k is rule 2's own 90 % branch - 'short of")
print("    its point, it will compact and carry on' - and is the routine")
print("    answer, not a decision. The claim under test is the absence of")
print("    HANDOVER, which is what 1a and 1b would produce")
check("neither 1a nor 1b fired at any size below the point",
      [u for u, p in _walk if p == "handover"], [])
print("    and AT the point it is 'compacting' - the bridge stands aside")
print("    and says nothing, which is the routine case")
statusline("executor", NEW, 900000, proj=POISON)
check("at the point, the bridge stands aside",
      daemon.plan_for(sess_of("executor", POISON), POISON)["do"], "compacting")

print("\nD5. the control: a pair whose point is honest is untouched")
print("    the same tick, one ordinary turn past a point it really does")
print("    compact at - case 22's shape, and it must stay routine")
HONEST = os.path.join(TMP, "honest")
os.makedirs(HONEST, exist_ok=True)
daemon.CFG["projects"][HONEST] = {}
H = "honest-1"
hook("SessionStart", "executor", H, proj=HONEST, transcript_path="")
for _c in (30000, 40000, 50000, 60000):
    daemon.note_turn_cost(HONEST, "executor", _c, H)
statusline("executor", H, 470000, proj=HONEST)
hook("PreCompact", "executor", H, proj=HONEST)
statusline("executor", H, 120000, proj=HONEST)
hook("Stop", "executor", H, proj=HONEST, last_assistant_message="ok")
statusline("executor", H, 500000, proj=HONEST)
_hs = sess_of("executor", HONEST)
_hwv = daemon.wall_view(_hs, HONEST)
check("it is past its own point, by less than one of its turns",
      0 < _hwv["used"] - (_hwv["compact"] or 0) <= 60000, True)
check("the point is still a measured number", _hwv["compact_measured"], True)
check("nothing was refuted for it",
      (daemon.STATE.get("point_refuted") or {}).get(
          "%s|executor" % daemon.norm(HONEST)), None)
check("and the bridge says it is compacting, as it always did",
      daemon.plan_for(_hs, HONEST)["do"], "compacting")

# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
SRV.shutdown()
check("CONTROL: every other window this simulation stopped went whole, tree "
      "and all - only the one with a job left running was spared its tree",
      (len(STOP_TREE) > 1,
       [p for p, t in STOP_TREE.items() if p != _oldpid_ex and not t]),
      (True, []))
if _win31 is not None and _win31.poll() is None:
    _win31.kill()
if _win36.poll() is None:
    _win36.kill()
print("windows opened in the whole simulation: %d" % len(launches()))
for i, r in enumerate(launches(), 1):
    print("  %d. %-9s %s" % (i, r["role"],
                             " ".join(r["argv"])[:150]))
print("=" * 68)
owntemp.finish(TMP, bool(FAILED))
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
