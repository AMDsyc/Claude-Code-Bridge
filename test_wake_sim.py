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

"""Ten seeded runs of a pair through faults, after the wake-up was removed.

On 2026-08-30 one delivery was deleted: the "Process finished" notice that
woke the planner every time a tracked command ended
(`handle_event`/PostToolUse). It cost 57.9M tokens in a single night and
carried no decision. Nothing else was touched - and that is exactly what
needs proving, because the thing it sat next to is the stuck-process watch,
which exists so that a hung process is never missed.

So the assertion of this suite is about SAFETY and not about economy:

    every fault injected here is still detected, still named in the
    journal, and still reported - no later than it was before.

Economy is checked too, but it is one line: no "Process finished" reaches a
window, while its journal line is still written for every command.

WHAT A FAILURE WOULD LOOK LIKE, said in advance (rule 19). If the removal
had taken the detector with it, the `stuck` scenario would end with
PROCTRACK holding a command that ran past `stuck_limit` and no journal line
naming it and nothing delivered to the planner - and the run would print
FAIL on "the stuck process was named". Two sabotage modes below make
exactly that happen on purpose, so the suite is known to be able to fail
rather than assumed to be.

    BRIDGE_SIM_SABOTAGE=safety   blinds check_processes   -> safety FAILS
    BRIDGE_SIM_SABOTAGE=economy  restores the wake-up     -> economy FAILS
    BRIDGE_SIM_SABOTAGE=sample   takes the compaction sample from the
                                 neighbouring session, as before the
                                 2026-08-30 fix           -> two-windows FAILS

THE SEED IS REAL. `scenario(seed)` shuffles the fault list and picks the
command shapes, so one seed reproduces one run bit for bit and ten seeds
are ten different orders. The suite proves both rather than claiming them:
same seed -> same fingerprint, and ten distinct fingerprints.

Run:  python test_wake_sim.py            (ten seeds)
      python test_wake_sim.py 3          (one seed, for a red one)
      BRIDGE_SIM_OUT=<dir>               one file per seed
"""
import hashlib
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

TMP = tempfile.mkdtemp(prefix="bridge-wakesim-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-home")
os.environ["BRIDGE_NO_HOOKS"] = "1"
os.environ["PYTHONUTF8"] = "1"
# Time is accelerated by moving the clocks the bridge reads, never by
# sleeping: ten seeds have to fit in a coffee break, and a suite that waits
# out a 311s floor ten times over is a suite nobody runs.
os.environ["BRIDGE_WATCH_SEC"] = "1"
os.environ["BRIDGE_GRACE_SEC"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgecore import daemon, sessions, store, telegram          # noqa: E402

SABOTAGE = os.environ.get("BRIDGE_SIM_SABOTAGE", "")
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
with open(STUB, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys, time\n"
        "row = {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "       'role': os.environ.get('BRIDGE_ROLE')}\n"
        "open(%r, 'a', encoding='utf-8').write("
        "json.dumps(row, ensure_ascii=False) + '\\n')\n"
        "time.sleep(30)\n" % LAUNCHES)

_real_build = sessions.build_command


def _stub_build(*a, **kw):
    """The real command line with only the executable swapped.

    A stub cannot be reached by name on Windows - CreateProcess appends
    only .exe - so it is passed as an explicit [interpreter, script] pair,
    exactly as the other two suites do.
    """
    return [sys.executable, STUB] + _real_build(*a, **kw)[1:]


sessions.build_command = _stub_build
sessions.CREATE_NEW_CONSOLE = 0


