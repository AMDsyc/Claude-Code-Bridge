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

"""Regression suite for when a session is replaced.

One rule decides it: the room the session has left to work in - from the
floor its last compaction left it on, up to where compaction fires again -
measured in turns. Five turns or fewer and it is replaced. No compaction
counter, no distance to an unmeasured wall.

The numbers in cases 1-4 are from the run of 2026-07-28: a 1M window, a
compaction seen firing after a turn that ended at 1002k, and ~33k turns.

Run:  python3 test_handover.py
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# first: it reads nothing from the package, and the folder it makes
# is the only one this run may remove (DECISIONS.md 8.43, 8.45)
from bridgecore import owntemp                 # noqa: E402
TMP = owntemp.make("bridge-test-")
TMUX_HOME = owntemp.tmux_home()   # its windows' tmux sockets (8.103)
if TMUX_HOME:
    os.environ["TMUX_TMPDIR"] = TMUX_HOME
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
# The client's own config is isolated too: install() marks a project trusted
# there, and without this a suite would merge its throwaway temp projects into
# the real ~/.claude.json on this machine.
os.environ["BRIDGE_CLAUDE_JSON"] = os.path.join(TMP, ".claude.json")
# and the user-level settings approve_channel merges into - never the
# real one (DECISIONS.md 8.35)
os.environ["BRIDGE_CLAUDE_SETTINGS"] = os.path.join(TMP, "user-settings.json")
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgecore import daemon, store            # noqa: E402

PATH = os.path.join(TMP, "proj")
os.makedirs(PATH, exist_ok=True)
MODEL = "opus 5"
WINDOW = 1000000
SID = "sess-new"

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %-4s %s\n       got %r, want %r" % ("ok" if ok else "FAIL",
                                                 name, got, want))
    if not ok:
        FAILED.append(name)


# A check of a fact only Windows has. Off Windows it is neither passed nor
# dropped: it is named as skipped, with the reason, and the list of them
# ends the run (a Mac, 2026-10-12, 8.89). `got` is still evaluated - a
# check whose expression cannot run off Windows needs its own guard.
SKIPPED = []


def win_check(name, got, want, why):
    if os.name == "nt":
        return check(name, got, want)
    print("  skip %s" % name)
    print("       Windows only: %s" % why)
    SKIPPED.append("%s - %s" % (name, why))


def skip(name, why):
    """A check this machine cannot make - the other way round from
    win_check: one that needs a Mac's tmux, named with what it lacks."""
    print("  skip %s" % name)
    print("       not here: %s" % why)
    SKIPPED.append("%s - %s" % (name, why))


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



def reset(compactions=(), sid=SID, compact_at=None, autocompact=80):
    daemon.STATE.clear()
    daemon.STATE.update({"sessions": {}, "compactions": {},
                         "last_session": {"%s|executor" % daemon.norm(PATH):
                                          sid},
                         "pids": {"%s|executor" % daemon.norm(PATH):
                                  {"pid": 1, "at": time.time(),
                                   "registered": True,
                                   "autocompact": autocompact,
                                   "model_req": "opus[1m]"}}})
    if compactions:
        daemon.STATE["compactions"]["%s|executor" % daemon.norm(PATH)] = \
            list(compactions)
    cal = store.load_calibration()
    cal[store.calib_key(MODEL, PATH)] = {
        "ceiling_pct": 97.0, "buffer_tokens": 33000, "misses": 0,
        "clean_streak": 0, "multiplier": 1.5, "wall_history_tokens": None,
        "compact_at_tokens": compact_at, "compact_at_window": WINDOW,
        "how": "test"}
    store.save_calibration(cal)
    # Cases 1-22 are the arithmetic of a FIVE-compaction wall, which since
    # 40.11 is the planner's default and no longer the executor's (15, and
    # worked out from its floors). The project names five for both here, so
    # the cases keep testing what they were written for; wall_for has its
    # own case.
    daemon.CFG.setdefault("projects", {})[PATH] = {}
    daemon.CFG["projects"][daemon.norm(PATH)] = {
        "wall_compactions": {"executor": 5, "planner": 5},
        "wall_computed": {"executor": False}}
    daemon.CFG["thresholds"] = daemon.CFG.get("thresholds") or {}


def comp(before, after, sid=SID):
    return {"at": "x", "tokens": before, "after": after, "session": sid}


def sess(used, costs=(33000, 31000, 35000), pending=None, sid=SID):
    s = {"role": "executor", "path": daemon.norm(PATH), "session_id": sid,
         "model": MODEL, "window": WINDOW, "window_observed": True,
         "context_tokens": used, "turn_costs": list(costs)}
    if pending:
        s["compaction_pending"] = pending
    daemon.STATE["sessions"]["executor:%s" % sid[:8]] = s
    return s


print("\n1. the wall is the fifth compaction, and the distance to it")
print("   is the rest of this cycle plus a cycle for each one left")
reset(compactions=[comp(800000, 200000)], compact_at=800000)
lv = daemon.life_view(sess(300000), PATH)
check("compactions used", lv["done"], 1)
check("the wall is the fifth", lv["budget"], 5)
check("rest of this cycle", lv["rest_of_cycle"], 500000)
check("whole cycles after it", lv["later_cycles"], 3)
check("distance to the wall", lv["left"], 500000 + 3 * 600000)
check("in turns at 33k", lv["turns_left"], (500000 + 1800000) // 33000)
check("plan", daemon.plan_for(sess(300000), PATH)["do"], "working")

print("\n2. the floor climbs, so each later cycle is shorter")
reset(compactions=[comp(800000, 200000), comp(800000, 300000)],
      compact_at=800000)
lv = daemon.life_view(sess(400000), PATH)
check("rise measured", lv["rise"], 100000)
check("two used, three left", lv["compactions_left"], 3)
check("rest 400k, then cycles of 500k and 400k",
      lv["left"], 400000 + 500000 + 400000)

print("\n3. the fifth compaction is the handover")
reset(compactions=[comp(800000, 200000)] * 4 +
                  [comp(800000, 600000)], compact_at=800000)
check("five used", daemon.compactions_done(PATH, "executor"), 5)
check("plan", daemon.plan_for(sess(650000), PATH)["do"], "handover")
check("nothing left", daemon.life_view(sess(650000), PATH)["left"], 150000)
reset(compactions=[comp(800000, 200000)] * 4, compact_at=800000)
check("four is still working",
      daemon.plan_for(sess(650000), PATH)["do"], "working")

print("\n4. a session that has never compacted cannot size later cycles,")
print("   and the rest of this cycle is not offered as the distance")
reset(compact_at=None)
lv = daemon.life_view(sess(300000), PATH)
check("rest of this cycle is exact", lv["rest_of_cycle"], 500000)
check("it ends at the FIRST compaction", lv["next_ordinal"], 1)
check("no distance to the fifth is claimed", lv.get("left"), None)
check("and it says so outright", lv["sizeable"], False)
check("with the reason", "cannot be sized" in lv["why_partial"], True)
check("nothing to draw a bar from", lv.get("pct"), None)
check("the plan names the missing term rather than the wrong number",
      "not sizeable yet" in daemon.plan_for(sess(300000), PATH)["why"], True)

print("\n5. a fresh session does not inherit its predecessor's compactions")
reset(compactions=[comp(800000, 600000, sid="sess-old")] * 5,
      sid="sess-new", compact_at=800000)
check("counted for this session", daemon.compactions_done(PATH, "executor"), 0)
check("plan", daemon.plan_for(sess(300000), PATH)["do"], "working")

print("\n6. the planner's real numbers from the panel of 2026-07-28:")
print("   200k window, carrying 159k, one compaction seen firing at 150k")
reset(compactions=[comp(150000, 60000)], compact_at=150000, autocompact=None)
cal = store.load_calibration()          # it was measured on a 200k window
cal[store.calib_key(MODEL, PATH)]["compact_at_window"] = 200000
store.save_calibration(cal)
p200 = sess(159000, costs=(8000, 9000, 7000))
p200["window"] = 200000
w = daemon.wall_view(p200, PATH)
check("the measured point is NOT discarded", w["compact"], 150000)
check("and a compaction is due", w["compact_due"], True)
check("so the plan is the routine one",
      daemon.plan_for(p200, PATH)["do"], "compacting")
lv = daemon.life_view(p200, PATH)
check("one of five compactions used", (lv["done"], lv["budget"]), (1, 5))
check("distance to the wall", lv["left"], 0 + 3 * 90000)

print("\n7. a reading from before the last compaction decides nothing")
reset(compactions=[comp(790000, 300000)], compact_at=790000)
p = daemon.plan_for(sess(790000, pending={"at": time.time(),
                                          "tokens": 790000}), PATH)
check("plan while stale", p["do"], "compacting")
check("marked stale", bool(p.get("stale")), True)

print("\n8. never compacted and no threshold set -> nothing is computed,")
print("   and it says which term is missing")
reset(compact_at=None, autocompact=None)
lv = daemon.life_view(sess(400000), PATH)
check("no distance", lv.get("left"), None)
check("none used", lv["done"], 0)
check("and it says what is missing", "not known" in lv["why_blank"], True)
check("plan", daemon.plan_for(sess(400000), PATH)["do"], "working")
print("   but one compaction of its own is enough to make it computable")
reset(compactions=[comp(700000, 200000)], compact_at=None, autocompact=None)
lv = daemon.life_view(sess(400000), PATH)
check("point from its own record", lv["compact"], 700000)
check("distance now known", lv["left"], 300000 + 3 * 500000)

print("\n9. carried context is the input context, not input plus output")
cw = {"current_usage": {"input_tokens": 10, "cache_creation_input_tokens":
                        30000, "cache_read_input_tokens": 900000,
                        "output_tokens": 4000}}
check("output is not counted in", daemon._tokens(cw), 930010)
cw2 = {"current_usage": {"input_tokens": 10, "cache_creation_input_tokens":
                         30000, "cache_read_input_tokens": 900000,
                         "ephemeral_5m_input_tokens": 20000,
                         "ephemeral_1h_input_tokens": 10000,
                         "output_tokens": 4000}}
check("and a cache breakdown is not counted twice",
      daemon._tokens(cw2), 930010)

print("\n10. a conversation bigger than its window discards the window,")
print("    even when the window came from the launch alias")
reset()
s = sess(1002000)
w, src = daemon.known_window(PATH, "executor", s)
check("window rejected", w, None)
check("and it says why", "one of the two numbers is wrong" in (src or ""),
      True)
check("plan decides nothing", daemon.plan_for(s, PATH)["do"], "unknown")

print("\n11. automatic handovers still name exactly one role")
import inspect
import io                                          # noqa: E402
src = inspect.getsource(daemon.handle_event)
check("the Stop path hands over the executor alone",
      'roles_to_go = ("executor",)' in src, True)
check("with its own launch check",
      "handover_blocked(path, roles_to_go)" in src, True)
asrc = inspect.getsource(daemon.assess)
check("assess: executor alone",
      'args=(path, plan["why"], ("executor",))' in asrc, True)
check("assess: planner alone",
      'args=(path, pl["plan"]["why"], ("planner",))' in asrc, True)

print("\n12. every handover decision is written down, with the cycle terms")
reset(compactions=[comp(1002000, 760000)], compact_at=1002000)
s = sess(770000)
row = daemon.log_handover_decision(PATH, "executor", s,
                                   daemon.plan_for(s, PATH))
check("floors on record", row["floors"], [760000])
check("compactions on record", (row["compactions_done"], row["budget"]),
      (1, 5))
check("distance to the wall on record", row["left_to_wall"] is not None, True)
check("kept for the panel", len(daemon.STATE.get("handover_log") or []), 1)

# A percentage written into a launch path itself rather than read from
# the project - `compact_pct=90` or similar. The project's own setting
# is the only place a number may live, which is the whole difference
# between this and the bridge-wide default removed on 2026-09-01.
import re as _re                                          # noqa: E402
_re13 = _re.compile(r"compact_pct\s*=\s*\d")
print("\n13. EVERY launch path passes the threshold, and none invents one")
print("    This case has now said three things, and the guard underneath")
print("    has not moved once: whatever the policy is, all the launch paths")
print("    must share it. The original incident was one path of six")
print("    forgetting to record what it had passed, so a window the bridge")
print("    had configured looked like somebody else's ever after. On")
print("    2026-09-01 nothing was passed at all and this asserted the empty")
print("    list. On 2026-09-02 the owner asked for a threshold again - as a")
print("    PROJECT setting - so the list must now be full instead, and a")
print("    path that quietly stops passing it is caught here. -> 8.2")
_lpaths = [daemon.ensure_session, daemon.handover, daemon.rotate_executor]
check("every launch path hands the project's percentage down",
      sorted(f.__name__ for f in _lpaths
             if "compact_pct=launch_pct(" in inspect.getsource(f)),
      sorted(f.__name__ for f in _lpaths))
print("    and NOT a number of its own: the only value any of them may")
print("    pass is what the project's config says")
check("no launch path names a percentage itself",
      [f.__name__ for f in _lpaths
       if _re13.search(inspect.getsource(f))], [])
print("    the record carries BOTH terms, because either alone is not a")
print("    threshold: a percentage without autoCompactWindow is inert (8.2)")
reset()
daemon.reg_pid(PATH, "executor", 4321, "sid-ac")
_rec = daemon.STATE["pids"]["%s|executor" % daemon.norm(PATH)]
check("reg_pid records the threshold and the window it multiplies with",
      ("autocompact" in _rec, "compact_window" in _rec), (True, True))
print("    and for a project that asks for nothing: no percentage, and the")
print("    window is the one the BRIDGE gave this role, named as such - since")
print("    2026-10-10 it is handed out per role (8.61), and until then this")
print("    field read the project's file, which is what two pairs' restart")
print("    loops decided from")
check("no percentage for a project that set none; the role's window, sourced",
      (_rec.get("autocompact"), _rec.get("compact_window"),
       _rec.get("compact_window_source")), (None, 222000, "role default"))
check("...and the control: it wrote the record it was asked for",
      _rec.get("pid"), 4321)
print("    with the control: the same sources DO still show a launch")
print("    happening, so the empty list above is not an empty read")
check("...the launch paths are still launch paths",
      sorted(f.__name__ for f in _lpaths
             if "sessions.launch(" in inspect.getsource(f)),
      sorted(f.__name__ for f in _lpaths))

print("\n15. a window the bridge did not start is not half of the pair")
check("executor is managed", daemon.managed("executor"), True)
check("planner is managed", daemon.managed("planner"), True)
check("the channel's fallback role is not", daemon.managed("unknown"), False)
check("nor is an empty role", daemon.managed(""), False)
reset()
out, _ = daemon.handle_event({"hook_event_name": "SessionStart",
                              "role": "unknown", "session_id": "zz11",
                              "project_dir": PATH, "cwd": PATH})
check("it is still recorded - hiding is a display decision, not an intake one",
      [v["managed"] for v in daemon.STATE["sessions"].values()
       if v.get("role") == "unknown"], [False])
check("and it is counted",
      list((daemon.STATE.get("strangers") or {}).get(daemon.norm(PATH), {})),
      ["zz11"])
out, _ = daemon.handle_event({"hook_event_name": "SessionStart",
                              "role": "", "session_id": "yy22",
                              "project_dir": PATH, "cwd": PATH})
check("and so is one with no role at all",
      sorted((daemon.STATE.get("strangers") or {}).get(daemon.norm(PATH), {})),
      ["yy22", "zz11"])
daemon.STATE["sessions"]["unknown:old"] = {"role": "unknown", "path":
                                           daemon.norm(PATH)}
daemon.STATE.setdefault("pids", {})["%s|unknown" % daemon.norm(PATH)] = {"pid": 9}
daemon.STATE.setdefault("channels", {})["%s|unknown" % daemon.norm(PATH)] = \
    {"port": 1234}
gone = daemon.forget_unmanaged()
check("stale records are cleared at startup", "unknown:old" in gone, True)
check("but the ports and pids of a live window are kept",
      "%s|unknown" % daemon.norm(PATH) in daemon.STATE["pids"], True)
check("channels are never touched",
      "%s|unknown" % daemon.norm(PATH) in daemon.STATE["channels"], True)

print("   and a role that arrives in the wrong case is still one of ours")
check("Executor", daemon.managed("Executor"), True)
check(" planner ", daemon.managed(" planner "), True)
check("PLANNER", daemon.managed("PLANNER"), True)

print("\n16. telegram failing is recorded, not swallowed")
reset()
daemon.telegram_note(False, "Unauthorized", 401)
h = daemon.STATE.get("telegram_health") or {}
check("health on record", h["ok"], False)
check("with the reason", h["why"], "Unauthorized")
check("and the code", h["code"], 401)
daemon.telegram_note(True)
check("and it clears", (daemon.STATE.get("telegram_health") or {})["ok"], True)

print("\n17. a config write can never empty the telegram credentials")
import json as _json                                     # noqa: E402
store.save_config({"telegram": {"token": "abc", "chat_id": "42"},
                   "projects": {}})
store.save_config({"projects": {}})                      # a partial writer
back = store.load_config()
check("token kept", back["telegram"]["token"], "abc")
check("pairing kept", back["telegram"]["chat_id"], "42")
store.save_config({"telegram": {"token": "new", "chat_id": "42"},
                   "projects": {}})
check("but a real change still goes through",
      store.load_config()["telegram"]["token"], "new")

print("\n18. the wall is shown with both ends and the room to each")
reset(compactions=[comp(790000, 300000)])
w = daemon.wall_view(sess(400000), PATH)
check("far end: window minus the 33k reserve", w["wall"], 967000)
check("near end: window minus the reported ~23%", w["wall_low"], 770000)
check("room to the far end", w["room_to_wall"], 567000)
check("room to the near end", w["room_to_wall_low"], 370000)
check("not measured here", w["wall_measured"], False)
print("   a 200k window has no second end - 33k is its own figure")
s200 = sess(100000)
s200["window"] = 200000
w2 = daemon.wall_view(s200, PATH)
check("one wall only", w2["wall_low"], None)
check("at window minus 33k", w2["wall"], 167000)
print("   and a wall actually hit replaces both")
cal = store.load_calibration()
cal[store.calib_key(MODEL, PATH)]["wall_history_tokens"] = 880000
store.save_calibration(cal)
w3 = daemon.wall_view(sess(400000), PATH)
check("measured wins", w3["wall"], 880000)
check("and the guess is dropped", w3["wall_low"], None)
check("marked as measured", w3["wall_measured"], True)

print("\n19. delivery and liveness consult the same witness")
import inspect                                           # noqa: E402,F811
dsrc = inspect.getsource(daemon.deliver_ex)
check("delivery falls back to the remembered port",
      "channel_for(path, role) or channel_alive(path, role)" in dsrc, True)
check("and checks that something is listening",
      "port_answers(port)" in dsrc, True)
check("absent and refused are told apart",
      '"absent"' in dsrc and '"failed"' in dsrc, True)

print("\n20. the channel waits longer than the bridge can take")
csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridgecore", "channel.py"), encoding="utf-8").read()
check("the task call gets its own timeout", "timeout=60" in csrc, True)
check("a timed-out call is not reported as a dead bridge",
      "do not say the bridge is down" in csrc, True)
check("and a delivery failure names the executor, not the bridge",
      "The bridge itself answered" in csrc, True)
dtask = inspect.getsource(daemon.Handler.do_POST)
check("the task endpoint returns the reason",
      '"why": None if sent else why' in dtask, True)
check("and names the executor's channel, not the bridge",
      "The bridge itself is fine" in dtask, True)

print("\n21. a window the status line stated does not flip when the")
print("    status line goes quiet and the transcript takes over")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {},
                     "pids": {"%s|planner" % daemon.norm(PATH):
                              {"pid": 1, "at": time.time(),
                               "registered": True, "model_req": "fable[1m]"}}})
daemon.touch_session({"role": "planner", "session_id": "pl",
                      "project_dir": PATH, "cwd": PATH},
                     window=200000, context_tokens=163000)
s = daemon.STATE["sessions"]["planner:pl"]
check("stated by the status line", daemon.known_window(PATH, "planner", s),
      (200000, "observed"))
s["window_observed"] = False                 # the transcript path used to do this
w, why = daemon.known_window(PATH, "planner", s)
check("still 200k, not the 1M of the launch alias", w, 200000)
check("and it says where it came from",
      "status line" in why or why == "observed", True)
check("it is remembered outside the record",
      (daemon.STATE["windows"]["%s|planner" % daemon.norm(PATH)]["tokens"]),
      200000)

print("\n22. a session's own compactions outrank the calibration file")
print("    - this is the planner that showed \'not known / not computable\'")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {},
    "compactions": {"%s|planner" % daemon.norm(PATH):
                    [{"at": "x", "tokens": 150000, "after": 65000,
                      "session": "pl-1"}]},
    "last_session": {"%s|planner" % daemon.norm(PATH): "pl-1"},
    "pids": {"%s|planner" % daemon.norm(PATH):
             {"pid": 1, "at": time.time(), "registered": True,
              "autocompact": None, "model_req": "fable"}}})
cal = store.load_calibration()
cal[store.calib_key("fable 5", PATH)] = {
    "ceiling_pct": 92.0, "buffer_tokens": 33000, "misses": 0,
    "clean_streak": 0, "multiplier": 1.5, "wall_history_tokens": None,
    "compact_at_tokens": 150000, "compact_at_window": 1000000,
    "how": "stamped with a window this session is not on"}
store.save_calibration(cal)
pl = {"role": "planner", "path": daemon.norm(PATH), "session_id": "pl-1",
      "model": "Fable 5", "window": 200000, "window_observed": True,
      "context_tokens": 168000, "turn_costs": [4000, 5000, 4000]}
daemon.STATE["sessions"]["planner:pl-1"] = pl
w = daemon.wall_view(pl, PATH)
check("the point comes from its own record", w["compact"], 150000)
check("not from the mis-stamped calibration",
      "this session was seen compacting" in w["compact_source"], True)
lv = daemon.life_view(pl, PATH)
check("so the cycle is computable", lv["cycle"], 150000 - 65000)
check("and the distance to the wall too", lv["left"], 0 + 3 * 85000)
check("in turns", lv["turns_left"], 255000 // 4333)
# 1b's exception is worth ONE TURN, and since 2026-08-31 that is one turn OF
# THIS PAIR, measured, rather than the module literal LARGEST_TURN_SEEN
# (200 274) that used to make every distance under 200k qualify. So the case
# states the history it argues from: a planner whose last three turns run
# 4-5k, with one 20 000 turn earlier in its life. 168 000 - 150 000 = 18 000
# past the point, and one 20 000 turn can be an overshoot of that.
for _c22 in (4000, 5000, 20000, 4000):
    daemon.note_turn_cost(PATH, "planner", _c22, "pl-1")
check("the exception is worth one turn of THIS pair, measured",
      daemon.turn_widest(PATH, "planner"), (20000, "measured"))
check("plan is the routine one", daemon.plan_for(pl, PATH)["do"], "compacting")
print("   and the literal was hiding the other half of this same rule. Take")
print("   the SAME session on a pair that has never taken a turn wider than")
print("   5k: 18 000 past the point is four turns past it, an overshoot")
print("   cannot be that wide, so the point is refuted and the session is")
print("   replaced. Under LARGEST_TURN_SEEN that pair got the exception too,")
print("   because 18 000 < 200 274 - and so did every pair alive, whatever")
print("   its turns actually cost")
daemon.STATE["turns"].pop("%s|planner" % daemon.norm(PATH), None)
for _c22 in (4000, 5000, 4000):
    daemon.note_turn_cost(PATH, "planner", _c22, "pl-1")
check("the same session, on a pair whose turns are small, is replaced",
      daemon.plan_for(pl, PATH)["do"], "handover")
check("and the width it was judged by is that pair's own",
      daemon.turn_widest(PATH, "planner"), (5000, "measured"))

print("\n23. the channel answers the daemon without waiting on the pipe")
print("   it used to answer BEFORE the write was attempted, full stop, and")
print("   the daemon read that 200 as delivery. On 2026-08-23 a window")
print("   stopped draining its pipe for 2h07m and every report inside that")
print("   was journalled 'delivered to the channel'. So the answer now")
print("   carries whether the SESSION took it - after a bounded wait that")
print("   must stay far below the daemon's own 20 s delivery timeout")
import json as _json23
import threading as _thr23
import urllib.request as _url23
from http.server import ThreadingHTTPServer as _THS23
from bridgecore import channel as _ch23
# A client whose handshake is over: the outbox holds everything until then
# (8.134), and this case is about a pipe that stops being drained AFTER it.
# Read, not required - a tree before 8.134 writes at once.
getattr(_ch23, "mark_ready", lambda: None)()
csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridgecore", "channel.py"), encoding="utf-8").read()
check("inbound events are queued, not written inline",
      "_outbox.put_nowait" in csrc, True)
check("a writer thread drains them", "_drain_outbox" in csrc, True)
check("the wait for the write is bounded and small",
      0 < _ch23.WRITE_WAIT <= 5.0, True)
_hold23 = _thr23.Event()
_real23 = _ch23.rpc_write
_ch23.rpc_write = lambda obj: _hold23.wait(30)
_sec23, _ch23.SECRET = _ch23.SECRET, "suite-secret"
_srv23 = _THS23(("127.0.0.1", 0), _ch23.Inbound)
_thr23.Thread(target=_srv23.serve_forever, daemon=True).start()
_thr23.Thread(target=_ch23._drain_outbox, daemon=True).start()
try:
    _req23 = _url23.Request(
        "http://127.0.0.1:%d/" % _srv23.server_address[1],
        data=_json23.dumps({"content": "report", "meta": {"kind": "report"}}
                        ).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Bridge-Secret": "suite-secret"})
    _t23 = time.time()
    _body23 = _json23.loads(_url23.urlopen(_req23, timeout=30).read()
                         .decode("utf-8"))
    _took23 = time.time() - _t23
    check("the POST is answered while the pipe is still blocked",
          _took23 < _ch23.WRITE_WAIT + 3.0, True)
    check("it says the event was taken", _body23.get("ok"), True)
    check("and that the session has NOT read it", _body23.get("written"),
          False)
    check("naming how many are waiting", _body23.get("backlog") >= 1, True)
    print("   the sabotage: unblock the pipe and ask again - the same field")
    print("   must answer the other way, or it could never fail")
    _hold23.set()
    _ch23.rpc_write = lambda obj: None
    _body23b = _json23.loads(_url23.urlopen(_req23, timeout=30).read()
                          .decode("utf-8"))
    check("a draining session is reported as having read it",
          _body23b.get("written"), True)
finally:
    _ch23.rpc_write = _real23
    _ch23.SECRET = _sec23
    _hold23.set()
    _srv23.shutdown()

print("\n24. the record the channel makes carries the flag too")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}})
for role, want in (("executor", True), ("unknown", False)):
    daemon.STATE["sessions"]["%s:channel" % role] = {
        "role": role, "path": daemon.norm(PATH), "managed": daemon.managed(role)}
    check("%s record" % role,
          daemon.STATE["sessions"]["%s:channel" % role]["managed"], want)
psrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridgecore", "panel.html"), encoding="utf-8").read()
check("the panel hides by role, not only by the flag",
      'r==="executor"||r==="planner"' in psrc, True)
check("and it is applied to every session row", psrc.count("ours(s)"), 4)

print("\n25. a turn cost that has not been measured is not printed as None")
dsrc = inspect.getsource(daemon.plan_for)
check("says so instead", "turn cost not measured yet" in dsrc, True)

print("\n26. telegram tells a dead token from a dead connection")
tsrc = inspect.getsource(daemon.telegram_note)
check("auth failures point at the panel",
      "The token is rejected" in tsrc, True)
check("connection failures do not",
      "api.telegram.org" in tsrc and "retries by itself" in tsrc, True)

print("\n27. /task answers before it delivers")
print("    verdicts always worked and tasks never did, because one")
print("    endpoint returns at once and the other waited up to 20s")
dsrc = inspect.getsource(daemon.Handler.do_POST)
check("reachability is checked, not the injection",
      "task_reachable(path)" in dsrc, True)
check("the injection is handed to a thread",
      "target=deliver_task_later" in dsrc, True)
check("and nothing blocking is left on the request path",
      "deliver_ex(path, \"executor\"" in dsrc, False)
tsrc = inspect.getsource(daemon.task_reachable)
check("the check is a port probe with a short timeout",
      "port_answers(int(ch[\"port\"]), timeout=1.5)" in tsrc, True)
lsrc = inspect.getsource(daemon.deliver_task_later)
check("delivery retries", "for attempt in range(1, tries + 1)" in lsrc, True)
check("and a final failure reaches the inbox and the human",
      "inbox_write" in lsrc and "needs_you" in lsrc, True)

print("\n28. a planner that can hear but cannot call is detected")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "asks": {}, "toolbroken": {},
                     "channels": {"%s|planner" % daemon.norm(PATH):
                                  {"port": 1, "at": time.time()}}})
daemon.CHANNELS[(daemon.norm(PATH), "planner")] = {"port": 1,
                                                   "ts": time.time()}
check("one ask is not a diagnosis", daemon.note_ask(PATH), 1)
check("and nothing is concluded", daemon.tool_path_broken(PATH), False)
check("two asks", daemon.note_ask(PATH), 2)
check("now it is", daemon.tool_path_broken(PATH), True)
check("recorded", bool((daemon.STATE.get("toolbroken") or {}).get(
    daemon.norm(PATH))), True)
daemon.note_task_arrived(PATH)
check("a task arriving clears it",
      (daemon.STATE.get("toolbroken") or {}).get(daemon.norm(PATH)), None)
check("and resets the count",
      (daemon.STATE.get("asks") or {}).get(daemon.norm(PATH)), None)
asrc = inspect.getsource(daemon.assess)
check("and the planner is told to answer by verdict instead",
      "Do not use the task tool for this one" in asrc, True)

print("\n29. a verdict reaches an executor that is idle at its prompt")
vsrc = inspect.getsource(daemon.Handler.do_POST)
check("it re-arms the loop like a task does",
      "work as a verdict" in vsrc, True)
check("answers before delivering", vsrc.count("task_reachable(path)"), 2)
check("and delivers on a thread",
      vsrc.count("target=deliver_task_later"), 2)

print("\n30. the planner can start the loop it stopped")
csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridgecore", "channel.py"), encoding="utf-8").read()
check("there is a loop tool", '"name": "loop"' in csrc, True)
check("with start and stop", '"enum": ["start", "stop"]' in csrc, True)
check("and the planner is told when to use it",
      "call loop with 'start' first" in csrc, True)
print("   a stop verdict is still the only thing that switches it off")
check("stop still ends the run", "stop = the whole job is" in csrc, True)

print("\n31. a task with nothing in it is not refused in silence")
check("the text is taken under whatever key it arrived",
      'for key in ("instructions", "task", "text", "work", "message")'
      in csrc, True)
check("and an empty one gets an answer it can act on",
      "Call task again with the whole instruction" in csrc, True)
dsrc = inspect.getsource(daemon.Handler.do_POST)
check("the bridge says the loop state in the refusal",
      "task turns it back on" in dsrc, True)

print("\n32. the wall distance never silently equals the distance to the")
print("    next compaction - a real planner at 736k of 1M,")
print("    0 of 5 compactions, shown as 64k and a 92% red bar")
reset(compact_at=None)                      # no floor: nothing has compacted
lv = daemon.life_view(sess(736000, costs=(9000, 9000, 9000)), PATH)
check("64k is the distance to the FIRST compaction", lv["rest_of_cycle"],
      64000)
check("and it is not published as the distance to the fifth",
      lv.get("left"), None)
check("four cycles are still to come", lv["later_cycles"], 4)
check("and they are named as unsized, not summed as zero",
      lv["sizeable"], False)
check("so there is no percentage to redden a bar with", lv.get("pct"), None)
check("nor a turn count off the wrong number", lv.get("turns_left"), None)
check("the turns that are known belong to this cycle", lv["rest_turns"], 7)
rep = daemon.state_report(PATH, "planner",
                          sess(736000, costs=(9000, 9000, 9000)),
                          "headline", "carry on")
check("the report says which compaction the 64k reaches",
      "1st compaction" in rep, True)
check("and calls that one routine", "which is routine" in rep, True)
check("and does not repeat the reason as a note afterwards",
      rep.count("cannot be sized"), 1)
psrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridgecore", "panel.html"), encoding="utf-8").read()
check("the panel has a branch for it", "L0.sizeable===false" in psrc, True)
check("the one-bar-from-a-total is gone from both branches",
      # This used to read "still only the sizeable branch": one bar drawn
      # from a total where a total was known, segments where it was not.
      # The presentational fix of 2026-08-11 made BOTH branches segmented,
      # which is what this case was protecting in the first place - a bar
      # whose fill means one thing here and another there is the fault it
      # was written about. Revised to the stronger statement: no bar to
      # the wall is drawn from a total anywhere.
      psrc.count("meter(lp"), 0)
check("and the sizeable branch draws segments like the other one",
      psrc.count("segbar(L0.budget,L0.done"), 2)
print("    it still gets a bar - the owner watches it - but a segmented one:")
print("    one segment per compaction to the wall, the current one filled")
print("    by measured progress, the rest hatched because they are not")
branch = psrc[psrc.index("}else if(L0&&L0.sizeable===false){"):
              psrc.index("}else if(L0&&L0.why_blank){")]
check("the branch draws the segmented bar",
      "segbar(L0.budget,L0.done,cf" in branch, True)
check("there is a segmented bar to draw", "function segbar(" in psrc, True)
check("the current segment is filled from measured terms only",
      "var cf=(w.compact&&w.used!=null)?(w.used/w.compact):null;" in branch,
      True)
check("future segments are marked as unmeasured, not as empty room",
      'class="seg future"' in psrc, True)
check("and they are hatched rather than filled", '"hatch"' in psrc, True)
check("no percentage is computed from a total that does not exist",
      "L0.pct" in branch, False)
check("and nothing in it is red", "--red" in branch, False)

print("\n33. one floor sizes the later cycles but cannot see the climb,")
print("    so the projection is kept and labelled an estimate")
reset(compactions=[comp(999000, 58000)], compact_at=None)
lv = daemon.life_view(sess(371000, costs=(9150, 9150, 9150)), PATH)
check("the projection is kept", lv["left"], 628000 + 3 * 941000)
check("it is a whole distance", lv["sizeable"], True)
check("the climb is not known from one floor", lv["rise"], None)
check("so it is marked an estimate", lv["estimated"], True)
check("and the reason is carried with it",
      "two floors" in lv["why_partial"], True)
print("    two floors measure the climb, and then it is not an estimate")
reset(compactions=[comp(999000, 58000), comp(999000, 108000)],
      compact_at=None)
lv = daemon.life_view(sess(371000, costs=(9150, 9150, 9150)), PATH)
check("the climb is measured", lv["rise"], 50000)
check("nothing is estimated", lv["estimated"], False)
check("the panel labels from the flag, not from the note",
      "L0.estimated?' &middot; <span class=\"warn\">estimate</span>'"
      .replace("&middot;", "·") in psrc, True)

print("\n34. a tool result is content blocks and nothing else")
print("    every task and verdict call today returned a second item that")
print("    was the loop tool's own definition - no 'type', so the client")
print("    rejected the result of a call that had actually worked")
VALID = ("text", "image", "audio", "resource_link", "resource")


def _blocks(name, args, replies):
    import bridgecore.channel as ch
    real, ch.post_daemon = ch.post_daemon, lambda *a, **k: replies
    try:
        r = ch.handle_request({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
    finally:
        ch.post_daemon = real          # never leave the stub in place
    return ((r.get("result") or {}).get("content") or [])


def _blocks_result(name, args, replies):
    """The whole result object, not just its content blocks - isError lives
    on the result, and it is the difference between a refusal the caller
    cannot miss and one it reads as a confirmation."""
    import bridgecore.channel as ch
    real, ch.post_daemon = ch.post_daemon, lambda *a, **k: replies
    try:
        r = ch.handle_request({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
    finally:
        ch.post_daemon = real
    return r.get("result") or {}


for tool, args in (("verdict", {"verdict": "done"}),
                   ("task", {"instructions": "do a thing"}),
                   ("loop", {"action": "start"}),
                   ("check", {"suite": "archive"})):
    blocks = _blocks(tool, args, {"ok": True, "delivered": True})
    check("%s returns exactly one block" % tool, len(blocks), 1)
    check("%s block is a valid content type" % tool,
          [b.get("type") for b in blocks if isinstance(b, dict)], ["text"])
    check("%s block carries text" % tool,
          all(isinstance(b.get("text"), str) and b["text"] for b in blocks),
          True)
# Counted against what the module actually declares, not against a list
# written out here: the hardcoded triple broke the moment a fourth tool was
# added, which is a suite failing on its own bookkeeping rather than on the
# thing it was asked to watch. This still fails on a schema written twice,
# or on a tool declared without one.
import bridgecore.channel as _ch                            # noqa: E402
check("a schema appears once per declared tool and nowhere else",
      csrc.count("inputSchema"), len(_ch.TOOLS))
check("and the planner has the four tools it is told about",
      sorted(x["name"] for x in _ch.TOOLS),
      ["check", "loop", "task", "verdict"])

print("\n35. one definition of carried context, whatever reads it")
print("    the transcript path used to add output_tokens while the status")
print("    line did not, and both wrote to the same field - so a turn cost")
print("    could be the difference between two different quantities")
from bridgecore import archive, sessions                        # noqa: E402
tdir = os.path.join(TMP, "transcripts")
os.makedirs(tdir, exist_ok=True)
tpath = os.path.join(tdir, "carried.jsonl")
LAST = {"input_tokens": 7, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 120000, "output_tokens": 4096,
        "cache_creation": {"ephemeral_1h_input_tokens": 300,
                           "ephemeral_5m_input_tokens": 0},
        "iterations": [{"input_tokens": 7, "output_tokens": 4096,
                        "cache_read_input_tokens": 120000,
                        "cache_creation_input_tokens": 300}]}
with open(tpath, "w", encoding="utf-8") as fh:
    fh.write(_json.dumps({"type": "assistant",
                          "timestamp": "2026-08-03T09:00:00Z",
                          "message": {"role": "assistant", "model": "opus",
                                      "content": [{"type": "text",
                                                   "text": "x"}],
                                      "usage": LAST}}) + "\n")
NAMED = 7 + 300 + 120000
u = sessions.usage_from_transcript(tpath)
check("the transcript gives the three named fields", u["context_tokens"],
      NAMED)
check("output is not folded in", u["context_tokens"] == NAMED + 4096, False)
check("it is reported under its own name", u["last_output_tokens"], 4096)
check("and the fields are named", u["token_fields"],
      list(store.CARRIED_CONTEXT_FIELDS))
print("    the status line, the transcript and the archive map all agree")
check("status line path", daemon._tokens({"current_usage": LAST}), NAMED)
check("archive path", archive.carried_tokens(LAST)[0], NAMED)
check("same file, same number",
      archive.scan_file(tpath, {})["carried_tokens"], u["context_tokens"])
check("one tuple behind all three",
      (daemon.INPUT_TOKEN_FIELDS, archive.INPUT_TOKEN_FIELDS),
      (store.CARRIED_CONTEXT_FIELDS, store.CARRIED_CONTEXT_FIELDS))
print("    a usage block with no named field is nothing to read, not zero")
check("nothing to read", store.carried_from_usage({"output_tokens": 9}),
      (None, []))

print("\n36. the panel speaks about the project it is showing, throughout")
print("    two failures, one cause. At the 11:17 restart the headline read")
print("    'running - loop on' over a subtitle reading 'the loop is OFF':")
print("    the subtitle looked up CUR before CUR had been defaulted. And the")
print("    headline itself came from a global read - the anyLoop 9 rejects,")
print("    which with two projects claims LOOP ON while showing the other")
psrc2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bridgecore", "panel.html"), encoding="utf-8").read()
head = psrc2[psrc2.index("function renderPanel(){"):
             psrc2.index('$("#stateSub").textContent=')]
check("the project is settled before anything is drawn from it",
      "if(!CUR&&allProj.length)CUR=allProj[0];" in head, True)
check("and it is not defaulted a second time further down",
      psrc2.count("CUR=allProj[0]"), 1)
print("    one read of that project, and every part of the panel off it")
check("the read is taken once", "var sc=scopeOf();" in head, True)
check("there is one to take", "function scopeOf(){" in psrc2, True)
check("the headline is derived from it",
      '$("#stateTitle").textContent=headlineOf(sc);' in head, True)
check("the state machine is asked about it too", "stateOf(sc)" in head, True)
check("the subtitle reads the same loop flag",
      '$("#stateSub").textContent=(liveN&&!sc.loop)' in psrc2, True)
check("and the same live count", "var liveN=sc.live.length;" in head, True)
check("the start-the-loop button too", "if(CUR&&!sc.loop&&" in psrc2, True)
print("    so the rejected global reads are gone from the panel entirely")
check("nothing reads the loops of every project at once",
      "D.state.loops[p].active" in psrc2, False)
check("the only anyLoop left is the comment saying why there is none",
      psrc2.count("anyLoop"), 1)
check("and the panel no longer renders the global string",
      "D.headline" in psrc2, False)
print("    telegram still gets ONE line, because a pin has no project on")
print("    screen to be about - but it stopped being a line that speaks for")
print("    every pair at once. Revised deliberately in step 5 of")
print("    PLAN-multipair.md: the rule was never 'one word for the bridge',")
print("    it was 'no claim without the project it is about'. One message,")
print("    every project named in it.")
hsrc = inspect.getsource(daemon.status_headline)
check("it is still one line, and still what the pin is built from",
      'status_headline()' in inspect.getsource(daemon.pinned_text), True)
check("but every project in it is named",
      'project_name(path)' in hsrc, True)
check("and each one is asked about itself",
      'project_headline(path)' in hsrc, True)
check("the two answers that are about the whole bridge stay whole-bridge",
      ('interrupted - open the resume tab' in hsrc,
       'session down - ' in hsrc), (True, True))
check("the payload still carries it for anyone who wants it",
      '"headline": status_headline(),'
      in inspect.getsource(daemon.Handler.do_GET), True)

print("\n37. a pair is held on its own, not by holding the bridge")
print("    a dead executor in one folder used to set mode=paused, which")
print("    stopped reviewing finished turns in EVERY other folder. The")
print("    other pairs were working, so nothing looked broken and nothing")
print("    said why their reports had stopped being carried")
PATH_B = os.path.join(TMP, "proj-b")
os.makedirs(PATH_B, exist_ok=True)
A, B = daemon.norm(PATH), daemon.norm(PATH_B)
daemon.STATE.clear()
daemon.STATE.update({"mode": "running", "sessions": {}, "paused": {},
                     "note": {},
                     "loops": {A: {"active": True, "iteration": 3},
                               B: {"active": True, "iteration": 7}}})
daemon.pause_project(PATH, "you paused this project")
check("the project asked for is held", daemon.paused_for(PATH), True)
check("the other one is not", daemon.paused_for(PATH_B), False)
check("and the bridge as a whole was never paused",
      daemon.STATE.get("mode"), "running")
check("the hold says who put it on",
      (daemon.STATE["paused"][A] or {}).get("why"),
      "you paused this project")
ssrc = inspect.getsource(daemon.situation)
check("the loop's own view asks about the project, not the mode",
      'paused_for(path)' in ssrc, True)
check("and so does the gate that holds a report",
      inspect.getsource(daemon.handle_event).count("paused_for(path)"), 1)
check("lifting it lifts only it", daemon.resume_project(PATH), True)
check("held nowhere now",
      (daemon.paused_for(PATH), daemon.paused_for(PATH_B)), (False, False))
print("   a window dying holds its own pair and leaves the others working -")
print("   in a project that does not raise its halves on its own; one that")
print("   does is not paused at all, the half comes back (8.56, 36.4)")
daemon.STATE["down"] = {}
real_notify, daemon.notify = daemon.notify, lambda *a, **k: "log"
_projects37 = daemon.CFG.get("projects", KeyError)
_rs37o, _rs37 = daemon.restart_session, []
daemon.restart_session = lambda *a, **k: _rs37.append(a[:2]) or {"ok": True}
try:
    daemon.CFG["projects"] = {A: {"auto_restart_dead_sessions": True}}
    daemon.handle_session_death(A, "executor", None)
    check("a project that raises: the half is raised, the pair not held",
          (_rs37, daemon.paused_for(PATH)), ([(A, "executor")], False))
    daemon.STATE["down"] = {}
    daemon.CFG["projects"] = {A: {"auto_restart_dead_sessions": False}}
    daemon.handle_session_death(A, "executor", None)
finally:
    daemon.notify = real_notify
    daemon.restart_session = _rs37o
    if _projects37 is KeyError:
        daemon.CFG.pop("projects", None)
    else:
        daemon.CFG["projects"] = _projects37
check("the pair whose window died is held", daemon.paused_for(PATH), True)
check("the other pair keeps working", daemon.paused_for(PATH_B), False)
check("the bridge is still running", daemon.STATE.get("mode"), "running")
check("and the hold is marked as one the bridge put on itself",
      (daemon.STATE["paused"][A] or {}).get("by_death"), True)
print("   the five-hour limit is still a property of the account, so it")
print("   still holds every pair - that one must NOT become per project")
daemon.STATE["paused"] = {}
daemon.STATE["mode"] = "paused"
check("a bridge-wide pause covers both",
      (daemon.paused_for(PATH), daemon.paused_for(PATH_B)), (True, True))
lsrc = inspect.getsource(daemon.handle_status)
# The flag became a record with its kind in piece 11-bis, so the
# account's own limits can hold the bridge through the same pause and the
# percentage lift can tell the five-hour one from theirs (DECISIONS 8.28).
check("the limit sets the bridge-wide one, not a project's",
      'STATE["paused_by_limit"] = {"kind": "five_hour",' in lsrc
      and "pause_project" not in lsrc, True)
print("   resume with nothing named is the everything-back-to-normal button")
daemon.STATE["mode"] = "running"
daemon.pause_project(PATH, "by hand")
daemon.handle_cmd({"cmd": "resume"})
check("so it lifts the individual holds too", daemon.STATE.get("paused"), {})

print("\n38. a note is left for a pair, and reaches that pair only")
print("    one string for the whole bridge meant the note reached whichever")
print("    project finished a turn first - and was wiped, so the pair it")
print("    was written for never saw it")
daemon.STATE.clear()
daemon.STATE.update({"mode": "running", "sessions": {}, "paused": {},
                     "note": {},
                     "loops": {A: {"active": True, "iteration": 0},
                               B: {"active": True, "iteration": 0}}})
daemon.set_note(PATH, "check the migration first")
check("it is stored against its own project",
      daemon.note_for(PATH), "check the migration first")
check("and against no other", daemon.note_for(PATH_B), "")
check("two projects and no addressee is refused, not guessed",
      daemon.handle_cmd({"cmd": "note", "text": "for whom?"}).get("ok"),
      False)
check("the note that was already there is untouched by the refusal",
      daemon.note_for(PATH), "check the migration first")

thr = daemon.CFG.setdefault("thresholds", {})
kept = dict(thr)
thr.update({"review_timeout": 0.4, "channel_silence_warn": 0.2})
delivered = []
real_dx, daemon.deliver_ex = daemon.deliver_ex, \
    lambda p, r, c, m: (delivered.append((p, c)), (True, ""))[1]
real_notify, daemon.notify = daemon.notify, lambda *a, **k: "log"
try:
    # the verdict never arrives, so this returns as soon as the (tiny)
    # review timeout is up - long enough to see what was sent
    daemon.run_review({}, B, daemon.STATE["loops"][B], "B finished a turn",
                      "proj-b", "executor")
finally:
    daemon.deliver_ex, daemon.notify = real_dx, real_notify
    thr.clear()
    thr.update(kept)
sent_to_b = "".join(c for _, c in delivered)
check("the other pair's report went out", "B finished a turn" in sent_to_b,
      True)
check("without the note that was not for it",
      "check the migration first" in sent_to_b, False)
check("and the note is still waiting for the pair it was written for",
      daemon.note_for(PATH), "check the migration first")
check("taking it hands it over once", daemon.take_note(PATH),
      "check the migration first")
check("and only once", daemon.take_note(PATH), "")
print("   a session starting reads the note, it does not eat it - a")
print("   rotation must not swallow the line written for the review")
esrc = inspect.getsource(daemon.handle_event)
check("the seed reads", "note_for(path)" in esrc, True)
check("and does not take", "take_note" in esrc, False)
check("the report is what takes it",
      "take_note(path)" in inspect.getsource(daemon.run_review), True)
print("   and everything that offers to leave one says who it is for,")
print("   or admits it did not leave one")
check("the panel's note box names the project it is showing",
      'cmd:"note",text:$("#noteInput").value,project:CUR' in psrc2, True)
print("   step 1 left a stopgap here - /note answered with whatever the")
print("   daemon returned instead of saying 'noted' over a refusal. Step 6")
print("   replaced it with real addressing, so the assertion moved with it:")
print("   the answer is built where the command is now understood")
tgsrc = inspect.getsource(daemon.run_telegram_command)
check("a refused note says so rather than claiming it was taken",
      "not noted" in tgsrc, True)
check("and a taken one names the pair it was taken for",
      "goes to the planner with the next" in tgsrc, True)
check("nothing about a command is decided inside the polling loop any more",
      "handle_cmd(" in inspect.getsource(daemon.telegram_poll), False)

print("\n39. a note written before notes had an addressee does not stop the")
print("    bridge from starting")
print("    migrate_keys re-keys dictionaries and skips everything else, so")
print("    listing 'note' in PATH_KEYED leaves an old STRING in place and")
print("    the first .get(path) on it raises")
check("both are re-keyed with the paths", ("paused" in daemon.PATH_KEYED,
                                           "note" in daemon.PATH_KEYED),
      (True, True))
daemon.STATE.clear()
daemon.STATE.update({"mode": "running", "note": "  finish the archive map  "})
check("the old text is handed back, not swallowed in silence",
      daemon.migrate_note(), "finish the archive map")
check("and what is left is the per-project form", daemon.STATE["note"], {})
check("nothing is delivered to a pair that may not be the right one",
      daemon.note_for(PATH), "")
check("reading it now is safe", daemon.take_note(PATH), "")
daemon.STATE["note"] = ""
check("an empty one converts with nothing to report",
      daemon.migrate_note(), None)
check("and converts", daemon.STATE["note"], {})
check("a converted state is left alone the second time",
      daemon.migrate_note(), None)
check("main converts before it re-keys",
      inspect.getsource(daemon.main).index("migrate_note()") <
      inspect.getsource(daemon.main).index("migrate_keys()"), True)

print("\n40. a handover's arithmetic knows which pair it belongs to")
print("    the log is one list for the whole bridge and the panel shows its")
print("    newest row under the gauges of the project on screen - so with")
print("    two pairs it showed the other one's numbers, unlabelled")
reset(compactions=[comp(1002000, 760000)], compact_at=1002000)
s40 = sess(770000)
rowA = daemon.log_handover_decision(PATH, "executor", s40,
                                    daemon.plan_for(s40, PATH))
rowB = daemon.log_handover_decision(PATH_B, "planner", s40,
                                    daemon.plan_for(s40, PATH))
check("the row carries its project", rowA.get("path"), daemon.norm(PATH))
check("in the canonical form, like every other key",
      daemon.log_handover_decision(PATH.upper(), "executor", s40,
                                   daemon.plan_for(s40, PATH))["path"],
      daemon.norm(PATH))
hist = daemon.STATE.get("handover_log") or []
mine = [r for r in hist if r.get("path") == daemon.norm(PATH)]
theirs = [r for r in hist if r.get("path") == daemon.norm(PATH_B)]
check("this project's rows are found by it", len(mine), 2)
check("the other pair's are not among them", len(theirs), 1)
check("and the newest of this project is not the newest overall",
      (mine[-1] is not hist[-1], hist[-1] is mine[-1]), (False, True))
check("the role is still on the row", rowB.get("role"), "planner")
print("   one list, several pairs: a busy pair must not push a quiet one's")
print("   only row out of the window the panel reads")
for _ in range(45):
    daemon.log_handover_decision(PATH_B, "executor", s40,
                                 daemon.plan_for(s40, PATH))
check("the log is bounded", len(daemon.STATE["handover_log"]), 40)
check("which is more than the one pair's worth it used to keep",
      len(daemon.STATE["handover_log"]) > 20, True)
print("   a row written before rows carried a project is not shown as this")
print("   project's - attribution is never guessed, and never silently lost")
psrc40 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bridgecore", "panel.html"), encoding="utf-8").read()
check("the panel filters the log by the project it is showing",
      "hlAll.filter(function(r){return r.path&&forCur(r.path)})" in psrc40,
      True)
check("the unfiltered read of the whole bridge's log is gone",
      "var hl=(D.state.handover_log||[]);" in psrc40, False)
check("what is drawn is the newest row of what survived the filter",
      "var h=hl[hl.length-1];" in psrc40, True)
check("and an unattributed row is counted and named, not dropped in silence",
      "recorded before handovers " in psrc40, True)
print("   reading an old row must not raise, whatever is missing from it")
daemon.STATE["handover_log"] = [{"at": "2026-08-01 10:00:00",
                                 "role": "executor", "why": "old"}]
old = (daemon.STATE["handover_log"] or [])[-1]
check("an old row simply has no project", old.get("path"), None)
check("and is therefore not this project's",
      [r for r in daemon.STATE["handover_log"]
       if r.get("path") == daemon.norm(PATH)], [])

print("\n41. one definition of the canonical path, for everything keyed on it")
print("    sessions.py keyed its live process handles with normpath alone,")
print("    which folds separators but not case - so launch() recorded a")
print("    window under one spelling and stop()/alive() looked under another")
check("the daemon's norm is the store's", daemon.norm is store.norm, True)
print("   nothing in, nothing out: normpath('') answers '.', a real folder -")
print("   the one the daemon runs in. An /archive-search naming no project")
print("   reached isdir('.') and searched the bridge's own directory, and")
print("   the 'fall back to the first project' branch behind it never ran")
check("an empty path canonicalises to nothing",
      [daemon.norm(v) for v in ("", None)], ["", ""])
check("so it is falsy, and a missing project cannot pass for a real one",
      any(bool(daemon.norm(v)) for v in ("", None)), False)
# folded as the store folds it: lower case on a Mac too (8.89)
check("a real path is untouched by that",
      daemon.norm(PATH) == (lambda c: c.lower() if getattr(
          daemon.store, "CASELESS", False) else c)(
          os.path.normcase(os.path.normpath(PATH))), True)
check("and the endpoint refuses instead of guessing",
      # a contiguous fragment: the sentence is wrapped across two string
      # literals in the source, and asserting across the join tests the
      # line wrapping rather than the code
      "sensible one to guess" in
      inspect.getsource(daemon.Handler.do_POST), True)
check("and sessions uses that one too",
      "store.norm(project)" in inspect.getsource(sessions.launch), True)
check("stopping looks it up the same way",
      "store.norm(project)" in inspect.getsource(sessions.stop), True)
check("so does the liveness check",
      "store.norm(project)" in inspect.getsource(sessions.alive), True)
check("and past sessions compare paths the same way",
      "store.norm(meta[\"cwd\"]) != store.norm(project)"
      in inspect.getsource(sessions.past_sessions), True)
print("   the transcript folder name is the one path that must NOT be folded:")
print("   it reproduces a name Claude Code wrote, case and all")
tsrc = inspect.getsource(sessions.transcript_of)
check("it still uses normpath", "os.path.normpath(cwd)" in tsrc, True)
check("with the reason written down next to it",
      "Deliberately NOT store.norm" in tsrc, True)

print("\n42. reading a line from the chat is separate from acting on it")
print("    it used to be one block inside the long-poll loop, so the only")
print("    way to reach the code that answers a verdict was to have Telegram")
print("    deliver a real update - which is why the bug that made /verdict")
print("    answer EVERY waiting project at once sat there unnoticed")
pc = daemon.parse_command
check("a plain sentence is not a command, and that is not an error",
      (pc("morning")["cmd"], pc("morning")["error"]), (None, ""))
check("an empty line likewise", pc("")["cmd"], None)
check("the slash form", pc("/status")["cmd"], "status")
check("and the bare word, because both get typed",
      pc("status")["cmd"], "status")
check("a verdict carries its word and its feedback",
      [(pc("/verdict continue fix the parser")[k])
       for k in ("cmd", "verdict", "text")],
      ["verdict", "continue", "fix the parser"])
check("the four verdict words and no others", daemon.VERDICT_WORDS,
      ("continue", "done", "wait", "stop"))
check("a word that is not one of them is refused, not guessed",
      ("is not a verdict" in pc("/verdict finished now")["error"],
       pc("/verdict finished now")["verdict"]), (True, None))
check("a verdict with nothing after it says what to say",
      "say which verdict" in pc("/verdict")["error"], True)
print("   an address is @name and nothing else: working out whether the")
print("   first word is a project or part of the command holds right up")
print("   until somebody has a project called 'done'")
check("the address is taken off the front",
      [pc("/verdict @godot done nice")[k] for k in ("addr", "verdict",
                                                    "text")],
      ["godot", "done", "nice"])
check("without one there is no address, not a guessed one",
      pc("/verdict done nice")["addr"], None)
check("a bare @ is refused",
      "nothing after the @" in pc("/verdict @ done")["error"], True)
check("a note keeps its text whole",
      [pc("/note @bridge look at the parser")[k] for k in ("addr", "text")],
      ["bridge", "look at the parser"])
_before = _json.dumps(daemon.STATE, sort_keys=True, default=str)
for _line in ("/verdict @proj stop done here", "/note @proj hello",
              "/rotate @proj", "/pause", "not a command at all"):
    pc(_line)
check("and it is pure - reading a line changes nothing",
      _json.dumps(daemon.STATE, sort_keys=True, default=str) == _before, True)
check("nor does it reach for a waiter", list(daemon.PENDING), [])

print("\n43. which pair a chat command is for")
daemon.CFG["projects"] = {PATH: {}, PATH_B: {}}
daemon.MSGPROJ.clear()
cands = [A, B]
check("an exact name wins", daemon.resolve_addr("proj", None, cands)[0], A)
check("case does not matter", daemon.resolve_addr("PROJ", None, cands)[0], A)
check("an unambiguous prefix is enough",
      daemon.resolve_addr("proj-", None, cands)[0], B)
check("an ambiguous one is refused, with the list",
      (daemon.resolve_addr("pro", None, cands)[0],
       "matches more than one" in daemon.resolve_addr("pro", None, cands)[1],
       sorted(daemon.resolve_addr("pro", None, cands)[2])),
      (None, True, ["proj", "proj-b"]))
check("an unknown one too",
      "no project called" in daemon.resolve_addr("nope", None, cands)[1],
      True)
print("   replying to a message the bridge sent about a pair addresses it")
daemon.remember_message(4242, PATH_B)
check("the reply is the address", daemon.resolve_addr(None, 4242, cands)[0],
      B)
check("a reply to something it no longer remembers is not an address",
      daemon.resolve_addr(None, 9999, cands)[0], None)
check("and a typed address still beats a reply",
      daemon.resolve_addr("proj", 4242, cands)[0], A)
print("   with exactly one candidate, nothing has to be said at all - that")
print("   is the behaviour of the day when there was only ever one project")
check("one candidate, no address needed",
      daemon.resolve_addr(None, None, [A])[0], A)
check("two, and it refuses rather than picking",
      (daemon.resolve_addr(None, None, cands)[0],
       "more than one" in daemon.resolve_addr(None, None, cands)[1]),
      (None, True))
check("none at all is not an error, there is simply nothing to address",
      daemon.resolve_addr(None, None, [])[:2], (None, ""))
print("   /rotate is the one command that is never done unaddressed, even")
print("   with a single candidate: it costs a window and cannot be undone")
check("that rule is written down where the commands are",
      daemon.TG_ADDRESSING["rotate"], "never")
check("while pause and resume mean the whole bridge when unaddressed",
      [daemon.TG_ADDRESSING[c] for c in ("pause", "resume")],
      ["bridge", "bridge"])
check("and a verdict is the one-candidate rule",
      daemon.TG_ADDRESSING["verdict"], "one")
print("   a button carries the pair in its data, because a press arrives")
print("   with nothing else that says which message it came from")
check("the id is short enough for telegram's 64 bytes",
      len(("restart executor|%s" % daemon.pair_id(PATH)).encode()) <= 64,
      True)
check("it is the same id every time, from the path alone",
      daemon.pair_id(PATH), daemon.pair_id(PATH.upper()))
check("different pairs, different ids",
      daemon.pair_id(PATH) != daemon.pair_id(PATH_B), True)
check("and it resolves back to the project it came from",
      daemon.path_of_pair_id(daemon.pair_id(PATH_B)), B)
check("an id from nowhere resolves to nothing, rather than to the first one",
      daemon.path_of_pair_id("deadbeef"), None)

print("\n44. the planner is told, in every text that instructs it, that")
print("    context is not its department")
print("    planners were halting the run when the executor looked full and")
print("    waiting for a replacement the bridge had not decided on and")
print("    would have made itself. The measuring is the bridge's and so is")
print("    the rotation; a full-looking context is not an event. This is")
print("    said in both places a role is instructed, because a session gets")
print("    one of them at a time: the channel's instructions arrive with a")
print("    NEW session, the seed only after the daemon has been restarted")
csrc44 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bridgecore", "channel.py"), encoding="utf-8").read()
# The seed's paragraph is a constant, not a run of literals wrapped across
# a dozen source lines - so it can be read as the one string it is. That is
# the whole reason it is a constant: an assertion on wrapped prose tests
# the line breaks rather than the text.
seed44 = daemon.PLANNER_CONTEXT_RULE
KEY = "is not a reason to do anything"
check("the channel tells the planner", KEY in csrc44, True)
check("and so does the seed", KEY in seed44, True)
check("and the seed's copy is what the seed actually appends",
      "PLANNER_CONTEXT_RULE" in inspect.getsource(daemon.handle_event), True)
print("   the three things it must not do are named, not implied")
for phrase in ("not a stop verdict", "not a wait",
               "not holding work back"):
    check("named in the channel: %r" % phrase, phrase in csrc44, True)
    check("named in the seed:    %r" % phrase, phrase in seed44, True)
check("and the way out is the human, not an imitation of the bridge",
      # a fragment that does not cross a line wrap: in channel.py the
      # sentence breaks between "the" and "bridge", and asserting across
      # that would be testing the wrapping
      ("say so to the human and let them decide" in csrc44,
       "say so to the human and let them decide" in seed44), (True, True))
print("   and the executor is told the same thing just as plainly - it was")
print("   ending turns early and reporting that it was waiting to be")
print("   replaced, which is the same mistake from the other side")
check("its own context is not its to think about",
      "not yours to think about" in csrc44, True)
for phrase in ("turn early because it looks full", "do not wind work down",
               "do not decline a task"):
    check("named: %r" % phrase, phrase in csrc44, True)
check("and the right behaviour is named, at any level",
      "to the natural end of the turn" in csrc44, True)
print("   the state report hands the executor the whole context readout, so")
print("   it says whose business the numbers are rather than dropping them")
print("   (1.6.8: not deciding from a figure is no reason to hide it)")
ssrc44 = inspect.getsource(daemon.state_report)
check("the readout is still there", "Compactions: %d of %d" in ssrc44, True)
check("and so is the line naming its owner",
      "None of the above is yours to act on" in ssrc44, True)
check("said to the executor only - the planner is told elsewhere",
      'if role == "executor":' in ssrc44, True)
print("   said to the planner in both texts, to the executor in the one it")
print("   gets - the executor's seed carries a handoff, not instruction")
check("the planner's own instructions still start where they did",
      "You are the PLANNER of a bridge pair" in csrc44, True)

print("\n45. the bar to the wall shows the whole life, not the current cycle")
print("    a session two compactions into five is two fifths of the way to")
print("    being replaced. The bar showed 2.6%, because carried size drops")
print("    back to the floor at every compaction and the figure it was")
print("    drawn from is how much of what is LEFT has been consumed - the")
print("    right answer to 'how far to the next compaction' and the wrong")
print("    one to the question the bar is named after")
reset(compactions=[comp(800000, 200000), comp(800000, 260000)],
      compact_at=800000)
lv45 = daemon.life_view(sess(300000), PATH)
check("two of five compactions are behind it", lv45["done"], 2)
check("the old figure is still what it always was, and still small",
      lv45["pct"] < 10, True)
check("and the life figure says two fifths and a little",
      0.40 <= lv45["life_pct"] / 100.0 <= 0.50, True)
print("   the arithmetic it is built from is untouched - the new field is")
print("   the cycles behind it plus its place in this one, over five")
check("cycles behind, from the same count the report uses",
      lv45["done"], daemon.compactions_done(PATH, "executor"))
check("place in this one, between nothing and all of it",
      0.0 <= lv45["life_frac"] <= 1.0, True)
check("and left, total and pct are the values they were",
      (lv45["left"], lv45["total"] > lv45["left"]),
      (int(lv45["rest_of_cycle"] + sum(
          [lv45["cycle"]] * lv45["later_cycles"])) if not lv45.get("rise")
       else lv45["left"], True))
print("   a session that has compacted nothing has barely started")
reset(compact_at=800000)
lv45b = daemon.life_view(sess(100000), PATH)
check("no compactions behind it", lv45b["done"], 0)
check("so a small fraction of a life", lv45b["life_pct"] < 10, True)
print("   and when the terms are not known it says so rather than zero")
reset(compact_at=None, autocompact=None)
lv45c = daemon.life_view(sess(400000), PATH)
check("no life figure at all", lv45c.get("life_pct"), None)
check("with the reason where it belongs",
      bool(lv45c.get("why_blank")), True)
print("   the panel draws the bar from that field and nothing else")
psrc45 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bridgecore", "panel.html"), encoding="utf-8").read()
check("segments, from the life fraction",
      "segbar(L0.budget,L0.done,L0.life_frac" in psrc45, True)
check("and the percentage it prints is the life one",
      "L0.life_pct!=null" in psrc45, True)
print("   the strip shows life too, because 'which pair needs me next' is")
print("   what it is for - window fill resets every compaction")
check("the row reads life", "var life=x.life;" in psrc45, True)
check("and keeps the fill in the hover, where it is still true",
      "window '+Math.round(x.pct)" in psrc45, True)
check("a pair the bridge cannot size yet reads as unknown, not as zero",
      'life==null?"-"' in psrc45, True)

print("\n46. the executor asks for nothing, and the planner is held back by")
print("    something a mode cannot loosen")
print("    the client went 2.1.227 -> 2.1.232 and 'auto' grew stricter: one")
print("    pair kept working because its window predated the update, two")
print("    newer ones asked on every fresh shape of command - 499 rules")
print("    accumulated in one project's settings.local.json, a click at a")
print("    time, and it still asked. 'dontAsk' is not what its name says:")
print("    measured against a real client it answers 'denied (don't-ask")
print("    mode)' and writes nothing - no questions AND no work")
check("the bridge-wide default is the one that does both",
      store.DEFAULT_CONFIG["role_modes"]["executor"], "bypassPermissions")
check("and the planner starts in auto - the owner's word of 2026-09-30 "
      "(it was plan)", store.DEFAULT_CONFIG["role_modes"]["planner"], "auto")
daemon.CFG["role_modes"] = dict(store.DEFAULT_CONFIG["role_modes"])
daemon.CFG["projects"] = {daemon.norm(PATH): {}}
check("a project that names nothing gets the default",
      daemon.mode_for(PATH, "executor"), "bypassPermissions")
daemon.CFG["projects"] = {daemon.norm(PATH): {"modes": {"executor":
                                                        "acceptEdits"}}}
check("a project that names its own wins",
      daemon.mode_for(PATH, "executor"), "acceptEdits")
print("   the saved 'auto' was the default of the day, not a choice, and")
print("   it would have shadowed the new one in every project")
daemon.CFG["projects"] = {
    daemon.norm(PATH): {"modes": {"executor": "auto", "planner": "plan"}},
    daemon.norm(PATH_B): {"modes": {"executor": "acceptEdits"}}}
check("before the migration it shadows it",
      daemon.mode_for(PATH, "executor"), "auto")
# an install that has never started this bridge: the migration's own record
# is owned here, and put back after (DECISIONS.md 8.112)
_mig46 = daemon.STATE.get("migrations")
daemon.STATE["migrations"] = {}
_said46 = []
_sj46 = store.journal
store.journal = lambda kind, text, *a, **k: (
    _said46.append((text, k.get("level"))), _sj46(kind, text, *a, **k))[-1]
try:
    daemon.migrate_executor_mode()
finally:
    store.journal = _sj46
check("after it, the default applies",
      daemon.mode_for(PATH, "executor"), "bypassPermissions")
check("a mode somebody chose is left alone",
      daemon.mode_for(PATH_B, "executor"), "acceptEdits")
check("and the planner's is never touched",
      daemon.mode_for(PATH, "planner"), "plan")
_drop46 = [lv for t, lv in _said46 if "Dropped the saved executor mode" in t]
check("the drop is said at warn, not at log - on the Mac a line at log was "
      "all the person had of it", _drop46, ["warn"])
check("and it names the way back, the panel's settings",
      any("panel's settings" in t for t, lv in _said46), True)
check("running it again changes nothing", daemon.migrate_executor_mode(), [])
print("   ONCE: the panel writes the mode a person picks at every launch, so")
print("   a migration run at every start dropped a chosen 'auto' at the next")
print("   one - the Mac, 2026-10-13, 03:35:07, and the window of 07:30 stood")
print("   on the Bypass warning for an hour")
daemon.CFG["projects"][daemon.norm(PATH)]["modes"]["executor"] = "auto"
check("auto chosen after the migration stays at the next start",
      (daemon.migrate_executor_mode(), daemon.mode_for(PATH, "executor")),
      ([], "auto"))
daemon.STATE["migrations"] = {daemon.BASE_LAUNCH_MARK: time.time()}
check("an install past the base launch of 2026-10-10 has had this at every "
      "start since: its auto is a choice, and is kept",
      (daemon.migrate_executor_mode(), daemon.mode_for(PATH, "executor")),
      ([], "auto"))
check("and the migration is marked done there too",
      sorted(daemon.STATE["migrations"]),
      sorted([daemon.BASE_LAUNCH_MARK, "executor-mode-once-2026-10-13"]))
if _mig46 is None:
    daemon.STATE.pop("migrations", None)
else:
    daemon.STATE["migrations"] = _mig46
print("   every launch path asks the same question, so a handover moves a")
print("   live pair onto the new mode without restarting the daemon")
dsrc46 = inspect.getsource(daemon)
# Since 2026-09-23 the mode is asked into a variable first, and the SAME
# value goes to note_launch's line and to sessions.launch - so the line
# cannot name one mode while the window starts in another (DECISIONS.md
# 8.25). The claim here is unchanged: the handover asks mode_for at launch.
_ho46 = inspect.getsource(daemon.handover)
check("the handover launches with it",
      "use_mode = mode_for(path, role)" in _ho46
      and "permission_mode=use_mode" in _ho46, True)
check("and so does the panel's button, which lands in handle_session",
      'body.get("mode") or' in inspect.getsource(daemon.handle_session), True)
check("as do the restart and the silence-driven launch",
      ("mode_for(path, role)" in inspect.getsource(daemon.restart_session),
       "mode_for(path, role)" in inspect.getsource(daemon.ensure_session)),
      (True, True))
print("   the planner is not protected by its mode - deny beats every mode,")
print("   and that is what holds it")
check("the reviewer's tools are denied outright",
      bool(daemon.disallow_for(PATH, "planner")), True)
check("with the editing ones on the list",
      all(t in daemon.disallow_for(PATH, "planner")
          for t in ("Edit", "Write")), True)
check("and the executor is denied nothing",
      daemon.disallow_for(PATH, "executor"), None)

print("\n47. the canon reaches both halves, every start, including the one a")
print("    handover brings up")
print("    The owner explained the same rules to every new pair by hand. They")
print("    are collected in HONESTY.md from what he actually said across four")
print("    projects - and handed over by the bridge instead. Two delivery")
print("    points, and neither is enough on its own: the seed does not exist")
print("    for a window that starts while the daemon is down, and channel.py")
print("    is a separate process that cannot import the daemon to ask")
import re                                                # noqa: E402
# Two files now, and the split is the point: the rules are short because they
# are paid for on every single delivery, and the evidence is long because a
# person reads it once. Each is checked for what it is FOR.
# Three assertions about HONESTY_CASES.md used to live here and moved to
# test_cases.py: this file has to pass from inside the PUBLIC folder,
# where that document does not exist and must not. They are not lost -
# the private suite checks the size, the pointer and the self-audit
# table, and it is the sixth suite of the private acceptance run.
canon = daemon.honesty_text()
check("the file is there and has something in it", len(canon) > 2000, True)
check("and the rules stayed short enough to put in front of every task",
      len(canon) < 15000, True)
# Counted from the canon itself, not pinned: the number changes when a rule
# is added (29 on 2026-08-21, the quiet-run rule), and a copy of it here
# would only ever be yesterday's. What must hold is that EVERY rule carries
# a check - that is the invariant, and it does not depend on how many.
_n_rules = len(re.findall(r"^\d+\. \*\*", canon, re.M))
check("with rules in it, not just prose", _n_rules >= 28, True)
check("and each of them carries its check where it is read",
      len(re.findall(r"^\s+\*[^*]+:\*", canon, re.M)), _n_rules)
print("   every rule carries the thing that makes it a rule and not a wish:")
print("   a way to check it from outside")
# Everything about HONESTY_CASES.md moved to test_cases.py, which is
# private: the only way to check a Russian document is to name Russian
# phrases, and this file has to pass the privacy gate. Two assertions
# that were LOST in the split - that a case says it was bad work, and
# that it says what should have been done - live there now, verbatim.
check("the rules a pair is handed carry no evidence at all - that is the "
      "whole point of the split, and what makes them cheap to prepend",
      len([l for l in canon.splitlines() if l.startswith("> ")]), 0)

print("   the seed hands it to both roles - a rotated session is a new")
print("   session and has been told nothing")
esrc47 = inspect.getsource(daemon.handle_event)
check("the seed appends it", "honesty_text()" in esrc47, True)
check("for both managed roles, not just the planner",
      "if canon and managed(role):" in esrc47, True)
check("and it is not inside the planner-only branch",
      esrc47.index("canon = honesty_text()") >
      esrc47.index('note = note_for(path)'), True)
print("   the channel carries it too, read from the same file rather than")
print("   copied - one text, and editing the file changes both")
csrc47 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bridgecore", "channel.py"), encoding="utf-8").read()
check("the channel reads it", "def _honesty()" in csrc47, True)
check("from the file, not from a copy of the words",
      '"HONESTY.md"' in csrc47, True)
check("and appends it for the two roles that work",
      'if ROLE in ("planner", "executor"):' in csrc47, True)
check("a missing file costs the reminder, not the run",
      csrc47.count("except Exception:\n        return \"\"") >= 1, True)
print("   it is read fresh, so rewriting the file reaches the next session")
print("   without a restart")
hsrc47 = inspect.getsource(daemon.honesty_text)
check("no cache between reads", "open(HONESTY" in hsrc47, True)

print("\n48. the gate that stands in the way of the action")
print("    A rule in a document is read once at session start and then")
print("    competes with the task for attention. These three stand in the")
print("    way instead. This case holds the parts that are not endpoints -")
print("    the wording, the shape of the refusal, and the edges of what")
print("    counts as a path. The endpoint behaviour is test_multipair 21-23")
print("   first: prose must not become a missing artefact. An early cut")
print("   refused a sound verdict because it could not find a folder")
print("   called \"z9\" - and a gate that refuses good work gets switched off")
_here = os.path.dirname(os.path.abspath(__file__))
_good, _dead = daemon.artifact_paths(
    "looked at zones z9/z10, rule 5.17, version 2.1.232 and the file "
    "bridgecore/store.py", _here)
check("a real path inside the project is found", "bridgecore/store.py" in _good,
      True)
check("and none of the prose is called a missing file", _dead, [])
_good2, _dead2 = daemon.artifact_paths("out/nowhere/render.png", _here)
check("a path that plainly means a file, and is not there, is named",
      _dead2, ["out/nowhere/render.png"])
_good3, _dead3 = daemon.artifact_paths("build.log", _here)
check("a bare name that does not exist is ignored rather than held "
      "against the writer", (_good3, _dead3), ([], []))
print("   the four verdicts: two accept work and are gated, two do not")
# This read "continue and wait are never gated". Only wait is now: continue
# carries a judgement, and a judgement made from the report is acceptance by
# hearsay. Changed deliberately - the case still asks which verdicts are free.
check("'wait' is never gated - it judges nothing, it says a process runs",
      daemon.verdict_gate(_here, "wait", "")[0], True)
check("'continue' is, because it judges",
      daemon.verdict_gate(_here, "continue", "looks fine")[0], False)
check("and its refusal names why, not just that it failed",
      "hearsay" in daemon.verdict_gate(_here, "continue", "looks fine")[1],
      True)
check("a continue with something real to open goes through",
      daemon.verdict_gate(_here, "continue", "Checked: bridgecore/store.py")[0],
      True)
for _v in ("done", "stop"):
    _ok, _why, _kind = daemon.verdict_gate(_here, _v, "accepted, good")
    check("'%s' without a block is refused" % _v, _ok, False)
    check("and the refusal tells the planner what to write, not that it "
      "failed", "Checked:" in _why, True)
_ok, _why, _kind = daemon.verdict_gate(_here, "done",
                                       "Checked: bridgecore/store.py")
check("a block naming something real passes, tagged as artefacts",
      (_ok, _kind), (True, "artifacts"))
print("   the named exit, and why its length is not the point")
_ok, _why, _kind = daemon.verdict_gate(
    _here, "done", "Checked: no artifacts — nothing")
check("a throwaway reason is refused", _ok, False)
_long = ("this was a read-only investigation of the logs with no code "
         "changed, and nothing to open")
_ok, _why, _kind = daemon.verdict_gate(
    _here, "done", "Checked: no artifacts — " + _long)
check("a reason a person could weigh later is accepted", (_ok, _kind),
      (True, "none"))
check("and taking it is recorded and counted, which is what makes it "
      "expensive rather than the word count",
      ("noart" in inspect.getsource(daemon.note_no_artifacts),
       "warn" in inspect.getsource(daemon.note_no_artifacts)), (True, True))
check("the counter is path-keyed like everything else about one pair",
      "noart" in daemon.PATH_KEYED and "frames" in daemon.PATH_KEYED, True)
print("   the channel must return a refusal as an ERROR - returned as plain")
print("   text it reads like any other confirmation, and the planner walks")
print("   away believing the piece was accepted")
_r = _blocks("verdict", {"verdict": "done"},
             {"ok": False, "refused": True, "error": "no block"})
_full = _blocks_result("verdict", {"verdict": "done"},
                       {"ok": False, "refused": True, "error": "no block"})
check("the refusal comes back as isError", _full.get("isError"), True)
check("and says the report is still waiting",
      "still waiting" in " ".join(b.get("text", "") for b in _r), True)
_okfull = _blocks_result("verdict", {"verdict": "continue"},
                         {"ok": True, "delivered": True})
check("an accepted verdict is not an error", _okfull.get("isError"), None)
print("   the hook that asks earlier has to be installed to ask at all")
_proj = os.path.join(TMP, "installee")
os.makedirs(os.path.join(_proj, ".claude"), exist_ok=True)
_settings = os.path.join(_proj, ".claude", "settings.json")
_theirs = {"type": "command", "command": "their-own-thing", "args": ["--x"]}
with open(_settings, "w", encoding="utf-8") as fh:
    _json.dump({"hooks": {"PreToolUse": [{"hooks": [dict(_theirs)]}]}}, fh)
from bridgecore import install as _install                        # noqa: E402
check("PreToolUse is one of the events the installer writes",
      "PreToolUse" in _install.EVENTS, True)
_install.install(_proj, python=sys.executable, statusline=False)
with open(_settings, encoding="utf-8") as fh:
    _cfg = _json.load(fh)
_pre = [h for g in _cfg["hooks"]["PreToolUse"] for h in g.get("hooks", [])]
check("the bridge hook is there after install",
      any(h.get("args") == [_install.HOOK_SCRIPT] for h in _pre), True)
check("and the project's own hook was kept, not overwritten",
      any(h.get("command") == "their-own-thing" for h in _pre), True)
print("   and the current directory is kept off sys.path, so a second copy")
print("   of this package sitting in whatever folder the session happens to")
print("   be in cannot shadow the installed one. Measured: with -m, Python")
print("   puts cwd FIRST, ahead of PYTHONPATH - a public copy assembled in a")
print("   subfolder of a watched project was the hook that actually ran.")
print("   (Since 8.122 the hooks run hook.py by path, with no -m at all: a")
print("   Mac's Python 3.9 ignores PYTHONSAFEPATH - case 153)")
with open(_settings, encoding="utf-8") as fh:
    _env = (_json.load(fh).get("env") or {})
check("the installer writes PYTHONPATH at the folder holding the "
      "package, so the hooks import it by name wherever they run",
      _env.get("PYTHONPATH", ""),
      os.path.dirname(os.path.dirname(
          os.path.abspath(_install.__file__))))
check("and PYTHONSAFEPATH, which is what keeps cwd out of it",
      _env.get("PYTHONSAFEPATH"), "1")
check("the role is never written here - it belongs to a window",
      "BRIDGE_ROLE" in _env, False)
_install.uninstall(_proj)
with open(_settings, encoding="utf-8") as fh:
    _cfg2 = _json.load(fh)
_pre2 = [h for g in (_cfg2.get("hooks") or {}).get("PreToolUse", [])
         for h in g.get("hooks", [])]
check("uninstall takes the bridge hook out by identity",
      any(h.get("args") == [_install.HOOK_SCRIPT] for h in _pre2), False)
check("and leaves theirs alone",
      any(h.get("command") == "their-own-thing" for h in _pre2), True)
with open(_settings, encoding="utf-8") as fh:
    _env2 = (_json.load(fh).get("env") or {})
check("uninstall takes both env keys back out, by value",
      ("PYTHONPATH" in _env2, "PYTHONSAFEPATH" in _env2), (False, False))

print("\n49. a quote in the canon is confirmed by the ORIGINAL, never by our")
print("    own retelling of it")
print("    The first audit searched every transcript for each quotation - ")
print("    including this project's, where the canon's own text and every")
print("    report about it live. So a fabricated quote would have been")
print("    'found' inside the document quoting itself. That is a check that")
print("    cannot fail, which is rule 19 of the very document it checks")
print("    The audit reads the owner's own transcripts, which exist only on")
print("    his machine; what lives here is the RULE it applies, so the rule")
print("    itself cannot quietly loosen")


def confirmed(corpus, quote, block_project, also_named=()):
    """Is this quotation carried by a primary record of the right project?

    corpus is (project, text). A hit inside the Bridge project counts only
    when the incident is attributed to Bridge, or when the canon names
    Bridge beside that quotation - which it does when a rule carries a
    second incident from another pair.
    """
    allowed = {block_project} | set(also_named)
    return any(p in allowed and quote in t for p, t in corpus)


CORPUS = [("Bridge", "the canon says: everything is produced by the "
                     "script - quoted in our own report"),
          ("a texture project", "everything is produced by the script"),
          ("a game project", "I told you not to use your own poses")]
check("a quote living only in this project is NOT confirmed for an "
      "incident attributed elsewhere",
      confirmed(CORPUS, "everything is produced", "a game project"), False)
check("the same quote is confirmed when the canon names Bridge beside it",
      confirmed(CORPUS, "everything is produced", "a game project",
                also_named=["Bridge"]), True)
check("and it is confirmed outright from the project that actually said it",
      confirmed(CORPUS, "everything is produced", "a texture project"),
      True)
check("a quote in the right project passes",
      confirmed(CORPUS, "I told you not to use your own poses",
                "a game project"),
      True)
check("and one that was never said fails, whoever it is attributed to",
      [confirmed(CORPUS, "nobody ever wrote this", p)
       for p in ("a game project", "a texture project", "Bridge")],
      [False, False, False])

print("\n50. the three locks, and the edges that decide whether they help")
print("    From a watched project, 2026-08-18, watching its own rule "
      "be bypassed:")
print("    the rule lived in prose, every workaround was lawful on the day it")
print("    was made, and a replay script that rebuilt the patch stack byte")
print("    for byte was mistaken for reproducibility. Three locks answer it -")
print("    on the way in (declared debt), on the way out (the package is the")
print("    bytes that were tested), and at acceptance (where does it live)")
print("   the code detector fires on a NAMED file, a diff or a commit - never")
print("   on words. A false demand teaches the pair to write a meaningless")
print("   residence line to get past it")
check("a named source file is a code change",
      daemon.touched_code("edited bridgecore/daemon.py, the suites are green"), True)
check("so is a diff", daemon.touched_code("@@ -1,4 +1,6 @@\n x"), True)
check("so is a commit", daemon.touched_code("commit a7474c0 on main"), True)
check("but words alone are not",
      daemon.touched_code("tidied the logic, it reads better"), False)
check("and prose that looks like paths is not",
      daemon.touched_code("looked at zones z9/z10 and rule 5.17"), False)
print("   a residence line has to name a PLACE - 'yes' is not an answer")
check("file:function counts",
      daemon.residence_ok("Residence: bridgecore/daemon.py:verdict_gate"), True)
check("a dotted identifier chain counts",
      daemon.residence_ok("Residence: store.norm"), True)
check("a named test counts",
      daemon.residence_ok("Residence: case 50 test_handover.py"), True)
check("a yes does not", daemon.residence_ok("Residence: yes"), False)
check("and a version number is not a residence",
      daemon.residence_ok("Residence: version 2.1.232"), False)
print("   AND IT HAS TO ACCEPT THE SEPARATORS THIS MACHINE ACTUALLY USES.")
print("   The file:place branch was ^[\\w./-]+::?[\\w.]+$ - no backslash in")
print("   the class, no hyphen to the right of the colon - so an honest")
print("   residence written in native Windows paths was REFUSED by the gate,")
print("   and the writer reads that as the gate being fussy (rule 24).")
print("   The absolute path is the trap: a drive letter makes TWO colon")
print("   groups, so adding a backslash to the class fixes the relative")
print("   form and leaves this one refused - half a fix that looks whole.")
check("a relative Windows path with a function",
      daemon.residence_ok(r"Residence: bridgecore\daemon.py:handover"), True)
print("   The absolute case is DERIVED, never written down: a published")
print("   suite may not carry this machine's layout - check_public refuses")
print("   an absolute local path, and its placeholder form cannot carry a")
print("   filename, so no literal exists that both gates would pass. It is")
print("   computed instead, which is the stronger check anyway: the drive")
print("   letter is whatever the machine running this actually uses.")
print("   On POSIX there is no drive letter and no second colon group - the")
print("   trap is a Windows one, and this is a Windows-first project.")
_abs = os.path.abspath("/proj/pkg/mod.py")
check("an ABSOLUTE path - on Windows the drive is a SECOND colon group",
      daemon.residence_ok("Residence: %s:handover" % _abs), True)
check("hyphens in the place name, right of the colon",
      daemon.residence_ok("Residence: bridge-logs/handoff/"
                          "087-shift.md:whose-compaction-counts"), True)
print("   and every refusal it used to make it must go on making, because a")
print("   gate with a hole is worse than a gate that nags")
print("   (the Russian word that started this gate is refused too, and")
print("    it is checked in test_cases.py block 4 - a published suite")
print("    carries no Cyrillic, written out or escaped)")
check("a bare yes is still not a residence",
      daemon.residence_ok("Residence: yes"), False)
check("a bare version is still not a residence",
      daemon.residence_ok("Residence: 2.1.232"), False)
check("a bare commit hash is still not a residence",
      daemon.residence_ok("Residence: a7474c0"), False)
check("and a line under the length floor is still not one",
      daemon.residence_ok("Residence: x.y"), False)
print("   debt is parsed from the executor's own words, and the register is")
print("   rendered from state so the file and the counter cannot disagree")
_dp = os.path.join(TMP, "debtproj")
os.makedirs(_dp, exist_ok=True)
_what, _how = daemon._debt_split(
    "the exception list is hard-coded - closed by moving it to config.json")
check("the two halves come apart on the dash",
      ("hard-coded" in _what, "config.json" in _how), (True, True))
daemon.note_debt(_dp, "debtproj", "Debt: a stub in the path parser - "
                                  "closed by the real parser")
check("one line is owed", len(daemon.open_debt(_dp)), 1)
daemon.note_debt(_dp, "debtproj", "Debt: a second stub - later")
check("two", len(daemon.open_debt(_dp)), 2)
daemon.note_debt(_dp, "debtproj",
                 "Debt closed: a stub in the path parser - the real parser is in")
check("closing one leaves the other standing", len(daemon.open_debt(_dp)), 1)
check("and keeps both rows - the pile is the evidence, not the balance",
      len(daemon.debt_rows(_dp)), 2)
check("«Debt closed:» is not read as a new debt",
      [d["what"] for d in daemon.debt_rows(_dp)
       if d["what"].lower().startswith("closed")], [])
check("the register file says what closed it",
      "the real parser is in" in open(os.path.join(_dp, "bridge-logs", "DEBT.md"),
                              encoding="utf-8").read(), True)
print("   the way out: running the suites from an unpacked copy proves the")
print("   copy WORKS, not that it is the code that was reviewed. Only")
print("   comparing bytes in all three places proves that")
import hashlib as _hl                                    # noqa: E402
import zipfile as _zf                                    # noqa: E402
_vp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "verify_package.py")
check("the tool ships with the repo", os.path.exists(_vp), True)
# _ns keeps a FILES key whatever happens, so the checks below fail on their
# own terms rather than raising KeyError and taking the suite with them.
_ns = {"FILES": []}
_vp_src = read_or_fail(_vp, "verify_package.py")
if _vp_src:
    exec(compile(_vp_src, _vp, "exec"), _ns)
# The COUNT is read from the list rather than pinned beside it: it moved
# 28 -> 29 on 2026-08-21 when QUIET.md joined the package, and a second
# copy of the number here would only ever be yesterday's. What matters is
# not how many there are but that the list contains the files a recipient
# needs in order to check the package - including this checker itself.
if _vp_src:
    _n_files = len(_ns["FILES"])
    check("it checks every file the package snippet lists, itself included - "
          "a package its recipient cannot verify is a weaker package",
          (_n_files >= 28, "source/verify_package.py" in _ns["FILES"],
           "source/HONESTY_CASES.md" in _ns["FILES"],
           "source/LICENSE" in _ns["FILES"]), (True, True, True, True))
    check("and the canon's own long form travels with it",
          "source/QUIET.md" in _ns["FILES"], True)
    _repo = os.path.join(TMP, "pkgrepo")
    _unp = os.path.join(TMP, "pkgunp")
    for _rel in _ns["FILES"]:
        for _d in (_repo, _unp):
            _p = os.path.join(_d, _rel)
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            with open(_p, "w", encoding="utf-8") as fh:
                fh.write("content of " + _rel + "\n")
    _zip = os.path.join(TMP, "good.zip")
    with _zf.ZipFile(_zip, "w") as z:
        for _rel in _ns["FILES"]:
            z.write(os.path.join(_repo, _rel), _rel)
    _rows, _bad, _extra, _names = _ns["compare"](_repo, _zip, _unp)
    check("a package built from the tested tree matches everywhere",
          (_bad, _extra, len(_names)), ([], [], _n_files))
    _tampered = os.path.join(TMP, "tampered.zip")
    with _zf.ZipFile(_tampered, "w") as z:
        for _rel in _ns["FILES"]:
            if _rel == "source/bridgecore/daemon.py":
                z.writestr(_rel, "content of bridge/daemon.py\n# and one more line\n")
            else:
                z.write(os.path.join(_repo, _rel), _rel)
    _rows2, _bad2, _extra2, _names2 = _ns["compare"](_repo, _tampered, _unp)
    check("one changed file in the archive is caught, and named",
          _bad2, ["source/bridgecore/daemon.py"])
    check("the others still match", len(_bad2), 1)
    _extra_zip = os.path.join(TMP, "extra.zip")
    with _zf.ZipFile(_extra_zip, "w") as z:
        for _rel in _ns["FILES"]:
            z.write(os.path.join(_repo, _rel), _rel)
        z.writestr("source/bridgecore/bridge/daemon.py",
                   "the stale nested copy\n")
    _r3, _b3, _e3, _n3 = _ns["compare"](_repo, _extra_zip, _unp)
    check("an entry that is not on the list is caught too - that is how the "
          "stale nested copy would get in", _e3,
          ["source/bridgecore/bridge/daemon.py"])
    print("   and the execute bit of what a Mac double-clicks: set by the")
    print("   recipe, not by the disk it was built on (8.104)")
    if all(n in _ns for n in ("EXEC", "write_zip", "unpack", "modes")):
        for _rel in _ns["EXEC"]:
            os.chmod(os.path.join(_repo, _rel), 0o644)  # no bit, as on the PC
        _built = os.path.join(TMP, "built.zip")
        _unpx = os.path.join(TMP, "pkgunpx")
        _ns["write_zip"](_repo, _built)
        _ns["unpack"](_built, _unpx)
        with _zf.ZipFile(_built) as z:
            _inf = {i.filename: (i.create_system, i.external_attr >> 16)
                    for i in z.infolist()}
        check("the recipe writes bridge.command as made on Unix with 755 and "
              "every other file 644, whatever the disk said",
              (_ns["EXEC"], _inf.get("bridge.command"),
               sorted({m for f, (c, m) in _inf.items()
                       if f not in _ns["EXEC"]}),
               sorted({c for c, m in _inf.values()})),
              (["bridge.command"], (3, 0o100755), [0o100644], [3]))
        check("and that package passes: the same bytes, and the bit in the "
              "archive and in the copy unpack() made",
              (_ns["compare"](_repo, _built, _unpx)[1],
               _ns["modes"](_built, _unpx)), ([], []))
        check("a package built the old way, zipfile.write from a disk "
              "without the bit, is caught in the archive",
              [(w, r) for w, r, _x in _ns["modes"](_zip, _unpx)],
              [("zip", "bridge.command")])
        if os.name != "nt":
            check("and the right archive unpacked by zipfile's own "
                  "extractall, which drops the bit, is caught in the copy",
                  [(w, r) for w, r, _x in _ns["modes"](_built, _unp)],
                  [("unpacked", "bridge.command")])
    else:
        check("verify_package has the recipe that sets the execute bit: "
              "EXEC, write_zip, unpack, modes", False, True)
else:
    # Without the file there is nothing to exec, so FILES and compare
    # do not exist and every check below would raise instead of
    # failing. One FAIL is recorded for the lot and the suite carries
    # on: a check that has already spoken must not un-speak itself by
    # crashing.
    check("the package checks need verify_package.py and it is gone",
          False, True)
print("   and rule 24 applied to this very document: a rule whose check")
print("   names a function must have that function")
_canon = daemon.honesty_text() + "\n" + daemon.honesty_cases_text()
for _fn in re.findall(r"`daemon\.([a-z_]+)`", _canon):
    check("the canon names daemon.%s and it exists" % _fn,
          callable(getattr(daemon, _fn, None)), True)

print("\n51. the rules go in FRONT of the work, not beside it")
print("    A canon handed over once at SessionStart is read once and then")
print("    loses to everything that follows: the task is concrete and")
print("    urgent, the rules are neither. So they head the two messages")
print("    where they can still change what happens - the task, as work")
print("    begins, and the report, as work is judged")
_task = daemon.rules_for_delivery("task")
_rep = daemon.rules_for_delivery("report")
check("a task carries them", len(_task) > 2000, True)
check("so does a report", len(_rep) > 2000, True)
check("they are visibly fenced off, so nobody reads them as the work",
      (_task.startswith("=" * 70), "End of the rules" in _task),
      (True, True))
check("and the fence names what comes after it, differently for each",
      ("the task itself" in _task, "the executor report itself" in _rep),
      (True, True))
_body = daemon.with_rules("CONTENT", {"kind": "task"})
check("the work itself is still there, and after the rules",
      (_body.endswith("CONTENT"), _body.index("RULES OF WORK")
       < _body.index("CONTENT")), (True, True))
print("   the full text once, titles every time after - because the full")
print("   canon is ~3.5k tokens and every delivered task keeps it in the")
print("   window for good, so fifty tasks would be ~175k spent on repeating")
print("   the same page. The titles still name all 28 rules")
_s1 = daemon.rules_for_delivery("task", "sess-alpha")
_s2 = daemon.rules_for_delivery("task", "sess-alpha")
_s3 = daemon.rules_for_delivery("report", "sess-alpha")
check("the first delivery a session gets carries the whole canon",
      "*" in _s1, True)
check("every one after that carries the titles alone",
      ("*" in _s2, "*" in _s3), (False, False))
check("and the short form is a fraction of the price",
      len(_s3) < len(_s1) / 4, True)
check("but still names every rule",
      len([l for l in _s3.splitlines() if re.match(r"^\d+\. \S", l)]),
      _n_rules)
check("and says where the full text is, so nothing is hidden by shortening",
      "HONESTY.md" in _s3, True)
print("   the EXECUTOR gets no titles at all after its first delivery - the")
print("   owner's word of 2026-10-10: at a 222k window every compaction's")
print("   SessionStart already hands it the whole canon, so the titles were a")
print("   third copy on every task. The closing lines stay - they are what")
print("   reaches a running session without a rotation")
_sv = daemon.rules_for_delivery("verdict", "sess-alpha")
check("a later task or verdict carries no title list",
      (len([l for l in _s2.splitlines() if re.match(r"^\d+\. \S", l)]),
       len([l for l in _sv.splitlines() if re.match(r"^\d+\. \S", l)])),
      (0, 0))
check("and no 'reminder' header pointing at a text it was not given",
      ("RULES OF WORK" in _s2, "RULES OF WORK" in _sv), (False, False))
check("but the closing lines and the role line ride on both",
      ("VISIBLE TEXT" in _s2, "VISIBLE TEXT" in _sv,
       daemon.RULES_ROLE_LINE["task"] in _s2,
       daemon.RULES_ROLE_LINE["verdict"] in _sv), (True, True, True, True))
check("and it says what follows it, behind the same boundary as the full form",
      "End of the rules. Below is the task itself" in _s2, True)
check("the planner's report keeps its titles - its window is a million and "
      "it is rarely compacted", "RULES OF WORK" in _s3, True)
print("   the mark is per SESSION - a handover makes a new one, and a")
print("   replacement window has been told nothing, so it is owed the whole")
print("   thing again. Per project or per role it would be told once in July")
print("   and never again")
_s4 = daemon.rules_for_delivery("task", "sess-beta")
check("a different session starts from the full text",
      "*" in _s4, True)
check("and does not un-mark the first one",
      "*" in daemon.rules_for_delivery("task", "sess-alpha"), False)
check("the mark is kept by session id and nothing else",
      sorted((daemon.STATE.get("rules_full") or {})),
      ["sess-alpha", "sess-beta"])
check("a caller that cannot say which window it is gets the full text - one "
      "extra copy is cheap, a session that never sees the rules is not",
      "*" in daemon.rules_for_delivery("task", None), True)
print("   a verdict is an answer to something the planner already holds, and")
print("   an info line is not work - neither is charged for the rules")
for _k in ("verdict", "info", "", None):
    check("kind %r carries nothing" % _k,
          daemon.with_rules("X", {"kind": _k}), "X")
check("and delivery is the one place it happens, so no caller can forget",
      "content = with_rules(content, meta, last_session_id(path, role))"
      in inspect.getsource(daemon.deliver_ex), True)
print("   BUT A LONG VERDICT IS WORK. `done` means 'accepted, send the next")
print("   piece', and in practice the next piece is often written straight")
print("   into the feedback - a `continue` on 2026-08-22 at 00:01:04 spent")
print("   four sentences telling the executor to reproduce a failure, find")
print("   the cause and fix it, with a Residence and a full green run. That")
print("   is a whole assignment arriving with nothing in front of it")
print("   the threshold is measured: across 4 656 verdicts in this bridge's")
print("   journals the median body is 515 characters and the p90 is 1 687,")
print("   so 1 000 splits 31 per cent above from 69 per cent below. Charging")
print("   every verdict would add 62 per cent to what the canon costs,")
print("   nearly all of it on one-line acceptances - and a rule that taxes")
print("   good work gets switched off")
daemon.STATE.pop("rules_full", None)
_short_v = "Accepted, nothing to add."
_long_v = "Accepted. " + ("Now do the next piece properly. " * 40)
check("the measured threshold is where the distribution turns",
      daemon.VERDICT_WORK_CHARS, 1000)
check("a short verdict is still free", len(_short_v) < 1000
      and daemon.with_rules(_short_v, {"kind": "verdict"}, "vsess")
      == _short_v, True)
check("a long one carries the rules in front of it",
      len(_long_v) >= 1000
      and daemon.with_rules(_long_v, {"kind": "verdict"}, "vsess")
      != _long_v, True)
_vhead = daemon.with_rules(_long_v, {"kind": "verdict"}, "vsess2")
check("and it is the whole canon the first time that session is written to",
      ("RULES OF WORK" in _vhead,
       len([l for l in _vhead.splitlines() if re.match(r"^\d+\. \*\*", l)])),
      (True, _n_rules))
check("the closing line names what follows, so it does not read as a task",
      "Below is the verdict itself." in _vhead, True)
print("   the sabotage: an info line and an empty kind stay free at any")
print("   length - nothing there is a decision")
for _k in ("info", "", None):
    check("kind %r carries nothing even when long" % _k,
          daemon.with_rules(_long_v, {"kind": _k}, "vsess3"), _long_v)
daemon.STATE.pop("rules_full", None)

print("   nothing on this path may drop the loop: a canon that is missing or")
print("   empty costs the reminder, never the delivery")
_realpath = daemon.HONESTY
_gone = os.path.join(TMP, "no-such-canon.md")
_empty = os.path.join(TMP, "empty-canon.md")
open(_empty, "w", encoding="utf-8").close()
_before = len(store.recent_events(200))
try:
    daemon.HONESTY = _gone
    daemon._RULES_MISSING_TOLD[0] = False
    check("a missing file adds nothing", daemon.with_rules("X", {"kind": "task"}),
          "X")
    _said = [e for e in store.recent_events(200)
             if "HONESTY.md is missing or empty" in (e.get("text") or "")]
    check("and says so once, at a level that reaches the panel",
          (len(_said), _said[0].get("level") if _said else None), (1, "warn"))
    daemon.with_rules("X", {"kind": "task"})
    daemon.with_rules("X", {"kind": "report"})
    _said2 = [e for e in store.recent_events(200)
              if "HONESTY.md is missing or empty" in (e.get("text") or "")]
    check("but not on every delivery - a line per message is noise, not a "
          "warning", len(_said2), 1)
    daemon.HONESTY = _empty
    check("an empty file is treated the same as a missing one",
          daemon.with_rules("X", {"kind": "task"}), "X")
    print("   and it is read from disk every time, so editing the file "
          "reaches")
    print("   the next delivery without restarting anything")
    _edited = os.path.join(TMP, "edited-canon.md")
    with open(_edited, "w", encoding="utf-8") as fh:
        fh.write("FIRST EDITION")
    daemon.HONESTY = _edited
    check("the first version is delivered",
          "FIRST EDITION" in daemon.with_rules("X", {"kind": "task"}), True)
    with open(_edited, "w", encoding="utf-8") as fh:
        fh.write("SECOND EDITION")
    _after = daemon.with_rules("X", {"kind": "task"})
    check("and the next delivery carries the edit, with no restart",
          ("SECOND EDITION" in _after, "FIRST EDITION" in _after),
          (True, False))
finally:
    daemon.HONESTY = _realpath
    daemon._RULES_MISSING_TOLD[0] = False
check("the real canon is back", len(daemon.honesty_text()) > 2000, True)
print("   AND THE ORDER MUST NOT BE A WAY ROUND THE RULES. Two tiers means")
print("   a session sees the full text once and titles for ever after, so a")
print("   rule edited later never reaches it - it goes on deciding from the")
print("   version in its memory. The owner, 2026-08-22: rules were already")
print("   got round by ordering once, and that must not happen again")
_realpath2 = daemon.HONESTY
_ordering = os.path.join(TMP, "ordering-canon.md")
try:
    with open(_ordering, "w", encoding="utf-8") as fh:
        fh.write("1. **Rule one.**\n    *Check:* first edition.\n")
    daemon.HONESTY = _ordering
    daemon.STATE.pop("rules_full", None)
    _o1 = daemon.rules_for_delivery("task", "ordering-sess")
    check("the session is given the canon in full the first time",
          "first edition" in _o1, True)
    _o2 = daemon.rules_for_delivery("task", "ordering-sess")
    check("and titles only the second time - the two tiers still hold",
          "first edition" in _o2, False)
    print("   now the rule changes. Every session running right now has the")
    print("   old text in its head and would never be told otherwise")
    with open(_ordering, "w", encoding="utf-8") as fh:
        fh.write("1. **Rule one.**\n    *Check:* SECOND edition.\n")
    _o3 = daemon.rules_for_delivery("task", "ordering-sess")
    check("the very next delivery carries the FULL new text",
          ("SECOND edition" in _o3, "first edition" in _o3), (True, False))
    check("and the mark now records the canon it was given",
          (daemon.STATE["rules_full"]["ordering-sess"].get("canon")
           == daemon.canon_fingerprint()), True)
    _o4 = daemon.rules_for_delivery("task", "ordering-sess")
    check("after which it drops back to titles - it is a reset, not a flood",
          "SECOND edition" in _o4, False)
    print("   the fingerprint is of the CONTENT, not of the file's clock: a")
    print("   rebuild or a copy must not re-send eleven thousand characters")
    print("   to every live session for nothing")
    _fp = daemon.canon_fingerprint()
    os.utime(_ordering, (time.time() + 500, time.time() + 500))
    check("touching the file changes nothing", daemon.canon_fingerprint(), _fp)
    check("and the session is still not owed the full text",
          "SECOND edition" in daemon.rules_for_delivery("task",
                                                        "ordering-sess"),
          False)
    print("   the sabotage: a mark with no fingerprint - one written before")
    print("   this existed - must count as stale, because what that session")
    print("   was shown cannot be established")
    with daemon._lock:
        daemon.STATE["rules_full"]["ordering-sess"] = daemon.now()
    check("an old bare-timestamp mark does not silence the reset",
          daemon.rules_seen("ordering-sess"), False)
    check("so the session is handed the whole canon again",
          "SECOND edition" in daemon.rules_for_delivery("task",
                                                        "ordering-sess"),
          True)
    print("   and the sabotage that proves the check can fail at all: pin")
    print("   the mark to the CURRENT canon and the reset must not fire")
    with daemon._lock:
        daemon.STATE["rules_full"]["ordering-sess"] = {
            "at": daemon.now(), "canon": daemon.canon_fingerprint()}
    check("a mark that matches the canon still means titles only",
          "SECOND edition" in daemon.rules_for_delivery("task",
                                                        "ordering-sess"),
          False)
finally:
    daemon.HONESTY = _realpath2
    daemon.STATE.pop("rules_full", None)

print("   what it costs, measured rather than guessed - this text is paid")
print("   for on every task and every report, so the number belongs here")
daemon.STATE.pop("rules_full", None)
_full = daemon.rules_for_delivery("task", "cost-probe")
_short = daemon.rules_for_delivery("task", "cost-probe")
print("  ..   first delivery of a session: %d chars, %d utf-8 bytes"
      % (len(_full), len(_full.encode("utf-8"))))
print("  ..   every delivery after that:   %d chars, %d utf-8 bytes"
      % (len(_short), len(_short.encode("utf-8"))))

print("\n52. silence is not consent")
print("    The night of 2026-08-18/19: the planner's window was restarted by")
print("    a lost connection. Its channel PROCESS stayed up and kept taking")
print("    deliveries, so every report was handed over successfully and the")
print("    session behind it saw none. 32 reports, 41 to 72, over 11.9 hours,")
print("    not one answered - and the last line of run_review read")
print("    `verdict = waiter[\"verdict\"] or \"continue\"`, so every one of them")
print("    resolved as continue. The executor was told to carry on, every")
print("    time, by nobody")
print("   the threshold comes from that night rather than from taste: the")
print("   median gap between unanswered reports was 21 minutes, so three in")
print("   a row is about an hour")
check("three is the default, and it is configurable",
      (daemon.silence_limit(),
       "silence_limit" in store.DEFAULT_CONFIG["thresholds"]), (3, True))
_sp = os.path.join(TMP, "silenceproj")
os.makedirs(_sp, exist_ok=True)
daemon.STATE.setdefault("unanswered", {}).pop(daemon.norm(_sp), None)
_held = [daemon.note_silence(_sp, "silenceproj", 40 + i) for i in range(1, 4)]
check("the first two are counted and let go", _held[:2], [False, False])
check("the third holds the pair", _held[2], True)
_rec = (daemon.STATE.get("paused") or {}).get(daemon.norm(_sp)) or {}
check("and the hold says why, in words a person can act on",
      "has not answered" in (_rec.get("why") or ""), True)
check("the count is in the readout the panel polls",
      daemon.situation(_sp)["unanswered"], 3)
print("   a held pair stops MAKING reports - there is no point adding to a")
print("   pile nobody is reading, and that is what turned three into 32")
check("run_review returns before it makes one",
      'if "has not answered" in (_held.get("why") or ""):'
      in inspect.getsource(daemon.run_review), True)
print("   what was missed is not lost, and comes back as one line rather")
print("   than as a flood")
_missed = daemon.clear_silence(_sp, "silenceproj")
check("a live verdict says how many went unanswered", _missed, 3)
check("and lifts the hold",
      bool((daemon.STATE.get("paused") or {}).get(daemon.norm(_sp))),
      False)
check("the reports themselves are on disk, not in memory",
      "inbox_write" in inspect.getsource(daemon.run_review), True)
print("   and silence no longer resolves as a verdict at all")
_src = inspect.getsource(daemon.run_review)
check("run_review distinguishes answered from unanswered",
      'answered = waiter["verdict"] is not None' in _src, True)
check("and calls the counter on the unanswered branch",
      "note_silence(path, project, n)" in _src, True)

print("\n53. the planner runs the check, because the planner cannot run")
print("   anything. Bash, PowerShell and every edit tool are denied to it")
print("   by disallow_for, so 'I verified the fix' could only ever mean")
print("   'I read that it was fixed' - a rule about behaviour with no")
print("   mechanism under it, which is the shape of defect this project")
print("   keeps finding in itself")
_cp = os.path.join(TMP, "checkproj")
os.makedirs(os.path.join(_cp, "bridgecore"), exist_ok=True)
# A real file, because the Checked: block below is opened for real by the
# older half of the same gate. Using a path that does not exist would fail
# these cases for a reason that has nothing to do with what they test.
with open(os.path.join(_cp, "bridgecore", "store.py"), "w",
          encoding="utf-8") as _fh:
    _fh.write("# a file the gate can find\n")
_cpn = daemon.norm(_cp)
daemon.CFG.setdefault("projects", {})[_cp] = {}
daemon.STATE.pop("checks", None)

print("   which projects it applies to, and why not all of them")
check("the bridge's own project is accepted by these suites",
      daemon.check_kinds(daemon.norm(os.path.dirname(daemon.ROOT))),
      ["suites"])
check("somebody else's project is not - running our suites over their "
      "shader would prove nothing, and demanding it would block that pair "
      "for ever on evidence that can never become relevant",
      daemon.check_kinds(_cpn), [])
daemon.CFG["projects"][_cp] = {"checks": ["suites"]}
check("a project earns the requirement by naming it in config",
      daemon.check_kinds(_cpn), ["suites"])
daemon.CFG["projects"][_cp] = {"checks": ["rm -rf /"]}
check("and the list is a vocabulary, never a command line: an entry that "
      "is not a known kind is dropped rather than run",
      daemon.check_kinds(_cpn), [])
daemon.CFG["projects"][_cp] = {"checks": ["suites"]}

print("   the tool takes no command, and never will")
_ref = daemon.run_check(_cpn, "nosuch")
check("an unknown suite is refused, not passed through",
      (_ref["ok"], _ref["refused"]), (False, True))
check("and the refusal names the ones that exist",
      all(s in _ref["why"] for s in daemon.CHECK_SUITES), True)
check("the endpoint accepts a suite NAME and nothing else - there is no "
      "argument through which a command could arrive",
      sorted((daemon.run_check.__code__.co_varnames or ())[:2]),
      ["path", "suite"])

print("   a real run, with the process spawning stubbed out: what the")
print("   planner gets back is exit codes and a folder, not a verdict")
_ran = []


def _fake_run(cmd, cwd, env, out_path):
    _ran.append((os.path.basename(str(cmd[-1])), env.get("BRIDGE_NO_HOOKS"),
                 env.get("BRIDGE_DATA"), cwd))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("stub\nEXIT=0\n")
    return 0, ["all cases pass"]


_real_run, daemon._run_one = daemon._run_one, _fake_run
_real_pkg, daemon._check_package = daemon._check_package, \
    lambda w, e, a: (0, ["RESULT: all files identical"])
try:
    _r = daemon.run_check(_cpn)
finally:
    daemon._run_one, daemon._check_package = _real_run, _real_pkg
check("every suite ran, plus py_compile and the package byte check",
      [row["what"] for row in _r["rows"]],
      ["py_compile"] + ["test_%s.py" % s for s in daemon.CHECK_SUITES]
      + ["verify_package"])
check("the result carries an exit code per line",
      all(isinstance(row["exit"], int) for row in _r["rows"]), True)
check("and a folder that exists, so the human can read the whole output",
      os.path.isdir(_r["dir"]), True)
# The artefacts belong to the project that was checked. Asserted because the
# first cut keyed them to this source tree instead, so running this very
# suite from a copy wrote a real folder beside that copy - outside TMP, where
# a suite has no business writing at all.
check("written beside the project that was checked, and inside this run's "
      "temp directory - a suite that writes outside TMP is the defect",
      _r["dir"].lower().startswith(_cp.lower()), True)
check("it ran in a COPY, not in the tree it is checking",
      all(daemon.ROOT.lower() not in (cwd or "").lower()
          for _n, _h, _d, cwd in _ran), True)
check("with hooks off, so the run does not take a seat in the panel as a "
      "session nobody launched",
      sorted({h for _n, h, _d, _c in _ran}), ["1"])
check("and its own BRIDGE_DATA, so it cannot write the live state",
      all(_d and daemon.ROOT.lower() not in _d.lower()
          for _n, _h, _d, _c in _ran), True)

print("   and now the gate: accepting code you did not run is refused")
_rep = ("Fixed the parsing in bridgecore/store.py, all suites green.\n"
        "Residence: bridgecore/store.py:norm")
_fb = "Good. Checked: bridgecore/store.py\nResidence: bridgecore/store.py:norm"
daemon.PENDING[_cpn] = {"content": _rep, "made": time.time()}
daemon.STATE.pop("checks", None)
_ok, _why, _kind = daemon.verdict_gate(_cpn, "done", _fb)
check("'done' on a report that changed code, with no check at all, is "
      "refused", _ok, False)
check("and the refusal says to call the tool rather than complaining",
      "check tool" in _why, True)

print("   a check that ran BEFORE the report says nothing about it")
daemon.STATE.setdefault("checks", {})[_cpn] = {
    "at": time.time() - 600, "ok": True, "rows": [], "dir": _r["dir"]}
_ok, _why, _kind = daemon.verdict_gate(_cpn, "done", _fb)
check("a check older than the report does not count", _ok, False)
check("and the refusal shows both times, so the planner can see why",
      _why.count(":") >= 4, True)

print("   a check that FAILED blocks acceptance and says what broke")
daemon.STATE["checks"][_cpn] = {
    "at": time.time(), "ok": False, "dir": _r["dir"],
    "rows": [{"what": "test_multipair.py", "exit": 1, "tail": ["FAILED: 2"]}]}
_ok, _why, _kind = daemon.verdict_gate(_cpn, "done", _fb)
check("a failed check refuses 'done'", _ok, False)
check("naming the suite that broke, not just that something did",
      "test_multipair.py" in _why, True)
check("and pointing at the output on disk", _r["dir"] in _why, True)

print("   a fresh, passing check lets the same verdict through")
daemon.STATE["checks"][_cpn] = {
    "at": time.time(), "ok": True, "rows": [], "dir": _r["dir"]}
_ok, _why, _kind = daemon.verdict_gate(_cpn, "done", _fb)
check("done goes through once the planner has actually run it",
      (_ok, _kind), (True, "artifacts"))

print("   and the gate does not fire where it would mean nothing")
daemon.CFG["projects"][_cp] = {}
daemon.STATE.pop("checks", None)
_ok, _why, _kind = daemon.verdict_gate(_cpn, "done", _fb)
check("a project that names no checks is accepted without one",
      _ok, True)
daemon.CFG["projects"][_cp] = {"checks": ["suites"]}
daemon.STATE.pop("checks", None)
check("'continue' never needs a check - it does not accept anything",
      daemon.verdict_gate(_cpn, "continue", "Checked: bridgecore/store.py")[0],
      True)
check("'wait' is free of all of it", daemon.verdict_gate(_cpn, "wait", "")[0],
      True)
daemon.PENDING.pop(_cpn, None)

print("\n54. 'stopped with an error: unknown' - 319 times out of 319")
print("   Every StopFailure this bridge has ever journalled, from")
print("   2026-07-28 to 2026-08-19, says 'unknown'. A field that has never")
print("   once been populated is not the field the client fills in. The")
print("   reason was on disk the whole time, one file away: the client")
print("   writes it into the transcript as an isApiErrorMessage record")
_sf = os.path.join(TMP, "sfproj")
os.makedirs(_sf, exist_ok=True)
_sfn = daemon.norm(_sf)
daemon.CFG.setdefault("projects", {})[_sf] = {}
daemon.CFG.setdefault("thresholds", {})["stopfail_grace"] = 150
daemon.STATE.pop("stopfail", None)
daemon.STATE.pop("stop_seen", None)

print("   first: a reason under whatever name the client happens to use")
_r, _w, _k = daemon.stopfail_reason(
    {"hook_event_name": "StopFailure", "error": "rate limit reached"},
    _sf, "executor")
check("a plausible key is read even though it is not error_type",
      (_r, _w), ("rate limit reached", "error"))
_r, _w, _k = daemon.stopfail_reason(
    {"hook_event_name": "StopFailure",
     "failure": {"message": "context window exceeded"}}, _sf, "executor")
check("and one nested a level down", (_r, _w),
      ("context window exceeded", "failure.message"))

print("   second: the transcript, which is where it really lives today.")
print("   The fixture repeats the shape of a real record read off this")
print("   machine - message.content is a LIST of blocks, and the text is")
print("   verbatim from 2026-08-19")
_tp = os.path.join(TMP, "sf-transcript.jsonl")
_real = {"type": "assistant", "isApiErrorMessage": True,
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(time.time())) + ".000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text",
              "text": "API Error: Connection closed mid-response. The "
                      "response above may be incomplete."}]}}
with open(_tp, "w", encoding="utf-8") as _fh:
    for _i in range(50):                       # a tail, not a whole file
        _fh.write(_json.dumps({"type": "assistant", "n": _i}) + "\n")
    _fh.write(_json.dumps(_real) + "\n")
_r, _w, _k = daemon.stopfail_reason(
    {"hook_event_name": "StopFailure", "transcript_path": _tp}, _sf,
    "executor")
check("the client's own words are recovered from the transcript",
      (_r, _w),
      ("API Error: Connection closed mid-response. The response above may "
       "be incomplete.", "transcript"))

print("   an error too old to be this turn's is not borrowed")
_old = dict(_real)
_old["timestamp"] = time.strftime(
    "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600)) + ".000Z"
_tp2 = os.path.join(TMP, "sf-old.jsonl")
with open(_tp2, "w", encoding="utf-8") as _fh:
    _fh.write(_json.dumps(_old) + "\n")
_r, _w, _k = daemon.stopfail_reason(
    {"hook_event_name": "StopFailure", "transcript_path": _tp2}, _sf,
    "executor")
check("an hour-old API error is not passed off as this failure",
      _w, "nothing")

print("   third: say so plainly, and keep the payload so the next one can")
print("   be read rather than reasoned about - nothing kept them before,")
print("   which is exactly why this could not be diagnosed from the")
print("   bridge's own records")
_ev = {"hook_event_name": "StopFailure", "session_id": "abc123",
       "cwd": _sf, "something_new": "a field nobody has seen yet"}
_r, _w, _k = daemon.stopfail_reason(_ev, _sf, "planner")
check("it admits it rather than inventing a word", "reported no reason"
      in _r, True)
check("the raw payload was written to disk", bool(_k) and os.path.isfile(_k),
      True)
check("and it is the whole payload, unedited",
      _json.loads(read_or_fail(_k, "the StopFailure payload") or "{}")
      .get("something_new"),
      "a field nobody has seen yet")
check("the reason points the reader at it", _k in _r, True)
check("kept beside the project, under bridge-logs",
      _k.startswith(os.path.join(_sf, "bridge-logs")), True)

print("   and the word 'unknown' is gone from the line a person reads")
check("the daemon no longer has a default of 'unknown' for this",
      'or "unknown").lower()' in inspect.getsource(daemon.handle_event),
      False)

print("   the second half, and the larger one: a turn that died and never")
print("   came back. On 2026-08-19, of 22 StopFailure events, 18 were")
print("   followed by NO report at all - no Stop hook, so no report, so no")
print("   verdict, so nothing woke the executor. The session went to 'idle")
print("   at the prompt' a minute later and the pair simply stood there")
_told = []
_real_notify, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _told.append((kind, text))
try:
    daemon.note_stopfail(_sf, "executor", "API Error: Connection closed "
                                          "mid-response.", None)
    daemon.check_lost_turn(_sf)
    check("nothing is said while the turn might still come back", _told, [])
    daemon.STATE["stopfail"]["%s|executor" % _sfn]["at"] = time.time() - 200
    daemon.check_lost_turn(_sf)
    print("   since 2026-08-21 the bridge picks it back up ITSELF before it")
    print("   rings anybody: a turn that died of an API error is a breakage,")
    print("   not a question, and the owner asked for the loop to solve this")
    print("   kind of problem on its own")
    check("the first pass past the grace wakes nobody", len(_told), 0)
    check("it counted an attempt instead",
          daemon.STATE["stopfail"]["%s|executor" % _sfn]["revives"], 1)
    check("and wrote down what it tried",
          len(daemon.STATE["stopfail"]["%s|executor" % _sfn]["tried"]), 1)
    print("   the wait grows between attempts, so a fault that keeps")
    print("   happening backs off instead of spinning")
    for _i in range(daemon.LOST_TURN_TRIES):
        daemon.STATE["stopfail"]["%s|executor" % _sfn]["at"] = time.time() - 200
        daemon.check_lost_turn(_sf)
    check("after the attempts run out, the human is told once", len(_told), 1)
    _t0 = _told[0][1] if _told else ""
    check("and told what it means - the pair is stopped, not working",
          "still stopped, so this one needs you" in _t0, True)
    check("with the reason in it, not 'unknown'",
          "Connection closed" in _t0, True)
    check("and with what the bridge already tried, so he is not guessing",
          "picked it back up" in _t0, True)
    daemon.check_lost_turn(_sf)
    check("and not told again on every pass", len(_told), 1)

    print("   a turn that DID come back is not reported as lost")
    _told[:] = []
    daemon.STATE.pop("stopfail", None)
    daemon.note_stopfail(_sf, "planner", "whatever", None)
    daemon.STATE["stopfail"]["%s|planner" % _sfn]["at"] = time.time() - 200
    daemon.note_stop_seen(_sf, "planner")
    daemon.check_lost_turn(_sf)
    check("a Stop after the failure clears it silently", _told, [])
    check("and the record is dropped rather than left to nag",
          "%s|planner" % _sfn in (daemon.STATE.get("stopfail") or {}), False)
finally:
    daemon.notify = _real_notify

print("   and nothing on this path may raise: keeping evidence must never")
print("   be what breaks a hook")
check("an unwritable project directory costs the payload, not the event",
      daemon.keep_stopfail_payload({"a": 1}, os.path.join(TMP, "no-such"),
                                   "executor"), None)
check("and a payload that will not serialise is caught too",
      daemon.keep_stopfail_payload({"f": object()}, _sf, "executor") is not
      None, True)
daemon.STATE.pop("stopfail", None)
daemon.STATE.pop("stop_seen", None)

print("\n55. no invisible damage in anything that ships")
print("   Writing a Windows path through a shell heredoc turns the two")
print("   characters backslash-b into ONE byte, 0x08, and backslash-r into")
print("   a real newline. Both are invisible: the text reads merely wrong")
print("   ('..ridge.zip') rather than corrupt, so it survived several")
print("   passes of proof-reading and reached check_public.py, which is in")
print("   the package AND in the public repository. Nine of them, found on")
print("   2026-08-19 by scanning bytes rather than by reading")
# 0x0D is deliberately NOT on this list by itself: CRLF is an ordinary line
# ending and .bat files require it. A LONE carriage return is the damage -
# that is what a heredoc turns backslash-r into - so it is looked for
# separately, as a CR that no LF follows.
_ctl = {0x00, 0x07, 0x08, 0x0B, 0x0C, 0x1A, 0x1B}


def _wounds(data):
    hit = sorted({c for c in _ctl if bytes([c]) in data})
    if data.replace(b"\r\n", b"").count(b"\r"):
        hit.append(0x0D)
    return hit


_root = os.path.dirname(os.path.abspath(__file__))
_wounded = []
for _dirp, _dirs, _files in os.walk(_root):
    _dirs[:] = [d for d in _dirs
                if d not in ("__pycache__", ".git", "data", "bridge-logs")]
    for _fn in _files:
        if not _fn.endswith((".py", ".md", ".bat", ".html", ".json")):
            continue
        _full = os.path.join(_dirp, _fn)
        try:
            _b = open(_full, "rb").read()
        except OSError:
            continue
        _hit = _wounds(_b)
        if _hit:
            _wounded.append((os.path.relpath(_full, _root),
                             ["0x%02X" % c for c in _hit]))
check("nothing in this tree carries a control byte - a path that lost its "
      "backslash to a shell is a broken path however plausible it reads",
      _wounded, [])
print("   and the check can fail: a planted 0x08 is found")
_probe = os.path.join(TMP, "wounded.md")
with open(_probe, "wb") as _fh:
    _fh.write("the package line read `..".encode("utf-8")
              + bytes([0x08]) + "ridge.zip`\n".encode("utf-8"))
check("a file with one backspace byte in it is caught",
      _wounds(open(_probe, "rb").read()), [0x08])
check("a lone carriage return is caught too - the other half of the same "
      "accident", _wounds(b"E:" + chr(92).encode() + b"Bridge\rreleases"),
      [0x0D])
check("but ordinary CRLF is not, because .bat files are written that way",
      _wounds(b"@echo off\r\ncd source\r\n"), [])
check("and a clean file of the same text is not",
      _wounds(("the package line read `.." + chr(92)
               + "bridge.zip`\n").encode("utf-8")), [])

print("\n56. the rebuild finishes itself, and picks the right config")
print("   The owner was left a finish-layout.bat to run between stopping")
print("   and starting the bridge. He tripped on it - restarted the old")
print("   bridge instead, which left a second data/ inside the package")
print("   folder holding a config with one project instead of four. So the")
print("   step is gone: bridge.bat calls this before every start, and it is")
print("   silent when there is nothing to move")
from bridgecore import relayout                            # noqa: E402
_rb = os.path.join(TMP, "relayout")
_old = os.path.join(_rb, "bridge")
_pkg = os.path.join(_old, "bridge")
_new = os.path.join(_rb, "source")
os.makedirs(os.path.join(_old, "data", "logs", "2026-08-01"))
os.makedirs(os.path.join(_old, "data", "backups"))
os.makedirs(os.path.join(_pkg, "data", "logs", "2026-08-19"))
os.makedirs(os.path.join(_new, "bridgecore"))
_full = {"projects": {"a": {}, "b": {}, "c": {}, "d": {}},
         "telegram": {"token": "t", "chat_id": "1"},
         "pair_marks": {"a": "blue"}}
_trim = {"projects": {"a": {}}, "telegram": {"token": "t"}}
_json.dump(_full, open(os.path.join(_old, "data", "config.json"), "w",
                       encoding="utf-8"))
_json.dump(_trim, open(os.path.join(_pkg, "data", "config.json"), "w",
                       encoding="utf-8"))
for _n in ("state.json", "calibration.json", "models.json",
           "profiles.json"):
    _json.dump({"from": "full"},
               open(os.path.join(_old, "data", _n), "w", encoding="utf-8"))
    _json.dump({"from": "trimmed"},
               open(os.path.join(_pkg, "data", _n), "w", encoding="utf-8"))
open(os.path.join(_pkg, "data", "logs", "2026-08-19", "events.jsonl"), "w",
     encoding="utf-8").write("only in the losing folder\n")
open(os.path.join(_old, "data", "logs", "2026-08-01", "events.jsonl"), "w",
     encoding="utf-8").write("in the winning folder\n")

print("   the config is chosen by CONTENT, never by where it sits - a rule")
print("   about paths would pick the wrong file the next time the accident")
print("   takes a different shape")
check("more projects beats fewer",
      relayout.score_config(os.path.join(_old, "data", "config.json")) >
      relayout.score_config(os.path.join(_pkg, "data", "config.json")), True)
_win, _lose = relayout.pick_config(
    [os.path.join(_pkg, "data", "config.json"),      # the trimmed one FIRST,
     os.path.join(_old, "data", "config.json")])     # so order cannot decide
check("the full one wins whatever order it is offered in",
      os.path.relpath(_win, _rb).replace(chr(92), "/"),
      "bridge/data/config.json")
check("and the other is named as a loser, not discarded", len(_lose), 1)
check("an unreadable config loses to anything readable",
      relayout.score_config(os.path.join(_rb, "no-such.json")), (-1, 0, 0, 0))

import socket as _sock_mod                                  # noqa: E402
_qs = _sock_mod.socket(); _qs.bind(("127.0.0.1", 0))
_QUIET = _qs.getsockname()[1]; _qs.close()
_out = []
_r = relayout.migrate(_rb, out=_out.append, port=_QUIET)
check("it moved", _r["moved"], True)
check("taking the config with four projects", _r["projects"], 4)
check("and it says which, out loud, so a person can see it",
      any("4 projects in it" in ln for ln in _out), True)
check("the old tree is gone", os.path.isdir(_old), False)
_data = os.path.join(_new, "data")
check("the config that landed is the full one",
      len(_json.load(open(os.path.join(_data, "config.json"),
                          encoding="utf-8"))["projects"]), 4)
check("the state that travelled with it is the full one too",
      _json.load(open(os.path.join(_data, "state.json"),
                      encoding="utf-8")), {"from": "full"})
check("journals from BOTH folders survived - one is evidence, and losing "
      "it because it sat in the folder that lost a vote is the worst "
      "outcome here",
      sorted(os.listdir(os.path.join(_data, "logs"))),
      ["2026-08-01", "2026-08-19"])
check("the config that was not used is kept, not deleted",
      any(f.startswith("config-not-used")
          for f in os.listdir(os.path.join(_rb, "releases"))), True)
check("and the whole old layout was zipped before anything moved",
      any(f.endswith("-relayout.zip")
          for f in os.listdir(os.path.join(_rb, "releases"))), True)

print("   and again: a rebuild that only works once breaks the second time")
print("   somebody starts the bridge")
_r2 = relayout.migrate(_rb, out=lambda _m: None, port=_QUIET)
check("a second run has nothing to do", _r2["moved"], False)
check("and says so rather than pretending it worked", _r2["why"],
      "nothing to move")
check("the config is untouched by the second run",
      len(_json.load(open(os.path.join(_data, "config.json"),
                          encoding="utf-8"))["projects"]), 4)
check("pending() is what bridge.bat asks, and it now says no",
      relayout.pending(_rb), False)

print("   it refuses rather than guesses when there is nowhere to move to")
_lonely = os.path.join(TMP, "lonely")
os.makedirs(os.path.join(_lonely, "bridge", "data"))
_r3 = relayout.migrate(_lonely, out=lambda _m: None, port=_QUIET)
check("no source folder means no move", _r3["moved"], False)
check("and the old tree is still there, untouched",
      os.path.isdir(os.path.join(_lonely, "bridge")), True)

print("   the launcher calls it, so there is no step for anyone to take")
_bat = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(daemon.__file__))), "..", "bridge.bat")
_bat = os.path.normpath(_bat)
if os.path.exists(_bat):
    _txt = open(_bat, encoding="utf-8", errors="replace").read()
    check("bridge.bat runs relayout before the daemon",
          "-m bridgecore.relayout" in _txt
          and _txt.index("-m bridgecore.relayout")
          < _txt.index("-m bridgecore.daemon"), True)
    check("and there is no finish-layout.bat left to run",
          os.path.exists(os.path.join(os.path.dirname(_bat),
                                      "finish-layout.bat")), False)

import socket as _socket                                    # noqa: E402

print("\n57. you cannot double-click the wrong launcher")
print("   The owner restarted the bridge and it came up in the OLD layout:")
print("   there were four bridge.bat on disk and he reached one of the")
print("   three that should not exist. Measured that day - the live daemon")
print("   was writing bridge/data while source/data had never been made")
print("   at all. Deleting them mid-move is not the answer, because the")
print("   move is what deletes them; until then they are signposts")
_base = os.path.dirname(os.path.dirname(os.path.abspath(daemon.__file__)))
_old = os.path.join(_base, "bridge")
if os.path.isdir(_old):
    _stubs = [os.path.join(_old, "bridge.bat"),
              os.path.join(_old, "add-project.bat"),
              os.path.join(_old, "bridge", "bridge.bat"),
              os.path.join(_old, "bridge", "add-project.bat")]
    _live = [s for s in _stubs if os.path.exists(s)]
    check("every launcher left in the old tree is there to be found",
          len(_live) > 0, True)
    for _s in _live:
        _txt = open(_s, encoding="utf-8", errors="replace").read()
        check("%s starts nothing" % os.path.relpath(_s, _base).replace(
            chr(92), "/"),
            ("daemon" in _txt or "-m bridge" in _txt or "%PY%" in _txt),
            False)
        check("   and says where the real one is",
              "bridge.bat" in _txt and "Projects" in _txt, True)
else:
    print("   (the old tree is gone, so there is nothing left to stub -")
    print("    which is the state this case exists to reach)")

print("   the one launcher that does work finds its own folder, so it")
print("   cannot be started 'from the wrong place' - %~dp0 is absolute")
_root_bat = os.path.join(_base, "bridge.bat")
if os.path.exists(_root_bat):
    _t = open(_root_bat, encoding="utf-8", errors="replace").read()
    check("it changes to its own directory before anything else",
          'cd /d "%~dp0"' in _t, True)
    check("and steps down into source only if the package is there",
          'if exist "source' + chr(92) + 'bridgecore' + chr(92)
          + 'daemon.py" cd source' in _t, True)
    check("it finishes the rebuild before starting the daemon, not after",
          _t.index("-m bridgecore.relayout") < _t.index("-m bridgecore.daemon"),
          True)

print("   and the move refuses to run under a live daemon: moving the")
print("   state file and the journals out from under one would leave it")
print("   reading one folder and writing another, which is worse than")
print("   not moving at all")
from bridgecore import relayout as _rl                       # noqa: E402
_sock = _socket.socket()
_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
_sock.bind(("127.0.0.1", 0))
_sock.listen(1)
_busy = _sock.getsockname()[1]
try:
    _fake = os.path.join(TMP, "livecheck")
    os.makedirs(os.path.join(_fake, "bridge", "data"), exist_ok=True)
    os.makedirs(os.path.join(_fake, "source", "bridgecore"), exist_ok=True)
    _r = _rl.migrate(_fake, out=lambda _m: None, port=_busy)
    check("a busy port stops it before a single file moves",
          _r["moved"], False)
    check("and the refusal says why, and what to do instead",
          ("still running" in _r["why"] and "button" in _r["why"]), True)
    check("nothing was created in the new tree",
          os.path.exists(os.path.join(_fake, "source", "data")), False)
    check("and the old tree is untouched",
          os.path.isdir(os.path.join(_fake, "bridge", "data")), True)
finally:
    _sock.close()
_free = _socket.socket()
_free.bind(("127.0.0.1", 0))
_freeport = _free.getsockname()[1]
_free.close()
_r2 = _rl.migrate(_fake, out=lambda _m: None, port=_freeport)
check("with the port quiet it goes ahead - so the guard is the port, "
      "not a mood", _r2["moved"], True)

print("\n58. a report NOBODY EVER ANSWERS is still counted as silence")
print("    Case 52 pinned the branch where the waiter wakes with no verdict.")
print("    That is the rare shape. The common one - and the one that cost 37")
print("    reports on 2026-08-19 while STATE['unanswered'] stayed at 0 - is")
print("    the wait simply timing out, and there run_review returned before")
print("    it ever reached the counter. Rule 27 had a gate with the door")
print("    beside it: the accounting lived after an early exit")
_qp = os.path.join(TMP, "quietproj")
os.makedirs(_qp, exist_ok=True)
_qn = daemon.norm(_qp)
daemon.STATE.setdefault("unanswered", {}).pop(_qn, None)
(daemon.STATE.get("paused") or {}).pop(_qn, None)
daemon.PENDING.pop(_qn, None)
_thr = dict(daemon.CFG.get("thresholds") or {})
_saved_thr, _saved_deliver, _saved_notify = _thr.copy(), daemon.deliver_ex, daemon.notify
daemon.CFG["thresholds"] = dict(_thr, review_timeout=0.4,
                                channel_silence_warn=0.2, idle_hold=0)
# The report is handed over successfully - that is the whole point. The
# channel took the bytes; the session behind it answers nothing, ever.
daemon.deliver_ex = lambda *_a, **_k: (True, "ok")
daemon.notify = lambda *_a, **_k: None
try:
    _, _lp = daemon.loop_state(_qn)
    _before = (daemon.STATE.get("unanswered") or {}).get(_qn, 0)
    _out = daemon.run_review({}, _qn, _lp, "a report nobody will answer",
                             "quietproj", "executor")
    _after = (daemon.STATE.get("unanswered") or {}).get(_qn, 0)
finally:
    daemon.CFG["thresholds"] = _saved_thr
    daemon.deliver_ex, daemon.notify = _saved_deliver, _saved_notify
check("the hook is released with no verdict to carry", _out, None)
check("and the unanswered report is COUNTED, not forgotten",
      (_before, _after), (0, 1))
print("   the fix is where the counting lives, not what it counts:")
print("   note_silence itself was always right. So the proof is that the")
print("   run reaches the hold - three unanswered reports in a row and the")
print("   pair stops, which is what nobody got on 2026-08-19")
daemon.CFG["thresholds"] = dict(_thr, review_timeout=0.4,
                                channel_silence_warn=0.2, idle_hold=0)
daemon.deliver_ex = lambda *_a, **_k: (True, "ok")
daemon.notify = lambda *_a, **_k: None
try:
    for _ in range(2):
        daemon.PENDING.pop(_qn, None)
        _, _lp = daemon.loop_state(_qn)
        daemon.run_review({}, _qn, _lp, "another unanswered report",
                          "quietproj", "executor")
    _run = (daemon.STATE.get("unanswered") or {}).get(_qn, 0)
    _hold = (daemon.STATE.get("paused") or {}).get(_qn) or {}
finally:
    daemon.CFG["thresholds"] = _saved_thr
    daemon.deliver_ex, daemon.notify = _saved_deliver, _saved_notify
check("three in a row are counted", _run, 3)
check("and the third one holds the pair, with the reason in words",
      "has not answered" in (_hold.get("why") or ""), True)
daemon.clear_silence(_qn, "quietproj")

print("\n59. two channel processes, one key: the leftover cannot win it back")
print("    Measured on 2026-08-19, twice over 150 s: the planner's channel")
print("    registration alternated between port 56598 (the live window,")
print("    started 17:25 as bridgecore.channel) and port 49318 (pid 2840,")
print("    started the PREVIOUS evening as bridge.channel - a package name")
print("    that no longer exists). channel.py heartbeats every 45 s whatever")
print("    became of its window, and the record simply took whoever posted")
print("    last. So about half of every report was handed to a process")
print("    whose window had been gone for 21 hours, and accepted by it")
print("   the discriminator is when the PROCESS started, which is the same")
print("   fact a person uses to spot the leftover by eye")
check("the start time of a real process can be read at all - our own",
      isinstance(daemon.proc_started(os.getpid()), float), True)
check("and it is in the past, but not in the last microsecond",
      0 < time.time() - daemon.proc_started(os.getpid()) < 86400 * 30, True)
check("a pid that cannot exist reads as unknown rather than as a time",
      daemon.proc_started(-1), None)
_young, _old = {"pid": 111, "port": 56598}, {"pid": 222, "port": 49318}
_ages = {111: 2000.0, 222: 1000.0}          # 111 started later: it is live
_saved_ps = daemon.proc_started
daemon.proc_started = lambda p: _ages.get(int(p))
try:
    check("the younger process takes the record from the older one",
          daemon.channel_supersedes(_old, 111), True)
    check("and the older one is REFUSED when it heartbeats again - this is "
          "the flap, and this is where it stops",
          daemon.channel_supersedes(_young, 222), False)
    check("the same process re-registering is always allowed",
          daemon.channel_supersedes(_young, 111), True)
    print("   and it fails open: a start time we cannot read never refuses,")
    print("   because a platform we cannot see must keep its loop")
    daemon.proc_started = lambda _p: None
    check("unknown ages let the registration through",
          daemon.channel_supersedes(_young, 222), True)
finally:
    daemon.proc_started = _saved_ps
print("   the pid has to reach DISK, not just the in-memory registry: the")
print("   comparison above happens on the next heartbeat, and the record it")
print("   compares against is the one that survived a restart")
_reg = inspect.getsource(daemon.Handler.do_POST)
_disk = _reg[_reg.find('STATE.setdefault("channels"'):][:700]
check("the record written to state.json carries the pid",
      '"pid": body.get("pid")' in _disk, True)
print("   and a port that merely ANSWERS is no longer promoted to current:")
print("   accepting bytes is what the leftover did best")
check("deliver_ex does not re-register whatever answered",
      "CHANNELS[(norm(path), role)] = {\"port\": port"
      in inspect.getsource(daemon.deliver_ex), False)

print("\n60. a decision only a document knows is not a decision")
print("    The owner said the old tree stays. That lived in CLAUDE.md and")
print("    nowhere else - while bridge.bat runs the migration BEFORE the")
print("    daemon at every single start, and the read-only retry added the")
print("    same morning would have made the delete succeed. The next")
print("    restart by anybody would have removed it, document or no")
_kp = os.path.join(TMP, "keepproj")
os.makedirs(os.path.join(_kp, "bridge", "data"), exist_ok=True)
os.makedirs(os.path.join(_kp, "source", "bridgecore"), exist_ok=True)
check("without the mark there is a migration pending, as before",
      _rl.pending(_kp), True)
_mark = os.path.join(_kp, "bridge", _rl.KEEP_MARK)
open(_mark, "w", encoding="utf-8").write("kept\n")
print("   the mark alone is not enough: a half-moved tree must not be")
print("   stranded by a file somebody dropped into it")
check("with no finished layout to keep, the mark is ignored",
      (_rl.kept_on_purpose(_kp), _rl.pending(_kp)), (False, True))
os.makedirs(os.path.join(_kp, "source", "data"), exist_ok=True)
open(os.path.join(_kp, "source", "data", "config.json"),
     "w", encoding="utf-8").write("{}\n")
check("with the move complete, the mark is honoured",
      (_rl.kept_on_purpose(_kp), _rl.pending(_kp)), (True, False))
print("   and the line that actually deletes answers to it too, so the")
print("   guard does not depend on pending() being consulted first")
check("migrate refuses to remove a tree that is kept",
      "kept_on_purpose(base)" in inspect.getsource(_rl.migrate), True)
check("the tree is still there afterwards",
      os.path.isdir(os.path.join(_kp, "bridge")), True)
print("   removing the file is how the decision gets changed - it is the")
print("   one deliberate way back, and it is written inside the file")
os.remove(_mark)
check("delete the mark and the migration is due again",
      _rl.pending(_kp), True)

print("\n61. an UNDELIVERED report must not freeze the window for 20 minutes")
print("    A blocked Stop hook draws nothing, so while run_review waits the")
print("    executor's window looks dead - reported on 2026-08-19 as the")
print("    executor freezing and never refreshing. It waited review_timeout")
print("    even when the report had reached nobody: twenty minutes for an")
print("    answer to something no planner was ever given")
_fp = os.path.join(TMP, "frozenproj")
os.makedirs(_fp, exist_ok=True)
_fn = daemon.norm(_fp)
daemon.STATE.setdefault("unanswered", {}).pop(_fn, None)
(daemon.STATE.get("paused") or {}).pop(_fn, None)
daemon.PENDING.pop(_fn, None)
_thr61 = dict(daemon.CFG.get("thresholds") or {})
_sv = (_thr61.copy(), daemon.deliver_ex, daemon.notify, daemon.ensure_session)
daemon.CFG["thresholds"] = dict(_thr61, review_timeout=6.0,
                                channel_silence_warn=6.0,
                                undelivered_hold=0.3, idle_hold=0)
# Nothing takes it: no channel at all. This is the shape that froze.
daemon.deliver_ex = lambda *_a, **_k: (False, "absent")
daemon.notify = lambda *_a, **_k: None
daemon.ensure_session = lambda *_a, **_k: None
try:
    _, _lp61 = daemon.loop_state(_fn)
    _t0 = time.time()
    _out61 = daemon.run_review({}, _fn, _lp61, "a report that reaches nobody",
                               "frozenproj", "executor")
    _took = time.time() - _t0
finally:
    daemon.CFG["thresholds"] = _sv[0]
    daemon.deliver_ex, daemon.notify, daemon.ensure_session = _sv[1:]
check("the window is released in about the short hold, not the timeout",
      _took < 3.0, True)
print("   measured: %.2f s, against a review_timeout of 6.0 s" % _took)
check("and it is released with NO verdict - not reviewed is not consent",
      _out61, None)
check("the unanswered report is still counted",
      (daemon.STATE.get("unanswered") or {}).get(_fn, 0), 1)
check("and it is on disk where a person can read it",
      os.path.isdir(os.path.join(_fp, "bridge-logs")), True)
print("   a DELIVERED report is not cut short: a planner thinking for")
print("   minutes must not be interrupted, so only the undelivered case")
print("   gets the short hold")
_src61 = inspect.getsource(daemon.run_review)
check("the short hold applies only when nothing was sent",
      "if not sent:" in _src61 and "undelivered_hold" in _src61, True)
check("and the threshold is a named setting, not a number in the code",
      "undelivered_hold" in store.DEFAULT_CONFIG["thresholds"], True)
daemon.clear_silence(_fn, "frozenproj")

print("\n62. one folder, one project key")
print("    On 2026-08-19 config.json held one project under two spellings")
print("    of the same folder - capitals in one, lower case in the other -")
print("    so five configured projects showed as four pairs in /state.")
print("    handle_add_project keyed by os.path.abspath(), which keeps the")
print("    capitals exactly as typed, while everything else the bridge does")
print("    is keyed by norm(). Rule 28 at the level of a dictionary key")
_saved_projects = daemon.CFG.get("projects")
_twin_a = os.path.join(TMP, "Games", "Shiny_Thing")
_twin_b = _twin_a.lower()
_lonely = os.path.join(TMP, "Other_Work")
daemon.CFG["projects"] = {
    _twin_a: {"checks": ["suites"]},
    _twin_b: {"modes": {"executor": "plan"}},
    _lonely: {},
}
_folded = daemon.migrate_project_keys()
check("the duplicate spelling is folded away", len(_folded), 1)
check("and one key is left for that folder",
      len([k for k in daemon.CFG["projects"]
           if "shiny_thing" in k.lower()]), 1)
print("   merged, not dropped: the two halves can carry different settings")
print("   and the newer one is not knowably the one that was meant")
_kept = [v for k, v in daemon.CFG["projects"].items()
         if "shiny_thing" in k.lower()][0]
check("what only the loser said survives", _kept.get("modes"),
      {"executor": "plan"})
check("and what the winner said is untouched", _kept.get("checks"),
      ["suites"])
check("a project with no twin is left exactly as it was",
      daemon.norm(_lonely) in daemon.CFG["projects"], True)
check("running it again is a no-op", daemon.migrate_project_keys(), [])
daemon.CFG["projects"] = _saved_projects
print("   and the door it came through is shut: the key is normed now")
check("handle_add_project writes a normed key",
      "setdefault(norm(path), {})"
      in inspect.getsource(daemon.handle_add_project), True)

print("\n63. BRIDGE_PORT has to move the DAEMON, not only its clients")
print("    Every edge honoured it - hook.py, statusline.py, channel.py,")
print("    install.py, relayout.py all read BRIDGE_PORT - while the daemon")
print("    took its listening port from CFG alone. So setting it sent every")
print("    client to one port and left the daemon on 8765. On 2026-08-19 a")
print("    run that believed itself isolated bound the LIVE bridge's port:")
print("    netstat showed two processes LISTENING on 127.0.0.1:8765 at once,")
print("    which Windows SO_REUSEADDR permits, with connections landing on")
print("    whichever socket the stack picked")
_src63 = inspect.getsource(daemon.main)
check("the daemon reads BRIDGE_PORT before falling back to the config",
      'os.environ.get("BRIDGE_PORT") or CFG.get("port"' in _src63, True)
print("   and every edge still reads the same variable, so one setting")
print("   moves the whole bridge rather than half of it")
for _mod, _name in ((__import__("bridgecore.hook", fromlist=["x"]), "hook"),
                    (__import__("bridgecore.statusline", fromlist=["x"]),
                     "statusline")):
    check("%s takes its port from BRIDGE_PORT" % _name,
          'BRIDGE_PORT' in inspect.getsource(_mod), True)

print("\n64. nothing may lead back into the retired tree")
print("    The folder stays - that was the owner's decision - but nothing")
print("    is to USE it. That was an assertion until it was checked, and")
print("    the check found a live channel process running out of it. So the")
print("    question is asked by the code now instead of being remembered")
_rt = os.path.join(TMP, "retire")
_proj = os.path.join(_rt, "aproject")
os.makedirs(os.path.join(_proj, ".claude"), exist_ok=True)
os.makedirs(os.path.join(_rt, "bridge"), exist_ok=True)


def _settings(pypath, hook, args=None):
    entry = {"command": hook}
    if args is not None:
        entry["args"] = args
    with open(os.path.join(_proj, ".claude", "settings.json"), "w",
              encoding="utf-8") as fh:
        _json.dump({"env": {"PYTHONPATH": pypath, "PYTHONSAFEPATH": "1"},
                   "hooks": {"Stop": [{"hooks": [entry]}]}}, fh)


_good = os.path.join(_rt, "source")
_bad = os.path.join(_rt, "bridge")
_cfg64 = {"projects": {_proj: {}}}
_settings(_good, "python -m bridgecore.hook")
check("healthy settings name nothing retired",
      _rl.retired_tree_users(_cfg64, _rt), [])
print("   and it has to go red on the real shapes of a relapse, one by one")
_settings(_bad, "python -m bridgecore.hook")
_hits = _rl.retired_tree_users(_cfg64, _rt)
check("PYTHONPATH pointing back at it is caught",
      [p for p, _v in _hits if "PYTHONPATH" in p] != [], True)
_settings(_good, os.path.join(_bad, "hook.py"))
_hits = _rl.retired_tree_users(_cfg64, _rt)
check("a hook command reaching into it is caught",
      [p for p, _v in _hits if p.endswith("hook")] != [], True)
_settings(_good, "python", [os.path.join(_bad, "bridgecore", "hook.py")])
_hits = _rl.retired_tree_users(_cfg64, _rt)
check("and a hook whose script, its argument since 8.122, lies in it",
      [p for p, _v in _hits if p.endswith("hook")] != [], True)
_settings(_good, "python", [os.path.join(_good, "bridgecore", "hook.py")])
check("CONTROL: a script path in the live tree is not",
      _rl.retired_tree_users(_cfg64, _rt), [])
_settings(_good, "python -m bridgecore.hook")
with open(os.path.join(_proj, ".mcp.json"), "w", encoding="utf-8") as fh:
    fh.write(_json.dumps({"mcpServers": {"bridge": {"args": [_bad]}}}))
check("an .mcp.json reaching into it is caught",
      [p for p, _v in _rl.retired_tree_users(_cfg64, _rt)
       if ".mcp.json" in p] != [], True)
os.remove(os.path.join(_proj, ".mcp.json"))
check("and the tree itself being watched as a project is caught",
      [p for p, _v in _rl.retired_tree_users({"projects": {_bad: {}}}, _rt)
       if "watched project" in p] != [], True)
print("   the launcher is NOT a user: <base>\\bridge.bat begins with the")
print("   same letters as <base>\\bridge, and a substring test called it")
print("   one - the only 'user' the first census found was that bug")
check("bridge.bat is not mistaken for the tree",
      _rl.names_retired(os.path.join(_rt, "bridge.bat"), _rt), False)
check("the tree itself is", _rl.names_retired(_bad, _rt), True)
check("and so is anything inside it",
      _rl.names_retired(os.path.join(_bad, "bridge", "daemon.py"), _rt), True)

print("\n65. a path is allowed to contain a space")
print("    The acceptance gate refused an honest verdict naming a file the")
print("    planner had genuinely read, because _TOKEN splits on whitespace")
print("    and 'Bridge Git\\README.md' arrived as two stumps - neither of")
print("    which exists, so both were reported as forgeries BY NAME. The")
print("    planner then stopped naming files in that folder and reached for")
print("    other paths instead: the verdict passed with less truth in it")
print("    than before. A check that refuses good work gets worked around,")
print("    and this one was, the same evening")
_sp65 = os.path.join(TMP, "with space")
os.makedirs(_sp65, exist_ok=True)
_file65 = os.path.join(_sp65, "README.md")
open(_file65, "w", encoding="utf-8").write("real\n")
_gone65 = os.path.join(_sp65, "NOPE.md")
check("quoted, and really there: accepted",
      daemon.artifact_paths('Checked: "%s"' % _file65, ""), ([_file65], []))
check("the same in guillemets, which is what this pair types",
      daemon.artifact_paths("Checked: «%s»" % _file65, ""),
      ([_file65], []))
check("alone on its line, unquoted: also accepted",
      daemon.artifact_paths("Checked: %s" % _file65, ""), ([_file65], []))
print("   and the gate is not weakened: a quoted path that is NOT there is")
print("   still refused, by name. That is the half that must not be lost")
_f65, _d65 = daemon.artifact_paths('Checked: "%s"' % _gone65, "")
check("quoted but missing: still refused, by name", (_f65, _d65),
      ([], [_gone65]))
print("   fail-open is intact: a bare token is never a demand")
check("section numbers and versions are not paths",
      daemon.artifact_paths("Checked: §5.17, 2.1.232 and 5.104", ""),
      ([], []))
check("quotes round an ordinary word demand nothing",
      daemon.artifact_paths('Checked: no artifacts - "done"', ""), ([], []))
print("   what is deliberately NOT guessed: two spaced paths on one line,")
print("   unquoted. There is no way to tell where the first ends, so the")
print("   refusal says how to write it rather than inventing a boundary")
_f2, _d2 = daemon.artifact_paths("Checked: %s and %s" % (_file65, _gone65), "")
check("it does not silently accept the ambiguous form", _d2 != [], True)
check("and the refusal tells the writer to quote it",
      "CONTAINS A SPACE" in inspect.getsource(daemon.verdict_gate), True)

print("\n66. a loop record for a folder that never was a pair")
print("    STATE['loops'] kept an entry for the old layout's package folder")
print("    - never added as a project, never ran a turn, just a path the")
print("    daemon had once been started from. When that folder was deleted")
print("    on 2026-08-19 the row became a pointer to nothing. It was found")
print("    by asking whether anything still named the vanished path, which")
print("    is the check that exists for exactly this")
_gl = dict(daemon.STATE.get("loops") or {})
_here = os.path.join(TMP, "realfolder")
os.makedirs(_here, exist_ok=True)
_gone = os.path.join(TMP, "vanished")
daemon.STATE["loops"] = {
    _gone: {"active": False, "iteration": 0},          # the ghost
    _gone + "-worked": {"active": False, "iteration": 12},   # gone, but ran
    _gone + "-busy": {"active": True, "iteration": 0},       # gone, running
    _here: {"active": False, "iteration": 0},               # here, idle
}
_saved_cfg66 = daemon.CFG.get("projects")
# since 8.55 the sweep also drops channel records and pair colours of any
# folder that is not a project and is under the temp folder - which, with
# the projects narrowed to one below, is every other project of this suite.
# What the case narrows it puts back, as it does for loops and projects.
_saved_sess66 = dict(daemon.STATE.get("sessions") or {})
_saved_marks66 = dict(daemon.CFG.get("marks") or {})
daemon.CFG["projects"] = {daemon.norm(_here): {}}
# The note the same ghost left in another dictionary. It looks like
# history and is not: the daemon deletes it as soon as the loop starts.
daemon.STATE["loop_off"] = {
    _gone: {"at": "2026-08-03 10:51:49", "why": "you pressed stop"},
    _here: {"at": "2026-08-19 12:00:00", "why": "a real project, kept"},
}
_dropped, _notes = daemon.migrate_ghost_records()
check("the ghost is dropped", _dropped, [_gone])
check("and so is the note it left elsewhere",
      [n for n in _notes if "loop_off" in n] != [], True)
check("while the note of a live project is untouched",
      _here in daemon.STATE["loop_off"], True)
print("   and nothing else is, because any one condition alone would throw")
print("   away something real: a project on an unplugged drive has")
print("   iterations behind it, and a running one is running")
check("a missing folder WITH iterations is kept",
      _gone + "-worked" in daemon.STATE["loops"], True)
check("a missing folder that is ACTIVE is kept",
      _gone + "-busy" in daemon.STATE["loops"], True)
check("a folder that exists is kept whatever its counters",
      _here in daemon.STATE["loops"], True)
check("running it again drops nothing",
      daemon.migrate_ghost_records(), ([], []))
daemon.STATE["loops"] = _gl
daemon.STATE.pop("loop_off", None)
daemon.CFG["projects"] = _saved_cfg66
daemon.STATE["sessions"] = _saved_sess66
daemon.CFG["marks"] = _saved_marks66

print("\n67. the folder's own spelling is what a person is shown")
print("    Keys are folded to norm(), which on Windows is lower case, and")
print("    project_name used to recover the capitals from the config key.")
print("    Then migrate_project_keys folded the config keys too - correctly,")
print("    two spellings of one folder were two projects - and every name in")
print("    the panel lost its capitals in a single restart.")
print("    The disk was the better witness all along")
_np = os.path.join(TMP, "Nice_Name")
os.makedirs(_np, exist_ok=True)
_saved_cfg67 = daemon.CFG.get("projects")
daemon.CFG["projects"] = {daemon.norm(_np): {}}      # folded, lower case
check("the name keeps its capitals even though the key lost them",
      daemon.project_name(daemon.norm(_np)), "Nice_Name")
check("and asking by the typed spelling gives the same answer",
      daemon.project_name(_np), "Nice_Name")
print("   a folder that has gone away has no spelling to read, so the key")
print("   is all there is - it still answers rather than raising")
_np_gone = os.path.join(TMP, "deleted_project")
check("a missing folder still gets a name",
      daemon.project_name(_np_gone), "deleted_project")
daemon.CFG["projects"] = _saved_cfg67

print("\n68. a project with no bridge marks is named, not merely called down")
print("    A project was removed from the watch list on 2026-08-19 at")
print("    23:32:47 - `/remove-project` called uninstall(), correctly - then")
print("    came back into config.json without install ever running again.")
print("    On 2026-08-21 both windows launched into that state and the only")
print("    thing anybody heard was `window never came up`, ten minutes late,")
print("    naming neither the cause nor a file. marks_missing() is the half")
print("    that knows what is absent; it has to say WHICH file, because")
print("    `not installed` sends the reader to the wrong place")
import io as _io                                         # noqa: E402
import json as _js                                       # noqa: E402
from bridgecore import install as _inst                  # noqa: E402
from bridgecore import sessions as _sess                 # noqa: E402

# A whole project to compare against, built here rather than borrowed from
# the machine: a suite that passes only where the bridge happens to be
# installed is a suite that proves nothing on anybody else's disk.
# THE CLIENT'S CONFIG IS ONE OF THE MARKS, and the only one that is not a
# file in the project. A window launched into a folder the client has not
# been told to trust stops on the trust dialog with "No, exit" selected,
# runs no hooks, starts no MCP server - and nobody answers, because rule 29
# births it minimised and unfocused. Read verbatim off a fixture window's
# console on 2026-09-03. The file here is a throwaway one (BRIDGE_CLAUDE_JSON)
# with a stranger's entry in it, so "install did not touch anybody else" is a
# check and not a hope. The stranger's KEY is not path-shaped on purpose:
# check_public refuses an absolute local path in a published file whether it
# points at anything or not, and this suite is published.
_cj = os.environ["BRIDGE_CLAUDE_JSON"]
_io.open(_cj, "w", encoding="utf-8").write(_js.dumps(
    {"numStartups": 7,
     "projects": {"a-stranger-project": {"hasTrustDialogAccepted": False,
                                       "allowedTools": ["x"]}}}))

_whole = os.path.join(TMP, "whole_project")
os.makedirs(_whole, exist_ok=True)
_inst.install(_whole, "executor")

_blind = os.path.join(TMP, "blind_project")
os.makedirs(os.path.join(_blind, ".claude"), exist_ok=True)
# what an older uninstall() left behind: one of the bridge's allow entries
# survived, every mark that makes the pair work did not
_io.open(os.path.join(_blind, ".claude", "settings.json"), "w",
         encoding="utf-8").write(
    u'{"permissions": {"allow": ["mcp__bridge__task"]}}')
_io.open(os.path.join(_blind, ".mcp.json"), "w", encoding="utf-8").write(
    u'{"mcpServers": {"aftereffects": {"command": "node", "args": ["x"]}}}')
_gaps = _inst.marks_missing(_blind)
# seven since 2026-09-30: the bridge tools allowed by name are a mark too,
# because a planner in auto stops on one that is missing (8.59)
check("a stripped project reports every kind of missing mark",
      len(_gaps), 7)
check("the allow gap names the tools it lacks, and not the one it has",
      [g for g in _gaps if "permissions.allow lacks" in g and
       "mcp__bridge__check" in g and "mcp__bridge__loop" in g and
       "mcp__bridge__verdict" in g and "mcp__bridge__task" not in g] != [],
      True)
check("the hooks gap names the settings file it is about",
      any("settings.json" in g and "no bridge hook" in g for g in _gaps), True)
check("and it names every one of the eight events, not just the first",
      all(ev in " ".join(_gaps) for ev in _inst.EVENTS), True)
check("the channel gap names .mcp.json and says what it costs",
      any(".mcp.json" in g and "no channel" in g for g in _gaps), True)
check("PYTHONSAFEPATH is checked too - a stray bridgecore shadows the real one",
      any("PYTHONSAFEPATH" in g for g in _gaps), True)
print("   a whole project answers with an empty list, or the gate would fire")
print("   on every launch for ever and be switched off within the day")
check("a fully installed project reports nothing missing",
      _inst.marks_missing(_whole), [])
print("   it never raises: a folder that is gone, and unreadable JSON, are")
print("   different answers and neither is an exception")
check("a folder that is not there says so instead of raising",
      _inst.marks_missing(os.path.join(TMP, "nope_not_here"))[0].startswith(
          "the folder itself is gone"), True)
_bad = os.path.join(TMP, "bad_json_project")
os.makedirs(os.path.join(_bad, ".claude"), exist_ok=True)
_io.open(os.path.join(_bad, ".claude", "settings.json"), "w",
         encoding="utf-8").write(u"{not json at all")
check("unreadable settings say so rather than reading as 'absent'",
      any("not valid JSON" in g for g in _inst.marks_missing(_bad)), True)

print("   AND THE TRUST MARK, which lives in the client's config and not in")
print("   the project. Without it the window stops before its first hook,")
print("   and the default answer on that screen is `No, exit` - so a blind")
print("   keystroke does not rescue it either.")
check("the stripped project is named as untrusted, with the file and the key",
      any(".claude.json" in g and "not marked trusted" in g
          and "No, exit" in g for g in _gaps), True)
# A SECOND stripped project, because installing into _blind here would
# repair it before case 69 gets to prove that the launch gate repairs it -
# a test that quietly does another test's work is a test that stops failing.
_blind_t = os.path.join(TMP, "blind_for_trust")
os.makedirs(os.path.join(_blind_t, ".claude"), exist_ok=True)
_io.open(os.path.join(_blind_t, ".claude", "settings.json"), "w",
         encoding="utf-8").write(
    u'{"permissions": {"allow": ["mcp__bridge__task"]}}')
_inst.install(_blind_t, "executor")
_cj_after = _js.loads(_io.open(_cj, encoding="utf-8").read())
_bkey = os.path.abspath(_blind_t).replace("\\", "/")
check("install marks the folder trusted for the client",
      (_cj_after.get("projects") or {}).get(_bkey, {})
      .get("hasTrustDialogAccepted"), True)
check("and marks_missing stops naming it",
      any("not marked trusted" in g for g in _inst.marks_missing(_blind_t)),
      False)
print("   MERGED, never replaced: a stranger's entry and every other key")
print("   survive, and one backup is kept")
check("somebody else's project is untouched",
      (_cj_after.get("projects") or {}).get("a-stranger-project"),
      {"hasTrustDialogAccepted": False, "allowedTools": ["x"]})
check("and the keys that have nothing to do with us are still there",
      _cj_after.get("numStartups"), 7)
check("a backup of the client config was kept",
      os.path.isfile(_cj + ".before-bridge"), True)
check("the backup is what it was BEFORE the field went in",
      (_js.loads(_io.open(_cj + ".before-bridge", encoding="utf-8").read())
       .get("projects") or {}).get(_bkey, {}).get("hasTrustDialogAccepted"),
      None)
print("   CONTROL: it never raises and never invents a file - an unreadable")
print("   config and a missing one are both simply nothing done")
_cj_bad = os.path.join(TMP, "claude-not-json.json")
_io.open(_cj_bad, "w", encoding="utf-8").write(u"{not json")
check("unreadable client config: nothing done, nothing raised",
      _inst.trust_folder(_blind_t, config=_cj_bad), "")
check("a client config that does not exist: the same",
      _inst.trust_folder(_blind_t, config=os.path.join(TMP, "no-such.json")),
      "")
check("and a project already trusted is not rewritten",
      _inst.trust_folder(_blind_t), "")

print("\n69. the gate repairs at launch, and says so where a person will see")
print("    it. It lives in sessions.launch and not at any of its callers -")
print("    there are seven of them (panel start, handover, auto-restart,")
print("    the archive seat, the bridge's own window), and a gate on one of")
print("    them is a gate with the door left open beside it")
import inspect as _insp
_src = _insp.getsource(_sess.launch)
check("launch calls the gate before it builds anything",
      "ensure_marks(project, role)" in _src, True)
check("and it does so before the command is built",
      _src.index("ensure_marks") < _src.index("build_command"), True)
_gsrc = _insp.getsource(_sess.ensure_marks)
check("the gate repairs rather than refusing",
      "installer.install(" in _gsrc, True)
check("it journals at warn, so the repair cannot be silent",
      '"warn"' in _gsrc, True)
check("it re-checks after installing rather than assuming it worked",
      _gsrc.count("marks_missing") >= 2, True)
print("   it repairs a real stripped project and leaves the project's OWN")
print("   mcp server alone - this is the aftereffects case, byte for byte")
_saved_j = store.journal
_lines = []
store.journal = lambda *a, **k: _lines.append(a[1] if len(a) > 1 else "")
_sess.ensure_marks(_blind, "executor")
store.journal = _saved_j
check("after the gate runs, nothing is missing any more",
      _inst.marks_missing(_blind), [])
_srv = _js.load(_io.open(os.path.join(_blind, ".mcp.json"),
                         encoding="utf-8"))["mcpServers"]
check("the project's own aftereffects server survived the merge",
      "aftereffects" in _srv, True)
check("and the bridge server is there beside it, not instead of it",
      "bridge" in _srv, True)
check("the warning named the project and what was wrong",
      any("missing bridge marks" in ln for ln in _lines), True)
check("a whole project makes the gate say nothing at all",
      _sess.ensure_marks(_whole, "executor"), [])
print("   the watchdog now names what it waited for. `never came up` on its")
print("   own sent everybody to look at the window, which was alive")
_wsrc = _insp.getsource(daemon)
check("the watchdog line says what it waited for",
      "waited %d min for" in _wsrc, True)
check("and offers the missing marks as the likely reason",
      "carries no bridge marks" in _wsrc, True)
print("   and /config, the way a project re-enters the list without install,")
print("   reports it - repair there would be writing into somebody's project")
print("   as a side effect of saving settings")
check("_warn_unmarked_projects reports and does not repair",
      "installer.install(" not in _insp.getsource(
          daemon._warn_unmarked_projects), True)
_lines2 = []
store.journal = lambda *a, **k: _lines2.append(a[1] if len(a) > 1 else "")
_blind2 = os.path.join(TMP, "blind_two")
os.makedirs(_blind2, exist_ok=True)
daemon._warn_unmarked_projects({_blind2: {}})
store.journal = _saved_j
check("a blind project entering the watch list is named at that moment",
      any("carries no bridge marks" in ln for ln in _lines2), True)

print("\n70. an edit to a launch chain survives the render that follows it")
print("    The panel re-read the saved chains at the top of renderLaunch(),")
print("    and every edit called renderLaunch() one line after making it -")
print("    so add and x both wrote to CHAINS and threw the write away.")
print("    tick() repeated it every 2.5s. saveChains() runs on every launch,")
print("    so any project ever started had pc.chains and could not be edited")
print("    at all: remove the head, press start, and the head still started")
_panel = _io.open(os.path.join(os.path.dirname(daemon.__file__), "panel.html"),
                  encoding="utf-8").read()
check("the saved chains are only re-read when the user has not edited",
      "if(pc.chains&&!window._launchTouched)" in _panel, True)
check("adding a model counts as an edit",
      "CHAINS[role].indexOf(sel.value)<0){window._launchTouched=true;"
      in _panel, True)
check("removing a chip counts as an edit too - it is a click, not an input",
      "window._launchTouched=true;" in _panel
      and "CHAINS[b.dataset.r].splice" in _panel, True)
print("   the latch is per project. Carrying it across a switch would show -")
print("   and launch - the previous project's models")
# The same branch drops the unapplied drop-down picks too since 2026-09-23
# (DECISIONS.md 8.25), so the latch is matched up to its own statement.
check("renderLaunch drops the latch when the project changes",
      bool(re.search(r"if\(CUR!==window\._launchProj\)\{window\._launchProj="
                     r"CUR;window\._launchTouched=false[;}]", _panel)), True)
check("and a switch re-renders even mid-edit, or the old chain would stay",
      "(!window._launchTouched||CUR!==window._launchProj)" in _panel, True)
print("   once saved, the config agrees with the screen, so the panel may")
print("   follow it again - a latch left set freezes the window for ever")
check("saveChains clears the latch after the config is written",
      "window._launchTouched=false;return r})}" in _panel, True)
print("   and the thing the button sends is still the head of the chain")
print("   that is on screen")
check("launch sends the head of the chain",
      "model:(CHAINS[role]||[])[0]||null" in _panel, True)

print("\n71. the model the panel chose reaches the command line, both roles")
print("    The panel was the whole bug; this half was always honest, and")
print("    that is worth pinning so a later change cannot quietly drop it")
_caught = []


class _FakePopen(object):
    def __init__(self, cmd, **kw):
        _caught.append(list(cmd))
        self.pid = 4242

    def poll(self):
        return None


_real_popen = _sess.subprocess.Popen
_real_probe = daemon.maybe_auto_probe
_sess.subprocess.Popen = _FakePopen
daemon.maybe_auto_probe = lambda *a, **k: None
# THIS BLOCK MEANS THE REAL COMMAND LINE, which is the whole point of it:
# it replaces Popen rather than build_command precisely so it can read the
# flags sessions.py really produces. sessions.real_client_refused stops a
# suite reaching a real client (-> DECISIONS.md 8.11) and refused here
# before Popen ever saw the command, taking four checks with it. So this
# is the one place that says outright that it means it. Nothing can be
# spawned while _FakePopen is in front, and if it ever were not, _caught
# would be empty and every check below would fail loudly rather than a
# window opening quietly.
_was_real = os.environ.get("BRIDGE_REAL_CLIENT")
os.environ["BRIDGE_REAL_CLIENT"] = "1"
_lp = os.path.join(TMP, "launch_model_project")
os.makedirs(os.path.join(_lp, ".claude"), exist_ok=True)


def _spawn71():
    """The window's own launch among everything Popen was handed. It was
    `_caught[0]`, the first spawn of the launch, and on a Mac it stopped
    being the window on 2026-10-13: 8.121's on_network_share reads
    /sbin/mount before the window opens, and the window itself goes
    through tmux new-session with the client line as its tail - so the
    case read the mount table, found no --model, and its control passed
    on an empty list. Every window's line carries --permission-mode, and
    nothing else a launch spawns does."""
    return next((_c for _c in _caught if "--permission-mode" in _c), [])


for _role, _want in (("executor", "opus"), ("planner", "sonnet")):
    _caught[:] = []
    daemon.handle_session({"action": "launch", "project": _lp,
                           "role": _role, "model": _want})
    _cmd = _spawn71()
    check("%s: --model is on the command line" % _role, "--model" in _cmd, True)
    check("%s: and it is the alias that was asked for" % _role,
          _cmd[_cmd.index("--model") + 1] if "--model" in _cmd else None, _want)
print("   choosing nothing forces nothing - the client keeps its own default")
_caught[:] = []
daemon.handle_session({"action": "launch", "project": _lp,
                       "role": "planner", "model": None})
check("the window's launch was caught, so the next check has a line to read",
      bool(_spawn71()), True)
check("no model chosen means no --model flag", "--model" in _spawn71(), False)
_sess.subprocess.Popen = _real_popen
daemon.maybe_auto_probe = _real_probe
if _was_real is None:
    os.environ.pop("BRIDGE_REAL_CLIENT", None)
else:
    os.environ["BRIDGE_REAL_CLIENT"] = _was_real

print("\n72. tier 1: both halves waiting on each other")
print("    The case this was built for: the executor finished a piece")
print("    and believed it had sent it, nothing actually went out, and")
print("    both halves now wait for each other. Neither is stuck the")
print("    way tier 2 means - both are healthy and idle, and neither")
print("    will move, because each believes the ball is with the other")
_cp = os.path.join(TMP, "clinch_project")
os.makedirs(_cp, exist_ok=True)
_ck = daemon.norm(_cp)


def _sit(**kw):
    base = {"loop": True, "paused": False, "reviewing": False,
            "verdict_in_flight": False, "handover": False, "inflight": [],
            "roles": {"executor": {"alive": True, "tail": []},
                      "planner": {"alive": True, "tail": []}}}
    base.update(kw)
    return base


daemon.STATE["last_task"] = {_ck: time.time() - 4000}
daemon.STATE["stop_seen"] = {"%s|executor" % _ck: time.time() - 5000}
daemon.STATE["idle_holding"] = {}
_f = daemon.clinch(_cp, _sit())
check("a pair with work owed and nothing moving is a clinch",
      bool(_f), True)
check("and it names the missing hop rather than saying 'stuck'",
      (_f or {}).get("why"), "task_no_turn")
check("naming which half to wake",
      (_f or {}).get("wake"), "executor")
print("   every legitimate reason to be quiet is excluded FIRST - each of")
print("   these is a working pair, not a deadlock")
check("a report being judged is not a clinch",
      daemon.clinch(_cp, _sit(reviewing=True)), None)
check("a verdict on its way is not a clinch",
      daemon.clinch(_cp, _sit(verdict_in_flight=True)), None)
check("a command still running is not a clinch",
      daemon.clinch(_cp, _sit(inflight=["build"])), None)
check("a handover under way is not a clinch",
      daemon.clinch(_cp, _sit(handover=True)), None)
check("a paused pair is not a clinch",
      daemon.clinch(_cp, _sit(paused=True)), None)
check("and the loop being off is not a clinch - that is a decision",
      daemon.clinch(_cp, _sit(loop=False)), None)
print("   the idle damper is the trap here: while it holds a pair, PENDING")
print("   is NOT set, so a held pair looks exactly like a clinch from")
print("   outside. Calling the damper a deadlock every time it worked is")
print("   the one way this check could have made things worse")
daemon.STATE["idle_holding"] = {_ck: time.time()}
check("a pair the damper is holding is not a clinch",
      daemon.clinch(_cp, _sit()), None)
daemon.STATE["idle_holding"] = {}
check("and it is a clinch again once the hold comes off",
      bool(daemon.clinch(_cp, _sit())), True)
print("   a fresh turn is movement, so the clock restarts")
daemon.STATE["stop_seen"] = {"%s|executor" % _ck: time.time()}
check("a pair that has just moved is not a clinch",
      daemon.clinch(_cp, _sit()), None)

print("\n73. tier 2: a half that owes work and has stopped writing")
print("    Three conditions, all required: it owes something, its")
print("    transcript has not grown, and nothing is legitimately running.")
print("    The threshold is MEASURED - across every journal this bridge has")
print("    written, 5400 tracked commands ran median 3s, p95 58s, p99 311s,")
print("    and turn gaps p95 843s. 600s is about twice the p99 command")
_sp = os.path.join(TMP, "stall_project")
os.makedirs(_sp, exist_ok=True)
print("   a transcript that cannot be found says NOT frozen - fail open, or")
print("   the detector accuses a pair it simply cannot see")
_saved_best = daemon.best_session
daemon.best_session = lambda p, r: {}
check("no session means not frozen",
      daemon.transcript_frozen(_sp, "executor", 1), (False, 0))
daemon.best_session = _saved_best
print("   busy is busy: a tracked command or a compaction is a reason to be")
print("   quiet, and neither may be called a stall")
daemon.STATE["inflight"] = {daemon.norm(_sp): {"x": {"cmd": "build"}}}
check("a tracked command means something is in flight",
      daemon.tool_in_flight(_sp, "executor"), True)
daemon.STATE["inflight"] = {}
check("a compacting session is in flight too",
      daemon.tool_in_flight(_sp, "executor",
                            {"roles": {"executor": {"state": "compacting"}}}),
      True)
check("and a session waiting on a process is in flight",
      daemon.tool_in_flight(_sp, "executor",
                            {"roles": {"executor":
                                       {"state": "waiting on a process"}}}),
      True)
check("an idle session is not",
      daemon.tool_in_flight(_sp, "executor",
                            {"roles": {"executor": {"state": "idle"}}}),
      False)
print("   and a stall is never looked for where nothing is owed")
check("the loop being off means no stall check at all",
      daemon.stalled(_sp, _sit(loop=False)), None)
check("nor while a handover is under way",
      daemon.stalled(_sp, _sit(handover=True)), None)

print("\n74. tier 3: the poll is untouched; a hand-back rings once")
print("    The half-hourly poll keeps its cadence and its reach - it wakes")
print("    halves that have gone dull, and the owner was explicit that")
print("    limiting it would remove the thing it is for. What was missing")
print("    is the other end: on 2026-08-21 the planner declined the same")
print("    question 15 times between 05:48 and 09:38, correctly, and not")
print("    one needs_you reached anybody (measured: 15 asks, 0 notifies)")
_op = os.path.join(TMP, "owner_q_project")
os.makedirs(_op, exist_ok=True)
check("an explicit hand-back to the owner is recognised",
      daemon.planner_declined("this is the owner's decision"), True)
check("and wording this installation adds is honoured too",
      daemon.planner_declined("call the human about this"), True)
print("   a pair working in another language adds its own wording in")
print("   config.json rather than in the source: the public repository is")
print("   English-only by design and check_public.py enforces that, so a")
print("   hard-coded list in another language could only ship by weakening")
print("   the very gate nobody may weaken to get their own file through")
_saved_marks = daemon.CFG.get("decline_marks")
daemon.CFG["decline_marks"] = ["ceci regarde le proprietaire"]
check("a mark from config is honoured",
      daemon.planner_declined("desole, ceci regarde le proprietaire"), True)
check("and the shipped ones still are, never one instead of the other",
      daemon.planner_declined("the owner decides"), True)
daemon.CFG["decline_marks"] = _saved_marks
check("a config that names nothing leaves the shipped list alone",
      len(daemon.decline_marks()) >= len(daemon.DECLINE_MARKS), True)
print("   a planner that has simply not answered yet is NOT declining -")
print("   waking exactly that case is what the poll is for")
check("silence is not a decline",
      daemon.planner_declined(""), False)
check("nor is ordinary work",
      daemon.planner_declined("Checked the file, continue with the next "
                              "piece"), False)
_qt = [{"who": "executor", "text": "Do we push to the public repo?"}]
daemon.STATE["owner_question"] = {}
check("the first hand-back on a wait rings the phone",
      daemon.note_owner_question(_op, _qt), True)
check("the same wait does not ring again - the chat is a phone, not a log",
      daemon.note_owner_question(_op, _qt), False)
check("and still does not, however many times it is asked",
      daemon.note_owner_question(_op, _qt), False)
print("   a different wait is a different question, and rings on its own")
_qt2 = [{"who": "executor", "text": "Which schema did you want?"}]
check("a new wait rings again",
      daemon.note_owner_question(_op, _qt2), True)
check("the fingerprint is what tells them apart",
      daemon.question_fingerprint(_qt) != daemon.question_fingerprint(_qt2),
      True)
print("   and the call itself is one message of a kind that reaches a phone")
check("needs_you is on TELEGRAM_KINDS",
      "needs_you" in daemon.TELEGRAM_KINDS, True)
_rang = []
_rn, _rj = daemon.notify, store.journal
daemon.notify = lambda kind, text, **kw: _rang.append((kind, text))
store.journal = lambda *a, **k: None
daemon.call_human_about(_op, _qt)
daemon.notify, store.journal = _rn, _rj
check("exactly one message goes out", len(_rang), 1)
check("of the kind that rings", _rang[0][0] if _rang else None, "needs_you")
check("carrying what the executor actually asked",
      "Do we push to the public repo?" in (_rang[0][1] if _rang else ""),
      True)

print("\n75. the chat is a phone, and it had been turned into a log")
print("    Seven messages the owner pointed at in one morning, all the same")
print("    disease. What arrives has to be worth looking up for")
print("   (A) compaction is routine and says nothing anyone acts on. It")
print("   stays in the journal and on the panel; only the approach to the")
print("   wall goes out")
check("PreCompact never reaches the chat",
      "PreCompact" in daemon.CHAT_SILENT_EVENTS, True)
check("and neither does the raw turn-error, which said nothing to do",
      "StopFailure" in daemon.CHAT_SILENT_EVENTS, True)
print("   the useful version of that error still goes out three minutes")
print("   later from check_lost_turn - one event, one message, and this is")
print("   the one that names the next move")
check("check_lost_turn still calls a human",
      'notify("crash"' in inspect.getsource(daemon.check_lost_turn), True)
print("   (B) every message about ONE pair carries that pair's colour. The")
print("   event tail sent most of them and never passed the project at all")
_tail = inspect.getsource(daemon.handle_event)
check("the event tail names the pair it is about",
      "notify(setting, text, path=path)" in _tail, True)
print("   limit_low has three jobs and they part company here: the")
print("   five-hour limit is the account's and stays colourless; the")
print("   context-percentage line is compaction and leaves the chat; the")
print("   chain running out IS the wall and stays, with colour")
_src = inspect.getsource(daemon)
check("the five-hour limit is still sent, still without a pair",
      'notify("limit_low", "Five-hour limit at %d%%." % int(pct))' in _src,
      True)
check("the context-percentage line is journalled, not notified",
      'store.journal("limit_low",' in _src, True)
# By the tokens, not the wrapping: a re-wrap is not a change of claim.
check("and the chain running out carries its pair",
      bool(_re.search(r'left in the chain\. Waiting for the reset\."'
                      r'\s*% project, path=path\)', _src)), True)
print("   (C) the same fact twice is not twice the information. The window")
print("   is measured: 1275 repeats of one (project, kind) in the journals,")
print("   4% inside a minute, 37% inside five, median gap 555s")
daemon.STATE["said"] = {}
_p = os.path.join(TMP, "noisy_project")
check("the first time a fact is said, it goes",
      daemon.notify_seen_recently("crash", "the turn died", _p), False)
check("the same fact again, straight after, does not",
      daemon.notify_seen_recently("crash", "the turn died", _p), True)
print("   but a DIFFERENT fact about the same pair must never be swallowed -")
print("   a guard on (project, kind) alone would hide a real second problem")
check("a different message of the same kind still goes",
      daemon.notify_seen_recently("crash", "the window vanished", _p), False)
check("and the same words about a different pair go too",
      daemon.notify_seen_recently("crash", "the turn died",
                                  os.path.join(TMP, "other_project")), False)
print("   and it lapses: an hour later the same fact is news again")
daemon.STATE["said"] = dict(
    (k, v - 4000) for k, v in daemon.STATE["said"].items())
check("once the window has passed the fact may be said again",
      daemon.notify_seen_recently("crash", "the turn died", _p), False)
print("   (D) a command is not slow because it is slower than usual. With")
print("   usual=2s the old rule fired after six seconds - that is where")
print("   \"has run 0 min (usual: 2s)\" came from. The floor is absolute and")
print("   measured: p99 of 5400 tracked commands is 311s")
check("a 2s command running 60s is not stuck",
      60 > daemon.stuck_limit(2.0), False)
check("nor is it at 300s, still under the measured p99",
      300 > daemon.stuck_limit(2.0), False)
check("past the floor it is worth a look",
      400 > daemon.stuck_limit(2.0), True)
check("a genuinely long-running command raises the bar, never lowers it",
      daemon.stuck_limit(600.0), 1800.0)
check("and with no history at all the old 15 minutes still applies",
      daemon.stuck_limit(None), 900.0)
print("   the pair is asked first - its planner has wait and task and can")
print("   settle this without waking anybody. The human hears only if the")
print("   pair was unreachable, or the grace passed with it still running")
print("   the deciding lives in check_processes; process_watch is the loop")
print("   around it, split the same way as stall_watch/check_stalls so it")
print("   can be run in a test at all")
_pw = inspect.getsource(daemon.check_processes)
check("the planner is asked before the human",
      _pw.index('deliver(path, "planner"') < _pw.index('notify("process_stuck"'),
      True)
check("the human is held back by a grace",
      "stuck_planner_grace" in _pw, True)
check("and the raw command tail no longer goes to the phone",
      'brief(meta["cmd"]))' in _pw, False)

print("\n76. one sound, and only one")
print("    The owner: only \"work finished, needs checking\" should make a")
print("    noise. Everything else still arrives - it is simply quiet, which")
print("    is the difference between a phone you can leave on the table")
check("run_finished is the only kind that sounds by default",
      daemon.SOUND_DEFAULT, ("run_finished",))
check("but the quiet ones still reach the chat",
      all(k in daemon.TELEGRAM_KINDS
          for k in ("needs_you", "crash", "session_died")), True)
print("   the panel writes the defaults of the day into config, so a saved")
print("   level equal to the OLD default is a copy of a default, not a")
print("   decision - and it would shadow the new one for ever. Same case")
print("   migrate_executor_mode was written for")
_saved_cfg75 = daemon.CFG.get("notify")
daemon.CFG["notify"] = {"needs_you": "sound", "process_stuck": "log",
                        "run_finished": "sound"}
_dropped = daemon.migrate_notify_levels()
check("yesterday's default is dropped",
      "needs_you" in _dropped, True)
check("so the new default decides",
      daemon.CFG["notify"].get("needs_you"), None)
check("a level chosen deliberately survives",
      daemon.CFG["notify"].get("process_stuck"), "log")
check("and run_finished keeps its sound, being the one that still sounds",
      daemon.CFG["notify"].get("run_finished"), "sound")
daemon.CFG["notify"] = _saved_cfg75

print("\n77. work already in the bridge's hands comes first")
print("    A task delivered WHILE a turn is running is nobody's: the turn")
print("    already has its subject, it ends with a report about that, the")
print("    planner accepts it - and the task that arrived in the middle is")
print("    never picked up. The executor waits for work it was already")
print("    given; the planner waits for a report on work it thinks was")
print("    taken. On 2026-08-21 four tasks landed mid-turn, the turn ended")
print("    with report 120, the verdict was done, and the pair stood still")
print("    until a person noticed")
_tp = os.path.join(TMP, "held_task_project")
daemon.STATE["tasks_open"] = {}
daemon.note_task_sent(_tp, "FIRST piece", mid_turn=True)
daemon.note_task_sent(_tp, "SECOND piece", mid_turn=True)
check("a task that lands mid-turn is kept",
      len(daemon.STATE["tasks_open"][daemon.norm(_tp)]), 2)
print("   and the OLDEST comes back first - a queue, not a stack")
check("the oldest is handed back first",
      (daemon.take_open_task(_tp) or {}).get("text"), "FIRST piece")
check("then the next",
      (daemon.take_open_task(_tp) or {}).get("text"), "SECOND piece")
check("and then there is nothing held",
      daemon.take_open_task(_tp), None)
print("   a task delivered to an IDLE executor is the next piece already -")
print("   holding it would hand the same work over twice")
daemon.note_task_sent(_tp, "given to an idle executor", mid_turn=False)
check("a task delivered between turns is not held",
      daemon.take_open_task(_tp), None)
print("   stop ends the run, so nothing is owed any more")
daemon.note_task_sent(_tp, "held", mid_turn=True)
daemon.clear_open_tasks(_tp)
check("stop empties the queue",
      daemon.take_open_task(_tp), None)
print("   the queue is a queue and not an archive - five is plenty")
for _i in range(9):
    daemon.note_task_sent(_tp, "piece %d" % _i, mid_turn=True)
check("it keeps the last five, not everything ever sent",
      len(daemon.STATE["tasks_open"][daemon.norm(_tp)]), 5)
check("and the oldest kept is the sixth of the nine",
      (daemon.take_open_task(_tp) or {}).get("text"), "piece 4")
print("   the done branch hands held work over BEFORE asking the planner")
print("   for something new - there is nothing to wait for, the fact is")
print("   known at the moment of the verdict, so clinch is only the backstop")
# Read from the FUNCTION, not from the module. It used to be the module,
# and the position of a string in a 12,000-line file is not the order two
# statements run in: moving the nudge into its own helper (which sits beside
# note_task_sent, hundreds of lines earlier) turned this red while the
# behaviour was untouched. What is being asserted is that the done branch
# takes held work BEFORE it considers asking for new work, so ask the branch.
_dv = inspect.getsource(daemon.run_review)
_a77 = "held = take_open_task(path)"
_b77 = "nudge_for_task"
check("done takes held work first", _a77 in _dv, True)
# index() ONLY when both are there. A flat script has no test runner behind
# it: a ValueError here does not fail one check, it kills the process and
# takes cases 78-105 with it, silently, because the output simply stops.
# That is the same shape as the unguarded read of a checked file, and it is
# why read_or_fail exists a few hundred lines up.
check("and only asks the planner when there is none",
      (_a77 in _dv and _b77 in _dv
       and _dv.index(_a77) < _dv.index(_b77)), True)
daemon.STATE["tasks_open"] = {}

print("\n78. stopping a window means it stopped, not that it was asked")
print("    stop() issued the kill and returned True on the strength of")
print("    having issued it, swallowing taskkill\'s exit code on the way.")
print("    On 2026-08-21 a pair was stopped at 05:02 and relaunched with")
print("    --resume onto its own session ids: stop() said True, the windows")
print("    were still alive, and the replacements sat trying to resume")
print("    conversations the old processes still held. Four and a half")
print("    hours dark. The SessionEnd of those sessions reached the journal")
print("    at 09:41:41, and the pair came up ten seconds later")
print("   (a) a process that really dies - the answer comes after the death,")
print("   not after the request")
import subprocess as _sp                                  # noqa: E402
from bridgecore import sessions                           # noqa: E402,F811
_proc = _sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
check("the process is running before we start",
      sessions.pid_alive(_proc.pid), True)
_t0 = time.time()
_said = sessions.stop(TMP, "executor", pid=_proc.pid)
_took = time.time() - _t0
check("stop says it stopped", _said, True)
check("and by then the process is actually gone",
      sessions.pid_alive(_proc.pid), False)
print("       (it took %.2fs, and the answer came after the death)" % _took)

print("   (b) a process that survives the kill. This is the case the old")
print("   code could not express at all: it had no way to say no")
_saved_alive = sessions.pid_alive
_saved_run = sessions.subprocess.run


class _Refused(object):
    returncode = 1
    stdout = b""
    stderr = b"ERROR: Access is denied."


sessions.pid_alive = lambda pid: True          # nothing can kill it
sessions.subprocess.run = lambda *a, **k: _Refused()
_t1 = time.time()
_answer = sessions.stop(TMP, "planner", pid=999999, wait=0.4)
_waited = time.time() - _t1
sessions.pid_alive = _saved_alive
sessions.subprocess.run = _saved_run
check("a process that will not die is reported as not stopped",
      _answer, False)
check("and it waited rather than answering at once",
      _waited >= 0.3, True)
print("   the shipped code could not fail this check - it returned True")
print("   without looking, which is why the case is worth having")
_ssrc = inspect.getsource(sessions.stop)
check("the answer is taken from the process, not from the request",
      "pid_alive(target)" in _ssrc, True)
check("and taskkill's exit code is no longer swallowed",
      "r.returncode" in _ssrc, True)
check("the wait has the relayout precedent behind it",
      "STOP_WAIT_SEC" in _ssrc, True)

print("   (c) nobody starts a replacement over a live window. Two processes")
print("   on one seat is what made that morning dark, and starting anyway")
print("   is not the smaller failure")
_rot = inspect.getsource(daemon.rotate_executor)
_hand = inspect.getsource(daemon.handover)
check("a rotation refuses when the old window would not die",
      "refuse_replacement(path, \"executor\", _pid, \"rotation\")" in _rot,
      True)
print("   and it refuses on the right question. stop() answers False for")
print("   'there was no pid to stop' as well, which is not a failure - it")
print("   is nothing in the way. Conflating them blocked every handover the")
print("   suite drives, where no real process exists at all")
print("   - and since 8.46 it asks by the one definition, record_alive: the")
print("   bare pid was answered by whatever process held the number next")
check("the rotation asks whether a KNOWN process is still alive",
      "_pid and record_alive(_rec)" in _rot, True)
check("a handover that refuses still answers in its own shape, a dict",
      '"ok": False, "error":' in _hand, True)
check("and returns before launching anything",
      _rot.index("refuse_replacement") < _rot.index("sessions.launch"), True)
# CHANGED DELIBERATELY 2026-09-04 (X1b), and one of these claims is
# REVERSED rather than moved. Three of them used to read handover()'s own
# text: that it asked `_pid and sessions.pid_alive(_pid)`, that it called
# refuse_replacement, and that it did so BEFORE sessions.launch - "it
# refuses the whole handover, not just the stuck half". All three were
# right while the old window was stopped first: a window that would not
# close then meant two live executors on one seat.
#
# The order is the other way round now. The replacement is launched and
# has reported for duty before anything is stopped, so refusing at this
# point would leave the new window AND the old one running with nobody
# told which is which - the very outcome the old claim existed to
# prevent. The refusal is kept (it still journals at warn and rings a
# person) and the swap completes. -> DECISIONS.md 8.7
_stop = inspect.getsource(daemon.stop_the_replaced)
# AND ONE MOVE FURTHER on 2026-09-26: the stop itself - background jobs,
# the refusal - is stop_window now, because finishing an orphaned swap
# stops the old window the same way and two copies of a stop is how they
# come to differ. stop_the_replaced calls it before it retires anything.
# find(), not index(): a missing name must fail this line, not kill the
# suite below it. -> DECISIONS.md 8.36
_sw = (inspect.getsource(daemon.stop_window)
       if hasattr(daemon, "stop_window") else "")
check("the handover's refusal moved with the stop, into stop_window, which "
      "stop_the_replaced calls",
      ('refuse_replacement(path, role, pid, "handover")' in _sw,
       "stop_window(path, role, pid)" in _stop), (True, True))
check("and it still asks the process, not the request",
      "sessions.pid_alive(pid)" in _sw, True)
check("but it is no longer an abort - the swap finishes after it",
      -1 < _stop.find("stop_window(") < _stop.find("retire_sessions"),
      True)
check("and handover() stops nothing itself any more",
      "sessions.stop(" in _hand, False)
# CORRECTED THE SAME DAY, and the first version of this line was wrong in
# a way worth keeping on the record: it asserted "launches first, records
# what to stop afterwards", which is exactly the order that let a fast
# replacement drop the handover record before stop_after was written - and
# then have it resurrected empty. The pids are known before the launch, so
# they are written before it, and what happens after is a PRUNE that never
# re-creates. -> DECISIONS.md 8.7, test_multipair case 76
check("it writes down what to stop BEFORE it launches anything",
      _hand.index('"stop_after"') < _hand.index("sessions.launch"), True)
check("and afterwards it prunes that record, never re-creates it",
      "hv is not None" in _hand, True)
print("   and the refusal is not silent - it names the pid that would not")
print("   die, at warn, and calls a person, because a window nobody can")
print("   close is not something the bridge can solve on its own")
_ref = inspect.getsource(daemon.refuse_replacement)
check("the refusal journals at warn", '"warn"' in _ref, True)
check("names the pid", "pid %s" in _ref, True)
check("and rings a human", 'notify("needs_you"' in _ref, True)

print("\n79. a window must not inherit somebody else's session")
print("    The client marks the environment of anything it spawns. A window")
print("    that inherits those marks is treated as a nested run, and answers")
print("    by not saving a transcript - and a window with no transcript is")
print("    one the bridge can read neither an rc link nor a context size")
print("    from. Blind for the whole of its life, with the wall accounting")
print("    quietly guessing.")
print("    How it got in on 2026-08-21: the daemon was restarted with")
print("    `relayout --now` typed INSIDE a Claude Code session, so it")
print("    inherited that session, and launch() copied os.environ wholesale")
print("    into every window it opened afterwards")
_dirty = dict(os.environ)
_dirty.update({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "someone-else",
               "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_PID": "4242",
               "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
               "CLAUDE_CODE_ENTRYPOINT": "cli"})
_clean = sessions.clean_env(_dirty)
for _m in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
           "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_TOKEN",
           "CLAUDE_CODE_ENTRYPOINT"):
    check("%s does not pass through" % _m, _m in _clean, False)
print("   the ONE that is still ours survives - stripping by prefix would")
print("   have taken it too. The compaction override is no longer among")
print("   them: it stopped being SET on 2026-09-01, and the same edit had")
print("   to make it start being STRIPPED, because a variable nobody sets")
print("   but everybody inherits still reaches every window - measured, in")
print("   a window a launch really opened, at 70 before and nothing after")
_dirty["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "80"
_dirty["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "200"
_clean2 = sessions.clean_env(_dirty)
check("an inherited compaction override does NOT pass through",
      "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in _clean2, False)
check("and the stop-hook cap, which IS ours, still does",
      _clean2.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"), "200")
check("while the rest of the environment is untouched",
      bool(_clean2.get("PATH")), True)
print("   launch() opens every window through it")
check("launch builds its environment clean",
      "env = clean_env()" in inspect.getsource(sessions.launch), True)
print("   and the daemon itself is spawned clean, or the next restart from")
print("   inside a session brings the whole thing straight back")
from bridgecore import relayout as _rl
check("start_daemon hands the daemon a cleaned environment",
      "env=clean" in inspect.getsource(_rl.start_daemon), True)
check("built from the same one list, not a second copy of it",
      "clean_env()" in inspect.getsource(_rl.start_daemon), True)

print("\n80. no claim about state without checking it as it is said")
print("    A turn died at 11:35:06 on 2026-08-21; the owner nudged the")
print("    window himself; and the message that went out at 11:38:36 said")
print("    \"the pair is idle, not working\" about a pair that was working.")
print("    True when it was decided, false when it was said - and 150")
print("    seconds is long enough for that to matter")
_mp = os.path.join(TMP, "moved_project")
_when = time.time() - 200
daemon.STATE["stop_seen"] = {}
daemon.STATE["last_task"] = {}
daemon.STATE["inflight"] = {}
check("a pair with no sign of life has not moved",
      daemon.pair_moved_since(_mp, "executor", _when), False)
print("   four independent witnesses, any one of which is enough")
daemon.STATE["stop_seen"] = {"%s|executor" % daemon.norm(_mp): time.time()}
check("a turn finished since then counts",
      daemon.pair_moved_since(_mp, "executor", _when), True)
daemon.STATE["stop_seen"] = {}
daemon.STATE["last_task"] = {daemon.norm(_mp): time.time()}
check("a task delivered since then counts",
      daemon.pair_moved_since(_mp, "executor", _when), True)
daemon.STATE["last_task"] = {}
daemon.STATE["inflight"] = {daemon.norm(_mp): {"x": {"cmd": "build"}}}
check("something running counts",
      daemon.pair_moved_since(_mp, "executor", _when), True)
daemon.STATE["inflight"] = {}
check("and with all of them cleared it is quiet again",
      daemon.pair_moved_since(_mp, "executor", _when), False)
print("   it errs towards SILENCE: a message wrongly withheld costs a line")
print("   in the journal, one wrongly sent tells a person a working pair is")
print("   dead. So anything it cannot read answers 'moved'")
_saved_best = daemon.best_session


def _boom(*a, **k):
    raise RuntimeError("cannot read")


daemon.best_session = _boom
print("   REVERSED on 2026-08-22: unreadable is no longer an alibi. It was")
print("   'cannot tell, say nothing', which is one more way an unnameable")
print("   witness silences a real death. The consequence changed first -")
print("   the answer to a dead turn is now revive_lost_turn handing the")
print("   work back, which is cheap and safe to do once too often, so")
print("   silence is the expensive mistake (case 97)")
check("an unreadable state is not an alibi",
      daemon.pair_moved_since(_mp, "executor", _when), False)
daemon.best_session = _saved_best
check("and a missing timestamp is not a claim either way",
      daemon.pair_moved_since(_mp, "executor", 0), False)
print("   the grace itself is untouched - it is there so a late report can")
print("   catch up, and it was never the thing that was wrong")
_cl = inspect.getsource(daemon.check_lost_turn)
check("the grace is still stopfail_grace",
      "stopfail_grace" in _cl, True)
check("and the check happens before the message, not instead of the grace",
      _cl.index("moved_witness") < _cl.index('notify("crash"'), True)
check("a pair that came back is dropped from the book, not just skipped",
      'STATE["stopfail"].pop(key, None)' in _cl, True)

print("\n81. compaction fires between turns, so a turn has to fit")
print("    An executor died on 2026-08-21 with its own compaction")
print("    request too big to send: 1,000,274 tokens against a")
print("    1,000,000 window. At 80% the threshold was 800k, which")
print("    leaves 200,000 of headroom - and the turn wanted 200,274.")
print("    It missed by 274 tokens")
check("the bridge keeps no compaction threshold of its own",
      "autocompact_pct" in store.PROJECT_DEFAULTS, False)
print("   70% left 300k - half as much again as the largest single turn")
print("   ever seen here. That arithmetic is still what anyone who sets a")
print("   threshold has to do; it is simply no longer ours to set")
_win = 1000000
_thr = min(int(_win * 70 / 100), _win - 13000)
check("on a 1M window that is a 700k threshold", _thr, 700000)
check("leaving 300k of headroom for one turn", _win - _thr, 300000)
check("which is more than the turn that died needed",
      (_win - _thr) > 200274, True)
print("   and launch() hands an override ONLY where it can work. Both")
print("   halves or neither: the percentage and autoCompactWindow multiply,")
print("   and the pair that never had the companion setting got the same 70")
print("   and compacted at the ceiling regardless - so sending one to a")
print("   project with no key would not merely do nothing, it would record")
print("   a threshold the window is not running under (8.2)")
_ls = inspect.getsource(sessions.launch)
check("launch sets the override", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in _ls,
      True)
# the second half may come from the bridge since 2026-10-10: the window
# launch() itself hands this window (8.61) - still both halves or neither
check("through the both-halves test and not straight from its argument",
      "compact_pct_for(project, compact_pct, window)" in _ls, True)
_nokey = os.path.join(TMP, "pct-nokey")
os.makedirs(os.path.join(_nokey, ".claude"), exist_ok=True)
with io.open(os.path.join(_nokey, ".claude", "settings.json"), "w",
             encoding="utf-8") as _fh:
    _json.dump({"hooks": {}}, _fh)
check("a project with no autoCompactWindow is given nothing",
      sessions.compact_pct_for(_nokey, 90), None)
check("...unless the bridge gives the window itself - the other half (8.61)",
      sessions.compact_pct_for(_nokey, 90, 222000), 90)
with io.open(os.path.join(_nokey, ".claude", "settings.json"), "w",
             encoding="utf-8") as _fh:
    _json.dump({"hooks": {}, "autoCompactWindow": 1000000}, _fh)
check("with the key, the project's percentage is what goes",
      sessions.compact_pct_for(_nokey, 90), 90)
check("and a project that asked for none still gets none",
      sessions.compact_pct_for(_nokey, None), None)
check("a nonsense percentage is refused rather than sent",
      [sessions.compact_pct_for(_nokey, v) for v in (0, 101, -5, "x")],
      [None, None, None, None])
print("   with a control, because a negative on source text is also green")
print("   when the source read is not the source you think it is")
check("...and launch still sets what it always set", 
      'env["BRIDGE_ROLE"] = role' in _ls, True)
check("and passes that environment to the process", "env=env" in _ls, True)
print("   the detector no longer accuses the setting of going missing. It")
print("   said so five times between 2026-07-30 and 2026-08-17 and was")
print("   wrong every time: `at` is where the TURN ENDED, not where")
print("   compaction fired, and a turn that starts under the threshold and")
print("   grows past it ends far above it with the setting working")
_ds = inspect.getsource(daemon)
check("it does not claim the setting is not reaching the session",
      "The setting is not reaching" in _ds, False)
check("it reports the overshoot instead",
      "one turn crossed the threshold and kept" in _ds, True)
check("and at log level, not as an alarm",
      '"The %s compacts at %d%% and this turn ran "' in _ds, True)
print("   auto-compaction is written down rather than assumed. The client")
print("   defaults it ON, so this changes nothing today - it is written so")
print("   that flipping it off becomes visible instead of silently making")
print("   the threshold meaningless")
from bridgecore import install as _inst2
_acp = os.path.join(TMP, "autocompact_project")
os.makedirs(os.path.join(_acp, ".claude"), exist_ok=True)
_io.open(os.path.join(_acp, ".claude", "settings.json"), "w",
         encoding="utf-8").write(u'{"env": {"KEEP": "me"}}')
check("the key is written", _inst2.keep_autocompact_on(_acp), True)
_after = _js.load(_io.open(os.path.join(_acp, ".claude", "settings.json"),
                           encoding="utf-8"))
check("and it is true", _after.get("autoCompactEnabled"), True)
check("while whatever was already there is untouched",
      (_after.get("env") or {}).get("KEEP"), "me")
check("writing it twice is a no-op, not a rewrite",
      _inst2.keep_autocompact_on(_acp), False)

print("\n82. the frozen consoles WERE QuickEdit, and the reading said not")
print("    The owner reported both windows hanging, freed by Esc, and")
print("    nothing moving in either window - not even a spinner. QuickEdit")
print("    fits that shape exactly: a click puts a console into selection")
print("    mode and the writing process blocks. It WAS measured rather than")
print("    assumed - AttachConsole to a live window, open CONIN$, read the")
print("    input mode - and the measurement came back mode=0x0208, with")
print("    ENABLE_QUICK_EDIT_MODE (0x40) clear. This case then pinned")
print("    'hypothesis excluded, no fix was built'.")
print("   THAT WAS WRONG, AND THIS CASE WAS WRONG WITH IT. 2026-09-05:")
print("   0x0208 also has ENABLE_EXTENDED_FLAGS (0x80) CLEAR, and with THAT")
print("   bit clear the mode word carries no QuickEdit or Insert bit at all")
print("   - they come from the console's defaults, and this machine's")
print("   HKCU/Console/QuickEdit is 1. The reading could not see the flag it")
print("   was used to rule out. Meanwhile the owner's windows went on")
print("   freezing, and the frozen one's TITLE read the host's own word for")
print("   a selection: 78 s in which that window's title never changed while")
print("   two controls changed five times each. -> DECISIONS.md 8.14")
print("   So the assertion is INVERTED rather than deleted: the bootstrap")
print("   exists now, and a check that pins its absence would be this same")
print("   mistake written down a second time.")
_ss = inspect.getsource(sessions)
check("the launch path DOES clear QuickEdit now",
      "ENABLE_QUICK_EDIT" in _ss or "QUICK_EDIT" in _ss, True)
check("and it sets EXTENDED in the same write, which is the whole defect",
      "EXTENDED" in _ss, True)
check("the arithmetic, so this cannot rot: on the mode that was measured, "
      "clearing 0x40 alone is a no-op", 0x0208 & ~0x0040, 0x0208)
check("and setting 0x80 with it is not", (0x0208 | 0x0080) & ~0x0040, 0x0288)
check("and the launch command is still claude, not a wrapper",
      inspect.getsource(sessions.build_command).count('cmd = ["claude"]'), 1)
print("   what remains in suspicion is ours and already written down: a")
print("   blocked Stop hook draws nothing. Report 122 went to the planner")
print("   at 11:11:10 and the executor was idle at 11:12:21 with a 13-minute")
print("   gap after it - a window held by its own Stop hook looks exactly")
print("   like a dead one, and Esc is what returns the prompt")
check("run_review still holds the hook while a report waits",
      "review_timeout" in inspect.getsource(daemon.run_review), True)
print("   and the panel stops keeping its own copy of a default. It said")
print("   ||80 and went on saying 80 after the real default became 70 -")
print("   one number in two places, and the copy was the one on screen")
_panel2 = _io.open(os.path.join(os.path.dirname(daemon.__file__),
                                "panel.html"), encoding="utf-8").read()
check("the hard-coded fallback is gone", "autocompact_pct||80" in _panel2,
      False)
check("and so is the control itself, now that nothing is behind it",
      "acPct" in _panel2, False)
_dsrc2 = inspect.getsource(daemon)
check("and nothing in the daemon reads a compaction default any more",
      'PROJECT_DEFAULTS.get("autocompact_pct")' in _dsrc2, False)
check("...with the control that both sources were really read",
      len(_panel2) > 10000 and len(_dsrc2) > 10000, True)

print("\n83. a channel a subagent started may not take the window's seat")
print("    PROJECT is os.getcwd() and ROLE is BRIDGE_ROLE, and anything a")
print("    window spawns inherits both - so a channel started deeper inside")
print("    the window registers under the very same (project, role) key.")
print("    It is always YOUNGER than the window's own channel, and the age")
print("    rule hands the record to the younger one")
# REAL processes, because parentage is the whole question. This case used
# invented pids - 1000, 2000, 2500 - and passed while the code asked only
# "is your parent the recorded window". It cannot: since 2026-08-28 the
# question is "are you INSIDE the recorded window", and numbers that name
# no process answer it with "cannot tell", which correctly falls through
# to the age rule. A fixture that hands the code the pids it wants to hear
# proves nothing about the pids it will actually get.
_seat_tree = os.path.join(TMP, "seat_tree.py")
with open(_seat_tree, "w", encoding="utf-8") as _fh:
    _fh.write(
        "import os, subprocess, sys, time" + chr(10) +
        "d = int(sys.argv[1])" + chr(10) +
        "if d > 0:" + chr(10) +
        "    p = subprocess.Popen([sys.executable, __file__, str(d - 1)],"
        + chr(10) +
        "                         stdout=subprocess.PIPE, text=True)"
        + chr(10) +
        "    print('%d %s' % (os.getpid(), p.stdout.readline().strip()),"
        + chr(10) +
        "          flush=True)" + chr(10) +
        "else:" + chr(10) +
        "    print(os.getpid(), flush=True)" + chr(10) +
        "time.sleep(120)" + chr(10))
_seat_procs = []
_seat_pids = []


def _seat_chain(depth):
    p = subprocess.Popen([sys.executable, _seat_tree, str(depth)],
                         stdout=subprocess.PIPE, text=True)
    _seat_procs.append(p)
    pids = [int(x) for x in p.stdout.readline().split()]
    # Every pid, not just the head. p.kill() reaches the process subprocess
    # started and no descendant of it, and this fixture is a chain by
    # design - so the cleanup below used to leave two of every three alive
    # until their own sleep(120) ended. Same defect as test_multipair's
    # _tree, found the same day and fixed in both.
    _seat_pids.extend(pids)
    return pids


_win, _wchan, _sub = _seat_chain(2)
_p = os.path.join(TMP, "seat_project")
_k = daemon.norm(_p)
daemon.STATE["pids"] = {"%s|planner" % _k: {"pid": _win}}
_window_chan = {"pid": _wchan, "ppid": _win}        # the window's own
print("   the window's own channel always keeps its seat")
check("the window's channel may register",
      daemon.channel_supersedes(None, _wchan, _win, _p, "planner"), True)
check("and may re-register over itself",
      daemon.channel_supersedes(_window_chan, _wchan, _win, _p, "planner"),
      True)
print("   a stranger under the same key is refused, however young it is -")
print("   and this is the case the age rule got exactly backwards")
check("a subagent's channel may not take it",
      daemon.channel_supersedes(_window_chan, _sub, _wchan, _p, "planner"),
      False)
print("   AND SO IS A SECOND LIVE WINDOW, which is the hole this leaves.")
print("   2026-08-28: the app forked the planner conversation into a new")
print("   local window, the bridge adopted it but kept the old window pid,")
print("   and this refused the newcomer 232 times over two hours while")
print("   three reports went to a window nobody was reading. The test is")
print("   the same in both cases and cannot tell them apart - written down")
print("   here rather than left for the next reader to rediscover")
_win2, _wchan2 = _seat_chain(1)
check("a live sibling window is refused too - correct for a subagent,",
      daemon.channel_supersedes(_window_chan, _wchan2, _win2, _p,
                                "planner"), False)
print("   wrong for a window, and the rule sees one thing. What says so")
print("   out loud is note_channel_refused, after five refusals")
print("   the theft would have been INVISIBLE: a win is silent, only a")
print("   refusal is journalled, and afterwards the window's own channel is")
print("   refused for ever - it is the older contender - so every report")
print("   goes to a process inside a subagent and the planner sees none")
print("   it fails open in both directions, or it would refuse real windows")
check("an unknown parent falls through to the age rule",
      daemon.channel_supersedes(_window_chan, 3000, None, _p, "planner")
      in (True, False), True)
check("a window pid we never recorded falls through too",
      daemon.channel_supersedes(_window_chan, 3000, 2500,
                                os.path.join(TMP, "unknown_project"),
                                "planner") in (True, False), True)
daemon.STATE["pids"] = {}
check("with no pids on record at all it behaves exactly as before",
      daemon.channel_supersedes(None, 3000, 2500, _p, "planner"), True)
print("   and the parent has to survive on the record, or the next")
print("   comparison has nothing to compare against")
_dsrc3 = inspect.getsource(daemon)
check("the registration stores the parent",
      '"ppid": body.get("ppid")' in _dsrc3, True)
check("and channel.py sends it",
      '"ppid": os.getppid()' in _io.open(
          os.path.join(os.path.dirname(daemon.__file__), "channel.py"),
          encoding="utf-8").read(), True)
from bridgecore import sessions                    # noqa: E402
_seat_left = []
for _pid in _seat_pids:
    # A WAIT on an owned handle, not a kill followed by hoping. os.kill on
    # Windows is TerminateProcess and returns before the process is reaped,
    # and only the chain HEADS are Popen objects - the descendants had
    # nobody to wait on them, so the check below raced. It was green on this
    # machine and red on the planner's, which shares it with a live daemon
    # and two pairs. A bounded poll was tried first and is not the fix: a
    # margin guesses how long dying takes (S5.38), and the next number would
    # be the same guess, larger.
    try:
        if not sessions.terminate_and_wait(_pid, 30):
            _seat_left.append(_pid)
    except Exception:
        _seat_left.append(_pid)
for _sp in _seat_procs:
    try:
        _sp.wait(5)
    except Exception:
        pass


def _alive3763(pid):
    """sessions.pid_alive: the project's own probe, correct on Windows.

    Not `tasklist` (subprocess.run answered stdout=None inside this suite)
    and above all not `os.kill(pid, 0)`, which on Windows is not a probe at
    all - it calls TerminateProcess and kills what it was asked about.
    """
    from bridgecore import sessions as _sess3763
    return _sess3763.pid_alive(pid)


print("   and the fixture is cleaned up COMPLETELY - every process in the")
print("   chain. p.kill() reached the head alone, so two of every three")
print("   outlived the run; found 2026-08-31 with several runs' worth alive")
print("   THE CONTROL (rule 19): the probe must still be able to SEE a live")
print("   process here, or an empty list below means nothing at all")
_ctl3763 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
check("a process nobody killed reads as alive", _alive3763(_ctl3763.pid), True)
check("terminate_and_wait says it is gone",
      sessions.terminate_and_wait(_ctl3763.pid, 30), True)
check("and the probe agrees, with no sleep in between",
      _alive3763(_ctl3763.pid), False)
# Never let the control CRASH the suite. When the sabotage above is real -
# terminate_and_wait lying, which is exactly what this control exists to
# catch - the process is still running, wait(5) raises TimeoutExpired, and
# the run dies before it prints its FAIL summary. That is S5.9: a suite that
# crashes instead of reporting is not a gate. It is also rule 9: the control
# must not be the thing that leaks.
try:
    _ctl3763.wait(5)
except Exception:
    try:
        _ctl3763.kill()
        _ctl3763.wait(5)
    except Exception:
        pass
check("the killer reports nothing left behind", _seat_left, [])
check("the seat fixture leaves no process running",
      [p for p in _seat_pids if _alive3763(p)], [])
print("   measured 2026-08-21: all six live channels were direct children")
print("   of exactly the window pid the bridge recorded at launch, so the")
print("   test is sound on real data - and there was not one refusal in the")
print("   journal that day, so this is a latent defect closed, not an")
print("   outage explained")

print("\n84. rule 29: a run that opens a window runs quiet")
print("    The owner: he must not see flashing windows or lose the keyboard")
print("    to a test while he is working, in any project. The canon is read")
print("    from disk on EVERY delivery, so a rule added here reaches every")
print("    pair on their next task or report - no restart, no message")
_here = os.path.dirname(os.path.dirname(os.path.abspath(daemon.__file__)))
_ru = _io.open(os.path.join(_here, "HONESTY.md"), encoding="utf-8").read()
# HONESTY.en.md is a source for the PUBLIC build, not a package file, so it
# is present in the repository and absent from an unpacked package. The
# English wording is therefore asserted only where the file exists - and its
# absence from the REPOSITORY is itself a failure, checked just below, so
# this cannot quietly become a check that never runs.
_en_path = os.path.join(_here, "HONESTY.en.md")
_en_here = os.path.isfile(_en_path)
_en = _io.open(_en_path, encoding="utf-8").read() if _en_here else ""
check("the English canon is in the repository beside the Russian one",
      _en_here or not os.path.isfile(os.path.join(_here, "make_public.py")),
      True)
check("rule 29 is in the canon", "29. **" in _ru, True)
check("and in the English canon", not _en_here or "29. **" in _en, True)
print("   the Russian canon is checked STRUCTURALLY here and the English one")
print("   for its wording, on purpose: this file is published, the public")
print("   repository is English-only, and check_public.py enforces that.")
print("   Spelling the Russian phrases out in escapes would pass the scan")
print("   while meaning exactly what the scan exists to stop")
import re as _re29
# 34 -> 35 on 2026-09-04, deliberately: rule 35 is "no claim about state
# without a witness opened in this same turn", and its gate is claim_gate
# on the planner's Stop hook (-> DECISIONS.md 8.8). These four are a COPY
# of a number and are meant to be: a rule added quietly is a rule the
# English twin, the header and the titles tier can drift away from, and
# that is what they catch. They caught this one.
# 35 -> 36 on 2026-10-11, deliberately: rule 36 is the owner's 30 000
# characters for CLAUDE.md and every file it links, and its gate is
# check_rent (-> DECISIONS.md 8.72).
check("the Russian canon now carries 36 numbered rules",
      len(_re29.findall(r"^\d+\. \*\*", _ru, _re29.M)), 36)
check("and every one of them still carries its check",
      len(_re29.findall(r"^\s+\*[^*]+:\*", _ru, _re29.M)), 36)
# ONE check marker per rule, not "at least one". Rule 35's first draft
# carried two - its check and its gate on separate italic lines - which
# reads as a 36th check for a 35th rule and would have made this counter
# meaningless from then on. The gate is named inside the check line.
check("the English canon carries 36 too",
      not _en_here
      or len(_re29.findall(r"^\d+\. \*\*", _en, _re29.M)) == 36, True)
check("the English header counts them",
      not _en_here or "Thirty-six rules." in _en, True)
# The English canon IS the public HONESTY.md, so the cap of "short enough to
# put in front of every task" binds it too - but that case reads the Russian
# here, and the English one was measured only by a public run: rule 36 took
# it to 15 292 while eleven of eleven stayed green (-> DECISIONS.md 8.80).
check("and the English canon is as short - it is the public HONESTY.md",
      not _en_here or len(_en.strip()) < 15000, True)
# AND THE TITLES TIER CARRIES IT, which is the half a count cannot see.
# honesty_titles() matches the number and the bold title ON ONE LINE, and
# rule 35's first draft wrapped its title onto the second - so the rule was
# in the full text and absent from the titles, which is every delivery
# after a session's first. That is S5.39's class exactly: a rule that never
# reaches a running session.
check("and every rule reaches the titles tier, not just the full text",
      len(_re29.findall(r"^\d+\. ", daemon.honesty_titles(), _re29.M)), 36)
print("   the English rule names the mechanism, not just the goal - in the")
print("   file it links: since 2026-10-12 the canon keeps rule 29's norm, its")
print("   check and the link, and the mechanism is QUIET.md's word for word")
print("   (QUIET.en.md here, published as QUIET.md; DECISIONS 8.82)")
_qen29 = os.path.join(_here, "QUIET.en.md")
_qe = " ".join(_io.open(_qen29, encoding="utf-8").read().split()) \
    if _en_here and os.path.isfile(_qen29) else ""
check("born minimised by the operating system",
      not _en_here or ("BORN minimised" in _qe
                       and "operating system" in _qe), True)
check("with no right to take the keyboard",
      not _en_here or "no right to take the keyboard" in _qe, True)
check("drawing forced while it stays minimised",
      not _en_here or "drawing forced" in _qe, True)
check("the default in the wrapper, never in project settings",
      not _en_here or "never in the project's settings" in _qe, True)
check("and coordinates refused outright",
      not _en_here or "Coordinates are not a mechanism" in _qe, True)
print("   and it is honest that there is NO mechanical gate - the bridge")
print("   cannot see anybody's screens, so the last word is the person's.")
print("   Inventing a pseudo-gate would be a check that cannot fail")
check("the English rule says the gate does not exist",
      not _en_here or "no mechanical gate" in _en, True)
check("and hands the last word to the person",
      not _en_here or "do not call the mode quiet until they" in _en, True)
print("   the full text is a separate file, so the canon pays four lines")
print("   and not an essay - QUIET.md is never delivered to anybody")
_q = os.path.join(_here, "QUIET.md")
check("QUIET.md exists", os.path.isfile(_q), True)
_qt = read_or_fail(_q, "QUIET.md")
print("   and the reading itself is guarded, which is not decoration: this")
print("   exact line used to be an unguarded open() three characters after")
print("   a check that had ALREADY marked FAIL, so one missing file killed")
print("   the script and took every block below it - silently, because the")
print("   output just stops. Measured on a copy with QUIET.md and")
print("   verify_package.py removed: 49 blocks then a traceback, against")
print("   103 blocks and four honest FAIL lines with this in place")
check("a file that is not there reads as empty, not as an exception",
      read_or_fail(os.path.join(TMP, "no-such-file-at-all.md"), "a probe"), "")
import re as _re29b
check("it carries principles, traps and a checklist - four sections",
      len(_re29b.findall(r"^## \d+\.", _qt, _re29b.M)), 4)
check("both canons point at it",
      "QUIET.md" in _ru and (not _en_here or "QUIET.md" in _en), True)
print("   minimised by an order from the OS, not minimised later by code")
_ls = inspect.getsource(sessions.launch)
check("the window is born minimised and unfocused",
      "SW_SHOWMINNOACTIVE" in _ls, True)
check("by STARTUPINFO at creation, not by a later call",
      "STARTF_USESHOWWINDOW" in _ls, True)

print("\n85. several pairs quiet at once, and picking the work back up")
print("    The owner: if the connection drops, probe once a minute, and if")
print("    there IS one, carry on. Read exactly - not \"if it comes back\".")
print("    On 2026-08-21 three pairs said nothing for forty minutes and")
print("    revived only by themselves")
print("   the obvious detector was measured FIRST and thrown away: two or")
print("   more pairs whose turns died network-shaped fires ZERO times in")
print("   this bridge's whole journal, including through that outage, which")
print("   produced no turn deaths at all. Telegram is no better - about")
print("   twenty drops a day while the sessions were fine")
print("   what DID fire: two or more pairs unanswered in one bucket, five")
print("   times in a month, two of them the outage itself")
_a = os.path.join(TMP, "quiet_a")
_b = os.path.join(TMP, "quiet_b")
daemon.STATE["quiet_pairs"] = {}
check("nothing quiet is not an outage", daemon.outage_suspected()[0], False)
daemon.note_unanswered_pair(_a)
check("ONE pair quiet is ordinary - a planner thinking, or running a check",
      daemon.outage_suspected()[0], False)
daemon.note_unanswered_pair(_b)
check("two at once is not about either of them",
      daemon.outage_suspected()[0], True)
print("   and it forgets: an outage is a window, not a life sentence")
daemon.STATE["quiet_pairs"] = {daemon.norm(_a): time.time() - 5000,
                               daemon.norm(_b): time.time() - 5000}
check("two pairs quiet an hour ago is not an outage now",
      daemon.outage_suspected()[0], False)
check("the window and the count are both configurable",
      ("outage_window" in inspect.getsource(daemon.outage_suspected)
       and "outage_pairs" in inspect.getsource(daemon.outage_suspected)),
      True)
print("   the probe is the ONE outbound exception in this project, and it")
print("   is narrow: only while several pairs are quiet, a HEAD, seconds of")
print("   timeout, and switchable off entirely")
_saved_probe = daemon.CFG.get("outage_probe")
daemon.CFG["outage_probe"] = ""
check("switched off, it is not asked at all",
      daemon.connection_is_there(), None)
daemon.CFG["outage_probe"] = _saved_probe
_cs = inspect.getsource(daemon.connection_is_there)
check("it is a HEAD, not a fetch", '"HEAD"' in _cs, True)
# NOT an escaped copy of the Russian: writing the quote in escapes
# would slip past check_public while meaning exactly what that gate
# exists to stop. What the shipped comment has to carry is the
# JUSTIFICATION - that this outbound exists because it was asked
# for, and where the words themselves are kept.
check("and it says whose decision the exception is",
      ("owner asked for it" in _cs and "decision record" in _cs),
      True)
_ow = inspect.getsource(daemon.outage_watch)
check("it is only asked while several pairs are quiet",
      _ow.index("outage_suspected") < _ow.index("connection_is_there"), True)
check("and the pass runs once per outage, not every minute",
      'rec.get("done")' in _ow, True)
print("   the resume pass uses only machinery that already exists, and")
print("   leaves alone every kind of quiet that is somebody's decision")
_rs = inspect.getsource(daemon.resume_after_outage)
check("a pair a person paused is left alone", 'sit.get("paused")' in _rs, True)
check("a pair whose loop is off is left alone",
      'not sit.get("loop")' in _rs, True)
check("a pair the damper is holding is left alone",
      "idle_holding" in _rs, True)
check("it hands back work the bridge already holds",
      "take_open_task(path)" in _rs, True)
check("it tries an undelivered report again",
      "deliver_ex(path" in _rs, True)
check("and it invents no new way to wake anybody",
      "state_report(path" in _rs, True)
print("   the silence counters are NOT reset by this: only a live verdict")
print("   clears them, or a pair could be un-held with nobody having read")
print("   a word of what it wrote")
check("resuming does not clear silence",
      "clear_silence" in _rs, False)
print("   and the whole pass is one summary line, not a burst of messages")
check("one journal line for the pass",
      _ow.count("store.journal") , 1)


print("\n86. one manual /compact may not own the compaction point for ever")
print("    Until 2026-08-21 the stored point was min(previous, this): a")
print("    ratchet that only fell. One entry here held 776,393 from a")
print("    manual compaction on 2026-07-30 while its nine later samples all")
print("    sat between 996,305 and 999,920 - and nothing could lift it")
_poisoned = [776393, 998975, 998619, 999875, 999887,
             999920, 999595, 999648, 999729, 996305]
check("the old ratchet answered with the outlier", min(_poisoned), 776393)
# The width of one turn, stated by this case rather than imported. It was
# the module literal LARGEST_TURN_SEEN = 200274 until 2026-08-31, when that
# turned out never to have been the largest turn seen (826 turns measured:
# 532 910) and became a per-pair measurement with a fallback and a source.
# A case that pins arithmetic states its own inputs; it does not borrow a
# production figure that is now allowed to move.
_TURN_W = 200000
check("the samples are read instead, and the outlier is dropped",
      daemon.compaction_point(_poisoned, _TURN_W), 996305)
print("   'dropped' means: further below the newest sample than one turn of")
print("   this pair, so it cannot be an overshoot of the same threshold")
check("999920 - 776393 is further than one turn",
      999920 - 776393 > _TURN_W, True)
print("   and the width is no longer a literal: it is measured for the pair,")
print("   and it says whether it was measured or borrowed")
check("a pair that has measured nothing says so",
      daemon.turn_widest(os.path.join(TMP, "no-turns-here"),
                         "executor")[1] in ("fallback", "assumed"), True)
check("one sample is still just that sample",
      daemon.compaction_point([150000], _TURN_W), 150000)
check("samples that agree still give the minimum",
      daemon.compaction_point([700100, 712000, 705000], _TURN_W), 700100)
check("nothing measured is still nothing",
      daemon.compaction_point([], _TURN_W), None)
check("and with no width the claim is not made - the plain minimum",
      daemon.compaction_point(_poisoned, None), 776393)
print("   and a file written before today is repaired at startup rather")
print("   than waiting for the pair to compact again")
_mg = inspect.getsource(daemon.migrate_compaction_points)
check("the migration recomputes from the samples",
      "compaction_point(samples," in _mg, True)
check("with the width measured for that project, not a constant",
      'turn_widest(_cal_path, "executor")' in _mg, True)
check("an entry with no samples is left alone",
      "if not samples:" in _mg, True)
check("and it runs from main", "migrate_compaction_points()"
      in inspect.getsource(daemon.main), True)

print("\n87. past the wall, 'it compacts at the end of the turn' is a lie")
print("    A pair here died twice on 2026-08-21 - 11:05:18 and 20:50:36 -")
print("    both with invalid_request, both four to fifteen seconds after")
print("    PreCompact. The bridge believed a compaction was due at 776k,")
print("    the session passed it and kept going, and plan_for answered")
print("    'compacting' at every turn boundary from 776k to 996,305")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {},
    "compactions": {}, "last_session": {},
    "pids": {"%s|executor" % daemon.norm(PATH):
             {"pid": 1, "at": time.time(), "registered": True,
              "autocompact": 70, "model_req": "opus"}}})
cal = store.load_calibration()
cal[store.calib_key("opus 5", PATH)] = {
    "ceiling_pct": 97.0, "buffer_tokens": 33000, "misses": 14,
    "clean_streak": 0, "multiplier": 3.0, "wall_history_tokens": None,
    "compact_at_tokens": 776393, "compact_at_window": 1000000,
    "how": "the poisoned value, as it stood that evening"}
store.save_calibration(cal)


def _sj(used):
    s = {"role": "executor", "path": daemon.norm(PATH), "session_id": "sj-1",
         "model": "Opus 5", "window": 1000000, "window_observed": True,
         "context_tokens": used, "turn_costs": [33877, 15151, 39330, 32199]}
    daemon.STATE["sessions"]["executor:sj-1"] = s
    return s


# The exception below is worth ONE TURN, and since 2026-08-31 that is one
# turn of this pair rather than a module constant. The sizes in this case
# were built around 200 274 - the literal that used to be there - so the
# pair is given a turn of that size and the arithmetic is stated instead of
# imported. What the case is about is the SHAPE of the rule: past the wall
# and further than one turn past the point, or neither.
daemon.note_turn_cost(PATH, "executor", 200274, "sj-1")
check("the exception is worth one turn, measured for this pair",
      daemon.turn_widest(PATH, "executor"), (200274, "measured"))
_w = daemon.wall_view(_sj(850000), PATH)
check("the wall is the window minus the compaction reserve",
      _w["wall"], 1000000 - daemon.RESERVED_TOKENS)
print("   at 850k it is past the believed point but still below the wall,")
print("   and nothing changes: this is the routine answer and stays one")
check("850k is still routine", daemon.plan_for(_sj(850000), PATH)["do"],
      "compacting")
check("and so is 900k", daemon.plan_for(_sj(900000), PATH)["do"],
      "compacting")
check("and 966k, one token below the wall",
      daemon.plan_for(_sj(966999), PATH)["do"], "compacting")
print("   past the wall the answer changes - but only once the compaction")
print("   point is REFUTED as well, which is being further past it than any")
print("   one turn could carry a session. Both, or neither")
check("past the wall but only 190k past the point is still routine",
      daemon.plan_for(_sj(967000), PATH)["do"], "compacting")
_pl = daemon.plan_for(_sj(976668), PATH)
check("one token past both, and it is a handover", _pl["do"], "handover")
check("and it says why, with the wall",
      "past the 967k wall" in _pl["why"], True)
check("and with the point that never fired", "776k" in _pl["why"], True)
check("where it actually died is a handover too",
      daemon.plan_for(_sj(996305), PATH)["do"], "handover")
print("   and the other half of the exception, which the first half needs:")
print("   repairing that calibration puts the point at 996k - honest, and")
print("   ABOVE the 967k wall. 'Refuted' alone would then never fire again,")
print("   because nothing gets one turn past 996k and lives")
cal[store.calib_key("opus 5", PATH)]["compact_at_tokens"] = 996305
store.save_calibration(cal)
_w2 = daemon.wall_view(_sj(970000), PATH)
# Guarded, and the guard was earned: rule 1r can blank this point (it is
# `compact_refuted` then), and written as a bare `>` the comparison raised
# TypeError against None and took the 40-odd blocks below it in silence -
# no FAIL line, no summary, output simply stopping. That is the class the
# read_or_fail note in CLAUDE.md is about, in its comparison form. The
# answer to a blanked point here is False, which fails on its own line and
# names itself.
check("the repaired point is above the wall",
      bool(_w2["compact"]) and _w2["compact"] > _w2["wall"], True)
check("and it was not refuted - a point above the wall is the pair's real "
      "problem, not a stale number", _w2.get("compact_refuted"), None)
print("   since 2026-08-22 rule 1a catches this one EARLIER and calmly - a")
print("   point that leaves less room than a compaction needs means this")
print("   session can never summarise itself, so it is replaced two of its")
print("   own turns before it finds that out, not at the wall (case 95)")
_e960 = daemon.plan_for(_sj(960000), PATH)
check("960k is already a planned replacement", _e960["do"], "handover")
check("and it says why, in the calm words not the emergency ones",
      "while there is still room to do it calmly" in _e960["why"], True)
print("   1b is still there underneath, and is still what answers when no")
print("   turn cost has been measured yet - then 1a cannot fire at all")
_noturns = dict(_sj(967000)); _noturns["turn_costs"] = []
daemon.STATE["sessions"]["executor:sj-1"] = _noturns
_ab = daemon.plan_for(_noturns, PATH)
check("at the wall it is still a handover", _ab["do"], "handover")
check("and it names the reason, which is a different one",
      "itself past the wall" in _ab["why"], True)
print("   so the session is replaced at the boundary BEFORE the compaction")
print("   that would have killed it - which is the whole point")
check("and at the size it died at, still a handover",
      daemon.plan_for(_sj(996305), PATH)["do"], "handover")
cal[store.calib_key("opus 5", PATH)]["compact_at_tokens"] = 776393
store.save_calibration(cal)
print("   case 22 is the reason both are needed: a planner one ordinary")
print("   turn past a point it really does compact at is not in trouble")
check("168k of a 200k window, 18k past a real point, stays routine",
      daemon.plan_for({"role": "planner", "path": daemon.norm(PATH),
                       "session_id": "c22", "model": "Fable 5",
                       "window": 200000, "window_observed": True,
                       "context_tokens": 168000,
                       "turn_costs": [4000, 5000]}, PATH)["do"] != "handover",
      True)
print("   the turn before the fatal one ended at 20:40:06, ten minutes")
print("   before the compaction that killed the session - a handover")
print("   decided at that boundary had time to run")
print("   this is not the 'distance to an unmeasured wall' rule 1 refuses:")
print("   it fires on being PAST the line, never on approaching it")
_ps = inspect.getsource(daemon.plan_for)
print("   1b's OWN test has no margin - it fires on being past the line,")
print("   never on approaching it. Rule 1a above it is the one allowed a")
print("   margin, because its whole job is to act early and calmly")
check("1b tests position, not distance",
      'used >= wall and wv.get(' + chr(34) + 'compact_measured'
      + chr(34) + ')' in _ps, True)
print("   the guard beside it is about PROVENANCE, not slack: since")
print("   2026-08-28 'no compaction is coming' may only be said from a")
print("   point somebody SAW. With none on record wall_view falls back")
print("   to the percentage - and 5.29 measured that the percentage does")
print("   not move the point in this client, so it is a number already")
print("   known to be wrong. A pair moved drive, the calibration")
print("   stayed under the old key, and 13 handovers followed")
_1b = [l.strip() for l in _ps.splitlines()
       if l.strip().startswith('if compact and wall and used >= wall')]
check("1b's condition is exactly one line, and this is it",
      len(_1b), 1)
check("and it is a provenance test, not a margin",
      any(m in (_1b[0] if _1b else "")
          for m in ('+', 'margin', 'RESERVED', '0.9')), False)
check("and the exception beside it is about the point, not a turn count",
      "compact < wall" in _ps and "used - compact <= _wide" in _ps, True)
print("   and since 2026-08-22 the line it reads is MEASURED, not the")
print("   window minus an unmeasured reserve: 33 compactions succeeded")
print("   above that reserve, so it was replacing sessions that would have")
print("   summarised themselves perfectly well (case 96)")
print("   Both terms are read ONCE at the top of the rules that share them,")
print("   because 1r, 1a and 1b all ask about the same ceiling and the same")
print("   widest turn, and three lookups is how they come to disagree.")
check("it asks what has actually been survived here",
      "_ceil = compaction_too_big_why(path, _rrole" in _ps
      and 'wall = _ceil["ceiling"]' in _ps, True)
check("and still fires on position, not on distance", "used >= wall" in _ps,
      True)
check("and the exception needs the point below the wall as well",
      "compact < wall" in _ps and "used - compact <= _wide" in _ps, True)
check("and the turn it compares against is this pair's, measured",
      "_wide, _wide_src = turn_widest(path, _rrole)" in _ps, True)
print("   and the SENTENCE beside the number is that number's own. It used")
print("   to be wv['wall_source'], which describes window - RESERVED_TOKENS")
print("   while the figure printed came from compaction_too_big: on")
print("   2026-09-02 the journal read 'past the 559k wall (window minus the")
print("   33k compaction reserve)' and window minus that reserve is 967 000")
print("   (5.45)")
check("the handover reason labels the number it prints",
      '_ceil.get("source")' in _ps, True)
check("and no longer borrows wall_view's label for it",
      'wv.get("wall_source")' in _ps, False)
print("   and a session with no compaction point at all decides nothing")
print("   from this: rule 8 says an unknown point is reported, not guessed.")
print("   Nothing measured AND no threshold passed at launch is the case -")
print("   with a threshold passed the point is known and the branch applies")
cal[store.calib_key("opus 5", PATH)]["compact_at_tokens"] = None
store.save_calibration(cal)
daemon.STATE["pids"]["%s|executor" % daemon.norm(PATH)]["autocompact"] = None
_np = daemon.plan_for(_sj(996305), PATH)
check("no point, no handover from this branch", _np["do"] != "handover", True)
check("and the view says the point is unknown rather than guessing it",
      daemon.wall_view(_sj(996305), PATH)["compact"], None)

print("\n88. a tracked command whose PostToolUse never came silences")
print("    every tier of the watchdog, and used to do it for ever")
print("    2026-08-21: a heredoc was tracked at 16:41:18, the turn died at")
print("    16:44:33 with server_error, and from then on clinch(), stalled()")
print("    and the half-hourly assess() all read the pair as busy. A planner")
print("    went 20 minutes without answering a report and not one of them")
print("    said a word. A second project had carried the same since 08-18")
print("   first: the record is only made for a command that IS a build -")
print("   the pattern is matched against the command, not its heredoc")
_pt = inspect.getsource(daemon.handle_event)
check("the match reads the first line only",
      'head = cmd.splitlines()[0] if cmd else ""' in _pt, True)
check("and tests that, not the whole string",
      "any(p in head for p in patterns)" in _pt, True)
check("the whole-string test is gone",
      "any(p in cmd for p in patterns)" in _pt, False)
_real = ("cat >> research/prompts.md <<'ZZEOF'\n\n## A\n\n```\n"
         "make an idle animation of a pixelart mech\n")
_pats = ("godot", "pytest", "npm test", "cargo build", "make",
         "gradle", "dotnet build")
check("the command that started it matched on its payload",
      any(p in _real for p in _pats), True)
check("and does not match on its first line",
      any(p in _real.splitlines()[0] for p in _pats), False)
print("   a real build still matches, on either shape")
check("a bare one", any(p in "godot --headless --export".splitlines()[0]
                        for p in _pats), True)
check("and a chained one",
      any(p in "cd game && godot --headless".splitlines()[0]
          for p in _pats), True)

print("   second: a record that outlives every real command stops")
print("   counting as work, so one leak cannot silence a pair for ever")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "compactions": {},
                     "last_session": {}, "inflight": {}, "loops": {},
                     "paused": {}, "pids": {}})
daemon.PROCTRACK.clear()
daemon._INFLIGHT_STALE_TOLD.clear()
_k = daemon.norm(PATH)
check("an hour is the ceiling", daemon.INFLIGHT_MAX_SEC, 3600)
daemon.STATE["inflight"][_k] = {
    "cat": {"cmd": "cat >> notes.md <<'ZZEOF'", "started": time.time() - 60}}
check("a command running a minute is work",
      len(daemon.inflight_live(PATH)), 1)
check("and tier 2 calls the pair busy",
      daemon.tool_in_flight(PATH, "planner"), True)
daemon.STATE["inflight"][_k]["cat"]["started"] = time.time() - 5.7 * 3600
check("the same record 5.7 hours on is not work",
      len(daemon.inflight_live(PATH)), 0)
check("so tier 2 can see this pair again",
      daemon.tool_in_flight(PATH, "planner"), False)
print("   it is keyed by PROJECT, which is why the executor's leaked")
print("   record took the check out for the PLANNER as well")
check("the planner was silenced by the executor's record",
      "inflight_live(path)" in inspect.getsource(daemon.tool_in_flight), True)
print("   and a leak is said out loud once, not swallowed")
_il = inspect.getsource(daemon.inflight_live)
check("journalled at warn", '"warn"' in _il, True)
check("once per record", "_INFLIGHT_STALE_TOLD" in _il, True)

print("   third: tiers 1 and 3 read the same value through the same door")
_sit = inspect.getsource(daemon.situation)
check("situation asks inflight_live",
      '"inflight": inflight_live(path)' in _sit, True)
check("and not the raw dict",
      'list((STATE.get("inflight") or {}).get(path, {}).values())' in _sit,
      False)
_cl = inspect.getsource(daemon.clinch)
check("clinch still stands down on something in flight",
      'sit.get("inflight")' in _cl, True)
_as = inspect.getsource(daemon.assess)
check("and assess still exits early on it", 'sit["inflight"]' in _as, True)
print("   both are right to - what was wrong was the value they were given")

print("   fourth: the only sweeper walks memory, which a restart empties")
print("   while the record itself lives on disk and survives. After the")
print("   21:56:07 restart nothing could report the leak any more")
_pw = inspect.getsource(daemon.check_processes)
check("the process watcher walks PROCTRACK", "PROCTRACK.items()" in _pw, True)
check("not the persisted record", 'STATE.get("inflight")' in _pw, False)
daemon.PROCTRACK.clear()
daemon.STATE["inflight"][_k] = {
    "cat": {"cmd": "cat >> notes.md <<'ZZEOF'", "started": time.time() - 900}}
check("after a restart the watcher sees nothing",
      len(daemon.PROCTRACK.get(_k) or {}), 0)
check("re-seeding hands it back", daemon.reseed_proctrack(), 1)
check("and now the watcher has it", len(daemon.PROCTRACK.get(_k) or {}), 1)
check("with the ORIGINAL start time, not now",
      int(time.time() - daemon.PROCTRACK[_k]["cat"]["started"]) >= 890, True)
print("   it re-seeds and does not clear: the windows outlive the daemon,")
print("   so a command genuinely running at restart is still running")
check("a record already in memory is not doubled",
      daemon.reseed_proctrack(), 0)
_rs = inspect.getsource(daemon.reseed_proctrack)
check("nothing is dropped here - ageing is INFLIGHT_MAX_SEC's job",
      "pop(" in _rs or "clear()" in _rs, False)

print("\n89. a turn that died of an API error is a breakage, not a question")
print("    2026-08-21, third time in one day. A planner's turn died at")
print("    23:21:09 with \"API Error: The response stopped arriving\". What")
print("    ended the five-minute stop was the owner typing into the window")
print("    by hand. His words: the loop has to solve this kind of problem")
print("    itself")
print("   part one, and it is a miss in the fix of the same morning:")
print("   pair_moved_since read the RAW inflight dict, so the leaked record")
print("   that case 88 is about still answered 'moving' and turned the")
print("   lost-turn message into a journal line")
_pms = inspect.getsource(daemon.moved_witness)
check("it goes through inflight_live now", "inflight_live(path)" in _pms, True)
check("and not through the raw dict",
      'STATE.get("inflight") or {}).get(norm(path))' in _pms, False)
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "inflight": {},
                     "stop_seen": {}, "last_task": {}, "loops": {},
                     "paused": {}, "pids": {}, "stopfail": {}})
daemon.PROCTRACK.clear()
daemon._INFLIGHT_STALE_TOLD.clear()
_k89 = daemon.norm(PATH)
_when = time.time() - 300
daemon.STATE["inflight"][_k89] = {
    "mkdir": {"cmd": "mkdir -p tools", "started": time.time() - 84 * 3600}}
check("a leaked record no longer counts as the pair moving",
      daemon.pair_moved_since(PATH, "planner", _when), False)
daemon.STATE["inflight"][_k89]["mkdir"]["started"] = time.time() - 60
print("   asked about the EXECUTOR, whose tool it is - since 2026-08-22 a")
print("   tracked command is not an alibi for the planner (case 94)")
check("a real running command still does",
      daemon.pair_moved_since(PATH, "executor", _when), True)

print("   part two: the message the owner actually got, and why it was")
print("   wrong. A verdict went out at 23:13:26, the executor picked it up")
print("   at 23:13:27 and its transcript shows unbroken work to 23:28:34 -")
print("   yet at 23:22:55 the bridge told him \"the executor never started a")
print("   turn. Type anything in its window to wake it\"")
_cs = inspect.getsource(daemon.check_stalls)
check("check_stalls asks whether it is working before nudging",
      "executor_is_working(path)" in _cs, True)
_ew = inspect.getsource(daemon.executor_is_working)
check("and 'working' means writing, not 'has finished a turn'",
      "transcript_frozen(path, \"executor\", grace)" in _ew, True)
print("   awaiting is cleared at the executor's next Stop - the END of the")
print("   turn - so a long turn and a dead one looked identical here")
check("cannot tell answers False, so the watcher is never silenced",
      "return False" in _ew.split("except Exception:")[-1], True)

print("   part three: the bridge picks a dead turn back up itself, and")
print("   only calls a person after that has failed")
check("three attempts", daemon.LOST_TURN_TRIES, 3)
check("with the wait growing between them", daemon.LOST_TURN_BACKOFF, 2.0)
_rl = inspect.getsource(daemon.revive_lost_turn)
print("   the boundary is exact: it re-delivers what the bridge already")
print("   holds, and writes nothing either half was in the middle of")
check("a held task goes back to the executor", "take_open_task(path)" in _rl,
      True)
check("a held report goes back to the planner",
      'deliver_ex(path, "planner", pend["content"]' in _rl, True)
check("it never writes a verdict", "verdict" in _rl.lower().replace(
    "never writes a verdict", ""), False)
check("and never invents a report",
      "report %s back to the planner" in _rl, True)
print("   a planner with nothing pending gets nothing invented for it")
daemon.PENDING.pop(_k89, None)
check("nothing to hand back means nothing done",
      daemon.revive_lost_turn(PATH, "planner"), "")

print("   and the escalation says what was already tried, so the person is")
print("   not asked to guess")
_cl89 = inspect.getsource(daemon.check_lost_turn)
check("the attempts are counted on the record", 'r["revives"] = tries + 1' in
      _cl89, True)
check("what was tried is kept", 'r["tried"]' in _cl89, True)
check("the backoff is applied to the grace",
      "LOST_TURN_BACKOFF ** tries" in _cl89, True)
check("and the message carries it", 'brief(tried, 120)' in _cl89, True)
print("   the OTHER class is untouched: a planner saying 'this is the")
print("   owner's decision' is a real question and still rings a person")
_ch = inspect.getsource(daemon.call_human_about)
check("the hand-back still calls the owner", "yours to decide" in _ch, True)
check("still on the needs_you kind", 'notify("needs_you"' in _ch, True)
print("   two classes, two functions, and neither one does the other's job")
check("the breakage path never rings on its own attempts",
      'notify(' in inspect.getsource(daemon.revive_lost_turn), False)

print("\n90. a record already aged out is not reported again")
print("    2026-08-21 23:01: two process_stuck messages went to the owner's")
print("    phone about records of 84 h and 6 h that inflight_live had")
print("    already decided were not work. They were fresh to this watcher")
print("    because reseed_proctrack had just handed them back after a")
print("    restart, so nothing here had flagged them yet")
print("   the owner's decision: one warn when it ages out is enough. The")
print("   chat is a telephone, not a journal")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "inflight": {},
                     "loops": {}, "paused": {}, "pids": {}})
daemon.PROCTRACK.clear()
daemon.DURATIONS.clear()
daemon._INFLIGHT_STALE_TOLD.clear()
_k90 = daemon.norm(PATH)
_said90 = []
_sent90 = []
_realn90, daemon.notify = daemon.notify, \
    lambda kind, text, **kw: _said90.append(kind)
_reald90, daemon.deliver = daemon.deliver, \
    lambda *a, **kw: _sent90.append(a[1] if len(a) > 1 else "?") or True

try:
    print("   a genuinely slow command is still reported, exactly as before")
    # ITS SESSION IS ONE THE BRIDGE KNOWS, as every live one is: the
    # PreToolUse that makes a record also puts its session in the books.
    # Since 8.35 a record of a session no book knows is closed as gone by
    # the very sweep this calls, and this fixture had wiped the books.
    daemon.remember_session(PATH, "executor", "s")
    daemon.PROCTRACK[_k90] = {"godot": {
        "cmd": "godot --headless --export", "session": "s",
        "started": time.time() - 1000}}
    daemon.check_processes()
    check("the pair is asked about a 16-minute command", len(_sent90), 1)
    check("and it went to the planner first",
          _sent90[0] if _sent90 else None, "planner")
    check("nobody's phone rang yet", _said90, [])

    print("   the same record once it is past the ageing ceiling: nothing")
    _sent90[:] = []
    _said90[:] = []
    daemon.PROCTRACK[_k90] = {"cat": {
        "cmd": "cat >> notes.md <<'ZZEOF'", "session": "s",
        "started": time.time() - 84 * 3600}}
    daemon.check_processes()
    check("the pair is not asked about a leaked record", _sent90, [])
    check("and the human is not woken about it either", _said90, [])
    check("nothing was flagged on it, so nothing can escalate later",
          daemon.PROCTRACK[_k90]["cat"].get("flagged"), None)

    print("   run it again, the way the 30-second loop would: still nothing")
    daemon.check_processes()
    daemon.check_processes()
    check("no message on any later pass", _said90 + _sent90, [])

    print("   the ceiling is the SAME constant inflight_live uses - one idea")
    print("   of 'too long to be real' in this file, not two")
    # The claim has not moved, the place has: since 2026-09-03 the ceiling
    # is decided in ONE function that three readers call, because a
    # background command needs a different one and two ideas of "too long"
    # is exactly what this case exists to prevent. So the assertion follows
    # it - check_processes must ask record_expired, and record_expired must
    # be the thing that knows the constant.
    _cp90 = inspect.getsource(daemon.check_processes)
    check("check_processes asks record_expired rather than timing it itself",
          "record_expired(meta)" in _cp90, True)
    check("and record_expired is where INFLIGHT_MAX_SEC is read",
          "INFLIGHT_MAX_SEC" in inspect.getsource(daemon.record_expired),
          True)
    check("and inflight_live agrees the record is not work",
          len(daemon.inflight_live(PATH)), 0)
    print("   a record just under the ceiling is still ordinary work")
    _said90[:] = []
    _sent90[:] = []
    daemon.PROCTRACK[_k90] = {"pytest": {
        "cmd": "pytest -q", "session": "s",
        "started": time.time() - (daemon.INFLIGHT_MAX_SEC - 60)}}
    daemon.check_processes()
    check("just under the ceiling, the pair is still asked", len(_sent90), 1)
finally:
    daemon.notify = _realn90
    daemon.deliver = _reald90

print("\n91. how long a half has been quiet is an epoch question")
print("    2026-08-22, 00:03. test_wall_handover had passed at 23:49 and")
print("    failed at 23:59 with nothing changed but the date. situation()")
print("    measured silence as now - _clock_of(last_seen), and last_seen is")
print("    \"%H:%M:%S\" with no date, so _clock_of put 23:58 on TODAY - five")
print("    minutes ago came out as MINUS 86 084 seconds")
print("   every caller reads a small number as 'answered recently', so the")
print("   blind poll stood down for every pair last seen before midnight -")
print("   and would have gone on doing it for the next 24 hours")
_ss = inspect.getsource(daemon.situation)
check("silence comes from the epoch now",
      '"silent_for": (time.time() - at) if at else None' in _ss, True)
check("and not from the clock stamp", "_clock_of(seen)" in _ss, False)
check("_clock_of is left for rendering, not for durations",
      "ON TODAY'S CLOCK" in inspect.getsource(daemon._clock_of), True)

print("   the arithmetic itself, on the two stamps that broke it")
_before_midnight = time.mktime((2026, 8, 21, 23, 58, 41, 0, 0, -1))
_after_midnight = time.mktime((2026, 8, 22, 0, 3, 23, 0, 0, -1))
check("five minutes apart, in truth",
      int(_after_midnight - _before_midnight), 282)
check("and the clock reading is negative and enormous",
      (_after_midnight - time.mktime((2026, 8, 22, 23, 58, 41, 0, 0, -1)))
      < -86000, True)

print("   the record a real session carries has both fields - touch_session")
print("   writes them together - and only the epoch one may be measured")
_ts = inspect.getsource(daemon.touch_session)
check("touch_session writes the clock for a person to read",
      'sess["last_seen"] = now()' in _ts, True)
check("and the epoch for the bridge to measure",
      'sess["seen_at"] = time.time()' in _ts, True)

print("   a record too old to have the epoch answers 'cannot say', not a")
print("   duration read off somebody else's day")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "inflight": {},
                     "loops": {}, "paused": {}, "pids": {}, "last_session": {}})
daemon.PROCTRACK.clear()
_k91 = daemon.norm(PATH)
daemon.STATE["sessions"]["executor:old91"] = {
    "role": "executor", "path": _k91, "session_id": "old91",
    "model": "Opus 5", "window": 1000000, "context_tokens": 100000,
    "state": "idle", "last_seen": "23:58:41"}
_sit91 = daemon.situation(PATH)
check("no seen_at, no claim about silence",
      _sit91["roles"]["executor"]["silent_for"], None)
daemon.STATE["sessions"]["executor:old91"]["seen_at"] = time.time() - 900
_sit91 = daemon.situation(PATH)
check("with the epoch, fifteen minutes reads as fifteen minutes",
      880 < _sit91["roles"]["executor"]["silent_for"] < 920, True)
check("and it is never negative",
      _sit91["roles"]["executor"]["silent_for"] > 0, True)

print("   the same rule broken twice in already_up: it string-sorted the")
print("   clock stamps to pick the newest record, and measured recency off")
print("   _clock_of. At 00:03 \"23:58:41\" sorts above \"00:03:22\"")
check("yesterday's stamp really does sort above today's",
      "23:58:41" >= "00:03:22", True)
_au = inspect.getsource(daemon.already_up)
check("the newest record is picked by epoch", "seen_at(sess) >= newest" in _au,
      True)
check("not by string order", 'if seen >= when:' in _au, False)
check("and recency is measured against the epoch too",
      "abs(time.time() - newest) < 300" in _au, True)

print("   a gate on the class, not just on the two places it was found:")
print("   nothing in the package may measure a DURATION from a clock stamp")
_dsrc = inspect.getsource(daemon)
_bad = [ln.strip() for ln in _dsrc.splitlines()
        if "_clock_of" in ln and not ln.strip().startswith("#")
        and ("time.time() -" in ln or "- _clock_of" in ln)]
check("no subtraction against _clock_of anywhere", _bad, [])
print("   the ledgers that ARE sorted by time carry an epoch to sort by")
check("session_roles stamps an epoch", '"at": time.time()}' in _dsrc, True)
check("telemetry keeps the clock for reading and an epoch beside it",
      '"at": time.strftime("%H:%M:%S"), "epoch": time.time()' in _dsrc, True)
print("   and the two date readers in store are date-stamped strings, not")
print("   clocks: a full \"%Y-%m-%d ...\" prefix and a folder name")
_ssrc = inspect.getsource(store)
check("the once-a-day guard compares a dated stamp",
      '(e.get("at") or "").startswith(today)' in _ssrc, True)
check("and the archiver measures age by mtime, not by name",
      "os.path.getmtime(full) < cutoff" in _ssrc, True)

print("\n92. a threshold that moves DOWN must not be filtered out as noise")
print("    compaction_point drops samples further than one turn from its")
print("    anchor, and the anchor was max(). That is right for one manual")
print("    /compact among automatic ones and wrong for a threshold that")
print("    moves: after 996k samples, an honest first compaction at 700k is")
print("    299k below the maximum and was thrown away, so the point stayed")
print("    at 996k for ever and the pair could never recover")
_stuck = [998975, 998619, 999875, 999887, 999920, 999595, 999648, 999729,
          996305, 998685]
check("the point while the old regime holds",
      daemon.compaction_point(_stuck, _TURN_W), 996305)
check("a first honest compaction at 700k IS the new point",
      daemon.compaction_point((_stuck + [700100])[-10:], _TURN_W), 700100)
check("and so is one at 690k",
      daemon.compaction_point((_stuck + [690000])[-10:], _TURN_W), 690000)
print("   the drop is bigger than any single turn, which is exactly why the")
print("   old anchor discarded it")
check("299820 is further than one turn of this pair",
      999920 - 700100 > _TURN_W, True)
print("   the case the filter was born for still works: the anchor is the")
print("   NEWEST sample, so a manual /compact far below the recent cluster")
print("   is still as far away as it ever was")
_poison = [776393, 998975, 998619, 999875, 999887, 999920, 999595, 999648,
           999729, 996305]
check("one manual compaction is still dropped",
      daemon.compaction_point(_poison, _TURN_W), 996305)
check("the band is two-sided now",
      "abs(anchor - s) <= widest"
      in inspect.getsource(daemon.compaction_point), True)
check("and the anchor is the newest sample, not the largest",
      "anchor = good[-1]" in inspect.getsource(daemon.compaction_point), True)
check("one sample is still itself",
      daemon.compaction_point([150000], _TURN_W), 150000)
check("nothing measured is still nothing",
      daemon.compaction_point([], _TURN_W), None)

print("\n93. a handover that never arrives is not tried for ever")
print("    2026-08-22, 05:16 to 08:41: plan_for said handover, a window")
print("    opened, ten minutes later it had not come up, expire_handover")
print("    cleared the flag and the next pass decided the same thing.")
print("    Twenty-one windows, and a message to a person each time. The")
print("    launches-per-hour cap never bit because the cadence IS six an")
print("    hour - handover_grace is 600s")
check("two failures in a row are enough", daemon.HANDOVER_FAILS_BEFORE_HOLD, 2)
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "inflight": {},
                     "loops": {}, "paused": {}, "pids": {},
                     "handover_failed": {}, "launches": {}, "handover": {}})
daemon.PROCTRACK.clear()
_k93 = daemon.norm(PATH)
check("with nothing failed, a handover may run",
      daemon.handover_blocked(PATH, ("executor",)), None)
daemon.STATE["handover_failed"][_k93] = {"n": 1, "at": time.time()}
check("one failure is not a pattern - still allowed",
      daemon.handover_blocked(PATH, ("executor",)), None)
daemon.STATE["handover_failed"][_k93] = {"n": 2, "at": time.time()}
_why93 = daemon.handover_blocked(PATH, ("executor",))
check("two in a row and it stops deciding", bool(_why93), True)
check("and says what it is waiting for",
      "none of the new windows ever came up" in (_why93 or ""), True)
print("   expire_handover is what counts them, and it counts on the way out")
_eh = inspect.getsource(daemon.expire_handover)
check("a handover that never finished is recorded as failed",
      'rec["n"] = int(rec.get("n") or 0) + 1' in _eh, True)
print("   the hold is not permanent: a window coming up ends the streak,")
print("   because that is the evidence that whatever swallowed the others")
print("   is over. Otherwise a pair whose stuck window somebody simply")
print("   closed could never be handed over again")
daemon.STATE.setdefault("pids", {})["%s|executor" % _k93] = {
    "pid": 4242, "at": time.time(), "registered": False}
daemon.mark_registered(PATH, "executor")
check("registering clears the streak",
      (daemon.STATE.get("handover_failed") or {}).get(_k93), None)
check("and the next handover may run again",
      daemon.handover_blocked(PATH, ("executor",)), None)
print("   the specific reasons still speak first - a pending window names")
print("   itself rather than being hidden behind the streak")
_hb = inspect.getsource(daemon.handover_blocked)
check("launch_guard is consulted before the streak",
      _hb.index("launch_guard(path, role)") < _hb.index("handover_failed"),
      True)

print("\n94. a working executor is not an alibi for a dead planner")
print("    pair_moved_since decides whether a dead turn gets picked back up,")
print("    and two of its five witnesses were keyed by PROJECT: last_task is")
print("    when work went to the EXECUTOR, and a tracked command is a Bash")
print("    tool, which only the executor has - disallow_for denies the")
print("    planner Bash outright. So a busy executor answered 'the pair is")
print("    moving' for a planner that had died, and the medicine never ran")
print("    Same class as stalled(): keyed by project, one half silences the")
print("    check for the other")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "inflight": {},
                     "stop_seen": {}, "last_task": {}, "loops": {},
                     "paused": {}, "pids": {}, "last_session": {}})
daemon.PROCTRACK.clear()
daemon._INFLIGHT_STALE_TOLD.clear()
_k94 = daemon.norm(PATH)
_died = time.time() - 600

print("   the executor is demonstrably busy: work went to it after the death")
daemon.STATE["last_task"][_k94] = _died + 30
check("the executor itself counts as moving",
      daemon.pair_moved_since(PATH, "executor", _died), True)
check("but the planner does not - its neighbour is not its alibi",
      daemon.pair_moved_since(PATH, "planner", _died), False)

print("   and the same for a tracked command, which is a Bash tool and")
print("   therefore the executor's by construction")
daemon.STATE["last_task"] = {}
daemon.STATE["inflight"][_k94] = {"godot": {"cmd": "godot --headless",
                                            "started": time.time() - 60}}
check("a running command means the executor is moving",
      daemon.pair_moved_since(PATH, "executor", _died), True)
check("and says nothing about the planner",
      daemon.pair_moved_since(PATH, "planner", _died), False)

print("   the planner's own witnesses still work, so a planner that really")
print("   did come back is not reported as dead")
daemon.STATE["stop_seen"]["%s|planner" % _k94] = _died + 60
check("its own finished turn is its own alibi",
      daemon.pair_moved_since(PATH, "planner", _died), True)

print("   the gate is written where it can be read")
_pm = inspect.getsource(daemon.moved_witness)
check("the two project-keyed witnesses are executor-only",
      'if role == "executor":' in _pm, True)
check("stop_seen stays role-keyed for both",
      '"%s|%s" % (norm(path), role)' in _pm, True)

print("   reconstruction of the four turns that died on 2026-08-22:")
print("   01:28:54 planner, 05:07:16, 08:54:01 and 09:02:51 executor. All")
print("   four had a leaked tracked record standing in the dict, and the")
print("   planner death had a busy executor beside it as well")


def _old_moved(path, role, when):
    """pair_moved_since exactly as it stood before 2026-08-22."""
    k = daemon.norm(path)
    if float((daemon.STATE.get("stop_seen") or {}).get(
            "%s|%s" % (k, role)) or 0) > when:
        return True
    if float((daemon.STATE.get("last_task") or {}).get(k) or 0) > when:
        return True
    if (daemon.STATE.get("inflight") or {}).get(k):      # the RAW dict
        return True
    return False


for _role, _busy in (("planner", True), ("executor", False),
                     ("executor", False), ("executor", False)):
    daemon.STATE["stop_seen"] = {}
    daemon.STATE["last_task"] = {_k94: _died + 30} if _busy else {}
    daemon.STATE["inflight"] = {_k94: {"mkdir": {
        "cmd": "mkdir -p tools", "started": time.time() - 94 * 3600}}}
    daemon._INFLIGHT_STALE_TOLD.clear()
    check("%s: it used to read as moving" % _role,
          _old_moved(PATH, _role, _died), True)
    check("%s: and now it reads as stopped, so the medicine runs" % _role,
          daemon.pair_moved_since(PATH, _role, _died), False)

print("\n95. a session that can never compact is replaced calmly, early")
print("    The owner, 2026-08-22: a session must be able to compact, or")
print("    every time it will be a replacement, and that is bad on long")
print("    tasks. The bridge cannot give him the first half - a window")
print("    launched with autocompact 70 compacted at 998 685, and ten")
print("    samples from windows given 80 AND 70 all land between 996 305")
print("    and 999 920. The percentage does not move the point in this")
print("    client build. So the goal is the second half: not losing the")
print("    work, which means replacing in a quiet moment with a full")
print("    handoff instead of crashing at the wall")
check("two of its own worst turns of room", daemon.EARLY_ROTATE_TURNS, 2)
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "compactions": {},
                     "last_session": {}, "inflight": {}, "loops": {},
                     "paused": {},
                     "pids": {"%s|executor" % daemon.norm(PATH):
                              {"pid": 1, "at": time.time(),
                               "registered": True, "autocompact": 70,
                               "model_req": "opus"}}})
daemon.PROCTRACK.clear()
cal = store.load_calibration()
cal[store.calib_key("opus 5", PATH)] = {
    "ceiling_pct": 97.0, "buffer_tokens": 33000, "misses": 14,
    "clean_streak": 0, "multiplier": 3.0, "wall_history_tokens": None,
    "compact_at_tokens": 996305, "compact_at_window": 1000000,
    "how": "the honest point, and it is above what a compaction needs"}
store.save_calibration(cal)


def _e95(used, costs=(33877, 15151, 39330, 32199)):
    s = {"role": "executor", "path": daemon.norm(PATH), "session_id": "e95",
         "model": "Opus 5", "window": 1000000, "window_observed": True,
         "context_tokens": used, "turn_costs": list(costs)}
    daemon.STATE["sessions"]["executor:e95"] = s
    return s


_wv95 = daemon.wall_view(_e95(500000), PATH)
check("the point leaves less than a compaction needs",
      _wv95["compact"] > 1000000 - daemon.RESERVED_TOKENS, True)
check("its worst measured turn", _wv95["worst_turn"], 39330)
print("   far from the point, nothing changes - a long task keeps its window")
check("half full is ordinary work", daemon.plan_for(_e95(500000), PATH)["do"],
      "working")
check("and so is 800k", daemon.plan_for(_e95(800000), PATH)["do"], "working")
print("   two of its own turns short of the point, it is replaced - calmly,")
print("   with the handoff, and the turn that would have hit the wall is")
print("   never started")
_edge = 996305 - 2 * 39330
print("   (near the point, rule 2 already calls it 'compacting' - that is")
print("   the ordinary answer and it is not a replacement)")
check("one token before that, not replaced",
      daemon.plan_for(_e95(_edge - 1), PATH)["do"], "compacting")
_pl95 = daemon.plan_for(_e95(_edge), PATH)
check("at it, a planned replacement", _pl95["do"], "handover")
check("and the words are the calm ones",
      "while there is still room to do it calmly" in _pl95["why"], True)
check("naming the point and what a compaction needs",
      "996k" in _pl95["why"] and "33k" in _pl95["why"], True)

print("   a session whose point IS reachable is never touched by this - it")
print("   compacts, which is what everybody wants")
cal[store.calib_key("opus 5", PATH)]["compact_at_tokens"] = 700000
store.save_calibration(cal)
check("a point that fits leaves the session alone at 690k",
      daemon.plan_for(_e95(690000), PATH)["do"] != "handover", True)
check("and at 660k", daemon.plan_for(_e95(660000), PATH)["do"] != "handover",
      True)
cal[store.calib_key("opus 5", PATH)]["compact_at_tokens"] = 996305
store.save_calibration(cal)

print("   with no turn cost measured yet it does not fire at all, rather")
print("   than guess a margin")
check("no measured turns, no early rotation",
      daemon.plan_for(_e95(_edge, costs=()), PATH)["do"] != "handover", True)
print("   and it decides only where a decision is quiet: plan_for is asked")
print("   at a turn boundary and by assess(), which has already returned on")
print("   anything in flight, under review, travelling or handing over")
_as95 = inspect.getsource(daemon.assess)
check("assess stands down on something running",
      'sit["inflight"] or looks_busy(ex["tail"])' in _as95, True)
check("on a report under review", 'sit["reviewing"]' in _as95, True)
check("on a verdict in flight", 'sit["verdict_in_flight"]' in _as95, True)
check("and on a handover already under way", 'sit["handover"]' in _as95, True)

print("\n96. what a session has survived outranks a reserve nobody measured")
print("    The owner, 2026-08-22: find after WHAT compaction stopped")
print("    happening. The record answers it, and the answer is us. Of 41")
print("    compactions on file, 37 recorded a FLOOR - proof the session")
print("    shrank and carried on - and 33 of those were at 995k or above,")
print("    the best of them at 1 001 318 on a 1 000 000 window")
print("   so the old line, window minus a 33k reserve, had 33 successful")
print("   compactions standing above it. Before 2026-08-21 the bridge stood")
print("   aside and those sessions compacted and lived; after it, rules 1a")
print("   and 1b replaced them first. The bridge took their compaction away")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "windows": {}, "compactions": {},
                     "last_session": {}, "inflight": {}, "loops": {},
                     "paused": {}, "pids": {}})
daemon.PROCTRACK.clear()
_k96 = daemon.norm(PATH)
check("with nothing measured it still falls back to the reserve",
      daemon.compaction_too_big(PATH, "executor", 1000000),
      1000000 - daemon.RESERVED_TOKENS)
print("   a compaction with no floor is not evidence of anything: the")
print("   session may have died in it, which is exactly what a missing")
print("   floor means")
daemon.STATE["compactions"]["%s|executor" % _k96] = [
    {"at": "2026-08-21 20:50", "tokens": 996305, "session": "a"}]
check("a floorless sample proves nothing",
      daemon.compaction_survivable(PATH, "executor"), None)
check("so the fallback still applies",
      daemon.compaction_too_big(PATH, "executor", 1000000),
      1000000 - daemon.RESERVED_TOKENS)
print("   a floor is the proof, and it moves the line UP")
daemon.STATE["compactions"]["%s|executor" % _k96].append(
    {"at": "2026-08-18 17:23", "tokens": 999920, "after": 188013,
     "session": "b"})
check("the best proven size is the one with a floor",
      daemon.compaction_survivable(PATH, "executor"), 999920)
print("   'one turn' is this pair's own, now, and ORDINARY rather than")
print("   widest: the branch asks how far above its own point a session")
print("   sits without being in trouble, and the top decile is exactly the")
print("   part that is not ordinary. Its four measured turns go in through")
print("   note_turn_cost, beside the 200 274 the case above recorded")
for _c96 in (33877, 15151, 39330, 32199):
    daemon.note_turn_cost(PATH, "executor", _c96, "s96")
_ord96, _src96 = daemon.turn_ordinary(PATH, "executor")
check("and it is measured for this pair", _src96, "measured")
check("and the line sits one turn above it",
      daemon.compaction_too_big(PATH, "executor", 1000000),
      999920 + _ord96)
print("   one turn, because a sample IS an overshoot: the threshold is")
print("   below it, and a session ordinarily ends a turn above its own last")
print("   compaction size without being in trouble at all")

print("   the effect on the two branches, on the real numbers of the pair")
print("   that prompted this: point 996 305, window 1M, worst turn 39 330")
cal = store.load_calibration()
cal[store.calib_key("opus 5", PATH)] = {
    "ceiling_pct": 97.0, "buffer_tokens": 33000, "misses": 0,
    "clean_streak": 0, "multiplier": 3.0, "wall_history_tokens": None,
    "compact_at_tokens": 996305, "compact_at_window": 1000000, "how": "test"}
store.save_calibration(cal)
daemon.STATE["pids"]["%s|executor" % _k96] = {
    "pid": 1, "at": time.time(), "registered": True, "autocompact": 70,
    "model_req": "opus"}


def _s96(used):
    s = {"role": "executor", "path": _k96, "session_id": "s96",
         "model": "Opus 5", "window": 1000000, "window_observed": True,
         "context_tokens": used, "turn_costs": [33877, 15151, 39330, 32199]}
    daemon.STATE["sessions"]["executor:s96"] = s
    return s


check("at 930k it is left alone to compact",
      daemon.plan_for(_s96(930000), PATH)["do"] != "handover", True)
check("and at 996k too - it has survived that size",
      daemon.plan_for(_s96(996000), PATH)["do"] != "handover", True)
print("   which is the whole point: the bridge stands aside again, and the")
print("   session gets to do the thing everybody wants it to do")
print("   a replacement is still earned by a compaction that really")
print("   fails - but 2026-08-22 moved WHERE that is decided, and this")
print("   check moved with it deliberately. It used to read: the")
print("   StopFailure branch rotates there and then. That is exactly")
print("   what killed five sessions: on this client a prompt-too-long")
print("   at the top of the window is the compaction STARTING, and the")
print("   bridge was firing two seconds after its own PreCompact")
_he = inspect.getsource(daemon.handle_event)
check("the error type is still what the branch turns on",
      '"invalid" in etype or "context" in etype' in _he, True)
check("but it now asks first whether a compaction is under way",
      "wait_for_compaction(path, role, sess, over)" in _he, True)
print("   and since 2026-08-30 it hands over what the client actually")
print("   SAID, because the witness it used to require - a PreCompact -")
print("   cannot exist when the API refuses first. That is the executor")
print("   of 18:27:43: prompt is too long, no PreCompact ever, wall")
print("   handling in the same second, a session killed 2-3 seconds into")
print("   its own recovery (test_multipair case 55)")
check("the overflow is read from the payload, not from the category",
      "overflow_said(event) or overflow_by_size(sess, path, role)" in _he,
      True)
check("and it is the client's sentence that identifies one",
      daemon.overflow_said({"error": "invalid_request", "error_details":
                            '400 {"message":"prompt is too long: 1000815 '
                            'tokens > 1000000 maximum"}'}),
      (1000815, 1000000))
check("while an invalid_request that is not an overflow is not read as one",
      daemon.overflow_said({"error": "invalid_request",
                            "error_details": "400 tool schema is wrong"}),
      None)
check("and the StopFailure branch no longer rotates by itself",
      'args=(path, "hit the wall")' in _he, False)
check("the replacement lives in handle_wall_hit instead",
      'args=(path, "hit the wall")'
      in inspect.getsource(daemon.handle_wall_hit), True)
check("with two callers: the immediate one and the timed-out one",
      ("handle_wall_hit(path, role, ref)" in _he,
       "handle_wall_hit(path, role, sess)"
       in inspect.getsource(daemon.check_compaction)), (True, True))

print("   and with NO proven history the caution is unchanged, so a fresh")
print("   pair is no worse off than before")
daemon.STATE["compactions"]["%s|executor" % _k96] = []
check("no history, the old reserve line",
      daemon.compaction_too_big(PATH, "executor", 1000000),
      1000000 - daemon.RESERVED_TOKENS)

print("\n97. a witness that cannot be named grants no alibi")
print("    Three dead turns in two days were swallowed by the same")
print("    reassuring sentence - 'the pair is moving again - not telling' -")
print("    and none of the three pairs had moved. The line never said WHO")
print("    said so, which is how it could be wrong three times quietly")
_mw97 = inspect.getsource(daemon.moved_witness)
check("the witness names itself and its stamp",
      "is past the death at" in _mw97, True)
check("and the journal line carries it",
      "Witness: %s" in inspect.getsource(daemon.check_lost_turn), True)
print("   the session's own seen_at is not a witness at all: EVERY event")
print("   stamps it, including the fatal one, so it cannot show life")
check("seen_at is not consulted", 'sess.get("seen_at")' in _mw97, False)
print("   and cannot-tell is no longer silence. Safe to reverse only")
print("   because the consequence changed: a dead turn is answered by")
print("   handing the work back, not by waking somebody")
check("an unreadable witness is not an alibi",
      _mw97.split("except Exception")[-1].strip().endswith('return ""'), True)
print("   the ordering that made the death its own alibi, locked down:")
_he97 = inspect.getsource(daemon.handle_event)
check("the death is stamped after everything the death writes",
      _he97.index('touch_session(event, state="error")')
      < _he97.index("note_stopfail(path, role, reason, kept)"), True)

print("   named witnesses still work when they are real")
daemon.STATE.clear()
daemon.STATE.update({"sessions": {}, "stop_seen": {}, "last_task": {},
                     "inflight": {}, "loops": {}, "paused": {}, "pids": {},
                     "last_session": {}})
daemon.PROCTRACK.clear()
_k97 = daemon.norm(PATH)
_d97 = time.time() - 300
check("nothing moved, so nothing is claimed",
      daemon.moved_witness(PATH, "executor", _d97), "")
daemon.STATE["stop_seen"]["%s|executor" % _k97] = _d97 + 60
_w97 = daemon.moved_witness(PATH, "executor", _d97)
check("a finished turn is a witness", bool(_w97), True)
check("and it says which one, and when",
      "stop_seen" in _w97 and "past the death at" in _w97, True)
print("   a task delivered to the executor is one too, and belongs only to")
print("   the executor - the planner has no Bash and takes no tasks")
daemon.STATE["stop_seen"] = {}
daemon.STATE["last_task"][_k97] = _d97 + 30
check("the executor has an alibi",
      "a task went out at" in daemon.moved_witness(PATH, "executor", _d97),
      True)
check("the planner does not",
      daemon.moved_witness(PATH, "planner", _d97), "")

print("\n98. the rule a LIVE session can still receive")
print("    2026-08-22: the owner wrote to a planner at 17:27:31 and got no")
print("    answer - it read the question and went straight to work. Its")
print("    transcript from there: thinking, tool_use, tool_use, thinking,")
print("    tool_use ... and ZERO text blocks. He had asked the same thing")
print("    at 12:56 - 'are you going to answer?'")
print("   the instructions were fixed that morning and could not reach it:")
print("   INSTRUCTIONS is returned in the MCP initialize handshake, read")
print("   once when the channel starts. That session started 08-21 04:18.")
print("   Same class as a live window carrying yesterday's threshold")
_ch = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bridgecore", "channel.py"),
              encoding="utf-8").read()
check("the instructions ride on the handshake",
      '"instructions": INSTRUCTIONS' in _ch, True)
print("   so the rule goes where a live session DOES look every time: the")
print("   preamble in front of every task and every report, read from disk")
print("   on each delivery, no rotation needed")
check("the close carries it", "VISIBLE TEXT" in daemon.RULES_CLOSE, True)
check("and says where it must go", "LAST block of your turn"
      in daemon.RULES_CLOSE, True)
check("leaning on rule 27, not inventing a new one",
      "rule 27" in daemon.RULES_CLOSE, True)
_d = daemon.rules_for_delivery("report", "some-session-id")
check("a report delivery carries it", "VISIBLE TEXT" in _d, True)
check("and so does a task", "VISIBLE TEXT"
      in daemon.rules_for_delivery("task", "some-session-id"), True)
print("   it is short on purpose - it rides on every delivery, and the")
print("   canon's own size rule says every character is paid for each time")
check("three lines, not a paragraph",
      daemon.RULES_CLOSE.count("\n") <= 6, True)

print("\n99. a journal row's identity, and what a project folder carries")
print("    ANALYSIS-portable-history.md step 1. journal() has always")
print("    written every line twice - data/logs and, when project_dir is")
print("    given, the project's own bridge-logs. Measured 2026-08-28: the")
print("    project copy of that day held 1894 rows, byte-identical to the")
print("    1894 the central journal held for it. Nothing read it back, so")
print("    a project carried to another machine arrived with a history the")
print("    bridge could not see")
PH = os.path.join(TMP, "carried-project")
os.makedirs(os.path.join(PH, "bridge-logs", "2026-08-01"), exist_ok=True)
os.makedirs(os.path.join(PH, "bridge-logs", "2026-08-02"), exist_ok=True)
OLDP = "e:" + chr(92) + "elsewhere" + chr(92) + "carried-project"


def _ph_row(at, text, path=OLDP, kind="loop"):
    return {"at": at, "kind": kind, "text": text, "project": "Carried",
            "path": path, "session": "executor", "level": "log"}


def _ph_write(day, rows):
    os.makedirs(os.path.join(PH, "bridge-logs", day), exist_ok=True)
    f = os.path.join(PH, "bridge-logs", day, "events.jsonl")
    with io.open(f, "w", encoding="utf-8", newline="") as fh:
        for r in rows:
            fh.write(_json.dumps(r, ensure_ascii=False) + "\n")
    return f


_ph_write("2026-08-01", [_ph_row("2026-08-01T10:00:00", "one"),
                         _ph_row("2026-08-01T10:00:01", "two")])
_ph_write("2026-08-02", [_ph_row("2026-08-02T11:00:00", "three")])

print("   the fingerprint answers 'is this the same event', and the answer")
print("   must not depend on which machine wrote it - path is the one field")
print("   the merge rewrites, so it is excluded")
_r1 = _ph_row("2026-08-01T10:00:00", "one")
_r2 = _ph_row("2026-08-01T10:00:00", "one",
              path="c:" + chr(92) + "here" + chr(92) + "carried-project")
check("same row under two drive letters is one fingerprint",
      store.row_fingerprint(_r1), store.row_fingerprint(_r2))
_r3 = dict(_r1)
_r3["path_was"] = OLDP
check("and an already-imported row does not become a different one",
      store.row_fingerprint(_r3), store.row_fingerprint(_r1))
print("   THE SABOTAGE (rule 19): change what the row SAYS and it must")
print("   stop matching, or the fingerprint would fold real events together")
check("a different text is a different fingerprint",
      store.row_fingerprint(_ph_row("2026-08-01T10:00:00", "ONE"))
      == store.row_fingerprint(_r1), False)
check("so is a different stamp",
      store.row_fingerprint(_ph_row("2026-08-01T10:00:09", "one"))
      == store.row_fingerprint(_r1), False)
check("so is a different level",
      store.row_fingerprint(dict(_r1, level="warn"))
      == store.row_fingerprint(_r1), False)
check("a row that is not a dict has no identity, and does not raise",
      store.row_fingerprint("not a row"), "")

print("   the inventory reads and writes nothing - it is what later shows a")
print("   person WHICH paths a folder carries, before anybody is asked")
_scan = store.scan_project_history(PH)
check("both days found", [d["day"] for d in _scan["days"]],
      ["2026-08-01", "2026-08-02"])
check("and all three rows", _scan["rows"], 3)
check("all of them under the other machine's path",
      _scan["paths"].get(OLDP), 3)
check("a folder with no bridge-logs answers empty rather than raising",
      store.scan_project_history(os.path.join(TMP, "nothing-here"))["rows"], 0)
check("and so does no project at all",
      store.scan_project_history("")["rows"], 0)


print("\n100. bringing that history in: once, re-keyed, and never pathless")
print("    step 2. Three properties, and a person can check each: the merge")
print("    is idempotent, it re-keys onto this machine's path, and it")
print("    refuses a row that names no project at all")
_ph_target = os.path.join(store.LOGS, "2026-08-01", "events.jsonl")
_m1 = store.merge_day(PH, "2026-08-01")
check("both rows of that day came in", (_m1["added"], _m1["read"]), (2, 2))
check("and both were written on another machine", _m1["rekeyed"], 2)
_h1 = _hl.sha256(io.open(_ph_target, "rb").read()).hexdigest()
_m2 = store.merge_day(PH, "2026-08-01")
_h2 = _hl.sha256(io.open(_ph_target, "rb").read()).hexdigest()
check("a second merge adds nothing", _m2["added"], 0)
check("and the file is byte-identical", _h1, _h2)
check("it says what it already had", _m2["already"], 2)

print("   re-keyed, because the feed filter is an exact match: without this")
print("   the history would sit in the file and appear in no feed at all.")
print("   Checked on TODAY's day folder, because recent_events reads today")
print("   and yesterday only - a carried day older than that lands in the")
print("   right file and is still outside the feed's two-day window. That",)
print("   is a property of the feed, not of the merge, and it is written")
print("   down here so nobody reads the merge as broken")
_ph_today = time.strftime("%Y-%m-%d")
os.makedirs(os.path.join(PH, "bridge-logs", _ph_today), exist_ok=True)
_ph_write(_ph_today, [_ph_row(_ph_today + "T10:00:00", "carried-one"),
                      _ph_row(_ph_today + "T10:00:01", "carried-two")])
_mt = store.merge_day(PH, _ph_today)
check("today's carried day comes in too", _mt["added"], 2)
_feed = store.recent_events(200, project=PH)
_mine = [r for r in _feed if r.get("text") in ("carried-one", "carried-two")]
check("the carried rows are in this project's feed", len(_mine), 2)
check("under this machine's path", _mine[0].get("path"), daemon.norm(PH))
check("and the original is kept, so a wrong import is reversible",
      _mine[0].get("path_was"), OLDP)
print("   and the older day really is on disk, just outside the window")
_old_rows = store._read_events(_ph_target)
check("2026-08-01 sits in its own file, re-keyed",
      len([r for r in _old_rows if r.get("path") == daemon.norm(PH)]), 2)

print("   a pathless row passes EVERY project's filter (_feed_rows), so one")
print("   import of one could flood every pair's feed at once")
_ph_write("2026-08-03", [_ph_row("2026-08-03T09:00:00", "belongs to nobody",
                                 path=""),
                         _ph_row("2026-08-03T09:00:01", "four")])
_m3 = store.merge_day(PH, "2026-08-03")
check("the pathless one is refused", _m3["no_path"], 1)
check("counted, not silently dropped", _m3["added"], 1)
check("and it is nowhere in the journal it was refused from",
      any(r.get("text") == "belongs to nobody" for r in store._read_events(
          os.path.join(store.LOGS, "2026-08-03", "events.jsonl"))), False)

print("   the carrier is READ-ONLY - the bridge does not write into the")
print("   folder it is reading, or the two copies would drift")
check("nothing was added to the project's own day",
      sorted(os.listdir(os.path.join(PH, "bridge-logs", "2026-08-01"))),
      ["events.jsonl"])

print("   edges never raise: this runs on the startup path, so a broken")
print("   line costs a line and a broken day costs a day, never a boot")
_bad = os.path.join(PH, "bridge-logs", "2026-08-04")
os.makedirs(_bad, exist_ok=True)
with io.open(os.path.join(_bad, "events.jsonl"), "w", encoding="utf-8") as fh:
    fh.write("{not json at all\n")
    fh.write(_json.dumps(_ph_row("2026-08-04T08:00:00", "five")) + "\n")
_m4 = store.merge_day(PH, "2026-08-04")
check("the good line survives the broken one", _m4["added"], 1)
check("and nothing was raised", _m4["error"], "")
check("a day that is not there answers empty",
      store.merge_day(PH, "1999-01-01")["added"], 0)

print("   and the whole folder in one call, which is what the daemon uses")
_all = store.merge_project_history(PH)
check("it walks every day the folder carries", _all["days"], 5)
print("   2026-08-02 was never merged on its own above, so the first pass")
print("   over the whole folder is the one that brings it in")
check("and picks up what was still outstanding", _all["added"], 1)
_all2 = store.merge_project_history(PH)
check("the pass after that adds nothing at all", _all2["added"], 0)
print("   seven: 2 + 1 + 1 + 1 + 2 across the five days - the pathless row",)
print("   and the unparseable line are not among them, by construction")
check("saying it already had every one", _all2["already"], 7)

print("\n101. a calibration key is a path, and paths fold their case")
print("     store.calib_key was the one comparison left in the package that")
print("     folded a Windows path with normpath and no normcase. So")
print("     one spelling and another were two entries for one")
print("     folder: a session measured under one spelling read under the")
print("     other and arrived at a window with no measurements at all")
CK_A = "C:" + os.sep + "Projects" + os.sep + "Game"
CK_B = "c:" + os.sep + "projects" + os.sep + "game"
check("the two spellings are one key now",
      store.calib_key("Opus 5", CK_A), store.calib_key("opus 5", CK_B))
check("and the key is the canonical path",
      store.calib_key("opus 5", CK_A).split("|", 1)[1], daemon.norm(CK_A))

print("   what is already on disk is folded at startup, like the other")
print("   migrations - and a collision keeps the entry that has actually")
print("   measured something, because samples are the whole value of one")
_cal101 = store.load_calibration()
_cal101["opus 5|" + CK_A] = {"ceiling_pct": 90.0, "compact_samples": [1, 2, 3]}
_cal101["opus 5|" + CK_B] = {"ceiling_pct": 50.0, "compact_samples": []}
store.save_calibration(_cal101)
_folded = store.migrate_calib_keys()
_cal101 = store.load_calibration()
check("both spellings folded into one", _folded >= 1, True)
check("only the canonical key is left",
      ("opus 5|" + CK_A) in _cal101, False)
check("and the entry with the samples is the one kept",
      (_cal101.get(store.calib_key("opus 5", CK_A)) or {})
      .get("compact_samples"), [1, 2, 3])
check("running it again moves nothing", store.migrate_calib_keys(), 0)

print("   bringing a calibration across from the path a project used to")
print("   have. Rule 33 decides the collision, and it decides it the")
print("   OPPOSITE way to STATE: a measurement made on this machine is")
print("   never replaced by one carried from another, however rich")
CM_OLD = daemon.norm("e:" + os.sep + "oldbox" + os.sep + "thing")
CM_NEW = daemon.norm(os.path.join(TMP, "thing-here"))
store.calib_update("opus 5", CM_OLD, ceiling_pct=96.6,
                   compact_at_tokens=994509,
                   compact_samples=[999595, 998685, 994509],
                   how="PreCompact fired")
store.calib_update("opus 5", CM_NEW, ceiling_pct=93.7,
                   compact_samples=[701000], how="PreCompact fired")
_r101 = store.calib_move(CM_OLD, CM_NEW)
_cal101 = store.load_calibration()
check("the local measurement is kept", _r101["kept_local"], 1)
check("nothing was adopted over it", _r101["moved"], 0)
check("and it is still the local numbers on the key",
      (_cal101[store.calib_key("opus 5", CM_NEW)] or {})["compact_samples"],
      [701000])
check("the old key is gone either way - nothing is left to rot",
      store.calib_key("opus 5", CM_OLD) in _cal101, False)

print("   THE SABOTAGE (rule 19): take the local measurement away and the")
print("   carried one MUST be adopted - an entry that has measured nothing")
print("   is not evidence, and 'initial estimate' is not a reading")
store.calib_update("opus 5", CM_OLD, ceiling_pct=96.6,
                   compact_at_tokens=994509,
                   compact_samples=[999595, 998685, 994509],
                   how="PreCompact fired")
_cal101 = store.load_calibration()
_cal101[store.calib_key("opus 5", CM_NEW)] = {"ceiling_pct": 93.7,
                                              "how": "initial estimate",
                                              "compact_samples": []}
store.save_calibration(_cal101)
_r101b = store.calib_move(CM_OLD, CM_NEW)
_cal101 = store.load_calibration()
_e101 = _cal101[store.calib_key("opus 5", CM_NEW)]
check("this time it is adopted", _r101b["moved"], 1)
check("with the measurement intact", _e101["compact_at_tokens"], 994509)
print("   and MARKED, so nothing downstream mistakes a figure that")
print("   travelled for one measured here (rule 33)")
check("where it came from is on the entry", _e101.get("carried_from"), CM_OLD)
check("when it was brought across too", bool(_e101.get("carried_at")), True)
check("and `how` says it in words a person reads",
      "measured on another machine" in _e101.get("how", ""), True)

print("   the same mark lets compaction_survivable be honest: a carried")
print("   success is real evidence and is used while it is all there is,")
print("   and steps aside the moment this machine measures the pair itself")
CS = daemon.norm(os.path.join(TMP, "survivable-here"))
with daemon._lock:
    daemon.STATE.setdefault("compactions", {})["%s|executor" % CS] = [
        {"tokens": 999000, "after": 120000, "session": "old",
         "carried_from": "e:" + os.sep + "elsewhere"}]
    daemon.save_state()
check("the carried success is used when it is all there is",
      daemon.compaction_survivable(CS, "executor"), 999000)
with daemon._lock:
    daemon.STATE["compactions"]["%s|executor" % CS].append(
        {"tokens": 700000, "after": 90000, "session": "here"})
    daemon.save_state()
check("a local measurement takes over, even a smaller one",
      daemon.compaction_survivable(CS, "executor"), 700000)


print("\n102. marks_missing compares a path, so it folds case too")
print("     Found 2026-08-28 while checking the new machine: the settings")
print("     carry one capitalisation of the source folder, the folder on")
print("     disk carries another, and this was the only comparison")
print("     left doing it raw. ensure_marks WARNS and re-runs install on")
print("     the answer, so a false one costs a warning and an install at")
print("     every single launch, for ever")
_mp102 = os.path.join(TMP, "marks-case")
os.makedirs(os.path.join(_mp102, ".claude"), exist_ok=True)
_install.install(_mp102, python=sys.executable, statusline=False)
_sp102 = os.path.join(_mp102, ".claude", "settings.json")
with io.open(_sp102, encoding="utf-8") as _fh:
    _cfg102 = _json.load(_fh)
def _pypath102():
    """Only the PYTHONPATH complaint - the fixture installs without a
    status line, so the rest of the list is legitimately not empty."""
    return [m for m in _install.marks_missing(_mp102)
            if "PYTHONPATH" in m]


check("a freshly installed project reads right", _pypath102(), [])
print("   now spell the very same folder differently, as another machine's")
print("   settings do after a move")
_cfg102["env"]["PYTHONPATH"] = _install.ROOT.upper()
with io.open(_sp102, "w", encoding="utf-8") as _fh:
    _json.dump(_cfg102, _fh)
check("the same folder in another case is still the same folder",
      _pypath102(), [])
print("   THE SABOTAGE (rule 19): a genuinely different folder must still")
print("   be reported, or the check would have stopped checking")
_cfg102["env"]["PYTHONPATH"] = os.path.join(_install.ROOT, "somewhere-else")
with io.open(_sp102, "w", encoding="utf-8") as _fh:
    _json.dump(_cfg102, _fh)
check("a different path is named, with the file it is in",
      len(_pypath102()), 1)

print("\n102b. install is idempotent whatever spells the interpreter")
print("      2026-09-02, found by running install on a live project and")
print("      watching its eight bridge hooks become SIXTEEN. That project's")
print("      hooks are written `\"command\": \"py\"`; install builds its own")
print("      entry with sys.executable, an absolute path to the same")
print("      interpreter. `already_there` matched on command AND args and")
print("      so called it absent, while `marks_missing` four lines down")
print("      matched on args alone and called it present - two readers of")
print("      one fact, free to disagree, and they did. Every event would")
print("      then fire twice: two Stop events a turn, two status posts, two")
print("      PreCompact samples. -> DECISIONS.md 8.4")
_ip = os.path.join(TMP, "install-idem")
os.makedirs(os.path.join(_ip, ".claude"), exist_ok=True)
_isp = os.path.join(_ip, ".claude", "settings.json")


def _hookcount():
    with io.open(_isp, encoding="utf-8") as _fh:
        cfg = _json.load(_fh)
    return sum(1 for groups in (cfg.get("hooks") or {}).values()
               for g in groups for h in (g.get("hooks") or [])
               if list(h.get("args") or []) == [_install.HOOK_SCRIPT])


_install.install(_ip, python=sys.executable, statusline=False)
_n1 = _hookcount()
check("a fresh install writes one hook per event", _n1, len(_install.EVENTS))
_install.install(_ip, python=sys.executable, statusline=False)
check("installing again adds none", _hookcount(), _n1)

print("   now the shape that broke it: the SAME hooks, with the")
print("   interpreter written the way a person writes it")
with io.open(_isp, encoding="utf-8") as _fh:
    _icfg = _json.load(_fh)
for _groups in (_icfg.get("hooks") or {}).values():
    for _g in _groups:
        for _h in (_g.get("hooks") or []):
            if list(_h.get("args") or []) == [_install.HOOK_SCRIPT]:
                _h["command"] = "py"
with io.open(_isp, "w", encoding="utf-8") as _fh:
    _json.dump(_icfg, _fh)
win_check("marks_missing already read that correctly",
          [m for m in _install.marks_missing(_ip) if "hook" in m], [],
          "`py` is the Windows launcher; off Windows it is on no PATH, so the hook written that way would not run there")
_install.install(_ip, python=sys.executable, statusline=False)
check("and install no longer doubles them", _hookcount(), _n1)
win_check("leaving the owner's own spelling alone - `py` is what works "
          "on this machine, not a mistake to correct",
          sorted({_h.get("command")
                  for _groups in (_json.load(io.open(_isp, encoding="utf-8"))
                                  .get("hooks") or {}).values()
                  for _g in _groups for _h in (_g.get("hooks") or [])
                  if list(_h.get("args") or []) == [
                      _install.HOOK_SCRIPT]}),
          ["py"], "`py` is the Windows launcher; off Windows it is on no PATH, so the hook written that way would not run there")

print("   THE SABOTAGE (rule 19): a hook that is NOT ours, with the same")
print("   command, must still be added rather than mistaken for ours")
with io.open(_isp, encoding="utf-8") as _fh:
    _icfg = _json.load(_fh)
_icfg["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "py",
                                      "args": ["-m", "somebody.else"]}]}]
with io.open(_isp, "w", encoding="utf-8") as _fh:
    _json.dump(_icfg, _fh)
_install.install(_ip, python=sys.executable, statusline=False)
with io.open(_isp, encoding="utf-8") as _fh:
    _after = _json.load(_fh)
_stop = [h for g in _after["hooks"]["Stop"] for h in (g.get("hooks") or [])]
check("somebody else's hook survives and ours joins it",
      (len(_stop), sorted(str(h.get("args")) for h in _stop)),
      (2, sorted([str([_install.HOOK_SCRIPT]),
                  "['-m', 'somebody.else']"])))

print("\n103. the polite stop reaches a console application")
print("    relayout.stop_daemon used to try `taskkill /PID` and nothing")
print("    else before the force. That cannot reach a console app at all:")
print("    taskkill posts WM_CLOSE to windows owned by the TARGET's own")
print("    threads, and a console window belongs to whoever created the")
print("    console - cmd.exe, for a double-clicked bridge.bat. So the")
print("    branch failed on every run, silently, and the only symptoms")
print("    were a 45s pause and a 'recovered' banner nobody could account")
print("    for. THE RETURN VALUE WAS THE SAME EITHER WAY, so a check on")
print("    what stop_daemon answers could never have caught it (rule 19).")
print("    This drives the real function against a real console built the")
print("    real way, and asks the STUB whether its handler ran.")

if os.name != "nt":
    print("   not Windows: there is no console window here, so this case")
    print("   has nothing to say. Not counted as passing.")
else:
    import socket as _sk103
    from bridgecore import relayout as _rl103

    _d103 = os.path.join(TMP, "stopwin")
    os.makedirs(_d103, exist_ok=True)

    # Stands in for the daemon in the one respect that matters: it holds a
    # port so pid_on_port can find it, and it has a console control handler
    # that leaves a trace ON DISK. The trace is the witness, and it is
    # independent of the event (rule 30) - stop_daemon cannot write it, and
    # a /F kill cannot cause it, because /F never runs a handler.
    _STUB103 = (
        "import ctypes, os, socket, sys, time\n"
        "mark, port = sys.argv[1], int(sys.argv[2])\n"
        "srv = socket.socket()\n"
        "srv.bind(('127.0.0.1', port))\n"
        "srv.listen(5)\n"
        "R = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)\n"
        "def _ctrl(ev):\n"
        "    if ev in (2, 5, 6):\n"
        "        fh = open(mark, 'w')\n"
        "        fh.write('handler ran on event %d' % ev)\n"
        "        fh.close()\n"
        "        os._exit(0)\n"
        "    return False\n"
        "_ref = R(_ctrl)\n"
        "ctypes.windll.kernel32.SetConsoleCtrlHandler(_ref, True)\n"
        "fh = open(mark + '.up', 'w')\n"
        "fh.write(str(os.getpid()))\n"
        "fh.close()\n"
        "time.sleep(120)\n")
    _sp103 = os.path.join(_d103, "stub.py")
    with open(_sp103, "w") as _fh:
        _fh.write(_STUB103)

    def _freeport103():
        s = _sk103.socket()
        s.bind(("127.0.0.1", 0))
        n = s.getsockname()[1]
        s.close()
        return n

    def _launch103(mark, port):
        """cmd.exe -> python.exe in ONE console, the real geometry.

        Through a .bat on purpose: that is what puts cmd.exe in the console
        and leaves python owning no window, which is the whole condition
        being tested. Born minimised and without focus (rule 29) - the
        console is created for cmd.exe with this STARTUPINFO and the python
        child inherits it, so nothing is drawn for anybody to see.
        """
        bat = os.path.join(_d103, "run-%d.bat" % port)
        with open(bat, "w") as fh:
            fh.write('@echo off\r\n"%s" "%s" "%s" %d\r\n'
                     % (sys.executable, _sp103, mark, port))
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                      # SW_SHOWMINNOACTIVE
        pr = subprocess.Popen(["cmd", "/c", bat], startupinfo=si,
                              creationflags=subprocess.CREATE_NEW_CONSOLE,
                              close_fds=True)
        for _ in range(120):
            if os.path.exists(mark + ".up") and _rl103.port_open(port):
                break
            time.sleep(0.25)
        return pr

    _port103 = _freeport103()
    _mark103 = os.path.join(_d103, "closed.txt")
    _proc103 = _launch103(_mark103, _port103)
    _pid103 = _rl103.pid_on_port(_port103)
    check("the stub holds its port and is the process on it",
          _pid103 is not None and _rl103.process_alive(_pid103), True)
    print("   listening on %d as pid %s, launched by cmd.exe %d"
          % (_port103, _pid103, _proc103.pid))

    print("   THE SABOTAGE (rule 19): the old body's whole polite branch,")
    print("   run on its own against this stub. It must NOT stop it, or")
    print("   this case could not tell the two bodies apart.")
    _tk103 = subprocess.run(["taskkill", "/PID", str(_pid103)],
                            capture_output=True, text=True,
                            errors="replace")
    time.sleep(2.0)
    check("taskkill /PID alone refuses a console application",
          _tk103.returncode != 0, True)
    check("no handler ran, so nothing would have been written cleanly",
          os.path.exists(_mark103), False)
    check("and it is still up - this is the 45s the old body then sat out",
          _rl103.process_alive(_pid103), True)

    print("   now the real function, on that same live stub")
    _t0103 = time.time()
    _ok103, _why103 = _rl103.stop_daemon(_port103, timeout=20)
    _took103 = time.time() - _t0103
    print("   stop_daemon took %.1fs and said: %s" % (_took103, _why103))
    check("it reports the stop", _ok103, True)
    _txt103 = ""
    if os.path.exists(_mark103):
        with open(_mark103) as _fh:
            _txt103 = _fh.read()
    check("THE CONSOLE HANDLER RAN - the stub's own word for it",
          _txt103, "handler ran on event 2")
    check("it did not have to be killed", "killed" in _why103, False)
    # The first green run of this case still had close_console returning
    # False on a success: the helper was attached to the console it closed,
    # so Windows took it down and its exit code was never 0. stop_daemon
    # then said "no console window" and "closed console window 3802802" in
    # one sentence and ran the taskkill fallback for nothing. Every check
    # above passed through that, because the stop DID happen - so the
    # reason it gives is pinned too, or the same contradiction comes back
    # silently. It came back once anyway, from a race rather than a missing
    # line, on a loaded machine (2026-09-26) - the held run below is the
    # gate for that one. "taskkill" at all, not one wording of it.
    check("and the reason names the console close, not the fallback",
          ("closed console window" in _why103,
           "taskkill" in _why103), (True, False))
    check("nor did it sit out the polite wait", _took103 < 15, True)
    check("the stub is gone", _rl103.process_alive(_pid103), False)
    check("and so is the cmd.exe that owned the window (rule 9)",
          _rl103.process_alive(_proc103.pid), False)

    def _handler_ran103(mark):
        for _ in range(40):
            if os.path.exists(mark):
                break
            time.sleep(0.25)
        if not os.path.exists(mark):
            return ""
        with open(mark) as _fh:
            return _fh.read()

    print("   THE RACE, FORCED (2026-09-26). Every process attached to a")
    print("   console is ended by the close event when its window closes,")
    print("   and the helper posted the close while still attached. On a")
    print("   loaded machine the event won: the planner's check read 'no")
    print("   answer from the helper; taskkill instead' of a close that had")
    print("   worked. Load is not a fixture, so the REAL helper is held one")
    print("   second after its post - then only the order of its lines")
    print("   decides whether it lives to answer.")
    _src103 = _rl103._CLOSE_CONSOLE_SRC
    _anchor103 = "    raise SystemExit(5)\n"
    check("the post has its one failure exit to hold the helper after",
          _src103.count(_anchor103), 1)
    _port103h = _freeport103()
    _mark103h = os.path.join(_d103, "closed-held.txt")
    _proc103h = _launch103(_mark103h, _port103h)
    _pid103h = _rl103.pid_on_port(_port103h)
    _rl103._CLOSE_CONSOLE_SRC = _src103.replace(
        _anchor103, _anchor103 + "import time\ntime.sleep(1.0)\n")
    try:
        _okh103, _whyh103 = (_rl103.close_console(_pid103h) if _pid103h
                             else (None, "the stub never came up"))
    finally:
        _rl103._CLOSE_CONSOLE_SRC = _src103
    print("   held after the post, the helper said: %s" % _whyh103)
    check("the helper outlives the close it asked for, and says so",
          (_okh103, "closed console window" in _whyh103), (True, True))
    check("and the close it reports is real - the stub's handler ran",
          _handler_ran103(_mark103h), "handler ran on event 2")

    print("   and a close that does NOT confirm itself is reported as")
    print("   exactly that. The helper here is the old order, held: it posts")
    print("   while attached and is ended by its own close - the planner's")
    print("   run, made certain. stop_daemon used to call that 'no console")
    print("   window ...; taskkill instead', and both halves were false.")
    _OLD103 = (
        "import ctypes, sys, time\n"
        "pid = int(sys.argv[1])\n"
        "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "u = ctypes.WinDLL('user32', use_last_error=True)\n"
        "k.GetConsoleWindow.restype = ctypes.c_void_p\n"
        "u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,\n"
        "                           ctypes.c_void_p, ctypes.c_void_p]\n"
        "k.FreeConsole()\n"
        "k.AttachConsole(pid)\n"
        "u.PostMessageW(ctypes.c_void_p(k.GetConsoleWindow()), 0x0010,\n"
        "               None, None)\n"
        "time.sleep(5)\n"
        "print('closed console window of pid %d' % pid)\n")
    _port103o = _freeport103()
    _mark103o = os.path.join(_d103, "closed-old.txt")
    _proc103o = _launch103(_mark103o, _port103o)
    _pid103o = _rl103.pid_on_port(_port103o)
    _rl103._CLOSE_CONSOLE_SRC = _OLD103
    try:
        _oko103, _whyo103 = (_rl103.stop_daemon(_port103o, timeout=20)
                             if _pid103o else (None, "the stub never came up"))
    finally:
        _rl103._CLOSE_CONSOLE_SRC = _src103
    print("   stop_daemon said: %s" % _whyo103)
    check("the stop is reported - it happened",
          _oko103, True)
    check("the close really landed - the stub's handler ran",
          _handler_ran103(_mark103o), "handler ran on event 2")
    check("the answer carries the helper's exit code, the close event's own",
          "exit code 0xC000013A" in _whyo103, True)
    check("it says the close did not confirm itself",
          "did not confirm itself" in _whyo103, True)
    check("it claims no missing window and no taskkill stop",
          ("no console window" in _whyo103, "taskkill instead" in _whyo103),
          (False, False))
    check("and says taskkill was not what stopped it",
          "so it was not what stopped it" in _whyo103, True)

    print("   close_console refuses what it cannot reach, rather than")
    print("   reporting a stop that did not happen")
    _okx103, _whyx103 = _rl103.close_console(_pid103)
    check("a pid that is gone gets a refusal, not a success", _okx103, False)
    check("and the refusal says which pid", str(_pid103) in _whyx103, True)

    _all103 = [x for x in (_proc103.pid, _pid103, _proc103h.pid, _pid103h,
                           _proc103o.pid, _pid103o) if x]
    for _leftover in _all103:
        if _rl103.process_alive(_leftover):
            subprocess.run(["taskkill", "/PID", str(_leftover), "/F"],
                           capture_output=True, text=True, errors="replace")
    check("nothing from this case is left running",
          [x for x in _all103 if _rl103.process_alive(x)], [])

print("\n103b. the restart asks the daemon before it stops it")
print("     2026-09-02: the pair may restart the bridge itself now, and the")
print("     safety of that rests on nothing being lost - PENDING is in")
print("     memory, so a Stop hook blocked on a report is answered by")
print("     nobody once its daemon is gone. The rule said 'check first',")
print("     and the first restart under it was taken half a minute after")
print("     the check, with a whole report going out, being answered and")
print("     coming back inside the gap - six seconds clear. A precondition")
print("     kept by memory is not one (24), and a check separated from its")
print("     act is 5.17. The gate asks in the same call as the stop. -> 8.3")

import json                                                      # noqa: E402
import socket                                                    # noqa: E402
import threading                                                 # noqa: E402
from http.server import BaseHTTPRequestHandler                   # noqa: E402
from http.server import ThreadingHTTPServer                      # noqa: E402
from bridgecore import relayout as _rl103b                       # noqa: E402

_d103b = os.path.join(TMP, "gate")
os.makedirs(_d103b, exist_ok=True)
_STATE103B = {"pairs": {}}


class _Gate103B(BaseHTTPRequestHandler):
    """A stand-in daemon that answers /state and nothing else."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps(_STATE103B).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv103b = ThreadingHTTPServer(("127.0.0.1", 0), _Gate103B)
_port103b = _srv103b.server_address[1]
threading.Thread(target=_srv103b.serve_forever, daemon=True).start()

_QUIET103B = {"busy": {"reviewing": False, "verdict_in_flight": False,
                       "inflight": 0, "handover": False, "compacting": []}}
try:
    print("   a pair with a report waiting for a verdict")
    _STATE103B["pairs"] = {
        "proj-one": {"name": "One", "busy": dict(_QUIET103B["busy"],
                                                reviewing=True)},
        "proj-two": {"name": "Two", "busy": dict(_QUIET103B["busy"])}}
    _v, _lines = _rl103b.busy_now(_port103b)
    check("the gate calls that busy", _v, "busy")
    check("and names the pair and what would be lost",
          bool(_lines) and _lines[0].startswith("One:")
          and "loses it" in _lines[0], True)
    print("   the refusal reads: %s" % _lines[0])
    # stop_daemon is REPLACED for this call, and that is not tidiness. The
    # port here is held by this very Python process, so a run_now that gets
    # past the gate reaches `taskkill /F` on the suite itself - which is
    # exactly what the first sabotage run did: the output stops at this
    # case's header and nothing after it exists. A case that can kill the
    # run cannot report a failure, so the stop is stubbed and whether it was
    # REACHED becomes the witness - independent of the gate's own answer.
    _stopped103b = []
    _realstop103b = _rl103b.stop_daemon
    _rl103b.stop_daemon = lambda *_a, **_k: (_stopped103b.append(1),
                                             (False, "must not be reached"))[1]
    try:
        _r103b = _rl103b.run_now(_d103b, _port103b, out=lambda *_a: None)
    finally:
        _rl103b.stop_daemon = _realstop103b
    check("run_now refuses rather than stopping", _r103b.get("ok"), False)
    check("at the gate, before anything was stopped", _r103b.get("stage"),
          "gate")
    check("and the stop was never reached at all", _stopped103b, [])

    print("   the other four things that would be lost, each named")
    for _field, _needle in (("verdict_in_flight", "on its way to the"),
                            ("inflight", "still running"),
                            ("handover", "handover is under way")):
        _STATE103B["pairs"] = {"proj-one": {"name": "One", "busy": dict(
            _QUIET103B["busy"], **{_field: 2 if _field == "inflight"
                                   else True})}}
        _v2, _l2 = _rl103b.busy_now(_port103b)
        check("%s is a refusal, and says so" % _field,
              (_v2, _needle in " ".join(_l2)), ("busy", True))
    _STATE103B["pairs"] = {"proj-one": {"name": "One", "busy": dict(
        _QUIET103B["busy"], compacting=["executor"])}}
    _v3, _l3 = _rl103b.busy_now(_port103b)
    check("a session left alone to compact is a refusal too",
          (_v3, "left alone to compact" in " ".join(_l3)), ("busy", True))

    print("   the same daemon with nothing owed: it stops")
    _STATE103B["pairs"] = {
        "proj-one": {"name": "One", "busy": dict(_QUIET103B["busy"])},
        "proj-two": {"name": "Two", "busy": dict(_QUIET103B["busy"])}}
    check("the gate calls that quiet", _rl103b.busy_now(_port103b)[0], "quiet")

    print("   FAIL CLOSED, and the third answer is the honest one: a port")
    print("   nobody is serving is positive evidence that no daemon is")
    print("   running, and no Stop hook can block on a socket that refuses")
    print("   connections. A port that IS open and will not answer is the")
    print("   one that must not be read as 'nothing there'")
    _STATE103B["pairs"] = {"proj-one": {"name": "One"}}
    check("a daemon that predates the gate cannot be assumed quiet",
          _rl103b.busy_now(_port103b)[0], "unreachable")
    _free103b = socket.socket()
    _free103b.bind(("127.0.0.1", 0))
    _freeport = _free103b.getsockname()[1]
    _free103b.close()
    check("a port nobody is serving is 'no daemon', not a refusal",
          _rl103b.busy_now(_freeport)[0], "no daemon")

    print("   and --force is named in the refusal, because the case it is")
    print("   for - a daemon that is really dead - has to be reachable")
    _src103b = inspect.getsource(_rl103b.run_now)
    check("the refusal points at --force", "--force" in _src103b, True)
    check("and the gate runs in the same call as the stop, with nothing "
          "between", _src103b.index("wait_until_quiet")
          < _src103b.index("stop_daemon(port)"), True)
finally:
    _srv103b.shutdown()
    _srv103b.server_close()

print("\n104. a hint matches a WHOLE word, and a hyphen binds")
print("    Both text heuristics used a plain `in`. Word START was the first")
print("    repair and it was not enough: it cured three misfires and left")
print("    five, because a hint sits at a word start in every form that was")
print("    still wrong - an interrogative pronoun turned indefinite by a")
print("    hyphenated particle, a verb stem reaching the PAST TENSE as")
print("    readily as the imperative, and 'confirm' inside 'confirmed'.")
print("    Measured on a fixed corpus: word-start 8 false fires, whole-word")
print("    0, with missed asks 0 both times. THE LITERALS ARE NOT HERE -")
print("    this file is published and the public repository carries no")
print("    Cyrillic; the corpus is test_cases.py case 6.")

print("   THE SABOTAGE (rule 19): what word-start alone did, and still")
print("   would. If these two agreed there would be nothing to test.")


def _startonly104(text, needle):
    """The previous body, kept here as the control."""
    i = text.find(needle)
    while i != -1:
        before = text[i - 1] if i else ""
        if not (before.isalnum() or before == "_"):
            return True
        i = text.find(needle, i + 1)
    return False


check("word-start called 'confirmed' a request",
      _startonly104("all of it is confirmed", "confirm"), True)
check("whole-word does not",
      daemon.hint_hit("all of it is confirmed", ("confirm",)), False)
check("nor 'confirms'",
      daemon.hint_hit("the run confirms the fix", ("confirm",)), False)
check("but the word itself is still a request",
      daemon.hint_hit("please confirm before i proceed", ("confirm",)), True)

print("   a hyphen is part of the word, which is the half word-start could")
print("   not do: the hint is still at the start of a hyphenated form")
check("a hyphenated compound is not the bare word",
      daemon.hint_hit("some-thing happened", ("some",)), False)
check("word-start thought it was",
      _startonly104("some-thing happened", "some"), True)
check("and the bare word still matches",
      daemon.hint_hit("some thing happened", ("some",)), True)

print("   THE COST, pinned so it cannot be forgotten: a stem no longer")
print("   reaches its own endings, so a list must NAME the forms it means.")
print("   That is why the hint lists changed in the same commit, and why")
print("   there is a migration to carry them - see below.")
check("a stem no longer reaches its longer form",
      daemon.hint_hit("confirms the run", ("confirm",)), False)

print("   hint_hit on its own, so the contract is not only observed")
print("   through its two callers")
check("empty needles never match", daemon.hint_hit("anything", ()), False)
check("an empty needle is skipped, not a match everywhere",
      daemon.hint_hit("anything", ("",)), False)
check("a match at the very start of the text counts",
      daemon.hint_hit("confirm this", ("confirm",)), True)
check("a match at the very end counts too",
      daemon.hint_hit("please confirm", ("confirm",)), True)
check("a digit before it is inside a word",
      daemon.hint_hit("utf8confirm", ("confirm",)), False)
check("an underscore either side is inside a word",
      (daemon.hint_hit("_confirm", ("confirm",)),
       daemon.hint_hit("confirm_", ("confirm",))), (False, False))
check("punctuation either side is a boundary",
      daemon.hint_hit("(confirm)", ("confirm",)), True)
check("a later clean occurrence is found when the first is embedded",
      daemon.hint_hit("utf8confirm, then confirm", ("confirm",)), True)
check("a multi-word hint still works",
      daemon.hint_hit("let me know what to do", ("let me know",)), True)

print("   BOTH heuristics go through it, or the repair reaches one and the")
print("   other keeps the defect")
_oldq104 = daemon.CFG.get("question_hints")
_oldi104 = daemon.CFG.get("idle_hints")
daemon.CFG["question_hints"] = ["ready to ship"]
daemon.CFG["idle_hints"] = ["out of work"]
try:
    check("looks_like_a_question uses it",
          [daemon.looks_like_a_question([{"who": "assistant", "text": t}])
           for t in ("nearly ready to ship it", "unready to ship it")],
          [True, False])
    check("waiting_for_direction uses it",
          [daemon.waiting_for_direction([{"who": "assistant", "text": t}])
           for t in ("i am out of work here", "burnout of workflow")],
          [True, False])
finally:
    daemon.CFG["question_hints"] = _oldq104
    daemon.CFG["idle_hints"] = _oldi104

print("   `which` is GONE from the built-ins, and that is a removal, not a")
print("   narrowing: as a whole word it fires on every relative clause, and")
print("   a real 'which ...?' carries the question mark that is tested")
print("   before any hint. It caught nothing that was not already caught.")
# Asserted by BEHAVIOUR with the configured lists emptied, so only the
# built-ins are in play. A source-text check was written first and was
# wrong: the comment that records the removal contains the word, so the
# check went red on a correct build - the failure a scan invites.
_sq104b, _si104b = daemon.CFG.get("question_hints"), daemon.CFG.get("idle_hints")
daemon.CFG["question_hints"] = []
daemon.CFG["idle_hints"] = []
try:
    check("with only the built-ins left, a relative clause is not a question",
          daemon.looks_like_a_question([{"who": "assistant", "text":
                                         "the branch which the daemon reads"}]),
          False)
    check("and the built-ins ARE still in play, or that proved nothing",
          daemon.looks_like_a_question([{"who": "assistant", "text":
                                         "please confirm before i proceed"}]),
          True)
finally:
    daemon.CFG["question_hints"] = _sq104b
    daemon.CFG["idle_hints"] = _si104b
check("while a real one is still caught, by the question mark",
      daemon.looks_like_a_question([{"who": "assistant", "text":
                                     "which branch does it read?"}]), True)

print("\n104b. the lists travel WITH the matcher, or the repair makes")
print("     things worse")
print("    Whole-word matching plus the OLD list scores 1 false fire and")
print("    THREE MISSED questions - worse than either consistent state,")
print("    because nobody is told when the executor really is asking. The")
print("    lists live in config.json, which /config cannot write and a")
print("    running daemon serialises over, so a migration is the only way")
print("    the two arrive together.")
print("    The words themselves are NOT in daemon.py: they are the owner's")
print("    own language and this suite ships publicly, so they are read")
print("    from hints.local.json, which is packaged and never published.")

_sq104, _si104 = daemon.CFG.get("question_hints"), daemon.CFG.get("idle_hints")
if not any(daemon.HINTS_AS_PRESCRIBED.values()):
    print("   this checkout has no hints.local.json, so there is nothing to")
    print("   carry - and THAT is the contract being checked here, not a skip")
    check("both lists are empty, not half-loaded",
          (daemon.HINTS_AS_PRESCRIBED, daemon.HINTS_MEASURED),
          ({"question_hints": [], "idle_hints": []},
           {"question_hints": [], "idle_hints": []}))
    try:
        daemon.CFG["question_hints"] = []
        daemon.CFG["idle_hints"] = []
        check("an empty list is not carried onto an empty list",
              daemon.migrate_hint_lists(), [])
        daemon.CFG["question_hints"] = ["a list of my own"]
        check("nor is anything else touched", daemon.migrate_hint_lists(), [])
        check("exactly as it was", daemon.CFG["question_hints"],
              ["a list of my own"])
    finally:
        daemon.CFG["question_hints"] = _sq104
        daemon.CFG["idle_hints"] = _si104
else:
    print("   first the half nobody would notice failing: the file is on")
    print("   disk AND it was read. Empty lists have two causes - no file,")
    print("   which is the published build, and a file that would not parse,")
    print("   which is a fault - and until HINTS_PROBLEM they looked alike")
    check("the file is there and read, with nothing to report",
          (os.path.exists(daemon.HINTS_FILE), daemon.HINTS_PROBLEM),
          (True, ""))
    check("the two lists are not the same, or there would be nothing to carry",
          daemon.HINTS_AS_PRESCRIBED == daemon.HINTS_MEASURED, False)
    check("neither list is empty",
          (bool(daemon.HINTS_MEASURED["question_hints"]),
           bool(daemon.HINTS_MEASURED["idle_hints"])), (True, True))
    check("no word was dropped from the question list - it only grew",
          len(daemon.HINTS_MEASURED["question_hints"])
          >= len(daemon.HINTS_AS_PRESCRIBED["question_hints"]), True)
    print("   every measured entry must be reachable by the matcher that")
    print("   ships, or an entry would be dead on arrival and nothing would")
    print("   say so")
    for _k104 in ("question_hints", "idle_hints"):
        _dead104 = [h for h in daemon.HINTS_MEASURED[_k104]
                    if not daemon.hint_hit(h, (h,))]
        check("every %s entry matches itself" % _k104, _dead104, [])

    try:
        daemon.CFG["question_hints"] = list(
            daemon.HINTS_AS_PRESCRIBED["question_hints"])
        daemon.CFG["idle_hints"] = list(
            daemon.HINTS_AS_PRESCRIBED["idle_hints"])
        check("the prescribed lists are carried, both of them",
              sorted(daemon.migrate_hint_lists()),
              ["idle_hints", "question_hints"])
        check("and what is in place afterwards is the measured pair",
              (daemon.CFG["question_hints"], daemon.CFG["idle_hints"]),
              (daemon.HINTS_MEASURED["question_hints"],
               daemon.HINTS_MEASURED["idle_hints"]))
        check("running it again does nothing", daemon.migrate_hint_lists(), [])

        print("   EXACT match only, the same discipline as")
        print("   migrate_executor_mode: a list somebody has edited is not")
        print("   somebody else's to rewrite")
        daemon.CFG["question_hints"] = ["a list of my own"]
        daemon.CFG["idle_hints"] = ["and another"]
        check("an edited list is left alone", daemon.migrate_hint_lists(), [])
        check("exactly as it was", daemon.CFG["question_hints"],
              ["a list of my own"])
        print("   and one changed entry is still an edited list, not a match")
        _near104 = list(daemon.HINTS_AS_PRESCRIBED["question_hints"])
        _near104[0] = _near104[0] + "x"
        daemon.CFG["question_hints"] = _near104
        daemon.CFG["idle_hints"] = ["mine"]
        check("a near miss is not carried", daemon.migrate_hint_lists(), [])
    finally:
        daemon.CFG["question_hints"] = _sq104
        daemon.CFG["idle_hints"] = _si104

    print("   and the other checkout, reached here because it cannot be")
    print("   reached there: the public tree cannot run this suite to the")
    print("   end - it stops at the QUIET.md case, and QUIET.md is in")
    print("   make_public.NEVER on purpose - so the no-file branch would")
    print("   otherwise be written and never executed by anybody")
    _hf104 = daemon.HINTS_FILE
    _hp104, _hm104 = daemon.HINTS_AS_PRESCRIBED, daemon.HINTS_MEASURED
    try:
        daemon.HINTS_FILE = os.path.join(TMP, "there-is-no-such-file.json")
        _a104, _b104 = daemon._load_hint_lists()
        check("with no file both halves are empty, not half-loaded",
              (_a104, _b104),
              ({"question_hints": [], "idle_hints": []},
               {"question_hints": [], "idle_hints": []}))
        daemon.HINTS_AS_PRESCRIBED, daemon.HINTS_MEASURED = _a104, _b104
        daemon.CFG["question_hints"] = []
        daemon.CFG["idle_hints"] = []
        check("an empty list is not carried onto an empty list",
              daemon.migrate_hint_lists(), [])
        daemon.CFG["question_hints"] = ["a list of my own"]
        check("and nothing else is touched either",
              daemon.migrate_hint_lists(), [])
        check("exactly as it was", daemon.CFG["question_hints"],
              ["a list of my own"])

        print("   and the case that must NOT look like the one above: a file")
        print("   that is there and will not parse. Same empty lists, but it")
        print("   has to say so, or the words stop working in silence")
        _bad104 = os.path.join(TMP, "hints-broken.json")
        with open(_bad104, "w", encoding="utf-8") as _fh104:
            _fh104.write("{ this is not json")
        daemon.HINTS_FILE = _bad104
        _c104, _d104 = daemon._load_hint_lists()
        check("a broken file still yields empty lists",
              (_c104, _d104),
              ({"question_hints": [], "idle_hints": []},
               {"question_hints": [], "idle_hints": []}))
        check("but it is NOT silent about it",
              bool(daemon.HINTS_PROBLEM), True)
        check("and it names the file, so the sentence is actionable",
              _bad104 in daemon.HINTS_PROBLEM, True)
        print("   the two causes must be distinguishable, which is the whole")
        print("   point - a missing file reports nothing at all")
        daemon.HINTS_FILE = os.path.join(TMP, "there-is-no-such-file.json")
        daemon._load_hint_lists()
        check("a missing file has nothing to report", daemon.HINTS_PROBLEM, "")
        print("   half a file counts as broken too: the keys are named")
        with open(_bad104, "w", encoding="utf-8") as _fh104:
            _fh104.write('{"prescribed": {"question_hints": ["x"]}}')
        daemon.HINTS_FILE = _bad104
        daemon._load_hint_lists()
        check("a half-written file is reported", bool(daemon.HINTS_PROBLEM),
              True)
    finally:
        daemon.HINTS_PROBLEM = ""
        daemon.HINTS_FILE = _hf104
        daemon.HINTS_AS_PRESCRIBED, daemon.HINTS_MEASURED = _hp104, _hm104
        daemon.CFG["question_hints"] = _sq104
        daemon.CFG["idle_hints"] = _si104

print("\n105. the planner is not told to do what it is already doing")
print("     The branch fired 1.5 s after the planner's own verdict, to say")
print("     'you accepted iteration N, now give the executor work'. A")
print("     planner writing that task at human pace had no chance of")
print("     beating it. The price of a message is the size of the window")
print("     it lands in - about 720k for a planner - not the size of the")
print("     message, so each of those cost a full wake and moved nothing.")
print("     Measured over the whole journal, 960 firings: the task followed")
print("     the verdict after a median of 21 s and a p75 of 40 s, and at")
print("     60 s 787 of the 960 had already gone out.")

check("the wait is the measured one, not a round number",
      daemon.NUDGE_AFTER_VERDICT_SEC, 60)

_p105 = daemon.norm(PATH)
_sent105 = []
_realdel105 = daemon.deliver
daemon.deliver = lambda path, role, body, meta: (
    _sent105.append((role, meta.get("kind"), body[:40])) or True)
_lt105 = daemon.STATE.get("last_task")
try:
    print("   the ordinary case: the task went out during the wait")
    _t0 = time.time()
    daemon.STATE["last_task"] = {_p105: _t0 + 5}
    daemon.nudge_for_task(PATH, 42, _t0)
    check("nothing is sent, because there is nothing to say", _sent105, [])

    print("   THE CASE THE BRANCH EXISTS FOR - it must still fire, or this")
    print("   is a saving bought by breaking the thing it was saving on")
    del _sent105[:]
    daemon.STATE["last_task"] = {_p105: _t0 - 600}
    daemon.nudge_for_task(PATH, 42, _t0)
    check("a planner that accepted and went quiet is still told",
          [(r, k) for r, k, _b in _sent105], [("planner", "info")])
    check("and it is told which iteration, so it is actionable",
          "iteration 42" in (_sent105[0][2] if _sent105 else ""), True)

    print("   no record at all is the same as silence: a pair whose task")
    print("   history the bridge has lost is not one to leave standing")
    del _sent105[:]
    daemon.STATE["last_task"] = {}
    daemon.nudge_for_task(PATH, 42, _t0)
    check("with no record it errs towards telling", len(_sent105), 1)

    print("   and it may never take a pair down with it: a timer thread")
    print("   that raises takes its message with it and says nothing")
    del _sent105[:]
    daemon.STATE["last_task"] = "not a dict at all"
    daemon.nudge_for_task(PATH, 42, _t0)
    check("a broken record is survived, not raised", _sent105, [])
finally:
    daemon.deliver = _realdel105
    if _lt105 is None:
        daemon.STATE.pop("last_task", None)
    else:
        daemon.STATE["last_task"] = _lt105

print("\n106. the live data folder has ONE writer, and it is the daemon")
print("     2026-09-13 00:31:48: a measurement script imported the package")
print("     with BRIDGE_DATA on the live folder, ran run_check(all) and")
print("     saved the STATE it had loaded at 00:17:35 over the daemon's")
print("     state.json - and wrote 'planner_check ... passed' into the live")
print("     journal under the planner's name, when the planner had run")
print("     nothing. The daemon's clean stop at 00:32:20 wrote its memory")
print("     back, so nothing was lost - by the order of events, not by a")
print("     rule. The same class had been caught before; rule 25 says a")
print("     legal exception does not happen twice. -> DECISIONS.md 8.20")
_probe106 = r'''
import os, sys
sys.path.insert(0, %r)
from bridgecore import store
out = []
try:
    store.journal("probe", "a stranger writes")
    out.append("journal:allowed")
except store.SecondAuthority as e:
    out.append("journal:refused")
    out.append("names_data:%%s" %% (os.environ["BRIDGE_DATA"] in str(e)))
    out.append("names_way_out:%%s" %% ("temp" in str(e)))
try:
    store.save_state({"probe": 1})
    out.append("save:allowed")
except store.SecondAuthority:
    out.append("save:refused")
out.append("claimed:%%s" %% (store.second_authority() == ""))
store.claim_writer()
out.append("after_claim:%%s" %% (store.second_authority() == ""))
print(" ".join(out))
''' % os.path.dirname(os.path.abspath(__file__))


# THE CHILD HAS ITS OWN TEMP ROOT, so "live-shaped" does not depend on
# where this tree happens to lie. The planner's `check` copies the whole
# tree under tempfile.mkdtemp() and runs the suites from there; a folder
# built beside this file was then under the child's temp folder too,
# data_is_throwaway() answered True, the stranger was let in and five
# checks went red on the planner's run while staying green in source -
# "the result depended on who ran it" (2026-09-13 01:53). The gate was
# right; the case had defined "live" by the tree's address.
_tmp106 = os.path.join(TMP, "child-temp-106")
os.makedirs(_tmp106, exist_ok=True)     # gettempdir() wants an existing one


def _stranger106(data):
    env = dict(os.environ, BRIDGE_DATA=data, BRIDGE_NO_HOOKS="1",
               TMPDIR=_tmp106, TEMP=_tmp106, TMP=_tmp106)
    env.pop("BRIDGE_PORT", None)
    r = subprocess.run([sys.executable, "-c", _probe106], env=env,
                       capture_output=True, text=True, timeout=60)
    return (r.stdout.strip() + " " + r.stderr.strip()[-200:]).split()


# A folder that is NOT under the CHILD's temp and does not exist: the
# refusal comes before any write, so nothing is created in the tree.
_live106 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data-second-authority-probe-%d" % os.getpid())
_out106 = _stranger106(_live106)
check("a stranger on a live-shaped folder is refused the journal",
      "journal:refused" in _out106, True)
check("and refused save_state", "save:refused" in _out106, True)
check("the refusal names BRIDGE_DATA", "names_data:True" in _out106, True)
check("and the way out - a copy under the temp folder",
      "names_way_out:True" in _out106, True)
check("nothing was created in the tree by the refused writes",
      os.path.exists(_live106), False)
check("the same process, once it claims the folder, may write",
      "after_claim:True" in _out106, True)
_thr106 = os.path.join(_tmp106, "throwaway-data")
_out106b = _stranger106(_thr106)
check("a folder under temp is nobody's live state - allowed both ways",
      ("journal:allowed" in _out106b, "save:allowed" in _out106b),
      (True, True))
print("     and the daemon claims it in ONE place - main(), after the port")
print("     check that already stops two daemons serving one port")
_src106 = inspect.getsource(daemon.main)
check("main() claims the folder", "store.claim_writer()" in _src106, True)
check("after the port refusal, not before",
      _src106.find("store.claim_writer()")
      > _src106.find("Port %d is already being served"), True)
check("and nowhere else in the package",
      sum(1 for _f in ("daemon", "sessions", "channel", "hook", "relayout",
                       "install", "archive", "telegram", "statusline")
          for _l in inspect.getsource(
              __import__("bridgecore." + _f, fromlist=[_f])).splitlines()
          if "claim_writer()" in _l and not _l.strip().startswith("#")
          and "def claim_writer" not in _l), 1)

print("\n107. the planner's channel serves calls CONCURRENTLY - a check does")
print("     not hold a verdict behind it")
print("     2026-09-13, planner transcript + journal: check issued 00:33:20,")
print("     `continue` on report 337 issued 00:37:11 while it ran, the client")
print("     gave the verdict call up at 00:39:11 ('still running'), and the")
print("     daemon saw the verdict at 00:47:38 - the same second the check")
print("     returned (journal: planner_check, then verdict). channel.main()")
print("     read stdin one call at a time; `check` waits the whole")
print("     acceptance run (853 s). Real order: a channel PROCESS, its")
print("     JSON-RPC stdin, a stub daemon that sleeps in /check, and the")
print("     stub's own arrival log as the receiver. -> DECISIONS.md 8.21")
import json as _js107                                          # noqa: E402
import threading as _thr107                                    # noqa: E402
from http.server import BaseHTTPRequestHandler as _BH107       # noqa: E402
from http.server import ThreadingHTTPServer as _THS107         # noqa: E402



def _until107(fn, seconds=15.0):
    """Wait for the fact the check asserts (the rule under case 74)."""
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return bool(fn())


_log107 = []            # (time, "in"|"out", path) as the stub daemon saw it
_HOLD107 = 6.0          # how long the stub holds /check - seconds, not minutes


class _Stub107(_BH107):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        _log107.append((time.time(), "in", self.path))
        if self.path == "/check":
            time.sleep(_HOLD107)
            body = {"ok": True, "rows": [{"what": "py_compile", "exit": 0}],
                    "dir": "stub"}
        elif self.path == "/verdict":
            body = {"ok": True, "delivered": True}
        else:
            body = {"ok": True}
        _log107.append((time.time(), "out", self.path))
        data = _js107.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_srv107 = _THS107(("127.0.0.1", 0), _Stub107)
_port107 = _srv107.server_address[1]
_thr107.Thread(target=_srv107.serve_forever, daemon=True).start()
_proj107 = os.path.join(TMP, "channel-107")
os.makedirs(_proj107, exist_ok=True)
_env107 = dict(os.environ, BRIDGE_PORT=str(_port107), BRIDGE_ROLE="planner",
               BRIDGE_DATA=os.path.join(TMP, "channel-107-data"),
               BRIDGE_NO_HOOKS="1",
               PYTHONPATH=os.path.dirname(os.path.abspath(__file__)))
_ch107 = subprocess.Popen([sys.executable, "-m", "bridgecore.channel"],
                          cwd=_proj107, env=_env107, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
_replies107 = []        # (time, id) in the order the CLIENT side reads them


def _reader107():
    for raw in _ch107.stdout:
        try:
            row = _js107.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if "id" in row and ("result" in row or "error" in row):
            _replies107.append((time.time(), row["id"], row))


_thr107.Thread(target=_reader107, daemon=True).start()


def _send107(obj):
    _ch107.stdin.write((_js107.dumps(obj) + "\n").encode("utf-8"))
    _ch107.stdin.flush()


def _reply107(mid):
    return next((r for r in _replies107 if r[1] == mid), None)


try:
    _send107({"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}})
    check("the channel process answers initialize",
          _until107(lambda: _reply107(0) is not None, 20), True)
    _send107({"jsonrpc": "2.0", "method": "notifications/initialized"})
    _t0 = time.time()
    _send107({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "check", "arguments": {}}})
    check("PRECONDITION: the stub is holding /check",
          _until107(lambda: any(k == "in" and p == "/check"
                            for _t, k, p in _log107), 10), True)
    _send107({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "verdict",
                         "arguments": {"verdict": "continue",
                                       "feedback": "Checked: x"}}})
    check("the verdict's reply reaches the client side",
          _until107(lambda: _reply107(2) is not None, _HOLD107 + 20), True)
    check("and the check's reply, after its hold",
          _until107(lambda: _reply107(1) is not None, _HOLD107 + 20), True)
    _v_in = next((t for t, k, p in _log107 if k == "in" and p == "/verdict"),
                 None)
    _c_out = next((t for t, k, p in _log107 if k == "out" and p == "/check"),
                  None)
    check("THE RECEIVER: /verdict arrived at the daemon BEFORE /check returned",
          (_v_in is not None and _c_out is not None and _v_in < _c_out), True)
    check("by seconds, not by a race - the whole hold is in between",
          ((_c_out - _v_in) > _HOLD107 * 0.5)
          if (_v_in and _c_out) else False, True)
    check("the client side read the verdict's reply before the check's",
          [mid for _t, mid, _r in _replies107 if mid in (1, 2)], [2, 1])
    check("and the check's reply still carries the run's result",
          "CHECK PASSED" in _js107.dumps(_reply107(1)[2] if _reply107(1)
                                         else {}), True)
    check("the verdict was answered inside the hold, not after it",
          (_reply107(2)[0] - _t0) < _HOLD107 if _reply107(2) else False,
          True)
finally:
    try:
        _ch107.stdin.close()
    except Exception:
        pass
    try:
        _ch107.wait(10)
    except Exception:
        _ch107.kill()
    _srv107.shutdown()
    _srv107.server_close()

print("\n108. the journal keeps every line when threads write at once - the")
print("     central day file and the project's own mirror")
print("    2026-09-23: case 12 of test_multipair lost a quiet pair's line")
print("    outright - no row had landed while the window was read, the row")
print("    was simply not in the file. store.journal appended with")
print("    open(p, 'a') and no lock, and on Windows an append is 'seek to")
print("    the end, then write': two threads that reach the same end write")
print("    over each other. Measured through store.journal itself, 8 threads")
print("    x 1 000 lines lost 269 to 330 lines in EACH of the two files, run")
print("    after run. The live journal is written by every thread the daemon")
print("    has, and it is the witness half the rules in CLAUDE.md read.")
print("    -> DECISIONS.md 8.27")
import ast as _ast108                                      # noqa: E402
import threading as _th108                                 # noqa: E402
import json as _json108                                   # noqa: E402
_proj108 = os.path.join(TMP, "journal-threads")
os.makedirs(_proj108, exist_ok=True)
_T108, _N108 = 8, 1000
_tag108 = "j108-%d" % int(time.time())
_days108 = {store.day_dir()}


def _w108(t):
    for i in range(_N108):
        store.journal("probe", "%s %d %d" % (_tag108, t, i),
                      "journal-threads", "executor", "log",
                      project_dir=_proj108)


_ths108 = [_th108.Thread(target=_w108, args=(t,)) for t in range(_T108)]
for _t in _ths108:
    _t.start()
for _t in _ths108:
    _t.join()
_days108.add(store.day_dir())      # a run across midnight writes two days
# the project's copy is written by the project's own writer since 8.117,
# and a burst like this one is still on its way when the threads end:
# what is counted below is what was LOST, so it is counted once it is all
# written, never while it is under way
check("the project's writer finishes the burst",
      getattr(store, "flush_project_writes", lambda t: True)(120), True)


def _count108(paths):
    """(lines of this run that read back whole, lines that did not)."""
    good = bad = 0
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = _json108.loads(line)
                except Exception:
                    bad += 1           # two lines written over each other
                    continue
                if _tag108 in (row.get("text") or ""):
                    good += 1
    return good, bad


_c108 = _count108([os.path.join(d, "events.jsonl") for d in _days108])
_mirror108 = store.project_log_dir(_proj108) or os.path.join(
    _proj108, "bridge-logs", "none")
_m108 = _count108([os.path.join(os.path.dirname(_mirror108),
                                os.path.basename(d), "events.jsonl")
                   for d in _days108])
_all108 = _T108 * _N108
print("   written: %d threads x %d lines = %d" % (_T108, _N108, _all108))
print("   central day file: kept %d, LOST %d, unreadable %d"
      % (_c108[0], _all108 - _c108[0], _c108[1]))
print("   the project's mirror: kept %d, LOST %d, unreadable %d"
      % (_m108[0], _all108 - _m108[0], _m108[1]))
check("the central journal keeps every line eight threads wrote at once",
      _c108, (_all108, 0))
check("and so does the project's own mirror in its bridge-logs",
      _m108, (_all108, 0))

print("   census: the store appends in ONE place, and that place holds a")
print("   lock of the file's own - not _lock, the state's")
_src108 = read_or_fail(store.__file__, "store.py")
try:
    _tree108 = _ast108.parse(_src108)
except SyntaxError:
    _tree108 = _ast108.parse("")
_par108 = {}
for _n in _ast108.walk(_tree108):
    for _ch in _ast108.iter_child_nodes(_n):
        _par108[_ch] = _n


def _is_open108(c):
    f = c.func
    if isinstance(f, _ast108.Name):
        return f.id == "open"
    return (isinstance(f, _ast108.Attribute) and f.attr == "open"
            and getattr(f.value, "id", "") in ("io", "codecs"))


def _appends_at108(c):
    """An open() that appends - or whose mode cannot be read, which is
    counted too: a mode in a variable is how a bypass would hide."""
    if not (isinstance(c, _ast108.Call) and _is_open108(c)):
        return False
    mode = c.args[1] if len(c.args) > 1 else next(
        (k.value for k in c.keywords if k.arg == "mode"), None)
    if mode is None:
        return False
    if isinstance(mode, _ast108.Constant) and isinstance(mode.value, str):
        return mode.value.startswith("a")
    return True


def _owner108(node):
    p = _par108.get(node)
    while p is not None and not isinstance(p, _ast108.FunctionDef):
        p = _par108.get(p)
    return p.name if p is not None else "<module>"


def _under_lock108(node):
    """Inside `with _file_lock(...)`: the file's own lock (8.117)."""
    p = _par108.get(node)
    while p is not None and not isinstance(p, _ast108.FunctionDef):
        if isinstance(p, _ast108.With) and any(
                isinstance(i.context_expr, _ast108.Call)
                and getattr(i.context_expr.func, "id", "") == "_file_lock"
                for i in p.items):
            return True
        p = _par108.get(p)
    return False


_opens108 = [(_owner108(c), c.lineno, _under_lock108(c))
             for c in _ast108.walk(_tree108) if _appends_at108(c)]
_fns108 = {n.name: n for n in _ast108.walk(_tree108)
           if isinstance(n, _ast108.FunctionDef)}
_WRITERS108 = ("journal", "dialogue", "index_append", "merge_day")


def _calls108(name, callee):
    fn = _fns108.get(name)
    return fn is not None and any(
        isinstance(c, _ast108.Call) and getattr(c.func, "id", "") == callee
        for c in _ast108.walk(fn))


print("   every appending open() in store.py: %s" % (_opens108,))
_ap108 = [o for o in _opens108 if o[0] == "_append"]
check("store has one place that appends, _append, and every append in it "
      "is inside 'with _file_lock(path)'",
      (bool(_ap108), all(o[2] for o in _ap108)), (True, True))
# one lock PER FILE since 8.117: the race is two threads on one file, and a
# single lock for every file made one slow share everybody's wait
_fl108 = getattr(store, "_file_lock", None)
_a108 = os.path.join(TMP, "a108", "events.jsonl")
_b108 = os.path.join(TMP, "b108", "events.jsonl")
_la108 = _fl108(_a108) if _fl108 else None
check("_file_lock gives a file one lock whatever spelling arrives, another "
      "file another lock, and none of them is store._lock or daemon._lock",
      (_la108 is not None and hasattr(_la108, "acquire"),
       _fl108 is not None and _fl108(os.path.join(
           TMP, "a108", ".", "events.jsonl")) is _la108,
       _fl108 is not None and _fl108(_b108) is not _la108,
       _la108 is not getattr(store, "_lock", None),
       _la108 is not getattr(daemon, "_lock", None)),
      (True, True, True, True, True))
for _name108 in _WRITERS108:
    # a project's copy goes through the project's writer since 8.117,
    # which appends through _append in its turn (checked just below)
    check("%s appends only through _append or the project's writer"
          % _name108,
          ([o[1] for o in _opens108 if o[0] == _name108],
           _calls108(_name108, "_append")
           or _calls108(_name108, "_to_project")), ([], True))
check("and the project's writer appends only through _append",
      ([o[1] for o in _opens108 if o[0] == "_write_project_item"],
       _calls108("_write_project_item", "_append")), ([], True))
check("and nothing else in store.py opens a file to append",
      [o[:2] for o in _opens108
       if o[0] not in ("_append",) + _WRITERS108], [])


print("\n109. install merges: a file it has nothing to change in keeps every")
print("     byte, and a mark that works is left as it is written")
print("    2026-09-25: install run on copies of three live projects added")
print("    nothing and changed 6 of their 14 files - every .mcp.json and")
print("    settings.json spelled PYTHONPATH again in another capitalisation, one")
print("    lost its closing newline, and a watched project's working status")
print("    line 'py -m bridgecore.statusline' became an absolute path. Here:")
print("    the same shapes, synthetic, and install run twice over them.")
print("    -> DECISIONS.md 8.30")
import ast as _ast109                                      # noqa: E402
import inspect                                             # noqa: E402,F811
import json as _json                                       # noqa: E402,F811
import hashlib as _hl109                                   # noqa: E402
import shutil as _sh109                                    # noqa: E402
from bridgecore import install as _in109                   # noqa: E402

_home109 = os.path.join(TMP, "home109")
os.makedirs(os.path.join(_home109, ".claude"), exist_ok=True)
_cj109 = os.path.join(TMP, "claude109.json")
_env109 = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME",
                                          "BRIDGE_CLAUDE_JSON")}
os.environ["USERPROFILE"] = os.environ["HOME"] = _home109
os.environ["BRIDGE_CLAUDE_JSON"] = _cj109
_SK109 = []


def _sha109(p):
    return (_hl109.sha256(open(p, "rb").read()).hexdigest()
            if os.path.exists(p) else None)


def _put109(p, data, tail="", crlf=False, indent=2):
    """`tail` is written in LF; `crlf` turns every line ending, the tail's
    too, into CRLF."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    t = _json.dumps(data, ensure_ascii=False, indent=indent) + tail
    if crlf:
        t = t.replace("\n", "\r\n")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(t)


def _files109(proj):
    return [os.path.join(proj, ".claude", "settings.json"),
            os.path.join(proj, ".claude", "settings.local.json"),
            os.path.join(proj, ".mcp.json"),
            os.path.join(proj, ".gitignore"),
            os.path.join(_home109, ".claude", "settings.json"), _cj109]


try:
    # A working spelling of the interpreter that is NOT sys.executable -
    # the way a person writes it. `py` here; elsewhere the bare name, if the
    # PATH has it; otherwise the status-line check cannot be set up.
    _bare109 = next((n for n in ("py", os.path.basename(sys.executable))
                     if _sh109.which(n)), None)
    _root109 = (_in109.ROOT.upper() if os.name == "nt" else _in109.ROOT)

    print("   (a) a project in the shapes found live: install once to lay the")
    print("   marks, then written back the way a person or the client leaves")
    print("   them, then install again")
    _p109 = os.path.join(TMP, "merge109")
    os.makedirs(_p109, exist_ok=True)
    _put109(_cj109, {"projects": {}})
    _put109(os.path.join(_home109, ".claude", "settings.json"),
            {"theme": "dark", "enabledMcpjsonServers": ["bridge"]}, "\n")
    _in109.install(_p109, python=sys.executable)
    _s109 = os.path.join(_p109, ".claude", "settings.json")
    _cfg109 = _json.load(open(_s109, encoding="utf-8"))
    _cfg109["env"]["PYTHONPATH"] = _root109
    if _bare109:
        _cfg109["statusLine"]["command"] = ('%s "%s"' % (
            _bare109, _in109.STATUS_SCRIPT))
    else:
        _SK109.append("(a) status line")
    _put109(_s109, _cfg109)                                   # no newline
    _l109 = os.path.join(_p109, ".claude", "settings.local.json")
    _put109(_l109, {"permissions": {"allow": ["Bash(npm test:*)",
                                              "WebFetch(domain:example.org)"]},
                    "enabledMcpjsonServers": ["theirs", "bridge"]},
            "\n", crlf=True, indent=4)             # by hand: CRLF, indent 4
    _m109 = os.path.join(_p109, ".mcp.json")
    _mcp109 = _json.load(open(_m109, encoding="utf-8"))
    _mcp109["mcpServers"]["bridge"]["env"]["PYTHONPATH"] = _root109
    _mcp109["mcpServers"] = {"theirs": {"command": "node",
                                        "args": ["server.js"]},
                             "bridge": _mcp109["mcpServers"]["bridge"]}
    _put109(_m109, _mcp109, "\n")                             # newline
    # Every file dated in the past, so a write that puts the same bytes
    # back is still seen: "not opened for writing at all" is the claim.
    for _f in _files109(_p109):
        if os.path.exists(_f):
            os.utime(_f, (1000000000, 1000000000))
    _before109 = {p: _sha109(p) for p in _files109(_p109)}
    _in109.install(_p109, python=sys.executable)
    for _f in _files109(_p109):
        check("(a) %s/%s keeps every byte"
              % (os.path.basename(os.path.dirname(_f)),
                 os.path.basename(_f)),
              _sha109(_f) == _before109[_f], True)
    check("(a) and none of them was even opened for writing",
          [os.path.basename(_f) for _f in _files109(_p109)
           if os.path.exists(_f)
           and int(os.path.getmtime(_f)) != 1000000000], [])
    _after109 = _json.load(open(_s109, encoding="utf-8"))
    if _bare109:
        check("(a) the working status line is still the one written by hand",
              _after109.get("statusLine", {}).get("command"),
              '%s "%s"' % (_bare109, _in109.STATUS_SCRIPT))
    if os.name == "nt":
        check("(a) settings.json keeps PYTHONPATH in its own capitalisation",
              (_after109.get("env") or {}).get("PYTHONPATH"), _root109)
        check("(a) .mcp.json keeps the channel entry as it was written",
              ((_json.load(open(_m109, encoding="utf-8")).get("mcpServers")
                or {}).get("bridge") or {}).get("env", {}).get("PYTHONPATH"),
              _root109)
    else:
        _SK109.append("(a) capitalisation - Windows only")
    check("(a) and nothing is missing - the marks read whole",
          _in109.marks_missing(_p109), [])

    print("   (b) CONTROL: a file that DOES need a change keeps its line ending")
    print("   and its closing newline, and loses nothing that is not ours")
    _put109(_l109, {"permissions": {"allow": ["Bash(npm test:*)"]},
                    "enabledMcpjsonServers": ["theirs"]}, "\n", crlf=True)
    _in109.install(_p109, python=sys.executable)
    _raw109 = open(_l109, "rb").read()
    check("(b) the approval was added and theirs kept",
          (_json.loads(_raw109.decode("utf-8")).get("enabledMcpjsonServers"),
           _json.loads(_raw109.decode("utf-8"))["permissions"]["allow"]),
          (["theirs", "bridge"], ["Bash(npm test:*)"]))
    check("(b) every line still ends CRLF",
          (b"\r\n" in _raw109, b"\n" in _raw109.replace(b"\r\n", b"")),
          (True, False))
    check("(b) and the closing newline is still there",
          _raw109.endswith(b"\n"), True)

    print("   (c) CONTROL: a status line whose interpreter is not here, and")
    print("   hooks naming one, are ours to repair - as 8.10 says")
    _cfg109 = _json.load(open(_s109, encoding="utf-8"))
    _gone109 = os.path.join(TMP, "no-such-dir", "python.exe")
    _cfg109["statusLine"]["command"] = ('"%s" "%s"' % (
        _gone109, _in109.STATUS_SCRIPT))
    for _g in _cfg109["hooks"]["Stop"]:
        for _h in _g["hooks"]:
            if _h.get("args") == [_in109.HOOK_SCRIPT]:
                _h["command"] = _gone109
    _put109(_s109, _cfg109)
    _in109.install(_p109, python=sys.executable)
    _cfg109 = _json.load(open(_s109, encoding="utf-8"))
    check("(c) the dead status line and the dead hook point at a live "
          "interpreter now",
          (_gone109 in _cfg109["statusLine"]["command"],
           any(_h.get("command") == _gone109 for _g in _cfg109["hooks"]["Stop"]
               for _h in _g["hooks"])), (False, False))

    print("   (d) CONTROL: a project with no marks gets them, and a new file")
    print("   ends with a newline")
    _q109 = os.path.join(TMP, "bare109")
    os.makedirs(_q109, exist_ok=True)
    _in109.install(_q109, python=sys.executable)
    check("(d) the bare project is whole after one install",
          _in109.marks_missing(_q109), [])
    check("(d) and the files install created end with a newline",
          [os.path.basename(p) for p in (
              os.path.join(_q109, ".claude", "settings.json"),
              os.path.join(_q109, ".claude", "settings.local.json"),
              os.path.join(_q109, ".mcp.json"))
           if not open(p, "rb").read().endswith(b"\n")], [])

    print("   (e) the census: install.py puts JSON into a file in ONE place")
    _tree109 = _ast109.parse(inspect.getsource(_in109))
    _dumps109 = []
    for _fn in _ast109.walk(_tree109):
        if isinstance(_fn, _ast109.FunctionDef):
            for _n in _ast109.walk(_fn):
                if isinstance(_n, _ast109.Call) \
                        and getattr(_n.func, "attr", "") == "dump" \
                        and _fn.name != "save_json":
                    _dumps109.append("%s:%d" % (_fn.name, _n.lineno))
    check("(e) no json.dump into a file outside save_json", _dumps109, [])
    print("  ..   checks not asked in this run: %d%s" % (
        len(_SK109), (" - " + ", ".join(_SK109)) if _SK109 else ""))
finally:
    for _k, _v in _env109.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


print("\n110. what the planner is told it may run is what it may run: edits,")
print("     Bash and PowerShell are denied, Monitor measures, only check accepts")
print("    2026-09-23, the owner: Monitor stays allowed, and the canon is to say")
print("    so. Eight places told the planner it 'cannot run anything' while")
print("    disallow_for never denied Monitor - a false sentence repeated in")
print("    every delivery, which no check here could see. The Russian half of")
print("    the negative lives in test_cases, as case 6's words do.")
print("    -> DECISIONS.md 8.32")
import ast as _ast110                                      # noqa: E402
import inspect as _in110                                   # noqa: E402
import io as _io110                                        # noqa: E402
from bridgecore import channel as _ch110                   # noqa: E402

_den110 = list(daemon.disallow_for(PATH, "planner") or [])
check("the fact every sentence below rests on: Monitor is not denied to the "
      "planner, and Bash and the edit tools are",
      ("Monitor" in _den110, all(t in _den110 for t in ("Bash", "Edit"))),
      (False, True))
_root110 = os.path.dirname(os.path.dirname(os.path.abspath(daemon.__file__)))


def _paras110(name):
    p = os.path.join(_root110, name)
    t = _io110.open(p, encoding="utf-8").read() if os.path.isfile(p) else ""
    return [b for b in t.replace("\r\n", "\n").split("\n\n") if "Bash" in b]


# Each canon is read WITH its gates file: the section that was one of the two
# places moved out of it whole under rule 36 (case 129, DECISIONS 8.82).
for _name110, _gates110 in (("HONESTY.md", "HONESTY_GATES.md"),
                            ("HONESTY.en.md", "HONESTY_GATES.en.md")):
    if (_name110 == "HONESTY.en.md"
            and not os.path.isfile(os.path.join(_root110, _name110))
            and not os.path.isfile(os.path.join(_root110, "make_public.py"))):
        # The public tree: its HONESTY.md IS the English canon, checked on
        # the pass above, and the twin exists only in the repository. This
        # was the public suite's one new FAIL since 2026-09-13, found by
        # running it in a copy of the built tree (DECISIONS 8.46). Where
        # make_public.py stands, a missing twin still fails below.
        print("   HONESTY.en.md is not beside this suite, and neither is")
        print("   make_public.py - the public tree: not counted")
        continue
    _b110 = _paras110(_name110) + _paras110(_gates110)
    check("%s: both places that tell the planner what is denied are found"
          % _name110, len(_b110) >= 2, True)
    check("%s: each of them names Monitor as what measures" % _name110,
          [b.strip()[:60] for b in _b110 if "Monitor" not in b], [])

_lits110 = []
for _mod110 in (daemon, _ch110):
    for _n in _ast110.walk(_ast110.parse(_in110.getsource(_mod110))):
        if isinstance(_n, _ast110.Constant) and isinstance(_n.value, str) \
                and "I verified" in _n.value:
            _lits110.append((_mod110.__name__, _n.value))
check("the four code texts that explain why check exists are found - the "
      "instructions, the tool, the gate's refusal, the seed",
      len(_lits110) >= 4, True)
check("and each says Monitor is what the planner's window runs",
      [(m, v[:60]) for m, v in _lits110 if "Monitor" not in v], [])
_old110 = ("cannot run anything", "can run nothing", "run anything yourself")
_en110 = os.path.join(_root110, "HONESTY.en.md")
# THE STRINGS AS THE PLANNER GETS THEM, not the source text: a sentence
# split over two literals ("You cannot run " / "anything in ...") is one
# string to the reader and two to a text search - the sabotage that put
# the old refusal back was invisible to this check until it read constants.


def _consts110(mod):
    return chr(10).join(n.value for n in _ast110.walk(
        _ast110.parse(_in110.getsource(mod)))
        if isinstance(n, _ast110.Constant) and isinstance(n.value, str))


_srcs110 = [("daemon.py", _consts110(daemon)),
            ("channel.py", _consts110(_ch110)),
            ("HONESTY.en.md", _io110.open(_en110, encoding="utf-8").read()
             if os.path.isfile(_en110) else ""),
            ("HONESTY_GATES.en.md", "\n\n".join(
                _paras110("HONESTY_GATES.en.md")))]
check("and nothing in them still says the planner cannot run anything",
      [(f, w) for f, t in _srcs110 for w in _old110 if w in t], [])


print("\n111. no suite reads or writes the user's own ~/.claude/settings.json:")
print("     approve_channel merges into BRIDGE_CLAUDE_SETTINGS where it is set")
print("    Every install in a suite ran approve_channel, and approve_channel")
print("    merged the channel approval into the user-level settings file of")
print("    the machine running the suite - read every time, and written on")
print("    the day it lacked the approval. BRIDGE_CLAUDE_JSON already moved")
print("    the client's .claude.json; this is the same seam for the other")
print("    file. -> DECISIONS.md 8.35")
import glob as _gl111                                      # noqa: E402
import hashlib as _hl111                                   # noqa: E402
import json as _json                                       # noqa: E402,F811
from bridgecore import install as _in111                   # noqa: E402

_root111 = os.path.dirname(os.path.dirname(os.path.abspath(daemon.__file__)))
print("   (a) the census: a suite that moves the client's .claude.json moves")
print("   the user's settings.json too, before it imports the package")
_bad111 = []
for _f111 in sorted(_gl111.glob(os.path.join(_root111, "test_*.py"))):
    _t111 = open(_f111, encoding="utf-8").read()
    if 'os.environ["BRIDGE_CLAUDE_JSON"]' not in _t111:
        continue
    _i111 = _t111.find('os.environ["BRIDGE_CLAUDE_SETTINGS"]')
    # owntemp is the one import allowed before it: it makes the folder the
    # environment is then pointed into, and reads nothing (checked below)
    _t111x = _t111.replace("\nfrom bridgecore import owntemp", "\n#owntemp")
    _imp111 = min([i for i in (_t111x.find("\nfrom bridgecore"),
                               _t111x.find("\nimport bridgecore"))
                   if i >= 0] or [len(_t111x)])
    if _i111 < 0 or _i111 > _imp111:
        _bad111.append(os.path.basename(_f111))
check("(a) every such suite sets BRIDGE_CLAUDE_SETTINGS before importing "
      "bridgecore", _bad111, [])
import ast as _ast111                                      # noqa: E402
_ot111 = _ast111.parse(read_or_fail(os.path.join(
    _root111, "bridgecore", "owntemp.py"), "owntemp.py") or "pass")
check("(a) owntemp, imported first, reads nothing from the package or the "
      "environment",
      ([n.module for n in _ast111.walk(_ot111)
        if isinstance(n, _ast111.ImportFrom)],
       "environ" in _ast111.dump(_ot111)), ([], False))
check("(a) and the census saw the suites it is about",
      len([f for f in _gl111.glob(os.path.join(_root111, "test_*.py"))
           if 'os.environ["BRIDGE_CLAUDE_JSON"]' in open(
               f, encoding="utf-8").read()]) >= 7, True)

print("   (b) with the seam set, the user's file keeps every byte and is not")
print("   even opened for writing; the seam's file takes the approval")
_home111 = os.path.join(TMP, "home111")
os.makedirs(os.path.join(_home111, ".claude"), exist_ok=True)
_user111 = os.path.join(_home111, ".claude", "settings.json")
with open(_user111, "w", encoding="utf-8") as _f:
    _f.write('{"theme": "dark"}\n')           # no approval: the old code writes
os.utime(_user111, (1000000000, 1000000000))
_sha111 = _hl111.sha256(open(_user111, "rb").read()).hexdigest()
_env111 = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME",
                                          "BRIDGE_CLAUDE_SETTINGS")}
_seam111 = os.path.join(TMP, "seam111-settings.json")
_p111 = os.path.join(TMP, "seam111-project")
os.makedirs(_p111, exist_ok=True)
try:
    os.environ["USERPROFILE"] = os.environ["HOME"] = _home111
    os.environ["BRIDGE_CLAUDE_SETTINGS"] = _seam111
    _in111.approve_channel(_p111)
    check("(b) the user's file keeps its bytes and its date",
          (_hl111.sha256(open(_user111, "rb").read()).hexdigest() == _sha111,
           int(os.path.getmtime(_user111))), (True, 1000000000))
    # guarded: on the red run the seam's file is never written, and a
    # raise here would take the summary with it
    _sj111 = (_json.load(open(_seam111, encoding="utf-8"))
              if os.path.isfile(_seam111) else {})
    check("(b) and the seam's file has the approval",
          "bridge" in (_sj111.get("enabledMcpjsonServers") or []), True)
    print("   (c) CONTROL: without the seam the same call writes the user's")
    print("   file - the check above can fail")
    os.environ.pop("BRIDGE_CLAUDE_SETTINGS", None)
    _in111.approve_channel(_p111)
    check("(c) without the seam the user's file is written",
          "bridge" in (_json.load(open(_user111, encoding="utf-8"))
                       .get("enabledMcpjsonServers") or []), True)
finally:
    for _k, _v in _env111.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


print("\n112. a check's command writes straight into its own file: a timeout")
print("     keeps what came out, and what the command left running holds")
print("     nothing the check waits on")
print("     2026-09-26 23:14:49: the planner's check got 'timed out after")
print("     1200s' for multipair and not one line more - run() raised with")
print("     the output inside the exception, and it was dropped - and got it")
print("     429 s past the limit, because a stub the suite had left behind")
print("     held the pipe the output was read from. -> DECISIONS.md 8.40")
from bridgecore import sessions                            # noqa: E402,F811
_d112 = os.path.join(TMP, "run-one")
os.makedirs(_d112, exist_ok=True)
# The command, in the real order: it prints (unflushed, as a suite does),
# starts a process that holds its stdout for 25 s, then waits argv[2]
# seconds - past the limit for (a), not at all for (b).
_CMD112 = (
    "import subprocess, sys, time\n"
    "print('line one before the hang')\n"
    "print('line two before the hang')\n"
    "h = subprocess.Popen([sys.executable, '-c',\n"
    "                      'import time; time.sleep(25)'],\n"
    "                     stdout=sys.stdout, stderr=sys.stderr)\n"
    "open(sys.argv[1], 'w').write(str(h.pid))\n"
    "print('the holder is up')\n"
    "time.sleep(float(sys.argv[2]))\n"
    "print('the command ends')\n")
# _check_env copies this process's environment, so a runner that exports
# PYTHONUNBUFFERED itself would make the check below unable to fail.
_unbuf112 = os.environ.pop("PYTHONUNBUFFERED", None)
_env112 = daemon._check_env(_d112)
_limit112 = daemon.CHECK_TIMEOUT
_holders112 = []


def _holder112(pidfile):
    try:
        with open(pidfile) as _fh:
            return int(_fh.read())
    except (OSError, ValueError):
        return None


try:
    # The limit is the module's, as run_check uses it - not a parameter
    # a caller has to remember to pass.
    daemon.CHECK_TIMEOUT = 4
    print("   (a) the command hangs past the limit, its holder still up")
    _pf112 = os.path.join(_d112, "holder-a.pid")
    _out112 = os.path.join(_d112, "hang.txt")
    _t112 = time.time()
    _code112, _tail112 = daemon._run_one(
        [sys.executable, "-c", _CMD112, _pf112, "60"], _d112, _env112,
        _out112)
    _took112 = time.time() - _t112
    _h112 = _holder112(_pf112)
    _holders112.append(_h112)
    print("   returned %.1fs after the start: exit %s, tail %r"
          % (_took112, _code112, _tail112))
    check("(a) the timeout is reported as one", _code112, 124)
    check("(a) and it came back while the holder was still running - it did "
          "not wait out what the command left behind",
          bool(_h112) and sessions.pid_alive(_h112), True)
    _txt112 = read_or_fail(_out112, "the command's own file")
    check("(a) what the command printed before the limit is in its file",
          ("line one before the hang" in _txt112,
           "the holder is up" in _txt112), (True, True))
    check("(a) and the file says the timeout and the exit, last",
          _txt112.rstrip().splitlines()[-2:] if _txt112 else [],
          ["timed out after 4s", "EXIT=124"])
    check("(a) the answer carries the tail AND the timeout, not the timeout "
          "alone", _tail112, ["line one before the hang",
                              "line two before the hang", "the holder is up",
                              "timed out after 4s"])

    print("   (b) the command ends at once and leaves its holder running -")
    print("   the shape of case 122's stub: the suite passed, and the row")
    print("   waited for the stub")
    _pf112b = os.path.join(_d112, "holder-b.pid")
    _out112b = os.path.join(_d112, "done.txt")
    _code112b, _tail112b = daemon._run_one(
        [sys.executable, "-c", _CMD112, _pf112b, "0"], _d112, _env112,
        _out112b)
    _h112b = _holder112(_pf112b)
    _holders112.append(_h112b)
    check("(b) the command's own exit code", _code112b, 0)
    check("(b) returned while the holder was still running",
          bool(_h112b) and sessions.pid_alive(_h112b), True)
    check("(b) and the answer is the command's own tail",
          _tail112b, ["line two before the hang", "the holder is up",
                      "the command ends"])
finally:
    daemon.CHECK_TIMEOUT = _limit112
    if _unbuf112 is not None:
        os.environ["PYTHONUNBUFFERED"] = _unbuf112
    for _h in _holders112:
        if _h:
            sessions.terminate_and_wait(_h)
check("nothing from this case is left running",
      [h for h in _holders112 if h and sessions.pid_alive(h)], [])


print("\n113. a temp folder is removed only by the process that made it, by the")
print("     exact path it was given - when the run passes; when it fails it")
print("     stays, and the run says where. Nothing sweeps.")
print("     2026-09-28: 4 041 folders of ours in the temp folder, 2.2 GB. A")
print("     sweep of 'our prefixes, older than a day' was tried, and a")
print("     sabotage that took its prefix check out deleted every folder in")
print("     the temp folder older than a day for five minutes - every Claude")
print("     Code session's scratchpad among them. The answer is not a second")
print("     lock on a sweep: there is none. -> DECISIONS.md 8.43, 8.45")
import ast as _ast113                                      # noqa: E402
import stat as _stat113                                    # noqa: E402
from bridgecore import owntemp as _ot113                   # noqa: E402
_d113 = os.path.join(TMP, "temp113")
os.makedirs(_d113, exist_ok=True)


def _ro113(folder):
    """A folder in the shape git leaves: a read-only file two levels down."""
    sub = os.path.join(folder, ".git", "objects")
    os.makedirs(sub, exist_ok=True)
    f = os.path.join(sub, "0f26")
    with open(f, "w") as _fh:
        _fh.write("x" * 1000)
    os.chmod(f, _stat113.S_IREAD)
    return f


# (a) the life of a suite's folder, seen from OUTSIDE: a child makes it the
# way a suite does, says where, waits for the word, and ends as a suite ends
_KID113 = (
    "import os, sys, time\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "from bridgecore import owntemp\n"
    "t = owntemp.make('bridge-test-', dir=sys.argv[1])\n"
    "os.makedirs(os.path.join(t, '.git', 'objects'))\n"
    "f = os.path.join(t, '.git', 'objects', 'ab')\n"
    "open(f, 'w').write('x')\n"
    "os.chmod(f, 0o444)\n"
    "open(os.path.join(sys.argv[1], 'where.txt'), 'w').write(t)\n"
    "while not os.path.exists(os.path.join(sys.argv[1], 'go.txt')):\n"
    "    time.sleep(0.1)\n"
    "owntemp.finish(t, sys.argv[2] == 'fail')\n")


def _life113(how):
    base = os.path.join(_d113, how)
    os.makedirs(base, exist_ok=True)
    kid = subprocess.Popen([sys.executable, "-c", _KID113, base, how,
                            os.path.dirname(os.path.abspath(__file__))],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    where = os.path.join(base, "where.txt")
    for _ in range(200):
        if os.path.exists(where):
            break
        time.sleep(0.1)
    t = open(where).read() if os.path.exists(where) else ""
    during = bool(t) and os.path.isdir(t)
    open(os.path.join(base, "go.txt"), "w").write("go")
    out, _ = kid.communicate(timeout=60)
    return t, during, os.path.isdir(t) if t else None, out or ""


_t113, _dur113, _aft113, _o113 = _life113("pass")
check("(a) a passing run: its folder is there while it runs", _dur113, True)
check("(a) and gone after, read-only file and all", _aft113, False)
_t113f, _dur113f, _aft113f, _o113f = _life113("fail")
check("(a) a failing run: its folder is still there after",
      (_dur113f, _aft113f), (True, True))
check("(a) and the run says where it is", bool(_t113f) and _t113f in _o113f,
      True)

print("   (b) a folder THIS process did not make is refused - even one with")
print("   our prefix, even one another run of ours left")
_theirs113 = tempfile.mkdtemp(prefix="bridge-test-", dir=_d113)
_ro113(_theirs113)
_r113 = _ot113.remove(_theirs113)
check("(b) refused, and the refusal names the path",
      (_r113[0], _theirs113 in _r113[1]), (False, True))
check("(b) and the folder is untouched, read-only file included",
      os.path.isfile(os.path.join(_theirs113, ".git", "objects", "0f26")),
      True)
check("(b) the failing child's folder too - made by another process",
      (_ot113.remove(_t113f)[0] if _t113f else None,
       os.path.isdir(_t113f) if _t113f else None), (False, True))
_mine113 = _ot113.make("bridge-test-", dir=_d113)
_ro113(_mine113)
check("(b) a folder this process made goes, read-only file and all",
      (_ot113.remove(_mine113), os.path.isdir(_mine113)), ((True, ""), False))
check("(b) and asking again is refused - it is off the list",
      _ot113.remove(_mine113)[0], False)

print("   (c) CONTROL: rmtree with ignore_errors leaves a read-only file's")
print("   folder - the reason owntemp clears the bit")
_c113 = _ot113.make("bridge-test-", dir=_d113)
_ro113(_c113)
import shutil as _sh113                                    # noqa: E402
_sh113.rmtree(_c113, ignore_errors=True)
check("(c) rmtree(ignore_errors=True) leaves it", os.path.isdir(_c113),
      os.name == "nt")
check("(c) owntemp.remove does not", _ot113.remove(_c113)[0], True)

print("   (d) nothing sweeps: no function of that name, no call to one; every")
print("   suite makes its folder through owntemp and ends through it;")
print("   run_check removes only its copy; and rmtree in the package stands")
print("   only where the census of 8.45 says it may")
_here113 = os.path.dirname(os.path.abspath(__file__))
_SUITES113 = ("test_handover.py", "test_archive.py", "test_search.py",
              "test_wall_handover.py", "test_multipair.py", "test_cases.py",
              "test_wake_sim.py", "test_recovery_sim.py")
_trees113 = {}
for _name in _SUITES113 + tuple(
        os.path.join("bridgecore", _m) for _m in sorted(
            os.listdir(os.path.join(_here113, "bridgecore")))
        if _m.endswith(".py")):
    _trees113[_name] = _ast113.parse(
        read_or_fail(os.path.join(_here113, _name), _name) or "pass")


def _calls113(tree):
    out = []
    for _n in _ast113.walk(tree):
        if isinstance(_n, _ast113.Call):
            f = _n.func
            out.append((f.attr if isinstance(f, _ast113.Attribute)
                        else getattr(f, "id", ""),
                        getattr(getattr(f, "value", None), "id", ""),
                        tuple(k.arg for k in _n.keywords), _n))
    return out


_sweeps113 = sorted(
    "%s:%s" % (name, getattr(n, "lineno", "?"))
    for name, tree in _trees113.items() for n in _ast113.walk(tree)
    if (isinstance(n, _ast113.FunctionDef) and "sweep" in n.name.lower()
        and "temp" in n.name.lower())
    or (isinstance(n, _ast113.Call) and "sweep_old_temp" in _ast113.dump(
        n.func)))
check("(d) no temp sweep is defined or called, anywhere", _sweeps113, [])
# test_cases.py is private (make_public.NEVER), so the public tree runs this
# suite without it - measured on a copy of the built tree, 2026-09-28, where
# its absence was this check's one FAIL. A suite that is not here is excused
# only where make_public.py is not either; in the repository and in the
# check's copy all eight must be here and pass (case 84's idiom, 8.46)
_repo113 = os.path.isfile(os.path.join(_here113, "make_public.py"))
_bad113 = []
for _name in _SUITES113:
    if not _repo113 and not os.path.isfile(os.path.join(_here113, _name)):
        print("   %s is not beside this suite, and neither is "
              "make_public.py - not counted" % _name)
        continue
    _cs = [(a, b) for a, b, _k, _n in _calls113(_trees113[_name])]
    if ("make", "owntemp") not in _cs or ("finish", "owntemp") not in _cs:
        _bad113.append(_name)
check("(d) every suite here makes and finishes through owntemp - all eight "
      "in the repository", _bad113, [])
# _check_execute since 8.53 (33.6): run_check and start_check share it,
# and it is the one that makes the copy and removes it
_rc113 = _calls113(_ast113.parse(inspect.getsource(daemon._check_execute)))
check("(d) the check's run: owntemp.make, owntemp.remove, and no rmtree",
      (any(a == "make" and b == "owntemp" for a, b, _k, _n in _rc113),
       any(a == "remove" and b == "owntemp" for a, b, _k, _n in _rc113),
       any(a == "rmtree" for a, b, _k, _n in _rc113)), (True, True, False))
# the census of 8.45: where rmtree may stand in the package, and why each
# cannot reach a folder that is not the bridge's own. store.archive_old
# and relayout.restore left the list in 8.46: both remove through
# remove_tree now, which clears read-only and says when it stops
_ALLOWED113 = {("bridgecore/owntemp.py", "remove"),
               ("bridgecore/relayout.py", "remove_tree")}
_rm113 = set()
for _name, _tree in _trees113.items():
    if not _name.startswith("bridgecore"):
        continue
    for _fn in _ast113.walk(_tree):
        if isinstance(_fn, _ast113.FunctionDef):
            for a, b, _k, _n in _calls113(_fn):
                if a == "rmtree":
                    _rm113.add((_name.replace(os.sep, "/"), _fn.name))
check("(d) rmtree in the package only where the census allows",
      sorted(_rm113 - _ALLOWED113), [])


print("\n114. what the census of 8.45 named doubtful: every removal reaches only")
print("     what its own code wrote")
print("     (1) make_public emptied its target - everything but .git - before")
print("     building; (2) archive_old took any folder in bridge-logs but today's;")
print("     (3) relayout.restore removed with ignore_errors, in silence; (4) a")
print("     second hook repair wrote over the first backup. -> DECISIONS.md 8.46")
import json as _js114                                      # noqa: E402
import stat as _st114                                      # noqa: E402
import zipfile as _zf114                                   # noqa: E402
from bridgecore import install as _in114                   # noqa: E402
from bridgecore import relayout as _rl114                  # noqa: E402
_d114 = os.path.join(TMP, "case114")
os.makedirs(_d114, exist_ok=True)
_here114 = os.path.dirname(os.path.abspath(__file__))


def _mp114(target):
    r = subprocess.run([sys.executable,
                        os.path.join(_here114, "make_public.py"), target],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _told114(what, rc, out, ok):
    """The build's own words when it did not do what the check expects.
    A bare "got 1" names nothing: the planner's check of 2026-09-28
    03:33:31 had six of those and no way to tell why (DECISIONS 8.46)."""
    if not ok:
        print("   %s: make_public exited %s and said:" % (what, rc))
        for _l in (out or "").strip().splitlines()[-12:]:
            print("      | " + _l)


def _files114(target):
    out = []
    for dp, dn, fn in os.walk(target):
        dn[:] = [d for d in dn if d != ".git"]
        out += [os.path.relpath(os.path.join(dp, f), target).replace(
            os.sep, "/") for f in fn]
    return sorted(out)


# make_public.py and its sources in public/ are the REPOSITORY's: neither
# is in the package, and the public tree ships this suite without them. So
# part (1) runs where the tool is - and where the tool is and its sources
# are not, that is said by name, not left to a bare exit code: the
# planner's check copied the tree without public/ and got six "got 1"
# (DECISIONS 8.46). Same idiom as case 84's English canon.
_mp_here114 = os.path.isfile(os.path.join(_here114, "make_public.py"))
_pub_here114 = [_n for _n in ("README.public.md", "ABOUT.public.md",
                              "Makefile.public", "gitignore.public",
                              # README's parts under rule 36 (8.81)
                              "CHANGELOG.md", "CONFIGURATION.md", "GATES.md",
                              # and the changelog's older part (2026-10-12)
                              "CHANGELOG-1.md",
                              # and the tests and packages (2026-10-13)
                              "TESTING.md",
                              # bridge.command's LF ends in a clone (8.114)
                              "gitattributes.public")
                if not os.path.isfile(os.path.join(_here114, "public", _n))]
check("(1) make_public.py is here, so are its sources in public/",
      _pub_here114 if _mp_here114 else [], [])
if not _mp_here114:
    print("   (1) not counted: no make_public.py beside this suite - an")
    print("   unpacked package or the public tree, which build nothing")
else:
    print("   (1a) a target that does not exist: built, and the witness names")
    print("   every file written")
    _t114a = os.path.join(_d114, "pub-new")
    _rc114, _o114 = _mp114(_t114a)
    _told114("(1a)", _rc114, _o114, _rc114 == 0)
    _w114 = {}
    try:
        with open(os.path.join(_t114a, ".make_public.json"), encoding="utf-8") \
                as _fh:
            _w114 = _js114.load(_fh)
    except (OSError, ValueError):
        pass
    check("(1a) built", _rc114, 0)
    check("(1a) the witness is make_public's and lists exactly what is there",
          (_w114.get("tool"), sorted(_w114.get("files") or [])),
          ("make_public.py", [f for f in _files114(_t114a)
                              if f != ".make_public.json"]))
    print("   and the gate that scans the built tree does not refuse the witness")
    print("   the build itself wrote - it did, the first time one was written.")
    print("   Asked of check_public's own answer over this tree; anything else it")
    print("   finds is acceptance command 11's business, not this check's")
    _cp114 = subprocess.run([sys.executable,
                             os.path.join(_here114, "check_public.py"), _t114a],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    _cpo114 = (_cp114.stdout or "") + (_cp114.stderr or "")
    check("(1a) check_public ran over the built tree, and read the witness "
          "as a file of the build",
          ("files read:" in _cpo114,
           os.path.isfile(os.path.join(_t114a, ".make_public.json")),
           ".make_public.json" in _cpo114), (True, True, False))
    # bridge.command keeps its LF ends through a Windows clone only if the
    # built tree carries the rule (8.114): asked of the tree, not of EXTRA
    _ga114 = ""
    if os.path.isfile(os.path.join(_t114a, ".gitattributes")):
        with open(os.path.join(_t114a, ".gitattributes"),
                  encoding="utf-8") as _fh:
            _ga114 = _fh.read()
    check("(1a) the built tree carries .gitattributes with bridge.command's "
          "LF rule, and check_public does not refuse it",
          ("bridge.command text eol=lf" in _ga114.splitlines(),
           ".gitattributes" in _cpo114), (True, False))
    print("   (1b) a rebuild: a file somebody else put there stays, and is named")
    _foreign114 = os.path.join(_t114a, "somebody-elses-notes.txt")
    os.makedirs(_t114a, exist_ok=True)    # (1a) may have failed: say, not crash
    with open(_foreign114, "w") as _fh:
        _fh.write("not the build's")
    os.makedirs(os.path.join(_t114a, ".git"), exist_ok=True)
    with open(os.path.join(_t114a, ".git", "HEAD"), "w") as _fh:
        _fh.write("ref: refs/heads/main")
    _rc114b, _o114b = _mp114(_t114a)
    _told114("(1b)", _rc114b, _o114b, _rc114b == 0
             and "somebody-elses-notes.txt" in _o114b)
    check("(1b) built", _rc114b, 0)
    check("(1b) the foreign file is untouched, and .git too",
          (os.path.isfile(_foreign114),
           os.path.isfile(os.path.join(_t114a, ".git", "HEAD"))), (True, True))
    check("(1b) and the build names it",
          "somebody-elses-notes.txt" in _o114b and "left untouched" in _o114b,
          True)
    print("   (1c) the first run into an existing repository: every file there is")
    print("   one the build writes - no witness yet, and it is built")
    _t114c = os.path.join(_d114, "pub-first")
    os.makedirs(_t114c, exist_ok=True)
    for _f in (_w114.get("files") or [])[:5]:
        _p = os.path.join(_t114c, *_f.split("/"))
        os.makedirs(os.path.dirname(_p), exist_ok=True)
        with open(_p, "w") as _fh:
            _fh.write("old")
    _rc114c, _o114c = _mp114(_t114c)
    _told114("(1c)", _rc114c, _o114c, _rc114c == 0)
    check("(1c) built over files that are all its own, without a witness",
          (_rc114c, os.path.isfile(os.path.join(_t114c, ".make_public.json"))),
          (0, True))
    print("   (1d) a folder with a file the build does not write, and no witness:")
    print("   refused, the file named, and NOTHING touched")
    _t114d = os.path.join(_d114, "pub-refuse")
    os.makedirs(_t114d, exist_ok=True)
    for _f in ((_w114.get("files") or [])[:3] + ["my-own-work.txt"]):
        _p = os.path.join(_t114d, *_f.split("/"))
        os.makedirs(os.path.dirname(_p), exist_ok=True)
        with open(_p, "w") as _fh:
            _fh.write("keep")
    _before114d = _files114(_t114d)
    _rc114d, _o114d = _mp114(_t114d)
    _told114("(1d)", _rc114d, _o114d, _rc114d != 0
             and "my-own-work.txt" in _o114d)
    check("(1d) refused, naming the file", (_rc114d != 0,
                                            "my-own-work.txt" in _o114d),
          (True, True))
    def _kept114(f):
        # a build that removed it must fail this line, not kill the suite
        p = os.path.join(_t114d, *f.split("/"))
        return os.path.isfile(p) and open(p).read() == "keep"


    check("(1d) and every file is still there, as it was",
          (_files114(_t114d), all(_kept114(_f) for _f in _before114d)),
          (_before114d, True))

print("   (2) archive_old: only the day folders the bridge writes")
_p114 = os.path.join(_d114, "proj-archive")
_bl114 = os.path.join(_p114, "bridge-logs")
_old114 = time.time() - 30 * 86400
for _n in ("2026-01-01", "extracts", "notes"):
    os.makedirs(os.path.join(_bl114, _n), exist_ok=True)
    with open(os.path.join(_bl114, _n, "x.txt"), "w") as _fh:
        _fh.write(_n)
    os.utime(os.path.join(_bl114, _n), (_old114, _old114))
_packed114 = store.archive_old(_p114, days=7)
check("(2) the old day folder is packed and removed",
      (_packed114, os.path.isdir(os.path.join(_bl114, "2026-01-01")),
       os.path.isfile(os.path.join(_bl114, "2026-01-01.zip"))),
      (1, False, True))
check("(2) extracts/ and a folder of any other name are not touched",
      (os.path.isdir(os.path.join(_bl114, "extracts")),
       os.path.isdir(os.path.join(_bl114, "notes"))), (True, True))

print("   (3) relayout.restore: read-only files go, and a failure is said")
_b114 = os.path.join(_d114, "relayout-base")
_zip114 = os.path.join(_d114, "backup114.zip")
with _zf114.ZipFile(_zip114, "w") as _z:
    _z.writestr("bridge/from-backup.txt", "restored")


def _tree114():
    os.makedirs(os.path.join(_b114, "bridge", ".git", "objects"),
                exist_ok=True)
    f = os.path.join(_b114, "bridge", ".git", "objects", "stale")
    with open(f, "w") as _fh:
        _fh.write("x")
    os.chmod(f, _st114.S_IREAD)
    return f


_stale114 = _tree114()
_said114 = []
_ok114 = _rl114.restore(_b114, _zip114, out=_said114.append)
check("(3) restored cleanly - the read-only file is gone, the backup is in",
      (_ok114, os.path.exists(_stale114),
       os.path.isfile(os.path.join(_b114, "bridge", "from-backup.txt"))),
      (True, False, True))
print("   a file held open cannot be removed: the failure is a line and a")
print("   False, not silence")
_held114 = os.path.join(_b114, "bridge", "held.txt")
with open(_held114, "w") as _fh:
    _fh.write("held")
_said114b = []
_rt114 = _rl114.remove_tree
# the real removal, fewer retries: the failure is the fact, not its patience
_rl114.remove_tree = lambda p, out=None, tries=8, wait=2.0: _rt114(
    p, out=out, tries=2, wait=0.2)
try:
    with open(_held114) as _hold:
        _ok114b = _rl114.restore(_b114, _zip114, out=_said114b.append)
finally:
    _rl114.remove_tree = _rt114
if os.name == "nt":
    check("(3) with a file held open: restore answers False",
          _ok114b, False)
    check("(3) and says so", any("could NOT be removed whole" in _s
                                 for _s in _said114b), True)
else:
    print("   not Windows: an open file does not stop removal - not counted")

print("   (4) a second repair of the hook interpreter keeps the first backup")
_p114r = os.path.join(_d114, "proj-repair")
os.makedirs(os.path.join(_p114r, ".claude"), exist_ok=True)
_s114 = os.path.join(_p114r, ".claude", "settings.json")


def _dead114(which):
    cfg = {"hooks": {"Stop": [{"hooks": [{
        "type": "command", "command": which,
        "args": ["-m", "bridgecore.hook"]}]}]}}
    with open(_s114, "w", encoding="utf-8") as _fh:
        _js114.dump(cfg, _fh)
    return open(_s114, encoding="utf-8").read()


_first114 = _dead114(os.path.join(_d114, "gone-a", "python.exe"))
_in114.repair_hook_python(_p114r, sys.executable)
_second114 = _dead114(os.path.join(_d114, "gone-b", "python.exe"))
_in114.repair_hook_python(_p114r, sys.executable)
_baks114 = sorted(_f for _f in os.listdir(os.path.join(_p114r, ".claude"))
                  if _f.startswith("settings.json.before-bridge-python"))
check("(4) the first backup still holds the settings as the bridge first "
      "found them",
      open(_s114 + ".before-bridge-python", encoding="utf-8").read()
      if os.path.isfile(_s114 + ".before-bridge-python") else None,
      _first114)
check("(4) and the second repair's backup has a name of its own",
      (len(_baks114), any(open(os.path.join(_p114r, ".claude", _f),
                               encoding="utf-8").read() == _second114
                          for _f in _baks114[1:])), (2, True))


print("\n115. archive_old: a day folder that could not be removed whole lost the")
print("     files it DID remove, a week later. The first pass zipped the day and")
print("     rmtree'd the folder under a bare except; a read-only or held file")
print("     stopped it half way, in silence. When the half folder aged again the")
print("     second pass rewrote <day>.zip, mode \"w\", from what was left.")
print("     Two passes in the real order: nothing may be in neither place, the")
print("     first archive is never written again, and a folder goes only after")
print("     its archive reads back whole. -> DECISIONS.md 8.46")
import hashlib as _hl115                                   # noqa: E402
import json as _js115                                      # noqa: E402
import stat as _st115                                      # noqa: E402
import zipfile as _zf115                                   # noqa: E402
_d115 = os.path.join(TMP, "case115")
_p115 = os.path.join(_d115, "proj")
_bl115 = os.path.join(_p115, "bridge-logs")
_day115 = os.path.join(_bl115, "2026-01-01")
os.makedirs(_day115, exist_ok=True)
for _n in ("a.txt", "b.txt", "zz-held.txt", "z-readonly.txt"):
    with open(os.path.join(_day115, _n), "w") as _fh:
        _fh.write(_n * 50)
os.chmod(os.path.join(_day115, "z-readonly.txt"), _st115.S_IREAD)
_old115 = time.time() - 30 * 86400
os.utime(_day115, (_old115, _old115))


def _names115(zp):
    try:
        with _zf115.ZipFile(zp) as _z:
            return set(_z.namelist())
    except (OSError, _zf115.BadZipFile):
        return set()


def _sha115(p):
    try:
        with open(p, "rb") as _fh:
            return _hl115.sha256(_fh.read()).hexdigest()
    except OSError:
        return None


def _lines115():
    p = os.path.join(_bl115, time.strftime("%Y-%m-%d"), "events.jsonl")
    try:
        with open(p, encoding="utf-8") as _fh:
            return [_js115.loads(_l).get("text", "") for _l in _fh
                    if _l.strip()]
    except (OSError, ValueError):
        return []


_zip1_115 = os.path.join(_bl115, "2026-01-01.zip")
_zip2_115 = os.path.join(_bl115, "2026-01-01.2.zip")
_all115 = {"2026-01-01/" + _n for _n in ("a.txt", "b.txt", "zz-held.txt",
                                          "z-readonly.txt")}
if os.name == "nt":
    print("   pass 1: zz-held.txt is held open, so the folder cannot go")
    print("   whole. Named to sort LAST: rmtree goes in name order and stops")
    print("   at the held file, so only a read-only file BEFORE it tests the")
    print("   clearing")
    with open(os.path.join(_day115, "zz-held.txt")) as _hold115:
        _pk115a = store.archive_old(_p115, days=7)
    _left115 = sorted(os.listdir(_day115)) if os.path.isdir(_day115) else []
    check("(a) pass 1: archived whole, the read-only file removed, only the "
          "held file left, and not counted as packed",
          (_pk115a, _names115(_zip1_115) == _all115, _left115),
          (0, True, ["zz-held.txt"]))
    check("(a) and it is said, not swallowed",
          any("could NOT be removed whole" in _t and "2026-01-01" in _t
              for _t in _lines115()), True)
    _sha1_115 = _sha115(_zip1_115)
    print("   a week later: the half folder is old again, the file is free")
    os.utime(_day115, (_old115, _old115))
    _pk115b = store.archive_old(_p115, days=7)
    check("(b) pass 2: the folder is gone and counted",
          (_pk115b, os.path.isdir(_day115)), (1, False))
    check("(b) the first archive was never written again",
          _sha115(_zip1_115) == _sha1_115 and _sha1_115 is not None, True)
    check("(b) and every file of the day is in an archive of the day",
          _names115(_zip1_115) | _names115(_zip2_115), _all115)
else:
    print("   not Windows: an open file does not stop removal - (a), (b) not "
          "counted")

print("   (c) an archive that falls short is not trusted: the folder stays,")
print("   the short archive - this call's own - is removed, and it is said")
_day115c = os.path.join(_bl115, "2026-01-02")
os.makedirs(_day115c, exist_ok=True)
for _n in ("c.txt", "d.txt"):
    with open(os.path.join(_day115c, _n), "w") as _fh:
        _fh.write(_n * 50)
os.utime(_day115c, (_old115, _old115))
_zw115 = _zf115.ZipFile.write


def _lossy115(self, filename, arcname=None, *a, **k):
    # a writer that loses a file without saying so
    if str(filename).endswith("d.txt"):
        return None
    return _zw115(self, filename, arcname, *a, **k)


_zf115.ZipFile.write = _lossy115
try:
    _pk115c = store.archive_old(_p115, days=7)
finally:
    _zf115.ZipFile.write = _zw115
check("(c) nothing packed, both files still in the folder, no short archive "
      "left",
      (_pk115c, sorted(os.listdir(_day115c)) if os.path.isdir(_day115c)
       else [], os.path.exists(os.path.join(_bl115, "2026-01-02.zip"))),
      (0, ["c.txt", "d.txt"], False))
check("(c) and it is said, naming the missing file",
      any("NOT archived" in _t and "d.txt" in _t for _t in _lines115()),
      True)


print("\n116. check_public refuses a folder it read nothing in")
print("     Over a folder that did not exist it printed 'files read: 0', 'no")
print("     personal data found', and exited 0 - a gate green over nothing,")
print("     which a build that failed would pass straight through. Found when")
print("     case 114's check of the witness stayed green over a tree that was")
print("     never built. -> DECISIONS.md 8.46")
_here116 = os.path.dirname(os.path.abspath(__file__))
_cp116 = os.path.join(_here116, "check_public.py")


def _scan116(folder):
    _r = subprocess.run([sys.executable, _cp116, folder],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    return _r.returncode, (_r.stdout or "") + (_r.stderr or "")


if not os.path.isfile(_cp116):
    print("   not counted: no check_public.py beside this suite - an unpacked")
    print("   package, which ships no scanner")
else:
    _d116 = os.path.join(TMP, "case116")
    os.makedirs(os.path.join(_d116, "empty"), exist_ok=True)
    os.makedirs(os.path.join(_d116, "one-file"), exist_ok=True)
    with open(os.path.join(_d116, "one-file", "notes.md"), "w",
              encoding="utf-8") as _fh:
        _fh.write("# notes\n\nnothing personal in here\n")
    _rc116a, _o116a = _scan116(os.path.join(_d116, "there-is-no-such-folder"))
    check("a folder that does not exist: refused, and it says it read 0 files",
          (_rc116a != 0, "read 0 files" in _o116a,
           "no such folder" in _o116a), (True, True, True))
    _rc116b, _o116b = _scan116(os.path.join(_d116, "empty"))
    check("an empty folder: refused the same way",
          (_rc116b != 0, "read 0 files" in _o116b,
           "holds no file" in _o116b), (True, True, True))
    print("   CONTROL (rule 19): a folder with one clean file passes - so the")
    print("   refusals above are about reading nothing, not a scanner that")
    print("   refuses everything")
    _rc116c, _o116c = _scan116(os.path.join(_d116, "one-file"))
    check("one clean file: passed, one file read",
          (_rc116c, "files read: 1" in _o116c, "read 0 files" in _o116c),
          (0, True, False))


print("\n117. check_public refuses the first eight hex of a real session id")
print("     A whole session id was always refused; its first eight characters,")
print("     written into a comment or a test print, are an ordinary hex word")
print("     to a pattern - and real ones reached the published tree four")
print("     times. The scan now asks the machine which sessions exist: the")
print("     names of the transcripts in the client's projects folder, read at")
print("     every run. Here that folder is one of this case's own, holding one")
print("     transcript with an invented id. -> DECISIONS.md 8.48")
_here117 = os.path.dirname(os.path.abspath(__file__))
_cp117 = os.path.join(_here117, "check_public.py")
if not os.path.isfile(_cp117):
    print("   not counted: no check_public.py beside this suite - an unpacked")
    print("   package, which ships no scanner")
else:
    _d117 = os.path.join(TMP, "case117")
    _cfg117 = os.path.join(_d117, "client")
    os.makedirs(os.path.join(_cfg117, "projects", "some-project"),
                exist_ok=True)
    # invented: a real prefix is the very thing this case keeps out
    # (assembled, so this file holds no whole id either - that would
    # be the other rule's refusal, over the published tree)
    _sid117 = "-".join(("c0ffee42", "7e57", "4abc", "8def",
                        "0123456789ab"))
    with open(os.path.join(_cfg117, "projects", "some-project",
                           _sid117 + ".jsonl"), "w") as _fh:
        _fh.write("{}\n")

    def _scan117(folder):
        _r = subprocess.run([sys.executable, _cp117, folder],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace",
                            env=dict(os.environ, CLAUDE_CONFIG_DIR=_cfg117))
        return _r.returncode, (_r.stdout or "") + (_r.stderr or "")

    for _n, _text in (("leak", "# the turn was in window 1234, session "
                                "c0ffee42, and it ended there\n"),
                      ("clean", "# a stand-in: session_XXXXXXXX, a hash "
                                "deadbeef, and c0ffee4 or c0ffee421 - none "
                                "is a session's first eight\n")):
        os.makedirs(os.path.join(_d117, _n), exist_ok=True)
        with open(os.path.join(_d117, _n, "notes.md"), "w",
                  encoding="utf-8") as _fh:
            _fh.write(_text)
    _rc117a, _o117a = _scan117(os.path.join(_d117, "leak"))
    # "session id prefix - N" is a FINDING's category line; the kind's bare
    # name is also in the summary of what was checked for, on every run -
    # the first form of this case matched that and called a clean pass a
    # refusal
    check("a real session's first eight hex: refused, the kind and the id "
          "named",
          (_rc117a != 0, "session id prefix - " in _o117a,
           "c0ffee42" in _o117a), (True, True, True))
    check("and the scan says how many sessions it knew",
          "sessions known on this machine, by their transcripts: 1"
          in _o117a, True)
    print("   CONTROL (rule 19): a placeholder, an unrelated hex word, and hex")
    print("   runs of seven and nine characters pass - so the refusal above")
    print("   is the session, not a scanner that refuses every hex word")
    _rc117b, _o117b = _scan117(os.path.join(_d117, "clean"))
    check("the look-alikes pass",
          (_rc117b, "session id prefix - " in _o117b,
           "files read: 1" in _o117b), (0, False, True))


import json                                               # noqa: E402
def _fn(name):
    """A package-B function, or None on a tree that predates it - so the red
    run on the old tree reads FAIL lines instead of a traceback."""
    return getattr(daemon, name, None)


def _call(name, *a, **k):
    f = _fn(name)
    if f is None:
        return "MISSING: %s" % name
    try:
        return f(*a, **k)
    except Exception as e:
        return "RAISED: %s: %s" % (name, e)


def _pkey(role="executor"):
    return "%s|%s" % (daemon.norm(PATH), role)


def _jl(tp, rows):
    with open(tp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return os.path.getsize(tp)


_BOUNDARY = {"type": "system", "subtype": "compact_boundary",
             "compactMetadata": {"trigger": "auto", "preTokens": 180000}}
# The client's own row, as it stands in a real transcript of 2026-10-10:
# type system, subtype informational, after the compact_boundary and a few
# attachment rows.
_IDLE = {"type": "system", "subtype": "informational", "level": "notice",
         "content": "Compacted while idle, before the prompt cache expired"}
_ATT = {"type": "attachment"}
_USER = {"type": "user", "message": {"role": "user", "content": "x"}}


print("\n118. a calibration key carries the window this half was launched with")
print("   FAILURE WOULD LOOK LIKE: 2026-10-10 21:34, an executor raised on a")
print("   222k compaction window was told 'compaction fires at 877k' - its own")
print("   samples from the million, filed under the same key, and the point")
print("   anchors on the newest sample")
reset()
check("no window recorded for the half: the bare model",
      _call("calib_model", MODEL, PATH, "executor"), MODEL.lower())
daemon.STATE["pids"][_pkey()]["compact_window"] = 222000
check("launched with 222k: a key of its own",
      _call("calib_model", MODEL, PATH, "executor"), MODEL.lower() + " w222000")
check("the planner's key is not moved by the executor's window",
      _call("calib_model", MODEL, PATH, "planner"), MODEL.lower())
daemon.STATE["pids"][_pkey()]["compact_window"] = 1000000
check("a million keeps the bare key, so what was measured there stays put",
      _call("calib_model", MODEL, PATH, "executor"), MODEL.lower())
print("   CONTROL: the configured default is NOT the witness - a window the")
print("   bridge did not launch runs at the client's million whatever the")
print("   setting says")
daemon.STATE["pids"][_pkey()].pop("compact_window", None)
_rw118 = daemon.CFG.get("role_window")
daemon.CFG["role_window"] = {"executor": 222000}
check("a role default with no launch record changes nothing",
      _call("calib_model", MODEL, PATH, "executor"), MODEL.lower())
daemon.CFG["role_window"] = _rw118

print("\n119. a compaction the client did while the session was IDLE is not")
print("   counted towards the wall, and its sample comes back out (40.4(a))")
print("   FAILURE WOULD LOOK LIKE: a quiet executor replaced for having waited -")
print("   the client compacts an idle window before its prompt cache expires,")
print("   and every one of those was a step towards the wall")
_tp119 = os.path.join(TMP, "idle-119.jsonl")
_off119 = _jl(_tp119, [_USER, _USER])
_jl(_tp119, [_USER, _USER, _BOUNDARY, _ATT, _ATT, _IDLE, _USER])
check("the client's notice after the boundary: idle",
      _call("idle_compaction_seen", _tp119, _off119), True)
_tp119b = os.path.join(TMP, "busy-119.jsonl")
_jl(_tp119b, [_USER, _BOUNDARY, _ATT, _USER])
check("a boundary with no notice: not idle",
      _call("idle_compaction_seen", _tp119b, 0), False)
_tp119c = os.path.join(TMP, "two-119.jsonl")
_jl(_tp119c, [_BOUNDARY, _USER, _BOUNDARY, _IDLE])
check("a notice belongs to its own boundary, not the one before it",
      _call("idle_compaction_seen", _tp119c, 0), False)
check("no boundary past the offset: nothing can be said",
      _call("idle_compaction_seen", _tp119, os.path.getsize(_tp119)), None)
check("no file: nothing can be said",
      _call("idle_compaction_seen", os.path.join(TMP, "none-119"), 0), None)
reset()
daemon.STATE["pids"][_pkey()]["compact_window"] = 222000
_ck119 = MODEL.lower() + " w222000"
daemon.STATE["compactions"][_pkey()] = [
    {"at": "x", "t": 1000.0, "tokens": 180000, "landed": 60000,
     "session": SID, "tp": _tp119, "tp_at": _off119, "calib": _ck119},
    {"at": "x", "t": 1001.0, "tokens": 182000, "landed": 70000,
     "session": SID, "tp": _tp119b, "tp_at": 0, "calib": _ck119}]
_cal119 = store.load_calibration()
_cal119[store.calib_key(_ck119, PATH)] = {
    "ceiling_pct": 97.0, "buffer_tokens": 33000, "misses": 0,
    "clean_streak": 0, "multiplier": 1.5, "wall_history_tokens": None,
    "compact_at_tokens": 182000, "compact_at_window": WINDOW,
    "compact_samples": [180000, 182000], "how": "test"}
store.save_calibration(_cal119)
check("both count before anybody looks",
      daemon.compactions_done(PATH, "executor"), 2)
_call("mark_idle_compactions", PATH, "executor", SID, settle=True)
_h119 = daemon.STATE["compactions"][_pkey()]
check("the idle one is marked, the other is settled as not idle",
      ([bool(h.get("idle")) for h in _h119],
       [bool(h.get("idle_checked")) for h in _h119]),
      ([True, False], [True, True]))
check("one compaction counts", daemon.compactions_done(PATH, "executor"), 1)
check("and only the working one is a size this session compacted at",
      daemon.compaction_sizes(PATH, "executor"), [182000])
check("its calibration sample is taken back",
      (store.load_calibration().get(store.calib_key(_ck119, PATH)) or {})
      .get("compact_samples"), [182000])
_said119 = [e for e in store.recent_events(400)
            if "was done while idle" in (e.get("text") or "")]
check("and the journal says so, with what was and was not undone",
      (len(_said119), "not restored" in
       ((_said119[-1].get("text") or "") if _said119 else "")), (1, True))
print("   a second look changes nothing: a settled record is not read again")
_call("mark_idle_compactions", PATH, "executor", SID, settle=True)
check("no second line",
      len([e for e in store.recent_events(400)
           if "was done while idle" in (e.get("text") or "")]), 1)
print("   CONTROL: a notice not yet written is asked again at the status line")
reset()
_tp119d = os.path.join(TMP, "late-119.jsonl")
_jl(_tp119d, [_BOUNDARY, _ATT])
daemon.STATE["compactions"][_pkey()] = [
    {"at": "x", "t": 2000.0, "tokens": 181000, "landed": 61000,
     "session": SID, "tp": _tp119d, "tp_at": 0}]
_call("mark_idle_compactions", PATH, "executor", SID, settle=False)
check("not settled at the status line",
      bool(daemon.STATE["compactions"][_pkey()][0].get("idle_checked")), False)
_jl(_tp119d, [_BOUNDARY, _ATT, _IDLE])
_call("mark_idle_compactions", PATH, "executor", SID, settle=True)
check("so the notice written later is still found",
      bool(daemon.STATE["compactions"][_pkey()][0].get("idle")), True)

print("\n120. the executor's wall is worked out from its floors, less two, and")
print("   never later than the cap (40.11): 15 for the executor, 5 for the")
print("   planner, whose wall is not computed")
print("   FAILURE WOULD LOOK LIKE: a 222k window compacting every eight minutes")
print("   replaced at its fifth compaction - forty minutes of life - or run")
print("   into a wall its floors had already reached")
check("the defaults the bridge ships",
      (store.DEFAULT_CONFIG.get("role_wall_compactions"),
       store.DEFAULT_CONFIG.get("role_wall_computed")),
      ({"executor": 15, "planner": 5}, {"executor": True}))


def _wall120(floors, calib=None, role="executor", sid=SID):
    reset()
    daemon.CFG["projects"][daemon.norm(PATH)] = {}
    daemon.CFG["role_wall_compactions"] = {"executor": 15, "planner": 5}
    daemon.CFG["role_wall_computed"] = {"executor": True}
    daemon.STATE["pids"][_pkey()]["compact_window"] = 222000
    ck = calib or (MODEL.lower() + " w222000")
    daemon.STATE["compactions"][_pkey()] = [
        {"at": "x", "t": 3000.0 + i, "tokens": 180000, "landed": f,
         "session": sid, "calib": ck} for i, f in enumerate(floors)]
    s = sess(100000)
    if role != "executor":
        s = dict(s, role=role)
    return s


_r = _call("wall_for", _wall120([]), PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("no floors: the cap, from the role default",
      (_r.get("n"), _r.get("cap_source"), _r.get("computed")),
      (15, "role default", False))
_r = _call("wall_for", _wall120([60000, 80000]), PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("two floors: still the cap, and it says how many it needs",
      (_r.get("n"), "worked out from 3" in (_r.get("why") or "")), (15, True))
_r = _call("wall_for", _wall120([60000, 80000, 100000]), PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("three floors climbing 20k reach 180k at the 7th: replaced at the 5th",
      (_r.get("wall"), _r.get("n"), _r.get("computed"), _r.get("rise")),
      (7, 5, True, 20000))
_r = _call("wall_for", _wall120([60000, 120000, 185000]), PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("a floor already at the compaction point is a MEASURED wall",
      (_r.get("wall"), _r.get("n"), "measured" in (_r.get("why") or "")),
      (3, 1, True))
_r = _call("wall_for", _wall120([100000, 90000, 95000]), PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("floors that do not climb name no wall: the cap holds",
      (_r.get("n"), _r.get("wall"), "not climbing" in (_r.get("why") or "")),
      (15, None, True))
_r = _call("wall_for", _wall120([60000, 80000, 100000]), PATH, None)
_r = _r if isinstance(_r, dict) else {}
check("with no known compaction point the floors have nothing to reach",
      (_r.get("n"), _r.get("computed")), (15, False))
_r = _call("wall_for", _wall120([60000, 80000, 100000],
                                calib=MODEL.lower()), PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("floors filed under another window's key are not this window's",
      (_r.get("n"), _r.get("computed")), (15, False))
_s120 = _wall120([60000, 80000, 100000])
daemon.CFG["projects"][daemon.norm(PATH)] = {
    "wall_compactions": {"executor": 4}}
_r = _call("wall_for", _s120, PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("a project's own cap wins, and is named as the project's",
      (_r.get("n"), _r.get("cap"), _r.get("cap_source"), _r.get("computed")),
      (4, 4, "project", False))
_s120 = _wall120([60000, 80000, 100000])
daemon.CFG["projects"][daemon.norm(PATH)] = {
    "wall_computed": {"executor": False}}
_r = _call("wall_for", _s120, PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("and a project can switch the computing off",
      (_r.get("n"), _r.get("computed")), (15, False))
_r = _call("wall_for", _wall120([60000, 80000, 100000], role="planner"),
           PATH, 180000)
_r = _r if isinstance(_r, dict) else {}
check("the planner: five, not computed",
      (_r.get("n"), _r.get("computed")), (5, False))
print("   and life_view's budget IS that number - one decision, not two")
_s120 = _wall120([60000, 80000, 100000])
_lv120 = daemon.life_view(_s120, PATH) or {}
_r = _call("wall_for", _s120, PATH,
           daemon.wall_view(_s120, PATH).get("compact"))
_r = _r if isinstance(_r, dict) else {}
check("life_view budget == wall_for n", (_lv120.get("budget"),
                                         _lv120.get("budget") == _r.get("n")),
      (_r.get("n"), True))

print("\n121. the wall decision is said at the Stop, once per change (40.4(b))")
print("   FAILURE WOULD LOOK LIKE: a handover at the 9th compaction that nobody")
print("   can read back to the floors that named the 9th")
_s121 = _wall120([60000, 80000, 100000])


def _lines121():
    return [e for e in store.recent_events(400)
            if "wall check at the Stop" in (e.get("text") or "")]


_n121 = len(_lines121())
_call("note_wall_check", _s121, PATH, "executor")
_l121 = _lines121()
check("one line, naming used, budget and why",
      (len(_l121) - _n121,
       "3 of " in ((_l121[-1].get("text") or "") if _l121 else "")),
      (1, True))
_call("note_wall_check", _s121, PATH, "executor")
check("the same decision again says nothing", len(_lines121()) - _n121, 1)
daemon.STATE["compactions"][_pkey()].append(
    {"at": "x", "t": 3100.0, "tokens": 180000, "landed": 120000,
     "session": SID, "calib": MODEL.lower() + " w222000"})
_call("note_wall_check", _s121, PATH, "executor")
check("a compaction more is a new line", len(_lines121()) - _n121, 2)
_call("note_wall_check", dict(_s121, role="unknown"), PATH, "unknown")
check("an unmanaged role is not told anything", len(_lines121()) - _n121, 2)

print("\n122. sizes and points this session does not own are not used")
print("   FAILURE WOULD LOOK LIKE: PreCompacts at 101k-160k between real ones")
print("   above 180k, none of which landed, pulling the point down; and a")
print("   percentage of the million offered for a window launched at 222k")
reset()
daemon.STATE["compactions"][_pkey()] = [
    {"at": "x", "tokens": 101000, "session": SID},
    {"at": "x", "tokens": 185000, "landed": 60000, "session": SID},
    {"at": "x", "tokens": 175000, "landed": 62000, "session": SID,
     "idle": True},
    {"at": "x", "tokens": 150000, "session": SID}]
check("a record that never landed is dropped; the newest, pending, is kept",
      daemon.compaction_sizes(PATH, "executor"), [185000, 150000])
reset(autocompact=80)
_wv122 = daemon.wall_view(sess(100000), PATH)
print("  ..   control, no launch window: compact %r (%s)"
      % (_wv122.get("compact"), _wv122.get("compact_source")))
daemon.STATE["pids"][_pkey()]["compact_window"] = 222000
# case 119 left a measured point under the 222k key; this one is about a
# window with nothing measured, so that entry goes
_cal122 = store.load_calibration()
_cal122.pop(store.calib_key(MODEL.lower() + " w222000", PATH), None)
store.save_calibration(_cal122)
_wv122 = daemon.wall_view(sess(100000), PATH)
check("launched at 222k: no point from a percentage of the reported window",
      _wv122.get("compact"), None)
check("and the reason names the window it was launched with",
      "222k compaction window" in (_wv122.get("compact_source") or ""), True)

print("\n123. after its compaction the executor is handed the thread back in")
print("   words, at most 10 000 characters (40.10)")
print("   FAILURE WOULD LOOK LIKE: a 222k executor compacting every eight")
print("   minutes and going on from a summary of its task instead of the task")
reset()
daemon.CFG["projects"][daemon.norm(PATH)]["state_file"] = "state-123.md"
with open(os.path.join(PATH, "state-123.md"), "w", encoding="utf-8") as fh:
    fh.write("old line\n" * 900 + "LAST ROW of the state file\n")
_call("note_task_sent", PATH, "TASK-123 " + "t" * 6000)
with daemon._lock:
    daemon.STATE.setdefault("last_verdict_text", {})[daemon.norm(PATH)] = {
        "at": time.time(), "n": 7, "verdict": "done",
        "text": "VERDICT-123 " + "v" * 3000}
_cc = _call("compact_context", PATH) or ""
check("the task, the verdict and the end of the state file are all there",
      ("TASK-123" in _cc, "VERDICT-123" in _cc,
       "LAST ROW of the state file" in _cc), (True, True, True))
check("within the limit, and each cut is SAID",
      (len(_cc) <= 10000, "not carried" in _cc), (True, True))
# 8.71: this used to read "kept to 4 000 characters at the source" - the
# defect itself, pinned: a task cut when it was STORED reached the cut at
# hand-back exactly at the limit, and that cut said nothing
check("the task is kept whole at the source, with its length",
      [((daemon.STATE.get("last_task_text") or {})
        .get(daemon.norm(PATH)) or {}).get(_k) for _k in ("text", "chars")],
      ["TASK-123 " + "t" * 6000, 6009])
# the task's own section only: the verdict's mark below it is the same
# words, and a match that ran on into it once summed the two
_m123 = _re.search(r"THE LAST TASK[^\n]*\n(.*?)\n\[\.\.\. (\d+) more "
                  r"characters not carried\]",
                  _cc.split("THE LAST VERDICT")[0], re.S)
check("and its cut at hand-back is said, kept plus count = the whole",
      (bool(_m123), len(_m123.group(1)) + int(_m123.group(2))
       if _m123 else -1), (True, 6009))
try:
    _x123 = daemon._clip_head("x" * 100, 50, 1000)
except TypeError as _e123:     # a _clip_head that knows no total: a FAIL
    _x123 = "raised: %s" % _e123   # line, not a traceback over 124-125
check("a count past the record's own cap is still the whole one",
      _x123.endswith("[... %d more characters not carried]"
                     % (1000 - len(_x123.split("\n")[0]))), True)
_q, _w = daemon.quiet_console_for, daemon.watch_rc_link
try:
    daemon.quiet_console_for = lambda *a, **k: None
    daemon.watch_rc_link = lambda *a, **k: None
    _o123, _ = daemon.handle_event({
        "hook_event_name": "SessionStart", "role": "executor",
        "session_id": SID, "source": "compact", "project_dir": PATH,
        "cwd": PATH})
    _ac = (((_o123 or {}).get("hookSpecificOutput") or {})
           .get("additionalContext") or "")
    check("a compaction's SessionStart carries it, before the canon",
          ("TASK-123" in _ac,
           _ac.find("TASK-123") < _ac.find("RULES OF WORK")
           if "RULES OF WORK" in _ac else True), (True, True))
    _o123b, _ = daemon.handle_event({
        "hook_event_name": "SessionStart", "role": "executor",
        "session_id": SID, "source": "resume", "project_dir": PATH,
        "cwd": PATH})
    check("CONTROL: a resume does not - the thread is for a compaction",
          "TASK-123" in (((_o123b or {}).get("hookSpecificOutput") or {})
                         .get("additionalContext") or ""), False)
    _o123c, _ = daemon.handle_event({
        "hook_event_name": "SessionStart", "role": "planner",
        "session_id": "pl-123", "source": "compact", "project_dir": PATH,
        "cwd": PATH})
    check("and the planner's compaction is not handed the executor's task",
          "TASK-123" in (((_o123c or {}).get("hookSpecificOutput") or {})
                         .get("additionalContext") or ""), False)
finally:
    daemon.quiet_console_for, daemon.watch_rc_link = _q, _w
print("   the migration adopts a state file the project already keeps, by")
print("   which file exists - never by the project's name")
_p123 = os.path.join(TMP, "proj-123")
os.makedirs(os.path.join(_p123, "bridge-logs"), exist_ok=True)
with open(os.path.join(_p123, "bridge-logs", "executor_state.md"), "w",
          encoding="utf-8") as fh:
    fh.write("x\n")
_p123b = os.path.join(TMP, "proj-123b")
os.makedirs(os.path.join(_p123b, ".tmp"), exist_ok=True)
with open(os.path.join(_p123b, ".tmp", "executor_state.md"), "w",
          encoding="utf-8") as fh:
    fh.write("x\n")
_p123c = os.path.join(TMP, "proj-123c")
os.makedirs(_p123c, exist_ok=True)
daemon.CFG["projects"][daemon.norm(_p123)] = {}
daemon.CFG["projects"][daemon.norm(_p123b)] = {}
daemon.CFG["projects"][daemon.norm(_p123c)] = {}
daemon.CFG["projects"][daemon.norm(PATH)]["state_file"] = "mine.md"
_call("migrate_state_files")
check("each project gets the file it has, and none is invented",
      [(daemon.CFG["projects"].get(daemon.norm(p)) or {}).get("state_file")
       for p in (_p123, _p123b, _p123c)],
      [os.path.join("bridge-logs", "executor_state.md"),
       os.path.join(".tmp", "executor_state.md"), None])
check("one the project already names is left alone",
      daemon.CFG["projects"][daemon.norm(PATH)].get("state_file"), "mine.md")
check("and it is written to config.json, not only to memory",
      (store.load_config().get("projects") or {}).get(
          daemon.norm(_p123b), {}).get("state_file"),
      os.path.join(".tmp", "executor_state.md"))
for _p in (_p123, _p123b, _p123c):
    daemon.CFG["projects"].pop(daemon.norm(_p), None)


print("\n124. a calibration sample goes where its window says it belongs (8.62)")
print("   FAILURE WOULD LOOK LIKE: a planner on a million told its compaction")
print("   point is its executor's 181k, or a wall at 183 867 - samples written")
print("   under the key without a window before the window was in the key")
_p124 = daemon.norm(os.path.join(TMP, "proj-124"))
os.makedirs(_p124, exist_ok=True)
_t124 = time.mktime(time.strptime("2026-10-10 20:00", "%Y-%m-%d %H:%M"))


def _r124(sid, mins, tokens, landed=True):
    h = {"at": time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(_t124 + mins * 60)),
         "tokens": tokens, "session": sid}
    if landed:
        h["landed"] = tokens - 150000
    return h


def _b124(mins, pre, idle=False):
    rows = [dict(_BOUNDARY, timestamp=time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(_t124 + mins * 60 + 90)),
        compactMetadata={"trigger": "auto", "preTokens": pre})]
    if idle:
        rows.append({"type": "system", "content": "Compacted while idle "
                     "(ctrl+o to see full summary)"})
    return rows


# ex-old: the half's previous session, 181 000 like the new one's but before
# the launch and not in the samples; ex-new: launched at minute 0 on 222k,
# compacting before it (700 100) and right after it at the old window's
# size (650 000); ex-hand: a window the bridge never opened, after the
# launch, on a million; pl-1: the planner, one idle compaction and one not
_x124 = [_r124("ex-old", -60, 181000), _r124("ex-new", -30, 700100),
         _r124("ex-new", 10, 650000), _r124("ex-new", 20, 181000),
         _r124("ex-new", 30, 160000, landed=False),
         _r124("ex-new", 40, 181500), _r124("ex-hand", 45, 880500)]
_l124 = [_r124("pl-1", 35, 450000), _r124("pl-1", 50, 881000)]
_tp124 = os.path.join(TMP, "pl-1.jsonl")
_jl(_tp124, _b124(35, 450600, idle=True) + _b124(50, 881700))
_samples124 = [700100, 650000, 181000, 160000, 450000, 181500, 880500, 881000]
_k124 = "opus t|%s" % _p124
_kw124 = "opus t w222000|%s" % _p124
_k1m124 = "opus t (1m context)|%s" % _p124
_cal = store.load_calibration()
_cal[_k124] = {"compact_samples": list(_samples124), "ceiling_pct": 40,
               "wall_history_tokens": 183867}
_cal[_kw124] = {"compact_samples": [179000]}
_cal[_k1m124] = {"compact_samples": [879000, 880000], "ceiling_pct": 70}
store.save_calibration(_cal)
with daemon._lock:
    daemon.STATE.setdefault("compactions", {})[
        "%s|executor" % _p124] = _x124
    daemon.STATE["compactions"]["%s|planner" % _p124] = _l124
    daemon.STATE.setdefault("pids", {})["%s|executor" % _p124] = {
        "pid": 1, "at": _t124, "sid": "ex-new", "compact_window": 222000}
    daemon.STATE["pids"]["%s|planner" % _p124] = {
        "pid": 2, "at": _t124, "sid": "pl-1", "compact_window": 1000000}
    daemon.STATE.setdefault("sessions", {})["planner:pl-124"] = {
        "role": "planner", "path": _p124, "model": "Opus T",
        "window": 1000000, "seen_at": time.time()}
_m124 = _call("migrate_calibration_windows",
              lambda sid: _tp124 if sid == "pl-1" else None)
_ok124 = isinstance(_m124, dict)
check("the migration runs", _ok124 or _m124, True)
_cal = store.load_calibration()
check("the bare key keeps what was measured on a million - before the "
      "launch, and in a window the bridge never opened - under the "
      "'(1m context)' samples it is the same window as",
      (_cal.get(_k124) or {}).get("compact_samples"),
      [879000, 880000, 700100, 880500, 881000])
check("the launched session's landed samples MOVE to its window's key, "
      "older first", (_cal.get(_kw124) or {}).get("compact_samples"),
      [181000, 181500, 179000])
check("the '(1m context)' key is gone, merged", _k1m124 in _cal, False)
check("the wall no refusal wrote is dropped",
      (_cal.get(_k124) or {}).get("wall_history_tokens"), None)
check("a sample in a window's key leaves it with a point",
      bool((_cal.get(_kw124) or {}).get("compact_at_tokens")), True)
check("the idle compaction is marked by the client's notice, and the "
      "other is only checked",
      [(h.get("idle"), h.get("idle_checked"))
       for h in daemon.STATE["compactions"]["%s|planner" % _p124]],
      [(True, True), (None, True)])
check("what each sample was taken for",
      dict((k, v) for k, v in ((_m124 or {}).get("tally") or {}).items()
           if k in ("move", "first", "unlanded", "idle")) if _ok124 else _m124,
      {"move": 2, "first": 1, "unlanded": 1, "idle": 1})
_m124b = _call("migrate_calibration_windows",
               lambda sid: _tp124 if sid == "pl-1" else None)
check("a second run changes nothing",
      ((_m124b or {}).get("changed"), ((_m124b or {}).get("tally") or {})
       .get("move")) if isinstance(_m124b, dict) else _m124b, ([], None))
print("   and a session a handover has since replaced keeps its witness: the")
print("   pids record is the NEXT window's by then (sid None), so the window")
print("   comes from window_log, window_sessions and the launch line - or,")
print("   where the line predates naming a window, from the session's own")
print("   records, which name the key they were written to")
_ph = daemon.norm(os.path.join(TMP, "proj-124h"))
os.makedirs(_ph, exist_ok=True)
_kh = "opus t|%s" % _ph
_kwh = "opus t w222000|%s" % _ph
_day124 = os.path.join(store.LOGS, time.strftime("%Y-%m-%d",
                                                 time.localtime(_t124)))
os.makedirs(_day124, exist_ok=True)
with open(os.path.join(_day124, "events.jsonl"), "a", encoding="utf-8") as fh:
    for _m, _txt in ((0, "(mode bypassPermissions, effort xhigh, window "
                         "222k): handover"),
                     (30, "(mode bypassPermissions): restart after it died"),
                     (60, "(mode bypassPermissions, effort xhigh): you "
                          "pressed start in the panel")):
        fh.write(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(_t124 + _m * 60 - 1)),
            "kind": "session", "text": "Opening a executor window on opus "
            + _txt, "path": _ph, "session": "executor"}) + "\n")
_xh = [_r124("ex-a", 10, 181100), _r124("ex-a", 20, 181200),
       _r124("ex-b", 35, 181400), dict(_r124("ex-b", 45, 181500),
                                       calib="opus t w222000"),
       _r124("ex-c", 70, 880700)]
_cal = store.load_calibration()
_cal[_kh] = {"compact_samples": [181100, 181200, 181400, 880700]}
_cal[_kwh] = {"compact_samples": [181500]}
store.save_calibration(_cal)
with daemon._lock:
    daemon.STATE["compactions"]["%s|executor" % _ph] = _xh
    daemon.STATE["pids"]["%s|executor" % _ph] = {
        "pid": 14, "at": _t124 + 80 * 60, "sid": None,
        "compact_window": 222000}
    for _pid, _m in ((11, 0), (12, 30), (13, 60)):
        daemon.STATE.setdefault("window_log", []).append(
            {"pid": _pid, "path": _ph, "role": "executor", "why": "x",
             "at": _t124 + _m * 60})
    daemon.STATE.setdefault("window_sessions", {})[
        "%s|executor" % _ph] = {"11": {"sid": "ex-a"}, "12": {"sid": "ex-b"},
                                "13": {"sid": "ex-c"}}
_mh = _call("migrate_calibration_windows", lambda sid: None)
_cal = store.load_calibration()
check("the replaced sessions' samples move by the launch line and by their "
      "own records; the one launched on no window stays",
      ((_cal.get(_kh) or {}).get("compact_samples"),
       (_cal.get(_kwh) or {}).get("compact_samples")),
      ([880700], [181100, 181200, 181400, 181500]))
with daemon._lock:
    for _k in ("%s|executor" % _p124, "%s|planner" % _p124,
               "%s|executor" % _ph):
        daemon.STATE["compactions"].pop(_k, None)
        daemon.STATE["pids"].pop(_k, None)
    daemon.STATE["window_sessions"].pop("%s|executor" % _ph, None)
    daemon.STATE["window_log"] = [r for r in daemon.STATE["window_log"]
                                  if r.get("path") != _ph]
    daemon.STATE["sessions"].pop("planner:pl-124", None)
_cal = store.load_calibration()
for _k in (_k124, _kw124, _k1m124, _kh, _kwh):
    _cal.pop(_k, None)
store.save_calibration(_cal)


print("\n125. a lost write of state.json names who holds the file (40.8)")
print("    2026-10-10 12:33:28-12:36:33: 201 writes lost in a row, every line")
print("    'something has it open', and the holder could afterwards only be")
print("    guessed from the clock. A child process holds state.json the way")
print("    that afternoon's reader did - an ordinary open() for reading, which")
print("    passes no FILE_SHARE_DELETE - and the line of the FIRST loss must")
print("    name its pid; the second loss of the run does not ask again. The")
print("    live control is the same probe asked of a file nobody holds: it")
print("    must answer an empty list, which a probe that has stopped seeing")
print("    anything would answer too - so the held answer is what proves it")
print("    sees, and the empty one that it does not invent. -> DECISIONS.md 8.67")
if os.name != "nt":
    print("   (Restart Manager is Windows only; nothing to ask here)")
else:
    if not os.path.exists(store.STATE_PATH):
        store.save_state(daemon.STATE)
    _lines125 = lambda: [e.get("text") or "" for e in store.recent_events(400)
                         if "Could not replace state.json" in
                         (e.get("text") or "")]
    _held125 = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; fh = open(sys.argv[1]); print('ready', flush=True); "
         "sys.stdin.read()", store.STATE_PATH],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        check("PRECONDITION: the child has state.json open",
              _held125.stdout.readline().strip(), "ready")
        store._state_write_fails[0] = 0
        store.save_state(daemon.STATE)
        check("PRECONDITION: the write lost against it",
              store._state_write_fails[0], 1)
        _first125 = [t for t in _lines125() if "; 1 in a row" in t
                     and "pid %d " % _held125.pid in t]
        check("the first loss's line names the child's pid", len(_first125), 1)
        if _first125:
            print("   the line ends: ...%s" % _first125[0][-160:])
        check("and its image, the interpreter it runs",
              bool(_first125) and os.path.basename(sys.executable).lower()
              in _first125[0].lower(), True)
        store.save_state(daemon.STATE)
        _second125 = [t for t in _lines125() if "; 2 in a row" in t]
        check("the second loss of the run does not ask again",
              (len(_second125), [t for t in _second125 if "Held by" in t]),
              (1, []))
    finally:
        _held125.stdin.close()
        _held125.wait(timeout=30)
    store.save_state(daemon.STATE)
    check("once the child let go the write wins and the run is over",
          store._state_write_fails[0], 0)
    _free125 = os.path.join(TMP, "nobody-holds-this.json")
    with open(_free125, "w", encoding="utf-8") as _fh:
        _fh.write("{}")
    _who125 = getattr(store, "who_holds", lambda paths: None)
    check("the control: a file nobody holds - asked, and nobody named",
          _who125([_free125]), [])
    os.remove(_free125)


print("\n126. a compaction that lands at the wall tells the executor so, first")
print("   in the thread it is handed back - and the handoff demand names the")
print("   wall it was decided on")
print("   2026-10-11 the bridge's own executor, its wall the 15th, compacted")
print("   the 15th, 16th and 17th time inside one turn and was handed over at")
print("   the Stop as \"17 of 15\"; the demand it was put said \"the wall of")
print("   five compactions\". The receiver is what the window is handed: the")
print("   SessionStart(compact) return and the Stop's demand.")
print("   FAILURE WOULD LOOK LIKE: no WALL line at the W-th compaction, one at")
print("   the (W-1)-th, the thread ahead of it, or \"five\" to a wall of four")
print("   -> DECISIONS.md 8.73")


def _wall126(counted, idle=False):
    """A wall of 4 for the executor; `counted` landed compactions of SID,
    and the one that has just landed - PreCompact wrote it, nothing has
    marked it landed: SessionStart(compact) is the first word of it."""
    reset()
    daemon.CFG["projects"][daemon.norm(PATH)] = {
        "wall_compactions": {"executor": 4, "planner": 5},
        "wall_computed": {"executor": False}}
    hist = [{"at": "x", "t": 3000.0 + i, "tokens": 180000, "landed": 60000,
             "session": SID} for i in range(counted)]
    hist.append({"at": "x", "t": 4000.0, "tokens": 180000, "session": SID})
    if idle:
        hist[-1]["idle"] = True
    daemon.STATE["compactions"][_pkey()] = hist
    _call("note_task_sent", PATH, "TASK-126 the work in hand")


def _lines126():
    return [e for e in store.recent_events(400)
            if "wall at the compaction landing" in (e.get("text") or "")]


def _start126():
    o, _ = daemon.handle_event({
        "hook_event_name": "SessionStart", "role": "executor",
        "session_id": SID, "source": "compact", "project_dir": PATH,
        "cwd": PATH})
    return (((o or {}).get("hookSpecificOutput") or {})
            .get("additionalContext") or "")


_q126, _w126 = daemon.quiet_console_for, daemon.watch_rc_link
try:
    daemon.quiet_console_for = lambda *a, **k: None
    daemon.watch_rc_link = lambda *a, **k: None
    _n126 = len(_lines126())
    _wall126(2)
    _a126 = _start126()
    check("the 3rd of 4: the thread, and no WALL line, no journal line",
          ("TASK-126" in _a126, "WALL:" in _a126,
           len(_lines126()) - _n126), (True, False, 0))
    _wall126(3)
    _a126 = _start126()
    check("the 4th of 4: the WALL line, with the count and the wall",
          ("WALL: compaction 4 of 4 for this session" in _a126,
           "the 4th compaction, the cap (project)" in _a126,
           "end the turn" in _a126), (True, True, True))
    check("first, ahead of the thread it is handed back",
          (_a126.find("WALL:") >= 0
           and _a126.find("WALL:") < _a126.find("You have just been "
                                                "compacted")
           < _a126.find("TASK-126")), True)
    _l126 = _lines126()
    check("and one journal line, saying the handover is decided at the Stop",
          (len(_l126) - _n126, "4 of 4" in ((_l126[-1].get("text") or "")
                                            if _l126 else ""),
           "decided at that Stop" in ((_l126[-1].get("text") or "")
                                      if _l126 else "")), (1, True, True))
    _wall126(4)
    check("past the wall it is still said: 5 of 4",
          "WALL: compaction 5 of 4" in _start126(), True)
    _wall126(3, idle=True)
    check("CONTROL: a compaction the client did while idle is not counted",
          "WALL:" in _start126(), False)
    _wall126(3)
    with daemon._lock:
        daemon.STATE.get("last_task_text", {}).pop(daemon.norm(PATH), None)
    check("with no thread to hand back the line still goes",
          _start126().count("WALL: compaction 4 of 4"), 1)
    _wall126(3)
    daemon.STATE["pids"][_pkey()].pop("model_req", None)
    _a126 = _start126()
    _v126 = daemon.wall_view(daemon.STATE["sessions"].get(daemon.session_key(
        {"role": "executor", "session_id": SID})) or {}, PATH)
    check("PRECONDITION: no window is known, only assumed - the Stop "
          "decides nothing on it", (not _v126.get("window")
                                    or bool(_v126.get("assumed"))), True)
    check("CONTROL: and the landing says nothing on it either",
          "WALL:" in _a126, False)
    _wall126(3)
    with daemon._lock:
        daemon.STATE["last_session"][_pkey()] = "another-session"
    _o126, _ = daemon.handle_event({
        "hook_event_name": "SessionStart", "role": "executor",
        "session_id": "another-session", "source": "compact",
        "project_dir": PATH, "cwd": PATH})
    check("CONTROL: another session's compactions are not this one's",
          "WALL:" in (((_o126 or {}).get("hookSpecificOutput") or {})
                      .get("additionalContext") or ""), False)
finally:
    daemon.quiet_console_for, daemon.watch_rc_link = _q126, _w126
print("   the Stop that follows hands it over, and the demand names that wall")
_wall126(3)
daemon.STATE["compactions"][_pkey()][-1]["landed"] = 60000
_p126 = daemon.plan_for(sess(100000), PATH)
check("PRECONDITION: the Stop decides the handover at the 4th",
      (_p126.get("do"), "the 4th compaction" in (_p126.get("why") or "")),
      ("handover", True))
daemon.note_handover_pending(PATH, "executor", _p126.get("why"), sid=SID)
_d126 = ((daemon.with_handoff_demand(None, PATH, "executor")
          .get("hookSpecificOutput") or {}).get("additionalContext") or "")
check("the demand names the wall it was decided on, not \"five\"",
      ("BEING HANDED OVER" in _d126, "the 4th compaction, the cap (project)"
       in _d126, "five" in _d126.lower()), (True, True, False))
daemon.clear_handover_pending(PATH, "executor")
_d126 = ((daemon.with_handoff_demand(None, PATH, "executor")
          .get("hookSpecificOutput") or {}).get("additionalContext") or "")
check("with no record of why, it says so plainly and invents no number",
      ("this session is at its wall" in _d126, "five" in _d126.lower()),
      (True, False))


print("\n127. the end of the state file says what it really cut (E4)")
print("   _clip_tail said length - limit, and cut more: the mark's own")
print("   length and the part line before the first whole one. The")
print("   receiver is the text it returns - what it carries plus the")
print("   count it says must be the whole, and within the limit.")
_t127 = "".join("row %03d %s\n" % (_i, "s" * 90) for _i in range(300))
_rx127 = _re.compile(r"\[\.\.\. (\d+) earlier characters not carried\]\n")


def _sum127(text, limit):
    """(marked, said + carried, carried is the text's end, within limit)"""
    out = daemon._clip_tail(text, limit)
    m = _rx127.match(out)
    kept = out[m.end():] if m else out
    return (bool(m), int(m.group(1)) + len(kept) if m else -1,
            text.endswith(kept), len(out) <= limit)


check("lines: the count and what is carried add up to the whole",
      _sum127(_t127, 4000), (True, len(_t127), True, True))
check("and what is carried starts at a line start",
      daemon._clip_tail(_t127, 4000).split("\n", 1)[1][:4], "row ")
_l127 = "x" * 9000        # one line: there is no line start to go to
check("one long line: the same sum",
      _sum127(_l127, 4000), (True, len(_l127), True, True))
_w127 = "y" * 10003       # the count is five digits wide, the cut four
check("a count narrower than the text's length still adds up",
      _sum127(_w127, 50), (True, len(_w127), True, True))
check("a text within the limit comes back untouched",
      daemon._clip_tail("short\n", 4000), "short\n")


print("\n128. an index of handoffs is not a handoff (E4)")
print("   docs/handoff-index.md lists every handoff and is rewritten with")
print("   each, so it is the freshest file whose name says handoff. The")
print("   executor is never to be told to write its handover there. The")
print("   receiver is project_handoff_file's own answer, on folders this")
print("   case makes under the suite's temp.")
_d128 = os.path.join(TMP, "p128")
os.makedirs(os.path.join(_d128, "docs"))


def _put128(rel, body, age):
    _f = os.path.join(_d128, rel)
    with open(_f, "w", encoding="utf-8") as _fh:
        _fh.write(body)
    _t = time.time() - age
    os.utime(_f, (_t, _t))


def _ans128():
    return os.path.basename(daemon.project_handoff_file(
        daemon.norm(_d128), "executor")[0])


_put128("BRIDGE_HANDOFF.md", "# handoff\n", 3600)
_put128(os.path.join("docs", "handoff-index.md"), "# index\n", 60)
_put128("CLAUDE.md", "Read `BRIDGE_HANDOFF.md`; the chain is "
        "`docs/handoff-index.md`.\n", 7200)
# this one held before E4 as well: a name is joined to the root and the
# scan reads the root only - it guards against a fix that would follow
# the folder the rules write
check("(a) as in this repo: the index in docs/ is not the answer",
      _ans128(), "BRIDGE_HANDOFF.md")
_put128("HANDOFF_INDEX.md", "# index\n", 30)
_put128("CLAUDE.md", "Read `BRIDGE_HANDOFF.md`; the chain is "
        "`HANDOFF_INDEX.md`.\n", 7200)
check("(b) an index the rules NAME, at the root and newer, is not either",
      _ans128(), "BRIDGE_HANDOFF.md")
_put128("CLAUDE.md", "Read `BRIDGE_HANDOFF.md`.\n", 7200)
check("(c) nor one the root's scan finds that the rules do not name",
      _ans128(), "BRIDGE_HANDOFF.md")
_put128("BRIDGE_HANDOFF_Part2.md", "# the live one\n", 10)
# a file the rules do not name is no longer taken for its clock at all
# (8.140, case 157), so the control is a newer real handoff they DO name
_put128("CLAUDE.md", "Read `BRIDGE_HANDOFF_Part2.md`, before it "
        "`BRIDGE_HANDOFF.md`; the chain is `HANDOFF_INDEX.md`.\n", 7200)
check("CONTROL: a newer real handoff the rules name is still taken",
      _ans128(), "BRIDGE_HANDOFF_Part2.md")


print("\n129. the canon under rule 36 without raising its 15 000: the bridge's")
print("     gates in HONESTY_GATES.md beside it, rule 29's mechanism in QUIET.md")
print("     The owner's \"Ok\" of 2026-10-12 to the planner: not a higher limit,")
print("     the description of mechanisms out of the canon. The receiver is the")
print("     text a session is handed - honesty_text() - and the files beside it.")
print("     FAILURE WOULD LOOK LIKE: the gates still in the canon, a canon that")
print("     links a file nobody packs or publishes, or the Russian published.")
print("     -> DECISIONS.md 8.82")
import runpy as _rp129                                       # noqa: E402
_root129 = os.path.dirname(daemon.HONESTY)
_canon129 = daemon.honesty_text()
_gp129 = os.path.join(_root129, "HONESTY_GATES.md")
_gates129 = read_or_fail(_gp129, "HONESTY_GATES.md")
# Words the section carries in either language, so the check is the same in
# this repository, in an unpacked package and in the public tree.
_moved129 = ("bridge-logs/DEBT.md", "NO FRAMES", "Debt closed",
             "Checked: no artifacts", "`[FRAMES]`")
check("the gates file is beside the canon, and the canon names it",
      (bool(_gates129), "`HONESTY_GATES.md`" in _canon129), (True, True))
check("what the section says is in the gates file and not in the canon",
      ([w for w in _moved129 if w not in _gates129],
       [w for w in _moved129 if w in _canon129]), ([], []))
_vp129 = {}
exec(compile(read_or_fail(os.path.join(_root129, "verify_package.py"),
                          "verify_package.py") or "FILES = []",
             "verify_package.py", "exec"), _vp129)
check("it travels in the package, as QUIET.md does",
      "source/HONESTY_GATES.md" in _vp129.get("FILES", []), True)
_en129 = os.path.join(_root129, "HONESTY.en.md")
if os.path.isfile(os.path.join(_root129, "make_public.py")):
    _mp129 = _rp129.run_path(os.path.join(_root129, "make_public.py"),
                             run_name="make_public_read")
    check("the public tree gets it from its English twin, and only from it",
          [os.path.basename(s) for s, r, _l in _mp129["plan"]()
           if r == "HONESTY_GATES.md"], ["HONESTY_GATES.en.md"])
    check("and the Russian original is never published",
          "HONESTY_GATES.md" in _mp129["NEVER"], True)
    _ent129 = read_or_fail(_en129, "HONESTY.en.md")
    _ge129 = read_or_fail(os.path.join(_root129, "HONESTY_GATES.en.md"),
                          "HONESTY_GATES.en.md")
    _qe129 = read_or_fail(os.path.join(_root129, "QUIET.en.md"),
                          "QUIET.en.md")
    check("the English canon names it too, and its section moved the same",
          ("`HONESTY_GATES.md`" in _ent129,
           [w for w in _moved129 if w not in _ge129],
           [w for w in _moved129 if w in _ent129]), (True, [], []))
    print("   rule 29 keeps its norm, its check and its link; how a window is")
    print("   kept quiet is QUIET.md's, word for word as the canon said it")
    _r29e = _ent129[_ent129.find("\n29. **"):_ent129.find("\n30. **")]
    check("the English rule 29: norm, check and link, and no mechanism",
          ("Nobody should see a single" in _r29e, "*Check:*" in _r29e,
           "`QUIET.md`" in _r29e, "Coordinates are not" in _r29e),
          (True, True, True, False))
    check("and the mechanism stands in QUIET.en.md, once",
          re.sub(r"\s+", " ", _qe129).count(
              "Coordinates are not a mechanism: the OS silently clamps "
              "them."), 1)
else:
    print("   (the public half not counted: no make_public.py beside this")
    print("   suite - the public tree or an unpacked package)")


print("\n130. a window on a Mac is a tmux pane, and its login is the token file")
print("   (8.86) - through a FAKE tmux and a stand-in client, so the PC runs")
print("   it too; on a Mac with tmux the same calls are the real thing")
from bridgecore import hook as _hook130                          # noqa: E402
_need130 = ("read_token", "tmux_command", "tmux_seat", "_launch_tmux",
            "TmuxSeat", "SEATS", "TOKEN_FILE", "TMUX")
_have130 = ([n for n in _need130 if hasattr(sessions, n)]
            + (["window_pid_from_env"]
               if hasattr(_hook130, "window_pid_from_env") else []))
check("the window layer has its tmux branch, its token reader and the hook's "
      "window pid", _have130, list(_need130) + ["window_pid_from_env"])
if len(_have130) == len(_need130) + 1:
    _d130 = os.path.join(TMP, "case130")
    os.makedirs(_d130)
    _tf130 = os.path.join(_d130, "token")
    _SECRET130 = "sk-ant-oat01-Case130" + "Q" * 40

    def _tok130(raw):
        with open(_tf130, "wb") as fh:
            fh.write(raw)
        return sessions.read_token(_tf130)

    print("   the reader: what the owner may paste, and what it must refuse")
    check("no file at all is no token and no trouble - this PC",
          sessions.read_token(os.path.join(_d130, "absent")), ("", ""))
    check("the token alone", _tok130(_SECRET130.encode()), (_SECRET130, ""))
    check("the whole line setup-token prints, with its newline",
          _tok130(("export CLAUDE_CODE_OAUTH_TOKEN=%s\n" % _SECRET130)
                  .encode()), (_SECRET130, ""))
    check("the assignment without export, quoted, with blanks around",
          _tok130(('  CLAUDE_CODE_OAUTH_TOKEN="%s"  \r\n\n' % _SECRET130)
                  .encode()), (_SECRET130, ""))
    _bad130 = [
        (b"", "is empty"),
        (b"  \n\t\n", "is empty"),
        (b"export CLAUDE_CODE_OAUTH_TOKEN=\n", "is empty"),
        ((_SECRET130 + "\n" + _SECRET130).encode(),
         "holds 2 lines, not one token"),
        ((_SECRET130[:20] + " " + _SECRET130[20:]).encode(),
         "is broken: a space at character 21 of %d" % (len(_SECRET130) + 1)),
        ((_SECRET130 + "\x07").encode(), "is broken: a control character"),
        ((_SECRET130 + "é").encode("utf-8"),
         "is broken: a non-ASCII character"),
        (b"\xff\xfe" + _SECRET130.encode(), "is not UTF-8 text"),
    ]
    _got130 = [_tok130(raw) for raw, _w in _bad130]
    check("each broken file: no token, and why",
          [(t, w.startswith(want)) for (t, w), (_r, want)
           in zip(_got130, _bad130)], [("", True)] * len(_bad130))
    check("and the why never quotes the file - half a token is a secret",
          [w for _t, w in _got130 if "Case130" in w or "QQQQ" in w], [])

    print("   the hook's window pid: the window's word, only from its own window")
    _tree130 = {500: 400, 400: 300, 300: 200, 200: 1}   # 500 is the hook
    _par130 = _tree130.get
    check("the client as grandparent or parent: through one shell, directly",
          [_hook130.window_pid_from_env({"BRIDGE_WINDOW_PID": w}, 500,
                                        _par130) for w in ("300", "400")],
          [300, 400])
    check("a hook of a `claude -p` (400) run in the window's shell (300): "
          "the window's client 200 is not its window",
          _hook130.window_pid_from_env({"BRIDGE_WINDOW_PID": "200"}, 500,
                                       _par130), 0)
    check("no variable, a word, a dead chain: 0 each",
          [_hook130.window_pid_from_env(e, 500, _par130) for e in
           ({}, {"BRIDGE_WINDOW_PID": "x"}, {"BRIDGE_WINDOW_PID": "-3"})]
          + [_hook130.window_pid_from_env({"BRIDGE_WINDOW_PID": "9"}, 7,
                                          _par130)], [0, 0, 0, 0])

    print("   the window: launch, screen, Enter and stop through tmux")
    _proj130 = os.path.join(_d130, "proj")
    os.makedirs(_proj130)
    _sock130 = os.path.join(_d130, "tmuxtmp")
    _calls130 = os.path.join(_d130, "calls.jsonl")
    _fake130 = os.path.join(_d130, "fake_tmux.py")
    _stub130 = os.path.join(_d130, "client_stub.py")
    with open(_fake130, "w", encoding="utf-8") as fh:
        fh.write(r'''import json, os, signal, subprocess, sys, time
a = sys.argv[1:]
label, sub, rest = a[1], a[2], a[3:]
d = os.path.join(os.environ["TMUX_TMPDIR"],
                 "tmux-%d" % getattr(os, "getuid", lambda: 0)())
os.makedirs(d, exist_ok=True)
seat = os.path.join(d, label)
with open(sys.argv[0] + ".calls", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"label": label, "sub": sub, "rest": rest,
                         "TMUX": "TMUX" in os.environ}) + "\n")
if sub == "new-session":
    i = rest.index("bridge-window")
    kw = ({"creationflags": 0x08000000} if os.name == "nt"
          else {"start_new_session": True})
    p = subprocess.Popen(rest[i + 1:], cwd=rest[rest.index("-c") + 1],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **kw)
    with open(seat, "w") as fh:
        json.dump({"pid": p.pid}, fh)
    print("%d %%7" % p.pid)
elif not os.path.exists(seat):
    sys.stderr.write("no server running on " + seat + "\n")
    sys.exit(1)
elif sub == "list-panes":
    print("%d %%7" % json.load(open(seat))["pid"])
elif sub == "capture-pane":
    print("  the screen of the pane   \n\n   -- box --  \n> \n\n")
elif sub == "kill-server":
    try:
        os.kill(json.load(open(seat))["pid"], signal.SIGTERM)
    except OSError:
        pass
    os.remove(seat)
''')
    with open(_stub130, "w", encoding="utf-8") as fh:
        fh.write(r'''import json, os, sys, time
out = sys.argv[1]
with open(out + ".tmp", "w") as fh:
    json.dump({"pid": os.getpid(), "cwd": os.getcwd(),
               "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
               "role": os.environ.get("BRIDGE_ROLE")}, fh)
os.replace(out + ".tmp", out)
time.sleep(60)
''')
    _calls130 = _fake130 + ".calls"

    def _client130(path, wait=20):
        end = time.time() + wait
        while time.time() < end:
            try:
                with open(path) as fh:
                    return _json.load(fh)
            except (OSError, ValueError):
                time.sleep(0.1)
        return {}

    def _log130():
        try:
            with open(_calls130, encoding="utf-8") as fh:
                return [_json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    _saved130 = {n: getattr(sessions, n, None) for n in
                 ("TMUX", "TOKEN_FILE", "TOKEN_FILE_ON", "build_command",
                  "ensure_marks")}
    _jsaved130 = store.journal
    _env130 = {n: os.environ.get(n) for n in
               ("TMUX_TMPDIR", "TMUX", "CLAUDE_CODE_OAUTH_TOKEN")}
    _lines130, _pids130 = [], []
    try:
        sessions.TMUX = [sys.executable, _fake130]
        sessions.TOKEN_FILE = _tf130
        sessions.TOKEN_FILE_ON = True     # the file ON; the default is 141
        sessions.ensure_marks = lambda project, role: []
        os.environ["TMUX_TMPDIR"] = _sock130
        os.environ["TMUX"] = "/tmp/somebody-elses,1,0"
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "inherited-130"
        store.journal = lambda *a, **k: _lines130.append(a)
        with open(_tf130, "w") as fh:
            fh.write("export CLAUDE_CODE_OAUTH_TOKEN=%s\n" % _SECRET130)
        _real_build130 = _saved130["build_command"]

        try:
            sessions.launch(_proj130, "executor")
            _guard130 = "launched"
        except RuntimeError as exc:
            _guard130 = "refused" if "BRIDGE_REAL_CLIENT" in str(exc) else exc
        check("the real client is refused BEFORE tmux is asked for a window",
              (_guard130, [c["sub"] for c in _log130()]), ("refused", []))

        _out130 = {"executor": os.path.join(_d130, "client-executor.json"),
                   "planner": os.path.join(_d130, "client-planner.json")}
        sessions.build_command = (
            lambda project, role, *a, **k:
            [sys.executable, _stub130, _out130[role]])
        _pid130 = sessions.launch(_proj130, "executor")
        _pids130.append(_pid130)
        _cl130 = _client130(_out130["executor"])
        _new130 = [c for c in _log130() if c["sub"] == "new-session"]
        check("launch answers the CLIENT's pid, as Popen does on Windows",
              (_pid130, _cl130.get("pid")), (_cl130.get("pid"), _pid130))
        check("the client gets the FILE's token over the inherited one, its "
              "role, and the project as its folder",
              (_cl130.get("token") == _SECRET130, _cl130.get("role"),
               os.path.normcase(os.path.realpath(_cl130.get("cwd") or ""))),
              (True, "executor",
               os.path.normcase(os.path.realpath(_proj130))))
        _rest130 = _new130[0]["rest"] if _new130 else []
        check("one detached session, in the project, its pane the wrapper "
              "that execs the client in its own place, no outer TMUX",
              (len(_new130), _rest130[:2], "-c" in _rest130
               and _rest130[_rest130.index("-c") + 1] == _proj130,
               _rest130[-7:-3] if len(_rest130) > 7 else _rest130,
               _rest130[-3:], _new130[0]["TMUX"] if _new130 else None),
              (1, ["-d", "-s"], True,
               ["/bin/sh", "-c", sessions.TMUX_WRAP, "bridge-window"],
               [sys.executable, _stub130, _out130["executor"]], False))
        _key130 = (store.norm(_proj130), "executor")
        _seat130 = sessions.PROCS.get(_key130)
        check("PROCS holds a seat that polls like a Popen while it lives",
              (type(_seat130).__name__, getattr(_seat130, "pid", None),
               _seat130.poll() if _seat130 else "none"),
              ("TmuxSeat", _pid130, None))
        sessions.SEATS.clear()   # asked of tmux, as after a daemon restart
        check("the screen is capture-pane's, shaped as the console's is",
              sessions.console_screen(_pid130),
              "the screen of the pane\n\n   -- box --\n>")
        check("Enter is send-keys to that pane, and nothing else is sent",
              (sessions.console_answer(_pid130),
               [c["rest"] for c in _log130() if c["sub"] == "send-keys"]),
              (True, [["-t", "%7", "Enter"]]))
        check("a pid that is no pane of ours has no seat",
              sessions.tmux_seat(_pid130 + 100000), None)
        check("a good token file writes no journal line", _lines130, [])
        _lab130 = _new130[0]["label"] if _new130 else ""
        check("stop: the client gone, and the server it leaves ended too",
              (sessions.stop(_proj130, "executor", wait=15),
               sessions.pid_alive(_pid130),
               [c["sub"] for c in _log130()
                if c["label"] == _lab130][-1:],
               _lab130 in os.listdir(os.path.join(
                   _sock130, "tmux-%d" % getattr(os, "getuid",
                                                 lambda: 0)()))),
              (True, False, ["kill-server"], False))

        print("   a broken file: one journal line, never the token, and the")
        print("   window starts with what it would have had without the file")
        with open(_tf130, "w") as fh:
            fh.write(_SECRET130[:30] + " " + _SECRET130[30:])
        _pid130b = sessions.launch(_proj130, "planner")
        _pids130.append(_pid130b)
        _cl130b = _client130(_out130["planner"])
        check("one warn line naming the file and why, no part of the token",
              [(a[0], a[2:5], _tf130 in a[1], "is broken: a space" in a[1],
                "Case130" in a[1] or "QQQQ" in a[1]) for a in _lines130],
              [("session", ("proj", "planner", "warn"), True, True, False)])
        check("and the planner's client has no token from it",
              (_cl130b.get("pid") == _pid130b, _cl130b.get("token")),
              (True, "inherited-130"))
        _lab130b = [c["label"] for c in _log130()
                    if c["sub"] == "new-session"][-1]
        check("stop ALONE (8.31): the client goes, the server is left to "
              "go with its last pane",
              (sessions.stop(_proj130, "planner", wait=15, tree=False),
               sessions.pid_alive(_pid130b),
               [c["sub"] for c in _log130() if c["label"] == _lab130b
                and c["sub"] == "kill-server"]),
              (True, False, []))
        sessions._tmux(_lab130b, "kill-server")
    finally:
        for _n, _v in _saved130.items():
            setattr(sessions, _n, _v)
        store.journal = _jsaved130
        for _n, _v in _env130.items():
            if _v is None:
                os.environ.pop(_n, None)
            else:
                os.environ[_n] = _v
        for _p in _pids130:
            if sessions.pid_alive(_p):
                sessions.terminate_and_wait(_p, 10)
        sessions.SEATS.clear()

    print("   and Windows keeps its console: no seam, no tmux")
    check("tmux_command with TMUX unset",
          sessions.tmux_command() if os.name == "nt" else None, None)


print("\n131. one bridge per project folder, across machines (8.87) - the lock")
print("   is fought over by real processes: this one and holders it starts,")
print("   some calling themselves another machine")
try:
    from bridgecore import projlock as _pl131                    # noqa: E402
except ImportError:
    _pl131 = None
check("the project lock exists", _pl131 is not None, True)
if _pl131 is not None:
    import socket as _sock131
    import threading
    _d131 = os.path.join(TMP, "case131")
    os.makedirs(_d131)
    _holder131 = os.path.join(_d131, "holder.py")
    with open(_holder131, "w", encoding="utf-8") as fh:
        fh.write(r'''import json, os, sys, threading, time
sys.path.insert(0, %r)
from bridgecore import projlock
_t = threading.Timer(90, os._exit, (3,))      # never outlives the case
_t.daemon = True
_t.start()
a = json.loads(sys.argv[1])
projlock.MACHINE = a.get("machine")
projlock.STALE_SEC = a.get("stale", projlock.STALE_SEC)
projlock.BEAT_SEC = a.get("beat", 1e9)
while a.get("go") and not os.path.exists(a["go"]):
    time.sleep(0.002)
for step in a["steps"]:
    if step == "acquire":
        ok, why = projlock.acquire(a["proj"])
        print(json.dumps({"ok": ok, "why": why, "pid": os.getpid()}), flush=True)
    elif step.startswith("sleep:"):
        time.sleep(float(step[6:]))
    elif step == "wait":
        sys.stdin.readline()
    elif step == "release":
        projlock.release()
        print(json.dumps({"released": True}), flush=True)
''' % os.path.dirname(os.path.abspath(__file__)))
    _procs131 = []

    def _start131(proj, steps, **kw):
        kw.update(proj=proj, steps=steps)
        env = dict(os.environ)
        env["BRIDGE_DATA"] = os.path.join(_d131, "data-%d" % len(_procs131))
        p = subprocess.Popen([sys.executable, _holder131, _json.dumps(kw)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, text=True)
        _procs131.append(p)
        return p

    def _line131(p):
        try:
            return _json.loads(p.stdout.readline() or "{}")
        except ValueError:
            return {}

    def _go131(p):
        p.stdin.write("\n")
        p.stdin.flush()

    def _file131(proj):
        for _ in range(20):     # a holder's beat may be replacing it
            try:
                with open(_pl131.lock_path(proj), encoding="utf-8") as fh:
                    return _json.loads(fh.read())
            except (OSError, ValueError):
                time.sleep(0.05)
        return None

    def _proj131(name):
        p = os.path.join(_d131, name)
        os.makedirs(p)
        return p

    _saved131 = {n: getattr(_pl131, n) for n in
                 ("MACHINE", "STALE_SEC", "BEAT_SEC")}
    _ssaved131 = {n: getattr(sessions, n) for n in
                  ("ensure_marks", "build_command")}
    _jsaved131 = store.journal
    _lines131 = []
    _host131 = _sock131.gethostname()
    _OTHER131 = "mac1-case131"
    try:
        _pl131.BEAT_SEC = 1e9          # no beat of this process moves a
        _pl131.STALE_SEC = 1.0         # claim unless a check says so
        store.journal = lambda *a, **k: _lines131.append((a, k))

        print("   a free folder is taken, and the claim says by whom")
        _pa131 = _proj131("a")
        _ok131 = _pl131.acquire(_pa131)
        _f131 = _file131(_pa131) or {}
        check("taken, in bridge-logs, naming this machine and pid",
              (_ok131, _f131.get("machine"), _f131.get("pid"),
               _f131.get("beat"), bool(_f131.get("since"))),
              ((True, ""), _host131, os.getpid(), 0, True))
        _h131 = _start131(_pa131, ["acquire"])
        _r131 = _line131(_h131)
        check("a second process on the same machine is refused, and the "
              "refusal names machine, pid and since when",
              (_r131.get("ok"), _host131 in _r131.get("why", ""),
               "(pid %d)" % os.getpid() in _r131.get("why", ""),
               _f131.get("since", "?") in _r131.get("why", ""),
               "nothing was started" in _r131.get("why", "")),
              (False, True, True, True, True))
        _pl131.release(_pa131)
        _h131 = _start131(_pa131, ["acquire", "wait", "release"])
        _r131 = _line131(_h131)
        check("released, it is taken by the next one",
              (os.path.exists(_pl131.lock_path(_pa131)) and _r131.get("ok"),
               (_file131(_pa131) or {}).get("pid") == _h131.pid), (True, True))
        _go131(_h131)
        check("and given back by it, the file goes",
              (_line131(_h131), os.path.exists(_pl131.lock_path(_pa131))),
              ({"released": True}, False))

        print("   the same machine: a pid that is not running is dead at once")
        _h131 = _start131(_pa131, ["acquire", "wait"])
        _line131(_h131)
        _h131.kill()
        _h131.wait(10)
        del _lines131[:]
        check("taken over at the first try, and said",
              (_pl131.acquire(_pa131),
               [(k.get("level"), "pid %d is not running" % _h131.pid in a[1],
                 "took the project lock over" in a[1])
                for a, k in _lines131]),
              ((True, ""), [("warn", True, True)]))
        _pl131.release(_pa131)

        print("   another machine: its pid means nothing here, only its")
        print("   heartbeat standing still for STALE_SEC of THIS clock")
        _h131 = _start131(_pa131, ["acquire", "wait"], machine=_OTHER131)
        _line131(_h131)
        _first131 = _pl131.acquire(_pa131)
        _h131.kill()
        _h131.wait(10)
        _dead131 = _pl131.acquire(_pa131)
        check("refused at first sight, and still refused with its pid dead "
              "on THIS machine",
              (_first131[0], "being watched" in _first131[1],
               _OTHER131 in _first131[1], _dead131[0],
               "has not moved for 0 s of the 1" in _dead131[1]),
              (False, True, True, False, True))
        time.sleep(1.2)
        del _lines131[:]
        check("its heartbeat still for longer than STALE_SEC: taken over, said",
              (_pl131.acquire(_pa131),
               [(k.get("level"), _OTHER131 in a[1],
                 "has not moved for 1 s" in a[1]) for a, k in _lines131]),
              ((True, ""), [("warn", True, True)]))
        _pl131.release(_pa131)
        _h131 = _start131(_pa131, ["acquire", "wait"], machine=_OTHER131,
                          beat=0.2)
        _line131(_h131)
        _first131 = _pl131.acquire(_pa131)
        time.sleep(1.3)
        _later131 = _pl131.acquire(_pa131)
        check("a heartbeat that moves is never dead, however long it is "
              "watched",
              (_first131[0], _later131[0], "being watched" in _later131[1],
               (_file131(_pa131) or {}).get("beat", 0) >= 3),
              (False, False, True, True))
        _go131(_h131)
        _h131.wait(10)

        print("   a bridge that slept through it learns it lost, and does not")
        print("   write its claim back")
        _pb131 = _proj131("b")
        _pl131.acquire(_pb131)
        _h131 = _start131(_pb131, ["acquire", "sleep:0.7", "acquire", "wait"],
                          machine=_OTHER131, stale=0.5, beat=0.2)
        _taken131 = (_line131(_h131).get("ok"), _line131(_h131).get("ok"))
        del _lines131[:]
        _pl131.beat()
        check("the other took it; this one's beat drops it and says so",
              (_taken131, _pl131.lock_path(_pb131) in _pl131.HELD,
               (_file131(_pb131) or {}).get("pid") == _h131.pid,
               [(k.get("level"), "no longer this bridge's" in a[1],
                 _OTHER131 in a[1]) for a, k in _lines131]),
              ((False, True), False, True, [("warn", True, True)]))
        check("and its next try is refused, naming the other machine",
              (lambda r: (r[0], _OTHER131 in r[1]))(_pl131.acquire(_pb131)),
              (False, True))

        print("   no window is started, and no hook repaired, in a folder")
        print("   another bridge holds")
        _marks131 = []
        sessions.ensure_marks = lambda project, role: _marks131.append(role)
        sessions.build_command = lambda *a, **k: _marks131.append("built")
        try:
            sessions.launch(_pb131, "executor")
            _launch131 = "launched"
        except RuntimeError as exc:
            _launch131 = _OTHER131 in str(exc)
        except Exception as exc:        # speak, do not crash the suite
            _launch131 = "crashed: %r" % exc
        check("launch refuses before ensure_marks, naming the holder",
              (_launch131, _marks131), (True, []))
        _go131(_h131)
        _h131.wait(10)

        print("   a race: six processes at once on a free folder, and two")
        print("   takers at once on a dead claim - one winner each time")
        _pc131 = _proj131("c")
        _gate131 = os.path.join(_d131, "go-c")
        _race131 = [_start131(_pc131, ["acquire", "wait"], go=_gate131)
                    for _ in range(6)]
        time.sleep(1.0)
        open(_gate131, "w").close()
        _won131 = [_line131(p) for p in _race131]
        _wins131 = [r["pid"] for r in _won131 if r.get("ok")]
        check("exactly one of six won, and the claim is the winner's",
              (len(_wins131), (_file131(_pc131) or {}).get("pid") in _wins131,
               all("(pid %d)" % _wins131[0] in r.get("why", "")
                   for r in _won131 if not r.get("ok")) if _wins131 else None),
              (1, True, True))
        for p in _race131:
            _go131(p)
        _pd131 = _proj131("d")
        os.makedirs(os.path.dirname(_pl131.lock_path(_pd131)))
        with open(_pl131.lock_path(_pd131), "w", encoding="utf-8") as fh:
            _json.dump({"machine": "ghost-case131", "pid": 1, "beat": 0,
                       "since": "2026-10-12 00:00:00+0300"}, fh)
        _gate131 = os.path.join(_d131, "go-d")
        _take131 = [_start131(_pd131, ["acquire", "sleep:0.5", "acquire",
                                       "wait"],
                              go=_gate131, stale=0.3, machine=m)
                    for m in (_OTHER131, None)]
        time.sleep(1.0)
        open(_gate131, "w").close()
        _tk131 = [(_line131(p), _line131(p)) for p in _take131]
        _twins131 = [b["pid"] for a, b in _tk131 if b.get("ok")]
        check("two takers of one dead claim: one winner, the claim its own, "
              "and nothing left aside",
              (len(_twins131), (_file131(_pd131) or {}).get("pid")
               in _twins131,
               [n for n in os.listdir(os.path.dirname(
                   _pl131.lock_path(_pd131)))
                if n.startswith(_pl131.NAME + ".")]),
              (1, True, []))
        for p in _take131:
            _go131(p)

        print("   the two narrow windows of a race, opened on purpose")
        _pe131 = _proj131("e")
        os.makedirs(os.path.dirname(_pl131.lock_path(_pe131)))
        _fresh131 = _json.dumps({"machine": _OTHER131, "pid": 4242,
                                 "since": "now", "beat": 7})
        with open(_pl131.lock_path(_pe131), "w", encoding="utf-8") as fh:
            fh.write(_fresh131)
        check("a taker that moved a FRESH claim (not the dead one it judged) "
              "puts it back and loses",
              (_pl131._take_away(_pl131.lock_path(_pe131), "the dead one"),
               (_file131(_pe131) or {}).get("beat")), ((False, False), 7))
        try:   # a broken take-away leaves no claim: said above, go on
            os.remove(_pl131.lock_path(_pe131))
        except FileNotFoundError:
            pass
        open(_pl131.lock_path(_pe131), "w").close()   # made, not yet written

        def _fill131():
            time.sleep(0.2)
            with open(_pl131.lock_path(_pe131), "w", encoding="utf-8") as fh:
                fh.write(_fresh131)
        _t131 = threading.Thread(target=_fill131)
        _t131.start()
        _r131 = _pl131.acquire(_pe131)
        _t131.join()
        check("a claim made but not yet written is waited for, and the "
              "refusal names who made it",
              (_r131[0], _OTHER131 in _r131[1], "(pid 4242)" in _r131[1]),
              (False, True, True))

        print("   nothing held: the heartbeat thread ends, and the next")
        print("   claim starts one (test_multipair found it outliving all)")

        def _ended131(t):
            _end = time.time() + 5
            while t is not None and t.is_alive() and time.time() < _end:
                t.join(0.2)
            return t is not None and not t.is_alive()

        def _beating131():
            t = _pl131._beating[0]
            return t, bool(t is not None and t.is_alive())

        _pf131 = _proj131("f")
        _pl131.acquire(_pf131)
        _bt131 = _pl131._beating[0]
        _pl131.release()
        _e1131 = _ended131(_bt131)
        _gotf131 = _pl131.acquire(_pf131)[0]
        _nb131, _b1131 = _beating131()
        _pl131.release()
        _e2131 = _ended131(_nb131)
        # a claim on disk that names this very process but is not held -
        # the re-take branch of acquire - is beaten too
        with open(_pl131.lock_path(_pf131), "w", encoding="utf-8") as fh:
            fh.write(_pl131._content())
        _back131 = _pl131.acquire(_pf131)[0]
        _b2131 = _beating131()[1]
        _pl131.release()
        check("nothing held: the heartbeat thread ends, and the next claim "
              "starts a new one - a fresh claim and a re-taken one alike",
              (_e1131, _gotf131, _b1131, _e2131, _back131, _b2131),
              (True, True, True, True, True, True))
    finally:
        for _n, _v in _saved131.items():
            setattr(_pl131, _n, _v)
        for _n, _v in _ssaved131.items():
            setattr(sessions, _n, _v)
        store.journal = _jsaved131
        _pl131.release()
        for p in _procs131:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(10)
            p.stdout.close()


print("\n132. the PC behind a share is found by its NAME and believed only when")
print("   the server at the address says that name back (8.88) - the resolver")
print("   and the server's answer are stand-ins, the outputs a Mac gave")
try:
    from bridgecore import netshare as _ns132                    # noqa: E402
except ImportError:
    _ns132 = None
check("the share finder exists", _ns132 is not None, True)
if _ns132 is not None:
    import socket as _sk132

    class _R132(object):
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    # what a Mac printed on 2026-10-12 (m2-names.txt), its PC's name and
    # addresses replaced by documentation ones: this file is published
    _STATUS132 = ("Using IP address of 192.0.2.5: 192.0.2.5\n"
                  "Workgroup: WORKGROUP\nServer: WORKPC\n")
    _LOOKUP132 = ("Got response from 192.0.2.5\n"
                  "IP address of WORKPC: 192.0.2.5\n")
    _calls132 = []

    def _run132(cmd, timeout=None):
        _calls132.append(cmd[1:])
        if cmd[1:] == ["status", "192.0.2.5"]:
            return _R132(0, _STATUS132)
        if cmd[1:] == ["lookup", "WORKPC"]:
            return _R132(0, _LOOKUP132)
        return _R132(68, "")

    print("   reading what smbutil says")
    check("the server's own name from its node status; nothing from a "
          "silent one",
          (_ns132.server_name("192.0.2.5", _run132),
           _ns132.server_name("192.0.2.77", _run132)), ("WORKPC", ""))
    check("candidates: mDNS first, then NetBIOS, each address once",
          _ns132.resolve("WORKPC", _run132, lookup=lambda *a: [
              (2, 1, 6, "", ("192.0.2.5", 445)),
              (2, 1, 6, "", ("192.0.2.6", 445))]),
          ["192.0.2.5", "192.0.2.6"])

    def _no_mdns132(*a):
        raise _sk132.gaierror(8, "nodename nor servname provided")

    try:
        _got132 = _ns132.resolve("WORKPC", _run132, lookup=_no_mdns132)
    except Exception as _e132:
        _got132 = "raised %r" % (_e132,)
    check("no mDNS answer: NetBIOS alone still finds it",
          _got132, ["192.0.2.5"])

    print("   the order, and who is believed")
    _net132 = {"192.0.2.5": "WORKPC", "192.0.2.7": "",
               "192.0.2.8": "NAS-BOX", "192.0.2.9": "workpc"}
    _asked132, _said132 = [], []

    def _ask132(a):
        _asked132.append(a)
        return _net132.get(a, "")

    def _find132(last, cands):
        del _asked132[:], _said132[:]
        got = _ns132.find("WORKPC", last, _ask132, lambda n: list(cands),
                          lambda t, lv: _said132.append((lv, t)))
        return got, list(_asked132), list(_said132)

    _g132 = _find132("192.0.2.5", ["192.0.2.5"])
    check("still where it was: used, asked once, nothing said",
          _g132, (("192.0.2.5", ""), ["192.0.2.5"], []))
    _g132 = _find132("192.0.2.7", ["192.0.2.5"])
    check("the old address is dead: found by name, and one line says so",
          (_g132[0][0], _g132[1], [lv for lv, _t in _g132[2]],
           "found at 192.0.2.5 by name" in _g132[0][1],
           "192.0.2.7 does not answer" in _g132[0][1]),
          ("192.0.2.5", ["192.0.2.7", "192.0.2.5"], ["log"],
           True, True))
    _g132 = _find132("192.0.2.8", ["192.0.2.8", "192.0.2.5"])
    check("the old address is now ANOTHER server: passed over, not asked "
          "twice, and named",
          (_g132[0][0], _g132[1],
           "answers as server NAS-BOX, not WORKPC" in _g132[0][1]),
          ("192.0.2.5", ["192.0.2.8", "192.0.2.5"], True))
    _g132 = _find132("", ["192.0.2.8"])
    check("the name leads only to a foreign server: NO address - nothing "
          "to mount from - and a warning naming it",
          (_g132[0][0], [lv for lv, _t in _g132[2]],
           "NAS-BOX" in _g132[0][1] and "nothing is mounted" in _g132[0][1]),
          ("", ["warn"], True))
    _g132 = _find132("", [])
    check("the name leads nowhere: no address, a warning",
          (_g132[0], [lv for lv, _t in _g132[2]]),
          (("", "WORKPC not found: no address for the name"), ["warn"]))
    _g132 = _find132("", ["192.0.2.9"])
    check("NetBIOS names are case-blind: 'workpc' is WORKPC",
          _g132[0][0], "192.0.2.9")


print("\n133. a project carried between this machine and one of the other OS")
print("   (the PC and a Mac working one folder over a share): its hooks, its")
print("   status line and its channel entry name THAT machine's Python and")
print("   bridge folder, and the launch's install puts this machine's back.")
print("   The folder is shared, so every move flips it - which is why the")
print("   project lock is taken before ensure_marks, not after (8.87)")
import io as _io133                                       # noqa: E402
import json as _json133                                   # noqa: E402
from bridgecore import install as _install133             # noqa: E402
from bridgecore import sessions as _sess133               # noqa: E402
# the other machine's interpreter and folder, in shapes check_public passes
_other133 = (("/usr/bin/python3", "/opt/bridge/source") if os.name == "nt"
             else ("C:\\path\\to\\my-python", "C:\\path\\to\\my-bridge"))
_p133 = os.path.join(TMP, "carried-project")
os.makedirs(_p133, exist_ok=True)
_install133.install(_p133, "executor", python=sys.executable)
_sp133 = os.path.join(_p133, ".claude", "settings.json")
_mp133 = os.path.join(_p133, ".mcp.json")


def _load133(p):
    with _io133.open(p, encoding="utf-8") as fh:
        return _json133.load(fh)


def _script133(name):
    """The other machine's script, by path, with ITS separator (8.122)."""
    root = _other133[1]
    sep = "\\" if root[1:2] == ":" else "/"
    return sep.join((root, "bridgecore", name + ".py"))


def _carry133():
    """What the other machine's install leaves in the shared folder."""
    py, root = _other133
    cfg = _load133(_sp133)
    for groups in cfg["hooks"].values():
        for g in groups:
            for h in g.get("hooks") or []:
                if _install133.is_bridge_hook(h):
                    h["command"] = py
                    h["args"] = [_script133("hook")]
    cfg["statusLine"]["command"] = '"%s" "%s"' % (py,
                                                 _script133("statusline"))
    cfg["env"]["PYTHONPATH"] = root
    with _io133.open(_sp133, "w", encoding="utf-8") as fh:
        _json133.dump(cfg, fh, indent=2)
    m = _load133(_mp133)
    m["mcpServers"]["bridge"]["command"] = py
    m["mcpServers"]["bridge"]["args"] = [_script133("channel")]
    m["mcpServers"]["bridge"]["env"]["PYTHONPATH"] = root
    with _io133.open(_mp133, "w", encoding="utf-8") as fh:
        _json133.dump(m, fh, indent=2)


def _here133():
    """Every place a machine's own interpreter or folder is written, as
    (hooks, status line ok, channel entry ok, PYTHONPATH ours, a trace of
    the other machine left in either file)."""
    cfg, m = _load133(_sp133), _load133(_mp133)
    text = _io133.open(_sp133, encoding="utf-8").read() + \
        _io133.open(_mp133, encoding="utf-8").read()
    return (sorted({h.get("command") for groups in cfg["hooks"].values()
                    for g in groups for h in g.get("hooks") or []
                    if _install133.is_bridge_hook(h)}),
            _install133.status_line_ok(cfg.get("statusLine")),
            _install133.channel_entry_ok(m["mcpServers"].get("bridge")),
            _install133.same_root(cfg["env"].get("PYTHONPATH")),
            any(_json133.dumps(x)[1:-1] in text for x in _other133))


def _baks133():
    return sorted(n for n in os.listdir(os.path.dirname(_sp133))
                  if n.startswith("settings.json.before-bridge-python"))


check("installed here, the project is whole and names only this machine",
      (_install133.marks_missing(_p133), _here133()),
      ([], ([sys.executable], True, True, True, False)))
_carry133()
_gaps133 = _install133.marks_missing(_p133)
print("   carried: what the other machine left reads as broken HERE")
check("the gap list names the other machine's Python and why it cannot run",
      any(_other133[0] in g and ("not on this machine" in g
                                 or "not on PATH" in g) for g in _gaps133),
      True)
check("and the other machine's bridge folder in PYTHONPATH",
      any("PYTHONPATH" in g for g in _gaps133), True)
check("the status line and the channel entry are as dead here - the gap "
      "list does not name them, the install that it sets off rewrites them",
      _here133()[1:], (False, False, False, True))
_miss133 = _sess133.ensure_marks(_p133, "executor")
check("the launch's own gate saw it, repaired, and left nothing missing",
      (_miss133 == _gaps133, _install133.marks_missing(_p133)), (True, []))
check("every mark names this machine again, and no trace of the other is "
      "left in either file",
      _here133(), ([sys.executable], True, True, True, False))
_first133 = os.path.join(os.path.dirname(_sp133), _baks133()[0]) \
    if _baks133() else ""
_firsttext133 = _io133.open(_first133, encoding="utf-8").read() \
    if _first133 else ""
check("what was rewritten is kept beside it, as the other machine left it",
      (_baks133(), _json133.dumps(_other133[0])[1:-1] in _firsttext133),
      (["settings.json.before-bridge-python"], True))
print("   and back again: the second move")
time.sleep(1.1)                     # the later backups are named by second
_carry133()
check("a second move is repaired the same way",
      (_sess133.ensure_marks(_p133, "executor") != [],
       _install133.marks_missing(_p133), _here133()),
      (True, [], ([sys.executable], True, True, True, False)))
check("the first backup is never written over (8.46) and the second move's "
      "is beside it - one more file every move, nothing prunes them",
      (len(_baks133()), bool(_first133) and _io133.open(
          _first133, encoding="utf-8").read() == _firsttext133), (2, True))


print("\n134. a Mac does not idle-sleep while the daemon runs: caffeinate holds")
print("   it for the daemon's pid and lets go when that pid exits; a closed")
print("   lid still sleeps it, which no user process can stop (8.87)")
from bridgecore import sessions as _sess134                 # noqa: E402
_ran134 = []


def _popen134(cmd, **kw):
    _ran134.append((cmd, sorted(kw)))
    return "the caffeinate"


check("on a Mac: caffeinate, idle sleep, tied to the daemon's pid",
      (_sess134.keep_awake(4321, _popen134, "darwin"), _ran134),
      ("the caffeinate", [(["/usr/bin/caffeinate", "-i", "-w", "4321"],
                           ["stderr", "stdin", "stdout"])]))
del _ran134[:]
check("anywhere else: nothing is run",
      (_sess134.keep_awake(4321, _popen134, "win32"),
       _sess134.keep_awake(4321, _popen134, "linux"), _ran134),
      (None, None, []))
_said134 = []
_was134 = _sess134.store.journal
_sess134.store.journal = lambda *a, **k: _said134.append((a, k))
try:
    def _nope134(cmd, **kw):
        raise OSError(2, "No such file or directory")
    try:
        _got134 = _sess134.keep_awake(4321, _nope134, "darwin")
    except Exception as _e134:
        _got134 = "raised %r" % (_e134,)
    check("caffeinate missing: no crash, and a warning says the Mac may sleep",
          (_got134,
           [k.get("level") for _a, k in _said134],
           "may idle-sleep" in str(_said134)),
          (None, ["warn"], True))
finally:
    _sess134.store.journal = _was134
if sys.platform == "darwin":
    print("   on this Mac, for real: the assertion is there while the pid")
    print("   lives, and caffeinate is gone once it has exited")
    _kid134 = subprocess.Popen(["/bin/sleep", "2"])
    _caf134 = _sess134.keep_awake(_kid134.pid)
    time.sleep(0.5)
    _as134 = subprocess.run(["/usr/bin/pmset", "-g", "assertions"],
                            capture_output=True, text=True).stdout
    check("pmset lists caffeinate's idle-sleep assertion",
          ("PreventUserIdleSystemSleep" in _as134
           and "pid %d(caffeinate)" % _caf134.pid in _as134), True)
    _kid134.wait()
    _end134 = time.time() + 5
    while _caf134.poll() is None and time.time() < _end134:
        time.sleep(0.1)
    check("and it is released when that pid exits", _caf134.poll(), 0)
    if _caf134.poll() is None:
        _caf134.kill()
else:
    print("   (the live half runs on a Mac)")


print("\n135. off Windows a window is stopped by its OWN process group only,")
print("   a SIGTERM it ignores is followed by a SIGKILL, a zombie is not")
print("   alive, and a Mac's process start and boot are read (8.89)")
from bridgecore import sessions as _s135                     # noqa: E402
import ctypes as _ct135                                      # noqa: E402
import struct as _st135                                      # noqa: E402
import re                                                    # noqa: E402
import signal                                                # noqa: E402


class _Os135(object):
    """os for sessions, with the process half played: `alive` is the pids
    that answer, `groups` their group ids, `deaf` the pids a SIGTERM does
    not end, `numb` the pids nothing ends."""

    name = "posix"
    WNOHANG = 1

    def __init__(self):
        self.sent, self.reaped = [], []
        self.alive, self.groups, self.deaf, self.numb = set(), {}, set(), set()

    def __getattr__(self, k):
        return getattr(os, k)

    def getpgid(self, pid):
        if pid not in self.groups:
            raise OSError(3, "No such process")
        return self.groups[pid]

    def _hit(self, pid, sig):
        if sig and pid not in self.numb and (sig == 9 or pid not in self.deaf):
            self.alive.discard(pid)

    def kill(self, pid, sig):
        if pid not in self.alive:
            raise OSError(3, "No such process")
        if sig:
            self.sent.append(("kill", pid, sig))
        self._hit(pid, sig)

    def killpg(self, pg, sig):
        self.sent.append(("killpg", pg, sig))
        for p, g in list(self.groups.items()):
            if g == pg:
                self._hit(p, sig)

    def waitpid(self, pid, opts):
        self.reaped.append(pid)
        return pid, 0


_was135 = (_s135.os, _s135.KILL_WAIT_SEC, _s135.proc_state)
_o135 = _Os135()
_s135.os = _o135
try:
    _o135.alive |= {501, 502, 503}
    _o135.groups.update({501: 501, 502: 77, 503: 503})
    _r135 = [_s135.signal_window(501, 15, True),
             _s135.signal_window(502, 15, True),
             _s135.signal_window(503, 15, False)]
    check("a window in its own group gets the group; one in its starter's "
          "group, or stopped alone, gets the signal by itself",
          (_r135, _o135.sent),
          (["group", "alone", "alone"],
           [("killpg", 501, 15), ("kill", 502, 15), ("kill", 503, 15)]))
    del _o135.sent[:]
    _o135.alive |= {504}
    _r135 = _s135.signal_window(504, 15, True)
    check("a pid whose group cannot be asked is signalled alone",
          (_r135, _o135.sent), ("alone", [("kill", 504, 15)]))

    del _o135.sent[:]
    _s135.KILL_WAIT_SEC = 0.3
    _o135.alive |= {511}
    _o135.groups[511] = 511
    _o135.deaf.add(511)
    _t135 = time.time()
    _g135 = _s135.stop(os.path.join(TMP, "w135"), "executor", pid=511,
                       wait=0.3)
    check("a window deaf to SIGTERM: SIGKILL to its group once the wait is "
          "up, and stop says it is gone",
          (_g135, _o135.sent, time.time() - _t135 < 2.0),
          (True, [("killpg", 511, 15), ("killpg", 511, 9)], True))
    del _o135.sent[:]
    _o135.alive |= {512}
    _o135.groups[512] = 512
    _o135.numb.add(512)
    check("one that survives the SIGKILL too: False, the honest answer",
          (_s135.stop(os.path.join(TMP, "w135"), "executor", pid=512,
                      wait=0.2), [s[2] for s in _o135.sent]),
          (False, [15, 9]))

    _o135.alive |= {521, 522, 523}
    _s135.proc_state = {521: "Z", 522: "", 523: None}.get
    check("a pid that answers but is a zombie is not alive, and is reaped; "
          "a live one or an unknown one stays alive",
          ([_s135.pid_alive(p) for p in (521, 522, 523)], _o135.reaped),
          ([False, True, True], [521]))
finally:
    _s135.os, _s135.KILL_WAIT_SEC, _s135.proc_state = _was135


class _Libc135(object):
    """sysctl as a Mac answers it: a kinfo_proc whose first bytes are the
    start timeval, p_stat at 36 and p_pid at 40; and kern.boottime."""

    def __init__(self, body, rc=0):
        self.body, self.rc = body, rc

    def sysctl(self, mib, n, buf, size, old, oldlen):
        _ct135.memmove(buf, self.body, len(self.body))
        size._obj.value = len(self.body)
        return self.rc

    def sysctlbyname(self, name, buf, size, old, oldlen):
        _ct135.memmove(buf, self.body[:16], 16)
        size._obj.value = 16
        return self.rc


def _kp135(sec, usec, stat, pid, n=648):
    b = bytearray(n)
    _st135.pack_into("<qi", b, 0, sec, usec)
    _st135.pack_into("<b", b, 36, stat)
    _st135.pack_into("<i", b, 40, pid)
    return bytes(b)


check("kinfo reads the start and the state of the pid it was asked about",
      _s135.kinfo(4242, _Libc135(_kp135(1791800000, 250000, 5, 4242))),
      (1791800000.25, 5))
check("and nothing it cannot stand behind: another pid's record, an empty "
      "answer, a refused call",
      [_s135.kinfo(4242, _Libc135(_kp135(1791800000, 0, 2, 4243))),
       _s135.kinfo(4242, _Libc135(b"")),
       _s135.kinfo(4242, _Libc135(_kp135(1791800000, 0, 2, 4242), rc=-1))],
      [None, None, None])
check("kern.boottime is read as a timeval, and a refused call is None",
      [_s135.mac_boottime(_Libc135(_st135.pack("<qi4x", 1788042123,
                                               318661))),
       _s135.mac_boottime(_Libc135(_st135.pack("<qi4x", 1788042123, 0),
                                   rc=-1))],
      [1788042123.318661, None])

class _Mac135(object):
    """sys, but on a Mac - every other name is the real one, for the
    daemon's threads that may ask while it stands"""

    platform = "darwin"

    def __getattr__(self, k):
        return getattr(sys, k)


_was135d = (daemon.sys, daemon.sessions.kinfo, daemon.sessions.mac_boottime)
daemon.sys = _Mac135()
daemon.sessions.kinfo = lambda pid: (1791800000.5, 2) if pid == 77 else None
daemon.sessions.mac_boottime = lambda: 1788042123.0
try:
    _pd135 = (daemon.proc_started(77), daemon.proc_started(78))
    _bd135 = daemon.boot_time()
finally:
    daemon.sys, daemon.sessions.kinfo, daemon.sessions.mac_boottime = \
        _was135d
check("on a Mac the daemon asks kinfo for a process's start, and None stays "
      "None", _pd135, (1791800000.5, None))
check("and kern.boottime for the boot", _bd135, 1788042123.0)

if os.name != "nt":
    print("   for real, on this machine")
    _c135 = subprocess.Popen(["sleep", "30"])
    _in135 = os.getpgid(_c135.pid) == os.getpgrp()
    _w135 = _s135.signal_window(_c135.pid, signal.SIGTERM, True)
    _c135.wait(5)
    check("a child left in OUR group: signalled alone, and we are still here",
          (_in135, _w135, _c135.returncode), (True, "alone", -signal.SIGTERM))
    _g135 = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"],
                             start_new_session=True)
    time.sleep(0.3)
    _ok135 = _s135.stop(os.path.join(TMP, "w135"), "executor", pid=_g135.pid)
    _left135 = subprocess.run(["pgrep", "-g", str(_g135.pid)],
                              capture_output=True, text=True).stdout.split()
    check("a window in its own group: stopped with what it started",
          (_ok135, _left135), (True, []))
    _d135 = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 30"],
                             start_new_session=True)
    time.sleep(0.3)
    _t135 = time.time()
    _ok135 = _s135.stop(os.path.join(TMP, "w135"), "executor", pid=_d135.pid,
                        wait=1)
    check("one deaf to SIGTERM: gone by SIGKILL soon after the wait",
          (_ok135, time.time() - _t135 < 1 + _s135.KILL_WAIT_SEC),
          (True, True))
    _z135 = subprocess.Popen(["true"])
    # Waited for, not slept on: under load 11 and a full swap on 2026-10-13
    # the child had not yet exited 0.5 s after it was started. The deadline
    # keeps the check able to fail.
    _zt135 = time.time() + 10
    while _s135.proc_state(_z135.pid) != "Z" and time.time() < _zt135:
        time.sleep(0.1)
    check("an exited child nobody polled: a zombie, and not alive",
          (_s135.proc_state(_z135.pid), _s135.pid_alive(_z135.pid)),
          ("Z", False))
    _z135.poll()
    if sys.platform == "darwin":
        _ps135 = subprocess.run(["ps", "-o", "lstart=", "-p",
                                 str(os.getpid())], capture_output=True,
                                text=True, env=dict(os.environ, LC_ALL="C"))
        _ls135 = time.mktime(time.strptime(_ps135.stdout.strip(),
                                           "%a %b %d %H:%M:%S %Y"))
        _bt135 = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                capture_output=True, text=True).stdout
        _bs135 = int(re.search(r"sec = (\d+)", _bt135).group(1))
        check("our start, as kinfo reads it, is the second ps prints",
              0 <= daemon.proc_started(os.getpid()) - _ls135 < 1.0, True)
        check("and the boot is the second sysctl prints",
              int(daemon.boot_time() or 0), _bs135)
else:
    print("   (the live half runs off Windows)")


print("\n136. the bridge's own headless `claude -p` - the archive's search and")
print("   the model probe - is found and logged in the way a window is, and")
print("   QuickEdit is not asked for where there is no console (8.89)")
from bridgecore import sessions as _s136                     # noqa: E402
from bridgecore import archive as _a136                      # noqa: E402
import shutil as _sh136                                      # noqa: E402


class _Os136(object):
    """os for sessions, with only its name changed"""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, k):
        return getattr(os, k)


class _Which136(object):
    """shutil, with `which` answering from a table and noting the PATH it
    was asked on"""

    def __init__(self, table):
        self.table, self.asked = table, []

    def which(self, exe, path=None):
        self.asked.append(path)
        return self.table.get((exe, path))

    def __getattr__(self, k):
        return getattr(_sh136, k)


_d136 = os.path.join(TMP, "client136")
_lb136 = os.path.join(_d136, "local-bin")
_none136 = os.path.join(_d136, "empty-bin")
for _x136 in (_lb136, _none136):
    os.makedirs(_x136, exist_ok=True)
_cl136 = os.path.join(_lb136, "claude")
with open(_cl136, "w") as _fh136:
    _fh136.write("#!/bin/sh\nexit 3\n")
os.chmod(_cl136, 0o755)
_tok136 = os.path.join(_d136, "token")
_TOKEN136 = "sk-ant-oat01-case136-not-a-real-one_Z9"


def _token136(text):
    if text is None:
        if os.path.exists(_tok136):
            os.remove(_tok136)
        return
    with open(_tok136, "w") as fh:
        fh.write(text)


_was136 = (_s136.os, _s136.shutil, _s136.LOCAL_BIN
           if hasattr(_s136, "LOCAL_BIN") else None, _s136.TOKEN_FILE,
           os.environ.get("BRIDGE_REAL_CLIENT"), _a136.subprocess,
           daemon.subprocess)
_s136.TOKEN_FILE = _tok136
_ton136 = getattr(_s136, "TOKEN_FILE_ON", None)
_s136.TOKEN_FILE_ON = True        # the file ON; the default is case 141
try:
    _w136 = _Which136({("claude", "P-136"): "/opt/on-path/claude"})
    _s136.shutil = _w136
    _s136.LOCAL_BIN = _none136
    _ce136 = getattr(_s136, "client_exe", None)
    _r136 = None
    if _ce136:
        _r136 = [_ce136("claude", {"PATH": "P-136"}),
                 _ce136("claude", {"PATH": "P-other"})]
        _s136.LOCAL_BIN = _lb136
        _r136 += [_ce136("claude", {"PATH": "P-other"}),
                  _ce136("/usr/x/claude", {"PATH": "P-136"})]
    check("the client is looked for on the PATH it will run with, then in "
          "~/.local/bin; nowhere is \"\", and a path is its own answer",
          (_r136, _w136.asked[:3]),
          (["/opt/on-path/claude", "", _cl136, "/usr/x/claude"],
           ["P-136", "P-other", "P-other"]))

    _s136.os = _Os136("posix")
    _token136(_TOKEN136 + "\n")
    _hc136 = getattr(_s136, "headless_client", None)
    _in136 = {"PATH": "P-other", "KEEP": "1"}
    _h136 = _hc136(["claude", "-p", "q"], _in136) if _hc136 else None
    check("off Windows a headless run gets the full path and the token "
          "file's token, and keeps the rest of its environment",
          _h136 and (_h136[0], _h136[1].get(_s136.TOKEN_VAR),
                     _h136[1].get("KEEP"), _h136[2], _in136),
          ([_cl136, "-p", "q"], _TOKEN136, "1", "",
           {"PATH": "P-other", "KEEP": "1"}))
    _s136.os = _Os136("nt")
    _h136 = _hc136(["claude", "-p", "q"], {"PATH": "P-136"}) \
        if _hc136 else None
    check("on Windows the name stays as it is, and the token still goes in",
          _h136 and (_h136[0], _h136[1].get(_s136.TOKEN_VAR)),
          (["claude", "-p", "q"], _TOKEN136))
    _s136.os = _Os136("posix")
    _token136("xq136half\nzq136half\n")
    _h136 = _hc136(["claude"], {"PATH": "P-other"}) if _hc136 else None
    check("a broken token file: no token, and a reason that does not quote "
          "it", _h136 and (_s136.TOKEN_VAR in _h136[1], bool(_h136[2]),
                           "q136half" in _h136[2]),
          (False, True, False))

    print("   the archive's search, as far as the process it starts")
    _seen136 = []

    class _Proc136(object):
        pid, returncode = 1360001, 0

        def communicate(self, timeout=None):
            return b"the answer, case 136", b""

        def poll(self):
            return 0

    class _Sub136(object):
        def Popen(self, cmd, *a, **kw):
            _seen136.append((list(cmd), dict(kw.get("env") or {})))
            return _Proc136()

        def __getattr__(self, k):
            return getattr(subprocess, k)

    _a136.subprocess = _Sub136()
    # the stub above is the only Popen this search can reach; the gate is
    # told the real client is meant so that it lets the search get to it
    os.environ["BRIDGE_REAL_CLIENT"] = "1"
    _p136 = os.path.join(TMP, "archive136")
    os.makedirs(os.path.join(_p136, "bridge-logs"), exist_ok=True)
    _notes136 = []
    _token136(_TOKEN136)
    _rec136 = _a136.search(_p136, "what did case 136 ask", claude="claude",
                           journal=lambda k, t, lv="log":
                           _notes136.append((k, t, lv)))
    check("the search's client: the full path and the token",
          (_rec136.get("state"), _seen136 and _seen136[-1][0][0],
           _seen136 and _seen136[-1][1].get(_s136.TOKEN_VAR),
           _seen136 and _seen136[-1][1].get("BRIDGE_NO_HOOKS")),
          ("done", _cl136, _TOKEN136, "1"))
    _token136("xq136half\nzq136half\n")
    del _seen136[:]
    _a136.search(_p136, "and again", claude="claude",
                 journal=lambda k, t, lv="log": _notes136.append((k, t, lv)))
    _warn136 = [n for n in _notes136 if n[2] == "warn"]
    check("a broken token file is said at warn, and the search goes on "
          "without one",
          (len(_warn136), bool(_warn136) and "token file" in _warn136[0][1],
           bool(_warn136) and "q136half" in _warn136[0][1],
           bool(_seen136) and _s136.TOKEN_VAR in _seen136[-1][1]),
          (1, True, False, False))

    print("   the model probe, as far as the process it starts")
    _run136 = []

    class _Done136(object):
        returncode = 0
        stdout = '{"modelUsage": {"claude-case136-x": {}}}'
        stderr = ""

    class _DSub136(object):
        def run(self, cmd, *a, **kw):
            if os.path.splitext(os.path.basename(str(list(cmd)[0])))[0] \
                    == "claude":
                _run136.append((list(cmd), dict(kw.get("env") or {})))
                return _Done136()
            return subprocess.run(cmd, *a, **kw)

        def __getattr__(self, k):
            return getattr(subprocess, k)

    daemon.subprocess = _DSub136()
    _token136(_TOKEN136)
    _f136 = daemon.probe_models(["case136"])
    check("the probe's client: the full path and the token",
          (_f136.get("case136"), _run136 and _run136[-1][0][0],
           _run136 and _run136[-1][1].get(_s136.TOKEN_VAR)),
          ("claude-case136-x", _cl136, _TOKEN136))
finally:
    (_s136.os, _s136.shutil, _lbw136, _s136.TOKEN_FILE, _rcw136,
     _a136.subprocess, daemon.subprocess) = _was136
    if _lbw136 is not None:
        _s136.LOCAL_BIN = _lbw136
    if _rcw136 is None:
        os.environ.pop("BRIDGE_REAL_CLIENT", None)
    else:
        os.environ["BRIDGE_REAL_CLIENT"] = _rcw136
    _s136.TOKEN_FILE_ON = _ton136

print("   QuickEdit, where there is no console")


class _Mac136(object):
    platform = "darwin"

    def __getattr__(self, k):
        return getattr(sys, k)


_qe136 = []
_wasq136 = (daemon.sys, daemon.sessions.console_quiet_edit,
            daemon.live_pid_of, daemon.CFG.get("nudge_console"))
daemon.sessions.console_quiet_edit = (
    lambda pid, apply=True, mode=None, timeout=20:
    (_qe136.append(pid) or {"pid": pid, "error": "no console here"}))
daemon.live_pid_of = lambda path, role: 1360002
daemon.CFG["nudge_console"] = True
daemon.sys = _Mac136()
try:
    _q136 = daemon.quiet_console_for(os.path.join(TMP, "q136"), "executor",
                                     "case 136")
finally:
    (daemon.sys, daemon.sessions.console_quiet_edit, daemon.live_pid_of,
     _n136) = _wasq136
    if _n136 is None:
        daemon.CFG.pop("nudge_console", None)
    else:
        daemon.CFG["nudge_console"] = _n136
check("off Windows nobody is asked for a console mode, and nothing is "
      "journalled as failed", (_q136, _qe136), ("no console", []))


print("\n137. off Windows the shells a window started are named by one `ps`, so")
print("   a job a replaced window leaves running has its witness there too")
print("   (8.89)")
from bridgecore import sessions as _s137                     # noqa: E402


class _Os137(object):
    name = "posix"

    def __getattr__(self, k):
        return getattr(os, k)


_PS137 = ("    1     0 /sbin/launchd\n"
          "  700   600 /bin/zsh\n"
          "  701   600 -zsh\n"
          "  702   600 /Applications/Some App.app/Contents/MacOS/Some App\n"
          "  703   600 /usr/bin/ssh-agent\n"
          "  704   600 sh\n"
          "  705   999 /bin/bash\n"
          "  706   600 node\n")


class _Sub137(object):
    """subprocess, with `ps` answered from a table"""

    def __init__(self, out, rc=0, boom=False):
        self.out, self.rc, self.boom, self.asked = out, rc, boom, []

    def run(self, cmd, *a, **kw):
        self.asked.append(list(cmd))
        if self.boom:
            raise OSError(2, "No such file or directory: 'ps'")
        return subprocess.CompletedProcess(cmd, self.rc, self.out, "")

    def __getattr__(self, k):
        return getattr(subprocess, k)


_was137 = (_s137.os, _s137.subprocess)
try:
    _s137.os = _Os137()
    _s137.subprocess = _Sub137(_PS137)
    _r137 = [_s137.child_pids(600), _s137.child_pids(600, names=()),
             _s137.child_pids(600, names=("node",))]
    check("the shells whose parent is the window - a path, a login shell, "
          "a bare name - and not ssh-agent, another parent's, or a program "
          "that is not a shell; every child when no names are given",
          _r137, [[700, 701, 704], [700, 701, 702, 703, 704, 706], [706]])
    check("from one `ps` of every process",
          (_s137.subprocess.asked or [[]])[0][:2], ["ps", "-A"])
    _s137.subprocess = _Sub137("", rc=1)
    _n137 = _s137.child_pids(600)
    _s137.subprocess = _Sub137("", boom=True)
    check("a `ps` that fails or cannot be run: None, not an empty list",
          [_n137, _s137.child_pids(600)], [None, None])
finally:
    _s137.os, _s137.subprocess = _was137

if os.name != "nt":
    print("   for real, on this machine")
    # its own group, so its sleep goes with it at the end
    _k137 = subprocess.Popen(["sh", "-c", "sleep 30; true"],
                             start_new_session=True)
    _p137 = subprocess.Popen([sys.executable, "-c",
                              "import time" + chr(10) + "time.sleep(30)"])
    time.sleep(0.3)
    _a137 = _s137.child_pids(os.getpid()) or []
    _b137 = _s137.child_pids(os.getpid(), names=()) or []
    check("a shell started here is named, a python is not; both are among "
          "the children",
          (_k137.pid in _a137, _p137.pid in _a137,
           _k137.pid in _b137 and _p137.pid in _b137), (True, False, True))
    try:
        os.killpg(_k137.pid, 9)
    except Exception:
        pass
    _p137.kill()
    for _x137 in (_k137, _p137):
        try:
            _x137.wait(5)
        except Exception:
            pass
else:
    print("   (the live half runs off Windows)")


print("\n138. three facts a Mac run found that the PC could not: one folder in")
print("   two cases is one key there too, a POSIX base is found in the text")
print("   that names it, and a refused signal is waited out (8.89)")
import posixpath as _pp138                                    # noqa: E402
from bridgecore import store as _st138                       # noqa: E402
from bridgecore import relayout as _rl138                    # noqa: E402
from bridgecore import sessions as _ss138                    # noqa: E402


class _Os138(object):
    """os, as a POSIX machine has it"""
    name = "posix"
    path = _pp138
    sep = "/"

    def __getattr__(self, k):
        return getattr(os, k)


print("   (a) the key folds case where the disk does")
_cl138 = getattr(_st138, "caseless", lambda *a: None)
check("a Mac and Windows are named as the machines whose disk is caseless, "
      "and this machine's answer is the one in force",
      (_cl138("posix", "darwin"), _cl138("nt", "win32"),
       _cl138("posix", "linux"), getattr(_st138, "CASELESS", None)),
      (True, True, False, _cl138()))
_was138 = (_st138.os, getattr(_st138, "CASELESS", None))
try:
    _st138.os = _Os138()
    _st138.CASELESS = True
    _mac138 = (_st138.norm("/Volumes/X/Old/../Game"),
               _st138.norm("/volumes/x/GAME"))
    _st138.CASELESS = False
    _lin138 = _st138.norm("/srv/Projects/Game")
finally:
    _st138.os = _was138[0]
    if _was138[1] is None:
        del _st138.CASELESS
    else:
        _st138.CASELESS = _was138[1]
check("on a Mac the two spellings are one key, and still a path",
      _mac138, ("/volumes/x/game", "/volumes/x/game"))
check("and on a machine whose disk is not caseless the case is kept",
      _lin138, "/srv/Projects/Game")
if os.name == "nt" or sys.platform == "darwin":
    check("for real here: this suite's folder in capitals is the same key",
          _st138.norm(TMP.upper()), _st138.norm(TMP))

print("   (b) the retired tree named by a POSIX path")
_was138b = _rl138.os
try:
    _rl138.os = _Os138()
    _ret138 = (
        _rl138.names_retired('{"command": "/Users/u/Br/bridge/source/hook.py"}',
                             base="/Users/u/Br"),
        _rl138.names_retired("PYTHONPATH=/Users/u/Br/bridge", base="/Users/u/Br"),
        _rl138.names_retired("/Users/u/Br/bridge.sh", base="/Users/u/Br"),
        _rl138.names_retired("/Users/u/Br/bridgework/x", base="/Users/u/Br"))
finally:
    _rl138.os = _was138b
check("a path into it and the tree itself are caught; a file and a folder "
      "that only begin like it are not",
      _ret138, (True, True, False, False))

print("   (c) a signal refused off Windows is a refusal, and the wait decides")
_was138c = (_ss138.os, _ss138.pid_alive, _ss138.signal_window,
            _ss138.KILL_WAIT_SEC)
_sent138 = []


def _refuse138(pid, sig, tree):
    _sent138.append(sig)
    raise PermissionError(1, "Operation not permitted")


try:
    _ss138.os = _Os138()
    _ss138.pid_alive = lambda pid: True
    _ss138.signal_window = _refuse138
    _ss138.KILL_WAIT_SEC = 0.2
    _t138 = time.time()
    _ans138 = _ss138.stop(TMP, "planner", pid=999999, wait=0.4)
    _took138 = time.time() - _t138
finally:
    (_ss138.os, _ss138.pid_alive, _ss138.signal_window,
     _ss138.KILL_WAIT_SEC) = _was138c
check("a window the signal could not reach is reported as not stopped",
      _ans138, False)
check("after the wait and not at once, and the SIGKILL was tried after it",
      (_took138 >= 0.5, len(_sent138)), (True, 2))


print("   (d) a project's settings and the package's own folder are found by")
print("   the same fold the keys were made with, not by normcase alone")
from bridgecore import install as _in138                     # noqa: E402
_was138d = (_st138.os, getattr(_st138, "CASELESS", None), _in138.os)
try:
    _st138.os = _Os138()
    _st138.CASELESS = True
    _pc138 = _st138.project_config(
        {"projects": {"/volumes/x/game": {"repo": "sub"}}},
        "/Volumes/X/Game").get("repo")
    _in138.os = _Os138()
    _sr138 = (_in138.same_root(_in138.ROOT.upper()),
              _in138.same_root(_in138.ROOT), _in138.same_root(TMP))
finally:
    _st138.os, _in138.os = _was138d[0], _was138d[2]
    if _was138d[1] is None:
        del _st138.CASELESS
    else:
        _st138.CASELESS = _was138d[1]
check("a setting kept under the folded key is that folder's however it is "
      "spelled", _pc138, "sub")
if os.name == "nt" or sys.platform == "darwin":
    check("the package's folder in capitals is the package's folder, asked "
          "of the disk; another folder is not", _sr138, (True, True, False))


print("\n139. the project lock follows the work: given back while the loop is off")
print("   or a person paused, so another machine can take the folder; taken")
print("   again before work resumes, a holder elsewhere holding the pair; and a")
print("   heartbeat that cannot be written is said, not swallowed (M4, 8.90)")
from bridgecore import projlock as _pl139                    # noqa: E402
_p139 = os.path.join(TMP, "case139")
os.makedirs(_p139)
_k139 = daemon.norm(_p139)
_lk139 = os.path.join(_p139, "bridge-logs", "daemon-lock.json")
_saved139 = {k: daemon.STATE.get(k) for k in ("loops", "paused", "mode",
                                              "sessions")}
_cfg139 = daemon.CFG.get("projects")
_beat139 = (_pl139.BEAT_SEC, _pl139._say)
_lf139 = getattr(daemon, "lock_follows_work", lambda: [])
_said139 = []
try:
    _pl139.BEAT_SEC = 1e9      # the beat below is this case's, not a thread's
    daemon.STATE["loops"] = {_k139: {"active": True, "iteration": 0}}
    daemon.STATE["paused"] = {}
    daemon.STATE["mode"] = "running"
    daemon.STATE["sessions"] = {"s139": {"path": _k139, "role": "executor",
                                         "state": "working"}}
    daemon.CFG["projects"] = {_k139: {}}

    print("   FAILURE WOULD LOOK LIKE: the claim kept while nothing is worked,")
    print("   so the Mac waits on a PC that is idle; or a pair working on")
    print("   with nothing claimed - two bridges in one folder")
    _lf139()
    check("a pair working here, windows alive: the lock is held",
          os.path.exists(_lk139), True)
    if os.name == "nt" or sys.platform == "darwin":
        check("and asked by another spelling of the folder it is the same "
              "claim, not a second one",
              getattr(_pl139, "holds", lambda p: None)(_p139.upper()), True)
    daemon.STATE["loops"][_k139]["active"] = False
    _out139 = _lf139()
    check("the loop off: the lock is given back - the file is gone",
          os.path.exists(_lk139), False)
    check("and it is said, with the reason",
          [l for l in _out139 if "given back: the loop is off" in l] != [],
          True)
    check("nothing is taken while the loop stays off",
          (_lf139(), os.path.exists(_lk139)), ([], False))

    daemon.STATE["loops"][_k139]["active"] = True
    daemon.STATE["paused"][_k139] = {"why": "by hand in case 139",
                                     "by_death": False, "by": ""}
    _lf139()
    check("a person's pause: not taken", os.path.exists(_lk139), False)
    daemon.STATE["paused"].pop(_k139)
    _lf139()
    check("the pause lifted: taken again before work goes on",
          os.path.exists(_lk139), True)
    daemon.STATE["paused"][_k139] = {"why": "a window died",
                                     "by_death": True, "by": ""}
    _lf139()
    check("a hold the bridge put on itself is still work: the lock is kept",
          os.path.exists(_lk139), True)
    daemon.STATE["paused"].pop(_k139)
    daemon.STATE["mode"] = "paused"
    _out139 = _lf139()
    check("the whole bridge paused: given back too",
          (os.path.exists(_lk139),
           [l for l in _out139 if "the bridge is paused" in l] != []),
          (False, True))
    daemon.STATE["mode"] = "running"

    print("   another machine took the folder meanwhile: the pair is held,")
    print("   by whom is said, and the hold lifts once the lock is taken")
    os.makedirs(os.path.dirname(_lk139), exist_ok=True)
    with open(_lk139, "w", encoding="utf-8") as _fh:
        _fh.write(_json.dumps({"machine": "WORKPC", "pid": 4242,
                               "since": "2026-10-12 20:00:00", "beat": 7,
                               "at": "2026-10-12 20:05:00"}))
    _lf139()
    _h139 = (daemon.STATE.get("paused") or {}).get(_k139) or {}
    check("held, by the lock, naming the other machine",
          (_h139.get("by"), "WORKPC" in (_h139.get("why") or "")),
          ("lock", True))
    os.remove(_lk139)
    _out139 = _lf139()
    check("the other machine gone: taken, and that hold lifted",
          (os.path.exists(_lk139), _k139 in (daemon.STATE.get("paused") or {}),
           [l for l in _out139 if "hold it put on this pair is lifted" in l]
           != []), (True, False, True))

    print("   a heartbeat that cannot be written: said once, and its return")
    _pl139._say = lambda project, text, level: _said139.append((level, text))
    _tmp139 = "%s.beat-%d" % (_pl139.lock_path(_p139), os.getpid())
    os.makedirs(_tmp139)                  # the write lands on a folder: fails
    _pl139.beat()
    _pl139.beat()
    os.rmdir(_tmp139)
    _pl139.beat()
    check("said once at the first miss, with what it risks, and once on its "
          "return",
          [(lv, "could not be written" in t and "another machine" in t,
            "written again, after 2" in t) for lv, t in _said139],
          [("warn", True, False), ("log", False, True)])

    print("   the folder itself out of reach - a share that dropped: said")
    del _said139[:]
    _gone139 = _p139 + "-away"
    os.rename(_p139, _gone139)
    _pl139.beat()
    os.rename(_gone139, _p139)
    check("the lock is not held any more, and that is said",
          (getattr(_pl139, "holds", lambda p: True)(_p139),
           [lv for lv, t in _said139 if "cannot be reached" in t]),
          (False, ["warn"]))
finally:
    _pl139.BEAT_SEC, _pl139._say = _beat139
    _pl139.release()
    for _k, _v in _saved139.items():
        daemon.STATE[_k] = _v
    daemon.CFG["projects"] = _cfg139
check("the bridge starts the watch that does this",
      "target=lock_watch" in inspect.getsource(daemon.main), True)


print("\n140. a Mac's project share is brought back at the start by name, with")
print("   the keychain - and never with a dialog on the screen (M4, 8.90)")
from bridgecore import netshare as _ns140                    # noqa: E402
_d140 = os.path.join(TMP, "case140")
os.makedirs(_d140)
_was140 = getattr(_ns140, "KNOWN", None)
_ns140.KNOWN = os.path.join(_d140, "netshare.json")


class _R140(object):
    def __init__(self, code, out=""):
        self.returncode, self.stdout = code, out


_MT140 = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
          "//user1@WORKPC._smb._tcp.local/X%20Projects on /Volumes/X "
          "Projects (smbfs, nodev, nosuid, mounted by user1)\n"
          "//WORKPC.local/Public on /Volumes/Public (smbfs, nodev)\n")
check("the mount table read: who, the server by its name, the share",
      getattr(_ns140, "mounted", lambda run: None)(lambda c: _R140(0, _MT140)),
      {"/Volumes/X Projects": ("user1", "WORKPC", "X Projects"),
       "/Volumes/Public": ("", "WORKPC", "Public")})
check("a project's volume, found from its folded key",
      [getattr(_ns140, "volume_of", lambda p: None)(p) for p in
       ("/volumes/x projects/game", "/Users/u/game", "/Volumes/X")],
      ["/Volumes/x projects", "", "/Volumes/X"])

_asked140 = []


def _run140(answers):
    def run(cmd, timeout=None):
        _asked140.append(list(cmd))
        return answers(cmd)
    return run


print("   the keychain is asked for ATTRIBUTES only")
_has140 = getattr(_ns140, "has_password", lambda *a, **k: None)
_yes140 = _has140("user1", "WORKPC", run=_run140(
    lambda c: _R140(0 if "WORKPC.local" in c else 44)))
_no140 = _has140("user1", "WORKPC", run=_run140(lambda c: _R140(44)))
check("a password there is found, none is not; no secret was read",
      (_yes140, _no140,
       [a for c in _asked140 for a in c if a in ("-g", "-w")]),
      (True, False, []))

_rec140 = {"who": "user1", "server": "WORKPC", "share": "X Projects",
           "addr": "192.0.2.7"}
_rm140 = getattr(_ns140, "remount", None)


def _try140(found, keyed, appears):
    """appears: how many looks after the ask before it is mounted; 0 never"""
    del _asked140[:]
    said, up = [], [None]

    def run(cmd, timeout=None):
        _asked140.append(list(cmd))
        if cmd[0] == _ns140.OPEN and appears:
            up[0] = appears
        return _R140(0 if (cmd[0] != getattr(_ns140, "SECURITY", "") or keyed)
                     else 44)

    def ismount(p):
        if up[0] is None:
            return False
        up[0] -= 1
        return up[0] <= 0
    if _rm140 is None:
        return None, [], []
    got = _rm140("/Volumes/X Projects", dict(_rec140),
                 finder=lambda name, last, say=None: (
                     ("192.0.2.9", "") if found else ("", "%s not found" % name)),
                 run=run, ismount=ismount, wait=0.3,
                 sleep=lambda s: time.sleep(0.05),
                 say=lambda t, lv: said.append((lv, t)))
    opened = [c[1:] for c in _asked140 if c[0] == _ns140.OPEN]
    return got[0], opened, said


_a140 = _try140(True, True, 3)
check("found by name, a password in the keychain: asked by NAME, in the "
      "background, and said with the address",
      (_a140[0], _a140[1], [lv for lv, t in _a140[2]],
       all(s in (_a140[2] or [("", "")])[0][1] for s in ("WORKPC", "192.0.2.9"))),
      ("192.0.2.9", [["-g", "smb://user1@WORKPC.local/X%20Projects"]],
       ["log"], True))
_b140 = _try140(True, False, 1)
check("no password in the keychain: nothing asked - a dialog would be on "
      "the screen - and it is said why",
      (_b140[0], _b140[1], [lv for lv, t in _b140[2]],
       "keychain" in "".join(t for lv, t in _b140[2])),
      ("", [], ["warn"], True))
_c140 = _try140(False, True, 1)
check("no server by that name: nothing asked, said",
      (_c140[0], _c140[1], [lv for lv, t in _c140[2]]), ("", [], ["warn"]))
_e140 = _try140(True, True, 0)
check("asked, and it never came: said, not waited on for ever",
      (_e140[1] != [], [lv for lv, t in _e140[2]]), (True, ["warn"]))

print("   at the start: learned while mounted, asked back when not")
_at140 = getattr(_ns140, "at_start", None)
_again140 = []
if _at140 is not None:
    _at140(["/volumes/x projects/game", "/Users/u/local"],
           now=lambda: {"/Volumes/X Projects": ("user1", "WORKPC",
                                                 "X Projects")},
           again=lambda p, rec, say=None: _again140.append(p) or ("", ""))
    _l140 = _at140(["/volumes/x projects/game"], now=lambda: {},
                   again=lambda p, rec, say=None: (
                       _again140.append((p, rec.get("who"), rec.get("server"),
                                         rec.get("share"))) or ("192.0.2.9", "back")))
    _n140 = _at140(["/volumes/other/game"], now=lambda: {},
                   again=lambda p, rec, say=None: _again140.append(p),
                   say=lambda t, lv: None)
else:
    _l140 = _n140 = None
check("a mounted share is not asked; one gone is asked back by the record "
      "learned while it was mounted",
      (_again140, _l140),
      ([("/Volumes/X Projects", "user1", "WORKPC", "X Projects")], ["back"]))
check("one never seen mounted is not guessed at: said",
      (_n140 or [""])[0].startswith("/Volumes/other is not mounted, and this "
                                    "machine has never seen it"), True)
check("and the address that worked is kept for the next find",
      (getattr(_ns140, "known", dict)().get("/volumes/x projects") or {})
      .get("addr"), "192.0.2.9")
_ns140.KNOWN = _was140

_sa140 = getattr(daemon, "shares_at_start", lambda platform=None: None)
_ats140 = getattr(_ns140, "at_start", None)
_ns140.at_start = lambda projects, **k: ["asked"]
try:
    check("off a Mac nothing is asked; on a Mac it is",
          (_sa140("win32"), _sa140("darwin")), ([], ["asked"]))
finally:
    if _ats140 is None:
        del _ns140.at_start
    else:
        _ns140.at_start = _ats140
_main140 = inspect.getsource(daemon.main)
check("the bridge checks them before its migrations read a project",
      -1 < _main140.find("shares_at_start()")
      < _main140.find("migrate_executor_mode()"), True)


print("\n141. the token file is read only when the config turns it on: by")
print("   default a window and a `claude -p` log in with the client's own")
print("   sign-in (8.91). The file on a Mac was the OWNER's; a colleague sits")
print("   there with their own account, and every session would have been")
print("   the owner's")
_d141 = os.path.join(TMP, "case141")
os.makedirs(_d141)
_tf141 = os.path.join(_d141, "token")
_SECRET141 = "sk-ant-oat01-Case141" + "Z" * 40
with open(_tf141, "w") as fh:
    fh.write("export CLAUDE_CODE_OAUTH_TOKEN=%s\n" % _SECRET141)
_cfg141, _lines141 = {}, []
_told141 = getattr(sessions, "_LOGIN_TOLD", [])
_was141 = (sessions.TOKEN_FILE, getattr(sessions, "TOKEN_FILE_ON", None),
           store.load_config, store.journal, list(_told141))
_on141 = getattr(sessions, "token_file_on", lambda: True)
_wt141 = getattr(sessions, "window_token",
                 lambda project, role: sessions.read_token()[0])
_say141 = getattr(sessions, "say_own_login", lambda os_name=None: None)
_env141 = {"PATH": os.environ.get("PATH", "")}
try:
    sessions.TOKEN_FILE = _tf141
    sessions.TOKEN_FILE_ON = None             # the seam off: the config asks
    _base141 = _was141[2]()     # the suite's own config, the key off it
    _base141.pop("use_token_file", None)
    store.load_config = lambda: dict(_base141, **_cfg141)
    store.journal = lambda *a, **k: _lines141.append(a)
    print("   FAILURE WOULD LOOK LIKE: the owner's token in a window's or a")
    print("   `claude -p`'s environment with nothing in the config asking")
    check("no setting in the config: the file is not read",
          (_on141(), _wt141(_d141, "executor"),
           sessions.headless_client(["claude", "-p", "x"],
                                    dict(_env141))[1].get(sessions.TOKEN_VAR)),
          (False, "", None))
    del _told141[:]
    del _lines141[:]
    _say141("nt")
    _nt141 = list(_lines141)
    _say141("posix")
    _say141("posix")
    check("off Windows ONE journal line per daemon: the client's own sign-in, "
          "the file named as NOT read, never quoted; on Windows none",
          (_nt141, len(_lines141),
           [("own sign-in" in a[1], _tf141 in a[1], "NOT read" in a[1],
             "Case141" in a[1] or "ZZZZ" in a[1]) for a in _lines141]),
          ([], 1, [(True, True, True, False)]))
    _cfg141["use_token_file"] = True
    check("CONTROL: use_token_file on, and the file is read as before",
          (_on141(), _wt141(_d141, "executor") == _SECRET141,
           sessions.headless_client(["claude", "-p", "x"],
                                    dict(_env141))[1].get(sessions.TOKEN_VAR)
           == _SECRET141), (True, True, True))
    _cfg141["use_token_file"] = False
    if "_fake130" in globals() and hasattr(sessions, "TmuxSeat"):
        print("   and the window itself, through case 130's fake tmux: the")
        print("   client is started with no token at all")
        _keep141 = {n: getattr(sessions, n) for n in
                    ("TMUX", "build_command", "ensure_marks")}
        _ev141 = {n: os.environ.get(n) for n in
                  ("TMUX_TMPDIR", "CLAUDE_CODE_OAUTH_TOKEN")}
        _out141 = os.path.join(_d141, "client.json")
        _pid141 = 0
        try:
            sessions.TMUX = [sys.executable, _fake130]
            sessions.ensure_marks = lambda project, role: []
            sessions.build_command = (lambda project, role, *a, **k:
                                      [sys.executable, _stub130, _out141])
            os.environ["TMUX_TMPDIR"] = _sock130
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            _pid141 = sessions.launch(_proj130, "executor")
            _cl141 = _client130(_out141)
            check("the window's client: started, and no token in it",
                  (_cl141.get("pid") == _pid141, _cl141.get("token")),
                  (True, None))
        finally:
            if _pid141:
                sessions.stop(_proj130, "executor", wait=15)
            for _n, _v in _keep141.items():
                setattr(sessions, _n, _v)
            for _n, _v in _ev141.items():
                if _v is None:
                    os.environ.pop(_n, None)
                else:
                    os.environ[_n] = _v
            sessions.SEATS.clear()
finally:
    (sessions.TOKEN_FILE, _ton141, store.load_config, store.journal,
     _t141) = _was141
    sessions.TOKEN_FILE_ON = _ton141
    _told141[:] = _t141


print("\n142. adding a project takes its lock BEFORE install writes a thing:")
print("   a folder another machine's bridge holds keeps its .claude and its")
print("   .mcp.json (8.91) - the hooks there are the ones its windows run")
from bridgecore import projlock as _pl142                    # noqa: E402
_p142 = os.path.join(TMP, "case142", "proj")
os.makedirs(_p142)
_lk142 = _pl142.lock_path(_p142)
os.makedirs(os.path.dirname(_lk142))
with open(_lk142, "w", encoding="utf-8") as _fh:
    _fh.write(_json.dumps({"machine": "WORKPC", "pid": 4242,
                           "since": "2026-10-12 20:00:00", "beat": 7,
                           "at": "2026-10-12 20:05:00"}))
_k142 = store.norm(_p142)
print("   FAILURE WOULD LOOK LIKE: `ok`, or a .claude folder in a project")
print("   whose lock names WORKPC")
_r142 = daemon.handle_add_project({"path": _p142})
check("held by another machine: refused, naming it, nothing written",
      (_r142.get("ok"), "WORKPC" in (_r142.get("error") or ""),
       sorted(os.listdir(_p142)), _k142 in daemon.CFG.get("projects", {})),
      (False, True, ["bridge-logs"], False))
os.remove(_lk142)
_pl142.SEEN.pop(_lk142, None)
_r142 = daemon.handle_add_project({"path": _p142})
check("CONTROL: free, it is added and installed, and the lock is not kept",
      (_r142.get("ok"),
       os.path.isfile(os.path.join(_p142, ".claude", "settings.json")),
       os.path.isfile(os.path.join(_p142, ".mcp.json")),
       _pl142.holds(_p142), os.path.exists(_lk142)),
      (True, True, True, False, False))
with daemon._lock:
    daemon.CFG.get("projects", {}).pop(_k142, None)
    store.save_config(daemon.CFG)


print("\n143. the Mac's launcher (8.91): bridge.command runs maclaunch, which")
print("   names what is missing in ONE line with its install command, asks")
print("   the PC's name, share and account once and keeps them beside it with")
print("   no password, and mounts the share by NAME - through stubs, so the PC")
print("   runs it too; the ssh witness on a Mac is the real thing")
try:
    from bridgecore import maclaunch as _ml143                  # noqa: E402
except ImportError:
    _ml143 = None
check("the launcher's module is there", _ml143 is not None, True)
_src143 = os.path.dirname(os.path.dirname(os.path.abspath(daemon.__file__)))
_cmd143 = [p for p in (os.path.join(os.path.dirname(_src143), "bridge.command"),
                       os.path.join(_src143, "bridge.command"))   # public
           if os.path.exists(p)]
if not _cmd143:
    print("   bridge.command is not beside this tree (a copy of the package's")
    print("   source alone): its bytes are not read here")
else:
    with open(_cmd143[0], "rb") as _fh:
        _raw143 = _fh.read()
    check("bridge.command: LF only (sh reads a CR as part of the word), "
          "and it hands maclaunch its own folder for the settings",
          (b"\r" in _raw143, _raw143.startswith(b"#!/bin/sh\n"),
           b'-m bridgecore.maclaunch --home "$HERE"' in _raw143,
           b"xcode-select --install" in _raw143), (False, True, True, True))
# The bytes above are the ones on THIS disk. A clone or a git archive made
# where core.autocrlf is on - the Windows default - rewrites them unless the
# repository says eol=lf for this file: the PC's run of the public 226176c
# failed the check above for exactly that (8.114). The rule is the public
# tree's own .gitattributes, read here from its source in public/.
_ga143 = [p for p in (os.path.join(_src143, "public", "gitattributes.public"),
                      os.path.join(_src143, ".gitattributes"))
          if os.path.exists(p)]
if not _ga143 and not os.path.isfile(os.path.join(_src143, "make_public.py")):
    print("   no .gitattributes beside this tree (an unpacked package: no git")
    print("   stands between its bytes and the disk): not counted")
else:
    with open((_ga143 or [os.devnull])[0], encoding="utf-8") as _fh:
        _rule143 = _fh.read()
    check("the public tree says bridge.command is text with LF ends",
          "bridge.command text eol=lf" in _rule143.splitlines(), True)
    import shutil as _sh143                                  # noqa: E402
    _git143 = _sh143.which("git")
    # the Mac's /usr/bin/git without the command line tools is a stub that
    # opens an install dialog: asked of xcode-select first, quietly
    if _git143 and sys.platform == "darwin":
        _git143 = _git143 if subprocess.run(
            ["xcode-select", "-p"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode == 0 else None
    if not _git143:
        print("   no git here: the archive under core.autocrlf not counted")
    else:
        import tarfile as _tar143                            # noqa: E402

        def _arch143(attrs):
            # the bytes a Windows git archive hands out, without and with
            # the rule; a tree, not a commit, so no identity is needed
            d = os.path.join(TMP, "case143git-" + ("bare" if attrs is None
                                                   else "rule"))
            os.makedirs(d)
            with open(os.path.join(d, "bridge.command"), "wb") as fh:
                fh.write(b"#!/bin/sh\necho one\n")
            if attrs is not None:
                with open(os.path.join(d, ".gitattributes"), "w",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(attrs)

            def git(*a):
                return subprocess.run(
                    [_git143, "-C", d, "-c", "core.autocrlf=true",
                     "-c", "core.safecrlf=false"] + list(a),
                    capture_output=True)
            git("init", "-q")
            git("add", "-A")
            tree = git("write-tree").stdout.decode().strip()
            out = git("archive", "--format=tar", tree).stdout
            with _tar143.open(fileobj=io.BytesIO(out)) as tf:
                return tf.extractfile("bridge.command").read()
        check("a git archive under core.autocrlf: CRLF without the rule "
              "(so this git does rewrite), LF only with it",
              (b"\r" in _arch143(None), b"\r" in _arch143(_rule143)),
              (True, False))
if _ml143 is not None:
    _h143 = os.path.join(TMP, "case143")
    _bin143 = os.path.join(_h143, "bin")
    os.makedirs(_bin143)
    open(os.path.join(_bin143, "tmux"), "w").close()
    check("a tool is found where its installer puts it, off PATH; one that "
          "is nowhere is ''",
          [_ml143.find_tool(n, path="", places=(_bin143,),
                            isexe=os.path.isfile) for n in ("tmux", "claude")],
          [os.path.join(_bin143, "tmux"), ""])
    _f143, _gone143 = _ml143.tools(lambda n: {"tmux": "/x/tmux"}.get(n, ""))
    check("what is missing: ONE line naming it with the command that "
          "installs it, and nothing about what is there",
          ("\n" in _gone143, _gone143.startswith(
              "Missing: claude - install: curl -fsSL "
              "https://claude.ai/install.sh | bash"), "tmux" in _gone143),
          (False, True, False))
    _said143 = []
    _gone_run143 = []
    check("nothing missing is a line too: none",
          _ml143.tools(lambda n: "/x/" + n)[1], "")
    check("a tool missing: the launcher stops there, asks nothing, starts "
          "nothing",
          (_ml143.main([], home=_h143, find=lambda n: "",
                       now=lambda: {}, run=_gone_run143.append,
                       ask=lambda q: 1 / 0, say=_said143.append, tty=True,
                       start=lambda *a: _gone_run143.append(a)),
           _gone_run143, [l for l in _said143 if l.startswith("Missing: ")]
           != []), (1, [], True))

    def _main143(argv, now, runs, answers=(), tty=True, started=None):
        it = iter(answers)
        out = []
        code = _ml143.main(
            argv, home=_h143, find=lambda n: "/x/" + n, now=now,
            run=runs.append, ask=lambda q: next(it), say=out.append,
            tty=tty, start=lambda src, env, rest: (started.append(
                (src, env["PATH"].split(os.pathsep)[0], rest))
                if started is not None else None) or 0)
        return code, out

    _runs143 = []
    _c143, _o143 = _main143(["--check"], lambda: {}, _runs143, tty=False)
    check("--check with no settings yet: says to run it at the screen once, "
          "asks nothing, opens nothing",
          (_c143, [l for l in _o143 if "No settings yet" in l] != [],
           _runs143), (1, True, []))
    print("   FAILURE WOULD LOOK LIKE: a password in bridge-mac.json, or an")
    print("   `open` with an address in place of the PC's name")
    _seq143 = [{}, {"/Volumes/X Projects": ("user1", "WORKPC",
                                             "X Projects")}]
    _now143 = lambda: _seq143.pop(0) if len(_seq143) > 1 else _seq143[0]
    _st143 = []
    _c143, _o143 = _main143(["--port", "8799"], _now143, _runs143,
                            ("smb://WORKPC.local/", "X Projects", "user1"),
                            started=_st143)
    with open(os.path.join(_h143, "bridge-mac.json"), encoding="utf-8") as _fh:
        _kept143 = _fh.read()
    check("first run: asked, and kept beside the launcher - the three, the "
          "name bare, no password",
          (sorted(_json.loads(_kept143).items()),
           "pass" in _kept143.lower()),
          ([("account", "user1"), ("server", "WORKPC"),
            ("share", "X Projects")], False))
    check("the share is opened BY NAME, once, and said where it mounted",
          (_runs143, [l for l in _o143
                      if "mounted: smb://user1@WORKPC.local/X%20Projects "
                         "at /Volumes/X Projects" in l] != []),
          ([["/usr/bin/open", "smb://user1@WORKPC.local/X%20Projects"]],
           True))
    check("then the bridge is started from the package, the tools' folder "
          "first on PATH, the rest of the arguments its own",
          (_c143, _st143), (0, [(_src143, "/x", ["--port", "8799"])]))
    _runs143 = []
    _c143, _o143 = _main143([], lambda: _seq143[0], _runs143, (),
                            started=_st143)
    check("the next run asks nothing and, mounted already, opens nothing",
          (_c143, _runs143, len(_st143)), (0, [], 2))
    _c143, _o143 = _main143(["--check"], lambda: {}, _runs143, tty=False)
    check("--check, not mounted: says so and what it would open, opens "
          "nothing, starts nothing, and is not green",
          (_c143, _runs143, len(_st143),
           [l for l in _o143 if "NOT mounted: smb://user1@WORKPC.local/"
            "X%20Projects" in l] != []), (1, [], 2, True))
    _c143, _o143 = _main143(["--check"], lambda: _seq143[0], _runs143,
                            tty=False)
    check("--check, mounted: green, and nothing asked of the system",
          (_c143, _runs143, _o143[-1]),
          (0, [], "check: everything is in place"))
try:
    import check_public as _cp143                               # noqa: E402
except ImportError:
    _cp143 = None
if _cp143 is not None:
    print("   the fixtures' login is a placeholder to the public scan, and a")
    print("   real one is not (the Mac's run refused case 140's, 8.91)")
    _hits143 = []
    _cp143.SESSION_PREFIXES = _cp143.SESSION_PREFIXES or set()
    # the two hits are built in halves: this suite is published, and the
    # scan would be right to refuse them written whole
    for _ln143 in ("smb://user1@WORKPC.local/X%20Projects",
                   "smb://owner" + "@HOMEPC" + ".local/Projects",
                   "write to someone" + "@example.org"):
        _cp143.scan_text("x.py", _ln143, _hits143)
    check("user1@WORKPC.local passes; an account at another PC, that PC's "
          ".local name and an e-mail are still hits (8.98)",
          [(h[2], h[3]) for h in _hits143],
          [("e-mail address", "owner" + "@HOMEPC" + ".local"),
           ("mDNS host name", "HOMEPC" + ".local"),
           ("e-mail address", "someone" + "@example.org")])
    print("   and the launcher is published and read, by its name: another")
    print("   .command is still refused unread")
    check("bridge.command is publishable; x.command is not",
          (_cp143.unexpected("bridge.command"),
           _cp143.unexpected("x.command")), (False, True))


print("\n144. the planner's check copies the files above source/ that the")
print("   PACKAGE LIST names, not a list of its own (8.92): its copy of 74c367a")
print("   had no bridge.command, and make_public and the package row failed")
_t144 = os.path.join(TMP, "case144", "top")
os.makedirs(os.path.join(_t144, "source"))
_names144 = ["bridge.bat", "add-project.bat", "bridge.command", "next.launcher",
             "not-packaged.txt"]
for _n144 in _names144:
    with open(os.path.join(_t144, _n144), "w", encoding="utf-8") as _fh:
        _fh.write(_n144 + "\n")
with open(os.path.join(_t144, "source", "verify_package.py"), "w",
          encoding="utf-8") as _fh:
    _fh.write('FILES = ["bridge.bat", "add-project.bat", "bridge.command",\n'
              '         "next.launcher", "source/verify_package.py"]\n')
_c144 = os.path.join(TMP, "case144", "copy", "source")
_root144 = daemon.ROOT
daemon.ROOT = os.path.join(_t144, "source")
try:
    daemon._check_copy(_c144)
finally:
    daemon.ROOT = _root144
print("   FAILURE WOULD LOOK LIKE: bridge.command or next.launcher missing")
print("   from the copy, or not-packaged.txt in it")
check("the copy has every top file the list names, and nothing else",
      sorted(os.listdir(os.path.dirname(_c144))),
      ["add-project.bat", "bridge.bat", "bridge.command", "next.launcher",
       "source"])
print("   and in the real tree every launcher make_public ships is on it")
# An unpacked package ships no make_public.py (verify_package.FILES), and
# nor does the public tree it builds: there the check is named as skipped.
# From the zip on the PC, 2026-10-13, it was the one FAIL of the suite, and
# the public tree went out with it (8.108). Asked of the FILE, not of the
# import: a make_public.py that is there and does not import is a FAIL.
if not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(
        __file__)), "make_public.py")):
    skip("make_public.LAUNCHERS is within the package list's top files",
         "no make_public.py beside this suite - an unpacked package or the "
         "public tree, neither ships the builder")
else:
    try:
        import make_public as _mp144                            # noqa: E402
        import verify_package as _vp144                         # noqa: E402
        _top144 = [f for f in _vp144.FILES if "/" not in f]
        _gap144 = sorted(set(_mp144.LAUNCHERS) - set(_top144))
    except ImportError as _e144:
        _gap144 = ["cannot import: %s" % _e144]
    check("make_public.LAUNCHERS is within the package list's top files",
          _gap144, [])


print("\n145. a window on a Mac is SHOWN in Terminal by the daemon and by")
print("   nobody else (8.93): a suite, a check or a probe - a throwaway")
print("   daemon's too - opens no viewer; a viewer of another size leaves the")
print("   window its own 160x48, so its screen is read and Enter pressed as")
print("   before; a gone window's dead viewer is closed by its tty")
print("   FAILURE WOULD LOOK LIKE: the stand-in opener called from this suite,")
print("   the window shrunk to the viewer's 80 columns and the line on two")
print("   rows, or a hand-opened window shown at the start")
import shutil as _sh145                                      # noqa: E402
_need145 = ("viewer_refused", "show_window", "open_viewer", "view_options",
            "viewer_ttys", "load_viewers", "save_viewers", "view_dir",
            "_sweep_viewers", "sweep_viewers", "sweep_soon",
            "close_dead_terminals", "TERMINAL_OPEN", "TERMINAL_CLOSE",
            "VIEWER_TRIES")
_has145 = [n for n in _need145 if hasattr(sessions, n)]
check("the window layer has its viewer, its guard and its sweep",
      sorted(set(_need145) - set(_has145)), [])
check("and the daemon its pass over the windows at the start",
      callable(getattr(daemon, "show_windows_at_start", None)), True)
_opened145, _closed145, _close_ok145, _lines145 = [], [], set(), []


def _open145(argv):
    _opened145.append(list(argv))


def _close145(ttys):
    _closed145.append(list(ttys))
    return [t for t in ttys if t in _close_ok145]


def _until145(fn, want, wait=10):
    end = time.time() + wait
    while True:
        got = fn()
        if got == want or time.time() >= end:
            return got
        time.sleep(0.1)


_saved145 = {n: getattr(sessions, n, None) for n in
             ("TERMINAL_OPEN", "TERMINAL_CLOSE", "viewer_refused",
              "show_window", "viewer_ttys", "build_command", "ensure_marks")}
_jsaved145 = store.journal
_writer145 = store._WRITER["daemon"]


def _restore145():
    for _n, _v in _saved145.items():
        if _v is not None or hasattr(sessions, _n):
            setattr(sessions, _n, _v)
    store.journal = _jsaved145
    store._WRITER["daemon"] = _writer145


if len(_has145) == len(_need145):
    print("   the guard is asked of the process: off a Mac, on temp data and")
    print("   outside the daemon each says why; only the daemon on a live")
    print("   folder may")
    _vr145 = sessions.viewer_refused
    _live145 = os.path.join(os.path.expanduser("~"), "bridge-data-145-unmade")
    check("win32 / the suite's data / a live folder outside the daemon / the "
          "daemon on a live folder",
          [_vr145(platform="win32", data=_live145, writer=True)[:9],
           "temp folder" in _vr145(platform="darwin", writer=True),
           "not the daemon" in _vr145(platform="darwin", data=_live145,
                                      writer=False),
           _vr145(platform="darwin", data=_live145, writer=True)],
          ["not a Mac", True, True, ""])

    print("   a THROWAWAY DAEMON - one that claimed the writer on temp data, as")
    print("   test_multipair's and the planner's check copy do - shows nothing,")
    print("   sweeps nothing, closes nothing")
    try:
        sessions.TERMINAL_OPEN, sessions.TERMINAL_CLOSE = _open145, _close145
        store._WRITER["daemon"] = True
        _r145 = (sessions.show_window(os.getpid()), sessions.sweep_viewers(),
                 sessions.sweep_soon(0.01))
        time.sleep(0.3)
    finally:
        _restore145()
    check("show_window refused with the reason, the sweep 0, no timer",
          (_r145[0][0], ("temp folder" if sys.platform == "darwin"
                         else "not a Mac") in _r145[0][1], _r145[1:]),
          (False, True, (0, None)))
    check("and the stand-in opener and closer were never called",
          (_opened145, _closed145), ([], []))

_tm145 = sessions.tmux_command() if os.name != "nt" else None
if not _tm145:
    skip("145: a real tmux window with an 80x24 viewer attached",
         "needs tmux off Windows - case 130 runs the same calls through a "
         "fake tmux")
else:
    import fcntl as _fc145                                   # noqa: E402
    import pty as _pty145                                    # noqa: E402
    import struct as _st145                                  # noqa: E402
    import termios as _tio145                                # noqa: E402
    print("   real tmux, a stand-in client, and a client on a pty of 80x24 -")
    print("   a person's Terminal window without the window")
    # a socket's path has 104 bytes on a Mac: the suite's own TMP is too deep
    _sock145 = tempfile.mkdtemp(prefix="b145-", dir="/tmp")
    _d145 = os.path.join(TMP, "case145")
    _proj145 = os.path.join(_d145, "proj")
    os.makedirs(_proj145)
    _stub145 = os.path.join(_d145, "client_stub.py")
    _out145 = os.path.join(_d145, "entered")
    _line145 = "L145-" + "x" * 145
    with open(_stub145, "w", encoding="utf-8") as fh:
        fh.write("import sys, time\n"
                 "print(sys.argv[2], flush=True)\n"
                 "input('> ')\n"
                 "open(sys.argv[1], 'w').write('entered')\n"
                 "time.sleep(120)\n")
    _env145 = {n: os.environ.get(n) for n in ("TMUX_TMPDIR", "TMUX")}
    _atts145, _fds145, _pid145, _lab145 = [], [], None, ""

    def _attach145(argv):
        m, s = _pty145.openpty()
        _fc145.ioctl(s, _tio145.TIOCSWINSZ, _st145.pack("HHHH", 24, 80, 0, 0))
        p = subprocess.Popen(argv, stdin=s, stdout=s, stderr=s,
                             env=dict(os.environ, TERM="xterm-256color"),
                             start_new_session=True)
        os.close(s)
        _fds145.append(m)

        def _drain():
            while True:
                try:
                    if not os.read(m, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=_drain, daemon=True).start()
        _atts145.append(p)
        return p

    def _sizes145(want):
        def _ask():
            r = sessions._tmux(_lab145, "list-clients", "-F",
                               "#{client_width}x#{client_height}")
            return sorted((r.stdout or "").split()) \
                if r and not r.returncode else None
        return _until145(_ask, want)

    def _win145():
        r = sessions._tmux(_lab145, "display-message", "-p",
                           "#{window_width}x#{window_height}")
        return (r.stdout or "").strip() if r else None

    try:
        os.environ["TMUX_TMPDIR"] = _sock145
        os.environ.pop("TMUX", None)
        sessions.TERMINAL_OPEN, sessions.TERMINAL_CLOSE = _open145, _close145
        sessions.ensure_marks = lambda project, role="": []
        sessions.build_command = (lambda project, role, *a, **k:
                                  [sys.executable, _stub145, _out145,
                                   _line145])
        store.journal = lambda *a, **k: _lines145.append(a)
        store._WRITER["daemon"] = True     # a throwaway DAEMON, as above
        _pid145 = sessions.launch(_proj145, "executor")
        _seat145 = sessions.tmux_seat(_pid145)
        _lab145 = _seat145[0] if _seat145 else ""
        _opt145 = sessions._tmux(_lab145, "show-options", "-gv", "window-size")
        check("launched in real tmux by a throwaway daemon: nothing opened, "
              "no line saying it was shown, and the window's size its own",
              (bool(_seat145), _opened145,
               [a[1] for a in _lines145 if "Terminal" in str(a[1])
                or "on the screen" in str(a[1])],
               (_opt145.stdout or "").strip() if _opt145 else None),
              (True, [], [], "manual"))
        _until145(lambda: _line145 in sessions.console_screen(_pid145)
                  .splitlines(), True)
        _a145 = _attach145(_tm145 + ["-L", _lab145, "attach-session"])
        check("a viewer of 80x24 attached, and the window 160x48 under it",
              (_sizes145(["80x24"]), _win145()), (["80x24"], "160x48"))
        check("the screen reads the 150-character line whole, on one row",
              _line145 in sessions.console_screen(_pid145).splitlines(), True)

        if "show_window" in _has145:
            print("   past the gate, with the stand-in opener: a window with a")
            print("   viewer gets no second one, one without gets its .command")
            print("   opened, and a second ask while it attaches opens nothing")
            sessions.viewer_refused = lambda *a, **k: ""
            try:
                _sw145 = [sessions.show_window(_pid145)]
                _a145.terminate()
                _sizes145([])
                _sw145 += [sessions.show_window(_pid145),
                           sessions.show_window(_pid145)]
            finally:
                sessions.viewer_refused = _saved145["viewer_refused"]
            _cmd145 = os.path.join(sessions.view_dir(), _lab145 + ".command")
            check("already on the screen / opened / being opened, and one "
                  "`open -g -a Terminal` of the window's .command",
                  (_sw145[0][0], _sw145[0][1].startswith(
                      "already on the screen (/dev/"), _sw145[1:], _opened145),
                  (True, True, [(True, "opened in Terminal"),
                                (True, "being opened")],
                   [["/usr/bin/open", "-g", "-a", "Terminal", _cmd145]]))
            print("   and that .command IS a viewer: run on an 80x24 pty it")
            print("   attaches to this window's server")
            _attach145(["/bin/sh", _cmd145])
            check("one client of 80x24 on the window's server, the window "
                  "160x48", (_sizes145(["80x24"]), _win145()),
                  (["80x24"], "160x48"))

        def _entered():
            try:
                with open(_out145) as fh:
                    return fh.read()
            except OSError:
                return None
        check("Enter is pressed in the window with a viewer on it, and "
              "arrives", (sessions.console_answer(_pid145),
                          _until145(_entered, "entered")), (True, "entered"))
        _gone145 = sessions.stop(_proj145, "executor", wait=15)
        _ls145 = sessions._tmux(_lab145, "list-sessions")
        check("stop: the window gone, no server answers on its label, the "
              "socket tmux leaves behind removed, and the viewer's client "
              "ended with them",
              (_gone145, sessions.pid_alive(_pid145),
               bool(_ls145) and _ls145.returncode != 0,
               _lab145 in os.listdir(sessions.tmux_dir()),
               _until145(lambda: _atts145[-1].poll() is not None, True)),
              (True, False, True, False, True))
        print("   and a socket nobody listens on is not asked at all: a")
        print("   pid that is no window costs a connect per socket, not a")
        print("   tmux run (326 of them took 7.25 s on a Mac)")
        _dead145 = []
        for _i145 in range(40):
            _lab = "bridge-dead145-%02d" % _i145
            _r = sessions._tmux(_lab, "new-session", "-d", "-s", "d",
                                "/bin/sh", "-c", "exit 0")
            _dead145.append(_lab)
        _until145(lambda: all(
            (lambda r: bool(r) and r.returncode != 0)(
                sessions._tmux(n, "list-sessions")) for n in _dead145), True)
        _calls145 = []
        _realtmux145 = sessions._tmux
        sessions._tmux = lambda label, *a, **k: (
            _calls145.append(label) or _realtmux145(label, *a, **k))
        try:
            _seatnone145 = sessions.tmux_seat(_pid145 + 100000)
        finally:
            sessions._tmux = _realtmux145
        check("forty dead sockets on disk, none of them asked, and no seat",
              (len([n for n in os.listdir(sessions.tmux_dir())
                    if n.startswith("bridge-dead145-")]),
               [c for c in _calls145 if c.startswith("bridge-dead145-")],
               _seatnone145), (40, [], None))
    finally:
        # the sweep a stop starts, and a viewer being learned, finish
        # against the stand-ins, never against Terminal
        for _t in threading.enumerate():
            if isinstance(_t, threading.Timer) or \
                    _t.name.startswith("viewer:"):
                _t.join(25)
        _restore145()
        for _p in _atts145:
            if _p.poll() is None:
                _p.kill()
        for _m in _fds145:
            try:
                os.close(_m)
            except OSError:
                pass
        if _pid145 and sessions.pid_alive(_pid145):
            sessions.terminate_and_wait(_pid145, 10)
        if _lab145:
            sessions._tmux(_lab145, "kill-server")
        for _n, _v in _env145.items():
            if _v is None:
                os.environ.pop(_n, None)
            else:
                os.environ[_n] = _v
        sessions.SEATS.clear()
        _sh145.rmtree(_sock145, ignore_errors=True)
    check("the socket folder this case made is gone, by its exact path",
          os.path.exists(_sock145), False)

if "_sweep_viewers" in _has145:
    print("   the sweep past its gate: a live window's viewers are learned")
    print("   again, a gone window's dead viewer closed and forgotten, and one")
    print("   that does not close yet tried again, VIEWER_TRIES times")
    _vt145 = {"bridge-executor-live145": ["/dev/ttys900"]}
    try:
        sessions.TERMINAL_CLOSE = _close145
        sessions.viewer_ttys = lambda label: _vt145.get(label)
        del _closed145[:]
        _close_ok145.add("/dev/ttys901")
        sessions.save_viewers({
            "bridge-executor-live145": {"ttys": ["/dev/ttys800"], "tries": 3},
            "bridge-executor-gone145": {"ttys": ["/dev/ttys901"], "tries": 0},
            "bridge-planner-empty145": {"ttys": [], "tries": 0},
            "bridge-planner-stuck145": {"ttys": ["/dev/ttys903"], "tries": 0}})
        for _n in ("bridge-executor-gone145", "bridge-planner-empty145"):
            open(os.path.join(sessions.view_dir(), _n + ".command"), "w").close()
        _n145 = sessions._sweep_viewers()
        _reg145 = sessions.load_viewers()
        check("one closed; the live one relearned; the gone and the empty "
              "dropped with their .command; the stuck one kept",
              (_n145, _closed145, sorted(_reg145),
               _reg145.get("bridge-executor-live145"),
               (_reg145.get("bridge-planner-stuck145") or {}).get("tries"),
               sorted(f for f in os.listdir(sessions.view_dir())
                      if f.endswith("145.command"))),
              (1, [["/dev/ttys901", "/dev/ttys903"]],
               ["bridge-executor-live145", "bridge-planner-stuck145"],
               {"ttys": ["/dev/ttys900"], "tries": 0}, 1, []))
        for _i145 in range(sessions.VIEWER_TRIES):
            sessions._sweep_viewers()
        check("the stuck one given up after VIEWER_TRIES sweeps, the live "
              "one kept", sorted(sessions.load_viewers()),
              ["bridge-executor-live145"])
    finally:
        _restore145()
        sessions.save_viewers({})

if callable(getattr(daemon, "show_windows_at_start", None)):
    print("   at the start the daemon shows the windows it opened and holds,")
    print("   and only those: one opened by hand and a dead one are left alone,")
    print("   and outside the daemon nothing is asked at all")
    _pa145 = os.path.join(TMP, "case145", "pa")
    _pb145 = os.path.join(TMP, "case145", "pb")
    for _p in (_pa145, _pb145):
        os.makedirs(_p, exist_ok=True)
    _sl145 = [subprocess.Popen([sys.executable, "-c",
                                "import time; time.sleep(60)"])
              for _i145 in range(2)]
    _dead145 = subprocess.Popen([sys.executable, "-c", "pass"])
    _dead145.wait()
    _now145 = time.time()
    _shown145 = []
    _keep145 = {k: daemon.STATE.get(k) for k in ("pids", "window_log")}
    _na145, _nb145 = store.norm(_pa145), store.norm(_pb145)
    try:
        daemon.STATE["pids"] = {
            _na145 + "|executor": {"pid": _sl145[0].pid, "at": _now145},
            _na145 + "|planner": {"pid": _sl145[1].pid, "at": _now145},
            _nb145 + "|executor": {"pid": _dead145.pid, "at": _now145},
            _na145 + "|watcher": {"pid": _sl145[0].pid, "at": _now145}}
        daemon.STATE["window_log"] = [
            {"pid": _sl145[0].pid, "path": _na145, "role": "executor",
             "at": _now145},
            {"pid": _dead145.pid, "path": _nb145, "role": "executor",
             "at": _now145}]
        sessions.show_window = lambda pid: (_shown145.append(pid)
                                            or (True, "stub"))
        store.journal = lambda *a, **k: _lines145.append(a)
        check("outside the daemon: nothing asked, nothing shown",
              (daemon.show_windows_at_start(), _shown145), ([], []))
        sessions.viewer_refused = lambda *a, **k: ""
        _said145 = daemon.show_windows_at_start()
        check("past the gate: the bridge's own live window, and it alone",
              ([(r[1], r[2]) for r in _said145], _shown145),
              ([("executor", _sl145[0].pid)], [_sl145[0].pid]))
    finally:
        _restore145()
        for _k, _v in _keep145.items():
            if _v is None:
                daemon.STATE.pop(_k, None)
            else:
                daemon.STATE[_k] = _v
        for _p in _sl145:
            _p.kill()
            _p.wait()



print("\n146. a Mac restarts the bridge through the gate (8.94): the pid on")
print("   the port is found without netstat, the stop is SIGTERM and writes")
print("   clean_shutdown, closing the bridge's Terminal (SIGHUP) is a clean")
print("   stop too, the start is bridge.command in Terminal - or headless for")
print("   a throwaway run - and the panel's restart button does the same. A")
print("   REAL daemon on a port of its own, with its own data, never 8765")
print("   FAILURE WOULD LOOK LIKE: pid_on_port None for a daemon serving the")
print("   port, a stop that says 'not running' and leaves it running, a")
print("   'Previous stop was not clean', or a launcher that loses BRIDGE_PORT")
import shutil as _sh146                                      # noqa: E402
import signal                                                # noqa: E402
import urllib.request as _ur146                              # noqa: E402
from bridgecore import relayout as _rl146                    # noqa: E402
check("relayout has its POSIX stop and start, and the seam",
      sorted(n for n in ("start_mode", "_start_posix", "_stop_posix",
                         "TERMINAL_OPEN") if not hasattr(_rl146, n)), [])
_mode146 = getattr(_rl146, "start_mode", lambda *a, **k: None)
check("a Mac serving the live folder starts in Terminal; a throwaway run "
      "and a machine without Terminal start headless",
      [_mode146("darwin", "/Users/someone/bridge/source/data"),
       _mode146("darwin", os.path.join(tempfile.gettempdir(), "x", "data")),
       _mode146("linux", "/home/someone/bridge/source/data")],
      ["terminal", "headless", "headless"])

print("   the panel's browser, by config.json's \"browser\" (8.95)")
_wb146 = []
import webbrowser as _webb146                                # noqa: E402
_wbsaved146 = (_webb146.open, store.journal, daemon.CFG.get("browser"))
_lines146 = []


class _Run146:
    def __init__(self, rc, err=""):
        self.rc, self.err, self.argv = rc, err, []

    def __call__(self, argv, **kw):
        self.argv.append(list(argv))
        return subprocess.CompletedProcess(argv, self.rc, "", self.err)


try:
    _webb146.open = lambda url, *a, **k: _wb146.append(url) or True
    store.journal = lambda *a, **k: _lines146.append(a)
    _open146 = getattr(daemon, "open_panel", None)
    check("the daemon has its panel opener", callable(_open146), True)
    if callable(_open146):
        daemon.CFG["browser"] = ""
        _r0 = _Run146(0)
        check("no browser named: the default one, and nothing run",
              (_open146("http://127.0.0.1:1/", run=_r0, platform="darwin"),
               _wb146, _r0.argv), ("default", ["http://127.0.0.1:1/"], []))
        del _wb146[:]
        daemon.CFG["browser"] = "Firefox"
        _r1 = _Run146(0)
        check("Firefox named on a Mac: open -a, no Apple Event, no default",
              (_open146("http://127.0.0.1:1/", run=_r1, platform="darwin"),
               _r1.argv, _wb146),
              ("Firefox", [["/usr/bin/open", "-a", "Firefox",
                            "http://127.0.0.1:1/"]], []))
        daemon.CFG["browser"] = "NoSuchBrowser146"
        _r2 = _Run146(1, "Unable to find application named "
                         "'NoSuchBrowser146'")
        check("a browser that is not there: the default one, and one line "
              "in the journal saying so",
              (_open146("http://127.0.0.1:1/", run=_r2, platform="darwin"),
               _wb146, len(_lines146),
               "NoSuchBrowser146" in str(_lines146)),
              ("default", ["http://127.0.0.1:1/"], 1, True))
finally:
    _webb146.open, store.journal = _wbsaved146[0], _wbsaved146[1]
    if _wbsaved146[2] is None:
        daemon.CFG.pop("browser", None)
    else:
        daemon.CFG["browser"] = _wbsaved146[2]

print("   the previous run's dead Terminal window, by its own tty only")
_cl146 = []
_csaved146 = (getattr(sessions, "TERMINAL_CLOSE", None),
              getattr(sessions, "viewer_refused", None),
              daemon.STATE.get("console_tty"), store.journal)
_prev146 = getattr(daemon, "_PREV_CONSOLE", None)
try:
    sessions.TERMINAL_CLOSE = lambda ttys: (_cl146.append(list(ttys))
                                            or list(ttys))
    store.journal = lambda *a, **k: None
    _cpc = getattr(daemon, "close_previous_console", None)
    check("the daemon has its close of the previous window",
          (callable(_cpc), isinstance(_prev146, list)), (True, True))
    if callable(_cpc) and isinstance(_prev146, list):
        daemon.STATE["console_tty"] = "/dev/ttys901"
        _prev146[0] = "/dev/ttys900"
        check("inside a suite: refused, nothing closed",
              (_cpc(), _cl146), ([], []))
        sessions.viewer_refused = lambda *a, **k: ""
        print("   a tty is not a window: a restart's new window takes the tty")
        print("   its old one let go of - on the Mac all four bridge.command")
        print("   windows were /dev/ttys000 (2026-10-13), and skipping this")
        print("   run's tty closed none of them (DECISIONS.md 8.113)")
        _prev146[0] = "/dev/ttys901"
        check("past the gate, the previous run's on this run's own tty: "
              "asked, and closed", (_cpc(), _cl146),
              (["/dev/ttys901"], [["/dev/ttys901"]]))
        check("this run's own window is safe by the script itself: one "
              "tab, and no process left in it",
              all(x in sessions.CLOSE_SCRIPT
                  for x in ("(count of tabs of w) is 1",
                            "(count of (processes of t)) is 0")), True)
        _cl146[:] = []
        _prev146[0] = "/dev/ttys900"
        check("past the gate, the previous run's: closed, once",
              (_cpc(), _cpc(), _cl146),
              (["/dev/ttys900"], [], [["/dev/ttys900"]]))
finally:
    for _n, _v in (("TERMINAL_CLOSE", _csaved146[0]),
                   ("viewer_refused", _csaved146[1])):
        if _v is None:
            if hasattr(sessions, _n):
                delattr(sessions, _n)
        else:
            setattr(sessions, _n, _v)
    store.journal = _csaved146[3]
    if _csaved146[2] is None:
        daemon.STATE.pop("console_tty", None)
    else:
        daemon.STATE["console_tty"] = _csaved146[2]
    if isinstance(_prev146, list):
        _prev146[0] = ""


print("   under a locked screen the tab goes and the window stays: Terminal")
print("   keeps an empty window, no tab, nothing in it, and a second close")
print("   does not remove it (the Mac, 2026-10-13: 21683 and 21702). The")
print("   bridge keeps the id of the empty window ITS close left, and closes")
print("   it once the screen is unlocked - or says it went by itself")
print("   (DECISIONS.md 8.116)")
print("   FAILURE WOULD LOOK LIKE: \"is closed\" in the journal while the")
print("   window stays, an empty window that was there before the close")
print("   taken for the bridge's, or a close sent while still locked")
_h146 = {"husks": ["500"], "locked": True, "asked": [], "takes": True}


def _husks146(close):
    if close:
        _h146["asked"].append(list(close))
        if not _h146["locked"] and _h146["takes"]:
            _h146["husks"] = [w for w in _h146["husks"] if w not in close]
    return list(_h146["husks"])


def _tabclose146(ttys):
    _h146["husks"].append("777")         # the lock keeps the window
    return list(ttys)


_hl146 = []
_hsaved146 = tuple(getattr(sessions, n, None) for n in
                   ("TERMINAL_CLOSE", "TERMINAL_HUSKS", "SCREEN_LOCKED",
                    "viewer_refused")) + (store.journal,
                                          daemon.STATE.get("terminal_husks"),
                                          daemon.STATE.get("console_tty"))
try:
    _ch146 = getattr(daemon, "close_husks", None)
    check("the daemon has its pass over the empty windows, and the window "
          "layer its seams", (callable(_ch146),
                              hasattr(sessions, "TERMINAL_HUSKS"),
                              hasattr(sessions, "SCREEN_LOCKED"),
                              callable(getattr(sessions, "terminal_husks",
                                               None)),
                              callable(getattr(sessions, "screen_locked",
                                               None))),
          (True, True, True, True, True))
    if callable(_ch146) and isinstance(_prev146, list):
        sessions.TERMINAL_CLOSE = _tabclose146
        sessions.TERMINAL_HUSKS = _husks146
        sessions.SCREEN_LOCKED = lambda: _h146["locked"]
        sessions.viewer_refused = lambda *a, **k: ""
        store.journal = lambda *a, **k: _hl146.append(" ".join(
            str(x) for x in a))
        daemon.STATE.pop("terminal_husks", None)
        _prev146[0] = "/dev/ttys900"
        _r146 = daemon.close_previous_console()
        check("locked: the tab is closed, and only the window that close "
              "left is kept - 500 was empty before it", (_r146,
              daemon.STATE.get("terminal_husks")),
              (["/dev/ttys900"], ["777"]))
        check("and the journal does not say \"is closed\": it names the "
              "lock and the empty window", (any("is closed -" in x
              for x in _hl146), any("locked" in x and "777" in x
                                    for x in _hl146)), (False, True))
        _hl146[:] = []
        check("still locked: nothing is sent, the id is kept",
              (_ch146(), _h146["asked"], daemon.STATE.get("terminal_husks")),
              ([], [], ["777"]))
        _h146["locked"] = False
        check("unlocked: 777 is closed, and 500, not the bridge's, never "
              "asked of", (_ch146(), _h146["asked"], _h146["husks"],
                           daemon.STATE.get("terminal_husks")),
              (["777"], [["777"]], ["500"], []))
        check("and the journal says it was closed after the unlock",
              any("777" in x and "unlock" in x for x in _hl146), True)
        print("   the window went by itself with the unlock: said so, and")
        print("   nothing is sent")
        _hl146[:] = []
        _h146.update(locked=True, asked=[])
        _prev146[0] = "/dev/ttys900"
        daemon.close_previous_console()
        _h146["husks"].remove("777")
        _h146["locked"] = False
        check("gone by itself: no close, the id dropped, the journal says so",
              (_ch146(), _h146["asked"], daemon.STATE.get("terminal_husks"),
               any("777" in x and "by itself" in x for x in _hl146)),
              ([], [], [], True))
        print("   a close that does not take after the unlock is tried a few")
        print("   rounds, then the person is told and the id let go")
        _hl146[:] = []
        _h146.update(locked=True, asked=[], takes=False)
        _prev146[0] = "/dev/ttys900"
        daemon.close_previous_console()
        _h146["locked"] = False
        _tries146 = [_ch146() for _ in range(daemon.HUSK_TRIES + 1)]
        check("tried HUSK_TRIES rounds, then dropped with a warning",
              (len(_h146["asked"]), daemon.STATE.get("terminal_husks"),
               any("777" in x and "by hand" in x for x in _hl146)),
              (daemon.HUSK_TRIES, [], True))
        print("   unlocked and nothing left behind: \"is closed\", nothing kept")
        _hl146[:] = []
        _h146.update(husks=["500"], asked=[], takes=True)
        sessions.TERMINAL_CLOSE = lambda ttys: list(ttys)
        _prev146[0] = "/dev/ttys900"
        check("the plain close: the old line, no id kept",
              (daemon.close_previous_console(),
               daemon.STATE.get("terminal_husks") or [],
               any("is closed -" in x for x in _hl146)),
              (["/dev/ttys900"], [], True))
finally:
    for _n, _v in zip(("TERMINAL_CLOSE", "TERMINAL_HUSKS", "SCREEN_LOCKED",
                       "viewer_refused"), _hsaved146):
        if _v is None and _n in ("TERMINAL_CLOSE", "viewer_refused"):
            if hasattr(sessions, _n):
                delattr(sessions, _n)
        else:
            setattr(sessions, _n, _v)
    store.journal = _hsaved146[4]
    for _k, _v in (("terminal_husks", _hsaved146[5]),
                   ("console_tty", _hsaved146[6])):
        if _v is None:
            daemon.STATE.pop(_k, None)
        else:
            daemon.STATE[_k] = _v
    if isinstance(_prev146, list):
        _prev146[0] = ""


def _free146():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    n = s.getsockname()[1]
    s.close()
    return n


def _lsof146(port):
    """The listeners on a port, asked here and not of the code under test,
    so the cleanup works on a tree where pid_on_port cannot. netstat's
    table first, read by this helper's own few lines: a system-wide lsof
    waits on every process's files, and one process stuck on the share
    held it past 30 s on 2026-10-14 - this said [] for a daemon that was
    listening (8.131). lsof where netstat names no process."""
    try:
        r = subprocess.run(["/usr/sbin/netstat", "-anv", "-p", "tcp"],
                           capture_output=True, text=True, timeout=30)
        lines = r.stdout.splitlines()
        if any("process:pid" in ln.split() for ln in lines[:3]):
            out = []
            for ln in lines:
                w = ln.split()
                if len(w) > 6 and w[0].startswith("tcp") \
                        and w[5] == "LISTEN" and w[3].endswith(".%d" % port):
                    out += [int(t.rpartition(":")[2]) for t in w[6:]
                            if ":" in t and t.rpartition(":")[2].isdigit()][:1]
            return out
    except Exception:
        pass
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % port,
                            "-sTCP:LISTEN", "-t"], capture_output=True,
                           text=True, timeout=30)
        return [int(w) for w in r.stdout.split() if w.isdigit()]
    except Exception:
        return []


def _gone146(pid):
    """Ended: no such process, or a zombie nobody has reaped yet - asked
    of ps, not of the code under test."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return not st or st.startswith("Z")


def _journal146(data):
    out = ""
    for day in sorted(os.listdir(os.path.join(data, "logs"))
                      if os.path.isdir(os.path.join(data, "logs")) else []):
        f = os.path.join(data, "logs", day, "events.jsonl")
        if os.path.isfile(f):
            with io.open(f, encoding="utf-8", errors="replace") as fh:
                out += fh.read()
    return out


def _state146(data):
    try:
        with io.open(os.path.join(data, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _wait146(fn, wait=60):
    end = time.time() + wait
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.3)
    return fn()


if os.name == "nt":
    skip("146's real daemon: stop by SIGTERM, start, SIGHUP, the panel's "
         "restart", "Windows - case 103 stops a real console there")
else:
    _base146 = tempfile.mkdtemp(prefix="b146-")
    _src146 = os.path.join(_base146, "source")
    _data146 = os.path.join(_base146, "data")
    _sh146.copytree(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "bridgecore"),
                    os.path.join(_src146, "bridgecore"),
                    ignore=_sh146.ignore_patterns("__pycache__"))
    os.makedirs(_data146)
    _port146 = _free146()
    _cfgport146 = _free146()
    while _cfgport146 == _port146:
        _cfgport146 = _free146()
    # The config names ANOTHER free port: a restart that takes CFG's port
    # instead of the one the daemon serves waits on a port nobody holds,
    # and never on the live one.
    with io.open(os.path.join(_data146, "config.json"), "w",
                 encoding="utf-8") as _fh:
        json.dump({"port": _cfgport146}, _fh)
    _envkeep146 = {k: os.environ.get(k) for k in
                   ("BRIDGE_DATA", "BRIDGE_PORT", "PYTHONPATH", "BROWSER")}
    os.environ.update({"BRIDGE_DATA": _data146, "BRIDGE_PORT": str(_port146),
                       "PYTHONPATH": _src146, "BROWSER": "/usr/bin/true"})
    _opened146 = []
    _launched146 = []
    try:
        check("the ports are this case's own", (_port146 != 8765,
              _cfgport146 != 8765, _lsof146(_port146)), (True, True, []))
        _ok, _why = _rl146.start_daemon(_base146, wait=90, port=_port146)
        check("a throwaway run's start: headless, and it answers",
              (_ok, "without a window" in _why), (True, True))
        _pidA = _rl146.pid_on_port(_port146)
        check("pid_on_port names the daemon on the port (a Mac's netstat "
              "has no -o)",
              (isinstance(_pidA, int), _pidA in _lsof146(_port146)),
              (True, True))
        _dead146 = subprocess.Popen(["/usr/bin/true"])
        _dead146.wait()
        check("process_alive: the daemon yes, an ended pid no",
              (bool(_pidA) and _rl146.process_alive(_pidA),
               _rl146.process_alive(_dead146.pid)), (True, False))
        _ok, _why = _rl146.stop_daemon(_port146, timeout=60)
        print("       stop: %s" % _why)
        check("the stop: SIGTERM, the process gone, the port closed",
              (_ok, "SIGTERM" in _why, bool(_pidA) and _gone146(_pidA),
               _rl146.port_open(_port146)), (True, True, True, False))
        check("and it was the daemon's own clean stop: clean_shutdown in its "
              "state, its goodbye in its journal",
              (_state146(_data146).get("clean_shutdown"),
               _journal146(_data146).count("Bridge stopped cleanly.")),
              (True, 1))

        _ok, _why = _rl146.start_daemon(_base146, wait=90, port=_port146)
        _pidB = _rl146.pid_on_port(_port146)
        check("started again: not a recovery",
              (_ok, _journal146(_data146).count("Bridge started."),
               "Previous stop was not clean" in _journal146(_data146),
               _state146(_data146).get("mode")),
              (True, 2, False, "running"))

        print("   the panel's restart button: POST /relayout")
        try:
            # the panel sends the secret since 2026-10-11, and so does this
            _rq = _ur146.Request("http://127.0.0.1:%d/relayout" % _port146,
                                 data=b"{}", headers={
                                     "Content-Type": "application/json",
                                     "X-Bridge-Secret": daemon.SECRET})
            _reply146 = json.loads(_ur146.urlopen(_rq, timeout=30).read())
        except Exception as exc:
            _reply146 = {"error": str(exc)}
        print("       reply: %s" % json.dumps(_reply146)[:300])
        check("it answers, and stops", _reply146.get("ok"), True)
        _pidC = _wait146(lambda: (lambda p: p if p and p != _pidB else None)(
            _rl146.pid_on_port(_port146)), 120 if _reply146.get("ok") else 0)
        _helper146 = os.path.join(_data146, "logs", "relayout-helper.txt")

        def _hread146():
            if not os.path.isfile(_helper146):
                return ""
            with io.open(_helper146, encoding="utf-8", errors="replace") as fh:
                return fh.read()

        # the helper sees the new bridge on its next look at the port, and
        # until then the new bridge is ITS child: a stop before that leaves
        # a zombie the helper goes on waiting for
        _hl146 = _wait146(lambda: (lambda t: t if "answering again" in t
                                   else "")(_hread146()),
                          60 if _pidC else 0) or _hread146()
        print("       helper said: %s" % " | ".join(_hl146.split("\n")[:6]))
        check("a new daemon serves the port, the old one is gone, and the "
              "helper said so in its log",
              (bool(_pidC), bool(_pidB) and _gone146(_pidB),
               "answering again" in _hl146),
              (True, True, True))
        check("the button's stop was clean too",
              (_journal146(_data146).count("Bridge stopped cleanly."),
               "Previous stop was not clean" in _journal146(_data146)),
              (2, False))

        print("   closing the bridge's Terminal window: SIGHUP")
        if _pidC:
            os.kill(_pidC, signal.SIGHUP)
            _wait146(lambda: _gone146(_pidC) and not _rl146.port_open(
                _port146), 30)
        check("SIGHUP is a clean stop",
              (bool(_pidC) and _gone146(_pidC),
               _journal146(_data146).count("Bridge stopped cleanly."),
               _state146(_data146).get("clean_shutdown")),
              (True, 3, True))

        print("   the Mac's start: bridge.command in Terminal, through the seam")
        with io.open(os.path.join(_base146, "bridge.command"), "w",
                     encoding="utf-8", newline="\n") as _fh:
            # the stand-in refuses to start without the port it was given:
            # a launcher that lost it must not reach for the live one
            _fh.write("#!/bin/sh\n"
                      "[ -n \"$BRIDGE_PORT\" ] || { echo no BRIDGE_PORT; "
                      "exit 3; }\n"
                      "cd \"$(dirname \"$0\")/source\" || exit 4\n"
                      "PYTHONPATH=\"$PWD\" exec %s -m bridgecore.daemon "
                      "--no-browser\n" % sys.executable)
        os.chmod(os.path.join(_base146, "bridge.command"), 0o700)

        def _term146(argv):
            # what Terminal does with the file: runs it with ITS
            # environment, which has neither BRIDGE_PORT nor BRIDGE_DATA
            _opened146.append(list(argv))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("BRIDGE_PORT", "BRIDGE_DATA", "PYTHONPATH")}
            log = open(os.path.join(_base146, "terminal-stand-in.txt"), "ab")
            _launched146.append(subprocess.Popen(
                ["/bin/sh", argv[-1]], env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
            log.close()

        _rl146.TERMINAL_OPEN = _term146
        _sp146 = getattr(_rl146, "_start_posix", None)
        _ok, _why = _sp146(_base146, 60, _port146, how="terminal") \
            if _sp146 else (False, "relayout has no _start_posix")
        _launcher146 = os.path.join(_data146, "logs", "start-bridge.command")
        _lt146 = ""
        if os.path.isfile(_launcher146):
            with io.open(_launcher146, encoding="utf-8") as _fh:
                _lt146 = _fh.read()
        check("one open, in the background, of a launcher carrying the port "
              "and the folder Terminal would drop",
              (_opened146, "BRIDGE_PORT=%d; export BRIDGE_PORT" % _port146
               in _lt146, "BRIDGE_DATA=" in _lt146),
              ([["/usr/bin/open", "-g", "-a", "Terminal", _launcher146]],
               True, True))
        _pidD = _rl146.pid_on_port(_port146)
        print("       start: %s" % _why)
        check("and the bridge it started answers on its own port",
              (_ok, "Terminal" in _why, bool(_pidD)), (True, True, True))
        # only onto a served port: onto a free one it would simply start
        _held146 = subprocess.run(
            [sys.executable, "-m", "bridgecore.daemon", "--no-browser"],
            cwd=_src146, capture_output=True, text=True, timeout=120) \
            if _rl146.port_open(_port146) else None
        check("a second start on a served port names the pid that holds it",
              (_held146 and _held146.returncode, bool(_held146) and
               "(pid %d)" % (_pidD or 0) in _held146.stdout), (1, True))
        _ok, _why = _rl146.stop_daemon(_port146, timeout=60)
        print("       stop: %s" % _why)
        check("and it stops through the gate's stop as well",
              (_ok, _rl146.port_open(_port146),
               _journal146(_data146).count("Bridge stopped cleanly.")),
              (True, False, 4))
    finally:
        _rl146.TERMINAL_OPEN = None
        for _k, _v in _envkeep146.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _pid in _lsof146(_port146):
            try:
                os.kill(_pid, signal.SIGKILL)
            except OSError:
                pass
        for _p in _launched146:
            try:
                _p.kill()
            except OSError:
                pass
            _p.wait()
        time.sleep(0.5)
        check("nothing of this case listens any more",
              (_lsof146(_port146), _lsof146(_cfgport146)), ([], []))
        _sh146.rmtree(_base146, ignore_errors=True)
        check("and its folder is gone", os.path.exists(_base146), False)

print("\n147. the way back between machines, through the launch and the lock:")
print("   while the other machine works the folder, a launch here refuses")
print("   and repairs nothing; once its loop is off and the lock given back,")
print("   the launch here takes the lock and puts this machine's hooks,")
print("   status line and channel entry back - and then the same the other")
print("   way round. Nothing in launch is stubbed: the suite's refusal of a")
print("   real client stops it, after the gate (8.10, 8.87, 8.90, 8.96)")
from bridgecore import install as _inst147                  # noqa: E402
from bridgecore import projlock as _pl147                   # noqa: E402
# The other OS's install in the shapes it really writes: a Mac names the
# Command Line Tools' python3 by its path, a PC an absolute python.exe with
# no "/" in it - here a placeholder of that shape, which check_public passes.
_other147 = (("/Library/Developer/CommandLineTools/usr/bin/python3",
              "/Users/<account>/bridge/source") if os.name == "nt"
             else ("C:\\path\\to\\my-python", "C:\\projects\\my-bridge"))
_d147 = os.path.join(TMP, "case147")
_p147 = os.path.join(_d147, "project")
os.makedirs(os.path.join(_p147, ".claude"))
_sp147 = os.path.join(_p147, ".claude", "settings.json")
_mp147 = os.path.join(_p147, ".mcp.json")
_own147 = {"type": "command", "command": "echo own-147"}
with open(_sp147, "w", encoding="utf-8") as fh:
    _json.dump({"hooks": {"PostToolUse": [{"matcher": "Bash",
                                           "hooks": [_own147]}]}}, fh)
with open(_mp147, "w", encoding="utf-8") as fh:
    _json.dump({"mcpServers": {"own147": {"command": "own-server-147"}}}, fh)
_inst147.install(_p147, "executor", python=sys.executable)
_holder147 = os.path.join(_d147, "other_machine.py")
with open(_holder147, "w", encoding="utf-8") as fh:
    fh.write(r'''import json, os, sys, threading
sys.path.insert(0, %r)
from bridgecore import projlock
_t = threading.Timer(90, os._exit, (3,))      # never outlives the case
_t.daemon = True
_t.start()
a = json.loads(sys.argv[1])
projlock.MACHINE = "the-other-machine-147"
projlock.BEAT_SEC = 1e9
ok, why = projlock.acquire(a["proj"])
if ok:
    # what that machine's install leaves in the folder it now works
    py, root = a["py"], a["root"]
    sep = "\\" if root[1:2] == ":" else "/"    # its own separator

    def script(name):                         # by path since 8.122
        return sep.join((root, "bridgecore", name + ".py"))
    s = os.path.join(a["proj"], ".claude", "settings.json")
    m = os.path.join(a["proj"], ".mcp.json")
    with open(s, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for groups in cfg["hooks"].values():
        for g in groups:
            for h in g.get("hooks") or []:
                if str((h.get("args") or [""])[-1]).replace(
                        "\\", "/").endswith("bridgecore/hook.py"):
                    h["command"] = py
                    h["args"] = [script("hook")]
    cfg["statusLine"]["command"] = '"%%s" "%%s"' %% (py, script("statusline"))
    cfg["env"]["PYTHONPATH"] = root
    with open(s, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    with open(m, encoding="utf-8") as fh:
        mcp = json.load(fh)
    mcp["mcpServers"]["bridge"]["command"] = py
    mcp["mcpServers"]["bridge"]["args"] = [script("channel")]
    mcp["mcpServers"]["bridge"]["env"]["PYTHONPATH"] = root
    with open(m, "w", encoding="utf-8") as fh:
        json.dump(mcp, fh, indent=2)
print(json.dumps({"ok": ok, "why": why}), flush=True)
sys.stdin.readline()                          # its loop goes off
print(json.dumps({"released": projlock.release()}), flush=True)
''' % os.path.dirname(os.path.abspath(__file__)))


def _other_takes147(n):
    env = dict(os.environ)
    env["BRIDGE_DATA"] = os.path.join(_d147, "other-data-%d" % n)
    p = subprocess.Popen(
        [sys.executable, _holder147,
         _json.dumps({"proj": _p147, "py": _other147[0],
                      "root": _other147[1]})],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
    try:
        said = _json.loads(p.stdout.readline() or "{}")
    except ValueError:
        said = {}
    return p, said


def _gives_back147(p):
    try:
        p.stdin.write("\n")
        p.stdin.flush()
        said = _json.loads(p.stdout.readline() or "{}")
    except (OSError, ValueError):
        said = {}
    p.wait(30)
    return said


def _bytes147():
    with open(_sp147, "rb") as a, open(_mp147, "rb") as b:
        return a.read(), b.read()


def _here147():
    """(hooks, status line ok, channel entry ok, PYTHONPATH ours, a trace
    of the other machine left in either file)"""
    with open(_sp147, encoding="utf-8") as fh:
        cfg = _json.load(fh)
    with open(_mp147, encoding="utf-8") as fh:
        m = _json.load(fh)
    text = b"".join(_bytes147()).decode("utf-8")
    return (sorted({h.get("command") for groups in cfg["hooks"].values()
                    for g in groups for h in g.get("hooks") or []
                    if _inst147.is_bridge_hook(h)}),
            _inst147.status_line_ok(cfg.get("statusLine")),
            _inst147.channel_entry_ok(m["mcpServers"].get("bridge")),
            _inst147.same_root(cfg["env"].get("PYTHONPATH")),
            any(_json.dumps(x)[1:-1] in text for x in _other147))


def _own_here147():
    with open(_sp147, encoding="utf-8") as fh:
        cfg = _json.load(fh)
    with open(_mp147, encoding="utf-8") as fh:
        m = _json.load(fh)
    return (_own147 in [h for g in cfg["hooks"].get("PostToolUse") or []
                        for h in g.get("hooks") or []],
            m["mcpServers"].get("own147"))


def _baks147():
    return sorted(n for n in os.listdir(os.path.dirname(_sp147))
                  if n.startswith("settings.json.before-bridge-python"))


_lines147 = []
_jsaved147 = store.journal


def _launch147():
    del _lines147[:]
    store.journal = lambda *a, **k: _lines147.append(
        (a[1] if len(a) > 1 else "", (a[4] if len(a) > 4
                                      else k.get("level", ""))))
    try:
        sessions.launch(_p147, "executor")
        return "launched"
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:                # speak, do not crash the suite
        return "crashed: %r" % exc
    finally:
        store.journal = _jsaved147


_procs147 = []
try:
    _h147, _said147 = _other_takes147(1)
    _procs147.append(_h147)
    _carried147 = _bytes147()
    check("the other machine took the folder and left its install in it",
          (_said147.get("ok"), any(
              _other147[0] in g for g in _inst147.marks_missing(_p147))),
          (True, True))
    _r147 = _launch147()
    check("a launch here is refused, naming the other machine, and neither "
          "file is touched - its live windows' hooks stay as it wrote them",
          ("the-other-machine-147" in _r147, _bytes147() == _carried147,
           [ln for ln, _lv in _lines147 if "bridge marks" in ln]),
          (True, True, []))
    print("   its loop goes off: the lock is given back")
    check("the claim is gone from the folder",
          (_gives_back147(_h147).get("released"),
           os.path.exists(_pl147.lock_path(_p147))), (1, False))
    _r147 = _launch147()
    check("the launch here gets past the lock and the gate, and is stopped "
          "only by the suite's refusal of a real client",
          "refused as a real client" if "BRIDGE_REAL_CLIENT" in _r147
          else _r147, "refused as a real client")
    check("the gate on the way repaired it: nothing missing, every mark "
          "names this machine, no trace of the other in either file",
          (_inst147.marks_missing(_p147), _here147()),
          ([], ([sys.executable], True, True, True, False)))
    check("the project's own hook and its own server came through",
          _own_here147(), (True, {"command": "own-server-147"}))
    _first147 = os.path.join(os.path.dirname(_sp147), "settings.json"
                             ".before-bridge-python")


    def _read147(path):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    _firstbytes147 = _read147(_first147)
    check("what the other machine left is kept beside it, byte for byte",
          (_baks147(), _firstbytes147 == _carried147[0]),
          (["settings.json.before-bridge-python"], True))
    check("and it said so: the repair at warn, naming the other machine's "
          "python, then that the marks are back",
          ([lv for ln, lv in _lines147 if "missing bridge marks" in ln
            and _other147[0] in ln],
           [lv for ln, lv in _lines147 if "bridge marks restored" in ln]),
          (["warn"], ["log"]))
    try:
        _claim147 = _json.loads(_read147(_pl147.lock_path(_p147)) or "{}")
    except ValueError:
        _claim147 = {}
    check("this machine works the folder now, under its own name",
          (_pl147.holds(_p147), _claim147.get("machine")),
          (True, _pl147.machine()))

    print("   and the other way round: this loop goes off, the other machine")
    print("   takes the folder and works it, gives it back, and it comes here")
    print("   again")
    check("this machine gives the lock back, and the claim is gone",
          (_pl147.release(_p147), os.path.exists(_pl147.lock_path(_p147))),
          (1, False))
    time.sleep(1.1)                     # the later backups are named by second
    _h147b, _said147b = _other_takes147(2)
    _procs147.append(_h147b)
    _carried147b = _bytes147()
    check("the other machine takes it and its install flips the folder",
          (_said147b.get("ok"), _here147()[1:]),
          (True, (False, False, False, True)))
    check("it gives it back", _gives_back147(_h147b).get("released"), 1)
    _r147 = _launch147()
    check("and the launch here repairs it again",
          ("BRIDGE_REAL_CLIENT" in _r147, _inst147.marks_missing(_p147),
           _here147(), _own_here147()[0]),
          (True, [], ([sys.executable], True, True, True, False), True))
    _second147 = [n for n in _baks147()
                  if n != "settings.json.before-bridge-python"]
    _secondbytes147 = _read147(os.path.join(
        os.path.dirname(_sp147), _second147[0])) if _second147 else None
    check("the first backup is never written over, and the second move's "
          "is beside it, as the other machine left the folder",
          (len(_baks147()), _firstbytes147 is not None
           and _read147(_first147) == _firstbytes147,
           _secondbytes147 == _carried147b[0]),
          (2, True, True))
finally:
    store.journal = _jsaved147
    _pl147.release(_p147)
    for _p in _procs147:
        if _p.poll() is None:
            _p.kill()
        _p.wait()
    check("no claim of this case is left, here or in the folder",
          (_pl147.holds(_p147), os.path.exists(_pl147.lock_path(_p147))),
          (False, False))

print("\n148. the public scan refuses what THIS machine is called (8.98): every")
print("   name, address and key the machine gives - its account, its own and")
print("   its neighbours' host names, the share's login, its networks, the")
print("   keys it holds and trusts by name, body and fingerprint - is a hit")
print("   where it stands; it is asked of the machine and written nowhere,")
print("   here neither: a check below names kinds, never a value. The forms")
print("   no machine is asked about are hits on any machine; placeholders pass,")
print("   and so does a name made of generic words only (bridge-mac)")
try:
    import check_public as _cp148                               # noqa: E402
except ImportError:
    _cp148 = None
if _cp148 is None:
    skip("the public scan refuses this machine's names", "no check_public.py "
         "beside this suite")
elif not hasattr(_cp148, "machine_values"):
    check("the public scan asks this machine what it is called", False, True)
else:
    import base64 as _b64148                                    # noqa: E402
    _cp148.SESSION_PREFIXES = _cp148.SESSION_PREFIXES or set()
    _vals148 = _cp148.machine_values()

    def _line148(kind, v):
        # each value as it would stand in a sentence: a network's prefix
        # with a host after it, a login before its server, a key's first
        # forty characters with the rest of the key after them
        if kind == "network address":
            return "the other machine answers at " + v + "23"
        if kind == "share login":
            return "mount it as smb://" + v + "server/X"
        if kind == "ssh key":
            return "its key is ssh-ed25519 " + v + "Zm9vYmFy"
        return "see " + v + " for this"

    def _kinds148(line):
        _h = []
        _cp148.scan_text("MAC.md", line, _h)
        return sorted(set(h[2] for h in _h if h[1] == 1))

    check("the machine gives its account name at least",
          "account name" in [k for k, _v in _vals148], True)
    check("every value it gives is a hit of its own kind, where it stands - "
          "the kinds of those that are not",
          sorted(set(k for k, v in _vals148
                     if "this machine's " + k not in _kinds148(_line148(k, v)))),
          [])
    # made up here, owned by nobody, and written in halves: this suite is
    # published, and the scan would be right to refuse them whole
    _blob148 = _b64148.b64encode(b"\x00\x00\x00\x0bssh-ed25519"
                                 + bytes(range(40))).decode()
    _fp148 = _b64148.b64encode(bytes(range(7, 39))).decode().rstrip("=")
    check("the forms no machine is asked about are hits anywhere: a key, a "
          "fingerprint, a MacN label, a .local name, a Mac's and a Linux home",
          [_kinds148(x) for x in (
              "ssh-ed25519 " + _blob148 + " nobody",
              "SHA256" + ":" + _fp148,
              "measured on " + "Mac" + "7 that night",
              "the share is on OTHERBOX" + ".local",
              "under /Users" + "/nobodyelse/bridge",
              "under /home" + "/nobodyelse/bridge")],
          [["ssh key"], ["key fingerprint"], ["machine label"],
           ["mDNS host name"], ["home directory (Mac, Linux)"],
           ["home directory (Mac, Linux)"]])
    # the owner's classes of 2026-10-12, each refused by a form that asks
    # no machine (the values a machine gives are the check above it): a
    # line per class, written in halves as above (8.105)
    _acct148 = "za" + os.urandom(4).hex() + "k"
    _deny148 = os.path.join(TMP, "deny148.txt")
    with open(_deny148, "w", encoding="utf-8") as _fh:
        _fh.write("# made by test_handover case 148 - no one's\n"
                  "account name\t%s\n" % _acct148)
    _cls148 = [
        ("machines", "on OTHERBOX" + ".local, called Mac" + "7 in the notes",
         ["mDNS host name", "machine label"]),
        # an account no pattern can know is a word of the private list
        # since 8.141 - "account name" left RULES, where the scanner
        # published it - so this line reads the case's own list below
        ("accounts", "as " + _acct148 + ", mail someone.real" + "@"
         + "example-mail.org", ["private list's account name",
                                "e-mail address"]),
        ("addresses", "it answers at 192.168" + ".1.23, 10.0" + ".0.7, "
         "172.20" + ".3.4 and 169.254" + ".9.9",
         ["private network address"]),
        ("keys", "ssh-ed25519 " + _blob148 + "; SHA256" + ":" + _fp148
         + "; 1234567" + "89:AAH" + "x" * 32 + "; bridge-secret" + ' = "ab'
         + 'c"; chat_id' + ": -1001234567; claude.ai/code/" + "session_01ab",
         ["ssh key", "key fingerprint", "telegram bot token",
          "shared secret value", "telegram chat id",
          "claude session link"]),
        ("home folders", "C" + ":\\Users\\nobodyelse\\x, /Users"
         + "/nobodyelse, /home" + "/nobodyelse",
         ["home directory", "home directory (Mac, Linux)"])]
    # the case owns the list it reads: neither this machine's list nor a
    # public clone's lack of one decides the class; what the module had
    # loaded is put back after
    _was148 = (os.environ.get("BRIDGE_PUBLIC_DENY"), _cp148.DENY_VALUES,
               _cp148.DENY_STATE, _cp148.DENY_PROBLEM,
               _cp148.HOST_ACCOUNTS)
    os.environ["BRIDGE_PUBLIC_DENY"] = _deny148
    _cp148.DENY_VALUES = _cp148.HOST_ACCOUNTS = None
    try:
        _miss148 = [(c, sorted(set(w) - set(_kinds148(x))))
                    for c, x, w in _cls148 if set(w) - set(_kinds148(x))]
        _read148 = _cp148.DENY_STATE
    finally:
        if _was148[0] is None:
            os.environ.pop("BRIDGE_PUBLIC_DENY", None)
        else:
            os.environ["BRIDGE_PUBLIC_DENY"] = _was148[0]
        (_cp148.DENY_VALUES, _cp148.DENY_STATE, _cp148.DENY_PROBLEM,
         _cp148.HOST_ACCOUNTS) = _was148[1:]
    check("every class the public version may not mention is refused "
          "without asking this machine - machines, accounts, addresses, "
          "keys, home folders; each with the kinds it was not refused as "
          "- and the accounts by the case's own list",
          (_miss148, _read148.startswith("private list: 1 entries from")),
          ([], True))
    check("and a run of the classes is made of the classes, nothing else: "
          "a loopback, a version and a public address pass",
          _kinds148("binds 127.0.0" + ".1, python 3.10" + ".12.1, "
                    "dns 8.8" + ".8.8, 172.32" + ".0.1"), [])
    check("and the placeholders and look-alikes pass: WORKPC.local, "
          "/Users/<account>, a stand-in account, settings.local.json, "
          "threading.local(), C:\\path\\to",
          [_kinds148(x) for x in (
              "smb://user1@WORKPC.local/X%20Projects",
              "PYTHONPATH=/Users/<account>/bridge/source",
              "base=/Users/someone/bridge",
              "os.path.join(proj, '.claude', 'settings.local.json')",
              "_REQ = threading.local()",
              "C:\\path\\to\\my-python")],
          [[], [], [], [], [], []])
    # a home made here, so the machine's own keys are not asked: the PC's
    # ssh setup of 2026-10-13 tagged the Mac's key "bridge-mac", and the
    # product's own bridge-mac.json was refused there as its name (8.100)
    _home148 = os.path.join(TMP, "home148")
    os.makedirs(os.path.join(_home148, ".ssh"))
    with open(os.path.join(_home148, ".ssh", "authorized_keys"), "w") as _fh:
        _fh.write("ssh-ed25519 %s bridge-mac\nssh-ed25519 %s otherbox-key\n"
                  % (_blob148, _blob148))
    _env148 = dict((k, os.environ.get(k)) for k in ("HOME", "USERPROFILE"))
    try:
        os.environ["HOME"] = os.environ["USERPROFILE"] = _home148
        _names148 = [v for k, v in _cp148.machine_values()
                     if k == "ssh key name"]
    finally:
        for _k, _v in _env148.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
    check("a key's name made of generic words only names no machine, and "
          "one with a word of its own does: bridge-mac, otherbox-key",
          ("bridge-mac" in _names148, "otherbox-key" in _names148),
          (False, True))


print("\n149. a long command's own tree is read off Windows too (8.99):")
print("   process_table and proc_cpu answered None everywhere but Windows, so")
print("   on a Mac a working call was asked about as stuck (test_multipair")
print("   131). One `ps` gives the table; the CPU is /proc's or `ps -o time=`,")
print("   whose two forms are a Mac's minutes and procps's [D-]HH:MM:SS")
from bridgecore import sessions as _s149                        # noqa: E402
if not hasattr(_s149, "cpu_seconds"):
    check("the CPU time of `ps -o time=` can be read", False, True)
else:
    check("a Mac's form, procps's form, and nothing - as seconds or None",
          [_s149.cpu_seconds(x) for x in ("455:59.77", "0:00.01",
                                          "1-02:03:04", "00:10:00", "",
                                          "n/a")],
          [27359.77, 0.01, 93784.0, 600.0, None, None])
    check("a shell is a whole name off Windows and a prefix on it",
          ([_s149.is_shell(n, nt=False) for n in
            ("zsh", "bash", "sh", "ssh-agent", "shasum", "python")],
           [_s149.is_shell(n, nt=True) for n in
            ("cmd.exe", "powershell.exe", "bash.exe", "python.exe")]),
          ([True, True, True, False, False, False],
           [True, True, True, False]))
_t149 = _s149.process_table()
check("the table names this process, under its own parent",
      ((_t149 or {}).get(os.getpid()) or (None, ""))[0], os.getppid())
_b149 = subprocess.Popen([sys.executable, "-c",
                          "import time\nt=time.time()\n"
                          "while time.time()-t<4: pass\n"],
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
try:
    time.sleep(0.5)
    _c149 = _s149.proc_cpu(_b149.pid)
    time.sleep(1.5)
    _d149 = _s149.proc_cpu(_b149.pid)
    check("and a busy child's CPU is read, and grows",
          (_c149 is not None, _d149 is not None
           and _c149 is not None and _d149 - _c149 >= 0.5), (True, True))
finally:
    _b149.kill()
    _b149.wait()

print("\n151. a slow project folder does not hold a reply (8.117): a SessionStart")
print("     and a journal line answer at local speed while every question to")
print("     the project's share takes 0.4 s, and the project's copy still gets")
print("     every line, in order")
print("    2026-10-13, a Mac with all three projects on a PC's share: the Space")
print("    Junk windows started blind - the hook gives SessionStart 8 s and the")
print("    daemon answered in 12 - and planners were told 'NOT delivered' about")
print("    verdicts the daemon had journalled. Measured there: 6.7-17.7 s for a")
print("    SessionStart and 11.6-19.9 s for a /verdict, nearly all of it")
print("    project_name's realpath/isdir/listdir and the journal's project copy.")
import builtins as _bi151
import json as _js151
_D151 = 0.4
_SHARE151 = os.path.join(TMP, "share151")
_SLOW151 = os.path.join(_SHARE151, "Slow151")
_FAST151 = os.path.join(TMP, "fast151")
for _d151 in (_SLOW151, _FAST151):
    os.makedirs(_d151, exist_ok=True)
_SHN151 = daemon.norm(_SHARE151)


def _slow151(fn):
    def _w151(*a, **k):
        if a and isinstance(a[0], str) and \
                daemon.norm(a[0]).startswith(_SHN151):
            time.sleep(_D151)
        return fn(*a, **k)
    return _w151


_in151 = None     # set while a slow open of the project's journal is under way


def _slowopen151(fn):
    def _o151(*a, **k):
        if a and isinstance(a[0], str) and \
                daemon.norm(a[0]).startswith(_SHN151):
            if a[0].endswith("events.jsonl") and _in151 is not None:
                _in151.set()
            time.sleep(_D151)
        return fn(*a, **k)
    return _o151


_saved151 = []
_ORDER151 = ["one", "two", "three", "four", "five"]


def _rows151(base):
    _p = os.path.join(base, "bridge-logs", time.strftime("%Y-%m-%d"),
                      "events.jsonl")
    try:
        with open(_p, encoding="utf-8") as _fh:
            return [_js151.loads(_l) for _l in _fh if _l.strip()]
    except Exception:
        return []


_qc151, _rl151 = daemon.quiet_console_for, daemon.watch_rc_link
try:
    daemon.quiet_console_for = lambda *a, **k: None
    daemon.watch_rc_link = lambda *a, **k: None
    for _m151, _n151 in ((_bi151, "open"), (os.path, "isdir"),
                         (os.path, "exists"), (os.path, "realpath"),
                         (os, "listdir"), (os, "makedirs")):
        _saved151.append((_m151, _n151, getattr(_m151, _n151)))
        setattr(_m151, _n151, (_slowopen151 if _n151 == "open"
                               else _slow151)(getattr(_m151, _n151)))
    _t151 = time.time()
    os.path.isdir(_SLOW151)
    check("the share is slow: one question to it takes the 0.4 s it is "
          "given - or every timing below would prove nothing",
          time.time() - _t151 >= _D151 * 0.9, True)
    daemon.CFG.setdefault("projects", {})[_SLOW151] = {}
    # what the daemon does at its start, off the hooks' path
    getattr(daemon, "warm_project_names",
            lambda: daemon.project_name(_SLOW151))()
    daemon.STATE.clear()
    daemon.STATE.update({"sessions": {}, "compactions": {},
                         "last_session": {}, "pids": {}})
    _t151 = time.time()
    _o151, _ = daemon.handle_event({
        "hook_event_name": "SessionStart", "role": "executor",
        "session_id": "sess-151", "source": "startup",
        "project_dir": _SLOW151, "cwd": _SLOW151})
    _took151 = time.time() - _t151
    _ac151 = (((_o151 or {}).get("hookSpecificOutput") or {})
              .get("additionalContext") or "")
    print("   SessionStart: %.2f s, %d chars of start words"
          % (_took151, len(_ac151)))
    check("a SessionStart on the slow project is answered in under 1.5 s - "
          "the hook gives it 8",
          _took151 < 1.5, True)
    check("and it carries its start words", len(_ac151) > 1000, True)
    check("the name is the folder's own spelling",
          daemon.project_name(_SLOW151), "Slow151")
    _t151 = time.time()
    for _w151 in _ORDER151:
        store.journal("case151", _w151, "Slow151", "", "log",
                      project_dir=_SLOW151)
    _took151 = time.time() - _t151
    print("   five journal lines to the slow project: %.2f s" % _took151)
    check("five journal lines for the slow project hold their caller under "
          "1 s - written one by one, they were 6",
          _took151 < 1.0, True)
    # while a line is INSIDE its slow append to the slow project - on
    # another thread, as it is in the daemon - the others are written
    import threading as _th151
    _in151 = _th151.Event()
    _bg151 = _th151.Thread(target=store.journal, args=(
        "case151", "background", "Slow151", "", "log"),
        kwargs={"project_dir": _SLOW151}, daemon=True)
    _bg151.start()
    check("a slow project's line is being appended on another thread",
          _in151.wait(15), True)
    _t151 = time.time()
    store.journal("case151", "fast", "fast151", "", "log",
                  project_dir=_FAST151)
    _wf151 = store._WRITERS.get(store.norm(_FAST151))
    store.journal("case151", "central", "", "", "log")
    _took151 = time.time() - _t151
    _bg151.join(30)
    print("   a line for another project and one for the bridge, while the "
          "slow one is written: %.2f s" % _took151)
    check("a line for ANOTHER project and one for the bridge's own journal "
          "do not wait behind the slow project's",
          _took151 < 0.3, True)
    check("and the other project's line is in its file when the call returns",
          [r.get("text") for r in _rows151(_FAST151)], ["fast"])
    # The PC's acceptance of 5721b75: a writer waited a minute for more, and
    # test_multipair's last watch_rc_link lines left two of them alive past
    # its 150 s for the bridge's threads.
    _tw151 = [_t for _t in _th151.enumerate()
              if getattr(getattr(_t, "_target", None), "__self__", None)
              is _wf151] if _wf151 is not None else []
    for _t in _tw151:
        _t.join(10)
    check("the other project's writer ends within 10 s of its last line - a "
          "writer left behind is a bridge thread outliving what it served",
          (_wf151 is not None, [_t.is_alive() for _t in _tw151]),
          (True, [False] * len(_tw151)))
    check("every queued line reaches the slow project's copy",
          getattr(store, "flush_project_writes", lambda t: True)(30), True)
    check("the slow project's copy has the start's lines and the five, in "
          "the order they were written",
          ([r.get("text") for r in _rows151(_SLOW151)
            if r.get("kind") == "case151"],
           len(_rows151(_SLOW151)) >= len(_ORDER151) + 2),
          (_ORDER151 + ["background"], True))
    _cen151 = [r for r in store._read_events(
        os.path.join(store.day_dir(), "events.jsonl"))
        if r.get("path") == daemon.norm(_SLOW151)]
    check("and the same lines as the bridge's own journal, in its order",
          [r.get("at") + r.get("kind") + r.get("text") for r in _cen151],
          [r.get("at") + r.get("kind") + r.get("text")
           for r in _rows151(_SLOW151)])
    print("   the sabotage: a caller that waits for its project line again")
    _pw151 = getattr(store, "PROJECT_WRITE_WAIT", None)
    store.PROJECT_WRITE_WAIT = 30
    _t151 = time.time()
    store.journal("case151", "waited", "Slow151", "", "log",
                  project_dir=_SLOW151)
    _took151 = time.time() - _t151
    store.PROJECT_WRITE_WAIT = _pw151
    check("makes the same line cost the share's time again - so the timing "
          "checks above can fail", _took151 >= _D151, True)
finally:
    for _m151, _n151, _f151 in _saved151:
        setattr(_m151, _n151, _f151)
    daemon.quiet_console_for, daemon.watch_rc_link = _qc151, _rl151
    daemon.CFG.get("projects", {}).pop(_SLOW151, None)
    getattr(store, "flush_project_writes", lambda t: True)(30)


print("\n152. the planner's verdict is called NOT delivered only when it was")
print("     not (8.117): a call the daemon took and did not answer in time is")
print("     'may well have been recorded, do not send it again'")
print("    2026-10-13: four verdicts of one planner and one of another were")
print("    answered 'the bridge daemon is not reachable; the verdict was NOT")
print("    delivered' after 8 s - every one of them is in its project's")
print("    journal. The executor got nearly the same verdict three times, and")
print("    a planner went round the bridge to the executor directly.")
import threading as _th152
from http.server import BaseHTTPRequestHandler as _BH152
from http.server import ThreadingHTTPServer as _THS152
from bridgecore import channel as _ch152
_got152 = []
_hold152 = _th152.Event()


class _Slow152(_BH152):
    def log_message(self, *a):
        pass

    def do_POST(self):
        _n = int(self.headers.get("Content-Length") or 0)
        _got152.append((self.path, self.rfile.read(_n)))
        _hold152.wait(30)
        _b = b'{"ok": true, "delivered": true}'
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(_b)))
            self.end_headers()
            self.wfile.write(_b)
        except Exception:
            pass


def _verdict152():
    _r = _ch152.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "verdict", "arguments": {
            "verdict": "continue", "feedback": "case 152"}}})
    return ((_r or {}).get("result") or {}).get("content", [{}])[0] \
        .get("text") or ""


_srv152 = _THS152(("127.0.0.1", 0), _Slow152)
_th152.Thread(target=_srv152.serve_forever, daemon=True).start()
_saved152 = (_ch152.DAEMON, _ch152.ROLE,
             getattr(_ch152, "VERDICT_CALL_TIMEOUT", None))
try:
    check("the verdict waits as long as a task does, not 8 s",
          getattr(_ch152, "VERDICT_CALL_TIMEOUT", 8) >= 60, True)
    _ch152.DAEMON = "http://127.0.0.1:%d" % _srv152.server_address[1]
    _ch152.ROLE = "planner"
    _ch152.VERDICT_CALL_TIMEOUT = 1.5
    _t152 = time.time()
    _x152 = _verdict152()
    print("   after %.1f s: %s" % (time.time() - _t152, _x152[:150]))
    check("the daemon did take this call - the case is about a verdict "
          "that was taken",
          [p for p, _ in _got152], ["/verdict"])
    check("an answer that did not come in time is NOT called undelivered",
          "NOT delivered" in _x152, False)
    check("it says the verdict may be recorded and must not be sent again",
          ("may well have been recorded" in _x152,
           "Do NOT send it again" in _x152), (True, True))
    _hold152.set()
    print("   and when nothing is listening at all, it is said as it is")
    _srv152.shutdown()
    _srv152.server_close()
    _ch152.DAEMON = "http://127.0.0.1:%d" % _srv152.server_address[1]
    _y152 = _verdict152()
    print("   %s" % _y152[:150])
    check("a refused connection is still 'NOT delivered' - the words are "
          "kept for when they are true",
          "NOT delivered" in _y152, True)
    # The PC's acceptance of 5721b75: Windows retries the SYN to a closed
    # port on 127.0.0.1 for about 2 s, so the wait above ended as
    # "<urlopen error timed out>" there - a call that never connected,
    # called taken. urllib raises exactly this for a connect that times
    # out on any system; here it is raised on every one.
    print("   and a connect that timed out never reached the daemon either")
    _uo152 = _ch152.urllib.request.urlopen

    def _connect_timeout152(*_a, **_k):
        raise _ch152.urllib.error.URLError(socket.timeout("timed out"))
    _ch152.urllib.request.urlopen = _connect_timeout152
    try:
        _z152 = _verdict152()
    finally:
        _ch152.urllib.request.urlopen = _uo152
    print("   %s" % _z152[:150])
    check("a connect that timed out is 'NOT delivered', not 'may well have "
          "been recorded'",
          ("NOT delivered" in _z152, "may well have been recorded" in _z152),
          (True, False))
finally:
    _hold152.set()
    _ch152.DAEMON, _ch152.ROLE = _saved152[0], _saved152[1]
    if _saved152[2] is not None:
        _ch152.VERDICT_CALL_TIMEOUT = _saved152[2]
    try:
        _srv152.shutdown()
    except Exception:
        pass


print("\n153. the hook, the status line and the channel never search the")
print("     project's folder (8.122). A Mac's client runs them with Python")
print("     3.9, which ignores PYTHONSAFEPATH, and `-m bridgecore.hook` put")
print("     the project's folder - the share - first on sys.path: the import")
print("     took 41-71 s and the client cancelled every hook at its 30 s,")
print("     SessionStart included (2026-10-13). Here a project whose folder")
print("     holds a slow decoy bridgecore/ and json.py, and every command")
print("     install wrote, run the way the client runs it, from that folder:")
print("     PYTHONSAFEPATH off, as 3.9 and 3.10 run, and on")
import ast as _ast153                                       # noqa: E402
import json as _js153                                       # noqa: E402
import shutil as _sh153                                     # noqa: E402
import threading as _th153                                  # noqa: E402
from http.server import BaseHTTPRequestHandler as _BH153    # noqa: E402
from http.server import ThreadingHTTPServer as _TS153       # noqa: E402
from bridgecore import discover as _disc153                 # noqa: E402
from bridgecore import install as _in153                    # noqa: E402

_GIT153 = "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"
# the mechanisms are new in this piece: a package without them is what the
# red-first run has, and it must fail on its own lines, not end in a crash
_HOOK153 = getattr(_in153, "HOOK_SCRIPT", None)
_STATUS153 = getattr(_in153, "STATUS_SCRIPT", None)
_CHANNEL153 = getattr(_in153, "CHANNEL_SCRIPT", None)
_ours153 = getattr(_in153, "is_bridge_hook",
                   lambda h: "bridgecore" in str(h.get("args")))
_d153 = os.path.join(TMP, "case153")
_p153 = os.path.join(_d153, "project")
os.makedirs(os.path.join(_p153, "bridgecore"))
_mark153 = os.path.join(_d153, "decoys-imported.txt")
_SLOW153 = 6                      # one import from the slow folder, here
for _rel153, _nm153 in ((os.path.join("bridgecore", "__init__.py"),
                         "bridgecore"), ("json.py", "json")):
    with open(os.path.join(_p153, _rel153), "w", encoding="utf-8") as fh:
        fh.write("import time\n"
                 "with open(%r, 'a') as fh:\n"
                 "    fh.write(%r + '\\n')\n"
                 "time.sleep(%d)\n" % (_mark153, _nm153, _SLOW153))


def _decoys153():
    try:
        with open(_mark153, encoding="utf-8") as fh:
            return sorted(set(fh.read().split()))
    except OSError:
        return []


_HEARD153 = []


class _Stub153(_BH153):
    """The daemon's place: every POST heard and answered `{}` - no live
    daemon is ever asked."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        _HEARD153.append(self.path)
        out = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST

    def log_message(self, *a):
        pass


_srv153 = _TS153(("127.0.0.1", 0), _Stub153)
_th153.Thread(target=_srv153.serve_forever, daemon=True).start()


def _load153(p):
    with open(p, encoding="utf-8") as fh:
        return _js153.load(fh)


def _env153(safe, extra=None):
    """What the client hands a hook: its own env (no PYTHONPATH or
    PYTHONSAFEPATH of the run's own, 8.108), the project's settings env
    over it, the server's own env over that for the channel."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONSAFEPATH", "BRIDGE_NO_HOOKS")}
    env.update({k: str(v) for k, v in (_cfg153.get("env") or {}).items()})
    env.update({k: str(v) for k, v in (extra or {}).items()})
    if not safe:
        env.pop("PYTHONSAFEPATH", None)
    env["BRIDGE_PORT"] = str(_srv153.server_address[1])
    env["BRIDGE_ROLE"] = "executor"
    env["CLAUDE_PROJECT_DIR"] = _p153
    return env


def _run153(cmd, env, feed, shell=False):
    """(exit, stdout, stderr, seconds, decoys imported, paths posted)"""
    try:
        os.remove(_mark153)
    except OSError:
        pass
    del _HEARD153[:]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=_p153, env=env, shell=shell, input=feed,
                           capture_output=True, text=True, timeout=120)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = None, "", "timed out"
    return (rc, out, err, time.time() - t0, _decoys153(), list(_HEARD153))


try:
    _in153.install(_p153, "executor", python=sys.executable)
    _cfg153 = _load153(os.path.join(_p153, ".claude", "settings.json"))
    _mcp153 = (_load153(os.path.join(_p153, ".mcp.json")).get("mcpServers")
               or {}).get("bridge") or {}
    _hooks153 = [h for ev in _in153.EVENTS
                 for g in (_cfg153.get("hooks") or {}).get(ev) or []
                 for h in g.get("hooks") or [] if _ours153(h)]
    check("install writes every hook to run hook.py by its path - no -m",
          (len(_hooks153), sorted({str(h.get("args")) for h in _hooks153})),
          (len(_in153.EVENTS), [str([_HOOK153])]))
    check("the status line and the channel too",
          ((_cfg153.get("statusLine") or {}).get("command"),
           _mcp153.get("args")),
          ('"%s" "%s"' % (sys.executable, _STATUS153), [_CHANNEL153]))
    _hk153 = [h for g in _cfg153["hooks"]["Notification"]
              for h in g.get("hooks") or [] if _ours153(h)][0]
    for _safe153, _how153 in ((False, "PYTHONSAFEPATH off, as 3.9 runs"),
                              (True, "PYTHONSAFEPATH on")):
        _r = _run153([_hk153.get("command")] + list(_hk153.get("args") or []),
                     _env153(_safe153),
                     _js153.dumps({"hook_event_name": "Notification",
                                   "session_id": "case153"}))
        check("(%s) the hook posts its event, imports no decoy, and does not "
              "wait one slow import" % _how153,
              (_r[0], "/event" in _r[5], _r[4], _r[3] < _SLOW153),
              (0, True, [], True))
        _r = _run153(_cfg153["statusLine"]["command"], _env153(_safe153),
                     _js153.dumps({"model": {"display_name": "case-153"},
                                   "context_window": {"used_percentage": 7}}),
                     shell=True)
        check("(%s) the status line posts and prints, imports no decoy, and "
              "does not wait" % _how153,
              (_r[0], "case-153" in _r[1], "/status" in _r[5], _r[4],
               _r[3] < _SLOW153), (0, True, True, [], True))
        _r = _run153([_mcp153.get("command")] + list(_mcp153.get("args") or []),
                     _env153(_safe153, _mcp153.get("env")), "")
        check("(%s) the channel comes up, imports no decoy, and does not "
              "wait" % _how153,
              (_r[0], "bridge channel up" in _r[2], _r[4], _r[3] < _SLOW153),
              (0, True, [], True))
    print("   CONTROL (rule 19): the form before 8.122, run the same way, is")
    print("   caught by the decoys - so their silence above means something")
    _r = _run153([sys.executable, "-m", "bridgecore.hook"], _env153(False),
                 "{}")
    check("CONTROL: `-m bridgecore.hook` from that folder, PYTHONSAFEPATH off, "
          "imports the folder's bridgecore and waits on it",
          ("bridgecore" in _r[4], _r[3] >= _SLOW153), (True, True))

    print("   run by path, bridgecore/ itself is first on sys.path: no module")
    print("   there may be named like a standard one, and the three may import")
    print("   nothing of the package - run by path it is not one")
    _pkg153 = os.path.dirname(os.path.abspath(_in153.__file__))
    _mods153 = sorted(os.path.splitext(f)[0] for f in os.listdir(_pkg153)
                      if f.endswith(".py"))

    def _imports153(path):
        out = set()
        with open(path, encoding="utf-8") as fh:
            tree = _ast153.parse(fh.read())
        for node in _ast153.walk(tree):
            if isinstance(node, _ast153.Import):
                out.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, _ast153.ImportFrom):
                out.add("." if node.level
                        else (node.module or "").split(".")[0])
        return out
    _got153 = {n: _imports153(os.path.join(_pkg153, n + ".py"))
               for n in ("hook", "statusline", "channel")}
    check("hook, statusline and channel import nothing of the package",
          {n: sorted(m for m in got if m in ("bridgecore", ".") + tuple(
              _mods153)) for n, got in _got153.items()},
          {"hook": [], "statusline": [], "channel": []})
    _std153 = getattr(sys, "stdlib_module_names", None)
    if _std153 is None:
        for _n153 in ("no module in bridgecore/ is named like a standard one",
                      "and the three import the standard library only"):
            skip(_n153, "sys.stdlib_module_names is 3.10+, this is %d.%d"
                 % sys.version_info[:2])
    else:
        check("no module in bridgecore/ is named like a standard one",
              [m for m in _mods153 if m in _std153
               or m in ("sitecustomize", "usercustomize")], [])
        check("and the three import the standard library only",
              {n: sorted(m for m in got if m not in _std153)
               for n, got in _got153.items()},
              {"hook": [], "statusline": [], "channel": []})

    print("   a project installed before 8.122 is brought up to date IN PLACE:")
    print("   the interpreter as it was spelled, the timeout kept, and a")
    print("   doubled hook (8.4) one again")
    _bare153 = next((n for n in ("py", "python3",
                                 os.path.basename(sys.executable))
                     if _sh153.which(n) and _in153.interp_ok(n)[0]), None)
    _word153 = _bare153 or sys.executable
    _line153 = _bare153 or '"%s"' % sys.executable
    _q153 = os.path.join(_d153, "older")
    os.makedirs(os.path.join(_q153, ".claude"))
    _old153 = {"hooks": {ev: [{"hooks": [{
        "type": "command", "command": _word153,
        "args": ["-m", "bridgecore.hook"], "timeout": 77}]}]
        for ev in _in153.EVENTS},
        "statusLine": {"type": "command", "padding": 0,
                       "command": "%s -m bridgecore.statusline" % _line153},
        "env": {"PYTHONPATH": _in153.ROOT, "PYTHONSAFEPATH": "1"}}
    _old153["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": sys.executable,
         "args": ["-m", "bridgecore.hook"]})
    with open(os.path.join(_q153, ".claude", "settings.json"), "w",
              encoding="utf-8") as fh:
        _js153.dump(_old153, fh, indent=2)
    with open(os.path.join(_q153, ".mcp.json"), "w", encoding="utf-8") as fh:
        _js153.dump({"mcpServers": {"bridge": {
            "command": _word153, "args": ["-m", "bridgecore.channel"],
            "env": {"PYTHONPATH": _in153.ROOT,
                    "BRIDGE_PORT": os.environ.get("BRIDGE_PORT", "8765")}}}},
            fh, indent=2)
    _gap153 = _in153.marks_missing(_q153)
    check("marks_missing calls each -m form missing - the launch's gate "
          "re-runs install on that",
          [any("-m bridgecore.hook" in g for g in _gap153),
           any("status line" in g for g in _gap153),
           any("`bridge` server" in g for g in _gap153)], [True, True, True])
    _in153.install(_q153, "executor", python=sys.executable)
    _qc153 = _load153(os.path.join(_q153, ".claude", "settings.json"))
    _qm153 = _load153(os.path.join(_q153, ".mcp.json"))["mcpServers"]["bridge"]
    check("every hook runs hook.py by path, its interpreter as it was written "
          "and its timeout kept",
          sorted({(h.get("command"), str(h.get("args")), h.get("timeout"))
                  for groups in _qc153["hooks"].values() for g in groups
                  for h in g.get("hooks") or [] if _ours153(h)}),
          [(_word153, str([_HOOK153]), 77)])
    check("and the doubled Stop hook is one again (8.4)",
          len([h for g in _qc153["hooks"]["Stop"] for h in g.get("hooks") or []
               if _ours153(h)]), 1)
    check("the status line keeps its interpreter and runs the script by path",
          _qc153["statusLine"]["command"], '%s "%s"' % (_line153, _STATUS153))
    check("the channel keeps its interpreter and runs the script by path",
          (_qm153.get("command"), _qm153.get("args")),
          (_word153, [_CHANNEL153]))
    check("and the project reads whole", _in153.marks_missing(_q153), [])
    check("the panel still knows it as installed",
          _disc153.is_installed(_q153), True)

    print("   8.121: a project on a Mac's network share gets the variable that")
    print("   takes the client's git status out of every new conversation; a")
    print("   local one, or one on another system, does not")
    _ons153 = getattr(_in153, "on_network_share", None)
    if _ons153 is None:
        check("the package has install.on_network_share", False, True)
    else:
        _real153 = os.path.realpath(_p153).replace("\\", "/")
        _root153 = "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, " \
                   "journaled)\n"
        _tab153 = _root153 + (
            "//who@server/Share on %s (smbfs, nodev, nosuid, mounted by "
            "someone)\n" % _real153.rsplit("/", 1)[0].upper())
        check("on a Mac, a folder under an SMB mount is on a share - the "
              "mount point in another case too (8.89)",
              _ons153(_p153, _tab153, "darwin"), True)
        check("CONTROL: the same table anywhere but a Mac - no",
              (_ons153(_p153, _tab153, "win32"),
               _ons153(_p153, _tab153, "linux")), (False, False))
        check("CONTROL: a local disk mounted deeper, at the folder itself - "
              "no: the longest mount point decides",
              _ons153(_p153, _tab153 + "/dev/disk5s1 on %s (apfs, local, "
                      "journaled)\n" % _real153, "darwin"), False)
        check("CONTROL: a folder on the local root only - no",
              _ons153(_p153, _root153, "darwin"), False)
        _was153 = _in153.on_network_share
        try:
            _in153.on_network_share = lambda project, *a, **k: True
            _s153 = os.path.join(_d153, "on-share")
            os.makedirs(_s153)
            _in153.install(_s153, "executor", python=sys.executable)
            _ss153 = os.path.join(_s153, ".claude", "settings.json")
            check("install writes it into a share project's env - where a "
                  "live window takes it from without a restart",
                  (_load153(_ss153).get("env") or {}).get(_GIT153), "1")
            check("and the project reads whole",
                  _in153.marks_missing(_s153), [])
            _sc153 = _load153(_ss153)
            _sc153["env"].pop(_GIT153, None)
            with open(_ss153, "w", encoding="utf-8") as fh:
                _js153.dump(_sc153, fh, indent=2)
            check("a share project without it is missing a mark - so the "
                  "launch's gate puts it back",
                  any(_GIT153 in g for g in _in153.marks_missing(_s153)),
                  True)
            _in153.install(_s153, "executor", python=sys.executable)
            # 8.130: and the client's wait for an MCP server. 2026-10-14
            # 10:11 a planner raised on the share lost `bridge` to
            # "connection timed out after 30000ms" - the timer fired at
            # 64 692 ms, the client's own loop stood still - while the
            # channel was up and registered in 6 s.
            check("8.130: install gives a share project MCP_TIMEOUT 120000 "
                  "- the client's own default is 30000",
                  (_load153(_ss153).get("env") or {}).get("MCP_TIMEOUT"),
                  "120000")
            _sc153 = _load153(_ss153)
            _sc153["env"].pop("MCP_TIMEOUT", None)
            with open(_ss153, "w", encoding="utf-8") as fh:
                _js153.dump(_sc153, fh, indent=2)
            check("a share project without it is missing a mark",
                  any("MCP_TIMEOUT" in g
                      for g in _in153.marks_missing(_s153)), True)
            _sc153["env"]["MCP_TIMEOUT"] = "300000"
            with open(_ss153, "w", encoding="utf-8") as fh:
                _js153.dump(_sc153, fh, indent=2)
            _in153.install(_s153, "executor", python=sys.executable)
            check("CONTROL: a longer wait someone set is kept, and is not a "
                  "missing mark",
                  ((_load153(_ss153).get("env") or {}).get("MCP_TIMEOUT"),
                   any("MCP_TIMEOUT" in g
                       for g in _in153.marks_missing(_s153))),
                  ("300000", False))
            _in153.uninstall(_s153)
            check("CONTROL: and uninstall leaves that one where it is",
                  (_load153(_ss153).get("env") or {}).get("MCP_TIMEOUT"),
                  "300000")
            _sc153 = _load153(_ss153)
            _sc153["env"].pop("MCP_TIMEOUT", None)
            with open(_ss153, "w", encoding="utf-8") as fh:
                _js153.dump(_sc153, fh, indent=2)
            _in153.install(_s153, "executor", python=sys.executable)
            _in153.uninstall(_s153)
            check("and uninstall takes it out again",
                  _GIT153 in (_load153(_ss153).get("env") or {}), False)
            check("8.130: uninstall takes its own MCP_TIMEOUT out too",
                  "MCP_TIMEOUT" in (_load153(_ss153).get("env") or {}), False)
            _in153.on_network_share = lambda project, *a, **k: False
            _l153 = os.path.join(_d153, "local")
            os.makedirs(_l153)
            _in153.install(_l153, "executor", python=sys.executable)
            check("CONTROL: a local project's env does not get it",
                  _GIT153 in (_load153(os.path.join(
                      _l153, ".claude", "settings.json")).get("env") or {}),
                  False)
            check("CONTROL: nor MCP_TIMEOUT (8.130)",
                  "MCP_TIMEOUT" in (_load153(os.path.join(
                      _l153, ".claude", "settings.json")).get("env") or {}),
                  False)
        finally:
            _in153.on_network_share = _was153
finally:
    _srv153.shutdown()
    _srv153.server_close()

print("\n154. relayout --wait asks /state again when it did not answer in time")
print("    2026-10-13, 21:54 and 22:18: the Mac's restart was refused at once")
print("    under --wait 1800 and --wait 600, 'port 8765 is open but /state did")
print("    not answer: timed out'. /state took 64.5 s and 36.9 s while that")
print("    daemon wrote a project's folder on the share; busy_now waited 8 and")
print("    wait_until_quiet asked again only on 'busy'. Now a late answer is")
print("    asked again, an ask under a wait is patient up to the time left,")
print("    and a daemon that predates the gate is still not asked twice.")
print("    FAILURE WOULD LOOK LIKE: one request and 'unreachable' back at")
print("    once, with the whole --wait unspent. -> 8.123")

from bridgecore import relayout as _rl154                        # noqa: E402

_QUIET154 = {"pairs": {"proj-one": {"name": "One", "busy": {
    "reviewing": False, "verdict_in_flight": False, "inflight": 0,
    "handover": False, "compacting": []}}}}
_ask154 = {"n": 0, "drop": 0, "sleep": 0.0, "state": _QUIET154}


class _Gate154(BaseHTTPRequestHandler):
    """/state that can be late: closed unanswered, or answered slowly."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        _ask154["n"] += 1
        if _ask154["n"] <= _ask154["drop"]:
            self.close_connection = True
            return
        time.sleep(_ask154["sleep"])
        body = json.dumps(_ask154["state"]).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the asker gave up first - that is the slow case itself


def _reset154(drop=0, sleep=0.0, state=None):
    _ask154.update(n=0, drop=drop, sleep=sleep, state=state or _QUIET154)


_srv154 = ThreadingHTTPServer(("127.0.0.1", 0), _Gate154)
_port154 = _srv154.server_address[1]
threading.Thread(target=_srv154.serve_forever, daemon=True).start()
_was154 = (getattr(_rl154, "ASK", None), getattr(_rl154, "ASK_WAITING", None))
try:
    print("   (a) the first /state is closed unanswered, the second is quiet")
    _reset154(drop=1)
    _said154 = []
    _v154, _l154 = _rl154.wait_until_quiet(_port154, 4, out=_said154.append)
    check("a /state that did not answer is asked again, not taken as the "
          "answer", (_v154, _ask154["n"]), ("quiet", 2))
    check("and the waiting says it is waiting on an answer",
          [ln for ln in _said154 if "no answer yet" in ln
           and "did not answer" in ln] != [], True)

    print("   (b) /state answers in 1 s: the bare gate's ASK is shorter, an")
    print("   ask under a wait is not")
    _rl154.ASK, _rl154.ASK_WAITING = 0.3, 5
    _reset154(sleep=1.0)
    check("the bare gate keeps the shorter ASK and calls a 1 s answer "
          "unreachable", _rl154.busy_now(_port154)[0], "unreachable")
    _reset154(sleep=1.0)
    _v154b, _ = _rl154.wait_until_quiet(_port154, 10, out=lambda *_a: None)
    check("under a wait one ask waits for the slow answer",
          (_v154b, _ask154["n"]), ("quiet", 1))
    _rl154.ASK, _rl154.ASK_WAITING = _was154

    print("   (c) a daemon that predates the gate is not asked twice")
    _reset154(state={"pairs": {"proj-one": {"name": "One"}}})
    _t154 = time.time()
    _v154c, _ = _rl154.wait_until_quiet(_port154, 20, out=lambda *_a: None)
    check("CONTROL: 'unreachable' at once, one request, the wait unspent",
          (_v154c, _ask154["n"], time.time() - _t154 < 5),
          ("unreachable", 1, True))

    print("   (d) a /state that never answers: refused when the wait is spent")
    _reset154(drop=99)
    _t154 = time.time()
    _v154d, _l154d = _rl154.wait_until_quiet(_port154, 2,
                                              out=lambda *_a: None)
    check("unreachable, after asking more than once and spending the wait",
          (_v154d, _ask154["n"] >= 2, time.time() - _t154 >= 1.9),
          ("unreachable", True, True))
    check("and the refusal still names the port and what failed",
          _l154d[0].startswith("port %d is open but /state did not answer"
                               % _port154), True)
finally:
    if _was154[0] is not None:
        _rl154.ASK, _rl154.ASK_WAITING = _was154
    _srv154.shutdown()
    _srv154.server_close()


if TMUX_HOME:
    check("no tmux server this run started still listens - their sockets "
          "are in its own folder, which goes with it (8.103)",
          (owntemp.tmux_left(TMUX_HOME, 5), os.environ.get("TMUX_TMPDIR")),
          ([], TMUX_HOME))

print("\n150. a report that NAMES a code file did not CHANGE one (8.118):")
print("    a watched project, 2026-10-13 11:13:32 - the planner's 'done' on")
print("    report 1 was refused for want of a Residence: line, and the turn")
print("    behind that report only READ: a Read, then ls, tail, sed -n, find")
print("    and python3 -c. The gate never saw a command. touched_code() reads")
print("    the REPORT, and it fired on the name of a file the executor had")
print("    read (tools/bridge_self_check.py), on state.json, and on .mcp.json")
print("    in the human's note the bridge appends to every report. What the")
print("    turn DID is in the executor's own transcript, and that is the")
print("    witness now - carried from one accepted piece to the next, so an")
print("    edit answered 'continue' and verified a turn later still counts")
import json as _json150                                    # noqa: E402
import inspect as _insp150                                 # noqa: E402
_cw150 = (getattr(daemon, "call_writes_code", None)
          or (lambda *_a, **_k: None))
_note150 = (getattr(daemon, "note_code_written", None)
            or (lambda *_a, **_k: None))
_clear150 = (getattr(daemon, "clear_code_written", None)
             or (lambda *_a, **_k: None))
check("the bridge has a witness of what the executor's calls wrote",
      [callable(getattr(daemon, _n, None)) for _n in
       ("call_writes_code", "note_code_written", "clear_code_written",
        "report_changed_code")], [True, True, True, True])

_p150 = os.path.join(TMP, "proj150")
os.makedirs(os.path.join(_p150, "tools"), exist_ok=True)
with open(os.path.join(_p150, "tools", "bridge_self_check.py"), "w") as _fh:
    _fh.write("print('self check')\n")
_home150 = os.path.expanduser("~")
_cd150 = 'cd "%s" && ' % _p150
_bl150 = "bridge-logs/"
_d13, _d12 = _bl150 + "2026-10-13/", _bl150 + "2026-10-12/"
# The six calls of that turn, as the client wrote them. Only the project
# folder and the home folder are this run's own - a published suite does
# not carry a machine's layout.
_calls150 = [
    ("Read", {"file_path": os.path.join(
        _home150, ".claude", "projects", "x", "tool-results",
        "hook-1-additionalContext.txt")}),
    ("Bash", {"command": _cd150 + "ls bridge-logs/ | tail -5; ls " + _d13
              + " 2>/dev/null; ls " + _d13 + "dialogue 2>/dev/null; python3 "
              "tools/bridge_self_check.py --role executor 2>&1 | head -40"}),
    ("Bash", {"command": _cd150 + "tail -c 6000 " + _d12
              + "dialogue/305-planner.md; echo ======; tail -c 3000 " + _d12
              + "dialogue/305-executor.md"}),
    ("Bash", {"command": _cd150 + "sed -n 1,83p tools/bridge_self_check.py; "
              "ls ~/Desktop/bridge_copy 2>&1 | head; ls ~/Desktop/bridge_copy/"
              "data 2>&1 | head -30"}),
    ("Bash", {"command": "ls ~/Desktop/bridge_copy/source | head -40; find "
              "~/Desktop/bridge_copy -name state.json -maxdepth 4 "
              "2>/dev/null"}),
    ("Bash", {"command": _cd150 + "python3 -c \"\nimport json;d=json.load("
              "open('%s'));p=d.get('pids') or {}\nfor k,v in p.items():\n"
              "  if 'enemy' in k.lower(): print(k, {x:v.get(x) for x in "
              "('pid','model_req','effort','compact_window','why')})\n\"; "
              "python3 tools/bridge_self_check.py --role executor --state "
              "~/Desktop/bridge_copy/source/data/state.json; echo \"code $?\""
              % os.path.join(_home150, "Desktop", "bridge_copy", "source",
                             "data", "state.json")}),
]
print("   each call of that turn is a READ - sed -n above all")
check("none of the six calls of that turn wrote code",
      [_cw150(_n, _i) for _n, _i in _calls150], [""] * 6)
check("the trap itself: sed -n on a .py prints it, sed -i edits it",
      (_cw150("Bash", {"command": "sed -n 1,83p tools/bridge_self_check.py"}),
       bool(_cw150("Bash", {"command": "sed -i '' 's/a/b/' "
                                       "tools/bridge_self_check.py"}))),
      ("", True))
print("   and every way an edit is really made is still counted")
_writes150 = [
    ("Edit", {"file_path": os.path.join(_p150, "tools",
                                        "bridge_self_check.py"),
              "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": os.path.join(_p150, "scripts", "enemy.gd"),
               "content": "extends Node\n"}),
    ("NotebookEdit", {"notebook_path": os.path.join(_p150, "n.ipynb")}),
    ("Bash", {"command": "sed -i.bak -e 's/a/b/' tools/x.py"}),
    ("Bash", {"command": "cat > tools/probe.py <<'EOF'\nprint(1)\nEOF"}),
    ("Bash", {"command": "echo 'x = 1' >> tools/x.py"}),
    ("Bash", {"command": "perl -pi -e 's/a/b/' scripts/x.gd"}),
    ("Bash", {"command": "git -C \"%s\" commit -am fix" % _p150}),
    ("Bash", {"command": "python3 - <<'EOF'\nfrom pathlib import Path\n"
                         "Path('tools/x.py').write_text('x')\nEOF"}),
    ("Bash", {"command": "printf x | tee tools/x.py"}),
    ("PowerShell", {"command": "Set-Content -Path tools\\x.py -Value 1"}),
]
check("an Edit, a Write, a notebook, sed -i, a heredoc, >>, perl -pi, a "
      "commit, a script's write_text, tee and Set-Content all write code",
      [bool(_cw150(_n, _i)) for _n, _i in _writes150],
      [True] * len(_writes150))
check("but a note, a state file or a redirect into /dev/null is not code",
      [_cw150(_n, _i) for _n, _i in [
          ("Write", {"file_path": os.path.join(_p150, "notes.md")}),
          ("Bash", {"command": "echo done > .tmp/executor_state.md"}),
          ("Bash", {"command": "python3 tools/x.py > /dev/null 2>&1"})]],
      ["", "", ""])

_tp150 = os.path.join(TMP, "exec150.jsonl")
_seq150 = [0]


def _turn150(calls, text):
    """One executor turn, appended the way the client writes it."""
    with open(_tp150, "a", encoding="utf-8") as fh:
        fh.write(_json150.dumps({"type": "user", "message": {
            "role": "user", "content": "the task"}}) + "\n")
        for name, tin in calls:
            _seq150[0] += 1
            tid = "toolu_150_%d" % _seq150[0]
            fh.write(_json150.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{
                    "type": "tool_use", "id": tid, "name": name,
                    "input": tin}]}}) + "\n")
            fh.write(_json150.dumps({"type": "user", "message": {
                "role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": tid,
                    "content": "ok"}]}}) + "\n")
        fh.write(_json150.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": [{
                "type": "text", "text": text}]}}) + "\n")


# The report, in English and without the machine's layout, keeping every
# token that tripped the gate on the day: the executor's own text named a
# .py it had read and a state.json it had looked for, and the human's note
# the bridge appends named .mcp.json.
_rep150 = ("Executor report 1:\n\nTO THE BRIDGE: the executor is free and "
           "waits for a task. The thread is read: the last verdict is stop "
           "in `" + _d12 + "dialogue/305-planner.md`. A trap on this "
           "machine: `tools/bridge_self_check.py` looks for state.json at "
           "the other machine's path and fails with FileNotFoundError; it "
           "works only with `--state ~/Desktop/bridge_copy/source/data/"
           "state.json`.\n\nNote from the human: the bridge rewrote only its "
           "own hooks, the status line and .mcp.json for this machine.")
_fb150 = "Accepted. Checked: %s" % os.path.join(_p150, "tools",
                                                "bridge_self_check.py")
_fbres150 = "Residence: tools/bridge_self_check.py:main\n" + _fb150
_p150n = daemon.norm(_p150)
check("the words still name a code file - the fix is not in the words",
      daemon.touched_code(_rep150), True)

print("   the day itself, through the gate: the turn read, so 'done' is taken")
_turn150(_calls150, "TO THE BRIDGE: the executor is free.")
_w150 = _note150(_p150, {"transcript_path": _tp150}, 1)
check("the first report this bridge carries for the pair, its whole "
      "transcript read: nothing written", _w150, [])
daemon.PENDING[_p150n] = {"content": _rep150, "made": time.time(), "n": 1,
                          "wrote": _w150}
_ok150, _why150, _k150 = daemon.verdict_gate(_p150, "done", _fb150)
check("'done' on that report is TAKEN without a Residence: line",
      (_ok150, _k150), (True, "artifacts"))
check("and it is not the residence refusal that answers",
      "where the fix LIVES" in (_why150 or ""), False)
daemon.PENDING[_p150n] = {"content": _rep150, "made": time.time(), "n": 1}
check("control: with no witness on the waiter the words decide, as before "
      "- refused for the Residence",
      "where the fix LIVES" in (daemon.verdict_gate(
          _p150, "done", _fb150)[1] or ""), True)

print("   an edit answered 'continue', then a turn that only verifies: the")
print("   piece still changed code, and 'done' on it still needs a Residence")
_clear150(_p150)
_turn150([_writes150[0]], "Edited tools/bridge_self_check.py: --state "
         "defaults to this machine's state.json.")
_w150b = _note150(_p150, {"transcript_path": _tp150}, 2)
check("the edit turn is witnessed", bool(_w150b), True)
_turn150([_calls150[3]], "Ran it again: tools/bridge_self_check.py passes.")
_w150c = _note150(_p150, {"transcript_path": _tp150}, 3)
check("and the read-only turn after it carries it - nothing accepted it",
      bool(_w150c), True)
daemon.PENDING[_p150n] = {"content": "Executor report 3:\n\nRan it again: "
                          "tools/bridge_self_check.py passes.",
                          "made": time.time(), "n": 3, "wrote": _w150c}
_ok150, _why150, _k150 = daemon.verdict_gate(_p150, "done", _fb150)
check("so 'done' without a Residence is refused",
      (_ok150, "where the fix LIVES" in (_why150 or "")), (False, True))
check("and with one, on the marker's own line, it is taken",
      daemon.verdict_gate(_p150, "done", _fbres150)[0], True)
check("run_review files the witness on the waiter and an accepted piece "
      "clears it",
      [s in _insp150.getsource(daemon.run_review) for s in
       ('"wrote": note_code_written(path, event, n)',
        'if verdict in ("done", "stop"):\n        clear_code_written(path)')],
      [True, True])
check("and both of the gate's code questions ask the witness, not the "
      "words alone",
      (_insp150.getsource(daemon.verdict_gate).count(
          "report_changed_code(waiter)"),
       "touched_code(report)" in _insp150.getsource(daemon.verdict_gate)),
      (2, False))
_clear150(_p150)
_turn150([_calls150[3]], "Looked at tools/bridge_self_check.py once more.")
_w150d = _note150(_p150, {"transcript_path": _tp150}, 4)
check("once the piece is accepted, a turn that only reads is clean again",
      _w150d, [])

print("   no witness is no evidence: the words decide, as they always did")
check("a transcript that is not there gives no answer",
      _note150(os.path.join(TMP, "proj150b"),
               {"transcript_path": os.path.join(TMP, "nope150.jsonl")}, 1),
      None)
check("nor does a pair first seen mid-run (report 5): the bridge did not "
      "see the piece begin, so it is not a witness of all of it",
      _note150(os.path.join(TMP, "proj150c"), {"transcript_path": _tp150},
               5), None)
check("and the record is in the inventory of path-keyed state, so a moved "
      "or removed project takes it along",
      daemon.STATE_PATHS.get("code_written"), "path")
daemon.PENDING.pop(_p150n, None)

print("\n155. a hung lsof holds neither the port's owner nor the asker of a")
print("    request (8.131): 2026-10-14 on the Mac, one grep stuck in state U")
print("    on the share held every system-wide lsof 17-80 s. pid_on_port")
print("    waited 30 s and said None for the daemon serving the port - ten")
print("    red checks of case 146 - and tcp_owner waited 15 s and said None,")
print("    so /session refused 'who asked could not be established' at")
print("    10:02:36 and 10:10:21, and a suite's own /session timed out. A")
print("    Mac's `netstat -anv` names the process and reads the kernel's")
print("    table alone, in 0.01 s: it is read first, and lsof is the")
print("    fallback, for 5 s")
import shutil as _sh155                                    # noqa: E402
import socket as _so155                                    # noqa: E402
from bridgecore import relayout as _rl155                 # noqa: E402
from bridgecore import sessions as _ss155                 # noqa: E402
_parse155 = (getattr(_ss155, "parse_netstat", None)
             or (lambda _t: "no parse_netstat"))
_head155 = ("Active Internet connections (including servers)\n"
            "Proto Recv-Q Send-Q  Local Address          Foreign Address    "
            "    (state)          rxbytes      txbytes  rhiwat  shiwat      "
            "    process:pid    state  options           gencnt    flags   "
            "flags1 usecnt rtncnt fltrs\n")
_tail155 = "  00000 00000006 0000000000ebbfa7 00000000 00000800      1      0"
# rows in this Mac's own shape, 2026-10-14: the names and pids as it
# printed them, the addresses off the loopback replaced by documentation
# ones. An end nobody holds keeps a pid and loses its name (:50246)
_text155 = _head155 + "\n".join(r + _tail155 for r in (
    "tcp4       0      0  127.0.0.1.8765         *.*                    "
    "LISTEN                 0            0  131072  131072           "
    "Python:87326",
    "tcp4       0      0  127.0.0.1.8765         127.0.0.1.59618        "
    "ESTABLISHED         8388            0  404288  146988           "
    "Python:87326",
    "tcp4       0      0  127.0.0.1.59618        127.0.0.1.8765         "
    "ESTABLISHED            0         4142  408300  146988          "
    "2.1.299:91228",
    "tcp4       0      0  203.0.113.7.58564      198.51.100.9.443       "
    "ESTABLISHED       614284      1490054  175960  131072 Google Chrome "
    "He:67263",
    "tcp6       0      0  2001:db8::1.1025       2001:db8::2.1028       "
    "ESTABLISHED         8258         5639  131072  132108 "
    "identityservices:611",
    "tcp4       0      0  127.0.0.1.61025        127.0.0.1.8765         "
    "TIME_WAIT            508         1234  408192  146988                "
    " :50246"))
check("parse_netstat reads the daemon's listener, both ends of one "
      "connection, a name with a space, an address with colons and an end "
      "nobody holds",
      _parse155(_text155),
      [("127.0.0.1.8765", "*.*", "LISTEN", 87326),
       ("127.0.0.1.8765", "127.0.0.1.59618", "ESTABLISHED", 87326),
       ("127.0.0.1.59618", "127.0.0.1.8765", "ESTABLISHED", 91228),
       ("203.0.113.7.58564", "198.51.100.9.443", "ESTABLISHED", 67263),
       ("2001:db8::1.1025", "2001:db8::2.1028", "ESTABLISHED", 611),
       ("127.0.0.1.61025", "127.0.0.1.8765", "TIME_WAIT", 50246)])
check("a header without a process:pid column is None - lsof is asked "
      "then - and so is no answer at all",
      (_parse155(_text155.replace("process:pid", "pid")), _parse155(""),
       _parse155(None)), (None, None, None))

_ns155 = ""
if sys.platform == "darwin":
    try:
        _ns155 = subprocess.run(["/usr/sbin/netstat", "-anv", "-p", "tcp"],
                                capture_output=True, text=True,
                                timeout=30).stdout or ""
    except Exception as _e155:
        _ns155 = ""
_named155 = any("process:pid" in _l.split()
                for _l in _ns155.splitlines()[:3])
_live155 = (
    "a hung lsof first on PATH: pid_on_port names this process on its own "
    "port, in under 3 s",
    "and tcp_owner names the client's process for the client's end, in "
    "under 3 s",
    "the other way round it is the server's end, this process: the "
    "direction is read, not the port alone",
    "the client's own port has no LISTEN row, so no listener: the state "
    "is read too",
    "and the hung lsof was not asked at all",
    "netstat without a process:pid column: pid_on_port asks lsof, and "
    "takes its answer",
    "and so does tcp_owner",
    "and a hung lsof in that fallback is given 5 s, not 30 - pid_on_port",
    "and not 15 - tcp_owner",
    "the client killed: its end is still in the table, with its dead pid",
    "and tcp_owner names nobody for it - the client let go")
if not _named155:
    for _n155 in _live155:
        skip(_n155, "a Mac's `netstat -anv -p tcp` with a process:pid "
             "column; this is %s" % sys.platform)
else:
    _d155 = os.path.join(TMP, "stub155")
    _mark155 = os.path.join(_d155, "lsof-was-asked")

    def _stub155(folder, name, body):
        os.makedirs(folder, exist_ok=True)
        _p = os.path.join(folder, name)
        with open(_p, "w") as _fh:
            _fh.write("#!/bin/sh\n%s\n" % body)
        os.chmod(_p, 0o755)
        return _p

    # exec: the timeout's kill lands on the sleep itself, nothing outlives it
    _hung155 = os.path.join(_d155, "hung")
    _stub155(_hung155, "lsof", "touch '%s'\nexec /bin/sleep 40" % _mark155)
    _srv155 = _so155.socket()
    _srv155.bind(("127.0.0.1", 0))
    _srv155.listen(4)
    _port155 = _srv155.getsockname()[1]
    # the client is ANOTHER process, so the two ends have two owners
    _cli155 = subprocess.Popen(
        [sys.executable, "-I", "-c",
         "import socket, time\n"
         "c = socket.create_connection(('127.0.0.1', %d))\n"
         "print(c.getsockname()[1], flush=True)\n"
         "time.sleep(120)\n" % _port155],
        stdout=subprocess.PIPE, text=True)
    _path155 = os.environ.get("PATH", "")
    try:
        _cport155 = int(_cli155.stdout.readline())
        _acc155 = _srv155.accept()[0]
        os.environ["PATH"] = _hung155 + os.pathsep + _path155
        check("the stub is the lsof this process finds",
              _sh155.which("lsof"), os.path.join(_hung155, "lsof"))
        _t155 = time.time()
        _pp155 = _rl155.pid_on_port(_port155)
        check(_live155[0], (_pp155, time.time() - _t155 < 3),
              (os.getpid(), True))
        _t155 = time.time()
        _o155 = _ss155.tcp_owner(_cport155, _port155)
        check(_live155[1], (_o155, time.time() - _t155 < 3),
              (_cli155.pid, True))
        check(_live155[2], _ss155.tcp_owner(_port155, _cport155),
              os.getpid())
        check(_live155[3], _rl155.pid_on_port(_cport155), None)
        check(_live155[4], os.path.exists(_mark155), False)

        _nopid155 = os.path.join(_d155, "nopid")
        _bare155 = ("echo 'Active Internet connections'\n"
                    "echo 'Proto Recv-Q Send-Q  Local Address  "
                    "Foreign Address  (state)'")
        _stub155(_nopid155, "netstat", _bare155)
        _stub155(_nopid155, "lsof",
                 'case "$*" in\n*LISTEN*) echo 4242 ;;\n'
                 '*) printf "p4243\\nn127.0.0.1:%d->127.0.0.1:%d\\n" ;;\n'
                 'esac' % (_cport155, _port155))
        os.environ["PATH"] = _nopid155 + os.pathsep + _path155
        check(_live155[5], _rl155.pid_on_port(_port155), 4242)
        check(_live155[6], _ss155.tcp_owner(_cport155, _port155), 4243)

        _nopidhung155 = os.path.join(_d155, "nopidhung")
        _stub155(_nopidhung155, "netstat", _bare155)
        _stub155(_nopidhung155, "lsof", "exec /bin/sleep 40")
        os.environ["PATH"] = _nopidhung155 + os.pathsep + _path155
        _t155 = time.time()
        _pp155 = _rl155.pid_on_port(_port155)
        check(_live155[7], (_pp155, time.time() - _t155 < 10), (None, True))
        _t155 = time.time()
        _o155 = _ss155.tcp_owner(_cport155, _port155)
        check(_live155[8], (_o155, time.time() - _t155 < 10), (None, True))

        os.environ["PATH"] = _hung155 + os.pathsep + _path155
        _cli155.kill()
        _cli155.wait()
        _left155 = ""
        for _i155 in range(20):
            _left155 = "\n".join(
                _l for _l in subprocess.run(
                    ["/usr/sbin/netstat", "-anv", "-p", "tcp"],
                    capture_output=True, text=True, timeout=30).stdout
                .splitlines()
                if "127.0.0.1.%d " % _cport155 in _l
                and "127.0.0.1.%d " % _port155 in _l
                and _l.split()[3] == "127.0.0.1.%d" % _cport155)
            if _left155 and "ESTABLISHED" not in _left155:
                break
            time.sleep(0.1)
        check(_live155[9], (bool(_left155), "ESTABLISHED" in _left155,
                            ":%d " % _cli155.pid in _left155 + " "),
              (True, False, True))
        check(_live155[10], _ss155.tcp_owner(_cport155, _port155), None)
    finally:
        os.environ["PATH"] = _path155
        _cli155.kill()
        _cli155.wait()
        _cli155.stdout.close()
        for _s155 in (locals().get("_acc155"), _srv155):
            if _s155 is not None:
                _s155.close()

print("\n156. the private words of the public scan come from a list outside")
print("     the repository (8.141). The closed projects' names and an account")
print("     were written in check_public.py itself, and so published by it.")
print("     The list now lies beside the source folder; a word in it is a hit")
print("     where it stands - in a copy of the scanner too, whose own source")
print("     covers its patterns and no value. The private tree refuses to scan")
print("     without the list; a public clone says it has none. Every word here")
print("     is made at the run and is no one's, and a copy whose loader hands")
print("     back nothing has to lose the hit - or the first check could not")
print("     fail")
_here156 = os.path.dirname(os.path.abspath(__file__))
_cp156 = os.path.join(_here156, "check_public.py")
if not os.path.isfile(_cp156):
    print("   not counted: no check_public.py beside this suite - an unpacked")
    print("   package, which ships no scanner")
else:
    _d156 = os.path.join(TMP, "case156")
    os.makedirs(_d156, exist_ok=True)
    # each ends in a letter, so a digit after it is still the word's (a
    # numbered copy) and a letter after it is another word
    _word156 = "zq" + os.urandom(6).hex() + "k"
    _host156 = "zh" + os.urandom(4).hex() + "k"
    _name156 = "zn" + os.urandom(4).hex() + "k"
    _cs156 = "ZQ" + os.urandom(4).hex().upper() + "K"
    with open(_cp156, encoding="utf-8") as _fh:
        _src156 = _fh.read()

    def _list156(path):
        with open(path, "w", encoding="utf-8") as _fh:
            _fh.write("# made by test_handover case 156 - no one's\n"
                      "closed project name\t%s\n"
                      "machine name\t%s\n"
                      "probe word\t%s\tcs\n" % (_word156, _host156, _cs156))

    def _tree156(name, files):
        _t = os.path.join(_d156, name)
        os.makedirs(_t, exist_ok=True)
        for _n, _text in files.items():
            with open(os.path.join(_t, _n), "w", encoding="utf-8") as _fh:
                _fh.write(_text)
        return _t

    def _env156(deny):
        # the case owns the list it reads: whatever list this machine has,
        # or the run was handed, is not the one asked about here
        _e = {k: v for k, v in os.environ.items() if k != "BRIDGE_PUBLIC_DENY"}
        _e["CLAUDE_CONFIG_DIR"] = os.path.join(_d156, "no-client")
        if deny:
            _e["BRIDGE_PUBLIC_DENY"] = deny
        return _e

    def _scan156(scanner, folder, deny):
        _r = subprocess.run([sys.executable, scanner, folder],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=_env156(deny))
        return _r.returncode, (_r.stdout or "") + (_r.stderr or "")

    def _hits156(out):
        # the report a person reads: "<kind> - <count>", then one
        # "    <file>:<line>  <hit>" under it for each hit
        _got, _kind = [], None
        for _l in out.splitlines():
            _m = re.match(r"^(\S.*) - \d+$", _l)
            if _m:
                _kind = _m.group(1)
                continue
            _m = re.match(r"^    (\S.*?):(\d+)  (.*)$", _l)
            if _m and _kind:
                _got.append((_kind, _m.group(1), int(_m.group(2)),
                             _m.group(3)))
        return _got

    def _lines156(out, kind, rel="notes.md"):
        return sorted(n for k, r, n, _h in _hits156(out)
                      if k == kind and r == rel)

    _deny156 = os.path.join(_d156, "deny.txt")
    _list156(_deny156)
    _P156 = "private list's closed project name"

    # 1. a word of the list is a hit where it stands - alone, before an
    #    underscore, before a digit - and not inside another word
    _leak156 = _tree156("leak", {"notes.md": (
        "see %s for this\n"
        "the folder %s_tools\n"
        "%s2 is the second copy\n"
        "%ss are something else\n"
        "and a%s is glued to a letter\n"
        % ((_word156,) * 5))})
    _rc156a, _o156a = _scan156(_cp156, _leak156, _deny156)
    check("(1) a word of the private list: refused, and each place it stands "
          "is a hit of the kind the list gave it - none where a letter is "
          "glued to it",
          (_rc156a != 0, _lines156(_o156a, _P156),
           "private list: 3 entries from" in _o156a), (True, [1, 2, 3], True))

    # 2. its own source covers its patterns, never a value
    _copy156 = _tree156("copy", {"check_public.py": _src156 + "\n# %s\n"
                                 % _word156})
    _rc156b, _o156b = _scan156(_cp156, _copy156, _deny156)
    _last156 = _src156.count("\n") + 2
    check("(2) the same word in a COPY of the scanner is a hit too - its own "
          "source covers the patterns it holds, not a value",
          (_rc156b != 0,
           _last156 in _lines156(_o156b, _P156, "check_public.py")),
          (True, True))

    # 3. a cs entry is a hit in its own case only
    _cs156t = _tree156("cs", {"notes.md": "small %s\nBIG %s\n"
                              % (_cs156.lower(), _cs156)})
    _rc156c, _o156c = _scan156(_cp156, _cs156t, _deny156)
    check("(3) an entry marked cs is a hit in that case only",
          _lines156(_o156c, "private list's probe word"), [2])

    # 4. the forms no list has to hold; built in halves, since this suite
    #    is published and the scan would be right to refuse them whole
    _forms156 = _tree156("forms", {"notes.md": "\n".join((
        "mounted at /Vol" + "umes/" + _name156 + "/work",
        "the client's folder -" + "Users-" + _name156 + "-work",
        "a scratchpad under /private/tmp/claude-" + "501" + "/x",
        "the task ran as " + _host156 + "\\" + "worker",
        # a share's login at the machine's mDNS name, and its UNC path:
        # the share is an ordinary word and stays out of the list, so
        # both are caught by the machine's name (verdict on report 49).
        # The login is a stand-in: a real one is this machine's value
        "mount it as someone@" + _host156 + ".local",
        "or open \\\\" + _host156 + "\\" + "Share",
    )) + "\n"})
    _rc156d, _o156d = _scan156(_cp156, _forms156, _deny156)
    check("(4) a share's /Volumes path, a home folder's slug, the client's "
          "temp folder, a login and a share behind a machine's name of the "
          "list are each a hit on their line - and the machine's name is "
          "one wherever it stands, before .local too",
          [_lines156(_o156d, k) for k in
           ("mounted share path", "home or share slug",
            "client temp folder", "login or share behind a machine's name",
            "private list's machine name", "e-mail address",
            "mDNS host name")],
          [[1], [2], [3], [4, 6], [4, 5, 6], [5], [5]])
    print("   CONTROL (rule 19): the documented placeholders pass, with the")
    print("   same list - so the hits above are the forms, not a scanner that")
    print("   refuses everything")
    _place156 = _tree156("placeholders", {"notes.md": "\n".join((
        "C:\\path\\to\\project",
        "mount it as <account>@<PC>.local",
        "/Volumes/<share>/work and /Users/<account>/work",
        "the client's folder -Users-someone-work",
        "a scratchpad under /tmp/claude/x",
    )) + "\n"})
    _rc156e, _o156e = _scan156(_cp156, _place156, _deny156)
    check("the placeholders: passed, one file read",
          (_rc156e, "files read: 1" in _o156e), (0, True))

    # 5. where the list is looked for when nobody names it, and what its
    #    absence means: the mark of the private tree is test_cases.py
    #    beside the scanner
    _clean156 = {"notes.md": "# notes\n\nnothing personal in here\n"}
    _marked156 = os.path.join(_d156, "marked", "source")
    _open156 = os.path.join(_d156, "open", "source")
    for _s in (_marked156, _open156):
        os.makedirs(_s, exist_ok=True)
        with open(os.path.join(_s, "check_public.py"), "w",
                  encoding="utf-8") as _fh:
            _fh.write(_src156)
    with open(os.path.join(_marked156, "test_cases.py"), "w") as _fh:
        _fh.write("# the mark of the private tree, for case 156\n")
    _clean156t = _tree156("clean", _clean156)
    _rc156f, _o156f = _scan156(os.path.join(_marked156, "check_public.py"),
                               _clean156t, None)
    check("(5a) the private tree with no list: refused over a clean file, and "
          "it says the list was not found",
          (_rc156f != 0, "private list: NOT FOUND" in _o156f,
           [k for k, _r, _n, _h in _hits156(_o156f)]),
          (True, True, ["private list"]))
    _rc156g, _o156g = _scan156(os.path.join(_open156, "check_public.py"),
                               _clean156t, None)
    check("(5b) a public clone with no list: passed, and it says it has none",
          (_rc156g, "private list: none" in _o156g), (0, True))
    _rc156h, _o156h = _scan156(os.path.join(_open156, "check_public.py"),
                               _clean156t,
                               os.path.join(_d156, "there-is-no-list.txt"))
    check("(5c) a list named by BRIDGE_PUBLIC_DENY and not there: refused in "
          "a public clone too - a misspelt name must not open the gate",
          (_rc156h != 0, "named by BRIDGE_PUBLIC_DENY" in _o156h),
          (True, True))
    os.makedirs(os.path.join(_d156, "marked", "private"), exist_ok=True)
    _list156(os.path.join(_d156, "marked", "private", "public-deny.txt"))
    _rc156i, _o156i = _scan156(os.path.join(_marked156, "check_public.py"),
                               _leak156, None)
    check("(5d) the list beside the source folder is the one read when no "
          "one names another",
          (_rc156i != 0, _lines156(_o156i, _P156)), (True, [1, 2, 3]))

    # 7. the planner's check runs the suites in a COPY of the tree, which
    #    holds the mark and no private/ beside it: the daemon names the
    #    tree's own list to it. Asked of the copy's scanner, not of the
    #    environment - a _check_env that ignores the tree leaves the run's
    #    own list, or none, and the word's hits go
    _copied156 = os.path.join(_d156, "copied", "source")
    os.makedirs(_copied156, exist_ok=True)
    for _n156 in ("check_public.py", "test_cases.py"):
        with open(os.path.join(_marked156, _n156), encoding="utf-8") as _fh:
            _t156 = _fh.read()
        with open(os.path.join(_copied156, _n156), "w",
                  encoding="utf-8") as _fh:
            _fh.write(_t156)
    _ce156 = daemon._check_env(os.path.join(_d156, "copied"), _marked156)
    _r156k = subprocess.run(
        [sys.executable, os.path.join(_copied156, "check_public.py"),
         _leak156], capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=_ce156)
    _o156k = (_r156k.stdout or "") + (_r156k.stderr or "")
    check("(7) the planner's check names the tree's own list to its copy, "
          "and the copy's scanner reads it: the word's hits, from a list "
          "the copy was never handed",
          (_r156k.returncode != 0, _lines156(_o156k, _P156)),
          (True, [1, 2, 3]))

    # 6. SABOTAGE (rule 19): a loader that hands back nothing. The word's
    #    hit has to go - so check (1) can fail - and the gate still refuses,
    #    saying the list keeps nothing out
    _hook156 = "def load_deny(path):\n"
    check("the sabotage found the loader it breaks", _src156.count(_hook156),
          1)
    _sab156 = os.path.join(_d156, "sabotage")
    os.makedirs(_sab156, exist_ok=True)
    with open(os.path.join(_sab156, "check_public.py"), "w",
              encoding="utf-8") as _fh:
        _fh.write(_src156.replace(_hook156, _hook156 + "    return []\n", 1))
    _rc156j, _o156j = _scan156(os.path.join(_sab156, "check_public.py"),
                               _leak156, _deny156)
    check("SABOTAGE: a loader that hands back nothing loses every hit of the "
          "word - so (1) would be red - and the scan still refuses, saying "
          "the list holds no entry",
          (_lines156(_o156j, _P156), _rc156j != 0,
           "private list: HOLDS NO ENTRY" in _o156j), ([], True, True))

print("\n157. the handoff a demand names: the file the last accepted handoff")
print("     was written in, then the names the project's CLAUDE.md gives -")
print("     never a file at its root for its clock alone (8.140). 2026-10-14")
print("     20:54:04 on the Mac: the rules named a Part26 file, an older Part25")
print("     one whose links had just been edited was newer, and the demand")
print("     named it. The receivers are project_handoff_file's own answer and")
print("     handoff_written's, on a folder this case makes under its temp.")
print("     FAILURE WOULD LOOK LIKE: the Part25 file named, the accepted file")
print("     forgotten, or a name the rules gained since overruled by it.")
print("     -> DECISIONS.md 8.140")
_d157 = daemon.norm(os.path.join(TMP, "p157"))
os.makedirs(os.path.join(_d157, "bridge-logs", "2026-10-14", "handoff"))
_k157 = "%s|executor" % _d157


def _put157(rel, body, age):
    _f = os.path.join(_d157, rel)
    with open(_f, "w", encoding="utf-8") as _fh:
        _fh.write(body)
    _t = time.time() - age
    os.utime(_f, (_t, _t))
    return _f


def _ans157():
    _f, _how = daemon.project_handoff_file(_d157, "executor")
    return os.path.basename(_f), _how


def _accept157(f, named=None):
    """A demand, the file written after it, and a first line naming it -
    what handoff_written takes. (ok, the file it checked)"""
    daemon.clear_handover_pending(_d157, "executor")
    daemon.note_handover_pending(_d157, "executor", "case 157")
    with open(f, "a", encoding="utf-8") as _fh:
        _fh.write("written for case 157\n")
    # past the demanded file's whole second, whatever its clock says
    _t = max(time.time(), daemon.handover_pending_for(
        _d157, "executor").get("mtime") or 0) + 2
    os.utime(f, (_t, _t))
    _ok, _why, _file = daemon.handoff_written(
        _d157, "HANDOFF WRITTEN: %s\nthe rest of the report" % (named or f),
        "executor")
    daemon.clear_handover_pending(_d157, "executor")
    return _ok, _file


_put157("X_HANDOFF.md", "# the first one\n", 9000)
_put157("X_HANDOFF_Part26_executor.md", "# the running one\n", 3000)
_put157("X_HANDOFF_Part25_executor_s13.md", "# an old one\n", 60)
_put157("CLAUDE.md", "The running handoff is `X_HANDOFF_Part26_executor.md`;"
        " the first was `X_HANDOFF.md`.\n", 9000)
check("(a) the incident: an old file at the root, touched after the named "
      "one, is not the answer - the rules' name is",
      _ans157()[0], "X_HANDOFF_Part26_executor.md")
_put157("X_HANDOFF_Part26_planner.md", "# the partner's\n", 30)
_put157("CLAUDE.md", "Handoffs: `X_HANDOFF_Part26_executor.md` and "
        "`X_HANDOFF_Part26_planner.md`; the first was `X_HANDOFF.md`.\n",
        9000)
check("(b) among the names, the one that says the role beats a newer "
      "partner's", _ans157()[0], "X_HANDOFF_Part26_executor.md")
_put157("X_HANDOFF_FROZEN.md", "# frozen\n", 8000)
_put157("X_HANDOFF_LIVE.md", "# live\n", 2000)
_put157("CLAUDE.md", "Frozen: `X_HANDOFF_FROZEN.md`. Running: "
        "`X_HANDOFF_LIVE.md`.\n", 9000)
check("CONTROL: among names that agree on the role, the freshest - the "
      "frozen ones are named too", _ans157()[0], "X_HANDOFF_LIVE.md")
_put157("CLAUDE.md", "The first handoff was `X_HANDOFF.md`.\n", 9000)
_p157 = _put157("X_HANDOFF_Part19_executor.md", "# the one it keeps\n", 4000)
check("(c) a file the rules do not name is not taken for its clock",
      _ans157()[0], "X_HANDOFF.md")
_ok157, _f157 = _accept157(_p157)
check("PRECONDITION: handoff_written takes a handoff written into a file "
      "the rules do not name", (_ok157, os.path.basename(_f157)),
      (True, "X_HANDOFF_Part19_executor.md"))
_a157, _h157 = _ans157()
check("(d) the next demand names the file the accepted handoff was written "
      "in, and says why",
      (_a157, "last executor handoff was written in" in _h157),
      ("X_HANDOFF_Part19_executor.md", True))
_put157("X_HANDOFF_Part25_executor_s13.md", "# its links edited\n", -60)
check("    and a file at the root newer than it does not move it",
      _ans157()[0], "X_HANDOFF_Part19_executor.md")
_r157 = (daemon.STATE.get("handoff_accepted") or {}).get(_k157) or {}
check("(e) the record is per project and role, RELATIVE to the project - a "
      "moved project's key moves and no value is rewritten - and the "
      "inventory names it",
      (_r157.get("rel"), daemon.STATE_PATHS.get("handoff_accepted")),
      ("X_HANDOFF_Part19_executor.md", "pair"))
_put157("X_HANDOFF_Part27_executor.md", "# the new part\n", 600)
_put157("CLAUDE.md", "The running handoff is `X_HANDOFF_Part27_executor.md`;"
        " the first was `X_HANDOFF.md`.\n", 9000)
check("(f) a handoff the rules have named SINCE is their newer answer, "
      "though the accepted file is fresher on the clock",
      _ans157()[0], "X_HANDOFF_Part27_executor.md")
_put157("CLAUDE.md", "The first handoff was `X_HANDOFF.md`.\n", 9000)
check("CONTROL: rules that name nothing new leave the accepted file the "
      "answer", _ans157()[0], "X_HANDOFF_Part19_executor.md")
_n157 = _put157(os.path.join("bridge-logs", "2026-10-14", "handoff",
                             "542-shift.md"), "# one iteration's\n", 0)
_ok157, _f157 = _accept157(_n157)
check("(g) a numbered handoff under bridge-logs/ is taken for the handover "
      "and not remembered: rule 3 names the next one",
      (_ok157, _ans157()[0]), (True, "X_HANDOFF_Part19_executor.md"))
_o157 = os.path.join(TMP, "outside_handoff.md")
with open(_o157, "w", encoding="utf-8") as _fh:
    _fh.write("# not this project's\n")
_ok157, _f157 = _accept157(_o157)
check("(h) a file outside the project is taken for the handover and not "
      "remembered either", (_ok157, _ans157()[0]),
      (True, "X_HANDOFF_Part19_executor.md"))
os.remove(_p157)
check("(i) an accepted file gone from disk is no answer: the rules' name is",
      _ans157()[0], "X_HANDOFF.md")
_pl157 = daemon.project_handoff_file(_d157, "planner")[0]
check("CONTROL: the planner's answer is still where the numbered handoffs go",
      (os.path.join("bridge-logs", "") in _pl157,
       _pl157.endswith("-planner.md")), (True, True))
_p157 = _put157("X_HANDOFF_Part19_executor.md", "# back\n", 100)
_up157 = os.path.join(_d157.upper(), "X_HANDOFF_Part19_executor.md")
with daemon._lock:
    (daemon.STATE.get("handoff_accepted") or {}).pop(_k157, None)
if _up157 != _p157 and os.path.isfile(_up157):
    _ok157, _f157 = _accept157(_p157, named=_up157)
    check("(j) on a disk that folds case, a first line spelling the folder "
          "in another case names the same file, and it is remembered",
          (_ok157, _ans157()[0]), (True, "X_HANDOFF_Part19_executor.md"))
else:
    skip("(j) a folder spelled in another case on a disk that folds it",
         "this disk does not fold case, or the folder has none to fold")

print("\n158. a window stopped ALONE is killed alone off Windows, and its")
print("     jobs are said to keep running only when its shells answer (8.138).")
print("     2026-10-14, client 2.1.299 on a Mac: a SIGTERM to the client alone")
print("     made it send TERM to every background shell it had, and")
print("     stop_window had already written that they keep running. The")
print("     receivers are a stand-in window's own files and shell, asked by")
print("     pid, and the journal lines stop_window and its witness write.")
print("     FAILURE WOULD LOOK LIKE: the stand-in's TERM file written by a")
print("     stop alone, its shell dead after it, or \"keep running\" written")
print("     before or without the shells' answer.")
print("     -> DECISIONS.md 8.138")
check("the shells are asked 10 s after the stop",
      getattr(daemon, "OUTLIVE_WITNESS_SEC", None), 10)
if os.name == "nt":
    skip("(a)-(f) a stand-in window and its shell, stopped by signal",
         "POSIX signals; Windows stops with taskkill /F, which 8.31 measured")
else:
    import signal as _sig158
    _d158 = daemon.norm(os.path.join(TMP, "p158"))
    os.makedirs(_d158)
    _st158 = os.path.join(_d158, "standin.py")
    with open(_st158, "w", encoding="utf-8") as _fh:
        _fh.write(
            "import os, signal, subprocess, sys, time\n"
            "mark = sys.argv[1]\n"
            "job = subprocess.Popen(['/bin/sh', '-c', 'sleep 120; :'],\n"
            "                       start_new_session=True)\n"
            "open(mark + '.job', 'w').write(str(job.pid))\n"
            "def term(*_):\n"
            "    # what the client does on SIGTERM: TERM to its shell's group\n"
            "    open(mark + '.term', 'w').write('TERM')\n"
            "    try:\n"
            "        os.killpg(job.pid, signal.SIGTERM)\n"
            "    except Exception:\n"
            "        pass\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, term)\n"
            "open(mark + '.up', 'w').write('up')\n"
            "while True:\n"
            "    time.sleep(1)\n")
    _jobs158 = []
    _wins158 = []

    def _win158(name):
        """A stand-in window born a group leader, as a window is; its
        shell in a session of its own, as the client's are. (pid, mark,
        shell pid) once it says it is up."""
        _m = os.path.join(_d158, name)
        _p = subprocess.Popen([sys.executable, "-I", _st158, _m],
                              start_new_session=True)
        _wins158.append(_p)
        for _ in range(200):
            if os.path.exists(_m + ".up"):
                break
            time.sleep(0.05)
        try:
            _j = int(open(_m + ".job").read())
        except Exception:
            _j = 0
        if _j:
            _jobs158.append(_j)
        return _p.pid, _m, _j

    def _lines158(since, word):
        return [a for a in _rec158[since:] if word in a[1]]

    _rec158 = []
    _jsaved158 = store.journal
    _wsaved158 = getattr(daemon, "OUTLIVE_WITNESS_SEC", None)
    try:
        store.journal = lambda *a, **k: _rec158.append(a)
        daemon.OUTLIVE_WITNESS_SEC = 0.5
        _pa, _ma, _ja = _win158("a")
        _ra = daemon.sessions.stop(_d158, "executor", pid=_pa, wait=5,
                                   tree=False)
        time.sleep(1.0)
        check("(a) a stop ALONE: the window gone, no TERM reached it, and "
              "the shell it left is alive after it",
              (_ra, daemon.sessions.pid_alive(_pa),
               os.path.exists(_ma + ".term"),
               bool(_ja) and daemon.sessions.pid_alive(_ja)),
              (True, False, False, True))
        _pb, _mb, _jb = _win158("b")
        _rb = daemon.sessions.stop(_d158, "executor", pid=_pb, wait=5)
        time.sleep(0.5)
        check("(b) control: a whole stop still asks first - TERM reached "
              "the window, and its own handler ended its shell",
              (_rb, daemon.sessions.pid_alive(_pb),
               os.path.exists(_mb + ".term"),
               bool(_jb) and daemon.sessions.pid_alive(_jb)),
              (True, False, True, False))

        def _swap158(name, kill_shell):
            """stop_window over a window with one background record; the
            shell killed by its pid right after, when asked. (window pid,
            shell pid, journal index before)"""
            _p, _m, _j = _win158(name)
            daemon.PROCTRACK.setdefault(_d158, {})["job-" + name] = {
                "bg": True, "from_window": _p, "cmd": "night_job_" + name}
            _at = len(_rec158)
            daemon.stop_window(_d158, "executor", _p)
            if kill_shell and _j:
                try:
                    os.killpg(_j, _sig158.SIGKILL)
                except Exception:
                    pass
            return _p, _j, _at

        _pc, _jc, _ic = _swap158("c", False)
        _now_c = [a[1] for a in _rec158[_ic:]]
        check("(c) at the stop: the line names the shell and says it is "
              "asked; nothing says keep running yet",
              ([("pids %s" % _jc) in l and "is asked in" in l
                for l in _now_c if "WITHOUT its tree" in l],
               [l for l in _now_c if "keep running" in l]),
              ([True], []))
        _end = time.time() + 6
        while time.time() < _end and not (
                _lines158(_ic, "keep running:")
                or _lines158(_ic, "DID NOT OUTLIVE")):
            time.sleep(0.1)
        check("(d) after the witness: its own line, keep running, naming the "
              "live shell",
              [(a[4], ("pids %s" % _jc) in a[1], "night_job_c" in a[1])
               for a in _lines158(_ic, "keep running:")],
              [("log", True, True)])
        _pe, _je, _ie = _swap158("e", True)
        _end = time.time() + 6
        while time.time() < _end and not (
                _lines158(_ie, "keep running:")
                or _lines158(_ie, "DID NOT OUTLIVE")):
            time.sleep(0.1)
        check("(e) a shell gone before the witness: a warning that the "
              "jobs did not outlive the window, and no keep running",
              ([(a[4], ("pids %s" % _je) in a[1])
                for a in _lines158(_ie, "DID NOT OUTLIVE")],
               _lines158(_ie, "keep running")),
              ([("warn", True)], []))
        check("(f) the records were handed the shells as their witness",
              [((daemon.PROCTRACK.get(_d158) or {}).get("job-" + n) or {})
               .get("orphans") for n in ("c", "e")], [[_jc], [_je]])
    finally:
        store.journal = _jsaved158
        if _wsaved158 is None:
            daemon.__dict__.pop("OUTLIVE_WITNESS_SEC", None)
        else:
            daemon.OUTLIVE_WITNESS_SEC = _wsaved158
        daemon.PROCTRACK.pop(_d158, None)
        (daemon.STATE.get("inflight") or {}).pop(_d158, None)
        # only what this case started, by the pids it was given
        for _j in _jobs158:
            try:
                os.killpg(_j, _sig158.SIGKILL)
            except Exception:
                pass
        for _p in _wins158:
            if _p.poll() is None:
                _p.kill()
            try:
                _p.wait(5)
            except Exception:
                pass

print("\n" + ("-" * 60))
owntemp.finish(TMP, bool(FAILED))
if TMUX_HOME:
    owntemp.finish(TMUX_HOME, bool(FAILED))
if SKIPPED:
    print("SKIPPED on this machine: %d" % len(SKIPPED))
    for f in SKIPPED:
        print("  - %s" % f)
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
