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

"""Config, state and the event journal.

Two rules here matter more than anything else in this file:

* state.json is written atomically (temp file + replace), so a power cut
  can never leave a half-written state file behind.
* events.jsonl is append-only, so a power cut costs at most the last
  line rather than the whole log.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("BRIDGE_DATA") or os.path.join(ROOT, "data")
LOGS = os.path.join(DATA, "logs")

CONFIG_PATH = os.path.join(DATA, "config.json")
STATE_PATH = os.path.join(DATA, "state.json")
CALIB_PATH = os.path.join(DATA, "calibration.json")
MODELS_PATH = os.path.join(DATA, "models.json")
PROFILES_PATH = os.path.join(DATA, "profiles.json")

_lock = threading.RLock()


class SecondAuthority(RuntimeError):
    """A process that is not the daemon tried to write the live folder."""


# THE LIVE FOLDER HAS ONE WRITER. STATE lives in the daemon's memory and
# state.json is its serialisation - the right way round, because the
# memory is what every decision reads. A second process that imports the
# package with BRIDGE_DATA pointing at the live folder gets its OWN copy of
# STATE, as old as its import, and the first save_state() it makes writes
# that copy over the daemon's file. 2026-09-13 00:31:48: a measurement of
# run_check(all) - a script, not the daemon - saved a STATE it had loaded at
# 00:17:35 over the live state.json, fourteen minutes of the daemon's
# changes gone from disk; the daemon's own clean stop at 00:32:20 wrote its
# memory back, so nothing was lost - by the order of events, not by any
# rule. Had the daemon died in those 32 seconds the file would have been
# the stale copy. The same class had been caught before ("a second
# authority on live BRIDGE_DATA"), and rule 25 says a legal exception does
# not happen twice; this is the gate under it. -> DECISIONS.md 8.20
#
# Who may write: the daemon, which claims it in main() the moment the port
# check has passed (two daemons cannot both serve one port); and anybody
# whose BRIDGE_DATA is under the temp folder - a suite, a check copy, a
# throwaway - because that folder is nobody's live state. Everyone else is
# refused with the way out in the sentence.
_WRITER = {"daemon": False}


def claim_writer():
    """The daemon says it is the daemon. Called once, from main()."""
    _WRITER["daemon"] = True


def data_is_throwaway(data=None):
    """Is BRIDGE_DATA under the temp folder, i.e. a suite's or a copy's?"""
    try:
        tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        here = os.path.normcase(os.path.abspath(data or DATA))
    except Exception:
        return False
    return here == tmp or here.startswith(tmp + os.sep)


def second_authority():
    """The sentence refusing this process, or "" when it may write."""
    if _WRITER["daemon"] or data_is_throwaway():
        return ""
    return ("this process is not the daemon serving the bridge, and "
            "BRIDGE_DATA=%s is the LIVE folder - a second writer here puts "
            "its own stale copy of STATE over the daemon's file. Nothing was "
            "written. Point BRIDGE_DATA at a copy under the temp folder "
            "(the suites and run_check do), or ask the daemon over HTTP."
            % DATA)


def norm(p):
    """The canonical form of a path, used as a key everywhere.

    normcase matters and normpath alone is not enough. On Windows the same
    folder arrives spelled several ways: the config keeps whatever was typed
    when the project was added, hooks report what Claude Code was given, and
    the channel reports os.getcwd(), which is the real casing on disk.
    Without folding case, C:\\path\\GAME and C:\\path\\Game are
    two different keys - so a session registers under one and is looked for
    under the other, and every "is this already running" check quietly
    answers no.

    It lives here, in the lowest module, because two others need the same
    answer: daemon keys all of its state on it, and sessions keys the live
    process handles on it. Two implementations would drift, and a drifting
    key is the bug this function exists to prevent.

    Nothing in stays nothing out. os.path.normpath("") answers ".", which is
    a real directory - the one the daemon happens to be running in - so a
    missing path used to canonicalise into the bridge's own folder. An
    /archive-search request that named no project reached os.path.isdir(".")
    and searched the daemon's own working directory; the "fall back to the
    first project in the config" branch behind it had never once run.
    """
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))

DEFAULT_CONFIG = {
    "port": 8765,
    "telegram": {
        "token": "",
        "chat_id": "",
        "pinned_message_id": 0,
    },
    "notify": {
        "iteration_done": "silent",
        "verdict_changes": "silent",
        "model_dropped": "silent",
        "waiting_process": "silent",
        "process_stuck": "sound",
        "needs_you": "sound",
        "rotation_name": "sound",
        "limit_low": "sound",
        "crash": "sound",
        "session_start": "log",
        "session_end": "log",
        # the planner called the whole job finished
        "run_finished": "sound",
    },
    # Pair -> colour marker, assigned once and then left alone. See
    # daemon.mark_for: a colour that moved when a project was removed would
    # be worse than no colour at all.
    "marks": {},
    # The permission mode each role's window is started in, for every
    # project that does not name its own in projects[path]["modes"].
    #
    # The executor asks for nothing. It was "auto", which stopped being
    # workable when the client went from 2.1.227 to 2.1.232 and auto grew
    # stricter: one pair kept running because its window predated the
    # update, two newer ones asked for permission on every fresh shape of
    # command - 499 rules accumulated in one project's settings.local.json,
    # one click at a time, and it still asked.
    #
    # "dontAsk" was tried first and is not what its name suggests: it does
    # not ask AND does not do. Measured against a real client on a
    # throwaway project - "Write to made.txt -> denied (don't-ask mode)",
    # nothing written. Zero questions and zero work.
    #
    # So bypassPermissions, deliberately and with its cost stated: an
    # executor in this mode can read and write outside its project, which
    # was confirmed in the same test rather than assumed. It is here, in
    # the config, so it can be changed back without touching code.
    #
    # THE PLANNER IN `auto` - the owner's word, 2026-09-30: planners always
    # start in auto mode, here and in the public version. Its protection was never
    # its mode: disallow_for denies its edit tools and its shell, and a deny
    # outranks every mode. What plan mode added was a person asked before the
    # verdict tool ran (8.26). migrate_planner_mode moves the saved defaults
    # of the day once. -> DECISIONS.md 8.59
    "role_modes": {"executor": "bypassPermissions", "planner": "auto"},
    # THE PLANNER'S EFFORT - the owner's word, 2026-09-30: the planner
    # always starts with max effort. Handed to the client as --effort
    # (2.1.285: "Effort level for the current session (low, medium, high,
    # xhigh, max)"). The executor has no entry: the client decides, as it
    # always has. A project may name its own in projects[path]["effort"].
    # -> DECISIONS.md 8.60
    "role_effort": {"planner": "max"},
    "thresholds": {
        "handoff_at": 75,
        "warn_at": 85,
        "rotate_at": 90,
        "headroom_multiplier": 1.5,
        "limit_warn_at": 85,
        "limit_pause_at": 90,
        "review_timeout": 1200,
        "channel_silence_warn": 240,
        "stall_grace": 180,
        "handover_at": 90,
        "startup_grace": 600,
        "handover_grace": 600,
        "restart_settle": 150,
        "launches_per_hour": 6,
        "name_timeout": 120,
        "buffer_tokens": 33000,
        # How long a pair with nothing to do is held before it
        # checks in once. Under both the client timeout (1500s)
        # and the hook's own (1800s), so the hold always ends on
        # the bridge's terms rather than by something expiring.
        "idle_hold": 1200,
        # Reports that may go unanswered in a row before the pair is held.
        # Three, because the median gap between unanswered reports on the
        # night this came from was 21 minutes - so three is about an hour
        # of silence, and that night would have stopped after three
        # reports instead of thirty-two.
        "silence_limit": 3,
        # How long the executor's window is held for a report that was
        # NEVER DELIVERED. It used to be held for the full review_timeout,
        # twenty minutes, waiting for an answer to something no planner had
        # been given - and a blocked Stop hook draws nothing, so the owner
        # sees a Claude Code that looks dead. A minute is enough for a
        # queued delivery to go through if the planner's channel comes up;
        # after that the turn ends honestly as "not reviewed" rather than
        # freezing the window on a hope. A DELIVERED report still waits the
        # full review_timeout, because a planner thinking for minutes must
        # not be cut off.
        "undelivered_hold": 60,
        # How long after a turn died in an error the bridge waits for a
        # report before saying the turn was lost. 150s: on 2026-08-19 the
        # sessions that did come back had done so within about a minute
        # (the client's own "idle at the prompt" notification lands at ~60s),
        # and 18 of 22 never came back at all.
        "stopfail_grace": 150,
    },
    "retention": {"days": 7, "size_gb": 2, "archive_on_rotate": False},
    # The archive search agent: a headless one-off that reads the archive
    # and answers a question about it. Cheap by default - it greps and
    # reads, it does not reason about a codebase - and bounded, because
    # nothing that runs unattended may run for ever.
    "archive_model": "sonnet",
    "archive_timeout": 600,
    # How many of these may run at once, across all projects. One search at
    # a time used to be a property of the code - a single seat for the whole
    # bridge - which with several pairs meant one project's question locked
    # everybody else out. The seat is per project now, so this is what stops
    # four pairs from starting four headless clients at the same moment.
    # Still one at a time within a project.
    # Words that make a tail read as a question, or as a pair with nothing
    # to do, in whatever language the pair actually writes. Empty by default:
    # the English forms are built in, and anything else belongs to the
    # deployment rather than to the code - which is what keeps the source
    # publishable without changing how a running bridge behaves.
    "question_hints": [],
    "idle_hints": [],
    "archive_parallel": 2,
    # What to run for it. A name is looked up on PATH; a list is taken as
    # given, for a machine where claude is not plainly on PATH.
    "archive_claude": "claude",
    "quiet_when_present": False,
    "presence_file": "",
    # MAY THE BRIDGE READ A LIVE WINDOW'S CONSOLE AT ALL. With this false
    # the bridge never calls AttachConsole on anybody's window - no screen
    # is read and no Enter is sent, and the two places that would have
    # (nudge_deaf_window, answer_window_prompt) say so in the journal.
    #
    # IT SHIPS ON, and the reason it exists at all is worth the paragraph.
    # 2026-09-05 the owner's windows began freezing - the picture stopping
    # while the model carried on, tool calls and all, and a keystroke
    # reviving it without interrupting the turn. Reading a console was the
    # first suspect, because the bridge had only started doing it for real
    # at 23:16 the evening before, which fits "this did not used to
    # happen". It was the wrong suspect. The cause was found from outside,
    # without attaching to anything: the frozen window's TITLE carried the
    # legacy console host's own word for a selection, localised, which is
    # what the host puts there while one is up - and a selection blocks the
    # application in WriteConsole until it is cleared. Measured: 78 s in
    # which that window's title never changed while two control windows
    # changed five times each. The word itself is quoted in DECISIONS.md,
    # which is not published; this file is, and check_public refuses
    # Cyrillic in a published file whether written as characters or as
    # \uXXXX escapes. It refused this comment once already.
    #
    # So the switch stays, off is a real off, and the default is ON:
    # turning a mechanism off for a cause it did not have is how a bridge
    # loses a repair it needs. What it buys is the ability to stop in one
    # move next time something is suspected, which is the thing that was
    # missing when this was suspected. -> DECISIONS.md 8.14
    "nudge_console": True,
    "projects": {},
}

PROJECT_DEFAULTS = {
    "chains": {"executor": ["opus", "sonnet"], "planner": ["fable", "opus"]},
    "commit_each_iteration": True,
    # WHERE THIS PROJECT'S REPOSITORY IS, relative to the project path.
    # Empty means "the project path itself", which is the only thing the
    # bridge ever assumed and which is right for most projects. It is
    # DECLARED and never searched for: hunting up or down the tree for a
    # .git would mean the bridge choosing a repository nobody named, and
    # `git add -A` in the wrong one is not a mistake you notice quickly.
    # Same shape as `checks`, `modes` and `moved_from` - stated, not
    # inferred. This bridge's own project needs "source".
    "repo": "",
    "conditional_review": False,
    "readonly_planner": True,
    # "compact": let Claude Code compact the session as it normally would and
    # only rotate when the wall is actually hit. Rotation costs a new window,
    # and a new window costs a manual dialog - so it must be rare, not routine.
    # "ceiling": the old behaviour, rotate before compaction ever happens.
    "rotate_policy": "compact",
    # RAISED WITHOUT ASKING. This key existed and was in no defaults at
    # all, so `project_config(...).get("auto_restart_dead_sessions")` was
    # falsy for every project ever configured and the automatic restart in
    # handle_session_died had never run once - a dead window rang a person
    # and waited. 2026-09-04, the owner: a fallen executor is to be raised
    # without questions so it can carry on. A project that wants the old
    # behaviour sets this to false deliberately. -> DECISIONS.md 8.8
    "auto_restart_dead_sessions": True,
    # A COMMAND SHORTER THAN THIS WRITES NO Started/Finished LINES (35.1).
    # Every foreground call used to write two, and most take a second or
    # two: the journal was mostly a copy of the transcript. A command still
    # running at this age gets its Started line from the process watch, and
    # its Finished line with it; a failure and a background job are always
    # written. 0 writes every one, as before. -> DECISIONS.md 8.58
    "journal_short_commands_sec": 30,
    # There was an "autocompact_pct" here, 70 since 2026-08-21, and the
    # bridge handed it to every window it opened as
    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE. It is gone (2026-09-01, the owner's
    # decision): the bridge does not manage auto-compaction at all. The
    # evidence that removing it costs nothing came from the pair that
    # never had the companion setting - it received the same variable and
    # compacted at the ceiling regardless, so the variable was demonstrably
    # not in force. What the number was FOR is still true of anyone who
    # sets a threshold themselves, and lives in README.md: a threshold and
    # a window size MULTIPLY, and one whole turn has to fit above the
    # result. The bridge no longer has an opinion about where that is.
    # How many compactions a session is worth continuing through. Each one
    # frees 60-70% of the context and the session carries on, so compaction
    # is not the danger; what degrades is understanding. Reports converge on
    # noticeable loss after two or three, which is why the handover is keyed
    # to this count rather than to a token number nobody publishes.
    "compactions_before_handover": 2,
    # How long a session may be silent before the bridge looks into why.
    # Long enough not to interrupt a session that is thinking, short enough
    # that a night does not pass with the pair stopped.
    "silence_minutes": 8,
    # Whether the planner may answer the executor's questions on your behalf.
    # It is asked to answer technical ones and to escalate anything that
    # picks a direction or cannot be undone.
    "planner_answers_questions": True,
    "auto_resume_after_reset": False,
}


# ---- what "carried context" means, in one place ---------------------------
#
# §1.3, settled with Max on 2026-07-29 and verified against a real
# transcript: what a conversation occupies is the INPUT context of the last
# request - fresh input, what was written to the cache, and what was read
# back from it. Output is not in it. It joins the *next* request, not this
# one.
#
# By name, never by pattern. The client's usage block repeats the same
# tokens inside a "cache_creation" breakdown and again under "iterations",
# so a sum over every key containing "token" counts them two and three
# times over - which is how a conversation once read as 1002k inside a 1M
# window.
#
# The tuple lives here because three modules each computed this number their
# own way and two of them disagreed: the status-line path counted input
# only, the transcript path added output_tokens. Both wrote their answer to
# the same field, so a turn cost could be the difference between two
# different quantities. Mixed definitions of one number is exactly what made
# the statistics of 2026-07-29 unreadable; there is one definition now, and
# this is it.
CARRIED_CONTEXT_FIELDS = ("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens")


def carried_from_usage(usage):
    """Carried context from a usage block: (tokens, the fields it came from).

    (None, []) when the block carries none of the named fields - so a caller
    can tell "nothing to read here" from "a conversation of zero", which are
    not the same thing and were once reported identically.
    """
    if not isinstance(usage, dict):
        return None, []
    total, present = 0, []
    for field in CARRIED_CONTEXT_FIELDS:
        v = usage.get(field)
        if isinstance(v, (int, float)):
            total += int(v)
            present.append(field)
    return (total if present else None), present


def project_config(cfg, path):
    """Settings for a project, found however its path happens to be spelled.

    The config keeps the path as it was typed; callers pass the canonical
    form. On Windows those differ by case, so an exact lookup silently
    returns defaults - which is how a project's model chain, permission
    modes and thresholds quietly stop applying.
    """
    merged = json.loads(json.dumps(PROJECT_DEFAULTS))
    projects = cfg.get("projects") or {}
    entry = projects.get(path)
    if entry is None:
        want = os.path.normcase(os.path.normpath(path or ""))
        for key, val in projects.items():
            if os.path.normcase(os.path.normpath(key)) == want:
                entry = val
                break
    merged.update(entry or {})
    return merged

DEFAULT_STATE = {
    "mode": "idle",
    "clean_shutdown": True,
    "started_at": 0,
    # Both are keyed by canonical project path. "note" was a single string
    # for the whole bridge until pairs could run on several projects at
    # once, at which point it reached whichever project finished a turn
    # first; daemon.main() converts an old one on startup.
    "note": {},
    "paused": {},
    "sessions": {},
    "limits": {},
}


def _ensure_dirs():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)


def _read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            merged = json.loads(json.dumps(fallback))
            merged.update(data)
            return merged
    except Exception:
        pass
    return json.loads(json.dumps(fallback))


# How long os.replace is given to win against somebody else's open handle,
# and how long it waits between tries.
#
# 2026-08-30 22:00:26 is why this exists. A plain /status POST - the status
# line redrawing - went touch_session -> remember_session -> save_state and
# died on
#
#   PermissionError: [WinError 5] ... state.json.tmp -> state.json
#
# taking a whole daemon crash bundle with it, and that save was lost.
# Nothing was wrong with the write: on Windows os.replace fails while ANY
# other process holds the destination open, and an ordinary Python open()
# for reading does exactly that - CPython does not pass FILE_SHARE_DELETE.
# So every reader of state.json is a coin toss against the writer, and the
# reader that afternoon was the investigation reading its own bridge.
#
# A retry and not a lock: the window is milliseconds wide, the temp file is
# already complete and fsynced before any of this, and a lock would put
# waiting on a path that must not raise. Six tries over ~1.1 s covers a
# reader that is merely reading; something holding the file longer than
# that is a fault worth seeing, so the last failure is raised exactly as
# before.
REPLACE_TRIES = 6
REPLACE_WAIT = 0.05


def _replace_with_retry(tmp, path):
    """os.replace, but it survives another process reading the destination."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            # 5 = access denied, 32 = sharing violation. Both mean "somebody
            # has it open"; anything else is a real problem and goes straight
            # up, because retrying a wrong path or a full disk only delays
            # the report of it.
            last = attempt == REPLACE_TRIES - 1
            if getattr(exc, "winerror", None) not in (5, 32) or last:
                raise
            time.sleep(REPLACE_WAIT * (attempt + 1))


