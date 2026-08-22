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

import json
import os
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
    "role_modes": {"executor": "bypassPermissions", "planner": "plan"},
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
    "projects": {},
}

PROJECT_DEFAULTS = {
    "chains": {"executor": ["opus", "sonnet"], "planner": ["fable", "opus"]},
    "commit_each_iteration": True,
    "conditional_review": False,
    "readonly_planner": True,
    # "compact": let Claude Code compact the session as it normally would and
    # only rotate when the wall is actually hit. Rotation costs a new window,
    # and a new window costs a manual dialog - so it must be rare, not routine.
    # "ceiling": the old behaviour, rotate before compaction ever happens.
    "rotate_policy": "compact",
    # Percent of the context window at which Claude Code compacts. Set it and
    # the point is known instead of discovered; leave it None to keep Claude
    # Code's own default, which has changed between versions.
    #
    # 70, not 80, since 2026-08-21, and the arithmetic is an incident rather
    # than a preference. Compaction fires BETWEEN turns, so what has to fit
    # between the threshold and the end of the window is one whole turn:
    #
    #   window                     1 000 000
    #   at 80% the threshold is      800 000   -> 200 000 of headroom
    #   the turn that died needed  1 000 274   -> it wanted 200 274
    #
    # It missed by 274 tokens, and the session died with its own compaction
    # request too big to send. 70% gives 300 000 - half as much again as the
    # largest single turn ever seen here - and that margin is the whole
    # reason for the number.
    "autocompact_pct": 70,
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


def _write_atomic(path, data):
    _ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


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


def save_state(state):
    with _lock:
        _write_atomic(STATE_PATH, state)


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


def journal(kind, text, project="", session="", level="log", extra=None,
            project_dir=None):
    """Append one line to today's journal. Never raises.

    Written centrally (the panel reads it) and, when project_dir is given,
    into <project>/bridge-logs/<date>/ as well - the logs live with the
    project, as asked.
    """
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
            with open(os.path.join(base, "events.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(line)
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
            with open(os.path.join(base, "dialogue.md"), "a",
                      encoding="utf-8") as fh:
                fh.write(text)
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


# ---- calibration (model x project) ----------------------------------------

def load_calibration():
    with _lock:
        return _read_json(CALIB_PATH, {})


def save_calibration(cal):
    with _lock:
        _write_atomic(CALIB_PATH, cal)


def calib_key(model, project):
    return "%s|%s" % ((model or "?").lower(), os.path.normpath(project or "?"))


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
    "endless run": {"executor_mode": "auto", "planner_mode": "plan",
                    "rc": True, "admin": False},
    "debugging": {"executor_mode": "default", "planner_mode": "plan",
                  "rc": True, "admin": False},
    "one-off repair": {"executor_mode": "acceptEdits", "planner_mode": "plan",
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
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("| iteration | time | what happened | verdict |\n"
                         "|---|---|---|---|\n")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
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


def archive_old(project_dir, days=7, size_gb=2):
    """Zip day folders older than `days`, or oldest-first past the size cap."""
    import zipfile
    root = os.path.join(project_dir, "bridge-logs")
    if not os.path.isdir(root):
        return 0
    packed = 0
    today = time.strftime("%Y-%m-%d")
    entries = sorted(x for x in os.listdir(root)
                     if os.path.isdir(os.path.join(root, x)) and x != today)
    cutoff = time.time() - days * 86400
    oversize = _dir_size(root) > size_gb * (1024 ** 3)
    for name in entries:
        full = os.path.join(root, name)
        old = os.path.getmtime(full) < cutoff
        if not (old or oversize):
            continue
        try:
            zpath = full + ".zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for dp, dn, fn in os.walk(full):
                    for f in fn:
                        p = os.path.join(dp, f)
                        z.write(p, os.path.relpath(p, root))
            import shutil
            shutil.rmtree(full)
            packed += 1
            oversize = _dir_size(root) > size_gb * (1024 ** 3)
        except Exception:
            pass
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