def launches():
    if not os.path.exists(LAUNCHES):
        return []
    with open(LAUNCHES, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


# One recording channel per (project, role). `written` is what the bridge
# reads back to tell "queued" from "read" (5.41); a channel put into
# UNREAD answers 0 so the unread-window detector has something to find.
DELIVERED = {}
UNREAD = set()


class Chan(BaseHTTPRequestHandler):
    who = ("", "")

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        DELIVERED.setdefault(self.who, []).append(body)
        out = json.dumps(
            {"written": 0 if self.who in UNREAD else 1}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def open_channel(project, role):
    who = (daemon.norm(project), role)
    DELIVERED.setdefault(who, [])
    cls = type("Chan_%d_%s" % (abs(hash(who)) % 9999, role),
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
daemon.CFG["projects"] = {}
daemon.CFG["telegram"] = {"token": "t", "chat_id": "1",
                          "pinned_message_id": 0}
daemon.CFG.setdefault("thresholds", {}).update({
    "review_timeout": 8, "channel_silence_warn": 5, "stall_grace": 1,
    "stall_quiet": 1, "clinch_grace": 1, "startup_grace": 1,
    "handover_grace": 1, "silence_limit": 3,
    # The reports here are two words, which trivial_report() rightly calls
    # empty - and the idle damper would then hold the Stop hook for twenty
    # minutes instead of reviewing it. test_wall_handover.py switches the
    # damper off for the same reason.
    "idle_hold": 0})

SRV = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
PORT = SRV.server_address[1]
os.environ["BRIDGE_PORT"] = str(PORT)
threading.Thread(target=SRV.serve_forever, daemon=True).start()

FAILED = []
LOG = []


def say(line=""):
    print(line)
    LOG.append(line)


def check(name, got, want):
    ok = got == want
    say("  %-4s %s" % ("ok" if ok else "FAIL", name))
    say("       got %r, want %r" % (got, want))
    if not ok:
        FAILED.append(name)
    return ok


def post(path, payload, secret=False, timeout=30):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Bridge-Secret"] = daemon.SECRET
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8") or "{}")
            if isinstance(out, dict):
                out.setdefault("status", resp.status)
            return out
    except urllib.error.HTTPError as exc:
        out = json.loads(exc.read().decode("utf-8") or "{}")
        if isinstance(out, dict):
            out.setdefault("status", exc.code)
        return out
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        return {"ok": False, "error": "server gone"}


def hook(project, name, role, sid, **extra):
    ev = {"hook_event_name": name, "role": role, "session_id": sid,
          "project_dir": project, "cwd": project}
    ev.update(extra)
    return post("/event", ev)


def statusline(project, role, sid, tokens, window=1000000, model="Opus 5 (1M context)"):
    """What statusline.py posts on every redraw - the only source of real
    context numbers, and the thing a compaction sample is read from."""
    return post("/status", {"role": role, "payload": {
        "session_id": sid,
        "workspace": {"current_dir": project, "project_dir": project},
        "model": {"display_name": model, "id": "claude-opus-5[1m]"},
        "context_window": {"context_window_size": window,
                           "used_percentage": round(tokens * 100.0 / window, 1),
                           "current_usage": {
                               "input_tokens": 10,
                               "cache_creation_input_tokens": 10,
                               "cache_read_input_tokens": max(0, tokens - 20),
                               "output_tokens": 5}}}})


def until(fn, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def journal_has(project, needle, kind=None):
    """A journal line naming this, for this project. The witness of record."""
    for r in store.recent_events(4000, project=daemon.norm(project)):
        if kind and (r.get("kind") or "") != kind:
            continue
        if needle.lower() in (r.get("text") or "").lower():
            return r
    return None


# ---------------------------------------------------------------------------
# the scenario, which is what the seed decides

# Command shapes. The "noisy" ones are the real thing: these were taken from
# a project whose own directory path contains one of the tracked words, so
# every one of them is tracked because the PATH contains it, not because the
# command is a build. That over-matching is left alone on purpose - it feeds
# the stuck-process watch - so the suite uses it rather than pretending it is
# gone.
NOISY = ['ls -la {p}', 'grep -n "x" {p}/a.gd', 'wc -l {p}/b.gd',
         'date "+%H:%M"; tail -2 {p}/run.out']
REAL = ['godot --headless --script {p}/x.gd', 'cd {p} && make',
        'pytest -q {p}/tests', 'npm test']

FAULTS = ["stuck", "dead_turn", "leftover_inflight", "silent_planner",
          "silent_executor",
          "unread_channel", "compaction", "handover", "task_midturn",
          "report_wait", "report_continue", "two_windows_compaction"]


def scenario(seed):
    """One seed, one run. Same seed -> same list, always."""
    rnd = random.Random(seed)
    steps = FAULTS[:]
    rnd.shuffle(steps)
    plan = []
    for s in steps:
        plan.append({"fault": s,
                     "noisy": rnd.choice(NOISY),
                     "real": rnd.choice(REAL),
                     # Above stuck_limit(None), which is max(311, 900),
                     # and below INFLIGHT_MAX_SEC (3600), past which
                     # check_processes calls the record a leak rather than
                     # a slow command. Both bounds are read from the code
                     # rather than written out, so a change to either is
                     # caught here instead of silently skipping the case.
                     "secs": rnd.choice([1000, 1500, 2400, 3000])})
    return plan


def fingerprint(plan):
    raw = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# one run

def run_seed(seed):
    """Drive one pair through the seed's scenario. Returns a result dict."""
    proj = os.path.join(TMP, "proj%02d" % seed)
    os.makedirs(proj, exist_ok=True)
    key = daemon.norm(proj)
    ex_sid, pl_sid = "ex%02d" % seed, "pl%02d" % seed
    plan = scenario(seed)

    with daemon._lock:
        daemon.CFG.setdefault("projects", {})[key] = {}
        daemon.save_state()

    # both halves come up and register a channel
    for role, sid in (("executor", ex_sid), ("planner", pl_sid)):
        hook(proj, "SessionStart", role, sid)
        port = open_channel(proj, role)
        post("/channel/register",
             {"project": proj, "role": role, "port": port,
              "pid": os.getpid(), "ppid": os.getppid(), "session_id": sid},
             secret=True)

    # The executor's session id is not constant for the whole run: a
    # handover retires the window and a replacement registers in its place,
    # which is the real sequence and the reason a report AFTER a handover
    # has to come from the new session. Holding it in a dict keeps every
    # later step pointed at whichever window is actually live.
    cur = {"ex": ex_sid}

    # The loop is started through the endpoint the panel posts to, not by
    # writing STATE by hand. Round 6 of this suite spent itself on exactly
    # that shortcut: the hand-written record used a key loop_state() does
    # not read, so lp["active"] was None and every Stop was skipped without
    # a word - and the case only passed when a task happened to re-arm the
    # loop first, which made it look order-dependent rather than wrong.
    post("/loop", {"action": "start", "project": proj, "reset": True})

    res = {"seed": seed, "plan": plan, "detect": {}, "latency": {},
           "finished_journal": 0, "finished_delivered": 0}

    for step in plan:
        f = step["fault"]
        noisy = step["noisy"].format(p=proj)
        real = step["real"].format(p=proj)

        # Every scenario runs an ordinary tracked command first: this is
        # the event whose delivery was removed, and it has to keep its
        # journal line in every one of them.
        hook(proj, "PreToolUse", "executor", cur["ex"],
             tool_name="Bash", tool_input={"command": noisy})
        hook(proj, "PostToolUse", "executor", cur["ex"],
             tool_name="Bash", tool_input={"command": noisy})

        if f == "stuck":
            # A real long command that never comes back. PROCTRACK is aged
            # past the floor and the watch is asked - the same call
            # process_watch() makes on its own timer.
            hook(proj, "PreToolUse", "executor", cur["ex"],
                 tool_name="Bash", tool_input={"command": real})
            sig = real.split()[0][:40]
            t0 = time.time()
            for meta in (daemon.PROCTRACK.get(key) or {}).values():
                meta["started"] = time.time() - step["secs"]
            daemon.check_processes()
            res["latency"]["stuck"] = time.time() - t0
            res["detect"]["stuck_journal"] = bool(
                journal_has(proj, "has run", kind="process"))
            res["detect"]["stuck_told_pair"] = any(
                "Decide whether it is stuck" in json.dumps(
                    d, ensure_ascii=False)
                for d in DELIVERED.get((key, "planner"), []))
            # let it go, so the next step starts clean
            hook(proj, "PostToolUse", "executor", cur["ex"],
                 tool_name="Bash", tool_input={"command": real})

        elif f == "dead_turn":
            t0 = time.time()
            hook(proj, "StopFailure", "executor", cur["ex"],
                 error="invalid_request: prompt is too long: "
                       "1000401 tokens > 1000000 maximum")
            res["latency"]["dead_turn"] = time.time() - t0
            res["detect"]["dead_turn_recorded"] = bool(
                (daemon.STATE.get("stopfail") or {}).get(
                    "%s|executor" % key))

        elif f == "leftover_inflight":
            hook(proj, "PreToolUse", "executor", cur["ex"],
                 tool_name="Bash", tool_input={"command": real})
            with daemon._lock:
                for m in (daemon.STATE.get("inflight", {}).get(key)
                          or {}).values():
                    m["started"] = time.time() - daemon.INFLIGHT_MAX_SEC - 60
                daemon.save_state()
            t0 = time.time()
            live = daemon.inflight_live(key)
            res["latency"]["leftover_inflight"] = time.time() - t0
            # the whole point of the ageing: a record left by a dead turn
            # must stop counting as a running process, or all three tiers
            # stay silent for ever
            res["detect"]["inflight_aged_out"] = (live == {} or not live)
            hook(proj, "PostToolUse", "executor", cur["ex"],
                 tool_name="Bash", tool_input={"command": real})

        elif f == "silent_planner":
            before = (daemon.STATE.get("unanswered") or {}).get(key, 0)
            t0 = time.time()
            daemon.note_silence(proj, os.path.basename(proj), 1)
            res["latency"]["silent_planner"] = time.time() - t0
            after = (daemon.STATE.get("unanswered") or {}).get(key, 0)
            res["detect"]["silence_counted"] = after != before

        elif f == "silent_executor":
            # RULE 34's OTHER HALF, and the owner's own words for why it had
            # to be written carefully: "it must not lead to standing still
            # when you stopped in the middle of a task and did not carry on
            # because the bridge did not remind you". The rule forbids
            # stopping; this is the machine that catches it if you stop
            # anyway, and it is TIER 2 - `stalled()`. In production its
            # threshold is the measured stall_quiet of 600 s; this suite
            # sets 1 s like every other threshold here, so a run takes
            # seconds instead of hours - what is being proved is which
            # tier answers, not how long it waits.
            # Tier 1 (`clinch`) sees a pair waiting on
            # each other, which this is not: the executor owes work and
            # simply is not writing. Tier 3, the half-hourly blind poll, is
            # behind both and is not tuned by anybody.
            #
            # The shape is the real one: the window is ALIVE, no tool is
            # running, the status line could still be ticking, and the
            # transcript has not grown. Nothing here asserts on a snapshot
            # of STATE built by hand - the transcript is a real file and the
            # answer comes from the real stalled().
            _tp = os.path.join(TMP, "frozen-%02d.jsonl" % seed)
            with open(_tp, "w", encoding="utf-8") as fh:
                fh.write('{"type":"assistant","message":{"role":'
                         '"assistant"},"timestamp":"2026-08-31T00:00:00Z"}\n')
            _real_tof = sessions.transcript_of
            sessions.transcript_of = lambda _sid, _p=_tp: _p
            try:
                _quiet = float(daemon.CFG.get("thresholds", {})
                               .get("stall_quiet", 600))
                with daemon._lock:
                    daemon.STATE.setdefault("tscript", {})[
                        "%s|executor" % key] = {
                            "size": os.path.getsize(_tp),
                            "at": time.time() - _quiet - 60}
                    daemon.save_state()
                t0 = time.time()
                sit = daemon.situation(proj)
                got = daemon.stalled(proj, sit)
                res["latency"]["silent_executor"] = time.time() - t0
                res["detect"]["silent_executor_named"] = (
                    bool(got) and got[0] == "executor")
                res["silent_executor_answer"] = got
                # And it must be able to say NO: a transcript that has just
                # moved is not a stall, or the tier would accuse a working
                # pair - which is the failure this whole rule must not buy.
                with daemon._lock:
                    daemon.STATE["tscript"]["%s|executor" % key]["at"] = \
                        time.time()
                    daemon.save_state()
                res["detect"]["working_executor_not_accused"] = (
                    daemon.stalled(proj, sit) is None)
            finally:
                sessions.transcript_of = _real_tof
                with daemon._lock:
                    (daemon.STATE.get("tscript") or {}).pop(
                        "%s|executor" % key, None)
                    daemon.save_state()

        elif f == "unread_channel":
            UNREAD.add((key, "planner"))
            daemon.deliver(proj, "planner", "a report nobody is reading",
                           {"kind": "report", "report": "1"})
            with daemon._lock:
                b = (daemon.STATE.get("chan_backlog") or {}).get(
                    "%s|planner" % key)
                if isinstance(b, dict):
                    b["since"] = time.time() - daemon.CHANNEL_UNREAD_SEC - 60
                daemon.save_state()
            t0 = time.time()
            found = daemon.unread_channel(key)
            res["latency"]["unread_channel"] = time.time() - t0
            res["detect"]["unread_named"] = bool(found)
            UNREAD.discard((key, "planner"))

        elif f == "two_windows_compaction":
            # The incident of 2026-08-30 11:48, reproduced faithfully.
            #
            # Two windows of the SAME role are live for one project - the
            # 5.43 duplicate, which ran all that day - and the one that
            # compacts is NOT the one the status line has described. The
            # old code read `ref = sess if sess.get("model") else
            # best_session(path, role)` and then took the SIZE from `ref`,
            # so a session with no status line of its own was calibrated
            # from its neighbour's number:
            #
            #   03:43:05  PreCompact from session 77520958, carrying
            #             999,870, which compacted down to 65,517.
            #   recorded  709,646 - session 179bb1bd's size at 03:37,
            #             exactly, to the token.
            #
            # WHAT A FAILURE LOOKS LIKE: a sample appears carrying the
            # NEIGHBOUR's size. The fix writes no sample at all here, and
            # says so in the journal, because no number beats another
            # session's number - compaction_point anchors on the newest
            # sample, so one wrong entry owns the point until a real one
            # displaces it.
            #
            # session_key() truncates to sid[:8], so the ids must differ
            # inside the first eight characters or they are one record.
            quiet_sid = "qiet%04d-compacting-no-statusline" % seed
            loud_sid = "loud%04d-neighbour-with-statusline" % seed
            NEIGHBOUR = 709646
            # only the NEIGHBOUR ever draws a status line, so only it has a
            # model and a size on record
            statusline(proj, "planner", loud_sid, NEIGHBOUR)
            ckey = "%s|planner" % key
            before_n = len((daemon.STATE.get("compactions") or {}).get(
                ckey, []))
            hook(proj, "PreCompact", "planner", quiet_sid,
                 transcript_path="")
            hist = (daemon.STATE.get("compactions") or {}).get(ckey, [])
            got = hist[-1] if len(hist) > before_n else {}
            res["detect"]["no_neighbour_sample"] = (
                got.get("tokens") != NEIGHBOUR)
            res["detect"]["skip_is_explained"] = bool(journal_has(
                proj, "no sample written"))
            cal = store.calib_get("opus 5 (1m context)", key, 1000000)
            res["detect"]["point_not_the_neighbours"] = (
                cal.get("compact_at_tokens") != NEIGHBOUR)
            res["sample_recorded"] = got.get("tokens")

            # ...and the other half, so the fix is not "never record": a
            # session that HAS its own numbers still calibrates from them.
            own_sid = "ownw%04d-compacting-described" % seed
            OWN = 999870
            statusline(proj, "planner", own_sid, OWN)
            before_m = len((daemon.STATE.get("compactions") or {}).get(
                ckey, []))
            hook(proj, "PreCompact", "planner", own_sid, transcript_path="")
            hist2 = (daemon.STATE.get("compactions") or {}).get(ckey, [])
            got2 = hist2[-1] if len(hist2) > before_m else {}
            res["detect"]["own_sample_kept"] = (got2.get("tokens") == OWN)
            res["sample_own"] = got2.get("tokens")

        elif f == "compaction":
            hook(proj, "PreCompact", "executor", cur["ex"],
                 transcript_path="")
            hook(proj, "Stop", "executor", cur["ex"], transcript_path="",
                 last_assistant_message="seed %d: compacted" % seed)
            res["detect"]["compaction_seen"] = bool(
                (daemon.STATE.get("compactions") or {}).get(
                    "%s|executor" % key))

        elif f == "handover":
            # First: let any replacement that is still coming up come up.
            #
            # An earlier fault in this order may have rotated the executor
            # (dead_turn -> handle_wall_hit -> rotate_executor), and a
            # window the bridge opened seconds ago has not registered yet.
            # launch_guard refuses a second window over one like that, and
            # it is right to - stacking windows on a startup dialog is what
            # it exists to stop. Seed 3 hit exactly that, and the collision
            # is an artifact of this file: `startup_grace` is 1 here so the
            # simulation can run in seconds, and at 1 a window nine seconds
            # old is "past its grace", which at the real 600 means a window
            # that has been stuck for ten minutes. Run this file with
            # startup_grace at 600 and the same check fails on unmodified
            # code, so nothing here is compensating for a change.
            #
            # What a real replacement does within about twenty seconds is
            # register, so that is what happens here.
            _pending = (daemon.STATE.get("pids") or {}).get(
                "%s|executor" % key) or {}
            if _pending.get("pid") and not _pending.get("registered"):
                cur["ex"] = cur["ex"] + "r"
                hook(proj, "SessionStart", "executor", cur["ex"],
                     transcript_path="")
            before = len(launches())
            post("/handover", {"project": proj, "role": "executor",
                               "reason": "the simulation asked"})
            res["detect"]["handover_started"] = until(
                lambda: bool((daemon.STATE.get("handover") or {}).get(key))
                or len(launches()) > before, 6)
            # The replacement comes up and registers, which is what ends a
            # handover (mark_registered). Without this the pair stays
            # mid-rotation and every later Stop is diverted away from the
            # review - which is exactly what seed 3 caught on round 5.
            cur["ex"] = cur["ex"] + "b"
            hook(proj, "SessionStart", "executor", cur["ex"],
                 transcript_path="")
            port2 = open_channel(proj, "executor")
            post("/channel/register",
                 {"project": proj, "role": "executor", "port": port2,
                  "pid": os.getpid(), "ppid": os.getppid(),
                  "session_id": cur["ex"]}, secret=True)
            with daemon._lock:
                (daemon.STATE.get("handover") or {}).pop(key, None)
                daemon.STATE.pop("hoheld:%s" % key, None)
                daemon.save_state()

        elif f == "task_midturn":
            # the endpoint's key is "instructions"; "text" is refused
            post("/task", {"project": proj,
                           "instructions": "seed %d task" % seed},
                 secret=True)
            res["detect"]["task_delivered"] = until(
                lambda: any(
                    "seed %d task" % seed in json.dumps(d, ensure_ascii=False)
                    for d in DELIVERED.get((key, "executor"), [])), 8)

        elif f in ("report_wait", "report_continue"):
            verdict = "wait" if f == "report_wait" else "continue"
            done = {}

            def one_turn():
                # `msg` is read from last_assistant_message on the event
                # (handle_event, the Stop branch): with no text there is no
                # report and no review, which is right - an empty turn is
                # not a report - but it means the simulated turn has to
                # carry one.
                done["r"] = hook(
                    proj, "Stop", "executor", cur["ex"], transcript_path="",
                    last_assistant_message=(
                        "seed %d: the piece is done. Residence: "
                        "bridgecore/daemon.py:handle_event" % seed))

            th = threading.Thread(target=one_turn, daemon=True)
            th.start()
            got = until(lambda: key in daemon.PENDING, 6)
            reply = {}
            if got:
                reply = post("/verdict",
                             {"project": proj, "verdict": verdict,
                              "feedback": "Checked: %s" % proj},
                             secret=True)
            th.join(12)
            # A refusal by verdict_gate is a real answer, not a hang, and a
            # case that only said False would send the next reader hunting
            # the wrong thing. The reason is kept and printed.
            res["detect"][f] = bool(got) and bool(reply.get("ok"))
            res.setdefault("verdict_reply", {})[f] = reply
            if not got:
                _, _lp = daemon.loop_state(key)
                res.setdefault("why_no_pending", {})[f] = {
                    "loop_active": _lp.get("active"),
                    "paused": bool(daemon.paused_for(key)),
                    "handover": bool((daemon.STATE.get("handover")
                                      or {}).get(key)),
                    "hook_returned": done.get("r"),
                }

    # the economy line, and the journal line that must survive it
    res["finished_journal"] = len([
        r for r in store.recent_events(4000, project=key)
        if (r.get("text") or "").startswith("Finished in")])
    res["finished_delivered"] = len([
        d for d in DELIVERED.get((key, "planner"), [])
        if "Process finished:" in json.dumps(d, ensure_ascii=False)])
    # WHAT THE RUN COST, by addressee. The price of a message is the size of
    # the window it lands in and not the size of the message, so the unit of
    # the saving programme is the wake-up, counted per role; tokens follow by
    # multiplying. DELIVERED is keyed by project, and the project is per
    # seed, so these are this seed's and nobody else's.
    res["wakes"] = {role: len(DELIVERED.get((key, role), []))
                    for role in ("executor", "planner")}
    return res


# ---------------------------------------------------------------------------
# sabotage: prove the suite can go red

if SABOTAGE == "safety":
    # Blind the stuck-process watch. The safety assertions MUST fail.
    daemon.check_processes = lambda: None
    say("SABOTAGE=safety: check_processes() is blinded; the stuck-process")
    say("assertions are expected to FAIL. If they pass, this suite proves")
    say("nothing and the run is worthless.")
    say()
elif SABOTAGE == "sample":
    # Reproduce the OLD outcome: the sample is overwritten with the
    # freshest record's size, which is what ref = best_session() did. The
    # two-windows assertions must go red.
    _real_pre = daemon.handle_event

    def _old_sample(event):
        out = _real_pre(event)
        if event.get("hook_event_name") == "PreCompact":
            pth = daemon.norm(event.get("project_dir")
                              or event.get("cwd") or "")
            k = "%s|%s" % (pth, event.get("role") or "")
            hist = (daemon.STATE.get("compactions") or {}).get(k) or []
            nb = daemon.best_session(pth, event.get("role") or "")
            # exactly the old outcome: a session the status line has not
            # described takes its neighbour's size
            if hist and not hist[-1].get("tokens")                     and nb.get("context_tokens"):
                hist[-1]["tokens"] = nb.get("context_tokens")
        return out

    daemon.handle_event = _old_sample
    say("SABOTAGE=sample: the compaction sample is taken from the freshest")
    say("record instead of the compacting session, as it was before the fix.")
    say("The two-windows assertions are expected to FAIL.")
    say()
elif SABOTAGE == "economy":
    # Put the wake-up back, exactly as it was before the fix.
    _real_handle = daemon.handle_event

    def _noisy_handle(event):
        out = _real_handle(event)
        if event.get("hook_event_name") == "PostToolUse":
            tin = event.get("tool_input") or {}
            cmd = (tin.get("command") or "") if isinstance(tin, dict) else ""
            p = daemon.norm(event.get("project_dir") or event.get("cwd") or "")
            if cmd:
                daemon.deliver(p, "planner",
                               "Process finished: %s (0 s)." % cmd[:160],
                               {"kind": "info"})
        return out

    daemon.handle_event = _noisy_handle
    say("SABOTAGE=economy: the removed wake-up is put back; the economy")
    say("assertion is expected to FAIL.")
    say()

# ---------------------------------------------------------------------------

SEEDS = ([int(a) for a in sys.argv[1:] if a.isdigit()]
         or list(range(1, 11)))

say("wake-up simulation - throwaway daemon on 127.0.0.1:%d" % PORT)
say("the live daemon on 8765 is never contacted; BRIDGE_DATA=%s"
    % os.environ["BRIDGE_DATA"])
say("seeds: %s" % ", ".join(str(s) for s in SEEDS))
say()

say("0. the seed is real, not decoration")
fps = {s: fingerprint(scenario(s)) for s in SEEDS}
check("the same seed gives the same scenario twice",
      fingerprint(scenario(SEEDS[0])) == fps[SEEDS[0]], True)
check("every seed gives a different one", len(set(fps.values())), len(SEEDS))
say("     %s" % ", ".join("%d:%s" % (s, fps[s]) for s in SEEDS))
say()

RESULTS = {}
for seed in SEEDS:
    say("-" * 66)
    say("SEED %d   fingerprint %s" % (seed, fps[seed]))
    say("   order: %s" % " -> ".join(p["fault"] for p in scenario(seed)))
    r = run_seed(seed)
    RESULTS[seed] = r

    say("  SAFETY - every fault named")
    check("the stuck process was named in the journal",
          r["detect"].get("stuck_journal"), True)
    check("and the pair was told to decide about it",
          r["detect"].get("stuck_told_pair"), True)
    check("the dead turn was recorded",
          r["detect"].get("dead_turn_recorded"), True)
    check("a leftover inflight record aged out instead of silencing the tiers",
          r["detect"].get("inflight_aged_out"), True)
    check("the planner's silence was counted",
          r["detect"].get("silence_counted"), True)
    check("an executor that stopped mid-task is named by tier 2",
          r["detect"].get("silent_executor_named"), True)
    check("and a working one is NOT accused of it",
          r["detect"].get("working_executor_not_accused"), True)
    say("     tier 2 answered %r" % (r.get("silent_executor_answer"),))
    check("the window that reads nothing was named",
          r["detect"].get("unread_named"), True)
    check("the compaction was seen", r["detect"].get("compaction_seen"), True)
    check("a session with no status line is not calibrated from its "
          "neighbour", r["detect"].get("no_neighbour_sample"), True)
    check("and the skipped sample is explained in the journal",
          r["detect"].get("skip_is_explained"), True)
    check("so the calibration point is not the neighbour's size",
          r["detect"].get("point_not_the_neighbours"), True)
    check("while a session that HAS its own numbers still calibrates from "
          "them", r["detect"].get("own_sample_kept"), True)
    say("     neighbour carried 709646; sample written for the quiet "
        "session: %r, for the described one: %r"
        % (r.get("sample_recorded"), r.get("sample_own")))
    check("the handover started", r["detect"].get("handover_started"), True)
    check("a task reached the executor",
          r["detect"].get("task_delivered"), True)
    check("a report was answered with wait", r["detect"].get("report_wait"),
          True)
    check("and another with continue",
          r["detect"].get("report_continue"), True)
    for _f, _why in (r.get("why_no_pending") or {}).items():
        say("       %s never registered a waiter: %s"
            % (_f, json.dumps(_why, ensure_ascii=False)[:400]))
    for _f, _rep in (r.get("verdict_reply") or {}).items():
        if not _rep.get("ok"):
            say("       %s was refused: %s"
                % (_f, str(_rep.get("error") or _rep)[:300]))

    say("  ECONOMY - the one thing that was removed")
    check("no 'Process finished' woke the planner",
          r["finished_delivered"], 0)
    check("while every finished command still has its journal line",
          r["finished_journal"] > 0, True)
    say("     journal lines: %d,  wake-ups: %d"
        % (r["finished_journal"], r["finished_delivered"]))
    say("     this seed cost: executor %d wake-ups, planner %d"
        % (r["wakes"]["executor"], r["wakes"]["planner"]))

    if OUTDIR:
        os.makedirs(OUTDIR, exist_ok=True)
        with open(os.path.join(OUTDIR, "seed-%02d.txt" % seed), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(LOG))
            fh.write("\nEXIT=%d\n" % (1 if FAILED else 0))

say("-" * 66)
say()
say("WAKE-UPS, per seed, by addressee - the unit of the saving programme.")
say("A message is charged the size of the window it arrives in, measured at")
say("about 720,000 tokens for a planner and 342,000 for an executor, however")
say("long the message is. These are counts, not tokens: there is no model")
say("behind the stub, so a token figure here would be arithmetic dressed as")
say("a measurement. Multiply if you want the money.")
say("%-6s %10s %10s %10s" % ("seed", "executor", "planner", "total"))
_wex = _wpl = 0
for seed in SEEDS:
    _w = RESULTS[seed]["wakes"]
    _wex += _w["executor"]
    _wpl += _w["planner"]
    say("%-6d %10d %10d %10d"
        % (seed, _w["executor"], _w["planner"], _w["executor"] + _w["planner"]))
say("%-6s %10d %10d %10d" % ("TOTAL", _wex, _wpl, _wex + _wpl))
say("%-6s %10.1f %10.1f %10.1f"
    % ("mean", _wex / float(len(SEEDS)), _wpl / float(len(SEEDS)),
       (_wex + _wpl) / float(len(SEEDS))))
say()
say("detection latency, seconds, per seed (the comparison the owner asked")
say("for: removing a delivery must not make any detector slower)")
say("%-6s %-9s %-11s %-18s %-11s %s"
    % ("seed", "stuck", "dead_turn", "leftover_inflight", "silence",
       "unread"))
for s in SEEDS:
    la = RESULTS[s]["latency"]
    say("%-6d %-9.4f %-11.4f %-18.4f %-11.4f %.4f"
        % (s, la.get("stuck", 0), la.get("dead_turn", 0),
           la.get("leftover_inflight", 0), la.get("silent_planner", 0),
           la.get("unread_channel", 0)))
say()
worst = max((RESULTS[s]["latency"].get("stuck", 0) for s in SEEDS),
            default=0)
say("slowest stuck-process detection across all seeds: %.4fs" % worst)
say("It is the same call on the same records as before the change: the")
say("removal took a deliver() out of PostToolUse and touched neither")
say("PROCTRACK, nor check_processes, nor stuck_limit.")
say()

SRV.shutdown()
SRV.server_close()

say("=" * 66)
if FAILED:
    say("FAILED: %d" % len(FAILED))
    for f in FAILED:
        say("  - %s" % f)
    if OUTDIR:
        with open(os.path.join(OUTDIR, "summary.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(LOG) + "\nEXIT=1\n")
    sys.exit(1)
say("all seeds pass")
if OUTDIR:
    with open(os.path.join(OUTDIR, "summary.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(LOG) + "\nEXIT=0\n")