def _write_atomic(path, data):
    _ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    _replace_with_retry(tmp, path)


def load_config():
    with _lock:
        cfg = _read_json(CONFIG_PATH, DEFAULT_CONFIG)
        if not os.path.exists(CONFIG_PATH):
            _write_atomic(CONFIG_PATH, cfg)
        return cfg


CONFIG_KEEP = ("telegram",)


def save_config(cfg):
    """Write the config, and refuse to be the call that empties it.

    The telegram token and pairing were set up once, by hand, and then
    disappeared - the panel offered step 2 again as if nothing had ever been
    configured. Every write here goes through the whole dict, so any caller
    holding a stale or partial copy can silently drop a subtree that
    somebody else had just filled in. Rather than trust every call site, the
    write itself checks: a key that has a value on disk is never replaced
    with an empty one. If that ever fires it is a bug in the caller, so it
    leaves a line saying which one.

    A copy of the previous file is kept as well, because "it reset itself"
    is only diagnosable if the version before the reset still exists.
    """
    with _lock:
        old = _read_json(CONFIG_PATH, {}) if os.path.exists(CONFIG_PATH) \
            else {}
        lost = []
        for key in CONFIG_KEEP:
            was, now = old.get(key), cfg.get(key)
            if isinstance(was, dict) and was:
                if not isinstance(now, dict) or (
                        any(was.get(f) for f in ("token", "chat_id"))
                        and not any((now or {}).get(f)
                                    for f in ("token", "chat_id"))):
                    cfg[key] = was
                    lost.append(key)
        if old:
            try:
                bdir = os.path.join(DATA, "backups")
                os.makedirs(bdir, exist_ok=True)
                _write_atomic(os.path.join(
                    bdir, "config-%s.json" % time.strftime("%Y%m%d-%H")), old)
            except Exception:
                pass
        _write_atomic(CONFIG_PATH, cfg)
    if lost:
        import traceback as _tb
        journal("bridge_error",
                "A config write would have emptied %s; kept what was on disk. "
                "Called from: %s" % (", ".join(lost),
                                     " | ".join(_tb.format_stack()[-4:-1])),
                level="sound")


