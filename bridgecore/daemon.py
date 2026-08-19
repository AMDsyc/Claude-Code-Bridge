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

"""The bridge daemon.

Everything meets here: hook events, status-line telemetry, the review loop
through the planner's live session, context calibration and rotation, plan
limits, stuck-process tracking, Telegram, the panel.

Runs on 127.0.0.1 only. Start with bridge.bat or python -m bridgecore.daemon.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import store, telegram, discover, remote, sessions, models, archive

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PANEL = os.path.join(HERE, "panel.html")
# The rules every pair is handed at startup, collected from what Max
# actually repeated to the pairs rather than composed. Kept as a file on
# disk, next to the package, so it can be rewritten without touching code -
# read fresh each time, so an edit reaches the next session that starts
# rather than waiting for a restart.
HONESTY = os.path.join(ROOT, "HONESTY.md")
HONESTY_CASES = os.path.join(ROOT, "HONESTY_CASES.md")


def honesty_cases_text():
    """The evidence half - dated incidents and quotations. Never delivered:
    it is five times the size of the rules and stays local."""
    try:
        with open(HONESTY_CASES, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def honesty_text():
    """The canon, or "" if it is not there.

    Never raises and never blocks a session from starting: a missing or
    unreadable file costs the reminder, not the run.
    """
    try:
        with open(HONESTY, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""

# The rules go in FRONT of the work, not beside it.
#
# A canon handed over once at SessionStart is read once and then competes for
# attention with everything that follows - and loses, because the task is
# concrete and urgent and the rules are neither. So the short form is put at
# the head of the two messages where it can still change what happens: the
# task, at the moment work begins, and the report, at the moment work is
# judged. That is also why HONESTY.md was cut to norms only and the evidence
# moved to HONESTY_CASES.md - this text is paid for on every delivery, so it
# has to be worth carrying.
#
# Read from disk EVERY time: editing the file reaches the next delivery
# without restarting anything.
RULES_KINDS = ("task", "report")
RULES_OPEN = ("=" * 70 + "\nRULES OF WORK - read them before you start. "
              "They do not replace the task,\nthey say how to do it.\n"
              + "=" * 70)
RULES_OPEN_SHORT = ("=" * 70 + "\nRULES OF WORK, a reminder. The full "
                    "text with its checks is HONESTY.md\nin the "
                    "bridge folder; this session was given it\n" + "whole at its start.\n" + "=" * 70)
RULES_CLOSE = ("=" * 70 + "\nEnd of the rules. Below is the %s itself.\n" + "=" * 70)
_RULES_MISSING_TOLD = [False]

# How much of the canon rides on a delivery, and why it is not the same every
# time.
#
# Measured before this was built: the whole short canon is ~8.5k characters,
# call it 3.2-3.8k tokens, so a task and a report together cost ~7k. That is
# not mainly a billing question - every delivered task adds it to the
# executor's WINDOW permanently, and fifty tasks would be ~175k of the same
# text carried in context, bringing compaction and then rotation that much
# sooner. The bridge would be spending windows on repetition.
#
# So: the full text once, where it is worth its price - the first delivery a
# session ever gets, which is also the first delivery after every handover,
# because a replacement window has been told nothing. After that the titles
# alone, ~1k characters, which still put all 26 rules in front of the work by
# name with the file one read away.
#
# The mark is kept per SESSION ID, not per project and not per role. That is
# the whole point: a handover makes a new session id, so the replacement is
# owed the full text again, and a project that runs for weeks does not get one
# reminder in August because it got one in July.


def honesty_titles():
    """Just the rule headings, read fresh like everything else here."""
    out = []
    for line in honesty_text().split("\n"):
        m = re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*", line.strip())
        if m:
            out.append("%s. %s" % (m.group(1), m.group(2)))
    return "\n".join(out)


def rules_seen(sid):
    """Has this session already been given the canon in full?"""
    if not sid:
        return False
    return bool((STATE.get("rules_full") or {}).get(sid))


def mark_rules_seen(sid):
    if not sid:
        return
    with _lock:
        seen = STATE.setdefault("rules_full", {})
        seen[sid] = now()
        # Bounded without needing a reaper: session ids are never reused, so
        # the only cost of an old entry is bytes. Trim the oldest when it
        # grows past anything a real bridge would hold at once.
        if len(seen) > 200:
            for k in list(seen)[:len(seen) - 200]:
                seen.pop(k, None)
        save_state()


def rules_for_delivery(kind, sid=None):
    """The canon in front of a delivery: in full the first time this session
    is written to, by title every time after.

    sid=None means the caller could not say which window this is going to.
    That answers "full text", deliberately: the cost of one extra copy is
    known and small, and the cost of a session never seeing the rules is the
    thing this whole mechanism exists to prevent.

    Nothing on this path may raise: a missing canon costs the reminder, never
    the delivery. It says so once per daemon run rather than on every
    message, because a line per delivery is not a warning, it is noise.
    """
    if kind not in RULES_KINDS:
        return ""
    try:
        text = honesty_text()
    except Exception:
        text = ""
    if not text:
        if not _RULES_MISSING_TOLD[0]:
            _RULES_MISSING_TOLD[0] = True
            store.journal("bridge", "HONESTY.md is missing or empty, so the "
                                    "rules are NOT being put in front of "
                                    "tasks and reports. Delivery itself is "
                                    "unaffected. Looked in: %s" % HONESTY,
                          "", "", "warn")
        return ""
    what = "task" if kind == "task" else "executor report"
    full = not rules_seen(sid)
    if full:
        mark_rules_seen(sid)
        body, head = text, RULES_OPEN
    else:
        body, head = honesty_titles(), RULES_OPEN_SHORT
        if not body:                      # a canon with no numbered rules
            body, head = text, RULES_OPEN
    return "%s\n\n%s\n\n%s\n\n" % (head, body, RULES_CLOSE % what)


def with_rules(content, meta, sid=None):
    """Put the rules in front of a task or a report. Everything else is
    passed through untouched - a verdict is an answer to something the
    planner already has in front of it, and an info line is not work."""
    kind = (meta or {}).get("kind") or ""
    head = rules_for_delivery(kind, sid)
    return (head + content) if head else content


CFG = store.load_config()
STATE = store.load_state()
_lock = threading.RLock()
SECRET = store.secret()

CHANNELS = {}   # (path, role) -> {"port": int, "ts": float, "pid": int}
PENDING = {}    # path -> {"event": Event, "verdict": str, "feedback": str}
QUEUED = {}     # path -> [json rows] awaiting the planner channel
LAUNCHED = {}   # (path, role) -> last auto-launch ts
NAMEWAIT = {}   # path -> {"event": Event, "name": str, "suggested": str}
PROCTRACK = {}  # path -> {sig: {"cmd", "started", "session"}}
DURATIONS = {}  # (path, sig) -> [seconds, ...]

EVENT_MAP = {
    "SessionStart": ("session_start", "Session started"),
    "SessionEnd": ("session_end", "Session ended"),
    "Stop": ("iteration_done", "Turn finished"),
    "StopFailure": ("crash", "Turn ended with an error"),
    "Notification": ("needs_you", "Claude needs you"),
    "PreCompact": ("limit_low", "Context compaction"),
    "SubagentStop": ("iteration_done", "Subagent finished"),
}


def now():
    return time.strftime("%H:%M:%S")


# The canonical form of a path, and the key for everything here. One
# definition, in the lowest module, because sessions.py keys its live
# process handles the same way and two implementations would drift apart -
# see store.norm for why normpath alone is not enough.
norm = store.norm


PATH_KEYED = ("loops", "inflight", "awaiting", "loop_off", "loop_off_told",
              "seed", "planner_seed", "handover", "last_feedback",
              "paused", "note", "idle_spin", "noart", "frames", "debt",
              "unanswered")
PAIR_KEYED = ("pids", "down", "launches", "last_session", "channels",
              "autostart_tried", "autostart_told")


def migrate_keys():
    """Re-key anything stored under an older, case-sensitive path."""
    moved = 0
    with _lock:
        for name in PATH_KEYED:
            d = STATE.get(name)
            if not isinstance(d, dict):
                continue
            for old in list(d):
                new = norm(old)
                if new != old:
                    d.setdefault(new, d.pop(old))
                    moved += 1
        for name in PAIR_KEYED:
            d = STATE.get(name)
            if not isinstance(d, dict):
                continue
            for old in list(d):
                p, _, role = old.rpartition("|")
                new = "%s|%s" % (norm(p), role)
                if new != old:
                    d.setdefault(new, d.pop(old))
                    moved += 1
        for sess in (STATE.get("sessions") or {}).values():
            if sess.get("path"):
                sess["path"] = norm(sess["path"])
        for pref in ("tight:", "hoheld:", "jump:", "warned:"):
            for old in [k for k in list(STATE) if k.startswith(pref)]:
                new = pref + norm(old[len(pref):])
                if new != old:
                    STATE.setdefault(new, STATE.pop(old))
                    moved += 1
        if moved:
            save_state()
    return moved


def migrate_executor_mode():
    """Let go of the executor mode that was the old default.

    Every project has modes.executor = "auto" saved against it - not
    because anyone chose auto, but because the panel wrote down whatever
    the default was at the time. A per-project value wins over the
    bridge-wide one, so leaving those in place would mean the new default
    never applied anywhere and the change would look like it had done
    nothing.

    Only that exact value, and only for the executor. Anything else in
    there was set on purpose and is left alone; the planner is not touched
    at all, because its protection is disallow_for and not its mode.
    """
    moved = []
    with _lock:
        for key, pc in (CFG.get("projects") or {}).items():
            modes = (pc or {}).get("modes") or {}
            if modes.get("executor") == "auto":
                modes.pop("executor", None)
                moved.append(project_name(key))
        if moved:
            store.save_config(CFG)
    if moved:
        store.journal("bridge", "Dropped the saved executor mode \"auto\" "
                      "from %d project(s) - %s - so the bridge-wide default "
                      "applies. It was written down as the default of the "
                      "day, not chosen; anything set deliberately was left "
                      "alone." % (len(moved), ", ".join(sorted(moved))),
                      level="log")
    return moved


def migrate_note():
    """Turn a pre-multipair note into the per-project form.

    migrate_keys cannot do this. It re-keys dictionaries and skips anything
    that is not one (the isinstance guard above), and the old note was a
    plain string - so listing it in PATH_KEYED would leave the string in
    place and the first `.get(path)` on it would raise.

    A note that is still sitting there is one the human wrote and no report
    has collected yet. It is dropped rather than handed to a project,
    because there is no honest way to say which pair it was meant for: the
    same silent "take the first project in the config" that this release is
    removing everywhere else. Dropped loudly - the text goes to the journal,
    so nothing the human typed disappears without trace.
    """
    old = STATE.get("note")
    if isinstance(old, dict):
        return None
    with _lock:
        STATE["note"] = {}
        save_state()
    text = (old or "").strip()
    if not text:
        return None
    store.journal("bridge", "A note was waiting from before notes belonged "
                            "to a project, and there is no way to tell which "
                            "pair it was for, so it was not delivered. It "
                            "said: %s" % text[:500], level="log")
    return text


# The two roles the bridge runs. Anything else is a window that exists and
# is alive, but is not half of this pair: a session started by hand, or one
# opened before BRIDGE_ROLE was in its environment. Its hooks report an
# empty role and its channel reports "unknown", so it arrives as two records
# under two invented role names - which is how a pair of sessions came to be
# shown as four, with a "hand over unknown" button offered for one of them.
# They are kept and shown, because they are real and they consume the plan
# limits; they are never planned for, never counted as the pair, and never
# handed over.
MANAGED_ROLES = ("executor", "planner")


def managed(role):
    """Is this one of the two roles the bridge runs?

    The value is normalised here because it arrives from three places that
    did not agree: the hooks and the status line sent the environment
    variable raw, channel.py lowercased it. A comparison against lowercase
    literals therefore depended on which door the message came through.
    """
    return str(role or "").strip().lower() in MANAGED_ROLES


def note_stranger(path, sid):
    """Count a window that is not part of the pair, and nothing more.

    Worth counting because it is real, it is in the same folder, and it
    spends the same plan limits - so "the loop is slow today" has an answer.
    Worth nothing else: it has no role, no runway and no handover.
    """
    if not sid:
        return
    with _lock:
        book = STATE.setdefault("strangers", {}).setdefault(norm(path), {})
        book[sid[:8]] = time.strftime("%Y-%m-%d %H:%M:%S")
        for k, seen in list(book.items()):
            if seen < time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(time.time() - 3600)):
                book.pop(k, None)
        save_state()


def forget_unmanaged():
    """Drop session records that were made for windows without a role.

    They arrived from two directions with two invented names - the hooks
    report an empty role, the channel reports "unknown" - so one stray
    window showed up as two sessions, and a pair was displayed as four.
    Cleared once at startup so an existing state file heals itself.
    """
    dropped = []
    with _lock:
        for key, s in list((STATE.get("sessions") or {}).items()):
            if not managed(s.get("role")):
                STATE["sessions"].pop(key, None)
                dropped.append(key)
        # Records only. The other books hold channel ports, pids and
        # compaction history - throwing those away for a role that merely
        # failed to introduce itself loses a live session's whole trail.
        if dropped:
            save_state()
    return dropped


def seen_at(sess):
    """When this record was last touched, as a real point in time.

    "last_seen" is a clock and nothing more - "%H:%M:%S", no date - because
    it is written to be read by a human on the panel. Compared as a string
    it says that yesterday at 16:40 is newer than today at 11:27, and that
    is not a nicety: prune_sessions keeps the newest record per role and
    retires the rest, so every 45 seconds it kept a session that ended
    yesterday afternoon and retired the one that was running now. The
    adoption pass then found the live window unrecorded, wrote it down
    again, and the next prune threw it away again. Two lines in the feed
    every 45 seconds for an hour, and a pair that was demonstrably working
    - finishing turns, taking verdicts - shown in the strip as having no
    sessions at all.

    Records written before this existed have no seen_at and sort oldest,
    which is what they are.
    """
    v = (sess or {}).get("seen_at")
    return float(v) if isinstance(v, (int, float)) and v else 0.0


def session_key(event):
    role = event.get("role") or ""
    sid = event.get("session_id") or ""
    return "%s:%s" % (role or "session", sid[:8])


def project_name(event_or_path):
    """The project's name as you spell it, not as the key was folded.

    Paths are keyed in a canonical form, which on Windows means lower case.
    Taking the basename of that key gave "my_project" in one message and
    "My_project" in the next, depending on which path happened to reach it.
    The config keeps the spelling you typed, so that is what gets shown.
    """
    d = event_or_path if isinstance(event_or_path, str) else (
        event_or_path.get("project_dir") or event_or_path.get("cwd") or "")
    want = norm(d)
    for key in (CFG.get("projects") or {}):
        if norm(key) == want:
            d = key
            break
    return os.path.basename(d.rstrip("\\/")) or "unknown"


def touch_session(event, **fields):
    with _lock:
        key = session_key(event)
        sess = STATE.setdefault("sessions", {}).setdefault(key, {})
        sess.setdefault("first_seen", now())
        sess["role"] = event.get("role") or sess.get("role", "")
        sess["project"] = project_name(event)
        sess["path"] = norm(event.get("project_dir") or event.get("cwd"))
        sess["session_id"] = event.get("session_id", sess.get("session_id", ""))
        sess["managed"] = managed(sess.get("role"))
        sess["last_seen"] = now()
        sess["seen_at"] = time.time()
        if fields.get("window") and fields.get("window_observed") is not False:
            # stated by the status line: the strongest source there is, and
            # the only one written here
            STATE.setdefault("windows", {})[
                "%s|%s" % (norm(sess.get("path")), sess.get("role"))] = {
                    "tokens": int(fields["window"]), "at": now()}
        sess.update({k: v for k, v in fields.items() if v is not None})
        # A size smaller than what was carried into the last compaction is
        # the summary landing: the record has caught up and can be decided
        # from again.
        pend = sess.get("compaction_pending") or {}
        cur = sess.get("context_tokens") or 0
        if pend.get("tokens") and cur and cur < int(pend["tokens"]) * 0.9:
            sess.pop("compaction_pending", None)
        store.save_state(STATE)
    if sess.get("session_id"):
        remember_session(sess.get("path"), sess.get("role"),
                         sess.get("session_id"))
    if sess.get("window"):
        remember_telemetry(sess.get("path"), sess.get("role"), sess)
    return sess


def presence_quiet():
    if not CFG.get("quiet_when_present"):
        return False
    path = CFG.get("presence_file") or ""
    return bool(path and os.path.exists(path))


SOUND_DEFAULT = ("crash", "session_died", "needs_you", "process_stuck",
                 "rotation_name", "limit_low", "run_finished")

# What is allowed to reach the chat at all.
#
# The chat is a phone, not a log. With one pair it could carry everything
# and still be read; with three or four the volume is the problem, and the
# messages that matter drown in the ones that do not. So the rule is the
# other way round from a log: nothing goes out unless it is on this list,
# which means a notify() added anywhere later is silent in the chat by
# default and has to be put here deliberately.
#
# Three things belong in the chat, and they are the three you would get up
# for:
#   * a link to take a session over from the phone - that is the pinned
#     message, and it never comes through here (push_links / pin_links);
#   * something has gone wrong and needs a human;
#   * the work is finished.
# Everything else - a turn ended, a verdict came back, a process is being
# waited on, a session started - is in the journal and on the panel, which
# is where you look when you are looking.
#
# This gate covers notify() only. Four other things reach Telegram on
# purpose and are not "notifications": the pinned links, the status message
# that is edited in place, the replies to commands you yourself typed, and
# the goodbye when the bridge stops.
TELEGRAM_KINDS = (
    # needs a human
    "needs_you", "crash", "session_died", "process_stuck", "rotation_name",
    "limit_low",
    # the work is over
    "run_finished",
)

# One colour per pair, so a glance at the phone says which project a message
# is about before a word of it is read. Telegram cannot colour a message -
# sendMessage has nothing for it, and parse_mode gives bold, italic and
# monospace but no colour - so the colour has to be a character in the text.
#
# Red is deliberately not in here. In this chat red means trouble, and if it
# were also the name of the third project then that project's ordinary
# messages would read as alarms and its alarms as ordinary.
PAIR_MARKS = ("\U0001F7E6", "\U0001F7E9", "\U0001F7E7", "\U0001F7EA")

# What the planner is told about context, in the seed it gets at
# SessionStart. Written because planners were doing the bridge's job and
# doing it badly: seeing an executor near the top of its window, calling a
# halt, and waiting for a replacement the bridge had not decided on and
# would have made itself at the right moment. The measuring is the
# bridge's, the rotation is the bridge's, and a full-looking context is not
# an event.
#
# The same paragraph is in channel.py's PLANNER_INSTRUCTIONS, spelled out
# again rather than imported: channel.py is spawned as its own process by
# the session and must not import this module. A session gets one of the
# two at a time, so both have to say it - and they arrive at different
# moments, which is worth knowing when only one of them seems to have
# taken: the channel's text comes with a NEW session, the seed only after
# the daemon has been restarted.
PLANNER_CONTEXT_RULE = (
    "How full anybody's context is, and what to do about it, is not your "
    "work. The bridge measures both halves - the window, where compaction "
    "fires, how much of the cycle is left - and it replaces a session "
    "itself when its own numbers say so, handing the replacement a written "
    "handoff it reads before its first turn. A session near the top of its "
    "window is a session working; one that compacts has summarised itself "
    "and carries on in the same window. So \"the executor is running out "
    "of context\" is not a reason to do anything: not a stop verdict, not "
    "a wait, not holding work back until somebody is replaced. Stopping "
    "the run for it costs the night and buys nothing, because the thing "
    "you are waiting for is the thing the bridge was already going to do. "
    "If you believe a rotation is genuinely needed sooner than the bridge "
    "would do it, say so to the human and let them decide - do not act as "
    "the bridge yourself.")


def pair_id(path):
    """A short, stable name for a pair, for places a path will not fit.

    Telegram allows 64 bytes of callback_data, and a Windows project path
    does not come close to fitting - so a button has to carry something
    shorter and then be resolved back. What it carries has to survive a
    restart and a project being removed and added again, which rules out an
    index into the config; and it has to be unique, which rules out the
    colour marker, since colours are reused once there are more pairs than
    the palette holds. A hash of the canonical path is both: eight hex
    characters, the same eight every time, derived from nothing but the
    path itself.
    """
    return "%08x" % (zlib.crc32(norm(path).encode("utf-8")) & 0xFFFFFFFF)


def path_of_pair_id(pid):
    """The project a button was about, or None if it is not one of ours."""
    for p in known_projects():
        if pair_id(p) == pid:
            return p
    return None


def known_projects():
    """Every project the bridge has a reason to know about, canonical."""
    out = set(norm(p) for p in CFG.get("projects", {}))
    out |= set(STATE.get("loops") or {})
    out |= {norm(s.get("path")) for s in STATE.get("sessions", {}).values()
            if s.get("path")}
    out.discard("")
    return sorted(out)


# message id in the chat -> the pair it was about, so that replying to a
# message addresses the command to that pair. It used to be the report body
# that you replied to; the report no longer goes to the chat (step 5), so
# the anchor is the alert that took its place. Bounded and in memory only -
# a reply to something from before a restart falls back to naming the pair.
MSGPROJ = {}
MSGPROJ_MAX = 200


def remember_message(mid, path):
    if not mid or not path:
        return
    MSGPROJ[int(mid)] = norm(path)
    if len(MSGPROJ) > MSGPROJ_MAX:
        for old in sorted(MSGPROJ)[:-MSGPROJ_MAX]:
            MSGPROJ.pop(old, None)


def mark_for(path):
    """The colour of this pair. Chosen once, then remembered.

    The slot comes from a hash of the canonical path so that a fresh config
    lands on a stable colour rather than on whatever order projects happen
    to be added in; a taken slot moves to the next free one, because two
    pairs sharing a colour defeats the point of having one. The answer is
    written to config.json and read from there afterwards - a colour that
    moved when a project was removed would be worse than no colour.
    """
    key = norm(path)
    if not key:
        return ""
    marks = CFG.setdefault("marks", {})
    if marks.get(key):
        return marks[key]
    taken = set(marks.values())
    start = zlib.crc32(key.encode("utf-8")) % len(PAIR_MARKS)
    pick = None
    for i in range(len(PAIR_MARKS)):
        cand = PAIR_MARKS[(start + i) % len(PAIR_MARKS)]
        if cand not in taken:
            pick = cand
            break
    if pick is None:
        # More pairs than colours. Say so once rather than hand out a
        # duplicate as though it were distinct.
        pick = PAIR_MARKS[start]
        store.journal("bridge", "More projects than pair colours, so %s now "
                      "shares %s with another project - the marker no longer "
                      "tells them apart" % (project_name(path), pick),
                      project_name(path), level="log")
    marks[key] = pick
    try:
        store.save_config(CFG)
    except Exception:
        pass
    return pick


CHAT_BRIEF = 160


def brief(text, limit=CHAT_BRIEF):
    """One line of something that may be enormous.

    A message that needs a human has to say enough to decide on and no
    more. What goes wrong without this is not verbosity, it is burial: a
    tracked command can be a heredoc writing a whole markdown file, and
    pasted into the chat it pushes the sentence that needed answering off
    the screen. The detail is on the panel and in the journal, both of
    which are made for reading.
    """
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def notify(kind, text, level=None, buttons=None, path=None):
    """Say something, and decide where it is allowed to be said.

    path names the pair this is about: it puts the pair's colour at the
    front of the chat message. Left out, the message is about the bridge
    rather than about one pair - the five-hour limit is the clearest case,
    since it belongs to the account and colouring it would blame a pair for
    something none of them did.
    """
    lvl = level or CFG.get("notify", {}).get(kind) or \
        ("sound" if kind in SOUND_DEFAULT else "silent")
    if kind not in TELEGRAM_KINDS:
        lvl = "log"
    if lvl == "sound" and presence_quiet():
        lvl = "silent"
    if buttons is None and kind in ("needs_you", "crash", "limit_low"):
        # "resume" only lifts a pause; it does nothing for a loop that was
        # stopped. Offering it under a message that says to start the loop
        # is a button that looks like the answer and is not.
        stopped = ((not (STATE.get("loops") or {}).get(norm(path), {})
                    .get("active")) if path else
                   any(not lp.get("active")
                       for lp in (STATE.get("loops") or {}).values()))
        buttons = (["start the loop", "pause", "status"] if stopped
                   else ["pause", "resume", "status"])
    if buttons and path:
        # The pair travels in the button's data, because a press arrives
        # with nothing else to say which message it came from. The label
        # stays as it was - it is what the human reads - and the id rides
        # behind it, which is why buttons are (label, data) here.
        pid = pair_id(path)
        buttons = [b if isinstance(b, (tuple, list))
                   else (b, "%s|%s" % (b, pid)) for b in buttons]
    if lvl != "log":
        mark = mark_for(path) if path else ""
        mid = telegram.send(CFG,
                            ("%s %s" % (mark, text)) if mark else text,
                            lvl, buttons)
        # Remember which pair this message was about, so that replying to it
        # in the chat addresses the answer to that pair without anything
        # having to be typed.
        remember_message(mid, path)
        h = telegram.health()
        if h.get("ok") is not None:
            telegram_note(h["ok"], h.get("why"), h.get("code"))
    return lvl


def best_session(path, role):
    """The freshest session of this role on this project that has telemetry.

    PreCompact and StopFailure can arrive from a session the status line has
    not described yet (right after a rotation); fall back to the project's
    last known numbers instead of calibrating a "?" model.
    """
    best = None
    for s in STATE.get("sessions", {}).values():
        if norm(s.get("path")) != norm(path) or s.get("role") != role:
            continue
        if not s.get("model") and not s.get("context_tokens"):
            continue
        if best is None or seen_at(s) > seen_at(best):
            best = s
    return best or {}


def nudge_loop_off(path, role, what):
    """The quietest failure in the whole bridge: both sessions alive, both
    busy, and the loop switched off between them - so reports go nowhere
    and the reviewer waits for something that will never arrive. Nothing
    is broken, so nothing else notices. Hence this."""
    path = norm(path)
    live = [s for s in (STATE.get("sessions") or {}).values()
            if norm(s.get("path")) == path
            and s.get("state") not in ("ended", "died", None)]
    if len(live) < 2:
        return                      # a lone session with no loop is normal
    with _lock:
        last = (STATE.get("loop_off_told") or {}).get(path, 0)
        if time.time() - last < 900:
            return
        STATE.setdefault("loop_off_told", {})[path] = time.time()
        save_state()
    name = project_name(path)
    store.journal("loop", "Loop is off - %s" % what, name, role, "log",
                  project_dir=path)
    # "loop_idle" is deliberately not in TELEGRAM_KINDS. Nothing is broken
    # here - both windows are alive and the loop is off because somebody
    # switched it off - so it belongs on the panel and in the journal,
    # where you look when you are looking, and not on the phone, where it
    # arrives as an alarm about a state you chose. Said once per project
    # per fifteen minutes even there, by the guard above.
    notify("loop_idle",
           "%s: %s because the loop is off. Both sessions are up, so "
           "nothing looks broken - but reports are not being carried and "
           "the planner is waiting for one. Press 'start the loop' in the "
           "panel to reconnect them." % (name, what), path=path)


def retire_sessions(path, role, keep_sid=None):
    """Mark a role's session records ended when its window is replaced.

    A window killed by the bridge never gets to fire SessionEnd, so its
    record used to sit in the state as a live session forever: a phantom
    context bar in the panel, and a phantom vote in every "are both halves
    up" check. Retiring it is what makes those honest.
    """
    with _lock:
        for key, sess in (STATE.get("sessions") or {}).items():
            if norm(sess.get("path")) != norm(path) or sess.get("role") != role:
                continue
            if keep_sid and sess.get("session_id") == keep_sid:
                continue
            if sess.get("state") not in ("ended", "died"):
                sess["state"] = "ended"
        save_state()


def prune_sessions(keep_per_role=1):
    """One record per role, because a role only runs once.

    Records are created from three directions - the SessionStart hook, a
    channel registering, and the periodic check that a port still answers -
    and each uses its own key. Ended ones were being cleared, but live
    duplicates never were, so the same session appeared two or three times
    under different names: twice in the panel, twice in every count of how
    many halves are up, twice in the plans. The newest record wins and the
    rest are retired.
    """
    with _lock:
        sess = STATE.get("sessions") or {}
        groups = {}
        for key, v in sess.items():
            groups.setdefault((norm(v.get("path")), v.get("role")),
                              []).append((seen_at(v), key))
        dropped = 0
        for rows in groups.values():
            rows.sort(reverse=True)
            for _, key in rows[keep_per_role:]:
                row = sess.get(key) or {}
                if row.get("state") in ("ended", "died"):
                    sess.pop(key, None)
                else:
                    row["state"] = "ended"
                dropped += 1
        if dropped:
            save_state()
        return dropped


def deactivate_loop(path, why, level="log"):
    """Every way the loop can switch off goes through here, so the journal
    always says which one it was. Not knowing that is what turns a stopped
    loop into an hour of staring at two idle windows."""
    path = norm(path)
    _, lp = loop_state(path)
    if not lp.get("active"):
        return
    lp["active"] = False
    with _lock:
        STATE.setdefault("loop_off", {})[path] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "why": why}
        save_state()
    store.journal("loop", "Loop stopped: %s" % why, project_name(path),
                  level=level, project_dir=path)


def loop_state(project_dir):
    path = norm(project_dir)
    with _lock:
        loops = STATE.setdefault("loops", {})
        if path not in loops:
            pconf = store.project_config(CFG, path)
            loops[path] = {"active": False, "iteration": 0,
                           }
        return path, loops[path]


# ---------------------------------------------------------------------------
# Pausing, and who it is about.
#
# There are two paused-nesses and they are not the same thing, which is why
# they are stored apart rather than sharing STATE["mode"]:
#
#   * the whole bridge is paused - you pressed pause with nothing selected,
#     or the five-hour limit ran out. The limit belongs to the account, so
#     one pair hitting it means every pair has hit it.
#   * one project is paused - you paused that pair, or a window of that pair
#     died. That says nothing at all about the other pairs, and it used to:
#     a dead executor in one folder set mode="paused" and quietly stopped
#     reviewing turns in every other folder on the machine.
#
# Both are read together everywhere a report might be held, and the reason
# is kept apart so the journal can say which of the two it was.

def paused_for(path):
    """Is work on this project on hold - by the bridge, or by itself?"""
    if STATE.get("mode") == "paused":
        return True
    return bool((STATE.get("paused") or {}).get(norm(path)))


def pause_project(path, why, by_death=False):
    """Hold one project. Never touches the other pairs.

    by_death marks a hold the bridge put on by itself because a window of
    this pair died, so the replacement registering can lift exactly that
    hold and leave a hold you put on by hand alone.
    """
    path = norm(path)
    with _lock:
        STATE.setdefault("paused", {})[path] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "why": why,
            "by_death": bool(by_death)}
        save_state()
    store.journal("command", "Paused %s: %s" % (project_name(path), why),
                  project_name(path), level="log", project_dir=path)


def resume_project(path):
    """Lift one project's own hold. A bridge-wide pause is not touched -
    lifting it is what /cmd resume without a project is for."""
    path = norm(path)
    with _lock:
        gone = (STATE.get("paused") or {}).pop(path, None)
        if gone:
            save_state()
    if gone:
        store.journal("command", "Resumed %s" % project_name(path),
                      project_name(path), level="log", project_dir=path)
    return bool(gone)


# ---------------------------------------------------------------------------
# The note.
#
# One line the human leaves for the pair, delivered with the next report and
# to the next session that starts. It used to be a single string for the
# whole bridge, so with two pairs it reached whichever project finished a
# turn first - and was wiped, so the pair it was written for never saw it.
# It is per project now, and take_note is the only way to read it, so
# "delivered once" stays true wherever it is delivered from.

def set_note(path, text):
    path = norm(path)
    with _lock:
        notes = STATE.setdefault("note", {})
        if text:
            notes[path] = text
        else:
            notes.pop(path, None)
        save_state()


def note_for(path):
    """The note left for this project, left where it is.

    isinstance guards the window between an old state.json being loaded and
    main() converting it - and any test that builds STATE by hand.
    """
    notes = STATE.get("note")
    if not isinstance(notes, dict):
        return ""
    return notes.get(norm(path)) or ""


def take_note(path):
    """The note left for this project, removed as it is handed over."""
    path = norm(path)
    with _lock:
        notes = STATE.get("note")
        if not isinstance(notes, dict):
            return ""
        text = notes.pop(path, "") or ""
        if text:
            save_state()
        return text


def save_state():
    with _lock:
        store.save_state(STATE)


def boot_time():
    """Epoch seconds when this machine last booted, or None."""
    try:
        if os.name == "nt":
            import ctypes
            ms = ctypes.windll.kernel32.GetTickCount64()
            return time.time() - ms / 1000.0
        with open("/proc/uptime") as fh:
            return time.time() - float(fh.read().split()[0])
    except Exception:
        return None


def fmt_reset(v):
    """resets_at arrives as an epoch or an ISO string - show HH:MM either way."""
    try:
        n = float(v)
        if n > 1e9:
            return time.strftime("%H:%M", time.localtime(n))
    except Exception:
        pass
    st = str(v or "")
    return st[11:16] if len(st) >= 16 and st[4:5] == "-" else st[:5]


# ---------------------------------------------------------------------------
# channels

def channel_for(path, role):
    ch = CHANNELS.get((norm(path), role))
    if ch and time.time() - ch["ts"] < 120:
        return ch
    return None


def port_answers(port, timeout=1.0):
    """Is anything listening on this loopback port?"""
    if not port:
        return False
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout):
            return True
    except Exception:
        return False


def channel_alive(path, role):
    """Live channel, or a remembered one whose port still answers.

    The in-memory registry empties whenever the bridge restarts, and the
    sessions only reappear on their next heartbeat - up to a minute during
    which every other witness is also blind: a window still sitting on its
    startup dialog has fired no hooks, and on Windows the pid we recorded
    belongs to the launcher rather than to claude itself. The channel's
    port is on disk and answers throughout, because the process listening
    on it is the session's own child.
    """
    ch = channel_for(path, role)
    if ch:
        return ch
    saved = (STATE.get("channels") or {}).get("%s|%s" % (norm(path), role))
    if saved and port_answers(saved.get("port")):
        return saved
    return None


def deliver_ex(path, role, content, meta):
    """Returns (ok, reason). reason is "absent" when no channel is
    registered at all, "failed" when one is and would not take the
    message. They look the same to the caller and are not the same thing:
    a session thinking for three minutes still has its channel, and
    opening another window for it is how six of them appear."""
    # Liveness and delivery used to consult different witnesses: every
    # "is it alive" check went through channel_alive, which accepts the port
    # remembered on disk, while this one took only the in-memory entry and
    # only while its heartbeat was under two minutes old. So the bridge
    # could say "its channel is answering" in the resume tab and refuse to
    # deliver in the same minute - which is exactly what the planner then
    # reported to Max as "the bridge is unreachable". One witness now.
    ch = channel_for(path, role) or channel_alive(path, role)
    if not ch or not ch.get("port"):
        return False, "absent"
    port = int(ch["port"])
    if not port_answers(port):
        store.journal("channel", "Nothing is listening on the %s channel's "
                      "port %d - the window is gone or its channel process "
                      "died while the window stayed open"
                      % (role, port), project_name(path), role, "sound",
                      project_dir=path)
        return False, "absent"
    # The rules ride in front of a task and in front of a report, and this
    # is the one place both pass through - so no caller can forget, and a
    # queued message re-delivered later carries them too. PENDING keeps the
    # clean text, which is what the residence gate reads.
    # Which SESSION this is going to, so the full canon is spent once per
    # window and a handover earns it again.
    content = with_rules(content, meta, last_session_id(path, role))
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/" % port,
            data=json.dumps({"content": content, "meta": meta}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Bridge-Secret": SECRET})
        urllib.request.urlopen(req, timeout=20).read()
        # The port answered, so whatever the heartbeat says, this channel is
        # current. Put it back in the registry rather than leaving the two
        # views to disagree again.
        CHANNELS[(norm(path), role)] = {"port": port, "ts": time.time(),
                                        "pid": ch.get("pid")}
        return True, "ok"
    except Exception as exc:
        store.journal("channel", "The %s channel is listening on port %d but "
                      "would not take the message: %s. The window is up; it "
                      "is the channel inside it that did not answer."
                      % (role, port, exc), project_name(path), role, "sound",
                      project_dir=path)
        return False, "failed"


def deliver(path, role, content, meta):
    return deliver_ex(path, role, content, meta)[0]


def ensure_session(path, role, why="a session was needed"):
    up = already_up(path, role)
    if up:
        store.journal("session", "Not opening a %s window - %s"
                      % (role, up), project_name(path), role, "log",
                      project_dir=path)
        return
    """Open a window for a missing session - once.

    The attempt counter is checked before the timer on purpose: the timer
    only spaces attempts out, and spacing an endless series out by three
    minutes is what fills a screen with dialogs overnight. One window, then
    it is a question for the human.
    """
    key = (norm(path), role)
    skey = key[0] + "|" + role
    with _lock:
        tried = (STATE.get("autostart_tried") or {}).get(skey, 0)
    if tried >= 1:
        store.journal("session", "A %s window was already opened for this "
                      "and never came up - waiting for you instead" % role,
                      project_name(path), role, "log", project_dir=path)
        with _lock:
            last = (STATE.get("autostart_told") or {}).get(skey, 0)
            quiet = time.time() - last < 900
            if not quiet:
                STATE.setdefault("autostart_told", {})[skey] = time.time()
                save_state()
        if not quiet:
            notify("needs_you",
                   "%s: the %s is still not reachable and a window has "
                   "already been opened for it. Opening more would only "
                   "stack up unanswered dialogs - look at that window."
                   % (project_name(path), role), path=path)
        return
    stop_reason = launch_guard(path, role)
    if stop_reason:
        store.journal("session", "Not opening a %s window: %s"
                      % (role, stop_reason), project_name(path), role, "log",
                      project_dir=path)
        return
    if time.time() - LAUNCHED.get(key, 0) < 180:
        return
    with _lock:
        STATE.setdefault("autostart_tried", {})[skey] = tried + 1
        save_state()
    LAUNCHED[key] = time.time()
    note_launch(path, role, why)
    pconf = store.project_config(CFG, path)
    chain = pconf["chains"].get(role) or []
    req = chain[0] if chain else None
    try:
        pid = sessions.launch(path, role,
                              model=models.resolve(req, store.load_models()),
                              permission_mode=mode_for(path, role),
                              disallow=disallow_for(path, role),
                              autocompact_pct=compact_pct(path))
        # ...and write down that it was passed. Five of the six launch paths
        # recorded it; this one did not, so a window the bridge opened itself
        # reported its compaction point as unknown ever after - which is how
        # a planner the bridge had configured came to look like somebody
        # else's window.
        reg_pid(path, role, pid, model_req=req,
                autocompact=compact_pct(path))
        notify("needs_you",
               "%s: starting the %s window. It opens on the "
               "development-channels dialog - press 1 in it once and the "
               "session starts." % (project_name(path), role), path=path)
    except Exception as exc:
        notify("crash", "%s: could not start the %s: %s"
               % (project_name(path), role, exc), path=path)


# ---------------------------------------------------------------------------
# context accounting and calibration

# What the conversation occupies is the input context of the last request:
# fresh input, plus what was written to the cache, plus what was read from
# it. Output is not in it - it becomes part of the *next* request, not this
# one.
#
# This used to sum every key whose name merely contained "token". That
# picked up output_tokens, and it picks up any breakdown the client adds
# later - a nested cache_creation split into ephemeral_5m / ephemeral_1h
# counts the same tokens a second time. The result read larger than the
# window holding it, which is impossible, and the bridge then computed a
# wall below that reading and ordered a session's replacement from it.
#
# One definition, in store, because this same number is read here from the
# status line, in sessions.py from a transcript and in archive.py from an
# archived copy - and two of those three used to disagree.
INPUT_TOKEN_FIELDS = store.CARRIED_CONTEXT_FIELDS


def _tokens(cw):
    cu = cw.get("current_usage") or {}
    used = sum(int(cu.get(k) or 0) for k in INPUT_TOKEN_FIELDS
               if isinstance(cu.get(k), (int, float)))
    if not used and cu:
        # An unfamiliar shape. Sum what is plainly input rather than
        # everything, and never anything named as output.
        used = sum(v for k, v in cu.items()
                   if isinstance(v, (int, float)) and "token" in k
                   and "output" not in k)
    if not used:
        used = cw.get("total_input_tokens") or 0
    return used or None


def context_check(sess, path):
    """Headroom maths at each status tick. Returns dict for the panel."""
    window = sess.get("window") or 0
    used_t = sess.get("context_tokens") or 0
    model = (sess.get("model") or "?").lower()
    if not window:
        return {}
    cal = store.calib_get(model, path, window)
    ceiling_t = window * cal["ceiling_pct"] / 100.0
    costs = sess.get("turn_costs") or []
    reserve = max(costs[-5:] or [0]) * cal.get("multiplier", 1.5)
    reserve = max(reserve, 15000)
    headroom = ceiling_t - used_t - reserve
    return {"ceiling_pct": cal["ceiling_pct"], "reserve": int(reserve),
            "headroom": int(headroom),
            "wall": cal.get("wall_history_tokens")}


def calib_miss(model, path, at_pct, how):
    cal = store.calib_get(model, path, 0)
    new_ceiling = max(40.0, (at_pct or cal["ceiling_pct"]) - 3.0)
    store.calib_update(model, path,
                       ceiling_pct=round(min(cal["ceiling_pct"],
                                             new_ceiling), 1),
                       multiplier=min(3.0, cal.get("multiplier", 1.5) + 0.25),
                       misses=cal.get("misses", 0) + 1,
                       clean_streak=0,
                       measured_at=time.strftime("%Y-%m-%d %H:%M"), how=how)


def calib_clean(model, path):
    cal = store.calib_get(model, path, 0)
    streak = cal.get("clean_streak", 0) + 1
    fields = {"clean_streak": streak}
    if streak and streak % 10 == 0:
        fields["ceiling_pct"] = round(min(cal["ceiling_pct"] + 1.0, 97.0), 1)
    store.calib_update(model, path, **fields)


# Claude Code holds part of the window back so that compaction - which is
# one request carrying the whole conversation plus the summariser's prompt
# plus room for the summary - can still fit. That reserve used to be 45k and
# is about 33k on current versions. Past it, the conversation is too long to
# continue and too long to compact: the deadlock people hit as "Conversation
# too long", reported at ~163k on a 200k window, which is this line exactly.
RESERVED_TOKENS = 33000

# ...but that 33 000 is the figure for a 200 000 window - 16.5 % of it - and
# the client does not hold back a fixed number. On a 1 M window it has been
# reported computing about 23 %, roughly 233 k, which puts the wall some
# 200 k below `window - 33 000`. Neither figure has been measured on this
# machine, so both are shown, the gap between them is stated, and no
# decision is taken from either. A real "Conversation too long" is what
# measures it, and that measurement replaces both.
RESERVE_FRACTION_LARGE = 0.23


def wall_view(sess, path):
    """Two points ahead of a session, and the gap between them.

    compaction - where the session summarises itself and carries on. The
    bridge sets this threshold, so it is known rather than discovered.
    wall - where compaction can no longer run at all, because the request
    that performs it would not fit. That is the window minus the reserve,
    and it is computable; a wall actually hit here replaces the estimate.

    The gap between them is the part worth watching: one huge tool result
    can cross it in a single step, which is how a session goes from healthy
    to unrecoverable without passing through a warning.
    """
    used = sess.get("context_tokens") or 0
    window, wsrc = known_window(path, sess.get("role"), sess)
    if not window:
        return {"used": int(used), "window": None,
                "why_blank": wsrc or "nothing read from this session yet"}
    model = (sess.get("model") or "?").lower()
    cal = store.calib_get(model, path, window)

    measured_wall = cal.get("wall_history_tokens")
    wall = int(measured_wall or max(0, window - RESERVED_TOKENS))
    wall_src = "measured here" if measured_wall else \
        "window minus the %dk compaction reserve" % (RESERVED_TOKENS // 1000)

    # The other end of the same uncertainty: if the reserve really is a
    # proportion, the wall on a large window is a long way below the
    # constant-reserve figure. Both are carried so the panel can show the
    # gap instead of picking one and calling it the answer.
    wall_low = None
    if not measured_wall and window > 200000:
        wall_low = int(window * (1.0 - RESERVE_FRACTION_LARGE))

    # Where compaction fires is the one number that has to come from this
    # machine. Published figures for a 1M window disagree wildly between
    # client versions - a hardcoded ~76k, 400k after one release, the full
    # window before it, ~83% in the guides - and the launch override is
    # reported as unreliable. So: an observed firing outranks the threshold
    # the bridge asked for, and both are labelled with where they came from.
    # This session's own compactions first: they are keyed to it and cannot
    # be invalidated by anything else's bookkeeping.
    own = compaction_sizes(path, sess.get("role"))
    measured_compact = min(own) if own else None
    if not measured_compact:
        measured_compact = cal.get("compact_at_tokens")
        if measured_compact and cal.get("compact_at_window") not in (None,
                                                                    window):
            measured_compact = None   # measured on a different window size
    # A session sitting above the size it was last seen compacting at is not
    # evidence that the number is stale. The recorded value is an overshoot -
    # where a turn ended, above the threshold - so being past it means the
    # threshold is at or below there and a compaction is due at the next turn
    # boundary. That is the most useful thing the bridge can know, and it
    # used to be thrown away here: the sample was discarded, the fallback was
    # empty, and a session that had demonstrably compacted was reported as
    # having no known compaction point at all.
    pct = applied_compact_pct(path, sess.get("role"))
    compact = int(measured_compact or (window * pct / 100.0 if pct else 0))
    compact_src = ("at or below %dk - this session was seen compacting there, "
                   "after a turn that had already run past it"
                   % (measured_compact // 1000)) if measured_compact else (
        "set by the bridge at %d%%" % pct if pct
        else "unknown - this window was not started with a threshold")

    costs = [c for c in (sess.get("turn_costs") or []) if c > 0]
    per_turn = int(sum(costs) / len(costs)) if costs else 0
    worst_turn = max(costs) if costs else 0

    limit = compact if compact else wall
    kind = "compaction" if compact else "wall"
    out = {"used": int(used), "window": int(window),
           "window_source": wsrc, "assumed": wsrc.startswith("assumed"),
           "wall": wall, "wall_source": wall_src,
           "wall_low": wall_low, "wall_measured": bool(measured_wall),
           "reserve_low": (int(window * RESERVE_FRACTION_LARGE)
                           if wall_low else None),
           "room_to_wall": max(0, wall - used),
           "room_to_wall_low": (max(0, wall_low - used) if wall_low else None),
           "compact": compact or None, "compact_source": compact_src,
           "compact_due": bool(compact and used >= compact),
           # Rule 8: without the compaction point there is no way to know
           # whether a compaction fires before the wall does, so the distance
           # to the wall is reported and never dressed as an alarm.
           "interception_unknown": not compact,
           "reserved": RESERVED_TOKENS,
           "gap": max(0, wall - compact) if compact else None,
           "per_turn": per_turn, "worst_turn": worst_turn,
           "limit": limit, "kind": kind,
           "left": int(max(0, limit - used)),
           "pct_of_limit": round(used * 100.0 / limit, 1) if limit else 0,
           "handover_at": CFG["thresholds"].get("handover_at", 90)}
    stale = stale_after_compaction(sess, used)
    if stale:
        out["stale_reading"] = stale
    if per_turn:
        out["turns_left"] = max(0, int((limit - used) / per_turn))
    return out


# How long a size from before a compaction is treated as "the reading has
# not caught up yet" rather than "the compaction did not free anything".
# Past this the session has had every chance to draw itself and has not, so
# the number is taken at face value again - a compaction that genuinely
# freed nothing is a real emergency and must not be silenced forever.
STALE_READING_GRACE = 1800


def stale_after_compaction(sess, used):
    """Is this size from before the last compaction?

    A compaction empties most of the window, but the number the bridge holds
    only changes when the session next draws its status line. In between,
    the record says the session is carrying everything it was carrying
    before the summary - a conversation that no longer exists. Decisions
    made from it are made about the wrong session, and the one they reach
    for is 'you are past the wall, hand over', at the exact moment the
    session has more room than it has had all run.
    """
    pend = (sess or {}).get("compaction_pending") or {}
    was = int(pend.get("tokens") or 0)
    if not was or not used:
        return None
    if used < was * 0.9:
        return None                       # a smaller size arrived: caught up
    age = time.time() - float(pend.get("at") or 0)
    if age > STALE_READING_GRACE:
        return None                       # long enough; take it at face value
    return {"was": was, "age": int(age)}


# Claude Code runs on one of two context windows: the standard 200k, or the
# million-token variant. There is nothing in between, which is what makes
# the size of a conversation evidence about the window holding it.
KNOWN_WINDOWS = (200000, 1000000)


def known_window(path, role, sess=None):
    """The context window of this session, and how confident that is.

    Returns (tokens, source). Three ways to know, strongest first:

    observed  - the status line states it outright, once the session draws
                itself. Only trusted while it can still hold what is in it;
                a window smaller than the conversation is a stale record.
    launched  - the alias this window was opened with. A [1m] means a
                million whatever the transcript calls the model, because
                the transcript records claude-opus-5 either way.
    deduced   - from the size itself. A conversation of 816k cannot live in
                a 200k window, so the window is a million and there is no
                other candidate. Below 200k the two are indistinguishable,
                so that case is marked as an assumption and never acted on.
    """
    used = 0
    for src in ((sess or {}), last_telemetry(path, role) or {}):
        used = max(used, int(src.get("context_tokens") or 0))
    for src in ((sess or {}), last_telemetry(path, role) or {}):
        w = src.get("window")
        if w and int(w) >= used and src.get("window_observed", True):
            return int(w), "observed"
    # A window the status line once stated does not stop being true when the
    # status line goes quiet. Reading the transcript marks the record
    # window_observed=False, which skipped the branch above and dropped the
    # answer through to the launch alias - so the same planner was shown as
    # "163k of 200k, 82%" one minute and "160k of 1.0M, 16%" the next,
    # depending on which source had written last. That flap also invalidated
    # its measured compaction point, which is keyed to the window size.
    seen = (STATE.get("windows") or {}).get("%s|%s" % (norm(path), role)) or {}
    if seen.get("tokens") and int(seen["tokens"]) >= used:
        return int(seen["tokens"]), "observed earlier by the status line"
    req = (STATE.get("pids") or {}).get("%s|%s" % (norm(path), role)) or {}
    asked = (req.get("model_req") or "") if isinstance(req, dict) else ""
    if "1m" in asked.lower():
        # The sanity rule applies here too: a conversation cannot be larger
        # than the window holding it. The observed source above is already
        # checked; this one used to answer regardless, so a window that had
        # just been rejected as too small came straight back in through the
        # alias - and every distance measured from it was nonsense.
        if not used or used <= 1000000:
            return 1000000, "launched with a 1M alias"
        return None, ("%dk is more than the 1M this window was launched with "
                      "- one of the two numbers is wrong, so neither is used"
                      % (used // 1000))
    if used:
        for w in KNOWN_WINDOWS:
            if w >= used:
                certain = used > KNOWN_WINDOWS[0]
                return w, ("deduced from %dk in it" % (used // 1000)
                           if certain else "assumed - %dk fits either window"
                           % (used // 1000))
    return None, None


def refresh_from_disk(path, role, sess=None):
    """Bring a session's numbers up to date without asking the session.

    A window that is idle, or sitting on a startup dialog, draws no status
    line and so reports nothing - which used to leave the bridge with a
    blank where a decision should be. Its transcript is on disk and current,
    so the size is readable at any moment. This is what lets the bridge
    judge a session that is not talking to it.
    """
    sid = last_session_id(path, role) or (sess or {}).get("session_id")
    if not sid:
        # Without the session id there is no way to tell this role's
        # transcript from the other one's, and the newest file in the folder
        # is as likely to be the planner's as the executor's. Reporting the
        # wrong session's size would poison every decision made from it, so
        # the honest answer here is nothing at all.
        return None
    tp = sessions.transcript_of(sid)
    usage = sessions.usage_from_transcript(tp) if tp else None
    if not usage:
        return None
    old = last_telemetry(path, role) or {}
    tokens = usage["context_tokens"]
    probe = dict(sess or {})
    probe["context_tokens"] = tokens
    window, wsrc = known_window(path, role, probe)
    # Only a status line observes a window. What the transcript gives is the
    # size; saying "observed=False" about a window that WAS observed is what
    # made the panel flip between 200k and 1M.
    seen = (STATE.get("windows") or {}).get("%s|%s" % (norm(path), role)) or {}
    if seen.get("tokens"):
        window, wsrc = int(seen["tokens"]), "observed earlier by the status line"
    fresh = {"window": window, "window_observed": bool(seen.get("tokens")),
             "window_source": wsrc,
             "model": old.get("model") or usage.get("model"),
             "model_id": usage.get("model"), "context_tokens": tokens,
             "context_pct": (round(tokens * 100.0 / window, 1)
                             if window else None),
             "turn_costs": old.get("turn_costs") or [],
             "at": time.strftime("%H:%M:%S"), "epoch": time.time(),
             "from": "transcript"}
    with _lock:
        STATE.setdefault("telemetry", {})["%s|%s" % (norm(path), role)] = fresh
        for sv in (STATE.get("sessions") or {}).values():
            if norm(sv.get("path")) == norm(path) and sv.get("role") == role \
                    and sv.get("state") not in ("ended", "died"):
                if window:
                    sv["window"] = window
                sv["context_tokens"] = tokens
                sv["context_pct"] = fresh["context_pct"]
                sv["model"] = sv.get("model") or fresh["model"]
                sv["stale"] = None
                sv["read_from"] = "transcript at %s" % fresh["at"]
                # This is the reading that ends a compaction's blind spot for
                # a window that is not drawing its status line: the transcript
                # is on disk and already carries the summarised size.
                pend = sv.get("compaction_pending") or {}
                if pend.get("tokens") and tokens < int(pend["tokens"]) * 0.9:
                    sv.pop("compaction_pending", None)
        save_state()
    return fresh


def ensure_record(path, role, why):
    """Make sure a session proved alive has a record to be seen through.

    Records were only ever created by hooks and by a channel registering.
    A window that answers its port but has fired neither - because it is
    sitting on a startup dialog, or because the bridge restarted after it
    registered - was known to be alive by every check and still invisible
    on the panel. The record is what the panel, the headline and the
    planning all read, so anything that proves life has to write one.
    """
    path = norm(path)
    with _lock:
        for v in (STATE.get("sessions") or {}).values():
            if norm(v.get("path")) == path and v.get("role") == role \
                    and v.get("state") not in ("ended", "died"):
                return False
        old = last_telemetry(path, role) or {}
        # The key carries the project. It was "<role>:seen", which is the
        # same key for every project - so with two pairs each holding a
        # noticed window, whichever was written last erased the other, the
        # erased one was noticed again on the next pass, and the two took
        # turns overwriting each other for ever: a pair of "adding it to
        # the panel" lines in the feed every 45 seconds, which is exactly
        # how often reconcile() runs.
        STATE.setdefault("sessions", {})["%s:seen:%s" % (role,
                                                         pair_id(path))] = {
            "role": role, "path": path, "project": project_name(path),
            "state": "idle", "first_seen": now(), "last_seen": now(),
            "seen_at": time.time(),
            "session_id": last_session_id(path, role) or "",
            "window": old.get("window"), "model": old.get("model"),
            "context_tokens": old.get("context_tokens"),
            "context_pct": old.get("context_pct"),
            "turn_costs": old.get("turn_costs") or [],
            "stale": old.get("at"), "seen_by": why}
        save_state()
    prune_sessions()
    store.journal("session", "Noticed a live %s window (%s) - adding it to "
                  "the panel" % (role, why), project_name(path), role, "log",
                  project_dir=path)
    return True


def reconcile():
    """Make what the panel shows agree with what is actually running."""
    paths = set(norm(p) for p in CFG.get("projects", {}))
    paths |= {norm(v.get("path")) for v in (STATE.get("sessions") or {}).values()
              if v.get("path")}
    paths |= {k.rpartition("|")[0] for k in (STATE.get("channels") or {})}
    for path in paths:
        if not path:
            continue
        expire_handover(path)
        for role in ("executor", "planner"):
            why = already_up(path, role)
            if why:
                ensure_record(path, role, why)
                refresh_from_disk(path, role)


def acted_recently(path, tag, gap=900):
    key = "acted:%s:%s" % (tag, path)
    with _lock:
        if time.time() - (STATE.get(key) or 0) < gap:
            return True
        STATE[key] = time.time()
        save_state()
    return False


def ask_planner_about(path, question_tail, ex):
    """Hand the executor's question to the planner, with the numbers.

    The bridge decides mechanical things - is a process running, has a
    verdict gone out. Whether a question can be answered without the human
    is a judgement about the work, and the planner is the half that holds
    the project. So it is asked, not guessed at.
    """
    allowed = store.project_config(CFG, path).get(
        "planner_answers_questions", True)
    name = project_name(path)
    said = "\n\n".join("%s: %s" % (r["who"], r["text"])
                        for r in question_tail[-3:])
    if not allowed:
        notify("needs_you", "%s: the executor is waiting on an answer:\n\n%s"
               % (name, said[-1200:]), path=path)
        return "asked the human"
    body = ("The executor has stopped and appears to be waiting for an "
            "answer. Its last exchange:\n\n%s\n\nDecide which this is. If "
            "it is a technical question you can answer from what you know "
            "of this project, answer it with the task tool and the work "
            "continues. If it picks a direction, changes what we are "
            "building, or cannot be undone, do not answer - say so, and the "
            "human will be called. Answer or decline; do not leave it."
            % said[-3000:])
    if deliver(path, "planner", body, {"kind": "info"}):
        store.journal("loop", "Asked the planner whether it can answer the "
                      "executor's question", name, "planner", "log",
                      project_dir=path)
        return "asked the planner"
    notify("needs_you", "%s: the executor is waiting on an answer and the "
           "planner is not reachable:\n\n%s" % (name, said[-1200:]), path=path)
    return "asked the human"


# How many times the bridge may ask the planner for a task, and get
# nothing back, before it concludes the planner's tool path is broken. Two
# is enough: the ask is only sent after eight minutes of silence, so two of
# them is a quarter of an hour of a planner that hears the bridge and
# cannot answer it.
ASKS_BEFORE_BROKEN = 2


def note_ask(path):
    """The bridge asked the planner for work. Count it."""
    with _lock:
        book = STATE.setdefault("asks", {})
        book[norm(path)] = int(book.get(norm(path), 0)) + 1
        save_state()
    return (STATE.get("asks") or {}).get(norm(path), 0)


def note_task_arrived(path):
    """A task came through the tool, so that path is alive."""
    # Real work: the pair is not idling, and a held hook is released.
    clear_spin(path)
    wake_idle(path)
    with _lock:
        if (STATE.get("asks") or {}).pop(norm(path), None) is not None:
            save_state()
        if (STATE.get("toolbroken") or {}).pop(norm(path), None) is not None:
            store.journal("channel", "The planner's task tool is answering "
                          "again.", project_name(path), "planner", "log",
                          project_dir=path)
            save_state()


def tool_path_broken(path):
    """Asked repeatedly, heard nothing, while the planner is plainly there.

    This is the failure that cost a whole evening: the planner receives
    everything the bridge sends, answers verdicts, and its own task calls
    never arrive - the break is inside its window, between Claude Code and
    the MCP process, and nothing on this side can reach it. What this side
    can do is notice, say so exactly, and move the work by a path that is
    demonstrably working.
    """
    asks = int((STATE.get("asks") or {}).get(norm(path), 0))
    if asks < ASKS_BEFORE_BROKEN:
        return False
    if not channel_alive(path, "planner"):
        return False        # it is simply not there; a different problem
    if (STATE.get("toolbroken") or {}).get(norm(path)):
        return True
    with _lock:
        STATE.setdefault("toolbroken", {})[norm(path)] = time.strftime(
            "%Y-%m-%d %H:%M:%S")
        save_state()
    store.journal("channel", "The planner has been asked for a task %d times "
                  "and none arrived, while its channel keeps answering. The "
                  "break is between its window and its own MCP process - "
                  "nothing on this side can reach it. Working round it by "
                  "verdict, which is the path that still carries."
                  % asks, project_name(path), "planner", "sound",
                  project_dir=path)
    notify("needs_you",
           "%s: the planner can hear the bridge but its task tool is not "
           "reaching it - %d asks, no answer. In the planner's window type "
           "/mcp and reconnect 'bridge', or restart that window. Meanwhile "
           "the bridge is asking it to send work as a verdict instead, which "
           "still works, so the loop keeps moving."
           % (project_name(path), asks), path=path)
    return True


def assess(path):
    """Look at everything, decide once, and say what was decided.

    The order matters more than any single rule: the first thing that is
    true wins, so a running build is never mistaken for a stall and a stall
    is never mistaken for a question.
    """
    expire_handover(path)
    sit = situation(path)
    ex = sit["roles"]["executor"]
    pl = sit["roles"]["planner"]
    quiet = float(store.project_config(CFG, path).get(
        "silence_minutes", 8)) * 60
    name = project_name(path)

    def done(what, action=""):
        with _lock:
            STATE.setdefault("assessed", {})[path] = {
                "at": time.strftime("%H:%M:%S"), "saw": what,
                "did": action or "nothing"}
            save_state()
        if action:
            store.journal("loop", "Saw %s - %s" % (what, action), name,
                          "executor", "log", project_dir=path)
        return {"saw": what, "did": action or "nothing"}

    if not ex["alive"] and not pl["alive"]:
        return done("no sessions running")
    if sit["paused"]:
        return done("the loop is paused")
    if sit["inflight"] or looks_busy(ex["tail"]):
        return done("something is still running for the executor")
    if sit["reviewing"]:
        return done("the planner is reviewing a report")
    if sit["verdict_in_flight"]:
        return done("a verdict is on its way to the executor")
    if sit["handover"]:
        return done("a handover is under way")

    silent = ex["silent_for"]
    if silent is None or silent < quiet:
        return done("the executor answered recently")

    plan = ex["plan"]
    sess = ex["sess"]

    # a turn cut for a handover the numbers no longer call for
    if was_cut_for_handover(path, ex["tail"]) \
            and plan["do"] in ("working", "compacting"):
        if acted_recently(path, "cancel"):
            return done("a handover was cancelled recently")
        with _lock:
            STATE.pop("cut_for_handover:%s" % path, None)
            save_state()
        deliver(path, "executor",
                state_report(path, "executor", sess,
                             "you were stopped for a handover; it is "
                             "cancelled",
                             "carry on with the work you had in hand and "
                             "finish your turn as usual."),
                {"kind": "task"})
        return done("a handover that the numbers no longer call for",
                    "cancelled it and told the executor to carry on")

    if plan["do"] == "handover":
        log_handover_decision(path, "executor", sess, plan)
        blocked = handover_blocked(path, ("executor",))
        if blocked:
            if not acted_recently(path, "blocked", 3600):
                notify("needs_you", "%s: a handover is due but cannot run - "
                       "%s" % (name, blocked), path=path)
            return done("a handover due but blocked", "told you")
        threading.Thread(target=handover,
                         args=(path, plan["why"], ("executor",)),
                         daemon=True).start()
        return done("the executor at the end of its runway",
                    "handing over the executor to a fresh session")

    # The planner has its own context, its own compactions and its own end
    # of runway, and it reaches them at its own pace - it reads reports
    # while the executor writes them. It is replaced when its own numbers
    # say so, on its own, and never as a passenger on the executor's
    # handover.
    if pl["alive"] and pl["plan"].get("do") == "handover" \
            and not sit["reviewing"] and not sit["verdict_in_flight"]:
        if acted_recently(path, "planner_handover", 3600):
            return done("the planner at the end of its runway",
                        "already handed over recently")
        log_handover_decision(path, "planner", pl["sess"], pl["plan"])
        blocked = handover_blocked(path, ("planner",))
        if blocked:
            if not acted_recently(path, "planner_blocked", 3600):
                notify("needs_you", "%s: the planner is at the end of its "
                       "runway but cannot be replaced - %s" % (name, blocked), path=path)
            return done("a planner handover due but blocked", "told you")
        threading.Thread(target=handover,
                         args=(path, pl["plan"]["why"], ("planner",)),
                         daemon=True).start()
        return done("the planner at the end of its runway",
                    "handing over the planner to a fresh session")

    if waiting_for_direction(ex["tail"]):
        if acted_recently(path, "direction"):
            return done("an executor between pieces, already passed on")
        said = "\n\n".join("%s: %s" % (r["who"], r["text"])
                            for r in ex["tail"][-2:])
        asks = note_ask(path)
        broken = tool_path_broken(path)
        if broken:
            body = ("The executor has finished what it had and stopped, "
                    "waiting for the next piece. What it last said:\n\n%s"
                    "\n\nYour task tool is not reaching the bridge - %d "
                    "asks have arrived here with no task behind them, while "
                    "everything the bridge sends you is clearly getting "
                    "through. Do not use the task tool for this one. Answer "
                    "with the verdict tool instead: verdict 'continue', and "
                    "put the whole instruction in the feedback - that path "
                    "is working and the executor will receive it. Tell Max "
                    "in one line that the task tool needs /mcp reconnect in "
                    "this window." % (said[-3000:], asks))
        else:
            body = ("The executor has finished what it had and stopped, "
                    "waiting for the next piece. What it last said:\n\n%s"
                    "\n\nGive it the next task with the task tool, or say "
                    "the work is done and the loop should stop."
                    % said[-3000:])
        if deliver(path, "planner", body, {"kind": "info"}):
            return done("the executor between pieces",
                        "asked the planner for the next task")
        notify("needs_you", "%s: the executor has finished its piece and is "
               "waiting for the next one; the planner is not reachable."
               % name, path=path)
        return done("the executor between pieces", "told you")

    if looks_like_a_question(ex["tail"]):
        if acted_recently(path, "question"):
            return done("an unanswered question, already passed on")
        return done("the executor waiting on an answer",
                    ask_planner_about(path, ex["tail"], ex))

    if not sit["loop"]:
        return done("the loop off with sessions up")

    if acted_recently(path, "nudge"):
        return done("an idle executor, already told")
    deliver(path, "executor",
            state_report(path, "executor", sess,
                         "you have been idle and nothing is waiting on you",
                         "if you have work in hand, finish your turn so it "
                         "can be reviewed. If you are between pieces and "
                         "waiting for direction, say so in one line - that "
                         "answer goes to the planner, which will give you "
                         "the next task."),
            {"kind": "task"})
    return done("an idle executor with the loop on", "sent it its state")


def idle_watch():
    """One considered decision a minute, per project."""
    while True:
        time.sleep(60)
        paths = set(norm(p) for p in CFG.get("projects", {}))
        paths |= {norm(v.get("path"))
                  for v in (STATE.get("sessions") or {}).values()
                  if v.get("path")}
        for path in paths:
            if not path:
                continue
            try:
                assess(path)
            except Exception:
                # a watcher that swallows its own errors is indistinguishable
                # from one that decided to do nothing, which is the worst
                # thing for something whose whole job is to explain itself
                tb = traceback.format_exc().strip().splitlines()[-1][:180]
                with _lock:
                    STATE.setdefault("assessed", {})[path] = {
                        "at": time.strftime("%H:%M:%S"),
                        "saw": "an error while looking", "did": tb}
                    save_state()
                store.journal("bridge_error", "Assessment of %s failed: %s"
                              % (project_name(path), tb), project_name(path),
                              level="log", project_dir=path)


def disk_watch():
    """Keep every live session's size current, whether or not it talks."""
    while True:
        try:
            reconcile()
        except Exception:
            pass
        time.sleep(45)


