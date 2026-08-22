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
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="bridge-wall-test-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
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


def hook(name, role, sid, **extra):
    ev = {"hook_event_name": name, "role": role, "session_id": sid,
          "project_dir": PROJ, "cwd": PROJ}
    ev.update(extra)
    return post("/event", ev)


def statusline(role, sid, tokens, window=WINDOW):
    return post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": PROJ, "project_dir": PROJ},
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


def sess_of(role):
    for s in (daemon.STATE.get("sessions") or {}).values():
        if s.get("role") == role and daemon.norm(s.get("path")) == \
                daemon.norm(PROJ) and s.get("state") not in ("ended", "died"):
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


def backdate(role, minutes=15):
    """Make a session look silent, which is what assess() waits for.

    Both fields, because touch_session writes both and nothing in the bridge
    produces a record with only one. Setting just the clock stamp was a
    fixture no code path makes, and on 2026-08-22 it cost a false red: the
    suite passed at 23:49 and failed at 23:59 on nothing but the date rolling
    over, because silence used to be measured off "%H:%M:%S" mapped onto
    today (-> DECISIONS.md 5.27 and rule 6.5). Silence is an EPOCH question.
    """
    s = sess_of(role)
    when = time.time() - minutes * 60
    s["last_seen"] = time.strftime("%H:%M:%S", time.localtime(when))
    s["seen_at"] = when
    daemon.save_state()


def bring_up(role, sid, port, tokens, model=None):
    """Start a session the way the panel does, then let it introduce itself.

    Launched through the real /session endpoint rather than faked into
    STATE: that is what records the compaction threshold the bridge passed
    at launch, and without it the session has no compaction point and every
    later number is unknown for the wrong reason.
    """
    post("/session", {"action": "launch", "project": PROJ, "role": role,
                      "model": model})
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
# Not a magic number: it is PROJECT_DEFAULTS["autocompact_pct"] of the
# window, and it moved 80 -> 70 on 2026-08-21 because compaction fires
# BETWEEN turns and 80% left only 200k of headroom, while the turn that
# killed a session needed 200,274. Read from the default rather than
# copied, so the suite follows the decision instead of pinning yesterday's.
_want_compact = min(int(WINDOW * store.PROJECT_DEFAULTS["autocompact_pct"]
                        / 100.0), WINDOW - 13000)
check("the compaction point comes from the launch threshold",
      daemon.wall_view(sess_of("executor"), PROJ)["compact"], _want_compact)
check("and that threshold leaves a whole turn of headroom",
      WINDOW - _want_compact >= 200274, True)
check("nothing has compacted yet",
      daemon.compactions_done(PROJ, "executor"), 0)
check("so the distance to the wall is not sizeable",
      daemon.life_view(sess_of("executor"), PROJ)["sizeable"], False)

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
check("five compactions now", daemon.compactions_done(PROJ, "executor"), 5)
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
check("no handover was fired off the stale reading",
      len(launches()) - before, 0)
check("and none is recorded as under way",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), False)

print("\n    the summary lands; the first smaller reading clears the mark")
statusline("executor", EX1, FLOORS[4])
check("the mark is gone",
      bool(sess_of("executor").get("compaction_pending")), False)
plan_now = daemon.plan_for(sess_of("executor"), PROJ)
check("and NOW the plan is handover", plan_now["do"], "handover")
check("for the reason the rule gives",
      "compacted 5 times" in plan_now["why"], True)
check("the planner, meanwhile, is nowhere near it",
      daemon.plan_for(sess_of("planner"), PROJ)["do"], "working")
check("and nothing is blocking it",
      daemon.handover_blocked(PROJ, ("executor",)), None)

print("\nA4. the turn ends - and THIS is the handover moment")
print("    it lands on the floor, right after a compaction, where the")
print("    window has just been freed and there is room to write a handoff")
pl_sess_before = dict(sess_of("planner"))
pl_compactions_before = daemon.compactions_done(PROJ, "planner")
DELIVERED["planner"] = []
before = len(launches())
out = finish_turn("executor", EX1, "the fifth cycle is spent")
ho = (out.get("hook_output") or {})
check("the turn was cut rather than reviewed", ho.get("continue"), False)
check("and the session was told why",
      "Handing over to a fresh executor" in (ho.get("stopReason") or ""), True)
note("stopReason", ho.get("stopReason"))
check("no report was sent for review - the turn did not finish normally",
      any("Executor report" in (d.get("content") or "")
          for d in DELIVERED["planner"]), False)
check("the fifth floor was recorded before the decision was taken",
      [f["after"] for f in daemon.floors(PROJ, "executor")], FLOORS)
for _ in range(300):
    if len(launches()) > before:
        break
    time.sleep(0.1)
check("exactly one window was opened", len(launches()) - before, 1)

lr = launches()[-1]
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
      lr["argv"][lr["argv"].index("--permission-mode") + 1],
      store.DEFAULT_CONFIG["role_modes"]["executor"])
check("with the first model of the executor chain",
      lr["argv"][lr["argv"].index("--model") + 1], "opus")
check("with remote control, so it shows up in the app",
      "--remote-control" in lr["argv"], True)
check("and the development-channels flag the channel needs",
      "--dangerously-load-development-channels" in lr["argv"], True)
check("the compaction threshold was passed to it", lr["autocompact"],
      str(store.PROJECT_DEFAULTS["autocompact_pct"]))
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
row = hl[-1]
check("for the executor", row["role"], "executor")
check("with the size it was carrying", row["used"], FLOORS[-1])
check("the window and where it came from",
      (row["window"], "observed" in (row["window_source"] or "")),
      (WINDOW, True))
check("the compaction point and where it came from",
      (row["compact_at"], "seen compacting" in (row["compact_source"] or "")),
      (min(FIRED), True))
check("every floor this session stood on", row["floors"], FLOORS)
check("and the climb between them", row["floor_rise"], 65000)
check("five of five compactions", (row["compactions_done"], row["budget"]),
      (5, 5))
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
check("assess says so plainly", res2["saw"], "a handover is under way")
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
check("and the surviving planner was told the hands changed",
      any("has been replaced by a fresh session" in (d.get("content") or "")
          for d in DELIVERED["planner"]), True)
check("told through its channel, as information not a task",
      any((d.get("meta") or {}).get("kind") == "info"
          for d in DELIVERED["planner"]), True)
check("the handover is no longer under way",
      bool((daemon.STATE.get("handover") or {}).get(daemon.norm(PROJ))), False)

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
lr = launches()[-1]
note("the stub's argv", " ".join(lr["argv"]))
check("it is the planner", lr["role"], "planner")
check("started in plan mode",
      lr["argv"][lr["argv"].index("--permission-mode") + 1], "plan")
check("with the first model of the planner chain",
      lr["argv"][lr["argv"].index("--model") + 1], "fable")
check("and the editing tools denied outright",
      "--disallowedTools" in lr["argv"], True)
denied = lr["argv"][lr["argv"].index("--disallowedTools") + 1]
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
daemon.assess(STRANGE)
check("no window was opened for it", len(launches()) - before, 0)

# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
SRV.shutdown()
print("windows opened in the whole simulation: %d" % len(launches()))
for i, r in enumerate(launches(), 1):
    print("  %d. %-9s %s" % (i, r["role"],
                             " ".join(r["argv"])[:150]))
print("=" * 68)
shutil.rmtree(TMP, ignore_errors=True)
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