def load_state():
    with _lock:
        return _read_json(STATE_PATH, DEFAULT_STATE)


# How many replaces of state.json in a row may lose before it stops being
# somebody reading and starts being a fault worth a person's attention.
STATE_WRITE_FAILS_TELL = 5
_state_write_fails = [0]


def save_state(state):
    """Write STATE. A replace that loses does NOT come back up.

    The retry in _replace_with_retry wins against a reader that lets go -
    an antivirus pass, a `type state.json`, one read in a script. It cannot
    win against a handle held open across the whole window, and on
    2026-08-30 that is what happened: an investigation had state.json open
    while the status line's /status POST went touch_session ->
    remember_session -> save_state, os.replace raised WinError 5 four
    frames down, and a crash bundle came out of a status redraw.

    So this one write is allowed to lose. Not swallowed silently, and not
    swallowed anywhere else - config.json and models.json still raise,
    because they are written rarely and losing one matters. STATE is
    different in kind: it is held in memory, it is the memory that is
    authoritative (see CFG's docstring for the same argument), and it is
    written again within seconds by the next thing that touches it. The
    complete .tmp is left on disk, so nothing that was serialised is lost
    even if the daemon stops here.

    A run of them is a different animal - a file genuinely locked, a
    read-only disk - and the count is what tells them apart.
    """
    why = second_authority()
    if why:
        raise SecondAuthority(why)
    lost = None
    with _lock:
        try:
            _write_atomic(STATE_PATH, state)
            _state_write_fails[0] = 0
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            _state_write_fails[0] += 1
            lost = (exc, _state_write_fails[0])
    # Outside the lock: journal() is file work, and file work under a lock
    # every writer needs is how one slow disk becomes everybody's problem.
    if lost:
        journal("bridge",
                "Could not replace state.json - something has it open (%s). "
                "The complete copy is beside it as state.json.tmp and the "
                "next save will carry it; %d in a row."
                % (lost[0], lost[1]), "", "",
                "warn" if lost[1] >= STATE_WRITE_FAILS_TELL else "log")