# There is no default floor any more. A typical figure was carried here for
# a long time - a third survives, two thirds come back - and it was used for
# every cycle a session had left, which quietly asserted that cycles never
# get shorter. They do: the floor climbs every compaction, because each
# summary has to cover the last one. So the floor is measured or it is
# absent, and while it is absent nothing is projected from it.


def floors(path, role):
    """Every measured floor of the session running now, oldest first.

    A floor is what a compaction left behind, in tokens. The pair is
    recorded per compaction: the size carried into it, and the size after.
    Only this session's own compactions count - a predecessor's floors
    describe a conversation this one never had.
    """
    hist = (STATE.get("compactions") or {}).get(
        "%s|%s" % (norm(path), role)) or []
    sid = last_session_id(path, role) or ""
    if not sid:
        return []
    return [{"before": int(h["tokens"]), "after": int(h["after"]),
             "frac": h["after"] / float(h["tokens"])}
            for h in hist
            if h.get("after") and h.get("tokens")
            and (h.get("session") or "") == sid]


def compaction_sizes(path, role):
    """The sizes this session was seen compacting at, oldest first.

    Straight from the session's own PreCompact records, which are keyed by
    session id and by nothing else. The calibration file also holds a
    compaction point, but it is keyed by model and project and stamped with
    the window size it was measured on - so when the window reading flapped
    between 200k and 1M, that stamp stopped matching and a session with a
    perfectly good measurement of its own was reported as having no known
    compaction point, and with it no cycle and no distance to the wall.
    """
    hist = (STATE.get("compactions") or {}).get(
        "%s|%s" % (norm(path), role)) or []
    sid = last_session_id(path, role) or ""
    if not sid:
        return []
    return [int(h["tokens"]) for h in hist
            if h.get("tokens") and (h.get("session") or "") == sid]


