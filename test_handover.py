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
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="bridge-test-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
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
    daemon.CFG.setdefault("projects", {})[PATH] = {}
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
import inspect                                          # noqa: E402
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

print("\n13. every launch path records the compaction threshold it passed")
lsrc = inspect.getsource(daemon.ensure_session)
check("ensure_session records it", "autocompact=compact_pct(path)" in lsrc,
      True)

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
check("plan is the routine one", daemon.plan_for(pl, PATH)["do"], "compacting")

print("\n23. the channel answers the daemon before it writes to stdout")
csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bridgecore", "channel.py"), encoding="utf-8").read()
check("inbound events are queued, not written inline",
      "_outbox.put_nowait" in csrc, True)
check("a writer thread drains them", "_drain_outbox" in csrc, True)
check("and the HTTP reply does not wait for the pipe",
      "answered before the write is attempted" in csrc, True)

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
print("    it still gets a bar - Max watches the bar - but a segmented one:")
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
print("   a window dying holds its own pair and leaves the others working")
daemon.STATE["down"] = {}
real_notify, daemon.notify = daemon.notify, lambda *a, **k: "log"
try:
    daemon.handle_session_death(A, "executor", None)
finally:
    daemon.notify = real_notify
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
check("the limit sets the bridge-wide one, not a project's",
      'STATE["paused_by_limit"] = True' in lsrc
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
check("a real path is untouched by that",
      daemon.norm(PATH) == os.path.normcase(os.path.normpath(PATH)), True)
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
check("and the planner is left in plan",
      store.DEFAULT_CONFIG["role_modes"]["planner"], "plan")
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
daemon.migrate_executor_mode()
check("after it, the default applies",
      daemon.mode_for(PATH, "executor"), "bypassPermissions")
check("a mode somebody chose is left alone",
      daemon.mode_for(PATH_B, "executor"), "acceptEdits")
check("and the planner's is never touched",
      daemon.mode_for(PATH, "planner"), "plan")
check("running it again changes nothing", daemon.migrate_executor_mode(), [])
print("   every launch path asks the same question, so a handover moves a")
print("   live pair onto the new mode without restarting the daemon")
dsrc46 = inspect.getsource(daemon)
check("the handover launches with it",
      "permission_mode=mode_for(path, role)" in
      inspect.getsource(daemon.handover), True)
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
print("    Max was explaining the same rules to every new pair by hand. They")
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
check("with rules in it, not just prose",
      len(re.findall(r"^\d+\. \*\*", canon, re.M)), 28)
check("and each of them carries its check where it is read",
      len(re.findall(r"^\s+\*[^*]+:\*", canon, re.M)), 28)
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
      any(h.get("args") == ["-m", "bridgecore.hook"] for h in _pre), True)
check("and the project's own hook was kept, not overwritten",
      any(h.get("command") == "their-own-thing" for h in _pre), True)
print("   and the current directory is kept off sys.path, so a second copy")
print("   of this package sitting in whatever folder the session happens to")
print("   be in cannot shadow the installed one. Measured: with -m, Python")
print("   puts cwd FIRST, ahead of PYTHONPATH - a public copy assembled in a")
print("   subfolder of a watched project was the hook that actually ran")
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
      any(h.get("args") == ["-m", "bridgecore.hook"] for h in _pre2), False)
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
print("    The audit runs against Max's own transcripts, which exist only on")
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
_ns = {}
exec(compile(open(_vp, encoding="utf-8").read(), _vp, "exec"), _ns)
check("it checks every file the package snippet lists, itself included - "
      "a package its recipient cannot verify is a weaker package",
      (len(_ns["FILES"]), "source/verify_package.py" in _ns["FILES"],
       "source/HONESTY_CASES.md" in _ns["FILES"],
       "source/LICENSE" in _ns["FILES"]), (27, True, True, True))
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
      (_bad, _extra, len(_names)), ([], [], 27))
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
      len(_s2) < len(_s1) / 4, True)
check("but still names every rule",
      len([l for l in _s2.splitlines() if re.match(r"^\d+\. \S", l)]), 28)
check("and says where the full text is, so nothing is hidden by shortening",
      "HONESTY.md" in _s2, True)
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
      _json.load(open(_k, encoding="utf-8")).get("something_new"),
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
    check("after the grace, the human is told once", len(_told), 1)
    check("and told what it means - the pair is stopped, not working",
          "idle, not working" in _told[0][1], True)
    check("with the reason in it, not 'unknown'",
          "Connection closed" in _told[0][1], True)
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
_ctl = {0x00, 0x07, 0x08, 0x0B, 0x0C, 0x0D, 0x1A, 0x1B}
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
        _hit = sorted({c for c in _ctl if bytes([c]) in _b})
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
      any(bytes([c]) in open(_probe, "rb").read() for c in _ctl), True)
check("and a clean file of the same text is not",
      any(bytes([c]) in ("the package line read `.." + chr(92)
                         + "bridge.zip`\n").encode("utf-8") for c in _ctl),
      False)

print("\n" + ("-" * 60))
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