def update_state(**fields):
    with _lock:
        state = load_state()
        state.update(fields)
        save_state(state)
        return state


def day_dir(base=None):
    d = os.path.join(base or LOGS, time.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    return d


def project_log_dir(project_dir):
    if not project_dir or not os.path.isdir(project_dir):
        return None
    return day_dir(os.path.join(project_dir, "bridge-logs"))


def secret():
    """Shared secret for daemon<->channel calls. Lives in HOME, never in a repo."""
    path = os.path.join(os.path.expanduser("~"), ".bridge-secret")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
            if val:
                return val
    except Exception:
        pass
    import secrets as _s
    val = _s.token_hex(24)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(val)
    except Exception:
        pass
    return val


# THE STORE APPENDS IN ONE PLACE, UNDER A LOCK OF ITS OWN. The journal's
# two files, the dialogue, the iteration index and a carried day brought
# in all go through _append and nowhere else. 2026-09-23: case 12 of
# test_multipair lost a quiet pair's journal line outright - no row had
# landed while it read, the row was simply not in the file. These files
# were appended with open(p, "a") and no lock, and on Windows an append is
# "seek to the end, then write", two steps: two threads that reach the same
# end write over each other and a line is gone. Measured through journal()
# itself, 8 threads x 1 000 lines lost 269 to 330 lines in EACH of its two
# files. Every thread of the daemon journals, and the journal is the
# witness most of the rules read: "there is no line" is read as "it did not
# happen".
#
# Not _lock: that one is the state's, held across a state save and its
# retries, and a journal line waiting behind that is one slow disk made
# everybody's problem (save_state journals OUTSIDE it for that reason).
# This one is held for one open, one write and one close, and nothing is
# taken under it, so it cannot be half of a deadlock. A thread lock is
# enough because nothing outside the daemon appends to these files: the
# hooks, the channel and the status line post to the daemon over HTTP and
# import nothing from this module.
_JOURNAL_LOCK = threading.Lock()


def _append(path, text, head=""):
    """Append text to path - the one place the store appends to a file.

    `head` goes in first when the file does not exist yet, decided under the
    same lock: the index used to look for its file and then write a header
    with "w" as two steps, and a second thread between them would have
    truncated the first one's header and line. Raises as open() does - every
    caller is an edge path and catches.
    """
    with _JOURNAL_LOCK:
        if head and not os.path.exists(path):
            text = head + text
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def journal(kind, text, project="", session="", level="log", extra=None,
            project_dir=None):
    """Append one line to today's journal. Never raises - IN THE DAEMON.

    Written centrally (the panel reads it) and, when project_dir is given,
    into <project>/bridge-logs/<date>/ as well - the logs live with the
    project, as asked.

    A process that is not the daemon and is not on a throwaway folder is
    refused loudly (SecondAuthority) rather than quietly: the daemon's own
    edge paths are unaffected because the daemon has claimed the folder,
    and a stranger writing "planner_check ... passed" into the live journal
    under the planner's name is exactly what 2026-09-13 00:31:48 was.
    """
    why = second_authority()
    if why:
        raise SecondAuthority(why)
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "text": text,
        "project": project,
        # Which project this line is about, canonically, so a reader can
        # filter by it. "project" above is the name as it is spelled for a
        # human, and two projects can be called the same thing - a basename
        # is not an identity. An empty path is not a gap: it means the line
        # is about the bridge itself rather than about any one pair.
        "path": norm(project_dir),
        "session": session,
        "level": level,
    }
    if extra:
        row["extra"] = extra
    line = json.dumps(row, ensure_ascii=False) + "\n"
    for base in (day_dir(), project_log_dir(project_dir)):
        if not base:
            continue
        try:
            _append(os.path.join(base, "events.jsonl"), line)
        except Exception:
            pass
    return row