def last_floor(path, role):
    """Where the most recent compaction left the session, in tokens.

    The most recent, never the average. Compaction summarises everything in
    the window, which after the first one already includes the previous
    summary - so each summary is longer than the last and the floor climbs
    every cycle. Averaging 0.30, 0.38 and 0.45 gives 0.38 and hides the one
    thing worth knowing: that it is rising.
    """
    fl = floors(path, role)
    return fl[-1]["after"] if fl else None


def floor_rise(path, role):
    """How much the floor climbs per cycle, if it has been seen twice."""
    fl = floors(path, role)
    if len(fl) < 2:
        return None
    steps = [fl[i]["after"] - fl[i - 1]["after"] for i in range(1, len(fl))]
    return int(sum(steps) / len(steps))


# The wall this bridge acts on: the fifth compaction. Agreed with Max, and
# it is the whole rule - a session is replaced when it has compacted five
# times, and not before. What makes it a wall rather than a counter is that
# the distance to it is computable at any moment from two things the bridge
# already knows: how many compactions this session has done, and where its
# context is now. Everything below is that arithmetic.
COMPACTIONS_TO_WALL = 5

ORDINALS = ("0th", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th")


def ordinal(n):
    """1 -> "1st". Which compaction is next, said the way a person says it."""
    try:
        return ORDINALS[int(n)]
    except Exception:
        return "%sth" % n


def life_view(sess, path):
    """How far this session is from the wall, in the work it can still absorb.

        rest of this cycle   = compaction point - what it carries now
        a later cycle        = compaction point - the floor it will start on
        the floor climbs     = by the rate measured between its own floors
        distance to the wall = rest of this cycle
                             + one whole cycle for each compaction left
                               before the fifth

    Every term is measured on this machine. Where one is missing it is named
    and nothing is invented in its place: without a compaction point there is
    no cycle, and without a floor the cycles after this one cannot be sized.

    So "left" - the distance to the fifth compaction - is present only when
    every term it needs exists, and "sizeable" says which it is. A partial
    sum is not a smaller answer, it is a different quantity: the rest of
    this cycle wearing the name of the whole distance.
    """
    wv = wall_view(sess, path)
    if not wv.get("window") or wv.get("assumed"):
        return {}
    role = sess.get("role") or "session"
    compact = wv.get("compact")
    used = wv.get("used") or 0
    per_turn = wv.get("per_turn") or 0
    done = compactions_done(path, role)
    floor = last_floor(path, role)
    rise = floor_rise(path, role)
    fl = floors(path, role)

    out = {"compact": compact, "floor": floor, "rise": rise,
           "per_turn": per_turn, "done": done,
           "budget": COMPACTIONS_TO_WALL,
           "compactions_left": max(0, COMPACTIONS_TO_WALL - done),
           "floors": [f["after"] for f in fl],
           "spent": done >= COMPACTIONS_TO_WALL}
    if not compact:
        out["why_blank"] = ("where compaction fires is not known for this "
                            "window, so neither the cycle nor the distance "
                            "to the fifth compaction can be worked out")
        return out

    out["cycle"] = max(0, compact - (floor if floor is not None else 0))
    out["cycle_turns"] = int(out["cycle"] / per_turn) if per_turn else None
    rest = max(0, compact - used)
    out["rest_of_cycle"] = rest
    out["rest_turns"] = int(rest / per_turn) if per_turn else None

    # How far through its whole life this session is, counting the cycles
    # it has already lived. Presentation only - nothing here decides
    # anything, and none of the arithmetic above is touched.
    #
    # "pct" a few lines down answers a different question and answers it
    # correctly: how much of what is LEFT has been consumed, where
    # consumed is progress through the current cycle. After a compaction
    # the carried size drops back to the floor, so that figure drops with
    # it - which is right for "how far to the next compaction" and wrong
    # for the thing a person reads off the bar, which is "how close is
    # this session to being replaced". A session two compactions into five
    # is two fifths of the way there and looked as though it had just
    # started.
    #
    # Life is the cycles behind it plus its position in this one, over the
    # five that end in a handover. Both terms are already computed.
    out["life_frac"] = (max(0.0, min(1.0, 1.0 - (rest / float(out["cycle"]))))
                        if out.get("cycle") else None)
    out["life_pct"] = (round((done + out["life_frac"]) * 100.0
                             / COMPACTIONS_TO_WALL, 1)
                       if out["life_frac"] is not None else None)
    # Which compaction the rest of this cycle actually ends at. With none
    # done that is the first one - a routine event, not the wall.
    out["next_ordinal"] = done + 1
    later = max(0, out["compactions_left"] - 1)
    out["later_cycles"] = later

    if later and floor is None:
        # It has never compacted, so there is no floor to start the next
        # cycle from and no honest way to size it.
        #
        # This used to answer with the rest of this cycle alone - left =
        # rest, the later cycles silently summed as zero - and everything
        # downstream read that as the whole distance to the fifth
        # compaction. A planner carrying 736k of a 1M window, 0 of 5
        # compactions done, 64k short of its FIRST compaction, was
        # published as "64k to the wall" at 92% of a bar that renders red:
        # a session about to do the most routine thing it does, displayed
        # as one about to die. The rest of this cycle is exact and is
        # given as exactly that; the total is not computed at all, because
        # the term it needs has not been measured. Callers must handle a
        # missing "left" - that is what "not sizeable" means.
        out["sizeable"] = False
        out["estimated"] = False
        out["why_partial"] = ("%d cycles after this one cannot be sized "
                              "until a compaction measures a floor" % later)
        return out

    sizes, f = [], floor if floor is not None else 0
    for _ in range(later):
        sizes.append(max(0, compact - f))
        f += (rise or 0)
    out["left"] = int(rest + sum(sizes))
    out["sizeable"] = True
    # One floor sizes the later cycles, but the climb between floors needs
    # two to be seen, so every cycle after this one is projected at the
    # same floor - which is known to be wrong in one direction: the floor
    # rises, so the real cycles are shorter and the real distance smaller.
    # It is a projection, and it is labelled one.
    out["estimated"] = bool(later and rise is None)
    if out["estimated"]:
        out["why_partial"] = ("the floor has been measured once, so the "
                              "later cycles are sized at that same floor "
                              "- the climb needs two floors to be seen")
    out["total"] = int(out["left"] + max(0, used - (floor or 0)))
    out["consumed"] = int(max(0, out["total"] - out["left"]))
    out["pct"] = (round(out["consumed"] * 100.0 / out["total"], 1)
                  if out["total"] else 100.0)
    out["turns_left"] = (int(out["left"] / per_turn) if per_turn else None)
    return out


def compactions_done(path, role):
    """How many times the session running right now has compacted.

    The budget belongs to a conversation, not to a chair. It used to be
    counted per role and never cleared, so a replacement session inherited
    everything its predecessor had spent: a brand-new executor, three
    minutes old, was already reported as "1 of 2 compactions used" and
    therefore already inside the last-cycle rules that can order a
    handover. One premature handover was enough to keep the pair handing
    over for the rest of the night.

    Entries carry the session id that produced them, so the count is now
    exact. Entries from before that was recorded belong to older sessions
    by definition and no longer count against this one.
    """
    hist = (STATE.get("compactions") or {}).get(
        "%s|%s" % (norm(path), role)) or []
    if not hist:
        return 0
    sid = last_session_id(path, role) or ""
    if not sid:
        return 0
    return len([h for h in hist if (h.get("session") or "") == sid])


def plan_for(sess, path):
    """What this session should do next about its own size, in one word.

    Every number this reads is already measured elsewhere - the window, the
    compaction point, the compactions spent, the runway, the biggest recent
    turn. The only thing that matters here is the order they are read in,
    and that order used to be wrong: the jump test came first and overruled
    everything, so a session with two compactions still ahead of it and
    nearly a million tokens of runway was ordered to hand over because one
    turn was larger than the gap to its next compaction. Crossing that gap
    is not a danger - it is what triggers the compaction.

    working    - carry on; an ordinary compaction is the next thing ahead
    compacting - at or near the compaction point. It summarises itself at
                 the end of the turn and continues in the same window. The
                 bridge says nothing; there is nothing to decide.
    handover   - worth replacing, because the room it has left to work in
                 no longer holds five turns. One rule, from measured terms;
                 no compaction budget and no distance to an unmeasured wall.
    """
    wv = wall_view(sess, path)
    if not wv.get("window"):
        used = wv.get("used") or 0
        return {"do": "unknown",
                "why": (("carrying %dk, but " % (used // 1000)) if used
                        else "") + (wv.get("why_blank")
                                    or "nothing read from this session yet")}
    used = wv["used"]
    compact = wv.get("compact")
    role = sess.get("role") or "session"
    done = compactions_done(path, role)
    pct = wv.get("pct_of_limit", 0)

    if wv.get("assumed"):
        return {"do": "working", "pct": pct, "compactions": done,
                "why": "carrying %dk; the window is %s, so nothing is "
                       "decided from it until the session states its own"
                       % (used // 1000, wv.get("window_source"))}

    # 0. A size from before the last compaction describes a conversation
    #    that no longer exists. Every rule below reads it, and the one they
    #    reach for is a handover - so nothing is decided until the session
    #    has drawn itself again.
    stale = wv.get("stale_reading")
    if stale:
        return {"do": "compacting", "pct": pct, "compactions": done,
                "stale": True,
                "why": "it compacted %d min ago and the size still reads "
                       "%dk, which is what it carried before the summary - "
                       "nothing is decided from it until a smaller reading "
                       "arrives" % (stale["age"] // 60, used // 1000)}

    # 1. The wall: the fifth compaction. One rule, and the only one.
    #    Not a distance to a reserve nobody measured, not a jump test, not a
    #    margin of turns - those were all mine, and none of them was what we
    #    agreed. A session is replaced when it has compacted five times.
    lv = life_view(sess, path) or {}
    if lv.get("spent"):
        return {"do": "handover", "pct": pct, "compactions": done,
                "why": "it has compacted %d times, which is the wall for "
                       "this bridge; a fresh session with the handoff is "
                       "what comes next" % done}

    # 2. At or near the compaction point: routine, and the bridge says
    #    nothing. Compaction is checked at the turn boundary, so the session
    #    crosses this line inside a turn and is summarised afterwards.
    if compact and used >= compact:
        return {"do": "compacting", "pct": pct, "compactions": done,
                "why": "at its compaction point (%dk); it compacts at the end "
                       "of the turn and carries on in the same window"
                       % (compact // 1000)}
    if compact and used >= compact * 0.9:
        return {"do": "compacting", "pct": pct, "compactions": done,
                "why": "%dk short of its compaction point at %dk; it will "
                       "compact and carry on"
                       % ((compact - used) // 1000, compact // 1000)}

    return {"do": "working", "pct": pct, "compactions": done,
            "cycle": lv.get("cycle"),
            "why": ("carrying %dk" % (used // 1000))
                   + (", compaction at %dk (%s)"
                      % (compact // 1000, wv.get("compact_source") or "")
                      if compact else ", compaction point not known")
                   + (", %d of %d compactions used"
                      % (done, lv.get("budget") or COMPACTIONS_TO_WALL))
                   + (", %dk of work to the fifth%s"
                      % (lv["left"] // 1000,
                         (" (%d turns)" % lv["turns_left"])
                         if lv.get("turns_left") else
                         " (turn cost not measured yet)")
                      if lv.get("left") else
                      (", %dk to its %s compaction and the cycles after it "
                       "not sizeable yet"
                       % ((lv.get("rest_of_cycle") or 0) // 1000,
                          ordinal(lv.get("next_ordinal") or 1)))
                      if lv.get("sizeable") is False else "")}


def mechanical_handoff(path, lp, extra_note=""):
    """The sliding handoff the bridge can always write from its own records."""
    name = project_name(path)
    branch = ""
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=8)
        branch = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        pass
    index_tail = "\n".join(store.read_index(path, 12))
    open_items = STATE.get("last_feedback", {}).get(norm(path), "")
    text = ("# handoff - %s - iteration %d\n\n"
            "## current task\n%s%s\n\n"
            "## recent iterations (from INDEX.md)\n%s\n\n"
            "## still open\n%s\n\n"
            "## where to look\nbridge-logs/<date>/dialogue/ - every report "
            "and verdict\nbridge-logs/INDEX.md - the map\n"
            % (name, lp.get("iteration", 0),
               "branch %s" % branch if branch else "see INDEX below",
               (" - " + extra_note) if extra_note else "",
               index_tail or "(none yet)",
               open_items or "(no open feedback recorded)"))
    store.write_handoff(path, lp.get("iteration", 0), text)
    return text


def git_commit_iteration(path, n, verdict):
    pconf = store.project_config(CFG, path)
    if not pconf.get("commit_each_iteration"):
        return
    try:
        if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=path,
                          capture_output=True, timeout=8).returncode != 0:
            return
        subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True,
                       timeout=30)
        subprocess.run(["git", "commit", "-m",
                        "bridge: iteration %d (%s)" % (n, verdict)],
                       cwd=path, capture_output=True, timeout=30)
    except Exception:
        pass


RC_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")


def transcript_candidates(session_id, cwd, given=None):
    """Files that could hold this session's remote-control link, best first."""
    out = []
    if given and os.path.exists(given):
        out.append(given)
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(base):
        return out
    if session_id:
        for folder in os.listdir(base):
            cand = os.path.join(base, folder, "%s.jsonl" % session_id)
            if os.path.exists(cand) and cand not in out:
                out.append(cand)
    if cwd:
        enc = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd))
        folder = os.path.join(base, enc)
        if os.path.isdir(folder):
            files = [os.path.join(folder, f) for f in os.listdir(folder)
                     if f.endswith(".jsonl")]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            out.extend(f for f in files[:5] if f not in out)
    return out


def _sid_of(path):
    """The session a transcript belongs to, read from its own lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                if "essionId" not in line and "ession_id" not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                sid = row.get("sessionId") or row.get("session_id")
                if sid:
                    return sid
    except Exception:
        pass
    return None


def link_in_file(path, expect_sid=None):
    """The last remote-control URL in this transcript, if it is the right one.

    The file must belong to the session being asked about - otherwise a
    neighbouring transcript of the same project would hand back somebody
    else's link, which is worse than having none.
    """
    if not path or not os.path.exists(path):
        return None
    if expect_sid:
        own = _sid_of(path)
        if own and own != expect_sid:
            return None
    found = None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "claude.ai/code/session_" not in line:
                    continue
                m = RC_URL.search(line)
                if not m:
                    continue
                slug = ""
                try:
                    slug = json.loads(line).get("slug") or ""
                except Exception:
                    pass
                found = {"url": m.group(0), "slug": slug}   # newest wins
    except Exception:
        return None
    return found


def find_rc_link(paths, expect_sid=None):
    """First file that yields a link belonging to this session."""
    if isinstance(paths, str) or paths is None:
        paths = [paths] if paths else []
    for path in paths:
        got = link_in_file(path, expect_sid)
        if got:
            return got
    return None


def links_text():
    rc = STATE.get("rc") or {}
    lines = ["bridge - your sessions"]
    for key in sorted(rc):
        path, _, role = key.rpartition("|")
        e = rc[key] or {}
        if not e.get("url"):
            continue
        # The pair's colour leads the line, the same one its messages
        # carry, so a pinned list of four links can be read at a glance
        # instead of by comparing project names character by character.
        mark = mark_for(path)
        lines.append("%s%s - %s%s\n%s"
                     % (("%s " % mark) if mark else "",
                        project_name(path), role,
                        (" (%s)" % e["slug"]) if e.get("slug") else "",
                        e["url"]))
    if len(lines) == 1:
        return None
    lines.append("Tap a link to take over that session from the phone.")
    return "\n\n".join(lines)


def push_links(force=False):
    """Put the links where the phone can reach them.

    force is only ever passed by a person pressing the button: it abandons
    the pinned message and sends a new one. Everything automatic edits the
    existing message in place, which is silent - that is what makes it safe
    to call on every session start, and also what leaves nothing to do when
    the pin has been lost.
    """
    global CFG
    text = links_text()
    if not text or not CFG.get("telegram", {}).get("chat_id"):
        return False
    CFG = telegram.pin_links(CFG, text, force=force)
    store.save_config(CFG)
    return True


def watch_rc_link(path, role, transcript_path, session_id=None, tries=24):
    """The link appears a moment after the session starts, so look again."""
    key = "%s|%s" % (norm(path), role)
    looked = []
    for _ in range(tries):
        looked = transcript_candidates(session_id, path, transcript_path)
        got = find_rc_link(looked, session_id)
        if got:
            with _lock:
                prev = (STATE.setdefault("rc", {}) or {}).get(key) or {}
                if prev.get("url") != got["url"]:
                    STATE["rc"][key] = got
                    save_state()
                    store.journal("session", "%s remote-control link: %s"
                                  % (role, got["url"]), project_name(path),
                                  role, "log", project_dir=path)
                    changed = True
                else:
                    changed = False
            if changed:
                push_links()
            return
        time.sleep(5)
    store.journal("session", "No remote-control link found for the %s after "
                  "%d min - is /rc actually on in that window? Looked in: %s"
                  % (role, tries * 5 // 60 or 1,
                     ", ".join(looked) or "no transcript found"),
                  project_name(path), role, "log", project_dir=path)


# Denying the editing tools alone is not enough: a shell command writes
# files just as well, which is exactly how a reviewer sneaks past. These are
# the tool names Claude Code v2.1 actually knows - MultiEdit is not one of
# them and would only earn a "matches no known tool" warning.
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit", "Bash", "PowerShell",
               "ExitPlanMode")


def disallow_for(path, role):
    """The reviewer's job is to judge and to speak, not to edit.

    With readonly_planner on (the default) the planner window is started
    with the editing tools denied, which no permission mode can loosen -
    so changes can only happen through the executor, whatever mode you
    picked for the reviewer.
    """
    if role != "planner":
        return None
    if not store.project_config(CFG, path).get("readonly_planner", True):
        return None
    # ExitPlanMode is on the list for a reason: without it the reviewer can
    # simply ask to leave plan mode and then do the work itself, which is the
    # exact thing this switch exists to prevent.
    return list(WRITE_TOOLS)


def mode_for(path, role):
    """The permission mode this role's window is started in.

    A project that names its own wins; otherwise the bridge-wide default in
    config.json ("role_modes"). Every launch path asks this - the panel's
    button, a restart, a handover, the silence-driven ensure_session - so
    changing the answer changes all of them at once, and a handover is
    enough to move a live pair onto a new mode without restarting the
    daemon.

    Returning None is still allowed and still means "whatever sessions.py
    would have chosen"; nothing here forces a mode that has not been set.
    """
    pc = (CFG.get("projects") or {}).get(norm(path)) or {}
    own = (pc.get("modes") or {}).get(role)
    if own:
        return own
    return (CFG.get("role_modes") or {}).get(role)


def remember_telemetry(path, role, sess):
    """Keep the last numbers per role, outside the session record.

    Telemetry arrives only while a session is rendering its status line, and
    it used to live inside the session record - so a bridge restart, a
    handover or a closed window took the numbers with it and the panel went
    blank. Kept here, the last known figures survive to be shown as what
    they are: the last reading, with the time it was taken.
    """
    if not sess.get("window"):
        return
    with _lock:
        STATE.setdefault("telemetry", {})["%s|%s" % (norm(path), role)] = {
            "window": sess.get("window"), "window_observed": True,
            "model": sess.get("model"),
            "model_id": sess.get("model_id"),
            "context_tokens": sess.get("context_tokens"),
            "context_pct": sess.get("context_pct"),
            "turn_costs": (sess.get("turn_costs") or [])[-5:],
            "at": time.strftime("%H:%M:%S"), "epoch": time.time()}
        save_state()


def last_telemetry(path, role):
    return (STATE.get("telemetry") or {}).get("%s|%s" % (norm(path), role))


def reg_pid(path, role, pid, sid=None, model_req=None, autocompact=None):
    with _lock:
        STATE.setdefault("pids", {})["%s|%s" % (norm(path), role)] = {
            "pid": pid, "at": time.time(), "sid": sid,
            "model_req": model_req, "registered": False,
            "autocompact": autocompact}
        save_state()


def applied_compact_pct(path, role):
    """The compaction threshold this window was actually started with.

    Only a window the bridge opened carries the setting, and only if it was
    opened after the setting existed. Anything else runs on Claude Code's
    own default, which is higher and not ours to know - so for those the
    honest answer is that the compaction point is unknown, not 80%.
    """
    entry = (STATE.get("pids") or {}).get("%s|%s" % (norm(path), role))
    if isinstance(entry, dict):
        return entry.get("autocompact")
    return None


def remember_session(path, role, sid):
    """Keep the last session id per role, outside the session records.

    Those records get retired and pruned - they are about what is running.
    The id is about what to reopen, and resume needs it long after the
    record it came from has gone.
    """
    if not sid:
        return
    with _lock:
        STATE.setdefault("last_session", {})["%s|%s" % (norm(path),
                                                        role)] = sid
        # ...and a ledger of every id this bridge ever ran, because
        # "last_session" holds one per role and the archive outlives them
        # both. Without it a transcript from last week has no honest way
        # back to a role, and the map would have to guess - which it does
        # not do. Bounded: the oldest entries fall off, and a file whose id
        # has fallen off is marked unknown rather than attributed loosely.
        led = STATE.setdefault("session_roles", {})
        led[sid] = {"role": role, "path": norm(path),
                    "project": project_name(path), "at": time.time()}
        if len(led) > 500:
            for old in sorted(led, key=lambda k: led[k].get("at") or 0)[:-500]:
                led.pop(old, None)
        save_state()


def known_sessions():
    """Every session id the bridge has a record of, with its role.

    The archive map attributes a file from this and from nothing else. Three
    records feed it, all of them the bridge's own bookkeeping: the ledger
    written at launch, the last id per role, and the live session records.
    A file whose id is in none of them is mapped as unknown - the map never
    reaches for the folder it sits in or the file next to it (§1.5).
    """
    out = {}
    with _lock:
        for sid, e in (STATE.get("session_roles") or {}).items():
            out[sid] = {"role": e.get("role") or "unknown",
                        "project": e.get("project") or "unknown",
                        "how": "bridge launched it as the %s"
                               % (e.get("role") or "unknown")}
        for key, sid in (STATE.get("last_session") or {}).items():
            p, _, role = key.rpartition("|")
            out.setdefault(sid, {"role": role,
                                 "project": project_name(p),
                                 "how": "last recorded %s of that project"
                                        % role})
        for sess in (STATE.get("sessions") or {}).values():
            sid = sess.get("session_id")
            if sid:
                out.setdefault(sid, {
                    "role": sess.get("role") or "unknown",
                    "project": sess.get("project") or "unknown",
                    "how": "live session record"})
    return out


def remap_archive(path, why=""):
    """Rebuild the archive map for a project, off whatever thread we are on.

    Called after every copy the bridge makes. It is deliberately fire and
    forget: the Stop hook that triggers it has an executor blocked on it,
    and a map is not worth a second of that.
    """
    try:
        archive.rebuild_async(path, known_sessions())
    except Exception:
        store.journal("archive", "Could not start the archive map rebuild "
                      "(%s): %s" % (why, traceback.format_exc()[-200:]),
                      project_name(path), "", "log")


def last_session_id(path, role):
    sid = (STATE.get("last_session") or {}).get("%s|%s" % (norm(path), role))
    if sid:
        return sid
    best, when = None, ""
    for sess in (STATE.get("sessions") or {}).values():
        if norm(sess.get("path")) != norm(path) or sess.get("role") != role:
            continue
        seen = sess.get("last_seen") or ""
        if seen >= when and sess.get("session_id"):
            best, when = sess.get("session_id"), seen
    return best


def mark_registered(path, role):
    """A session proved it is alive: it started, or its channel connected."""
    key = "%s|%s" % (norm(path), role)
    with _lock:
        entry = (STATE.get("pids") or {}).get(key)
        if isinstance(entry, dict) and not entry.get("registered"):
            entry["registered"] = True
        (STATE.get("autostart_tried") or {}).pop(key, None)
        (STATE.get("autostart_told") or {}).pop(key, None)
        save_state()


def compact_pct(path):
    """The compaction threshold the bridge tells sessions to use."""
    v = store.project_config(CFG, path).get("autocompact_pct")
    try:
        v = int(v)
    except Exception:
        return None
    return v if 1 <= v <= 100 else None


def already_up(path, role):
    """Is this role already running?

    Three witnesses, because no single one survives everything. The channel
    registry is the strongest but lives in memory, so a bridge restart wipes
    it and the sessions only reappear on their next heartbeat - up to a
    minute of blindness during which a resume would open windows on top of
    perfectly healthy ones. The pid survives on disk. The session record is
    the last resort: a session that reported a turn a minute ago is running,
    whatever the other two say.
    """
    # Evidence of life now beats any record of the past. A channel only
    # exists while its session does, and SessionEnd removes it, so a
    # connected channel is present tense - it cannot be outvoted by an
    # older record saying something ended.
    if channel_alive(path, role):
        return "its channel is answering"
    entry = (STATE.get("pids") or {}).get("%s|%s" % (norm(path), role)) or {}
    if isinstance(entry, dict) and sessions.pid_alive(entry.get("pid")):
        return "its window is still running"
    latest, when = None, ""
    for sess in (STATE.get("sessions") or {}).values():
        if norm(sess.get("path")) != norm(path) or sess.get("role") != role:
            continue
        seen = sess.get("last_seen") or ""
        if seen >= when:
            latest, when = sess, seen
    if latest and latest.get("state") in ("ended", "died"):
        return None                      # said goodbye and nothing disputes it
    if latest and when and abs(time.time() - _clock_of(when)) < 300:
        return "it reported activity at %s" % when
    return None


def _clock_of(hhmmss):
    """Turn a HH:MM:SS stamp back into an epoch on today's clock."""
    try:
        h, m, sec = (int(x) for x in hhmmss.split(":"))
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, m, sec,
                            0, 0, -1))
    except Exception:
        return 0


def launch_guard(path, role):
    """Refuse to open a second window for a session that never came up.

    A session stuck on a startup dialog looks dead to every meter: no
    channel, no SessionStart, and on Windows the tracked pid can belong to
    a shim that has already exited. Without this guard the bridge reads
    that as a death and opens another window, which gets stuck the same
    way - that is how six of them appear in a row. So: one pending start
    at a time, and a hard ceiling on launches per hour whatever happens.
    """
    key = "%s|%s" % (norm(path), role)
    entry = (STATE.get("pids") or {}).get(key) or {}
    if isinstance(entry, dict) and entry.get("pid") \
            and not entry.get("registered"):
        waited = time.time() - entry.get("at", 0)
        if waited < float(CFG.get("thresholds", {}).get("startup_grace",
                                                        600)):
            return ("a %s window opened %d s ago and has not come up yet - "
                    "it is probably sitting on the development-channels "
                    "dialog. Answer it, or close that window."
                    % (role, int(waited)))
    now_ts = time.time()
    with _lock:
        hist = [t for t in (STATE.setdefault("launches", {})
                            .setdefault(key, [])) if now_ts - t < 3600]
        STATE["launches"][key] = hist
        save_state()
    cap = int(CFG.get("thresholds", {}).get("launches_per_hour", 6))
    if len(hist) >= cap:
        return ("%d %s windows have been opened for this project in the last "
                "hour - not opening another. Something is stopping them from "
                "starting." % (len(hist), role))
    return None


def note_launch(path, role, why="unspecified"):
    key = "%s|%s" % (norm(path), role)
    with _lock:
        STATE.setdefault("launches", {}).setdefault(key, []).append(
            time.time())
        hist = STATE.setdefault("launch_log", [])
        hist.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "project": project_name(path), "role": role,
                     "why": why})
        del hist[:-40]
        save_state()
    store.journal("session", "Opening a %s window: %s" % (role, why),
                  project_name(path), role, "log", project_dir=path)


def model_req_of(path, role):
    v = STATE.get("pids", {}).get("%s|%s" % (norm(path), role))
    return (v or {}).get("model_req") if isinstance(v, dict) else None


def pid_of(path, role):
    v = STATE.get("pids", {}).get("%s|%s" % (norm(path), role))
    return v.get("pid") if isinstance(v, dict) else v


def last_record(path, role):
    best = None
    for s in STATE.get("sessions", {}).values():
        if norm(s.get("path")) == norm(path) and s.get("role") == role:
            if best is None or seen_at(s) > seen_at(best):
                best = s
    return best


# ---------------------------------------------------------------------------
# session death watch: the bridge is alive but a session window is not.
# Its own meters (nothing to do with the clean-shutdown marker): the pid of
# the window we started, the channel heartbeat from inside the session, and
# the SessionEnd hook that marks a proper exit. All three silent = it died.

def state_report(path, role, sess, headline, whats_next):
    """The state, as facts. No sentence is assembled to sound right.

    Everything here is either measured or explicitly absent, and the
    session draws its own conclusion - it is better at that than a
    template. What the bridge must not do is imply knowledge it does not
    have, so an unknown says so in the line where it belongs.
    """
    wv = wall_view(sess, path) or {}
    lv = life_view(sess, path) or {}
    lines = ["Situation: %s" % headline]

    if wv.get("window"):
        lines.append("Context: %dk of %dk (window %s)."
                     % (wv["used"] // 1000, wv["window"] // 1000,
                        wv.get("window_source") or "known"))
    elif wv.get("used"):
        lines.append("Context: %dk carried; the bridge does not know your "
                     "window yet." % (wv["used"] // 1000))
    else:
        lines.append("Context: not read yet.")

    if wv.get("stale_reading"):
        lines.append("Note: that size is what you carried into your last "
                     "compaction, %d minutes ago; the summary has not been "
                     "read back yet, so nothing is being decided from it."
                     % (wv["stale_reading"]["age"] // 60))

    done = compactions_done(path, role)
    lines.append("Compactions this session has done: %d." % done)

    if lv.get("floor") is not None:
        lines.append("Your last compaction left you at %dk."
                     % (lv["floor"] // 1000))
    if lv.get("compact"):
        lines.append("Compaction fires at %dk (%s)."
                     % (lv["compact"] // 1000,
                        wv.get("compact_source") or "source not recorded"))
    else:
        lines.append("Where compaction fires is not known for this window.")

    if wv.get("wall"):
        if wv.get("wall_measured"):
            lines.append("The wall - where a compaction can no longer be "
                         "assembled - is %dk, measured here. You have %dk to "
                         "it." % (wv["wall"] // 1000,
                                  wv["room_to_wall"] // 1000))
        elif wv.get("wall_low"):
            lines.append("The wall is somewhere between %dk and %dk; neither "
                         "end is measured here. You have %dk to the near end "
                         "and %dk to the far one. Your biggest recent turn "
                         "was %dk."
                         % (wv["wall_low"] // 1000, wv["wall"] // 1000,
                            wv["room_to_wall_low"] // 1000,
                            wv["room_to_wall"] // 1000,
                            (wv.get("worst_turn") or 0) // 1000))
        else:
            lines.append("The wall is %dk (%s). You have %dk to it."
                         % (wv["wall"] // 1000, wv.get("wall_source") or "",
                            wv["room_to_wall"] // 1000))

    lines.append("Compactions: %d of %d. The fifth is where this bridge "
                 "replaces you." % (lv.get("done", 0),
                                    lv.get("budget") or COMPACTIONS_TO_WALL))
    if lv.get("left") is not None:
        lines.append("Work still to absorb before it: about %dk%s. That is "
                     "the rest of this cycle (%dk) plus %d whole cycles "
                     "after it.%s"
                     % (lv["left"] // 1000,
                        (", roughly %d turns at your recent %dk a turn"
                         % (lv["turns_left"], (lv.get("per_turn") or 0) // 1000))
                        if lv.get("turns_left") else "",
                        (lv.get("rest_of_cycle") or 0) // 1000,
                        lv.get("later_cycles", 0),
                        " Those later cycles are an estimate."
                        if lv.get("estimated") else ""))
    elif lv.get("sizeable") is False:
        # Not a smaller number - a different one. Saying "you have 64k left"
        # here would name the first compaction and mean the fifth.
        lines.append("Work still to absorb before it: not a single number "
                     "yet. %dk%s takes you to your %s compaction, which is "
                     "routine; the %d cycles after it cannot be sized until "
                     "a compaction measures a floor."
                     % ((lv.get("rest_of_cycle") or 0) // 1000,
                        (" (roughly %d turns)" % lv["rest_turns"])
                        if lv.get("rest_turns") else "",
                        ordinal(lv.get("next_ordinal") or 1),
                        lv.get("later_cycles", 0)))
    if lv.get("why_partial") and lv.get("sizeable") is not False:
        # The not-sizeable sentence above already carries the reason; adding
        # it again as a note said the same thing twice in a row.
        lines.append("Note: %s." % lv["why_partial"])
    if lv.get("why_blank"):
        lines.append("Note: %s." % lv["why_blank"])

    if role == "executor":
        # Every number above is honest and stays (§1.6.8 - not deciding
        # from a figure is no reason to hide it), but handed to an
        # executor unqualified it reads as an instruction to watch its own
        # fill, and executors were ending turns early and reporting that
        # they were waiting to be replaced. So the numbers keep their
        # provenance and get their owner said out loud.
        lines.append("None of the above is yours to act on: the bridge "
                     "measures it and does the compaction and the "
                     "replacement itself, between turns, carrying the "
                     "thread across. Work the task you have to the natural "
                     "end of the turn whatever the figures say.")
    lines.append("What to do: %s" % whats_next)
    return "\n".join(lines)


def expire_handover(path):
    """A handover only counts as under way while it can still finish.

    It is cleared when every replaced session registers - and a window
    sitting on its startup dialog never does. Left alone, that one flag
    stays true forever, and because 'a handover is under way' is checked
    before everything else, the whole assessment stops there: no cancel, no
    question passed on, no nudge. One stuck flag silences the lot, which is
    exactly the failure it was meant to prevent.
    """
    hv = (STATE.get("handover") or {}).get(norm(path))
    if not hv:
        return None
    age = time.time() - (hv.get("at") or 0)
    limit = float(CFG.get("thresholds", {}).get("handover_grace", 600))
    if age < limit:
        return None
    waiting = hv.get("waiting") or []
    with _lock:
        (STATE.get("handover") or {}).pop(norm(path), None)
        save_state()
    store.journal("rotation", "The handover started %d min ago never "
                  "finished - %s never came up. Clearing it so the bridge "
                  "can see the rest of the picture again."
                  % (age // 60, " and ".join(waiting) or "a session"),
                  project_name(path), "executor", "sound", project_dir=path)
    notify("needs_you",
           "%s: a handover stalled - the new %s window never started, almost "
           "certainly waiting on its development-channels dialog. Answer it "
           "or close it; the bridge is no longer waiting on it."
           % (project_name(path), " and ".join(waiting) or "session"), path=path)
    return waiting


def situation(path):
    """Everything the bridge knows about a project, in one place."""
    path = norm(path)
    _, lp = loop_state(path)
    out = {"path": path, "loop": bool(lp.get("active")),
           "paused": paused_for(path),
           "reviewing": bool(PENDING.get(path)),
           "verdict_in_flight": bool((STATE.get("awaiting") or {}).get(path)),
           # How many pieces this pair has had accepted with nothing to open.
           # It rides in the same payload the panel already polls, so the
           # named exit is visible without anyone going to look for it.
           "no_artifacts": (STATE.get("noart") or {}).get(path, 0),
           # Reports that got no verdict, in a row. Silence used to read as
           # consent; it is a number on the panel now.
           "unanswered": (STATE.get("unanswered") or {}).get(path, 0),
           # Declared temporary solutions still open. This never blocks -
           # blocking would only teach the pair to stop saying the word -
           # but it is always in front of a human, which is the mechanism.
           "debt_open": len(open_debt(path)),
           "debt_total": len(debt_rows(path)),
           "frames_wanted": bool((STATE.get("frames") or {}).get(path)),
           "handover": bool((STATE.get("handover") or {}).get(path)),
           "inflight": list((STATE.get("inflight") or {}).get(path, {}).values())
                       + list(PROCTRACK.get(path, {}).values()),
           "roles": {}}
    for role in ("executor", "planner"):
        sess = best_session(path, role) or {}
        sid = last_session_id(path, role) or sess.get("session_id")
        tp = sessions.transcript_of(sid) if sid else None
        tail = sessions.tail_of_transcript(tp) if tp else []
        seen = sess.get("last_seen") or ""
        out["roles"][role] = {
            "alive": bool(already_up(path, role)), "sess": sess,
            "state": sess.get("state"), "tail": tail,
            "silent_for": (time.time() - _clock_of(seen)) if seen else None,
            "plan": plan_for(sess, path) if sess else {"do": "unknown"},
        }
    return out


def looks_like_a_question(tail):
    """Did the session end its turn by asking something?"""
    for row in reversed(tail or []):
        if row.get("who") != "assistant":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        last = text[-400:]
        return ("?" in last) or any(
            w in last.lower() for w in
            ("should i", "shall i", "which ", "confirm",
             "let me know"
             ) + tuple(CFG.get("question_hints") or ()))
    return False


def was_cut_for_handover(path, tail):
    """Did the bridge stop this turn for a handover?

    The marker set when a turn is cut only covers turns cut since it was
    added. The evidence that outlives any bookkeeping is in the session's
    own transcript: the stop reason the bridge itself wrote is sitting
    there in plain words.
    """
    if STATE.get("cut_for_handover:%s" % norm(path)):
        return True
    for row in (tail or [])[-4:]:
        text = (row.get("text") or "").lower()
        if "bridge:" in text and "handing over" in text:
            return True
    return False


def waiting_for_direction(tail):
    """Did the session finish cleanly and stop for want of a next piece?

    An executor that has just handed off, listed a queue and stopped is not
    stalled - it is done and waiting. Telling it to carry on with the work
    in hand is telling it to do nothing, because there is none. What it
    needs is the next task, and that comes from the planner.
    """
    for row in reversed(tail or []):
        if row.get("who") != "assistant":
            continue
        low = (row.get("text") or "").lower()
        if not low.strip():
            continue
        return any(p in low for p in (
            "nothing in flight", "no work to carry on", "nothing to carry on",
            "handover is already complete", "clean seam", "i'd rather stop",
            "ready to pick up", "queue for the fresh session",
            "waiting for direction")
            + tuple(CFG.get("idle_hints") or ()))
    return False


def looks_busy(tail):
    """Is the last thing in the conversation a tool that has not returned?"""
    for row in reversed(tail or []):
        text = row.get("text") or ""
        if row.get("who") == "assistant" and "[ran " in text:
            return True
        if row.get("who") == "user" and "[result]" in text:
            return False
    return False


def stall_watch():
    """A loop can stall silently: the verdict went out, the executor never
    picked it up, and the planner is waiting for a report that will not come.
    Nothing is broken, so no other watcher fires - hence this one."""
    while True:
        grace = float(CFG.get("thresholds", {}).get("stall_grace", 180))
        time.sleep(max(5.0, min(45.0, grace / 3.0)))
        try:
            check_stalls()
        except Exception:
            pass


def check_stalls():
    grace = float(CFG.get("thresholds", {}).get("stall_grace", 180))
    for path, w in list((STATE.get("awaiting") or {}).items()):
        if not (STATE.get("loops") or {}).get(path, {}).get("active"):
            with _lock:
                (STATE.get("awaiting") or {}).pop(path, None)
                save_state()
            continue
        waited = time.time() - w.get("since", 0)
        if waited < grace:
            continue
        tries = w.get("nudges", 0)
        name = project_name(path)
        if tries < 2:
            again = deliver(path, "executor", w.get("body") or "", 
                            {"kind": "verdict"})
            with _lock:
                w["nudges"] = tries + 1
                w["since"] = time.time()
                save_state()
            store.journal("loop", "Verdict %s re-sent to the executor - it "
                          "had not picked it up in %d min"
                          % (w.get("iteration"), int(waited // 60)), name,
                          "executor", "log", project_dir=path)
            if not again:
                notify("needs_you",
                       "%s: the executor is not taking the verdict and its "
                       "channel is gone. Open its window." % name, path=path)
        else:
            with _lock:
                (STATE.get("awaiting") or {}).pop(path, None)
                save_state()
            notify("needs_you",
                   "%s: the loop is stalled. The planner's verdict on "
                   "iteration %s went out three times and the executor never "
                   "started a turn. Type anything in its window to wake it, "
                   "or restart it from the panel." % (name, w.get("iteration")), path=path)


def session_watch():
    grace = float(os.environ.get("BRIDGE_GRACE_SEC", "90"))
    interval = float(os.environ.get("BRIDGE_WATCH_SEC", "20"))
    _wdbg("watch thread up, grace=%s interval=%s" % (grace, interval))
    while True:
        time.sleep(interval)
        try:
            check_sessions(grace)
        except Exception:
            pass


def _wdbg(msg):
    """BRIDGE_DEBUG=1 writes watch decisions to <temp>/bridge-watch.log."""
    if not os.environ.get("BRIDGE_DEBUG"):
        return
    try:
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "bridge-watch.log")
        with open(p, "a") as fh:
            fh.write("%.1f %s\n" % (time.time() % 10000, msg))
    except Exception:
        pass


def check_sessions(grace):
    now_ts = time.time()
    # The bridge keeps no live handles across its own restart: PROCS is
    # empty, the pids on disk may be stale, and the channels take a few
    # seconds to reconnect. Judging a session in that gap declares a
    # perfectly healthy window dead and opens another one - so nothing is
    # judged until the dust settles.
    settle = float(CFG.get("thresholds", {}).get("restart_settle", 150))
    if now_ts - (STATE.get("started_at") or 0) < settle:
        return
    for key, meta in list(STATE.get("pids", {}).items()):
        path, _, role = key.rpartition("|")
        pid = meta.get("pid") if isinstance(meta, dict) else meta
        started = meta.get("at", 0) if isinstance(meta, dict) else 0
        if now_ts - started < grace:
            continue                       # still booting, do not judge yet
        if key in (STATE.get("down") or {}):
            continue                       # already flagged
        if isinstance(meta, dict) and not meta.get("registered"):
            # never came up: a startup dialog, a crash on launch, a bad
            # flag. Restarting would only open another stuck window.
            wait = now_ts - started
            if wait < float(CFG.get("thresholds", {}).get("startup_grace",
                                                          600)):
                continue
            if not meta.get("gave_up"):
                meta["gave_up"] = True
                save_state()
                store.journal("session", "%s window never came up" % role,
                              project_name(path), role, "sound",
                              project_dir=path)
                notify("session_died",
                       "%s: the %s window opened %d min ago and never "
                       "started - almost certainly waiting on the "
                       "development-channels dialog. Answer it in that "
                       "window (press 1), or close it and start the pair "
                       "again. No further windows will be opened."
                       % (project_name(path), role, int(wait // 60)), path=path)
            continue
        a1 = sessions.alive(path, role)
        a2 = sessions.pid_alive(pid)
        a3 = bool(channel_for(path, role))
        _wdbg("tick %s pid=%s age=%.1f alive=%s/%s/%s" %
              (key, pid, now_ts - started, a1, a2, a3))
        if a1 or a2 or a3:
            continue                       # any pulse = alive
        _wdbg("DEAD %s pid=%s -> handling" % (key, pid))
        rec = last_record(path, role)
        if rec and rec.get("state") == "ended":
            with _lock:                    # proper exit, nothing to report
                STATE.get("pids", {}).pop(key, None)
                save_state()
            continue
        handle_session_death(path, role, rec)


def handle_session_death(path, role, rec):
    _wdbg("hsd enter %s/%s rec=%s" % (path, role, bool(rec)))
    name = project_name(path)
    sid = (rec or {}).get("session_id") or         (STATE.get("pids", {}).get("%s|%s" % (path, role)) or {}).get("sid")
    with _lock:
        STATE.setdefault("down", {})["%s|%s" % (path, role)] = {
            "at": time.time(), "sid": sid}
        if rec is not None:
            rec["state"] = "died"
        lp = STATE.get("loops", {}).get(path)
        # Only this pair is held. Setting mode="paused" here - which is what
        # this did - stopped reviewing finished turns in every other folder
        # on the machine because one window died in this one. The other
        # pairs were working, so nothing looked broken and nothing said why
        # their reports had stopped being carried.
        died_mid_loop = bool(role == "executor" and lp and lp.get("active")
                             and not paused_for(path))
        save_state()
    if died_mid_loop:
        pause_project(path, "its executor window died", by_death=True)
    if role == "planner":
        w = PENDING.get(path)
        if w and w.get("content"):
            QUEUED.setdefault(path, []).append(json.dumps(
                {"content": w["content"], "meta": w.get("meta", {})}))
            store.journal("session_died", "Pending report re-queued for the "
                          "restarted planner", name, role, "log",
                          project_dir=path)
    store.journal("session_died", "%s / %s: the window process is gone"
                  % (name, role), name, role, "sound", project_dir=path)
    pconf = store.project_config(CFG, path)
    _wdbg("hsd auto=%s cfg_projects=%s" % (pconf.get("auto_restart_dead_sessions"), list((CFG.get("projects") or {}).keys())))
    if pconf.get("auto_restart_dead_sessions"):
        restart_session(path, role, auto=True)
    else:
        notify("session_died",
               "%s: the %s window is gone - the claude process (or its "
               "console host) died or was closed. The bridge itself is fine "
               "and the session's transcript is intact; it can be brought "
               "back exactly where it stopped." % (name, role),
               buttons=["restart %s" % role, "status"], path=path)


def restart_session(path, role, auto=False):
    _wdbg("rs enter %s/%s auto=%s" % (path, role, auto))
    path = norm(path)
    key = "%s|%s" % (path, role)
    now_ts = time.time()
    with _lock:
        tries = [t for t in STATE.setdefault("restart_tries", {})
                 .setdefault(key, []) if now_ts - t < 600]
        STATE["restart_tries"][key] = tries
        save_state()
    if auto and len(tries) >= 2:
        with _lock:
            STATE.setdefault("down", {}).setdefault(key, {})["giveup"] = True
            save_state()
        store.journal("session_died", "%s / %s keeps dying right after "
                      "start - automatic retries stopped"
                      % (project_name(path), role), project_name(path), role,
                      "sound", project_dir=path)
        notify("session_died",
               "%s: the %s keeps dying right after start - not retrying on "
               "my own. Open the window by hand and look at the error it "
               "prints." % (project_name(path), role), path=path)
        return {"ok": False, "error": "kept dying - retries stopped"}
    stop_reason = launch_guard(path, role)
    if stop_reason:
        notify("session_died", "%s: not restarting the %s - %s"
               % (project_name(path), role, stop_reason), path=path)
        return {"ok": False, "error": stop_reason}
    d = (STATE.get("down") or {}).get(key) or {}
    sid = d.get("sid") or (last_record(path, role) or {}).get("session_id")
    _wdbg("rs sid=%s tries=%d" % (sid, len(tries)))
    pconf = store.project_config(CFG, path)
    chain = pconf["chains"].get(role) or []
    req = chain[0] if chain else None
    try:
        note_launch(path, role, "restart after it died")
        pid = sessions.launch(path, role, resume_id=sid,
                              model=models.resolve(req,
                                                   store.load_models()),
                              permission_mode=mode_for(path, role),
                              disallow=disallow_for(path, role),
                              autocompact_pct=compact_pct(path))
    except Exception as exc:
        _wdbg("restart launch failed %s: %s" % (key, exc))
        notify("session_died", "%s: could not restart the %s: %s"
               % (project_name(path), role, exc), path=path)
        return {"ok": False, "error": str(exc)}
    with _lock:
        # only automatic restarts count toward giving up - a human clicking
        # restart is a decision, not a symptom
        STATE["restart_tries"][key] = tries + ([now_ts] if auto else [])
        (STATE.get("down") or {}).pop(key, None)
        save_state()
    reg_pid(path, role, pid, sid, model_req=req,
            autocompact=compact_pct(path))
    _wdbg("restarted %s -> pid %s" % (key, pid))
    store.journal("session_died", "Restarting the %s where it stopped "
                  "(same session)" % role, project_name(path), role, "log",
                  project_dir=path)
    notify("session_died", "%s: restarting the %s where it stopped (same "
           "session, --resume)." % (project_name(path), role),
           level="silent" if auto else None, path=path)
    return {"ok": True, "pid": pid, "resumed": sid or ""}


# ---------------------------------------------------------------------------
# rotation

def ask_name_later(path, suggested):
    """Offer to rename the incoming session, without holding it up.

    Naming used to block the handover while it waited for a reply - up to
    five minutes during which the old session was already stopped and no
    replacement had been started. The pair sat dead in the middle of a
    handover because nobody had typed a name. Now the suggestion is used at
    once, and a better one, if it arrives in time, replaces it in the seed
    the new window has not read yet.
    """
    if not CFG.get("telegram", {}).get("chat_id"):
        return
    waiter = {"event": threading.Event(), "name": None,
              "suggested": suggested}
    NAMEWAIT[norm(path)] = waiter
    # The message id is remembered against this pair, so a reply naming the
    # session names THIS one. Two handovers running at once used to take
    # whichever name arrived first and apply it to both.
    mid = telegram.send(
        CFG, "%s Handing over in %s. The new session is called '%s'.\n"
             "Reply to this message within %d s with another name if you "
             "want one - the handover is already under way either way."
             % (mark_for(path), project_name(path), suggested,
                CFG["thresholds"].get("name_timeout", 120)),
        "silent", [("accept name", "accept name|%s" % pair_id(path))])
    remember_message(mid, path)
    waiter["event"].wait(CFG["thresholds"].get("name_timeout", 120))
    NAMEWAIT.pop(norm(path), None)
    name = waiter["name"]
    if name and name != suggested:
        with _lock:
            seed = (STATE.get("seed") or {}).get(norm(path))
            if seed:
                seed["title"] = name
                save_state()


def rotate_executor(path, reason, next_model=None):
    path = norm(path)
    _, lp = loop_state(path)
    name = project_name(path)
    store.journal("rotation", "Rotating executor: %s" % reason, name,
                  "executor", "log", project_dir=path)

    handoff = mechanical_handoff(path, lp, extra_note="rotated: " + reason)
    title = "%s-%d" % (name, lp.get("iteration", 0) + 1)
    threading.Thread(target=ask_name_later, args=(path, title),
                     daemon=True).start()

    sessions.stop(path, "executor", pid=pid_of(path, "executor"))
    retire_sessions(path, "executor")
    time.sleep(2)

    with _lock:
        STATE.setdefault("seed", {})[path] = {
            "handoff": handoff, "title": title, "reason": reason}
        save_state()

    stop_reason = launch_guard(path, "executor")
    if stop_reason:
        notify("needs_you", "%s: replacing the executor is on hold - %s"
               % (name, stop_reason), path=path)
        store.journal("rotation", "Rotation held: %s" % stop_reason, name,
                      "executor", "log", project_dir=path)
        return
    pconf = store.project_config(CFG, path)
    chain = pconf["chains"].get("executor") or []
    model = next_model or (chain[0] if chain else None)
    maybe_auto_probe()
    note_launch(path, "executor", "rotation: %s" % reason)
    try:
        pid = sessions.launch(path, "executor",
                              model=models.resolve(model,
                                                   store.load_models()),
                              permission_mode=mode_for(path, "executor"),
                              disallow=disallow_for(path, "executor"),
                              autocompact_pct=compact_pct(path))
        reg_pid(path, "executor", pid, model_req=model,
                autocompact=compact_pct(path))
        notify("rotation_name",
               "%s: new executor session '%s' is up (%s). The handoff went "
               "in with it." % (name, title, reason), path=path)
    except Exception as exc:
        notify("crash", "%s: could not start the replacement executor: %s"
               % (name, exc), path=path)


def log_handover_decision(path, role, sess, plan, wv=None, lv=None):
    """Write down every number a handover was decided from, before it runs.

    The first handover this bridge ever performed was correct in mechanism
    and wrong in timing, and there was nothing in the journal to say why -
    the reasoning had to be reconstructed from a screenshot afterwards.
    A decision that ends a session states its arithmetic at the moment it
    is taken, not in hindsight.
    """
    wv = wv if wv is not None else wall_view(sess or {}, path)
    lv = lv if lv is not None else life_view(sess or {}, path)
    row = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # Which pair this arithmetic belongs to. The row carried the role but
        # nothing about the project, and the log is one list for the whole
        # bridge - so the panel, which shows the newest row under the gauges
        # of the project on screen, was showing the numbers of whichever pair
        # happened to hand over last.
        "path": norm(path),
        "role": role,
        "why": plan.get("why"),
        "used": wv.get("used"),
        "window": wv.get("window"), "window_source": wv.get("window_source"),
        "compact_at": wv.get("compact"),
        "compact_source": wv.get("compact_source"),
        "floor": lv.get("floor"), "floors": lv.get("floors"),
        "floor_rise": lv.get("rise"),
        "cycle": lv.get("cycle"), "cycle_turns": lv.get("cycle_turns"),
        "per_turn": wv.get("per_turn"), "worst_turn": wv.get("worst_turn"),
        "rest_of_cycle": lv.get("rest_of_cycle"),
        "left_to_wall": lv.get("left"), "turns_left": lv.get("turns_left"),
        "sizeable": lv.get("sizeable"), "estimated": lv.get("estimated"),
        "budget": lv.get("budget"),
        "compactions_done": plan.get("compactions"),
        "compactions_left": lv.get("compactions_left"),
        "session_id": (sess or {}).get("session_id", ""),
    }
    with _lock:
        hist = STATE.setdefault("handover_log", [])
        hist.append(row)
        # Twenty was one pair's worth. The panel only ever shows the newest
        # row of the project on screen, so with several pairs sharing this
        # list a busy one could push a quiet one's only row out and blank
        # its "last handover" line.
        del hist[:-40]
        save_state()
    store.journal(
        "rotation",
        "Handover decided for the %s: %s | carrying %sk of a %sk window "
        "(%s) | compaction fires at %s (%s) | floors so far %s, climbing %sk "
        "a cycle | %s of %s compactions used | %s"
        % (role, plan.get("why"),
           (row["used"] or 0) // 1000, (row["window"] or 0) // 1000,
           row["window_source"],
           ("%dk" % (row["compact_at"] // 1000)) if row["compact_at"]
           else "not known", row["compact_source"],
           [f // 1000 for f in (row["floors"] or [])],
           (row["floor_rise"] or 0) // 1000,
           row["compactions_done"], row["budget"],
           ("%sk of work was left to the fifth (%s turns at %sk a turn)%s"
            % (row["left_to_wall"] // 1000, row["turns_left"],
               (row["per_turn"] or 0) // 1000,
               ", the later cycles estimated" if row["estimated"] else "")
            if row["left_to_wall"] is not None else
            "the distance to the fifth was not sizeable - %sk to the next "
            "compaction, no floor measured to size the cycles after it"
            % ((row["rest_of_cycle"] or 0) // 1000))),
        project_name(path), role, "log", extra=row, project_dir=path)
    return row


def handover_blocked(path, roles=("executor", "planner")):
    """Why a handover could not run, or None if it can.

    Checked before the turn is cut, never after: ending a session for a
    replacement that then fails to arrive leaves the work stopped with
    nothing to continue it - which is worse than a full context.
    """
    for role in roles:
        why = launch_guard(path, role)
        if why:
            return why
    return None


def handover(path, reason, roles=("executor", "planner")):
    """Move a session into a fresh one, carrying the thread across.

    Only the roles named are touched, and the automatic paths name exactly
    one: the session whose own numbers ran out. The two halves fill at
    different rates - the executor carries tool output, the planner carries
    reports - so they reach the end of their runways at different times,
    and replacing the one that has not is throwing away a working session
    and opening a window nobody needs. Both together is what the panel's
    'hand over both' button asks for, and nothing else.

    Everything after the dialog is automatic: each new window is seeded at
    SessionStart, and a new executor is handed its first task through the
    channel the moment it registers.
    """
    path = norm(path)
    name = project_name(path)
    _, lp = loop_state(path)
    stop_reason = handover_blocked(path, roles)
    if stop_reason:
        store.journal("rotation", "Handover held: %s" % stop_reason, name,
                      "executor", "sound", project_dir=path)
        notify("needs_you", "%s: handover is on hold - %s The sessions keep "
               "working; see 'windows the bridge opened' in sessions for "
               "what used the allowance." % (name, stop_reason), path=path)
        return {"ok": False, "error": stop_reason}

    handoff = mechanical_handoff(path, lp, extra_note="handover: " + reason)
    title = "%s-%d" % (name, lp.get("iteration", 0) + 1)
    threading.Thread(target=ask_name_later, args=(path, title),
                     daemon=True).start()
    feedback = (STATE.get("last_feedback") or {}).get(path, "")

    with _lock:
        for role in roles:
            (STATE.get("last_session") or {}).pop(
                "%s|%s" % (path, role), None)
        STATE["handover"] = dict(STATE.get("handover") or {})
        STATE["handover"][path] = {"at": time.time(), "reason": reason,
                                   "waiting": list(roles),
                                   "roles": list(roles),
                                   "iteration": lp.get("iteration", 0)}
        seeds = STATE.setdefault("seed", {})
        if "executor" in roles:
            seeds[path] = {"handoff": handoff, "title": title,
                           "reason": reason}
        if "planner" in roles:
            STATE.setdefault("planner_seed", {})[path] = {
                "handoff": handoff, "feedback": feedback,
                "iteration": lp.get("iteration", 0), "reason": reason,
                "roles": list(roles)}
        save_state()

    for role in roles:
        sessions.stop(path, role, pid=pid_of(path, role))
        retire_sessions(path, role)
    prune_sessions()
    time.sleep(2)

    pconf = store.project_config(CFG, path)
    started = []
    for role in roles:
        chain = pconf["chains"].get(role) or []
        req = chain[0] if chain else None
        try:
            note_launch(path, role, "handover: %s" % reason)
            pid = sessions.launch(
                path, role, model=models.resolve(req, store.load_models()),
                permission_mode=mode_for(path, role),
                disallow=disallow_for(path, role),
                autocompact_pct=compact_pct(path))
            reg_pid(path, role, pid, model_req=req,
                    autocompact=compact_pct(path))
            started.append(role)
        except Exception as exc:
            notify("crash", "%s: handover could not start the %s: %s"
                   % (name, role, exc), path=path)
    store.journal("rotation", "Handover (%s): new %s"
                  % (reason, " and ".join(started)),
                  name, roles[0] if roles else "executor", "log",
                  project_dir=path)
    many = len(started) > 1
    kept = [r for r in ("executor", "planner") if r not in roles]
    notify("rotation_name",
           "%s: handing over the %s (%s). %s - press 1 for the "
           "development-channels dialog and the work is picked up on its "
           "own; the handoff and the name '%s' are already loaded.%s"
           % (name, " and ".join(started) if started else "session", reason,
              "%d new windows are opening" % len(started) if many
              else "One new window is opening", title,
              (" The %s keeps working and is not touched."
               % " and ".join(kept)) if kept else ""), path=path)
    return {"ok": True, "started": started, "title": title}


def resume_after_handover(path, role):
    """Called once a fresh session registers. When both are up, the new
    executor is handed the thread through the channel, which is what
    actually restarts the work - a seeded session with nobody talking to
    it would just sit there."""
    path = norm(path)
    with _lock:
        hv = (STATE.get("handover") or {}).get(path)
        if not hv:
            return
        waiting = [r for r in hv.get("waiting", []) if r != role]
        hv["waiting"] = waiting
        save_state()
        if waiting:
            return
        replaced = list(hv.get("roles") or [])
        STATE["handover"].pop(path, None)
        save_state()
    if "executor" not in replaced:
        # the planner alone was replaced; the executor never stopped working,
        # so handing it "pick up where you left off" would interrupt it
        store.journal("rotation", "Planner handover complete - the executor "
                      "was left alone", project_name(path), "planner", "log",
                      project_dir=path)
        notify("rotation_name", "%s: the planner is on a fresh session and "
               "has the handoff." % project_name(path), level="silent", path=path)
        return
    seed = (STATE.get("seed") or {}).get(path) or {}
    text = seed.get("handoff") or store.read_handoff(path)
    body = ("You are picking up where the previous session stopped. The "
            "handoff below is the whole thread - read it and carry on with "
            "the next step in it. Finish your turn when you have something "
            "to report.\n\n%s" % (text or "(no handoff was written)"))
    for _ in range(12):
        if channel_for(path, "executor"):
            break
        time.sleep(5)
    if deliver(path, "executor", body, {"kind": "task"}):
        store.journal("rotation", "Handover complete - the new executor has "
                      "the thread", project_name(path), "executor", "log",
                      project_dir=path)
        if "planner" not in replaced:
            # The planner was not replaced and holds the whole run in its
            # head. It should hear that the hands changed, or the next
            # report will read as if the executor forgot the conversation.
            deliver(path, "planner",
                    "The executor reached the end of its context runway and "
                    "has been replaced by a fresh session (%s). You were not "
                    "replaced - you still hold the thread. The new executor "
                    "has the written handoff and nothing else, so it knows "
                    "the state of the work but not what was said. Keep "
                    "reviewing as before; if something in your recent "
                    "verdicts is not in the handoff, say it again in your "
                    "next one."
                    % (seed.get("reason") or "runway spent"),
                    {"kind": "info"})
        notify("rotation_name", "%s: handover done, the new pair is working."
               % project_name(path), level="silent", path=path)
    else:
        notify("needs_you",
               "%s: the new executor came up but its channel never did, so "
               "the handoff could not be handed over. Paste "
               "bridge-logs/<date>/handoff/current.md into its window."
               % project_name(path), path=path)


# ---------------------------------------------------------------------------
# the loop

# A turn that did nothing, and how many in a row end the run.
#
# The loop has no idle gear. When the executor has nothing to do it still
# finishes a turn, the Stop hook still makes a report of it, the planner
# still answers, and a "continue" verdict is injected into the executor -
# which wakes it for another empty turn. One pair span like that all
# night: reports 576 to 581 in three minutes, "Standing by." answered
# "Standing by.", burning the plan limits of both halves around the clock.
#
# The instructions were not the fix. Neither was the "wait" verdict, which
# already delivers nothing and lets the session go idle - the planner was
# answering "continue", and continue is the verdict that wakes.
#
# So the loop counts empty turns and stops itself. What counts as empty is
# deliberately crude: a report with almost nothing in it, from a project
# with no tracked process running. A real report is paragraphs; a session
# that kicked off a build has a process on record. Neither can be spun.
# Idle is not silence. Once the pair is recognised as idling, the Stop hook
# is simply held instead of being answered - the hook can wait, that is what
# it is for - and released when work arrives or when the hold is up. So the
# pair still checks in, about twice an hour instead of twice a minute, and a
# task or a human wakes it the moment it arrives. The loop stays ON
# throughout: switching it off would also switch off the handover decision,
# which lives behind the same "is the loop active" guard.
#
# 1200s sits under both timeouts with room to spare - the client gives the
# hook 1500s and the hook itself is allowed 1800s - so the hold always ends
# on the bridge's terms and never by something timing out underneath it.
IDLE_TURN_CHARS = 120
IDLE_SPIN_LIMIT = 3
IDLE_HOLD_SEC = 1200

# path -> Event, set the moment work arrives so a held hook stops waiting.
IDLEWAIT = {}


def wake_idle(path):
    ev = IDLEWAIT.get(norm(path))
    if ev:
        ev.set()


def trivial_report(path, msg):
    """Did this turn do anything worth carrying?

    Both halves have to be empty, not just one. A short report on its own
    is a perfectly good report - "picked up the handoff, carrying on" is
    six words and means something - and treating length alone as emptiness
    held a pair that was working. What has no content is the exchange:
    a report that says nothing answered by a verdict that says nothing,
    which is what "Standing by." was, six hundred times.

    A tracked process settles it outright: a session that kicked off a
    build has not been idling, whatever it wrote.
    """
    path = norm(path)
    if PROCTRACK.get(path):
        return False
    if len(" ".join((msg or "").split())) > IDLE_TURN_CHARS:
        return False
    said = (STATE.get("last_feedback") or {}).get(path) or ""
    return len(" ".join(said.split())) <= IDLE_TURN_CHARS


def note_spin(path, msg):
    """Count consecutive empty turns; any real one resets it."""
    path = norm(path)
    with _lock:
        spins = STATE.setdefault("idle_spin", {})
        spins[path] = (spins.get(path, 0) + 1) if trivial_report(path, msg) \
            else 0
        save_state()
        return spins[path]


def clear_spin(path):
    """Work arrived, so the pair is not idling any more."""
    with _lock:
        if (STATE.get("idle_spin") or {}).pop(norm(path), None):
            save_state()


# ---- the verdict gate -----------------------------------------------------
#
# A rule kept in a document is read once at session start and then competes
# with the task for attention; a rule kept in the daemon is in the way of the
# action every single time. This is that second kind. `done` and `stop` are
# the two verdicts that ACCEPT work, and neither is taken unless the planner
# says what it opened - and unless the bridge can find those files itself.
#
# Checking existence is the whole point. Requiring a "Checked:" line only
# asks for a sentence, and a sentence is free; requiring paths that resolve
# turns "I checked it" from something said into something done, and turns a
# false claim from carelessness into a plain forgery - one the daemon names
# out loud, by filename, at the moment it is attempted.
#
# `continue` and `wait` are deliberately outside the gate. They accept
# nothing: continue asks for more work, wait says a process is still running.
# Gating them would only make the loop expensive to run.

CHECKED_MARKS = ("checked:",)

# The named exit. There are real tasks with nothing openable at the end - a
# question answered, a piece of reasoning, a refusal. Pretending otherwise
# would just teach the pair to fabricate a path, which is worse than the
# thing being prevented.
NOART_MARKS = ("no artifacts",)

# Why 40 characters and five words, and why that is not a loophole:
# the length is not the deterrent. "n/a", "nothing to attach", "an "
# "analysis" are
# unfalsifiable - a human reading the journal a week later cannot tell a real
# analysis turn from a skipped check. Forty characters is about what it takes
# to name BOTH what the work was and why it produced nothing to open, which
# is the minimum a later reader needs to call it out. What actually makes the
# exit expensive is that it cannot be taken quietly: every use is journalled
# at "warn", counted per project in /state, and marked in the feed. A pair
# that leans on it leaves a visible column of them.
NOART_MIN_CHARS = 40
NOART_MIN_WORDS = 5

# A token is only worth testing as a path if it carries a separator or is
# absolute. Prose is full of things that look like paths and are not - "z9/z10",
# "1/2", "and/or" - and an early version that tested every slash-bearing
# refused a perfectly good verdict because it could not find a folder called
# "z9". So: a candidate that does not exist is a REFUSAL only when it also
# looks unmistakably like a path (absolute, or carrying a file extension, or
# ending in a separator). Anything else is prose and is ignored.
_TOKEN = re.compile(r"[^\s,;'\"()\[\]{}<>«»]+")
_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2}\w)")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,6}$")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif",
              ".tiff", ".mp4", ".mov", ".exr", ".tga")


def artifact_paths(text, project_dir=""):
    """Split the path-like tokens of a text into the ones that exist and the
    ones that were named but are not there.

    Returns (found, dead). A relative path is resolved against the project,
    because that is how a planner writes it.
    """
    found, dead = [], []
    base = project_dir or ""
    for raw in _TOKEN.findall(text or ""):
        tok = raw.strip("`*.,:;")
        if not tok:
            continue
        absolute = bool(_ABS.match(tok))
        bare = not absolute and "/" not in tok and "\\" not in tok
        cand = tok if absolute or not base else os.path.join(base, tok)
        try:
            here = os.path.exists(cand)
        except Exception:
            here = False
        if here:
            found.append(tok)
            continue
        # A token with no directory part is only ever counted when it
        # EXISTS - never held against the writer. "§5.17", "2.1.232" and
        # "5.104" all satisfy any reasonable "looks like a filename" test,
        # and an earlier cut of this refused a sound verdict because it
        # could not find a file called "5.17". A missing artefact is only
        # called missing when the writer plainly meant a path.
        if bare:
            continue
        if absolute or _EXT.search(tok) or tok[-1] in "\\/":
            dead.append(tok)
    return found, dead


# ---- the bilingual vocabulary --------------------------------------------
#
# Everything the bridge MATCHES ON rather than prints. Both spellings are kept
# deliberately: this bridge is run by a Russian-speaking pair, and removing the
# aliases would change behaviour for work already in flight. Every name here
# is exempt in check_public.py by name, on its defining line and nowhere else,
# so a quotation that merely mentions one of them is still caught.

DEBT_CLOSED_WORD = r"^\s*closed"
RESIDENCE_TEST_WORDS = r"(test|case)\w*\s*\S"
FRAMES_MARKS = ("[FRAMES]",)


# ---- the debt register ----------------------------------------------------
#
# The other half of the same lesson. On one watched project every workaround
# "a lawful exception for today", and one at a time forty-five of them piled
# up. None of them was wrong on its own; what was wrong was that nothing
# counted them, so nobody could see the pile until it was the whole system.
#
# So a temporary solution is allowed - and has to be DECLARED. The executor
# writes one line in its report:
#
#     Debt: <what is temporary> - <what closes it>
#
# and the bridge writes it into <project>/bridge-logs/DEBT.md, counts it in
# /state and shows it in the panel. It does not block anything: blocking
# would only teach the pair to stop saying the word. What it does is make
# the pile impossible to not see, and it stays open until something says
#
#     Debt closed: <what> - <what closed it>
#
# The register is rendered from state rather than appended to, so the file
# and the counter cannot disagree; the journal keeps the history either way.

DEBT_OPEN = re.compile(r"^\s*debt\s*:\s*(.+)$", re.I | re.M)
DEBT_CLOSE = re.compile(r"^\s*debt closed\s*:\s*(.+)$", re.I | re.M)


def _debt_split(line):
    """"<what> — <how it gets closed>" into its two halves."""
    for dash in ("—", " - ", "–", "--"):
        if dash in line:
            what, _, how = line.partition(dash)
            return " ".join(what.split()), " ".join(how.split())
    return " ".join(line.split()), ""


def debt_rows(path):
    return (STATE.get("debt") or {}).get(norm(path)) or []


def open_debt(path):
    return [d for d in debt_rows(path) if not d.get("closed")]


def write_debt_file(path):
    """Render the register from state. Never raises - an edge path."""
    rows = debt_rows(path)
    if not rows:
        return ""
    out = ["# Debt", "",
           "Temporary solutions the executor declared. An open line "
           "blocks nothing,", "and is put out only by an explicit "
           "closing.", ""]
    live = [d for d in rows if not d.get("closed")]
    out.append("Open: **%d** of %d." % (len(live), len(rows)))
    out.append("")
    for d in rows:
        out.append("- %s **%s**" % ("[ ]" if not d.get("closed") else "[x]",
                                    d.get("what") or "(no description)"))
        if d.get("how"):
            out.append("      closed by: %s" % d["how"])
        if d.get("closed"):
            out.append("      closed %s: %s" % (d.get("closed_at") or "",
                                                 d.get("closed_by") or ""))
        out.append("      declared %s, iteration %s"
                   % (d.get("at") or "", d.get("iteration") or "?"))
    text = "\n".join(out) + "\n"
    try:
        d = os.path.join(path, "bridge-logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "DEBT.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    return text


def note_debt(path, project, msg, iteration=None):
    """Pick the debt lines out of a report and record them."""
    path = norm(path)
    opened, closed = [], []
    for line in DEBT_OPEN.findall(msg or ""):
        if re.match(DEBT_CLOSED_WORD, line, re.I):
            continue                  # "Debt closed:" is the other one
        what, how = _debt_split(line)
        if not what:
            continue
        opened.append({"what": what, "how": how, "at": now(),
                       "iteration": iteration, "closed": False})
    for line in DEBT_CLOSE.findall(msg or ""):
        what, how = _debt_split(line)
        closed.append((what, how))
    if not opened and not closed:
        return 0, 0
    with _lock:
        rows = STATE.setdefault("debt", {}).setdefault(path, [])
        rows.extend(opened)
        shut = 0
        for what, how in closed:
            key = what.lower()[:40]
            for d in rows:
                if not d.get("closed") and key and key in \
                        (d.get("what") or "").lower():
                    d["closed"] = True
                    d["closed_at"] = now()
                    d["closed_by"] = how
                    shut += 1
                    break
        save_state()
    write_debt_file(path)
    live = len(open_debt(path))
    for d in opened:
        store.journal("debt", "DEBT declared: %s (closed by: %s). %d open on "
                              "this project." % (d["what"], d["how"] or "not "
                                                 "said", live),
                      project, "executor", "warn", project_dir=path)
    if opened:
        notify("verdict_changes",
               "%s: a temporary solution was declared - %s (%d open)"
               % (project, brief(opened[0]["what"], 160), live), path=path)
    if shut:
        store.journal("debt", "DEBT closed: %d line%s, %d still open"
                      % (shut, "" if shut == 1 else "s", live),
                      project, "executor", "log", project_dir=path)
    return len(opened), shut


# ---- residence: where does the fix LIVE ----------------------------------
#
# This came from a watched project, 2026-08-18, and the shape is the same as
# everything else here: a rule that lives only in prose is obeyed until it is
# inconvenient. There the rule "everything is produced by the pipeline" had
# gates for the QUALITY of a patch and no gate at all asking whether the
# pipeline reproduces it. Forty-five patch steps accumulated, eighteen of them
# pure carry-over, each one a lawful exception on the day it was made. A
# replay script then rebuilt the stack of patches byte for byte, and that was
# taken for reproducibility - it was documentation of the patchwork, not one
# pass. The planner said so itself afterwards: it had been checking the
# gates and the crops, and never asking where the fix lived.
#
# So when a report says code changed, accepting it costs one more line: WHERE
# the fix lives. Not "it works" - "it lives in this file, this function, this
# test". A fix nobody can point at is a patch by definition.
#
# The detector is deliberately conservative. It fires on a NAMED source file,
# a diff marker or a commit - never on words like "fixed it" alone. Two
# reasons: a false demand teaches the pair to write a meaningless residence
# line to get past it, and when it does fire there is already a filename in
# the report, so the answer is there to be given rather than invented.

RESIDENCE_MARKS = ("residence:",)

_CODE_EXT = (".py", ".html", ".js", ".jsx", ".ts", ".tsx", ".css", ".gd",
             ".bat", ".sh", ".ps1", ".c", ".cpp", ".h", ".hpp", ".java",
             ".go", ".rs", ".rb", ".php", ".sql", ".json", ".yml", ".yaml",
             ".toml", ".ini", ".cfg", ".gdshader", ".glsl", ".shader")
_DIFF = re.compile(r"^(@@ |\+\+\+ |--- |diff --git )", re.M)
_COMMIT = re.compile(r"commit\w*\s+`?[0-9a-f]{6,40}`?", re.I)


def touched_code(text):
    """Does this report say that code changed? Conservative on purpose."""
    t = text or ""
    if _DIFF.search(t) or _COMMIT.search(t):
        return True
    for raw in _TOKEN.findall(t):
        tok = raw.strip("`*.,:;()").lower()
        if tok.endswith(_CODE_EXT):
            return True
    return False


def residence_ok(text):
    """Does the "Residence:" line say where the fix lives?

    "Residence: yes" is not an answer. What counts is a place a reader
    can open: a named source file, a file:function or module::name pair,
    or a named test.
    """
    low = (text or "").lower()
    tail = ""
    for mark in RESIDENCE_MARKS:
        i = low.find(mark)
        if i >= 0:
            tail = text[i + len(mark):]
            break
    line = tail.split(chr(10))[0].strip()
    if len(" ".join(line.split())) < 8:
        return False
    for raw in _TOKEN.findall(line):
        tok = raw.strip("`*.,:;()")
        if tok.lower().endswith(_CODE_EXT):
            return True
        if re.match(r"^[\w./\-]+::?[\w.]+$", tok):
            return True
        # A dotted chain of identifiers - store.norm, daemon.verdict_gate -
        # is a place a person can open even though the bridge cannot check
        # it. Segments must be identifiers, which is what keeps "2.1.232"
        # and "5.17" out: a version is not a residence.
        if re.match(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+$", tok):
            return True
    return bool(re.search(RESIDENCE_TEST_WORDS, line, re.I))


def verdict_gate(path, verdict, feedback):
    """May this verdict be taken? Returns (ok, why, kind).

    kind is "artifacts" when real files were named and found, "none" when the
    named exit was used, and None when the verdict does not accept work and
    was never gated. `why` is written to be read by the planner as an
    instruction, not as a complaint: it says exactly what is missing.
    """
    v = (verdict or "").lower()
    # "wait" is the only verdict that judges nothing - it says a process is
    # still running. Everything else carries an assessment, and an
    # assessment made on the executor's word is acceptance by hearsay.
    #
    # This started with done and stop, and another pair found the gap by
    # falling into it: their planner checked that a receipt EXISTED and then
    # passed judgement on the substance from the report, in a "continue" -
    # which the gate let through, because a gate on the accepting verdicts
    # only is a gate with a door beside it. "continue" is where most of the
    # judging actually happens.
    if v == "wait" or v not in ("done", "stop", "continue"):
        return True, "", None
    text = feedback or ""
    low = text.lower()
    if not any(m in low for m in CHECKED_MARKS):
        return False, (
            ("This verdict accepts work, so it needs a Checked: block "
             if v in ("done", "stop") else
             "This verdict passes judgement, so it needs a Checked: block ") + "saying what you opened yourself. Write the "
            "paths to the artefacts - a log, a folder of run output, a "
            "render, a file you looked at - and the bridge will check they "
            "exist. If this piece genuinely has nothing to open, write "
            "Checked: no artifacts - <reason> with a real reason (at "
            "least %d characters and %d words); every one of those is "
            "logged and counted where the human can see it.\n\n"
            "Only 'wait' is free of this, because it judges nothing - it "
            "says a process is still running. Everything else carries an "
            "assessment, and an assessment made on the executor's word is "
            "acceptance by hearsay: it reads as review and is not one."
            % (NOART_MIN_CHARS, NOART_MIN_WORDS)), None
    # The report this verdict answers is the evidence for whether code
    # changed - not the verdict's own wording. A planner cannot talk its way
    # out of the requirement by describing the work differently, and cannot
    # be caught by it when the executor did no code at all.
    waiter = PENDING.get(norm(path)) or {}
    report = waiter.get("content") or ""
    if v in ("done", "stop") and report and touched_code(report)             and not residence_ok(text):
        return False, (
            "This report changed code, and accepting a code change takes one "
            "more line than Checked: - where the fix LIVES. Write "
            "Residence: <file:function> or the name of the test that now "
            "holds it.\n\nThis is not paperwork. A fix nobody can point at "
            "is a patch: it works today, the next full run does not produce "
            "it, and the next person finds the symptom back with no record "
            "of what was done. Forty-five such steps piled up on another "
            "project before anyone asked the question - eighteen of them "
            "pure carry-over, every one a reasonable exception on the day it "
            "was made.\n\nIf the change genuinely lives nowhere - a "
            "throwaway probe, a one-off measurement - then it is not "
            "something to accept with 'done': say so in the feedback and "
            "answer 'continue'."), None
    tail = ""
    for mark in CHECKED_MARKS:
        i = low.find(mark)
        if i >= 0:
            tail = text[i + len(mark):]
            break
    tail_low = tail.lower()
    if any(m in tail_low for m in NOART_MARKS):
        reason = tail
        for m in NOART_MARKS:
            j = reason.lower().find(m)
            if j >= 0:
                reason = reason[j + len(m):]
                break
        reason = reason.lstrip(" —-–:,").strip()
        reason = " ".join(reason.split())
        if len(reason) < NOART_MIN_CHARS or len(reason.split()) < NOART_MIN_WORDS:
            return False, (
                "no artifacts needs a reason a person can weigh later: "
                "at least %d characters and %d words saying what the work "
                "was and why it produced nothing to open. Yours was %d "
                "characters and %d words. This exit is counted and shown to "
                "the human every time it is used."
                % (NOART_MIN_CHARS, NOART_MIN_WORDS,
                   len(reason), len(reason.split()))), None
        return True, reason, "none"
    found, dead = artifact_paths(tail, path)
    if dead:
        return False, (
            "The Checked: block names %d path%s that %s not on disk: %s. "
            "A path that is not there is not a check. Either give the real "
            "ones, or say Checked: no artifacts - <reason>."
            % (len(dead), "" if len(dead) == 1 else "s",
               "is" if len(dead) == 1 else "are", ", ".join(dead[:5]))), None
    if not found:
        return False, (
            "The Checked: block names nothing the bridge can find. Put "
            "the actual paths in it - a folder of run artefacts, a log, a "
            "render - resolving either from the project directory "
            "(out/run-2026-01-01/handover.txt) or absolutely "
            "(C:\\path\\to\\project\\...). A description of what you looked at is not "
            "a path. If there is genuinely nothing to open, say "
            "Checked: no artifacts - <reason>."), None
    return True, ", ".join(found[:6]), "artifacts"


def note_no_artifacts(path, project, reason):
    """A verdict accepted on the named exit. Loud on purpose."""
    with _lock:
        counts = STATE.setdefault("noart", {})
        counts[norm(path)] = counts.get(norm(path), 0) + 1
        n = counts[norm(path)]
        save_state()
    store.journal("verdict_no_artifacts",
                  "Accepted with NO ARTEFACTS (%d so far for this project). "
                  "Reason given: %s" % (n, brief(reason, 300)),
                  project, "planner", "warn", project_dir=path)
    notify("verdict_changes",
           "%s: a piece was accepted with no artefacts to open - %s (that is "
           "%d for this project)" % (project, brief(reason, 200), n),
           path=path)
    return n


# ---- silence is not consent ----------------------------------------------
#
# The night of 2026-08-18/19. The planner's window was restarted by a lost
# connection to the server. Its channel PROCESS stayed up and kept accepting
# deliveries, so every report was handed over successfully - and the session
# behind it never saw one. Thirty-two reports, 41 through 72, over 11.9 hours.
# Not one got a verdict.
#
# What made it invisible rather than loud was one line at the end of
# run_review: `verdict = waiter["verdict"] or "continue"`. A report nobody
# answered resolved as "continue" - so the executor was told to carry on,
# every time, by nobody. Silence read as consent. The human thought the work
# was being accepted; the planner thought there were no reports.
#
# The idle damper did engage - the gaps between reports were about 21 minutes,
# which is its hold - so it made the night QUIETER without making it visible.
# It answers "the pair has nothing to do", and this was "nobody is answering",
# which is a different thing and was not covered by anything.
#
# The threshold: the median gap between unanswered reports that night was 21
# minutes, so three in a row is about an hour of silence. With this in place
# the night would have stopped after three reports and an hour instead of
# thirty-two and twelve.

SILENCE_LIMIT = 3


def silence_limit():
    try:
        n = int((CFG.get("thresholds") or {}).get("silence_limit",
                                                  SILENCE_LIMIT))
    except Exception:
        n = SILENCE_LIMIT
    return max(1, n)


def note_silence(path, project, n):
    """One more report went unanswered. Returns True when the pair was
    stopped by it."""
    path = norm(path)
    with _lock:
        counts = STATE.setdefault("unanswered", {})
        counts[path] = counts.get(path, 0) + 1
        run = counts[path]
        save_state()
    limit = silence_limit()
    if run < limit:
        store.journal("silence", "Report %d got no verdict - %d in a row. At "
                                 "%d the pair stops and asks for you."
                      % (n, run, limit), project, "planner", "warn",
                      project_dir=path)
        return False
    why = ("the planner has not answered %d reports in a row (last was %d). "
           "Nothing is being reviewed, so the pair is held rather than left "
           "to carry on unread." % (run, n))
    pause_project(path, why)
    store.journal("silence", "PAIR HELD: " + why, project, "planner", "warn",
                  project_dir=path)
    notify("needs_you",
           "%s: the planner has answered nothing for %d reports. The pair is "
           "held - work is NOT being reviewed. Check the planner window; the "
           "unanswered reports are in bridge-logs/.../inbox/. Resume from the "
           "panel, or just answer with a verdict." % (project, run),
           path=path)
    return True


def clear_silence(path, project=""):
    """A real verdict arrived. Returns how many reports had gone unanswered,
    so the planner can be told what it missed in one line rather than in a
    flood."""
    path = norm(path)
    with _lock:
        run = (STATE.get("unanswered") or {}).pop(path, 0)
        held = (STATE.get("paused") or {}).get(path) or {}
        was_silence = "has not answered" in (held.get("why") or "")
        save_state()
    if was_silence:
        resume_project(path)
        store.journal("silence", "Planner answered again - the hold is off. "
                                 "%d reports had gone unanswered." % run,
                      project or project_name(path), "planner", "log",
                      project_dir=path)
    return run


def run_review(event, path, lp, msg, project, role):
    """Deliver the report, wait for the verdict. Returns hook_output or None."""
    hook_output = None
    # Held because nobody is answering: do not make another report to add
    # to a pile no one is reading. The hold comes off when a verdict
    # arrives or a person resumes the project.
    _held = (STATE.get("paused") or {}).get(path) or {}
    if "has not answered" in (_held.get("why") or ""):
        store.journal("silence", "Turn finished while the pair is held - no "
                      "report made, nothing is reading them.", project,
                      role, "log", project_dir=path)
        return None
    hold = float(CFG.get("thresholds", {}).get("idle_hold")
                 if CFG.get("thresholds", {}).get("idle_hold") is not None
                 else IDLE_HOLD_SEC)
    # idle_hold = 0 switches the damper off entirely. Suites that
    # drive terse exchanges on purpose - the handover simulation is
    # all two-word reports - set it there rather than being held.
    spin = note_spin(path, msg) if hold else 0
    if hold and spin >= IDLE_SPIN_LIMIT:
        # Held here rather than answered: this is the cheapest point in the
        # circle and it spends no planner turn to be told again what the
        # last two said. The hook waits; the executor is not woken, because
        # nothing is delivered to it; and when the hold ends the turn
        # finishes normally and the pair checks in once. The count is
        # cleared first so that check-in is judged on its own merits - the
        # rhythm itself must not read as the disease.
        ev = IDLEWAIT.setdefault(path, threading.Event())
        ev.clear()
        clear_spin(path)
        store.journal("loop", "Nothing to review for %d turns - holding the "
                              "pair until there is work, or %d minutes, "
                              "whichever comes first. The last turn said: %s"
                      % (spin, int(hold) // 60,
                         brief(msg, 80) or "(nothing)"),
                      project, role, "log", project_dir=path)
        woken = ev.wait(hold)
        store.journal("loop", "Held pair released - %s"
                      % ("work arrived" if woken else
                         "checking in after %d minutes"
                         % (int(hold) // 60)),
                      project, role, "log", project_dir=path)
        return None
    lp["iteration"] += 1
    n = lp["iteration"]
    save_state()

    note = take_note(path)
    # A declared temporary solution is recorded now, whatever the verdict
    # turns out to be: it is the executor's own statement, and a refused
    # verdict must not lose it.
    note_debt(path, project, msg, iteration=n)

    content = "Executor report %d:\n\n%s" % (n, msg)
    # The planner marked this piece as visual, so the report is expected to
    # open with the frames. Saying so at the top of the delivery means the
    # planner can send it back without reading the prose underneath - which
    # is the whole value: the cheapest possible "no, show me" .
    if (STATE.get("frames") or {}).get(path):
        shots = [p for p in artifact_paths(msg, path)[0]
                 if p.lower().endswith(_IMAGE_EXT)]
        if not shots:
            content = ("NO FRAMES. You marked this piece as visual and the "
                       "report names no image or video file that exists on "
                       "disk. Answer 'continue' and ask for them - you have "
                       "not been shown the work.\n\n") + content
        else:
            with _lock:
                (STATE.get("frames") or {}).pop(path, None)
                save_state()
    if note:
        content += "\n\nNote from the human: %s" % note
    meta = {"kind": "report", "report": str(n)}

    waiter = {"event": threading.Event(), "verdict": None, "feedback": "",
              "content": content, "meta": meta}
    PENDING[path] = waiter

    sent, why = deliver_ex(path, "planner", content, meta)
    if not sent:
        QUEUED.setdefault(path, []).append(
            json.dumps({"content": content, "meta": meta}))
        if why == "absent":
            ensure_session(path, "planner", "no planner channel for report "
                                            "%d" % n)
        else:
            store.journal("channel", "Report %d held: the planner channel is "
                          "up but did not take it - not opening a window"
                          % n, project, role, "log", project_dir=path)
        if time.time() - LAUNCHED.get((path, "planner"), 0) > 150:
            inbox = store.inbox_write(path, n, content)
            # The report itself stays on disk and out of the chat. It used
            # to follow this message in full, up to 3500 characters of it,
            # which with one pair was merely long and with several is a
            # wall - and the one line that needs answering scrolls away
            # above it. What goes out is where the report is and what to
            # reply; the text is in the inbox file named here.
            notify("needs_you",
                   "%s: the planner channel never came up (channels may be "
                   "gated on this account). Report %d saved to %s. Answer in "
                   "the planner chat, then send:\n"
                   "/verdict continue <feedback> | /verdict done | /verdict wait"
                   % (project, n, inbox), path=path)

    store.journal("loop", "Report %d %s" % (
        n, "sent to planner" if sent else "queued"), project, role, "log",
        project_dir=path)
    store.iteration_file(path, n, "executor", msg)
    store.dialogue(path, "%s  %s / executor - report %d" % (now(), project, n),
                   msg)

    timeout = float(CFG.get("thresholds", {}).get("review_timeout", 1200))
    warn_after = float(CFG.get("thresholds", {}).get("channel_silence_warn",
                                                    240))
    got = waiter["event"].wait(min(warn_after, timeout))
    if not got and sent:
        # The delivery to the channel process succeeded, yet the planner has
        # not reacted. On this Claude Code version inbound notifications from
        # a bare "server:" channel can be dropped silently after the dev-flag
        # dialog, so do not sit here for twenty minutes: hand the report over
        # by the routes that always work, and keep listening in case the
        # channel comes through after all.
        inbox = store.inbox_write(path, n, content)
        store.journal("loop", "Report %d delivered to the channel but the "
                      "planner has not answered in %d s - sent it out the "
                      "fallback way too" % (n, int(warn_after)), project,
                      role, "log", project_dir=path)
        # As above: the alert goes out, the report stays in the inbox file.
        notify("needs_you",
               "%s: report %d reached the planner's channel process, but the "
               "session has not reacted. If the planner window shows nothing, "
               "its inbound channel messages are being dropped (known Claude "
               "Code issue with bare server channels) - the report is in %s. "
               "Answer in the planner chat, then send:\n"
               "/verdict continue <feedback> | /verdict done | /verdict wait"
               % (project, n, inbox), path=path)
        got = waiter["event"].wait(max(0.0, timeout - warn_after))
    PENDING.pop(path, None)

    if not got:
        notify("needs_you",
               "%s: the planner has not answered report %d in %d min. The "
               "loop is holding - look at the planner's window."
               % (project, n, int(timeout // 60)), path=path)
        return None

    # A report nobody answered used to resolve as "continue" - the executor
    # told to carry on, every time, by nobody. That is what made the night
    # of 2026-08-18/19 invisible instead of loud. Silence is now counted,
    # and after silence_limit reports in a row the pair is held.
    answered = waiter["verdict"] is not None
    if not answered:
        note_silence(path, project, n)
    else:
        missed = clear_silence(path, project)
        if missed:
            store.journal("silence", "Planner is back. %d report%s had gone "
                          "unanswered while it was away - they are in "
                          "bridge-logs/.../inbox/."
                          % (missed, "" if missed == 1 else "s"),
                          project, "planner", "warn", project_dir=path)
    verdict = waiter["verdict"] or "continue"
    feedback = waiter["feedback"] or ""
    store.iteration_file(path, n, "planner",
                         "verdict: %s\n\n%s" % (verdict, feedback))
    store.dialogue(path, "%s  %s / planner - verdict: %s"
                   % (now(), project, verdict), feedback)
    store.index_append(path, n, msg.splitlines()[0][:90] if msg else "",
                       verdict)
    with _lock:
        STATE.setdefault("last_feedback", {})[path] = feedback[:600]
        save_state()
    git_commit_iteration(path, n, verdict)

    if verdict == "stop":
        deactivate_loop(path, "the planner called the whole job finished on "
                              "iteration %d" % n)
        notify("run_finished", "%s: the planner says the job is finished "
               "after %d iterations, so the loop is off. Start it again when "
               "you have more for the pair." % (project, n), path=path)
    elif verdict == "done":
        # Accepting a piece of work is not the end of the run. It used to
        # switch the loop off, which meant one satisfied review ended a
        # night: the pair kept talking, the bridge stopped carrying reports,
        # and nothing said so until someone looked. A finished piece means
        # the next piece is due, and the planner is the half that knows what
        # it is - so it is asked, and the loop stays on.
        store.journal("loop", "Iteration %d accepted - asking the planner "
                      "what comes next" % n, project, "planner", "log",
                      project_dir=path)
        notify("verdict_changes", "%s: iteration %d accepted. The loop stays "
               "on; the planner is being asked for the next piece."
               % (project, n), level="silent")
        threading.Timer(1.5, deliver, args=(
            path, "planner",
            "You accepted iteration %d. The loop is still on, so the "
            "executor is waiting for its next piece of work. Give it one "
            "with the task tool. If there is genuinely nothing left to do, "
            "answer the next report with the 'stop' verdict instead and the "
            "loop will be switched off." % n,
            {"kind": "info"})).start()
    elif verdict == "wait":
        touch_session(event, state="waiting on a process")
        notify("waiting_process", "%s: planner says wait - %s"
               % (project, feedback[:200]))
    else:
        notify("verdict_changes", "%s iteration %d: %s"
               % (project, n, feedback[:200]))
        body = ("The planner reviewed report %d. Its findings (facts about "
                "the work, act on them):\n%s" % (n, feedback[:9000]))
        # Two ways to hand the verdict back, and the choice matters for an
        # all-night run: keeping the turn alive from this hook is capped at
        # eight consecutive continuations, after which the session simply
        # stops and waits for a human. Channel injection has no such cap and
        # wakes an idle session just as well, so it is the primary path -
        # delivered a moment after this hook returns, once the turn has
        # actually ended. additionalContext stays as the fallback for when
        # the executor has no channel.
        if channel_for(path, "executor"):
            threading.Timer(1.5, deliver,
                            args=(path, "executor", body,
                                  {"kind": "verdict"})).start()
            with _lock:
                STATE.setdefault("awaiting", {})[path] = {
                    "since": time.time(), "iteration": n,
                    "body": body, "nudges": 0}
                save_state()
        else:
            hook_output = {"hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": body}}
    return hook_output


# ---------------------------------------------------------------------------
# hook events

def handle_event(event):
    name = event.get("hook_event_name", "?")
    project = project_name(event)
    role = event.get("role") or "session"
    path = norm(event.get("project_dir") or event.get("cwd"))
    setting, phrase = EVENT_MAP.get(name, (None, name))
    text = None
    hook_output = None

    if not managed(event.get("role")):
        # Someone else's window - or one of ours whose BRIDGE_ROLE did not
        # survive the trip. It is counted so the panel can say it is there,
        # and then handled exactly as before: recorded, its hooks answered,
        # its numbers taken. It is hidden in the panel and never planned for
        # or handed over, and that is the whole difference.
        #
        # This used to return here, which dropped the session at the door -
        # no record, no Stop hook, no telemetry. A window whose role failed
        # to arrive went from oddly labelled to invisible, and the loop
        # stopped for it. Hiding something is a display decision; it does
        # not belong on the intake path.
        note_stranger(path, event.get("session_id"))

    if name == "Stop":
        with _lock:
            hist = (STATE.get("compactions") or {}).get(
                "%s|%s" % (norm(path), role))
            if hist and not hist[-1].get("after") \
                    and hist[-1].get("session") in (
                        None, "", event.get("session_id")):
                # ...and only from the session that did the compacting. A
                # replacement session's first small size was being written
                # in as "what the previous session was left with", which is
                # where the measured 88% floor came from - a number about
                # one session recorded as a measurement of another.
                now_tokens = (STATE.get("sessions", {})
                              .get(session_key(event), {})
                              .get("context_tokens"))
                if now_tokens and hist[-1].get("tokens") \
                        and now_tokens < hist[-1]["tokens"]:
                    hist[-1]["after"] = int(now_tokens)
            STATE.pop("woke:%s" % path, None)
            STATE.pop("cut_for_handover:%s" % path, None)
            (STATE.get("awaiting") or {}).pop(path, None)
            if event.get("stop_hook_active"):
                STATE["continuations"] = STATE.get("continuations", 0) + 1
            else:
                STATE["continuations"] = 0
            save_state()
        msg = (event.get("last_assistant_message") or "").strip()
        first = msg.splitlines()[0][:200] if msg else ""
        sess = touch_session(event, last_turn=first, state="idle")
        store.transcript_copy(event.get("session_id"),
                              event.get("transcript_path"), project_dir=path)
        remap_archive(path, "a turn was archived")
        text = "%s / %s finished a turn.\n%s" % (project, role, first)

        if role == "executor":
            with _lock:
                last = sess.get("last_stop_tokens") or 0
                cur = sess.get("context_tokens") or 0
                # A turn that straddled a compaction gives a meaningless
                # difference: the context went up by the turn and then down
                # by the summary, and what is left is neither. The sample is
                # dropped rather than averaged in - this number sets the
                # margin that decides when the session is replaced.
                straddled = bool(sess.pop("compacted_this_turn", None))
                if cur and last and cur > last and not straddled:
                    hist = sess.setdefault("turn_costs", [])
                    hist.append(cur - last)
                    del hist[:-5]
                sess["last_stop_tokens"] = cur
                save_state()

        _, lp = loop_state(path)
        if role == "executor" and not lp.get("active") and msg:
            nudge_loop_off(path, role,
                           "the executor finished a turn and no one reviewed "
                           "it")
        if role == "executor" and lp.get("active") and msg:
            if paused_for(path):
                store.journal("loop", "Paused - report held", project, role,
                              "log", project_dir=path)
            else:
                chk = context_check(sess, path)
                warn = CFG["thresholds"].get("rotate_at", 90)
                pct = sess.get("context_pct") or 0
                policy = store.project_config(CFG, path).get("rotate_policy",
                                                             "compact")
                wv = wall_view(sess, path)
                plan = plan_for(sess, path)
                cycle_spent = plan["do"] == "handover"
                if cycle_spent and (STATE.get("handover") or {}).get(path):
                    # One is already under way. The rule is a property of the
                    # session, not of the moment, so it stays true at every
                    # turn boundary until the replacement arrives - and
                    # without this it would start another handover each time.
                    cycle_spent = False
                if cycle_spent:
                    # Only the session whose numbers said so is replaced.
                    # The other half of the pair has its own context, its
                    # own floor and its own cycle; replacing it because its
                    # partner ran out throws away a working session and
                    # opens a window nobody asked for.
                    roles_to_go = ("executor",)
                    log_handover_decision(path, "executor", sess, plan, wv)
                    blocked = handover_blocked(path, roles_to_go)
                    if blocked:
                        # the turn stays alive: a stopped session with no
                        # replacement is the worse of the two outcomes
                        if not STATE.get("hoheld:%s" % path):
                            with _lock:
                                STATE["hoheld:%s" % path] = True
                                save_state()
                            store.journal("rotation", "Handover was due at "
                                          "%d%% but could not run: %s"
                                          % (wv["pct_of_limit"], blocked),
                                          project, role, "sound",
                                          project_dir=path)
                            notify("needs_you",
                                   "%s: the executor is at %d%% of the way "
                                   "to %s and a handover is due, but it "
                                   "cannot run - %s The session is being "
                                   "left to work rather than stopped with "
                                   "nothing to replace it."
                                   % (project, wv["pct_of_limit"],
                                      wv["kind"], blocked), path=path)
                    else:
                        hook_output = {
                            "continue": False,
                            "stopReason": "Bridge: %s Handing over to a fresh "
                                          "executor." % plan["why"]}
                        with _lock:
                            STATE["cut_for_handover:%s" % path] = True
                            save_state()
                        threading.Thread(
                            target=handover,
                            args=(path, plan["why"], roles_to_go),
                            daemon=True).start()
                        tight = False
                        return hook_output, text
                tight = bool(chk) and (chk.get("headroom", 1) < 0
                                       or pct >= warn)
                if tight and policy == "compact":
                    # Keep the handoff current and say so once, then carry on:
                    # compaction is free and automatic, rotation is neither.
                    mechanical_handoff(path, lp, extra_note="context tight")
                    if not STATE.get("tight:%s" % path):
                        with _lock:
                            STATE["tight:%s" % path] = True
                            save_state()
                        store.journal("loop", "Context is tight (%d%%) - "
                                      "letting the session compact; handoff "
                                      "is current" % pct, project, role,
                                      "log", project_dir=path)
                    tight = False
                if tight:
                    hook_output = {"continue": False,
                                   "stopReason": "Bridge: context ceiling - "
                                                 "rotating this session."}
                    threading.Thread(target=rotate_executor,
                                     args=(path, "headroom spent at %d%%"
                                           % pct), daemon=True).start()
                else:
                    hook_output = run_review(event, path, lp, msg, project,
                                             role)
                    mechanical_handoff(path, lp)
                    calib_clean((sess.get("model") or "?").lower(), path)

    elif name == "Notification":
        msg_n = event.get("message", "waiting for input")
        low_n = msg_n.lower()
        # "waiting for your input" is Claude Code saying the session went
        # idle at an empty prompt - that is a fact about the session, not a
        # question aimed at you. Only a real prompt earns the alarm.
        idle_notice = ("waiting for your input" in low_n
                       or "waiting for input" in low_n)
        wants_you = (not idle_notice) and any(k in low_n for k in (
            "permission", "approve", "approval", "confirm", "attention",
            "choose", "select", "y/n"))
        if idle_notice:
            _, lp_i = loop_state(path)
            touch_session(event, state="idle")
            if not lp_i.get("active"):
                nudge_loop_off(path, role, "the %s is idle" % role)
            store.journal("Notification", "%s / %s is idle at the prompt"
                          % (project, role), project, role, "log",
                          project_dir=path if os.path.isdir(path) else None)
            refresh_pin()
            return None, None
        if not wants_you:
            # things like "login successful" are news, not a question - they
            # must not leave the panel stuck on "waiting on you"
            touch_session(event)
            store.journal("Notification", "%s / %s: %s"
                          % (project, role, msg_n), project, role, "log",
                          project_dir=path if os.path.isdir(path) else None)
            refresh_pin()
            return None, None
        sess = touch_session(event, state="needs you")
        with _lock:
            sess["needs_you_at_tokens"] = sess.get("context_tokens") or 0
            save_state()
        text = "%s / %s needs you: %s" % (project, role, msg_n)

    elif name == "StopFailure":
        etype = (event.get("error_type") or "unknown").lower()
        sess = touch_session(event, state="error")
        ref = sess if sess.get("model") else best_session(path, role)
        text = "%s / %s stopped with an error: %s" % (project, role, etype)
        model = (ref.get("model") or "?").lower()
        if "invalid" in etype or "context" in etype:
            store.calib_update(model, path,
                               wall_history_tokens=ref.get("context_tokens"))
            calib_miss(model, path, ref.get("context_pct"), "wall hit")
            _, lp = loop_state(path)
            mechanical_handoff(path, lp, extra_note="session hit the wall")
            policy = store.project_config(CFG, path).get("rotate_policy",
                                                         "compact")
            if role == "executor" and lp.get("active") and \
                    policy == "compact":
                notify("crash", "%s: the executor hit the context wall - the "
                       "conversation grew too long to compact in a single "
                       "step. Replacing it now with the handoff; the new "
                       "window needs its development-channels dialog "
                       "answered once." % project, path=path)
                threading.Thread(target=rotate_executor,
                                 args=(path, "hit the wall"),
                                 daemon=True).start()
            else:
                notify("crash", "%s: the session hit the context wall - too "
                       "long to compact. The handoff is written; press 'hand "
                       "over executor' in the panel, or send /rotate here."
                       % project, path=path)
        elif "rate" in etype:
            pconf = store.project_config(CFG, path)
            chain = pconf["chains"].get(role) or []
            nxt = models.next_in_chain(chain, (sess.get("model") or
                                               "").lower())
            if role == "executor" and nxt:
                notify("model_dropped", "%s: rate limit on %s - dropping the "
                       "executor to %s." % (project, sess.get("model"), nxt))
                threading.Thread(target=rotate_executor,
                                 args=(path, "rate limit", nxt),
                                 daemon=True).start()
            else:
                notify("limit_low", "%s: rate limit hit and no model left in "
                       "the chain. Waiting for the reset." % project)

    elif name == "PreCompact":
        sess = touch_session(event, state="compacting")
        ref = sess if sess.get("model") else best_session(path, role)
        model = (ref.get("model") or "?").lower()
        _, lp = loop_state(path)
        mechanical_handoff(path, lp, extra_note="written at PreCompact")
        store.snapshot_transcript(path, event.get("session_id"),
                                  event.get("transcript_path"))
        remap_archive(path, "a pre-compaction snapshot was taken")
        calib_miss(model, path, ref.get("context_pct"),
                   "PreCompact fired - estimate was high")
        at = ref.get("context_tokens")
        with _lock:
            key = "%s|%s" % (norm(path), role)
            hist = STATE.setdefault("compactions", {}).setdefault(key, [])
            hist.append({"at": time.strftime("%Y-%m-%d %H:%M"),
                         "tokens": at,
                         "session": event.get("session_id", "")})
            del hist[:-20]
            if at:
                # From here until the session draws itself again, the size on
                # record is the size of a conversation that is being replaced
                # by a summary. Marked, so that nothing is decided from it.
                sess["compaction_pending"] = {"at": time.time(),
                                              "tokens": int(at),
                                              "session": event.get(
                                                  "session_id", "")}
            sess["compacted_this_turn"] = True
            save_state()
        n_done = compactions_done(path, role)
        store.journal("loop", "Compaction %d for this %s session"
                      % (n_done, role), project, role, "log",
                      project_dir=path)
        if at:
            # This is where the turn *ended*, not where the threshold is.
            # Compaction is checked at the turn boundary and the turn runs
            # unchecked, so the context crosses the point somewhere inside
            # the turn and keeps going. Every sample is therefore an
            # overshoot, and the threshold is at or below the smallest one
            # ever seen - which is why the minimum is kept, and why it is
            # labelled an upper bound rather than a measurement.
            cal_now = store.calib_get(model, path, ref.get("window") or 1)
            prev = cal_now.get("compact_at_tokens")
            store.calib_update(model, path,
                               compact_at_tokens=min(prev or at, at),
                               compact_at_window=ref.get("window"),
                               compact_samples=(cal_now.get("compact_samples")
                                                or [])[-9:] + [int(at)])
            store.journal("loop", "Compaction fired on %s after a turn that "
                          "ended at %dk. The threshold is at or below that - "
                          "the turn ran unchecked past it, so this is an "
                          "upper bound, not the point itself. Smallest seen "
                          "here: %dk"
                          % (model, at // 1000,
                             min(prev or at, at) // 1000),
                          project, role, "log", project_dir=path)
            # The threshold the bridge passes at launch is only a belief
            # until a compaction proves where one actually fires. When the
            # two disagree this far, the override is not reaching the
            # session, and every distance computed from the set value is
            # wrong. Said once per role, then dropped - the measurement
            # takes over from here anyway.
            want = applied_compact_pct(path, role)
            win = ref.get("window") or 0
            if want and win:
                got = at * 100.0 / win
                if abs(got - want) > 10 and not STATE.get("pctgap:%s|%s"
                                                          % (path, role)):
                    with _lock:
                        STATE["pctgap:%s|%s" % (path, role)] = True
                        save_state()
                    store.journal(
                        "loop", "The %s was launched with the compaction "
                        "threshold set to %d%% but compacted at %d%% of its "
                        "window (%dk of %dk). The setting is not reaching "
                        "the session; the measured point is used instead."
                        % (role, want, round(got), at // 1000, win // 1000),
                        project, role, "sound", project_dir=path)
        policy = store.project_config(CFG, path).get("rotate_policy",
                                                     "compact")
        if policy == "compact":
            # Everything worth keeping is already on disk - the handoff just
            # written, the transcript snapshot, INDEX.md - so compaction can
            # go ahead. The session keeps its window, and its dialog stays
            # answered.
            text = ("%s / %s is compacting. Handoff and transcript saved "
                    "first; the session carries on in the same window."
                    % (project, role))
        else:
            text = ("%s / %s reached auto-compaction. Ceiling lowered; the "
                    "bridge rotates instead next time." % (project, role))
            if role == "executor" and loop_state(path)[1].get("active"):
                hook_output = {"decision": "block",
                               "reason": "bridge rotates instead of "
                                         "compacting"}
                threading.Thread(target=rotate_executor,
                                 args=(path, "PreCompact caught"),
                                 daemon=True).start()

    elif name == "PreToolUse":
        tool = event.get("tool_name") or ""
        blocked = disallow_for(path, role) or []
        if role == "planner" and tool in blocked:
            return ({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    "%s belongs to the executor, not to you. You two work as "
                    "a pair on this project: you hold the plan and the "
                    "judgement, it has the hands - it edits, runs and "
                    "reports back, and you answer with a verdict. That "
                    "division is what keeps your context small enough to "
                    "keep reviewing clearly for hours. Call the bridge tool "
                    "'task' with concrete instructions instead; the work "
                    "will be done and come back to you as a report."
                    % tool}}, None)
        tin = event.get("tool_input") or {}
        # The same gate the daemon applies at /verdict, asked one step
        # earlier - here the call has not left the planner's session yet, so
        # the refusal arrives as a denied tool call rather than as an error
        # to be interpreted. The two levels are deliberately independent:
        # this one catches it sooner, the daemon catches it always (a window
        # started without the bridge's hooks, or an older install, still
        # cannot slip a bare "done" past it). They share one implementation
        # so they cannot drift apart.
        if tool.endswith("__verdict") or tool == "verdict":
            args = tin if isinstance(tin, dict) else {}
            okg, whyg, _kind = verdict_gate(
                path, str(args.get("verdict") or ""),
                str(args.get("feedback") or ""))
            if not okg:
                return ({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": whyg}}, None)
        cmd = (tin.get("command") or "") if isinstance(tin, dict) else ""
        patterns = ("godot", "pytest", "npm test", "cargo build", "make",
                    "gradle", "dotnet build")
        bg = bool(tin.get("run_in_background")) if isinstance(tin, dict) \
            else False
        if event.get("tool_name") == "Bash" and (
                bg or any(p in cmd for p in patterns)):
            sig = cmd.split()[0][:40] if cmd else "bash"
            PROCTRACK.setdefault(path, {})[sig] = {
                "cmd": cmd[:160], "started": time.time(),
                "session": event.get("session_id", "")}
            with _lock:
                STATE.setdefault("inflight", {}).setdefault(path, {})[sig] = \
                    {"cmd": cmd[:160], "started": time.time()}
                save_state()
            touch_session(event, state="waiting on a process")
            store.journal("process", "Started: %s" % cmd[:120], project, role,
                          "log", project_dir=path)
        return None, None

    elif name == "PostToolUse":
        tin = event.get("tool_input") or {}
        cmd = (tin.get("command") or "") if isinstance(tin, dict) else ""
        sig = cmd.split()[0][:40] if cmd else "bash"
        with _lock:
            (STATE.get("inflight", {}).get(path) or {}).pop(sig, None)
            save_state()
        tracked = PROCTRACK.get(path, {}).pop(sig, None)
        if tracked:
            dur = time.time() - tracked["started"]
            DURATIONS.setdefault((path, sig), []).append(dur)
            del DURATIONS[(path, sig)][:-30]
            touch_session(event, state="idle")
            deliver(path, "planner",
                    "Process finished: %s (%.0f s)." % (tracked["cmd"], dur),
                    {"kind": "info"})
            store.journal("process", "Finished in %.0fs: %s"
                          % (dur, tracked["cmd"]), project, role, "log",
                          project_dir=path)
        return None, None

    elif name == "SessionStart":
        sess = touch_session(event, state="starting")
        mark_registered(path, role)
        prune_sessions()
        with _lock:
            STATE.pop("tight:%s" % path, None)
            STATE.pop("hoheld:%s" % path, None)
            save_state()
        threading.Thread(target=watch_rc_link,
                         args=(path, role, event.get("transcript_path"),
                               event.get("session_id")),
                         daemon=True).start()
        with _lock:
            (STATE.get("down") or {}).pop("%s|%s" % (path, role), None)
            hold = (STATE.get("paused") or {}).get(path) or {}
            # The bridge-wide form is what a pre-multipair state.json holds;
            # it is honoured once so an upgrade cannot leave the loop stuck
            # paused for a window that has already come back.
            stale_global = (STATE.get("paused_by_death") == path
                            and STATE.get("mode") == "paused")
            if hold.get("by_death") or stale_global:
                (STATE.get("paused") or {}).pop(path, None)
                if stale_global:
                    STATE.pop("paused_by_death", None)
                    STATE["mode"] = "running"
                save_state()
                notify("session_died", "%s: the %s is back - resuming the "
                       "loop." % (project, role), level="silent", path=path)
            else:
                save_state()
        parts = []
        seed = None
        with _lock:
            seed = STATE.get("seed", {}).pop(path, None) \
                if role == "executor" else None
            save_state()
        if role == "planner":
            parts.append(
                "You are the PLANNER of a bridge pair working this project "
                "together with an executor session in the same folder.\n\n"
                "How the two of you work: the executor has the hands - it "
                "edits files, runs commands, hits the errors and reports "
                "back. You hold the thread: you decide what should happen "
                "next, you read its reports and answer them. Neither half "
                "works alone. The reason for the split is practical - the "
                "executor's context fills up with tool output and gets "
                "rotated, while yours stays small enough to keep judging "
                "clearly all night, and to carry what matters across those "
                "rotations.\n\n"
                "So when work needs doing - including when the human types a "
                "request here - do not do it yourself. Turn it into concrete "
                "instructions, call the bridge tool 'task', and tell the "
                "human it is on its way. When a report arrives, answer with "
                "the bridge tool 'verdict': continue with what to fix, done "
                "to accept, wait if a long process is still running. There "
                "is no need to leave plan mode; nothing you do requires "
                "it.\n\n" + PLANNER_CONTEXT_RULE)
        if seed:
            parts.append("You continue a rotated session (%s). Handoff:\n\n%s"
                         % (seed.get("reason", ""), seed.get("handoff", "")))
        if role == "planner":
            pseed = None
            with _lock:
                pseed = (STATE.get("planner_seed") or {}).pop(path, None)
                save_state()
            if pseed:
                also = "executor" in (pseed.get("roles") or
                                      ["executor", "planner"])
                parts.append(
                    "You are continuing a handover (%s) at iteration %s. %s "
                    "Your predecessor's last verdict was:\n%s\n\nThe "
                    "handoff:\n\n%s"
                    % (pseed.get("reason", ""), pseed.get("iteration"),
                       "The executor has been replaced too and is being "
                       "handed the same handoff." if also else
                       "Only you were replaced - the executor is the same "
                       "session as before, still working, and it remembers "
                       "the run. You do not; the handoff below is what you "
                       "have.",
                       pseed.get("feedback") or "(none recorded)",
                       pseed.get("handoff", "")))
        if (STATE.get("handover") or {}).get(path):
            threading.Thread(target=resume_after_handover,
                             args=(path, role), daemon=True).start()
        if STATE.get("mode") == "recovered":
            parts.append("The machine stopped without shutting down cleanly. "
                         "Files may have changed after your last recorded "
                         "turn - check the working tree before redoing "
                         "anything.")
        # Read, not taken: a session starting is not the note's delivery -
        # the report is (run_review). Taking it here would mean a rotation
        # swallowed the line the human wrote for the review.
        note = note_for(path)
        if note:
            parts.append(note)
        # Both roles, every start, including the replacement a handover
        # brings up - a rotated session is a new session and has been told
        # nothing. Last in the seed on purpose: it is the part that should
        # still be in view when the rest has been read.
        canon = honesty_text()
        if canon and managed(role):
            parts.append(canon)
        out = {}
        if parts:
            out["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(parts)}
        if seed and not event.get("session_title"):
            out.setdefault("hookSpecificOutput", {
                "hookEventName": "SessionStart"})
            out["hookSpecificOutput"]["sessionTitle"] = seed.get("title", "")
        hook_output = out or None
        text = "%s / %s session started." % (project, role)

    elif name == "SessionEnd":
        touch_session(event, state="ended")
        prune_sessions()
        # the channel process is a child of this session, so it is gone too -
        # leaving its registration behind makes a closed window look alive
        # for the two minutes the entry stays fresh
        CHANNELS.pop((path, role), None)
        with _lock:
            (STATE.get("channels") or {}).pop("%s|%s" % (path, role), None)
            (STATE.get("rc") or {}).pop("%s|%s" % (path, role), None)
            STATE.get("pids", {}).pop("%s|%s" % (path, role), None)
            (STATE.get("down") or {}).pop("%s|%s" % (path, role), None)
            save_state()
        text = "%s / %s session ended." % (project, role)
        _, lp0 = loop_state(path)
        if role == "executor" and lp0.get("active"):
            notify("session_died", "%s: the executor exited while the loop "
                   "was on. Start it again from launch, or stop the loop."
                   % project, path=path)

    store.journal(name, text or phrase, project, role,
                  CFG.get("notify", {}).get(setting or "", "log"),
                  project_dir=path if os.path.isdir(path) else None)
    if text and setting:
        notify(setting, text)
    refresh_pin()
    return hook_output, text


# ---------------------------------------------------------------------------
# status line telemetry

def record_limits(limits):
    """Plan limits belong to the account, so any window can report them."""
    with _lock:
        lim = STATE.setdefault("limits", {})
        extra = lim.setdefault("extra", {})
        for key, val in (limits or {}).items():
            if not isinstance(val, dict):
                continue
            entry = {"pct": val.get("used_percentage"),
                     "resets": val.get("resets_at", "")}
            if key == "five_hour":
                lim["five_hour"] = entry
            elif key in ("seven_day", "weekly", "week"):
                lim["weekly"] = entry
            else:
                extra[key] = entry
        save_state()


def task_reachable(path):
    """Can the executor be delivered to right now? Fast, no injection."""
    ch = channel_for(path, "executor") or channel_alive(path, "executor")
    if not ch or not ch.get("port"):
        return False, ("the executor has no channel the bridge can reach - "
                       "its window is gone, or its channel process died "
                       "inside a window that is still open")
    if not port_answers(int(ch["port"]), timeout=1.5):
        return False, ("nothing is listening on the executor's channel port "
                       "%s" % ch["port"])
    return True, None


def deliver_task_later(path, text, tries=3):
    """Put the task into the executor, off the request thread.

    The planner has already been told it is on its way; if it turns out not
    to be, that lands in the inbox and in Max's Telegram rather than in a
    tool result nobody can act on.
    """
    body = "Task from the planner:\n\n%s" % text
    for attempt in range(1, tries + 1):
        ok, why = deliver_ex(path, "executor", body, {"kind": "task"})
        if ok:
            store.journal("task", "Task delivered to the executor%s"
                          % ("" if attempt == 1 else
                             " on attempt %d" % attempt),
                          project_name(path), "executor", "log",
                          project_dir=path)
            return True
        time.sleep(3 * attempt)
    inbox = store.inbox_write(path, 0, "TASK\n\n" + text)
    store.journal("task", "The task never reached the executor after %d "
                  "attempts: %s. Written to %s"
                  % (tries, why, inbox), project_name(path), "executor",
                  "sound", project_dir=path)
    notify("needs_you",
           "%s: the planner's task did not reach the executor - %s. The "
           "bridge is fine; it is the executor's channel that would not "
           "take it. Saved to %s:\n\n%s"
           % (project_name(path), why, inbox, text[:1200]), path=path)
    return False


def selftest(path):
    """Walk the task path the planner walks, and say where it stops.

    Four evenings of "the bridge is unreachable" were four different
    things, and none of them could be told apart from the tool result the
    planner sees. This does each hop itself and names the one that fails,
    so the answer stops depending on how a session phrases its error.
    """
    out = {"at": time.strftime("%H:%M:%S"), "steps": []}

    def step(name, ok, detail=""):
        out["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    port = CFG.get("port", 8765)
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%s/task" % port,
            data=json.dumps({"project": path, "instructions": ""}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Bridge-Secret": SECRET})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read().decode("utf-8"))
        step("the daemon answers its own /task on port %s" % port, True,
             "%.0f ms, replied %s" % ((time.time() - t0) * 1000,
                                      json.dumps(body)))
    except Exception as exc:
        step("the daemon answers its own /task on port %s" % port, False,
             "%r - if this fails, nothing else can work" % exc)
        out["verdict"] = ("The daemon is not answering on its own port. "
                          "Nothing to do with the sessions.")
        return out

    for role in MANAGED_ROLES:
        ch = channel_for(path, role) or channel_alive(path, role)
        if not step("%s channel is registered" % role, bool(ch),
                    "port %s" % (ch or {}).get("port", "-")):
            continue
        step("%s channel port answers" % role,
             port_answers(int(ch["port"]), timeout=2),
             "port %s" % ch["port"])

    ex = channel_for(path, "executor") or channel_alive(path, "executor")
    if ex and ex.get("port"):
        ok, why = deliver_ex(path, "executor",
                             "Bridge self-test - ignore this message.",
                             {"kind": "info"})
        step("a message actually lands in the executor", ok, why or "")

    bad = [s for s in out["steps"] if not s["ok"]]
    if not bad:
        out["verdict"] = ("Every hop the bridge owns works. If a session "
                          "still says the bridge is unreachable, the break "
                          "is between that window and its own MCP process - "
                          "type /mcp in it and reconnect 'bridge', or "
                          "restart the window. Nothing here can reach that.")
    else:
        out["verdict"] = "First failing hop: %s - %s" % (bad[0]["name"],
                                                         bad[0]["detail"])
    return out


def handle_status(body):
    payload = body.get("payload") or {}
    role = body.get("role") or ""
    cw = payload.get("context_window") or {}
    limits = payload.get("rate_limits") or {}
    ws = payload.get("workspace") or {}
    fake_event = {"role": role, "session_id": payload.get("session_id", ""),
                  "cwd": ws.get("current_dir", ""),
                  "project_dir": ws.get("project_dir", "")}
    path = norm(fake_event["project_dir"] or fake_event["cwd"])

    if not managed(role):
        # Counted, then treated like any other window - see handle_event.
        note_stranger(path, payload.get("session_id"))

    # Once per session, write down the whole context_window object rather
    # than the three fields the bridge reads. Every argument in this project
    # about where compaction fires and how big the window is has been an
    # argument about numbers nobody had looked at. The client knows both; if
    # it states them here, they stop being in dispute.
    seen_key = "cwdump:%s" % (payload.get("session_id") or "")[:8]
    if cw and not STATE.get(seen_key):
        with _lock:
            STATE[seen_key] = True
            save_state()
        store.journal("loop", "Status line context_window, verbatim, for the "
                      "%s: %s" % (role or "session",
                                  json.dumps(cw, ensure_ascii=False)[:2000]),
                      project_name(path), role, "log",
                      project_dir=path if os.path.isdir(path) else None)
    sess = touch_session(
        fake_event,
        context_pct=cw.get("used_percentage"),
        window=cw.get("context_window_size"),
        context_tokens=_tokens(cw),
        model=(payload.get("model") or {}).get("display_name", ""),
        model_id=(payload.get("model") or {}).get("id", ""))

    if sess.get("state") == "needs you":
        base = sess.get("needs_you_at_tokens")
        if base is not None and (sess.get("context_tokens") or 0) > base:
            with _lock:      # the session moved on, so it is not waiting
                sess["state"] = "idle"
                sess.pop("needs_you_at_tokens", None)
                save_state()

    mid = (payload.get("model") or {}).get("id") or ""
    disp = (payload.get("model") or {}).get("display_name") or mid
    if mid:
        req = model_req_of(path, role)
        fam_got = models.family_of(mid) or models.family_of(disp)
        if req:
            store.models_note(req, mid, disp, "live session")
        if fam_got:
            store.models_note(fam_got, mid, disp, "live session")
        fam_req = models.family_of(req) if req else None
        if fam_req and fam_got and fam_req != fam_got:
            if store.models_sub(req, mid, disp):
                store.journal("model_dropped",
                              "%s asked for %s, the server is running %s "
                              "instead" % (project_name(path), req, disp),
                              project_name(path), role, "silent",
                              project_dir=path)
                notify("model_dropped",
                       "%s: asked for %s, got %s - the alias resolved "
                       "across families on the server side."
                       % (project_name(path), req, disp), level="silent")

    record_limits(limits)

    if role == "executor":
        fh = (STATE.get("limits", {}).get("five_hour") or {}).get("pct")
        with _lock:
            last = STATE.get("fh_last")
            if fh is not None and last is not None and fh > last:
                hist = STATE.setdefault("fh_costs", [])
                hist.append(fh - last)
                del hist[:-5]
            if fh is not None:
                STATE["fh_last"] = fh
            save_state()

    fh = (STATE.get("limits", {}).get("five_hour") or {})
    pct = fh.get("pct")
    if pct is not None:
        warn = CFG["thresholds"].get("limit_warn_at", 85)
        pause = CFG["thresholds"].get("limit_pause_at", 90)
        if pct >= pause and STATE.get("mode") == "running":
            with _lock:
                STATE["mode"] = "paused"
                STATE["paused_by_limit"] = True
                save_state()
            notify("limit_low", "Five-hour limit at %d%% - pausing the loop. "
                   "Resets %s." % (int(pct), fh.get("resets", "?")))
        elif pct >= warn and not STATE.get("limit_warned"):
            with _lock:
                STATE["limit_warned"] = True
                save_state()
            notify("limit_low", "Five-hour limit at %d%%." % int(pct))
        elif pct < 50 and (STATE.get("limit_warned") or
                           STATE.get("paused_by_limit")):
            with _lock:
                STATE["limit_warned"] = False
                if STATE.pop("paused_by_limit", None):
                    auto = any(store.project_config(CFG, p).get(
                        "auto_resume_after_reset")
                        for p in CFG.get("projects", {}))
                    if auto:
                        STATE["mode"] = "running"
                        notify("iteration_done",
                               "Limit reset - resuming the loop.")
                save_state()

    cpct = cw.get("used_percentage")
    if cpct is not None and role == "executor":
        warn = CFG["thresholds"].get("warn_at", 85)
        key = "warned:%s" % session_key(fake_event)
        if cpct >= warn and not STATE.get(key):
            with _lock:
                STATE[key] = True
                save_state()
            notify("limit_low", "%s / executor context at %d%% - the handoff "
                   "is kept fresh; it compacts and carries on, and is only "
                   "replaced once its compactions are spent."
                   % (project_name(path), int(cpct)))
    refresh_pin()


# ---------------------------------------------------------------------------
# model discovery: a one-token probe per alias, keyed by what really served

def probe_models(aliases):
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "bridge-probe")
    os.makedirs(tmp, exist_ok=True)
    found = {}
    no_persist = ["--no-session-persistence"]
    for a in aliases:
        try:
            cmd = ["claude", "-p", "ok", "--model", a,
                   "--output-format", "json", "--max-turns", "1"]
            r = subprocess.run(cmd + no_persist, capture_output=True,
                               text=True, encoding="utf-8",
                               errors="replace", timeout=180, cwd=tmp)
            if r.returncode != 0 and no_persist and \
                    "no-session-persistence" in (r.stderr or "") + \
                    (r.stdout or ""):
                no_persist = []   # older CLI - drop the flag for this run
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=180, cwd=tmp)
            line = (r.stdout or "").strip().splitlines()
            data = json.loads(line[-1]) if line else {}
            mid = next(iter((data.get("modelUsage") or {}).keys()), None)
            if mid:
                store.models_note(a, mid, mid, "probe")
                found[a] = mid
            else:
                found[a] = "? " + ((r.stderr or
                                    data.get("result") or "")[:80])
        except Exception as exc:
            found[a] = "? %s" % str(exc)[:80]
    with _lock:
        STATE["model_probe_running"] = False
        save_state()
    m = store.load_models()
    m["last_probe"] = time.strftime("%Y-%m-%d %H:%M")
    m["last_errors"] = {a: str(v)[2:].strip() for a, v in found.items()
                        if str(v).startswith("? ")}
    store.save_models(m)
    store.journal("model_dropped", "Model probe: %s" % ", ".join(
        "%s -> %s" % kv for kv in sorted(found.items())), level="log")
    return found


def start_probe():
    with _lock:
        if STATE.get("model_probe_running"):
            return False
        STATE["model_probe_running"] = True
        save_state()
    aliases = set()
    for p in CFG.get("projects", {}):
        pc = store.project_config(CFG, p)
        for chain in pc.get("chains", {}).values():
            aliases.update(chain)
    aliases.update(models.FAMILIES)
    reg = store.load_models()
    if not reg["opts"].get("allow_best"):
        aliases.discard("best")
    threading.Thread(target=probe_models, args=(sorted(aliases),),
                     daemon=True).start()
    return True


def maybe_auto_probe():
    """reread_on_launch: refresh the registry in the background when it is
    older than a day. Costs one short -p call per alias."""
    reg = store.load_models()
    if not reg["opts"].get("reread_on_launch", True):
        return
    last = reg.get("last_probe") or ""
    try:
        stale = (time.time() - time.mktime(
            time.strptime(last, "%Y-%m-%d %H:%M"))) > 86400
    except Exception:
        stale = True
    if stale:
        start_probe()


# ---------------------------------------------------------------------------
# stuck-process watchdog

def process_watch():
    while True:
        time.sleep(30)
        for path, procs in list(PROCTRACK.items()):
            for sig, meta in list(procs.items()):
                run = time.time() - meta["started"]
                hist = DURATIONS.get((path, sig), [])
                usual = (sum(hist) / len(hist)) if len(hist) >= 5 else None
                limit = (usual * 3) if usual else 900
                if run > limit and not meta.get("flagged"):
                    meta["flagged"] = True
                    ev = ("Process %s has run %.0f min (usual: %s). "
                          "Decide whether it is stuck."
                          % (meta["cmd"], run / 60,
                             ("%.0f s over %d runs" % (usual, len(hist)))
                             if usual else "no history yet"))
                    if not deliver(path, "executor", ev, {"kind": "info"}):
                        deliver(path, "planner", ev, {"kind": "info"})
                    # The sessions get the whole thing - they need the
                    # command to act on it. The chat gets the decision:
                    # what, how long, what to do. A tracked "command" is
                    # whatever was run, and one of them was a heredoc
                    # writing a markdown file, so the untrimmed version put
                    # the entire file on the phone.
                    notify("process_stuck",
                           "%s: a process has run %.0f min (usual: %s) - "
                           "decide whether it is stuck: %s"
                           % (project_name(path), run / 60,
                              ("%.0fs" % usual) if usual else "no history",
                              brief(meta["cmd"])), path=path)


# ---------------------------------------------------------------------------
# telegram pinned + poll

def project_headline(path):
    """One project's state, in three or four words.

    The order is the same as it always was; what changed is that it is
    asked per project instead of once for the whole bridge. A pair held on
    its own used to be invisible here - the headline only knew the
    bridge-wide pause - so a project whose reports were being held read as
    "running" next to the pairs that really were.
    """
    path = norm(path)
    # Same rule as the strip's, from the same function, so the pin and the
    # panel cannot disagree about whether a pair is up.
    live = live_sessions(path)
    lp = (STATE.get("loops") or {}).get(path) or {}
    aw = (STATE.get("awaiting") or {}).get(path) or {}
    if aw.get("nudges", 0) >= 1 and lp.get("active"):
        return "loop stalled"
    if STATE.get("mode") == "paused":
        return "paused (limit)" if STATE.get("paused_by_limit") else "paused"
    hold = (STATE.get("paused") or {}).get(path)
    if hold:
        return "held - %s" % (hold.get("why") or "by hand")
    if [s for s in live if s.get("state") == "needs you"]:
        return "waiting on you"
    if [s for s in live if s.get("state") == "waiting on a process"]:
        return "waiting on a process"
    if not live:
        return "no sessions"
    return "loop on" if lp.get("active") else "loop off"


def live_sessions(path):
    """This project's sessions that have not said goodbye.

    A record with no "state" at all is alive. It means the session has been
    seen - a status line arrived, which only a running window draws - and
    has not yet said what it is doing; touch_session writes fields only
    when they are not None, so a session first met through /status has no
    state key. Reading that as "not running" is what made a pair that was
    demonstrably working - finishing turns, taking verdicts - show up in
    the strip as "no sessions" with both contexts blank.
    """
    path = norm(path)
    return [s for s in (STATE.get("sessions") or {}).values()
            if norm(s.get("path")) == path
            and s.get("state") not in ("ended", "died")]


def pair_paths():
    """The projects that have earned a row of their own.

    Configured, or with a session record to their name. Deliberately not
    "anything with a loop record": loop_state() creates one for whatever
    path is handed to it, so a single stray event from a folder that is not
    a project leaves a permanent entry behind - which is how a second row
    called "bridge" appeared next to "Bridge", the first being the project
    and the second the repository folder inside it, with no sessions and
    nothing to show. A row asserts that a pair lives here; a loop record on
    its own does not say that.
    """
    out = set(norm(p) for p in CFG.get("projects", {}))
    out |= {norm(s.get("path")) for s in (STATE.get("sessions") or {}).values()
            if s.get("path")}
    out.discard("")
    return sorted(out)


def pairs_view():
    """One row per pair: enough for a summary, and nothing more.

    Built here rather than in the panel on purpose. The panel already draws
    one project in full, and the strip above it exists only to say which
    other pairs are up and let you switch to them - so if it worked the
    numbers out for itself it would be a second implementation of the same
    reading, free to disagree with the first. It gets the same words the pin
    gets, from the same functions.
    """
    out = {}
    for path in pair_paths():
        lp = (STATE.get("loops") or {}).get(path) or {}
        hold = (STATE.get("paused") or {}).get(path) or {}
        roles = {}
        for role in MANAGED_ROLES:
            best = None
            for s in live_sessions(path):
                if s.get("role") != role:
                    continue
                if best is None or seen_at(s) > seen_at(best):
                    best = s
            if best:
                lv = life_view(best, path) or {}
                roles[role] = {"pct": best.get("context_pct"),
                               "tokens": best.get("context_tokens"),
                               "window": best.get("window"),
                               "model": best.get("model") or "",
                               "state": best.get("state") or "",
                               # How far through its life, not how full its
                               # window is. On a strip whose job is "which
                               # pair needs me next", the useful number is
                               # how close a session is to being replaced;
                               # window fill answers that only within one
                               # cycle and resets every compaction. Both are
                               # here - the panel shows life and keeps fill
                               # in the hover - because they are different
                               # questions and neither substitutes.
                               "life": lv.get("life_pct"),
                               "compacted": lv.get("done"),
                               "budget": lv.get("budget")}
        out[path] = {
            "name": project_name(path),
            "mark": mark_for(path),
            "state": project_headline(path),
            "loop": bool(lp.get("active")),
            "iteration": lp.get("iteration", 0),
            "held": bool(hold),
            "held_why": hold.get("why") or "",
            # Two counts a person should never have to go looking for: the
            # pieces accepted with nothing to open, and the temporary
            # solutions still standing. Neither blocks anything, and that is
            # exactly why they have to be visible - the failure they came
            # from was forty-five workarounds nobody was counting.
            "no_artifacts": (STATE.get("noart") or {}).get(path, 0),
            "debt_open": len(open_debt(path)),
            "unanswered": (STATE.get("unanswered") or {}).get(path, 0),
            "roles": roles,
        }
    return out


def status_headline():
    """What is actually going on, project by project.

    The first two answers are about the whole bridge and stay that way -
    it was interrupted, or a window is down - because neither is a fact
    about one pair. Everything after names the projects, because with
    several pairs a single word for all of them is either wrong about most
    of them or too vague to act on.
    """
    if STATE.get("mode") == "recovered":
        return "interrupted - open the resume tab"
    down = STATE.get("down") or {}
    if down:
        roles = sorted({k.rpartition("|")[2] for k in down})
        giveup = any((v or {}).get("giveup") for v in down.values())
        return "session down - %s%s" % (", ".join(roles),
                                        " (retries stopped)" if giveup else "")
    loops = [p for p, l in (STATE.get("loops") or {}).items()
             if l.get("active")]
    parts = []
    for path in pair_paths():
        word = project_headline(path)
        if word == "no sessions" and path not in loops:
            continue                  # nothing to say about an idle project
        parts.append("%s: %s" % (project_name(path), word))
    if not parts:
        return "idle - no sessions running"
    return " · ".join(parts)


def pinned_text():
    lines = []
    for key, s in sorted(STATE.get("sessions", {}).items()):
        if s.get("state") in ("ended",) or not managed(s.get("role")):
            continue
        pct = s.get("context_pct")
        # Which pair, not just which half of it. Four lines reading
        # executor, planner, executor, planner told you nothing about who
        # was who; the colour is there for the glance and the name for the
        # certainty.
        tag = ("%s %s/%s" % (mark_for(s.get("path")),
                             project_name(s.get("path")),
                             s.get("role") or key)).strip()
        if pct is None:
            lines.append("%-24s %s" % (tag, "-"))
        else:
            lines.append("%-24s %s %3d%%" % (tag, telegram.bar(pct),
                                             int(pct)))
    lim = STATE.get("limits", {})
    fh = lim.get("five_hour") or {}
    wk = lim.get("weekly") or {}
    # The limits belong to the account, so they carry no pair colour: they
    # are one budget that every pair is spending at once.
    if fh.get("pct") is not None:
        lines.append("%-24s %s %3d%% -> %s" % ("five hours (all pairs)",
                     telegram.bar(fh["pct"]), int(fh["pct"]),
                     fmt_reset(fh.get("resets"))))
    if wk.get("pct") is not None:
        lines.append("%-24s %s %3d%%" % ("week (all pairs)",
                                         telegram.bar(wk["pct"]),
                                         int(wk["pct"])))
    for nm, e in (lim.get("extra") or {}).items():
        if e.get("pct") is not None:
            lines.append("%-24s %s %3d%%" % (nm[:24],
                         telegram.bar(e["pct"]), int(e["pct"])))
    costs = STATE.get("fh_costs") or []
    fhp = fh.get("pct")
    if costs and fhp is not None:
        avg = sum(costs) / len(costs)
        if avg > 0:
            # Deliberately not per project. The cost of an iteration is
            # averaged over whichever pairs happened to run, so this is a
            # figure for the account as a whole and is labelled as one
            # rather than split into numbers that would look per-pair and
            # not be.
            lines.append("enough for ~%d more iterations, all pairs together"
                         % int((100 - fhp) / avg))
    head = "bridge - %s" % status_headline()
    return "\n".join([head] + lines) if lines else head


_last_pin = [0.0]


def refresh_pin(force=False):
    global CFG
    if not CFG.get("telegram", {}).get("chat_id"):
        return
    if not force and time.time() - _last_pin[0] < 20:
        return
    _last_pin[0] = time.time()
    CFG = telegram.status_message(CFG, pinned_text())
    store.save_config(CFG)


def start_stopped_loops():
    """Turn the loop back on wherever it is off but a pair is up."""
    started = []
    for path, lp in list((STATE.get("loops") or {}).items()):
        if lp.get("active"):
            continue
        if not (already_up(path, "executor") or already_up(path, "planner")):
            continue
        handle_loop({"action": "start", "project": path})
        started.append(project_name(path))
    return started


def handle_cmd(body):
    """pause, resume and note, either about one project or about the bridge.

    A "project" in the body scopes the command to that pair and nothing else.
    Without one the meaning is what it has always been - the whole bridge -
    so every existing caller keeps working unchanged. Resuming the bridge
    also lifts the individual holds: it is the "everything back to normal"
    button, and leaving a pair paused under it would be a pause nobody could
    see the reason for.
    """
    cmd = body.get("cmd")
    project = body.get("project") or ""
    if project and cmd in ("pause", "resume", "note"):
        if cmd == "pause":
            pause_project(project, "you paused this project")
        elif cmd == "resume":
            resume_project(project)
        else:
            set_note(project, body.get("text", ""))
        refresh_pin(force=True)
        return {"ok": True, "mode": STATE.get("mode"),
                "project": norm(project)}

    if cmd == "note":
        # No project named. One project is unambiguous; several are not, and
        # guessing is how the note reached the wrong pair before.
        targets = sorted(set([norm(p) for p in (CFG.get("projects") or {})]) |
                         set(STATE.get("loops") or {}))
        if len(targets) == 1:
            set_note(targets[0], body.get("text", ""))
        else:
            return {"ok": False, "mode": STATE.get("mode"),
                    "error": ("there are no projects yet, so there is nobody "
                              "to leave a note for") if not targets else
                             ("there are %d projects, so this note has no "
                              "addressee - say which one it is for"
                              % len(targets)),
                    "projects": [project_name(p) for p in targets]}
    with _lock:
        if cmd == "pause":
            STATE["mode"] = "paused"
        elif cmd == "resume":
            STATE["mode"] = "running"
            STATE.pop("paused_by_limit", None)
            STATE["paused"] = {}
        elif cmd == "ack_recovery":
            STATE["mode"] = "running"
            STATE["recovered_reason"] = None
            STATE["inflight"] = {}
            STATE["restart_tries"] = {}
            STATE["down"] = {}
        save_state()
    store.journal("command", "You: %s" % cmd, level="log")
    refresh_pin(force=True)
    return {"ok": True, "mode": STATE.get("mode")}


def telegram_note(ok, why="", code=None):
    """Remember whether Telegram is answering, and say so once when it stops.

    Silence is the failure mode that hides itself here: every path in
    telegram.py swallows its errors on purpose, so a token that has been
    revoked looks exactly like a quiet night.
    """
    prev = (STATE.get("telegram_health") or {}).get("ok")
    row = {"ok": bool(ok), "why": why or "", "code": code,
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _lock:
        STATE["telegram_health"] = row
        save_state()
    if prev is not False and not ok:
        low = (why or "").lower()
        auth = any(w in low for w in ("unauthorized", "not found", "token",
                                      "forbidden", "401", "404"))
        store.journal("crash", "Telegram stopped answering: %s. Buttons and "
                      "messages will do nothing until it comes back. %s"
                      % (why or "no reason given",
                         "The token is rejected - set it again in the "
                         "telegram tab." if auth else
                         "This is the connection to api.telegram.org, not "
                         "the token: nothing to fix in the panel, and it "
                         "retries by itself."),
                      "", "", "log")
    elif prev is False and ok:
        store.journal("session", "Telegram is answering again.", "", "", "log")
    return ok


# ---------------------------------------------------------------------------
# What the chat asked for.
#
# The reading of a message and the doing of it are separate on purpose. They
# used to be one block inside the long-poll loop, which meant the only way to
# reach the code that answers a verdict was to have Telegram deliver a real
# update - so none of it was ever tested, and the bug that made /verdict
# answer every waiting project at once sat there in plain sight.

VERDICT_WORDS = ("continue", "done", "wait", "stop")

# The commands, and what each one is allowed to leave unaddressed.
#
#   "one"    - works with no address when exactly one pair is waiting for it,
#              which is what it has always done. Several, and it refuses and
#              lists them rather than picking.
#   "bridge" - no address means the whole bridge, which is what the words
#              mean: pause everything, resume everything.
#   "never"  - an address is required whatever the count. Only /rotate:
#              replacing a session costs a window and cannot be undone, so
#              "there was only one candidate anyway" is not a good enough
#              reason to do it to a pair nobody named.
TG_ADDRESSING = {
    "verdict": "one",
    "note": "one",
    "rotate": "never",
    "pause": "bridge",
    "resume": "bridge",
    "loop": "bridge",
    "status": "bridge",
}

TG_ALIASES = {
    "pause": "pause", "/pause": "pause",
    "resume": "resume", "/resume": "resume",
    "status": "status", "/status": "status",
    "/loop": "loop", "start loop": "loop", "start the loop": "loop",
    "/verdict": "verdict", "/note": "note", "/rotate": "rotate",
}


def parse_command(text):
    """Read one line from the chat. Pure: no state, no network, no effects.

    Returns {"cmd", "addr", "verdict", "text", "error"}. cmd is None when the
    line is not a command at all, which is not an error - people talk in
    that chat.

    An address is "@name" and nothing else. Working out whether the first
    word after the command is a project or part of the command would hold
    right up until somebody had a project called "done".
    """
    raw = (text or "").strip()
    out = {"cmd": None, "addr": None, "verdict": None, "text": "",
           "error": ""}
    if not raw:
        return out
    low = raw.lower()
    for phrase, cmd in TG_ALIASES.items():
        if low == phrase or low.startswith(phrase + " "):
            out["cmd"] = cmd
            rest = raw[len(phrase):].strip()
            break
    else:
        return out
    if rest.startswith("@"):
        addr, _, rest = rest.partition(" ")
        out["addr"] = addr[1:].strip()
        rest = rest.strip()
        if not out["addr"]:
            out["error"] = "there is nothing after the @"
            return out
    if out["cmd"] == "verdict":
        word, _, tail = rest.partition(" ")
        word = word.lower()
        if not word:
            out["error"] = ("say which verdict: %s"
                            % ", ".join(VERDICT_WORDS))
            return out
        if word not in VERDICT_WORDS:
            out["error"] = ("%r is not a verdict - say one of %s"
                            % (word, ", ".join(VERDICT_WORDS)))
            return out
        out["verdict"] = word
        out["text"] = tail.strip()
    else:
        out["text"] = rest
    return out


def name_of(path):
    return project_name(path)


def resolve_addr(addr, reply_to, candidates):
    """Which pair a command is for: (path, error, choices).

    Three ways, in the order they cost the human effort. Replying to a
    message needs nothing typed at all; @name needs a word; and with exactly
    one candidate nothing is needed either, which is the behaviour of the
    day when there was only ever one project.
    """
    cands = [norm(p) for p in candidates if p]
    names = [name_of(p) for p in cands]
    if addr:
        want = addr.strip().lower().lstrip("@")
        exact = [p for p in cands if name_of(p).lower() == want]
        if len(exact) == 1:
            return exact[0], "", names
        if len(exact) > 1:
            return None, ("more than one project is called %r" % addr), names
        pref = [p for p in cands if name_of(p).lower().startswith(want)]
        if len(pref) == 1:
            return pref[0], "", names
        if len(pref) > 1:
            return None, ("%r matches more than one project" % addr), names
        return None, ("no project called %r" % addr), names
    if reply_to:
        got = MSGPROJ.get(int(reply_to))
        if got and got in cands:
            return got, "", names
    if len(cands) == 1:
        return cands[0], "", names
    if not cands:
        return None, "", names
    return None, "there is more than one, so this needs an address", names


def waiting_projects():
    return sorted(PENDING)


def rotatable_projects():
    return [p for p in known_projects() if already_up(p, "executor")]


def run_telegram_command(text, reply_to=None):
    """Do what the chat asked, and say what happened. No network in here.

    Every answer is a string the caller sends back; an addressed command is
    answered with that pair's colour in front, so the reply is as easy to
    place as the message that prompted it.
    """
    cmd = parse_command(text)
    if not cmd["cmd"]:
        return None
    if cmd["error"]:
        return "Not done: %s." % cmd["error"]
    name = cmd["cmd"]
    rule = TG_ADDRESSING.get(name, "one")

    if name == "status":
        return pinned_text()

    if name in ("verdict", "note", "rotate"):
        if name == "verdict":
            cands = waiting_projects()
        elif name == "rotate":
            cands = rotatable_projects()
        else:
            cands = known_projects()
        path, why, names = resolve_addr(cmd["addr"], reply_to, cands)
        if path is None and rule == "never" and not cmd["addr"] \
                and not reply_to:
            return ("Rotating replaces a session and cannot be undone, so it "
                    "is never done to a pair nobody named. Say @<project> or "
                    "reply to one of its messages. Candidates: %s."
                    % (", ".join(names) or "none"))
        if path is None:
            if not names:
                return "Nothing is waiting for that right now."
            return ("Not done: %s. Say @<project> or reply to one of its "
                    "messages. Waiting: %s." % (why or "no addressee",
                                                ", ".join(names)))
        if rule == "never" and not cmd["addr"] and not reply_to:
            return ("Rotating replaces a session and cannot be undone, so it "
                    "is never done to a pair nobody named. Say @%s to rotate "
                    "it." % name_of(path))
        mark = mark_for(path)
        if name == "verdict":
            waiter = PENDING.get(path)
            if not waiter:
                return "%s %s: no report is waiting." % (mark, name_of(path))
            # The door beside the door. This path answered the waiter
            # directly, so a verdict typed in the chat reached the executor
            # without meeting the gate the tool and the HTTP endpoint both
            # go through. A lock on one of the ways in is not a lock on the
            # resource.
            okg, whyg, kindg = verdict_gate(path, cmd["verdict"], cmd["text"])
            if not okg:
                return ("%s %s: NOT accepted. %s" % (mark, name_of(path),
                                                     whyg))
            if kindg == "none":
                note_no_artifacts(path, name_of(path), whyg)
            waiter["verdict"] = cmd["verdict"]
            waiter["feedback"] = cmd["text"]
            waiter["event"].set()
            clear_silence(path, name_of(path))
            return "%s %s: verdict %s delivered." % (mark, name_of(path),
                                                     cmd["verdict"])
        if name == "note":
            r = handle_cmd({"cmd": "note", "text": cmd["text"],
                            "project": path})
            if not r.get("ok"):
                return "%s %s: not noted - %s." % (mark, name_of(path),
                                                   r.get("error") or "?")
            return ("%s %s: noted, and it goes to the planner with the next "
                    "report." % (mark, name_of(path)))
        threading.Thread(target=rotate_executor,
                         args=(path, "asked for over telegram"),
                         daemon=True).start()
        return "%s %s: rotating the executor." % (mark, name_of(path))

    # pause / resume / loop - the whole bridge unless a pair is named
    path = None
    if cmd["addr"] or reply_to:
        path, why, names = resolve_addr(cmd["addr"], reply_to,
                                        known_projects())
        if path is None and cmd["addr"]:
            return ("Not done: %s. Known projects: %s."
                    % (why or "no such project", ", ".join(names) or "none"))
    if name == "loop":
        if path:
            handle_loop({"action": "start", "project": path})
            return "%s %s: loop on." % (mark_for(path), name_of(path))
        started = start_stopped_loops()
        return ("Loop on for %s." % ", ".join(started)) if started \
            else "No stopped loop to start."
    body = {"cmd": name}
    if path:
        body["project"] = path
    handle_cmd(body)
    if path:
        return "%s %s: %s." % (mark_for(path), name_of(path),
                               "held" if name == "pause" else "running again")
    return ("Paused. Reports are held." if name == "pause"
            else "Running again.")


def run_telegram_button(data):
    """A button press. The pair it is about travels in the data, after |."""
    raw = (data or "").strip()
    act, _, pid = raw.partition("|")
    act = act.lower().strip()
    path = path_of_pair_id(pid.strip()) if pid.strip() else None
    if act in ("pause", "resume"):
        handle_cmd({"cmd": act})
        return None
    if act == "status":
        return pinned_text()
    if act == "start the loop":
        if path:
            handle_loop({"action": "start", "project": path})
            return "%s %s: loop on." % (mark_for(path), name_of(path))
        started = start_stopped_loops()
        return ("Loop on for %s." % ", ".join(started)) if started \
            else "No stopped loop to start."
    if act.startswith("restart "):
        role = act.split()[1]
        # Deliberately a fan-out when no pair is named, and it must stay
        # one: "restart" means "bring back whatever fell over", it is cheap,
        # it is repeatable, and a window that is already up is not touched
        # by it. This is the single command where answering for every pair
        # at once is the useful answer rather than the careless one - do not
        # tidy it away with the others.
        done = []
        for key in list(STATE.get("down") or {}):
            p2, _, rr = key.rpartition("|")
            if rr != role:
                continue
            if path and norm(p2) != path:
                continue
            restart_session(p2, rr)
            done.append(name_of(p2))
        if not done:
            return "Nothing was down."
        return "Restarting the %s of %s." % (role, ", ".join(done))
    if act == "accept name":
        for key in ([path] if path else list(NAMEWAIT)):
            w = NAMEWAIT.get(norm(key or ""))
            if w:
                w["name"] = w["suggested"]
                w["event"].set()
        return None
    return None


def telegram_poll():
    offset = 0
    while True:
        tg = CFG.get("telegram", {})
        token, chat = tg.get("token"), tg.get("chat_id")
        if not token or not chat:
            time.sleep(10)
            continue
        try:
            url = (telegram.api_url(token, "getUpdates")
                   + "?timeout=50&offset=%d" % offset)
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                telegram_note(True)
            except urllib.error.HTTPError as exc:
                # A revoked or regenerated token answers 401 here and the
                # loop used to swallow it and retry for ever: no messages, no
                # buttons, and nothing anywhere saying why. Telegram is the
                # one channel that reports on everything else, so when it
                # stops working that has to arrive somewhere - the journal
                # and the panel, since it obviously cannot arrive by
                # Telegram.
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    body = {}
                telegram_note(False, body.get("description")
                              or "HTTP %s" % exc.code, exc.code)
                time.sleep(30)
                continue
            except Exception as exc:
                telegram_note(False, "did not reach Telegram: %s" % exc)
                time.sleep(15)
                continue
            for upd in data.get("result", []):
                offset = max(offset, upd.get("update_id", 0) + 1)
                cb = upd.get("callback_query")
                if cb:
                    ok = str((cb.get("from") or {}).get("id")) == str(chat) \
                        or str(((cb.get("message") or {}).get("chat") or {})
                               .get("id")) == str(chat)
                    said = ""
                    if ok:
                        try:
                            said = run_telegram_button(cb.get("data") or "")
                        except Exception:
                            said = "That button hit an error - see the journal."
                    # The answer to a press is a toast, never a message.
                    # Every tap used to leave a line in the chat for ever,
                    # which is how a chat meant for three kinds of thing
                    # filled up with confirmations of things you had just
                    # done yourself. A press that has a long answer - the
                    # status - refreshes the status message instead, which
                    # is the one that is edited in place anyway.
                    long_answer = len(said or "") > 180
                    if long_answer:
                        refresh_pin(force=True)
                    telegram.answer_callback(
                        token, cb.get("id"),
                        "status updated above" if long_answer
                        else (said or "done"))
                    continue
                msg = upd.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != str(chat):
                    continue
                textmsg = (msg.get("text") or "").strip()
                reply = (msg.get("reply_to_message") or {}).get("message_id")
                # A reply while a handover is asking for a name is a name,
                # not a command - and it belongs to the handover it is a
                # reply to, not to whichever one happens to be waiting.
                if reply and NAMEWAIT and not parse_command(textmsg)["cmd"]:
                    want = MSGPROJ.get(int(reply))
                    for key in ([want] if want else list(NAMEWAIT)):
                        w = NAMEWAIT.get(norm(key or ""))
                        if w:
                            w["name"] = textmsg[:60]
                            w["event"].set()
                    continue
                try:
                    said = run_telegram_command(textmsg, reply_to=reply)
                except Exception:
                    said = "That did not work - see the journal."
                if said:
                    telegram.send(CFG, said, "silent")
        except Exception:
            time.sleep(5)


# ---------------------------------------------------------------------------
# resume report

def resume_report():
    projects = list(CFG.get("projects", {})) or sorted(
        {s.get("path") for s in STATE.get("sessions", {}).values()
         if s.get("path")})
    out = []
    for p in projects:
        if not p or not os.path.isdir(p):
            continue
        entry = {"path": p, "name": project_name(p), "sessions": [],
                 "changes": []}
        newest = {}
        for key, s in STATE.get("sessions", {}).items():
            if norm(s.get("path")) != norm(p) or not managed(s.get("role")):
                continue
            role = s.get("role")
            prev = newest.get(role)
            if not prev or seen_at(s) >= seen_at(prev):
                newest[role] = s
        for s in newest.values():
            infl = dict((STATE.get("inflight", {}) or {}).get(norm(p), {}))
            for k2, m2 in PROCTRACK.get(norm(p), {}).items():
                infl.setdefault(k2, m2)
            inflight = [m2["cmd"] for m2 in infl.values()]
            tag = "a build was running" if inflight else (
                "needs a heads-up" if s.get("state") not in
                ("idle", "ended", None) else "nothing in the air")
            entry["sessions"].append({
                "live": bool(already_up(p, s.get("role"))),
                "role": s.get("role"), "model": s.get("model"),
                "pct": s.get("context_pct"),
                "session_id": s.get("session_id"),
                "last_turn": s.get("last_turn", ""),
                "state": s.get("state", ""), "tag": tag,
                "inflight": inflight})
        try:
            r = subprocess.run(["git", "status", "--porcelain"], cwd=p,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
            if r.returncode == 0:
                entry["changes"] = [l for l in r.stdout.splitlines()][:20]
        except Exception:
            pass
        out.append(entry)
    return out


def do_resume(body):
    path = norm(body.get("project") or "")
    scope = body.get("scope") or "project"
    roles = [body.get("role")] if scope == "session" else \
        ["executor", "planner"]
    started = []
    skipped = []
    for role in roles:
        if not role:
            continue
        up = already_up(path, role)
        store.journal("session", "Resume check for the %s: %s"
                      % (role, up or "nothing answering - will start it"),
                      project_name(path), role, "log", project_dir=path)
        if up and not body.get("force"):
            skipped.append("%s (%s)" % (role, up))
            store.journal("session", "Resume skipped the %s - %s"
                          % (role, up), project_name(path), role, "log",
                          project_dir=path)
            continue
        sid = last_session_id(path, role)
        try:
            stop_reason = launch_guard(path, role)
            if stop_reason:
                return {"ok": False, "error": stop_reason,
                        "started": started}
            note_launch(path, role, "you pressed continue on the resume tab")
            pid = sessions.launch(path, role, resume_id=sid,
                                  permission_mode=mode_for(path, role),
                                  disallow=disallow_for(path, role),
                                  autocompact_pct=compact_pct(path))
            reg_pid(path, role, pid, sid, autocompact=compact_pct(path))
            started.append(role)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "started": started}
    with _lock:
        STATE["mode"] = "running"
        (STATE.get("inflight") or {}).pop(path, None)
        save_state()
    return {"ok": True, "started": started, "skipped": skipped}


# ---------------------------------------------------------------------------
# crash bundle + repair

def crash_bundle(exc_text):
    d = os.path.join(ROOT, "crashes", time.strftime("%m%d-%H%M%S"))
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "traceback.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(exc_text)
        with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(STATE, fh, indent=2, ensure_ascii=False)
        with open(os.path.join(d, "events.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(store.recent_events(50), fh, indent=2,
                      ensure_ascii=False)
    except Exception:
        pass
    return d


def do_repair():
    plan = os.path.join(store.DATA, "fix-plan.md")
    if not os.path.exists(plan):
        return {"ok": False,
                "error": "No fix-plan.md yet. Talk it through with the "
                         "planner in the app first; when you two have a "
                         "plan, have it written to data/fix-plan.md."}
    try:
        pid = sessions.launch(ROOT, "executor", permission_mode="acceptEdits")
        return {"ok": True, "pid": pid,
                "note": "A repair session opened in the bridge folder with "
                        "the plan. It commits when done."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def do_rollback():
    try:
        r = subprocess.run(["git", "-C", ROOT, "checkout", "--", "."],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return {"ok": r.returncode == 0, "out": (r.stderr or r.stdout)[:300]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# http server

def _query(path):
    """The query string of a GET, as a plain dict of single values.

    Windows paths arrive percent-encoded and full of backslashes, which
    parse_qs handles and hand-rolled splitting does not.
    """
    try:
        q = urllib.parse.urlparse(path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items() if v}
    except Exception:
        return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            # the client hung up before reading the answer (status line and
            # hooks do that under time pressure) - nothing to act on
            pass

    def do_GET(self):
        try:
            if self.path.startswith("/state"):
                # ?project=<path> narrows the feed to one pair (plus the
                # lines that are about the bridge itself). Everything else
                # in the payload is already keyed by project and the panel
                # picks what it needs; the feed is the one part that has to
                # be cut down here, because the cut is what loses rows.
                want_feed = _query(self.path).get("project") or None
                with _lock:
                    STATE["channels_view"] = {
                        "%s|%s" % k: {"online": time.time() - c["ts"] < 120}
                        for k, c in CHANNELS.items()}
                return self._send(200, {
                    "state": STATE,
                    "events": store.recent_events(40, project=want_feed),
                    "feed_project": norm(want_feed) if want_feed else "",
                    # One row per pair for the strip above the panel. Its
                    # words come from the same place the pin's do, so the
                    # two cannot end up disagreeing about a project.
                    "pairs": pairs_view(),
                    "headline": status_headline(),
                    "canon": {p: norm(p) for p in CFG.get("projects", {})},
                    "loop_off": STATE.get("loop_off") or {},
                    "caps": {p: {"iteration": l.get("iteration", 0)}
                             for p, l in (STATE.get("loops") or {}).items()},
                    "plans": {k: plan_for(v, v.get("path") or "")
                              for k, v in (STATE.get("sessions") or {}).items()
                              if v.get("state") not in ("ended", "died")},
                    "assessed": STATE.get("assessed") or {},
                    "life": {k: life_view(v, v.get("path") or "")
                             for k, v in (STATE.get("sessions") or {}).items()
                             if v.get("state") not in ("ended", "died")},
                    "walls": {k: wall_view(v, v.get("path") or "")
                              for k, v in (STATE.get("sessions") or {}).items()
                              if v.get("state") not in ("ended", "died")},
                    "calibration": store.load_calibration(),
                    "profiles": store.load_profiles(),
                    "models_registry": store.load_models(),
                    "model_probe_running": bool(
                        STATE.get("model_probe_running")),
                    "models": models.known(CFG, store.load_models()),
                    "config": {
                        "telegram": {
                            "connected": bool(CFG["telegram"].get("chat_id")),
                            "token_set": bool(CFG["telegram"].get("token"))},
                        "notify": CFG.get("notify", {}),
                        "thresholds": CFG.get("thresholds", {}),
                        "retention": CFG.get("retention", {}),
                        "projects": CFG.get("projects", {})}})
            if self.path.startswith("/projects"):
                rows = discover.scan()
                known = {norm(r["path"]) for r in rows}
                for p in CFG.get("projects", {}):
                    if norm(p) in known:
                        continue
                    rows.insert(0, {
                        "path": p,
                        "name": os.path.basename(p.rstrip("\\/")) or p,
                        "exists": os.path.isdir(p),
                        "installed": True, "sessions": 0,
                        "last_used": "not used yet", "seen": 0})
                return self._send(200, {"ok": True, "projects": rows})
            if self.path.startswith("/honesty"):
                # Read-only on purpose. The file is on disk and any editor
                # opens it; a write endpoint would be a new way to change
                # what every pair is told, reachable from a browser, for
                # the sake of saving a double-click.
                return self._send(200, {"ok": True, "path": HONESTY,
                                        "text": honesty_text()})
            if self.path.startswith("/remote"):
                return self._send(200, {"ok": True,
                                        "remote": remote.status()})
            if self.path.startswith("/resume-report"):
                return self._send(200, {"ok": True,
                                        "report": resume_report(),
                                        "running": len(
                                            sessions.claude_processes()),
                                        "reason": STATE.get(
                                            "recovered_reason"),
                                        "recovered": STATE.get("mode") ==
                                        "recovered"})
            if self.path.startswith("/logs-view"):
                proj = list(CFG.get("projects", {}))
                return self._send(200, {
                    "ok": True,
                    "disk": store.logs_disk_by_project(proj),
                    "index": {p: store.read_index(p, 20) for p in proj},
                    "handoff": {p: store.read_handoff(p)[:2000]
                                for p in proj}})
            with open(PANEL, "rb") as fh:
                return self._send(200, fh.read(),
                                  "text/html; charset=utf-8")
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError):
            return          # browser closed a poll mid-flight - routine
        except Exception:
            store.journal("bridge_error", traceback.format_exc()[-400:],
                          level="sound")
            return self._send(500, {"error": "panel error - see journal"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        try:
            p = self.path
            if p.startswith("/event"):
                out, row = handle_event(body)
                return self._send(200, {"ok": True, "hook_output": out})
            if p.startswith("/status"):
                handle_status(body)
                return self._send(200, {"ok": True})
            if p.startswith("/cmd"):
                return self._send(200, handle_cmd(body))
            if p.startswith("/channel/register"):
                if self.headers.get("X-Bridge-Secret") != SECRET:
                    return self._send(403, {"error": "bad secret"})
                path = norm(body.get("project"))
                role = (body.get("role") or "unknown").lower()
                if not managed(role):
                    # channel.py falls back to "unknown" when the window has
                    # no BRIDGE_ROLE - which also happens to one of ours when
                    # the variable does not reach the MCP subprocess. It is
                    # registered like any other; refusing it here is what
                    # stopped a real session's channel from ever being
                    # reachable.
                    note_stranger(path, body.get("session_id"))
                first = (path, role) not in CHANNELS
                CHANNELS[(path, role)] = {"port": int(body.get("port") or 0),
                                          "ts": time.time(),
                                          "pid": body.get("pid")}
                with _lock:
                    STATE.setdefault("channels", {})["%s|%s" % (path, role)] \
                        = {"port": int(body.get("port") or 0),
                           "at": time.time()}
                    save_state()
                mark_registered(path, role)
                with _lock:
                    have = [k for k, v in (STATE.get("sessions") or {}).items()
                            if norm(v.get("path")) == path
                            and v.get("role") == role
                            and v.get("state") not in ("ended", "died")]
                    if not have:
                        # the window is up - it may still be sitting on its
                        # startup dialog, which fires no hooks, but it exists
                        # and the panel should say so rather than "nothing
                        # running"
                        old = last_telemetry(path, role) or {}
                        # keyed by project as well: "<role>:channel"
                        # was one key for every project, so two
                        # pairs overwrote each other exactly as
                        # "<role>:seen" did.
                        STATE.setdefault("sessions", {})[
                            "%s:channel:%s" % (role,
                                               pair_id(path))] = {
                                "role": role, "path": path,
                                "managed": managed(role),
                                "project": project_name(path),
                                "state": "starting",
                                "first_seen": now(), "last_seen": now(),
                                "seen_at": time.time(),
                                "session_id": last_session_id(path, role)
                                or "",
                                "window": old.get("window"),
                                "model": old.get("model"),
                                "context_tokens": old.get("context_tokens"),
                                "context_pct": old.get("context_pct"),
                                "turn_costs": old.get("turn_costs") or [],
                                "stale": old.get("at")}
                        save_state()
                    prune_sessions()
                    threading.Thread(target=refresh_from_disk,
                                     args=(path, role), daemon=True).start()
                if first:
                    store.journal("channel", "%s channel connected" % role,
                                  project_name(path), role, "log",
                                  project_dir=path)
                if role == "planner":
                    for item in QUEUED.pop(path, []):
                        try:
                            parsed = json.loads(item)
                            deliver(path, "planner", parsed["content"],
                                    parsed["meta"])
                        except Exception:
                            pass
                return self._send(200, {"ok": True})
            if p.startswith("/channel/unregister"):
                key = (norm(body.get("project")),
                       (body.get("role") or "unknown").lower())
                CHANNELS.pop(key, None)
                with _lock:
                    (STATE.get("channels") or {}).pop("%s|%s" % key, None)
                    save_state()
                return self._send(200, {"ok": True})
            if p.startswith("/verdict"):
                if self.headers.get("X-Bridge-Secret") != SECRET:
                    return self._send(403, {"error": "bad secret"})
                path = norm(body.get("project"))
                waiter = PENDING.get(path)
                v = (body.get("verdict") or "continue").lower()
                fb = body.get("feedback") or ""
                # The gate runs FIRST and touches nothing. A refused verdict
                # must cost the report nothing: PENDING is left exactly as it
                # was, so the executor is still waiting, the planner can
                # answer again, and "one verdict per report" still holds -
                # the refusal was not a verdict.
                okg, whyg, kindg = verdict_gate(path, v, fb)
                if not okg:
                    store.journal(
                        "verdict_refused",
                        "Refused a '%s' verdict - %s" % (v, brief(whyg, 200)),
                        project_name(path), "planner", "warn",
                        extra={"full": whyg}, project_dir=path)
                    return self._send(200, {"ok": False, "refused": True,
                                            "error": whyg})
                store.journal("verdict", "Planner: %s - %s" % (v, fb[:160]),
                              project_name(path), "planner", "log",
                              extra={"full": fb[:4000]}, project_dir=path)
                if kindg == "none":
                    note_no_artifacts(path, project_name(path), whyg)
                if waiter:
                    waiter["verdict"] = v
                    waiter["feedback"] = fb
                    waiter["event"].set()
                    return self._send(200, {"ok": True, "delivered": True})
                # No report was waiting, but a live verdict still proves the
                # planner is back - so a pair held for silence is let go here
                # too, rather than waiting for a report nobody would make.
                clear_silence(path, project_name(path))
                # No Stop hook is waiting - the executor is idle at its
                # prompt. The verdict still has somewhere to go, and this is
                # the path the loop falls back on when the planner's task
                # tool cannot reach the bridge: everything it wants to say
                # travels as feedback instead. So it re-arms the loop the
                # same way a task does, answers at once, and injects on a
                # thread - the 20s wait that used to live here is the same
                # trap that made /task look like a dead bridge.
                _, lp2 = loop_state(path)
                if not lp2.get("active") and fb:
                    lp2["active"] = True
                    save_state()
                    store.journal("loop", "Loop re-armed - the planner sent "
                                  "work as a verdict", project_name(path),
                                  "planner", "log", project_dir=path)
                ok, why = task_reachable(path)
                if ok:
                    threading.Thread(
                        target=deliver_task_later,
                        args=(path, "Verdict from the planner: %s\n\n%s"
                              % (v, fb)), daemon=True).start()
                    return self._send(200, {"ok": True, "delivered": True,
                                            "note": "the executor was idle, "
                                                    "so this went straight "
                                                    "into its window"})
                inbox = store.inbox_write(path, 0,
                                          "VERDICT %s\n\n%s" % (v, fb))
                notify("needs_you",
                       "%s: the planner answered but the executor could not "
                       "be reached - %s. Saved to %s."
                       % (project_name(path), why, inbox), path=path)
                return self._send(200, {"ok": True, "delivered": False,
                                        "why": why})
            if p.startswith("/task"):
                if self.headers.get("X-Bridge-Secret") != SECRET:
                    return self._send(403, {"error": "bad secret"})
                path = norm(body.get("project"))
                text = (body.get("instructions") or "").strip()
                if not text:
                    _, lp0 = loop_state(path)
                    return self._send(200, {
                        "ok": False,
                        "error": "the task arrived with no instructions in "
                                 "it, so there was nothing to send. The "
                                 "bridge is running%s"
                                 % ("" if lp0.get("active") else
                                    " and the loop is off - sending a real "
                                    "task turns it back on")})
                name = project_name(path)
                note_task_arrived(path)
                # The planner says whether this piece is visual; the bridge
                # never guesses. An earlier sketch tried to infer it from the
                # words of the report - "render", "screenshot", "frame" -
                # that is a heuristic that would both nag text-only work and
                # miss visual work described in other words. A marker the
                # planner writes costs it four characters and is never wrong.
                # It rides in the instructions text, so the tool signature is
                # untouched and an older planner is unaffected.
                want_frames = any(m in text.upper()
                                  for m in FRAMES_MARKS)
                with _lock:
                    fr = STATE.setdefault("frames", {})
                    if want_frames:
                        fr[path] = True
                    else:
                        fr.pop(path, None)
                    save_state()
                # sending work means expecting a report back, so re-arm the
                # loop if a previous "done" had closed it - otherwise the
                # executor works and nobody is listening
                _, lp = loop_state(path)
                if not lp.get("active"):
                    lp["active"] = True
                    save_state()
                    store.journal("loop", "Loop re-armed - the planner sent "
                                  "the executor a task", name, "planner",
                                  "log", project_dir=path)
                    notify("iteration_done",
                           "%s: the planner handed the executor new work, so "
                           "the loop is on again." % name, level="silent")
                # Answer the planner before delivering, not after.
                #
                # This endpoint used to perform the injection inline, which
                # can take up to 20s while the executor's channel is busy -
                # and the planner's own call gives up after 8. So /verdict,
                # which returns at once, always worked, and /task, which did
                # the same journey plus a wait, always looked like "the
                # bridge daemon is not reachable". The planner reported that
                # about a bridge that was answering it perfectly well.
                #
                # What is checked here is only whether the executor's channel
                # port is listening - about a second at worst. The injection
                # itself, with retries, happens on a thread.
                sent, why = task_reachable(path)
                store.journal("task", "Planner -> executor: %s"
                              % text.splitlines()[0][:120], name, "planner",
                              "log", extra={"full": text[:4000]},
                              project_dir=path)
                if sent:
                    threading.Thread(target=deliver_task_later,
                                     args=(path, text), daemon=True).start()
                else:
                    inbox = store.inbox_write(path, 0, "TASK\n\n" + text)
                    notify("needs_you",
                           "%s: the planner sent the executor a task and it "
                           "did not arrive - %s. The bridge itself is fine; "
                           "it is the executor's channel that did not take "
                           "it. Saved to %s:\n\n%s"
                           % (name, why, inbox, text[:1500]), path=path)
                return self._send(200, {"ok": True, "delivered": bool(sent),
                                        "why": None if sent else why})
            if p.startswith("/selftest"):
                return self._send(200, selftest(norm(body.get("project"))))
            if p.startswith("/loop"):
                return self._send(200, handle_loop(body))
            if p.startswith("/session"):
                return self._send(200, handle_session(body))
            if p.startswith("/handover"):
                path = norm(body.get("project"))
                roles = body.get("roles")
                if not roles:
                    one = body.get("role")
                    roles = [one] if one else ["executor", "planner"]
                roles = [r for r in roles if r in ("executor", "planner")]
                if not roles:
                    return self._send(200, {"ok": False,
                                            "error": "no such role"})
                threading.Thread(
                    target=handover,
                    args=(path, body.get("reason") or "asked for from the "
                          "panel", tuple(roles)), daemon=True).start()
                return self._send(200, {"ok": True, "roles": roles})
            if p.startswith("/rotate"):
                path = norm(body.get("project"))
                threading.Thread(target=rotate_executor,
                                 args=(path, "manual"), daemon=True).start()
                return self._send(200, {"ok": True})
            if p.startswith("/resume"):
                return self._send(200, do_resume(body))
            if p.startswith("/repair"):
                return self._send(200, do_repair())
            if p.startswith("/rollback"):
                return self._send(200, do_rollback())
            if p.startswith("/verify-archives"):
                path = norm(body.get("project"))
                return self._send(200, {"ok": True,
                                        "result": store.verify_archives(path)})
            if p.startswith("/archive-map"):
                # Rebuild on demand, but never wedge the request thread on
                # it: the build runs on its own thread and this waits a
                # bounded time for it. A big archive that outlasts the wait
                # answers with the last map and says it is still building,
                # rather than holding the connection open (§6).
                path = norm(body.get("project"))
                if not path or not os.path.isdir(path):
                    return self._send(400, {"error": "no such project"})
                out = {}
                t = archive.rebuild_async(path, known_sessions(),
                                          done=lambda m: out.setdefault("m", m))
                if t is not None:
                    t.join(float(body.get("wait") or 20))
                fresh = out.get("m")
                m = fresh or archive.last_map(path)
                return self._send(200, {
                    "ok": True, "map": m, "rebuilding": fresh is None,
                    "why": "" if fresh else
                           "a rebuild is still running; this is the map from "
                           "the last one"})
            if p.startswith("/archive-search"):
                # Two requests in one endpoint: ask a question, or ask what
                # happened to a question. Neither does any of the work on
                # this thread - the agent takes minutes.
                if body.get("run_id"):
                    r = archive.get_run(body["run_id"])
                    return self._send(200, {"ok": bool(r), "run": r,
                                            "error": "" if r else
                                            "no run by that id - the bridge "
                                            "keeps the last few in memory "
                                            "only, and extracts on disk"})
                # The project is named or the request is refused. It used to
                # fall back to the first project in the config, which read
                # as a convenience while there was only ever one - with
                # several pairs it silently searched somebody else's
                # archive and answered about the wrong work.
                path = norm(body.get("project"))
                if not path:
                    return self._send(400, {
                        "error": "no project in the request, and there is no "
                                 "sensible one to guess - say which archive "
                                 "to search",
                        "projects": [project_name(p)
                                     for p in CFG.get("projects", {})]})
                if not os.path.isdir(path):
                    return self._send(400, {"error": "no such project"})
                rid, why = archive.search_async(
                    path, (body.get("question") or "").strip(),
                    parallel=CFG.get("archive_parallel")
                    or archive.DEFAULT_PARALLEL,
                    model=CFG.get("archive_model")
                    or archive.DEFAULT_MODEL,
                    timeout=CFG.get("archive_timeout")
                    or archive.DEFAULT_TIMEOUT,
                    claude=CFG.get("archive_claude") or "claude",
                    journal=lambda kind, text, level="log": store.journal(
                        kind, text, project_name(path), "archive", level,
                        project_dir=path))
                if not rid:
                    return self._send(200, {"ok": False, "error": why,
                                            "runs": archive.recent_runs(5)})
                return self._send(200, {"ok": True, "run_id": rid,
                                        "run": archive.get_run(rid)})
            if p.startswith("/archive-now"):
                ret = CFG.get("retention", {})
                packed = sum(store.archive_old(pp, ret.get("days", 7),
                                               ret.get("size_gb", 2))
                             for pp in CFG.get("projects", {}))
                return self._send(200, {"ok": True, "packed": packed})
            if p.startswith("/links/push"):
                for sk, sv in list((STATE.get("sessions") or {}).items()):
                    if sv.get("state") in ("ended", "died", None):
                        continue
                    got = find_rc_link(transcript_candidates(
                        sv.get("session_id"), sv.get("path")),
                        sv.get("session_id"))
                    if got:
                        with _lock:
                            STATE.setdefault("rc", {})[
                                "%s|%s" % (norm(sv.get("path")),
                                           sv.get("role"))] = got
                            save_state()
                # force only when the panel says so - see push_links.
                sent = push_links(force=bool(body.get("force")))
                return self._send(200, {"ok": True, "rc": STATE.get("rc"),
                                        "sent": sent,
                                        "forced": bool(body.get("force"))})
            if p.startswith("/models/refresh"):
                return self._send(200, {"ok": start_probe(),
                                        "note": "probing in the background"})
            if p.startswith("/models/options"):
                m = store.load_models()
                for k in ("prefer_aliases", "reread_on_launch",
                          "allow_best"):
                    if k in body:
                        m["opts"][k] = bool(body[k])
                store.save_models(m)
                return self._send(200, {"ok": True, "opts": m["opts"]})
            if p.startswith("/config"):
                with _lock:
                    for key in ("notify", "thresholds", "retention",
                                "projects", "quiet_when_present",
                                "presence_file", "custom_models"):
                        if key in body:
                            CFG[key] = body[key]
                    store.save_config(CFG)
                return self._send(200, {"ok": True})
            if p.startswith("/remove-project"):
                from . import install as installer
                target = norm(body.get("path") or "")
                try:
                    removed = installer.uninstall(target)
                except Exception as exc:
                    return self._send(200, {"ok": False, "error": str(exc)})
                with _lock:
                    (CFG.get("projects") or {}).pop(target, None)
                    for k in list(CFG.get("projects") or {}):
                        if norm(k) == target:
                            CFG["projects"].pop(k, None)
                    store.save_config(CFG)
                    (STATE.get("loops") or {}).pop(target, None)
                    for d0 in ("rc", "down", "pids", "inflight"):
                        for k in list(STATE.get(d0) or {}):
                            if k.startswith(target + "|") or k == target:
                                STATE[d0].pop(k, None)
                    save_state()
                store.journal("project", "Stopped watching %s"
                              % os.path.basename(target), level="log")
                return self._send(200, {"ok": True, "removed": removed})
            if p.startswith("/add-project"):
                return self._send(200, handle_add_project(body))
            if p.startswith("/remote"):
                try:
                    st = remote.set_enabled(bool(body.get("enabled", True)))
                    return self._send(200, {"ok": True, "remote": st})
                except Exception as exc:
                    return self._send(200, {"ok": False, "error": str(exc)})
            if p.startswith("/telegram/"):
                return self._send(200, handle_telegram(p.split("/")[-1],
                                                       body))
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError):
            return          # client gave up waiting - the work itself is done
        except Exception:
            tb = traceback.format_exc()
            d = crash_bundle(tb)
            store.journal("bridge_error", "Daemon error - bundle %s" % d,
                          level="sound")
            # No path on purpose, and not because there is none in scope:
            # this is the catch-all for the whole endpoint, so `path` here
            # is whatever the branch that failed happened to have set - or
            # nothing at all, if it failed before setting one. The message
            # is about the bridge anyway.
            notify("crash", "The bridge hit an error. Crash bundle: %s\n"
                   "Talk it over with the planner; when there is a plan in "
                   "data/fix-plan.md, press Repair." % d,
                   buttons=["status"])
            return self._send(500, {"error": "bridge error", "bundle": d})
        return self._send(404, {"error": "no such endpoint"})


def handle_loop(body):
    pdir = body.get("project") or ""
    path, lp = loop_state(pdir)
    act = body.get("action")
    name = project_name(path)
    if act == "start":
        with _lock:
            (STATE.get("loop_off_told") or {}).pop(path, None)
            (STATE.get("loop_off") or {}).pop(path, None)
            save_state()
        prune_sessions()
        if body.get("reset"):
            lp["iteration"] = 0
        lp["active"] = True
        save_state()
        # Starting the loop is an answer to "it was idling", so the
        # count goes with it and anything held is let go at once.
        clear_spin(path)
        wake_idle(path)
        store.journal("loop", "Loop started at iteration %d"
                      % lp.get("iteration", 0), name, level="log",
                      project_dir=path)
        deliver(path, "planner", "The loop is on. The executor's next "
                "finished turn arrives here as a report.", {"kind": "info"})
        # Not a chat message. Switching the loop on is something you did,
        # and telling you that it worked is not news you would get up for -
        # it is on the panel, in the journal, and in the reply to whatever
        # you pressed or typed. With three pairs these were most of the
        # traffic in the chat.
        return {"ok": True}
    if act == "stop":
        deactivate_loop(path, "you pressed stop in the panel")
        waiter = PENDING.get(path)
        if waiter:
            waiter["verdict"] = "wait"
            waiter["feedback"] = "loop stopped by the human"
            waiter["event"].set()
        return {"ok": True}
    return {"ok": False, "error": "unknown action"}


def handle_session(body):
    act = body.get("action")
    project = body.get("project") or ""
    role = body.get("role") or "executor"
    if act == "launch":
        try:
            maybe_auto_probe()
            # counted, but never blocked: you are looking at the screen, so
            # the guard is there to stop the bridge looping, not you
            note_launch(project, role, "you pressed start in the panel")
            pid = sessions.launch(project, role,
                                  resume_id=body.get("resume_id"),
                                  model=models.resolve(body.get("model"),
                                                       store.load_models()),
                                  permission_mode=(body.get("mode") or
                                                   mode_for(project, role)),
                                  disallow=disallow_for(project, role),
                                  autocompact_pct=compact_pct(project))
            reg_pid(project, role, pid, body.get("resume_id"),
                    model_req=body.get("model"),
                    autocompact=compact_pct(project))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        store.journal("session", "Started %s window" % role,
                      project_name(project), role, "log",
                      project_dir=norm(project))
        return {"ok": True, "pid": pid}
    if act == "stop":
        ok = sessions.stop(project, role, pid=pid_of(project, role))
        retire_sessions(project, role)
        with _lock:
            (STATE.get("down") or {}).pop("%s|%s" % (norm(project), role),
                                          None)
            save_state()
        return {"ok": ok}
    if act == "restart":
        return restart_session(project, role)
    if act == "past":
        try:
            return {"ok": True, "sessions": sessions.past_sessions(project)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "sessions": []}
    return {"ok": False, "error": "unknown action"}


def handle_add_project(body):
    from . import install as installer
    path = (body.get("path") or "").strip().strip('"')
    if not path:
        return {"ok": False, "error": "No folder given."}
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {"ok": False, "error": "There is no folder at %s" % path}
    try:
        added = installer.install(path, role="executor")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    store.journal("project", "Added project %s" % os.path.basename(path),
                  os.path.basename(path), level="log")
    with _lock:
        CFG.setdefault("projects", {}).setdefault(path, {})
        store.save_config(CFG)
    return {"ok": True, "added": added, "path": path}


def handle_telegram(step, body):
    global CFG
    if step == "token":
        token = (body.get("token") or "").strip()
        username = telegram.check_token(token)
        if not username:
            return {"ok": False,
                    "error": "That token was not accepted. Copy the whole "
                             "line BotFather sent, digits before the colon "
                             "included."}
        CFG.setdefault("telegram", {})["token"] = token
        store.save_config(CFG)
        return {"ok": True, "username": username}
    if step == "pair":
        chat, who = telegram.first_sender(CFG["telegram"].get("token", ""))
        if not chat:
            return {"ok": False, "error": "Nothing received yet. Open the "
                                          "bot and send it any word."}
        CFG["telegram"]["chat_id"] = chat
        store.save_config(CFG)
        return {"ok": True, "who": who}
    if step == "reveal":
        # localhost-only panel asking to display the saved token; it is
        # never included in /state - only here, on an explicit press
        return {"ok": True, "token": CFG.get("telegram", {}).get("token",
                                                                 "")}
    if step == "test":
        telegram.send(CFG, "The bridge is connected. This is the test "
                           "message.", "sound", ["status"])
        refresh_pin(force=True)
        return {"ok": True}
    return {"ok": False, "error": "unknown step"}


# ---------------------------------------------------------------------------

REASONS = {
    "reboot": "Power was lost or the machine restarted while work was in "
              "flight. Nothing was restarted on its own.",
    "killed": "The bridge was ended the hard way - window closed or process "
              "killed - while work was in flight. Nothing was restarted on "
              "its own.",
    "shutdown_mid_run": "The bridge was shut down normally, but a loop was "
                        "still running at that moment.",
}


def farewell(mid_run, live_roles):
    """Say goodbye on the way out: the pin must not keep claiming we are up.

    Windows gives a closing console only a few seconds, so this runs on a
    thread the caller waits on briefly and then leaves regardless.
    """
    when = time.strftime("%H:%M")
    if live_roles:
        tail = ("The %s window%s still open and will keep going unwatched - "
                "no reports, no rotation, no limit pauses until the bridge "
                "is back." % (" and ".join(sorted(live_roles)),
                              " is" if len(live_roles) == 1 else "s are"))
    else:
        tail = "No sessions were left running."
    head = "bridge - stopped at %s" % when
    try:
        cfg = telegram.pin_status(CFG, "%s\n%s" % (head, tail))
        store.save_config(cfg)
    except Exception:
        pass
    try:
        if mid_run:
            telegram.send(CFG, "The bridge was closed at %s while a loop was "
                          "running. %s" % (when, tail), "sound", ["status"])
        else:
            telegram.send(CFG, "The bridge was closed at %s. %s"
                          % (when, tail), "silent")
    except Exception:
        pass


_said_goodbye = [False]


def shutdown(*_):
    # Windows delivers Ctrl+C twice: once as SIGINT and once through the
    # console control handler, and both are wired here. That sent the
    # goodbye message twice - the same line, seconds apart, which reads in
    # the chat like the bridge stopped and then stopped again.
    with _lock:
        if _said_goodbye[0]:
            return
        _said_goodbye[0] = True
    with _lock:
        mid = any(lp.get("active") for lp in
                  STATE.get("loops", {}).values()) or \
            any((STATE.get("inflight") or {}).values())
        live_roles = {s.get("role") or "session"
                      for s in STATE.get("sessions", {}).values()
                      if s.get("state") not in ("ended", "died", None)}
        STATE["last_stop_mid_run"] = bool(mid)
        STATE["clean_shutdown"] = True
        STATE["mode"] = "idle"
        store.save_state(STATE)
    store.journal("bridge", "Bridge stopped cleanly.", level="log")
    if CFG.get("telegram", {}).get("chat_id"):
        t = threading.Thread(target=farewell, args=(mid, live_roles),
                             daemon=True)
        t.start()
        t.join(5)
    os._exit(0)


def main():
    global STATE
    prev_clean = STATE.get("clean_shutdown", True)
    prev_alive = STATE.get("alive_at") or 0
    prev_mid = STATE.get("last_stop_mid_run", False)
    stakes = any(lp.get("active") for lp in
                 STATE.get("loops", {}).values()) or \
        any((STATE.get("inflight") or {}).values())
    booted = boot_time()
    rebooted = bool(prev_alive and booted and booted > prev_alive + 60)

    reason = None
    if not prev_clean and stakes:
        reason = "reboot" if (rebooted or not prev_alive) else "killed"
    elif prev_clean and prev_mid and stakes:
        reason = "shutdown_mid_run"

    port = int(CFG.get("port", 8765))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        # state stays untouched on this path - a busy port must not look
        # like a dirty death next time
        print("\n  Could not listen on port %d.\n  %s\n" % (port, exc))
        print("  Most likely the bridge is already running - look for "
              "another console window, or open http://127.0.0.1:%d/ first."
              % port)
        raise SystemExit(1)

    with _lock:
        STATE["clean_shutdown"] = False       # armed only now, after bind
        STATE["last_stop_mid_run"] = False
        STATE["started_at"] = time.time()
        STATE["alive_at"] = time.time()
        STATE["mode"] = "recovered" if reason else "running"
        STATE["recovered_reason"] = reason
        store.save_state(STATE)

    migrate_note()
    migrate_executor_mode()
    moved = migrate_keys()
    if moved:
        store.journal("bridge", "Re-keyed %d stored entries to the canonical "
                      "path form" % moved, level="log")

    gone = forget_unmanaged()
    if gone:
        store.journal("bridge", "Dropped %d session record%s for windows "
                      "with no role - they belong to whoever opened them, "
                      "not to this pair: %s"
                      % (len(gone), "" if len(gone) == 1 else "s",
                         ", ".join(gone)), level="log")

    if reason:
        store.journal("recovery", REASONS[reason] + " Open the resume tab.",
                      level="sound")
        notify("crash", REASONS[reason] +
               " Open the bridge - the resume tab has the picture.")
    elif not prev_clean:
        store.journal("bridge", "Previous stop was not clean, but no loop "
                      "was running and nothing was in flight - carrying on.",
                      level="log")
    else:
        store.journal("bridge", "Bridge started.", level="log")

    def _baseline():
        # A rollback point for the repair button. It runs in the background:
        # on a cold folder the add+commit can take many seconds, and the
        # panel and the hooks must not wait on it.
        try:
            if subprocess.run(["git", "-C", ROOT, "rev-parse"],
                              capture_output=True, timeout=8).returncode != 0:
                subprocess.run(["git", "-C", ROOT, "init"],
                               capture_output=True, timeout=15)
                subprocess.run(["git", "-C", ROOT, "add", "-A"],
                               capture_output=True, timeout=60)
                subprocess.run(["git", "-C", ROOT, "commit", "-m",
                                "bridge: baseline"], capture_output=True,
                               timeout=60)
        except Exception:
            pass

    threading.Thread(target=_baseline, daemon=True).start()

    for sg in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sg, shutdown)
        except Exception:
            pass
    if os.name == "nt":
        # closing the console window, logging off or shutting down Windows
        # never delivers SIGTERM - catch the console control events so the
        # X button counts as a clean stop, not a fake power cut
        try:
            signal.signal(signal.SIGBREAK, shutdown)
        except Exception:
            pass
        try:
            import ctypes
            import ctypes.wintypes as wt
            routine = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)

            def _ctrl(ev):
                if ev in (2, 5, 6):   # close, logoff, shutdown
                    shutdown()
                return False

            _ref = routine(_ctrl)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_ref, True)
            globals()["_CTRL_REF"] = _ref
        except Exception:
            pass

    def _alive():
        while True:
            time.sleep(15)
            with _lock:
                STATE["alive_at"] = time.time()
                store.save_state(STATE)

    threading.Thread(target=_alive, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    print("Bridge listening on %s  (Ctrl+C to stop)" % url)
    if "--no-browser" not in os.sys.argv:
        try:
            import webbrowser
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:
            print("Open %s in your browser." % url)

    refresh_pin(force=True)
    threading.Thread(target=telegram_poll, daemon=True).start()
    threading.Thread(target=process_watch, daemon=True).start()
    threading.Thread(target=session_watch, daemon=True).start()
    threading.Thread(target=stall_watch, daemon=True).start()
    threading.Thread(target=idle_watch, daemon=True).start()
    threading.Thread(target=disk_watch, daemon=True).start()
    threading.Timer(2.0, lambda: reconcile()).start()
    maybe_auto_probe()   # fill the model registry before anyone launches
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