def dialogue(project_dir, heading, body):
    """Append a readable block to the project's dialogue log (and central)."""
    text = "\n\n## %s\n\n%s\n" % (heading, body)
    for base in (day_dir(), project_log_dir(project_dir)):
        if not base:
            continue
        try:
            _append(os.path.join(base, "dialogue.md"), text)
        except Exception:
            pass


def _read_events(path):
    rows = []
    try:
        if not os.path.exists(path):
            return rows
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _feed_rows(path, project=None):
    """Journal lines from one file, keeping only the ones asked for.

    A line with no path is about the bridge rather than about a pair - it
    started, telegram stopped answering, a config write was refused - and
    those belong in every project's feed, because they are true of every
    pair at once.
    """
    rows = _read_events(path)
    if not project:
        return rows
    want = norm(project)
    return [r for r in rows if not r.get("path") or r.get("path") == want]


def recent_events(limit=40, project=None):
    """Newest first, and it does not go blank at midnight.

    The journal rolls into a new file every day, so a run that crosses
    midnight would otherwise show an empty feed for the first few minutes
    of the new day - exactly when an overnight run needs watching.

    The filter is applied before the cut, never after. One journal carries
    every project, so trimming to the newest `limit` lines first and then
    dropping the other projects' hands back a handful of rows: a busy pair
    pushes a quiet one out of the window entirely, and its feed reads as
    "nothing happened" while it was working. Same reason the top-up from
    yesterday counts what survived the filter rather than what was read.
    """
    rows = _feed_rows(os.path.join(day_dir(), "events.jsonl"), project)
    if len(rows) < limit:
        y = time.strftime("%Y-%m-%d",
                          time.localtime(time.time() - 86400))
        older = _feed_rows(os.path.join(LOGS, y, "events.jsonl"), project)
        rows = older[-(limit - len(rows)):] + rows
    return rows[-limit:][::-1]


# ---- portable history: the project folder carries its own journal ---------
#
# ANALYSIS-portable-history.md. `journal()` above already writes every line
# twice - once to data/logs and, when project_dir is given, into the
# project's own bridge-logs. That copy is the carrier: measured 2026-08-28,
# the project copy of that day held 1894 rows, byte-identical to the 1894
# rows the central journal held for the same project. What was missing was
# reading it back, so a project arriving on a new machine brought its
# history with it and the bridge could not see it.
#
# Everything here is FILE work only. It never takes _lock, never reads or
# writes STATE, and never writes into the project folder - the carrier is
# read-only. That is the safety property of the whole feature: what the
# bridge DECIDES from stays in STATE, and this moves only what a person
# reads.

# Fields that are about the machine the row was written on, not about the
# event. They are excluded from the fingerprint so that the same event
# recorded under E:\ and re-keyed to C:\ is recognised as one row - which is
# the whole point - and so that merging an already-merged row cannot create
# a duplicate of itself.
MACHINE_LOCAL_FIELDS = ("path", "path_was")


def row_fingerprint(row):
    """What makes two journal rows the same event, as 16 hex characters.

    Everything except the machine-local fields: `at`, `kind`, `text`,
    `project`, `session`, `level` and `extra` are written once and do not
    change when the folder is carried to another computer.

    The stamp is one-second, so two genuinely different events in the same
    second with the same text, kind and level fold into one. That is
    accepted deliberately: such rows are indistinguishable to a reader too,
    and a missed duplicate is cheaper than a doubled feed.
    """
    if not isinstance(row, dict):
        return ""
    body = {k: v for k, v in row.items() if k not in MACHINE_LOCAL_FIELDS}
    try:
        blob = json.dumps(body, sort_keys=True, ensure_ascii=False,
                          default=str)
    except Exception:
        # A row that cannot be serialised has no identity we can compare,
        # and this is an edge path: give it none rather than raise.
        return ""
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _day_names(base):
    """The <date> folders under a bridge-logs directory, oldest first."""
    out = []
    try:
        for name in os.listdir(base):
            if len(name) == 10 and name[4] == "-" and name[7] == "-" \
                    and name.replace("-", "").isdigit() \
                    and os.path.isdir(os.path.join(base, name)):
                out.append(name)
    except OSError:
        return []
    return sorted(out)


def scan_project_history(project):
    """What a project's own bridge-logs holds, without changing anything.

    Inspection only: it opens files, counts and returns. Nothing is written,
    no lock is taken. The `paths` count per day is what makes a move
    visible - a project that came from another machine carries rows whose
    `path` is that machine's spelling, and the difference between those and
    norm(project) is the finding a person is later asked about.
    """
    out = {"project": norm(project), "days": [], "rows": 0,
           "paths": {}, "unreadable": []}
    base = os.path.join(project or "", "bridge-logs")
    if not project or not os.path.isdir(base):
        return out
    for day in _day_names(base):
        f = os.path.join(base, day, "events.jsonl")
        if not os.path.exists(f):
            continue
        rows = _read_events(f)          # never raises; skips broken lines
        if not rows and os.path.getsize(f) > 0:
            out["unreadable"].append(day)
        out["days"].append({"day": day, "rows": len(rows)})
        out["rows"] += len(rows)
        for r in rows:
            p = r.get("path") or ""
            out["paths"][p] = out["paths"].get(p, 0) + 1
    return out


def merge_day(project, day):
    """Bring one day of a project's own journal into data/logs/<day>/.

    Direction is one way, project -> bridge, and it is the only direction
    there can be: the other machine is not here to be written to, and the
    bridge does not write into anybody's past.

    Three things this does and a person should be able to check:

    - IDEMPOTENT. The target day is read first and turned into a set of
      fingerprints; only rows missing from it are appended. Run twice and
      the file is byte-identical, which is what the case asserts.
    - RE-KEYED. An imported row carries the path of the machine it was
      written on, and the feed filter is an exact match against
      norm(project) - so without this the history would sit in the file and
      appear in no feed at all. Re-keying is not a guess here and needs
      nobody's permission: a row found inside <project>/bridge-logs is
      about that project by construction, because that is where it lies.
      The original is kept as `path_was` so a wrong import is reversible.
    - REFUSED IF PATHLESS. `_feed_rows` lets a row with no path through
      EVERY project's filter, because a pathless line is about the bridge
      itself. An imported row must therefore never be pathless, or one
      import would flood every feed at once. Such rows EXIST: the carrier
      holds every line written before rows had a `path` at all (the last
      of them 2026-08-19 - 7 685 in one project, 583 in another), and they
      are counted and left where they are. This said "today none can be"
      until 2026-09-28, which was never true of an old carrier.
      -> DECISIONS.md 8.58

    The day comes from the FOLDER NAME, never from a row's `at`: two
    machines' clocks drift, and taking the day from the stamp would scatter
    one day across two files. For the same reason nothing here is compared
    against local time, and `last_seen` - a clock with no date (§6.5) - is
    not touched at all, because it lives in STATE and STATE is not this
    function's business.

    Returns a dict; it never raises. A broken row is skipped, a broken file
    is reported and the caller carries on - this runs on the startup path.
    """
    out = {"day": day, "read": 0, "added": 0, "already": 0,
           "no_path": 0, "rekeyed": 0, "error": ""}
    src = os.path.join(project or "", "bridge-logs", day, "events.jsonl")
    if not project or not os.path.exists(src):
        return out
    want = norm(project)
    try:
        incoming = _read_events(src)
        out["read"] = len(incoming)
        if not incoming:
            return out

        target_dir = os.path.join(LOGS, day)
        target = os.path.join(target_dir, "events.jsonl")
        seen = set()
        for r in _read_events(target):
            fp = row_fingerprint(r)
            if fp:
                seen.add(fp)

        fresh = []
        for row in incoming:
            if not isinstance(row, dict):
                continue
            if not (row.get("path") or ""):
                out["no_path"] += 1
                continue
            fp = row_fingerprint(row)
            if not fp or fp in seen:
                out["already"] += 1
                continue
            seen.add(fp)
            keep = dict(row)
            if keep.get("path") != want:
                keep["path_was"] = keep.get("path")
                keep["path"] = want
                out["rekeyed"] += 1
            fresh.append(json.dumps(keep, ensure_ascii=False))

        if not fresh:
            return out
        # Append-only, and the directory is made here rather than through
        # day_dir(), which always builds TODAY and would create the wrong
        # folder for an imported older day. Through _append, because when
        # the day is today this is the very file journal() is appending to
        # from every other thread.
        os.makedirs(target_dir, exist_ok=True)
        _append(target, "\n".join(fresh) + "\n")
        out["added"] = len(fresh)
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def merge_project_history(project):
    """Every day this project carries that the bridge has not got.

    The whole of what a caller needs; `merge_day` is the unit underneath it.
    Never raises, for the same reason: both callers are startup paths.
    """
    total = {"project": norm(project), "days": 0, "added": 0, "already": 0,
             "no_path": 0, "rekeyed": 0, "errors": []}
    base = os.path.join(project or "", "bridge-logs")
    if not project or not os.path.isdir(base):
        return total
    for day in _day_names(base):
        one = merge_day(project, day)
        total["days"] += 1
        for k in ("added", "already", "no_path", "rekeyed"):
            total[k] += one[k]
        if one["error"]:
            total["errors"].append("%s %s" % (day, one["error"]))
    return total


# ---- calibration (model x project) ----------------------------------------

def load_calibration():
    with _lock:
        return _read_json(CALIB_PATH, {})


def save_calibration(cal):
    with _lock:
        _write_atomic(CALIB_PATH, cal)


def calib_key(model, project):
    """model|project, folded the way every other key in the bridge is.

    It used to be `os.path.normpath` with no `normcase`, and it was the only
    place in the package that compared a Windows path without folding its
    case - see `norm` above for why that matters. C:\\path\\to\\Game and
    c:\\path\\to\\game are one folder and were two calibration entries, so a
    session could be measured under one spelling and read under the other,
    and arrive at a window with no measurements at all. `migrate_calib_keys`
    folds what is already on disk.
    """
    return "%s|%s" % ((model or "?").lower(), norm(project) or "?")


def migrate_calib_keys():
    """Fold existing calibration keys onto the canonical path form.

    Runs at every start, like the other migrations, and is a no-op once it
    has run. A collision - the same model and the same folder under two
    spellings - keeps the entry with the most compaction samples, because
    samples are the whole value of the record and the other spelling's
    entry is the same pair measured under a different name.
    """
    with _lock:
        cal = _read_json(CALIB_PATH, {})
        moved = 0
        for old in list(cal):
            model, _, proj = old.partition("|")
            new = calib_key(model, proj)
            if new == old or not proj:
                continue
            here, there = cal.get(new), cal[old]
            if here is None:
                cal[new] = cal.pop(old)
            else:
                mine = len((here or {}).get("compact_samples") or [])
                theirs = len((there or {}).get("compact_samples") or [])
                cal[new] = there if theirs > mine else here
                cal.pop(old)
            moved += 1
        if moved:
            _write_atomic(CALIB_PATH, cal)
        return moved


def calib_move(old_project, new_project):
    """Bring one project's calibration across from the path it used to have.

    Called only from the "project moved" migration, which runs only on the
    owner's written claim.

    COLLISION POLICY, and it is the opposite of the one used for STATE.
    A calibration entry is a MEASUREMENT, and rule 33 says evidence has a
    shelf life: the client went 2.1.227 -> 2.1.240 in three days, and a
    figure measured on another computer is about that computer's client and
    its window. So a local entry that has actually measured something - one
    compaction sample or more - is never replaced by a carried one, however
    rich the carried one is. A local entry that has measured NOTHING is
    replaced, because "initial estimate" is not evidence and the carried
    figure is.

    What is adopted is MARKED. `how` says where it came from in words,
    `carried_from` holds the path it was measured under, and
    `carried_at` when it was brought across - so `compaction_point` and
    `compaction_survivable` can tell a figure measured here from one that
    travelled, and so can a person reading the panel. Nothing is deleted:
    the entry the old key held moves, it does not evaporate.
    """
    out = {"moved": 0, "kept_local": 0}
    with _lock:
        cal = _read_json(CALIB_PATH, {})
        changed = False
        for key in list(cal):
            model, _, proj = key.partition("|")
            if norm(proj) != norm(old_project):
                continue
            entry = dict(cal[key] or {})
            fresh = calib_key(model, new_project)
            here = cal.get(fresh) or {}
            if len(here.get("compact_samples") or []):
                # This machine has measured this pair itself. Fresh local
                # evidence outranks a carried figure, always.
                out["kept_local"] += 1
                cal.pop(key)
                changed = True
                continue
            entry["carried_from"] = norm(old_project)
            entry["carried_at"] = time.strftime("%Y-%m-%d %H:%M")
            entry["how"] = ("%s (measured on another machine, under %s)"
                            % (entry.get("how") or "measured",
                               norm(old_project)))
            cal[fresh] = entry
            cal.pop(key)
            out["moved"] += 1
            changed = True
        if changed:
            _write_atomic(CALIB_PATH, cal)
    return out


def calib_get(model, project, window):
    """A COMPLETE calibration record for this model and project.

    The defaults are filled in for a partial entry, not only for a missing
    one, and that is the whole of the change made on 2026-08-22. calib_update
    creates the key with setdefault, so any caller that writes one field
    before reading - and the wall handling does exactly that, recording
    wall_history_tokens and then calling calib_miss - left an entry holding
    that one field. The next read handed it back as if it were a record, and
    `cal["ceiling_pct"]` raised KeyError inside the branch that replaces a
    session: the crash landed precisely where the bridge was trying to
    rescue a pair. Never seen live only because every real project already
    had a full entry by the time it got there.
    """
    cal = load_calibration()
    key = calib_key(model, project)
    buffer_t = 33000
    if window and window > buffer_t:
        ceiling = max(50.0, (window - buffer_t) * 100.0 / window - 3.0)
    else:
        ceiling = 80.0
    blank = {"ceiling_pct": round(ceiling, 1), "buffer_tokens": buffer_t,
             "measured_at": "", "how": "initial estimate",
             "misses": 0, "clean_streak": 0, "multiplier": 1.5,
             "wall_history_tokens": None,
             "compact_at_tokens": None}
    entry = cal.get(key)
    if not isinstance(entry, dict):
        cal[key] = blank
        save_calibration(cal)
        return cal[key]
    missing = {k: v for k, v in blank.items() if k not in entry}
    if missing:
        entry.update(missing)
        save_calibration(cal)
    return entry


def calib_update(model, project, **fields):
    with _lock:
        cal = load_calibration()
        key = calib_key(model, project)
        entry = cal.setdefault(key, {})
        entry.update(fields)
        _write_atomic(CALIB_PATH, cal)
        return entry


# ---- model registry: what each alias actually resolves to today --------
# Passive source: every live session's status line carries the concrete
# model id. Active source: a one-token probe run. Both land here.

DEFAULT_MODELS = {
    "map": {},        # alias -> {id, display, seen, via}
    "subs": [],       # [{from, to, at}]
    "opts": {"prefer_aliases": True, "reread_on_launch": True,
             "allow_best": False},
    "last_probe": "",
}


def load_models():
    with _lock:
        return _read_json(MODELS_PATH, DEFAULT_MODELS)


def save_models(m):
    with _lock:
        _write_atomic(MODELS_PATH, m)


def models_note(alias, model_id, display, via):
    """Record that `alias` resolved to this concrete model."""
    if not alias or not model_id:
        return
    with _lock:
        m = _read_json(MODELS_PATH, DEFAULT_MODELS)
        m.setdefault("map", {})[alias.lower()] = {
            "id": model_id, "display": display or model_id,
            "seen": time.strftime("%Y-%m-%d %H:%M"), "via": via}
        _write_atomic(MODELS_PATH, m)


def models_sub(req, got_id, got_display):
    """A cross-family substitution happened (e.g. fable served by opus)."""
    with _lock:
        m = _read_json(MODELS_PATH, DEFAULT_MODELS)
        subs = m.setdefault("subs", [])
        today = time.strftime("%Y-%m-%d")
        for e in subs[-5:]:
            if e.get("from") == req and e.get("to_id") == got_id and \
                    (e.get("at") or "").startswith(today):
                return False
        subs.append({"from": req, "to": got_display or got_id,
                     "to_id": got_id,
                     "at": time.strftime("%Y-%m-%d %H:%M")})
        del subs[:-30]
        _write_atomic(MODELS_PATH, m)
        return True


# ---- profiles -------------------------------------------------------------

DEFAULT_PROFILES = {
    # the planner in auto in every preset, as everywhere else (8.59)
    "endless run": {"executor_mode": "auto", "planner_mode": "auto",
                    "rc": True, "admin": False},
    "debugging": {"executor_mode": "default", "planner_mode": "auto",
                  "rc": True, "admin": False},
    "one-off repair": {"executor_mode": "acceptEdits", "planner_mode": "auto",
                       "rc": True, "admin": False},
}


def load_profiles():
    with _lock:
        p = _read_json(PROFILES_PATH, DEFAULT_PROFILES)
        if not os.path.exists(PROFILES_PATH):
            _write_atomic(PROFILES_PATH, p)
        return p


def save_profiles(p):
    with _lock:
        _write_atomic(PROFILES_PATH, p)


# ---- handoff, INDEX, snapshots, inbox -------------------------------------

def handoff_dir(project_dir):
    base = project_log_dir(project_dir)
    if not base:
        return None
    d = os.path.join(base, "handoff")
    os.makedirs(d, exist_ok=True)
    return d


def write_handoff(project_dir, iteration, text):
    d = handoff_dir(project_dir)
    if not d:
        return None
    for name in ("current.md", "%03d.md" % iteration):
        try:
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass
    return os.path.join(d, "current.md")


def read_handoff(project_dir):
    d = handoff_dir(project_dir)
    if not d:
        return ""
    try:
        with open(os.path.join(d, "current.md"), "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def index_append(project_dir, iteration, what, verdict):
    base = project_log_dir(project_dir)
    if not base:
        return
    path = os.path.join(os.path.dirname(base), "INDEX.md")
    line = "| %03d | %s | %s | %s |\n" % (
        iteration, time.strftime("%m-%d %H:%M"),
        (what or "").replace("|", "/")[:90], verdict)
    try:
        _append(path, line, head="| iteration | time | what happened | "
                                 "verdict |\n|---|---|---|---|\n")
    except Exception:
        pass


def read_index(project_dir, limit=30):
    path = os.path.join(project_dir, "bridge-logs", "INDEX.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.startswith("|")]
        return lines[:2] + lines[max(2, len(lines) - limit):]
    except Exception:
        return []


def iteration_file(project_dir, iteration, role, text):
    base = project_log_dir(project_dir)
    if not base:
        return
    d = os.path.join(base, "dialogue")
    os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(d, "%03d-%s.md" % (iteration, role)), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass


def snapshot_transcript(project_dir, session_id, transcript_path):
    """Copy the transcript right before a compaction eats it."""
    if not transcript_path or not os.path.exists(transcript_path):
        return
    base = project_log_dir(project_dir) or day_dir()
    d = os.path.join(base, "snapshots")
    os.makedirs(d, exist_ok=True)
    try:
        dest = os.path.join(d, "%s-%s.jsonl"
                            % (session_id or "s", time.strftime("%H%M%S")))
        with open(transcript_path, "rb") as src, open(dest, "wb") as out:
            out.write(src.read())
    except Exception:
        pass


def inbox_write(project_dir, iteration, text):
    base = project_log_dir(project_dir) or day_dir()
    d = os.path.join(base, "inbox")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%03d-report.md" % iteration)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    return path


# ---- archive + verify -----------------------------------------------------

def _dir_size(path):
    total = 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except Exception:
                pass
    return total


def logs_disk_by_project(projects):
    rows = []
    for p in projects:
        d = os.path.join(p, "bridge-logs")
        if os.path.isdir(d):
            days = sorted(x for x in os.listdir(d)
                          if os.path.isdir(os.path.join(d, x)))
            rows.append({"project": os.path.basename(p), "path": p,
                         "bytes": _dir_size(d),
                         "oldest": days[0] if days else "-"})
    return rows


# The name of a day folder the bridge writes in bridge-logs: YYYY-MM-DD.
_DAY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_zip_name(root, name):
    """The first archive name of this day that does not exist yet: the
    day's own `<day>.zip`, then `<day>.2.zip`, `<day>.3.zip` ..."""
    zpath = os.path.join(root, name + ".zip")
    n = 2
    while os.path.exists(zpath):
        zpath = os.path.join(root, "%s.%d.zip" % (name, n))
        n += 1
    return zpath


def _zip_short_of(zpath, full, root):
    """None when every file under `full` is in the archive at `zpath`
    whole - under its name, at its size, with a CRC that reads back.
    Otherwise a sentence naming the first thing that is not."""
    import zipfile
    want = {}
    for dp, _dn, fn in os.walk(full):
        for f in fn:
            p = os.path.join(dp, f)
            want[os.path.relpath(p, root).replace(os.sep, "/")] = \
                os.path.getsize(p)
    with zipfile.ZipFile(zpath) as z:
        have = {i.filename: i.file_size for i in z.infolist()}
        bad = z.testzip()
    if bad:
        return "%s does not read back (CRC)" % bad
    for rel, size in sorted(want.items()):
        if rel not in have:
            return "%s is not in it" % rel
        if have[rel] != size:
            return "%s is %d bytes in it and %d on disk" % (rel, have[rel],
                                                             size)
    return None


def archive_old(project_dir, days=7, size_gb=2):
    """Zip day folders older than `days`, or oldest-first past the size cap.

    A FOLDER IS REMOVED ONLY AFTER ITS ARCHIVE HAS BEEN READ BACK, AND AN
    ARCHIVE IS NEVER WRITTEN OVER. This used to zip with mode "w" to
    `<day>.zip` and then rmtree the folder under a bare except: a read-only
    or held file stopped the rmtree half way, in silence, and when the half
    folder aged again the next pass rewrote `<day>.zip` from what was left
    - the files removed the first time were then in neither place
    (DECISIONS 8.46, archive-rezip-loss.txt). Now:
      - a day whose archive exists gets a NEW one, `<day>.2.zip` and on,
        opened with mode "x", so the OS itself refuses to write over
        anything. Appending ("a") was the other choice and is refused: it
        rewrites the old archive's central directory when it closes, so
        a failure half way through damages the archive that was whole;
      - the folder goes only when `_zip_short_of` finds every file in the
        new archive by name and size, and its CRCs read back; an archive
        that falls short is this call's own and is removed by its exact
        path, and the folder stays;
      - the folder is removed by relayout.remove_tree, which clears
        read-only, and a removal that stops half way is a warn line.
    """
    import zipfile
    from . import relayout
    root = os.path.join(project_dir, "bridge-logs")
    if not os.path.isdir(root):
        return 0
    packed = 0
    today = time.strftime("%Y-%m-%d")
    # ONLY the day folders the bridge writes itself, by their name. This
    # took any folder in bridge-logs but today's - extracts/ among them -
    # and removed it after the zip; since 2026-09-28 a removal reaches only
    # what its own code made (DECISIONS 8.43, 8.46).
    entries = sorted(x for x in os.listdir(root)
                     if _DAY_NAME.match(x) and x != today
                     and os.path.isdir(os.path.join(root, x)))
    cutoff = time.time() - days * 86400
    oversize = _dir_size(root) > size_gb * (1024 ** 3)

    def say(text):
        journal("archive", text, os.path.basename(project_dir), "archive",
                "warn", project_dir=project_dir)

    for name in entries:
        full = os.path.join(root, name)
        old = os.path.getmtime(full) < cutoff
        if not (old or oversize):
            continue
        zpath = _day_zip_name(root, name)
        made = False
        try:
            with zipfile.ZipFile(zpath, "x", zipfile.ZIP_DEFLATED) as z:
                made = True
                for dp, _dn, fn in os.walk(full):
                    for f in fn:
                        p = os.path.join(dp, f)
                        z.write(p, os.path.relpath(p, root))
            short = _zip_short_of(zpath, full, root)
        except Exception as exc:
            short = "%s: %s" % (exc.__class__.__name__, exc)
        if short:
            if made and os.path.isfile(zpath):
                try:
                    os.remove(zpath)
                except OSError:
                    pass
            say("%s was NOT archived and is kept as it is: the archive %s "
                "fell short - %s%s" % (
                    name, os.path.basename(zpath), short,
                    "" if not os.path.exists(zpath)
                    else "; that archive could not be removed either"))
            continue
        gone, why = relayout.remove_tree(full, out=lambda _s: None,
                                         tries=3, wait=1.0)
        if not gone:
            say("%s is archived whole in %s, but the folder could NOT be "
                "removed whole: %s. What is left is archived into a new "
                "file next time; %s is never written again"
                % (name, os.path.basename(zpath), why,
                   os.path.basename(zpath)))
            continue
        packed += 1
        oversize = _dir_size(root) > size_gb * (1024 ** 3)
    return packed


def verify_archives(project_dir):
    """Every rotation must have a handoff, a transcript and an index line."""
    root = os.path.join(project_dir, "bridge-logs")
    out = {"rotations": 0, "handoffs_ok": 0, "transcripts_ok": 0,
           "index_ok": os.path.exists(os.path.join(root, "INDEX.md")),
           "problems": []}
    if not os.path.isdir(root):
        return out
    for day in os.listdir(root):
        hd = os.path.join(root, day, "handoff")
        if not os.path.isdir(hd):
            continue
        for f in os.listdir(hd):
            if f == "current.md" or not f.endswith(".md"):
                continue
            out["rotations"] += 1
            full = os.path.join(hd, f)
            try:
                ok = os.path.getsize(full) > 80
            except Exception:
                ok = False
            if ok:
                out["handoffs_ok"] += 1
            else:
                out["problems"].append("handoff %s/%s looks cut short"
                                       % (day, f))
        raw = os.path.join(root, day, "raw")
        if os.path.isdir(raw):
            out["transcripts_ok"] += len(
                [x for x in os.listdir(raw) if x.endswith(".jsonl")])
    return out


def transcript_copy(session_id, transcript_path, project_dir=None):
    """Keep our own copy of a session transcript next to the logs."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        base = project_log_dir(project_dir) or day_dir()
        raw = os.path.join(base, "raw")
        os.makedirs(raw, exist_ok=True)
        dest = os.path.join(raw, "%s.jsonl" % (session_id or "unknown"))
        with open(transcript_path, "rb") as src, open(dest, "wb") as out:
            out.write(src.read())
        return dest
    except Exception:
        return None
