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

"""Install the bridge into a project.

Writes hooks and a status line into <project>/.claude/settings.json,
merging with whatever is already there. Existing hooks are kept: if the
project already has a Stop hook, the bridge is added alongside it.

    python -m bridgecore.install "C:\\path\\to\\project" --role executor
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# PostToolUseFailure since 2026-09-23: a call that fails ends with that
# event ONLY, never PostToolUse (measured on client 2.1.280), so without it
# a failed tracked command stayed "running" in the bridge. -> DECISIONS.md
# 8.24
EVENTS = ["SessionStart", "SessionEnd", "Stop", "StopFailure",
          "Notification", "PreCompact", "PreToolUse", "PostToolUse",
          "PostToolUseFailure"]


def save_json(path, data):
    """The ONE way install writes a JSON file. Returns whether it wrote.

    INSTALL MERGES, AND A MERGE THAT CHANGES NOTHING WRITES NOTHING. Every
    writer here used to `json.dump` the whole file back, always: a working
    `.mcp.json` lost its closing newline, a PYTHONPATH that differed from
    ours only in capitalisation was spelled again, and on 2026-09-25 install
    run on copies of three live projects changed 6 of their 14 files while
    adding nothing. So the text on disk is read first, and if it already
    parses to `data` the file is not opened for writing at all - byte for
    byte what it was. When it is written: indent 2, the line ending and the
    closing newline the old file had (a new file gets one), atomically.
    `test_handover` case 109 holds the census: no `json.dump` into a file
    outside this function. -> DECISIONS.md 8.30
    """
    old = None
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            old = fh.read()
    except OSError:
        old = None
    if old is not None:
        try:
            if json.loads(old) == data:
                return False
        except ValueError:
            pass
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if old is None or old.endswith("\n"):
        text += "\n"
    if old is not None and "\r\n" in old:
        text = text.replace("\n", "\r\n")
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".bridge-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return True


def same_root(value):
    """Is this PYTHONPATH our package's folder, spelled any way Windows
    allows? The comparison marks_missing already makes (8.10's normcase)."""
    return os.path.normcase(value or "") == os.path.normcase(ROOT)


def line_interp(command):
    """The interpreter a status-line command names: the quoted first word,
    or the bare one."""
    c = (command or "").strip()
    if c.startswith('"'):
        return c[1:].split('"', 1)[0]
    return c.split(None, 1)[0] if c else ""


def status_line_ok(line):
    """Is this the bridge's status line, and does it run here?

    `py -m bridgecore.statusline` is a working line on this machine (py is
    on PATH and imports the package) and it is the owner's spelling - the
    same thing `already_there` says about a hook written with `py`. It is
    kept. Only a line naming the old module, or an interpreter that is not
    here or cannot import bridgecore, is the bridge's to rewrite.
    """
    command = (line or {}).get("command") if isinstance(line, dict) else ""
    if "bridgecore.statusline" not in (command or ""):
        return False
    return interp_ok(line_interp(command))[0]


def channel_entry_ok(entry):
    """Is this `bridge` entry in .mcp.json ours, current and runnable?"""
    if not isinstance(entry, dict):
        return False
    env = entry.get("env") or {}
    return (list(entry.get("args") or []) == ["-m", "bridgecore.channel"]
            and same_root(env.get("PYTHONPATH"))
            and str(env.get("BRIDGE_PORT") or "")
            == os.environ.get("BRIDGE_PORT", "8765")
            and interp_ok(entry.get("command") or "")[0])


def hook_entry(python, event=""):
    return {
        "type": "command",
        "command": python,
        "args": ["-m", "bridgecore.hook"],
        # Stop may block while the planner reviews; everything else is quick
        "timeout": 1800 if event == "Stop" else 30,
        "statusMessage": "bridge",
    }


def write_mcp_json(project, python):
    """Merge the bridge channel server into the project's .mcp.json."""
    path = os.path.join(project, ".mcp.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            print("  ! .mcp.json is not valid JSON, leaving it alone -")
            print("    the planner channel will not load until it is fixed")
            return False
    servers = data.setdefault("mcpServers", {})
    # A working entry is left exactly as it is - its interpreter spelled
    # its own way, its PYTHONPATH in its own capitalisation (8.30).
    if not channel_entry_ok(servers.get("bridge")):
        servers["bridge"] = {
            "command": python,
            "args": ["-m", "bridgecore.channel"],
            "env": {
                "PYTHONPATH": ROOT,
                "BRIDGE_PORT": os.environ.get("BRIDGE_PORT", "8765"),
            },
        }
    save_json(path, data)
    return True


def _merge_approval(path, backup=False):
    """Add "bridge" to enabledMcpjsonServers in one settings file."""
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return False
        if backup and not os.path.exists(path + ".before-bridge"):
            try:
                shutil.copyfile(path, path + ".before-bridge")
            except Exception:
                pass
    names = data.get("enabledMcpjsonServers")
    if not isinstance(names, list):
        names = []
    if "bridge" not in names:
        names.append("bridge")
    data["enabledMcpjsonServers"] = names
    save_json(path, data)
    return True


# THE BRIDGE TOOLS A WINDOW CALLS WITHOUT ASKING, by exact name - never the
# whole server. `check` and `loop` joined verdict and task on 2026-09-30:
# the planner runs in `auto` since then (8.59), and a planner stood 6.4
# minutes on "asking for permission (to use bridge - check (MCP))" on
# 2026-09-30 14:24:28 - check was never on this list. marks_missing asks
# for every one of them, so ensure_marks repairs a project at its next
# launch. Monitor is deliberately NOT here: it runs any shell command, and
# allowing it in a project's settings would let the planner run commands
# with nobody asked - the thing its deny list exists to prevent.
BRIDGE_TOOLS_ALLOWED = ("mcp__bridge__verdict", "mcp__bridge__task",
                        "mcp__bridge__check", "mcp__bridge__loop")


def allow_verdict_tool(project):
    """Let the planner call the bridge's verdict tool without asking.

    Every MCP tool call needs approval by default, so an unattended loop
    would stop dead on the first verdict. Only this one tool is allowed,
    by exact name - not the whole server, and nothing else is touched.
    """
    path = os.path.join(project, ".claude", "settings.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return False
    perms = data.setdefault("permissions", {})
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    for name in BRIDGE_TOOLS_ALLOWED:
        if name not in allow:
            allow.append(name)
    perms["allow"] = allow
    save_json(path, data)
    return True


def keep_autocompact_on(project):
    """Make auto-compaction explicit rather than assumed.

    Measured against the client on 2026-08-21: it defaults to ON -

        function iP(){ if(DISABLE_COMPACT||DISABLE_AUTO_COMPACT) return false;
                       return setting("autoCompactEnabled", true).value }

    - so writing `true` changes nothing today. It is written anyway, for one
    reason: the setting is a switch a person can flip in the client's own
    settings screen, and if it is ever flipped off the compaction threshold
    the bridge sets stops meaning anything, silently, and every window runs
    to the hard limit. An explicit `true` in the project's settings makes
    that visible instead of mysterious.

    Written to the project's settings.json, beside the hooks, so it travels
    with the project and shows up in a diff.
    """
    path = os.path.join(project, ".claude", "settings.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return False
    if data.get("autoCompactEnabled") is True:
        return False
    data["autoCompactEnabled"] = True
    save_json(path, data)
    return True


def approve_channel(project):
    """Pre-approve the bridge channel so no session ever asks about it.

    Only "bridge" is approved, by name - never enableAllProjectMcpServers,
    which would also wave through any other server that lands in the repo
    later. Written in two places on purpose: an approval in the project's
    own checked-in settings is ignored until the folder is trusted, while
    the user-level file applies right away, trusted or not.
    """
    done = []
    # BRIDGE_CLAUDE_SETTINGS is a TEST SEAM, never a setting - the same
    # arrangement BRIDGE_CLAUDE_JSON has: without it every suite that
    # installs into a throwaway project reads, and on the day it lacks the
    # approval WRITES, the user's own ~/.claude/settings.json. -> DECISIONS.md
    # 8.35
    user = (os.environ.get("BRIDGE_CLAUDE_SETTINGS")
            or os.path.join(os.path.expanduser("~"), ".claude",
                            "settings.json"))
    if _merge_approval(user, backup=True):
        done.append("user settings")
    local = os.path.join(project, ".claude", "settings.local.json")
    if _merge_approval(local):
        done.append("project local settings")
    return done


def trust_folder(project, config=None):
    """Say, in the CLIENT's own config, that this folder is trusted.

    NOT A CONVENIENCE - FATAL BY DEFAULT. A window launched into a folder
    the client has not been told to trust stops on

        Quick safety check: Is this a project you created or one you trust?
        ...
        > No, exit
          Yes, I trust this folder
        Enter to confirm - Esc to cancel

    with the selection on "No, exit". It runs no hooks and starts no MCP
    server until somebody answers, and nobody does: rule 29 births the
    window minimised and unfocused, so the pair never comes up. That is
    exactly the symptom 5.7 calls "the window never came up", and a blind
    keystroke there does not rescue it - the default ANSWERS "exit". Read
    verbatim off the console of a fixture window on 2026-09-03, from a
    separate process -> ANALYSIS-client-silence.md.

    `approve_channel`'s docstring has said since it was written that an
    approval in the project's own settings "is ignored until the folder is
    trusted". Nothing set the trust. This does.

    The key is the absolute path with FORWARD slashes: the spelling the
    client's own message asks for and the one its existing entries use.
    Merged, never replaced - the entry keeps its other fields, every other
    project is untouched, one backup is kept, and the write is atomic.

    Returns the key it set, or "" when there was nothing to do. Never raises:
    this is on the install path and a missing or unreadable client config is
    not a reason to fail an install.

    THE ONE THING IT CANNOT ANSWER is the other prompt a bridge window
    meets, the `--dangerously-load-development-channels` warning. Measured
    the same evening: answering it writes nothing to ~/.claude.json, a
    graceful close writes nothing, an entry made byte-identical to a live
    project's still gets it, and `--channels` removes the prompt but then
    delivers nothing at all. So this fixes the fatal half and leaves the
    survivable one, where the default is "I am using this for local
    development" and Enter proceeds.
    """
    # BRIDGE_CLAUDE_JSON is a TEST SEAM, never a setting - the same
    # arrangement BRIDGE_TELEGRAM_API has, and for the same reason: without
    # it every suite that launches a window would merge its throwaway temp
    # projects into the real client config on this machine.
    path = (config or os.environ.get("BRIDGE_CLAUDE_JSON")
            or os.path.join(os.path.expanduser("~"), ".claude.json"))
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            return ""
        key = os.path.abspath(project).replace("\\", "/")
        projects = cfg.setdefault("projects", {})
        entry = projects.setdefault(key, {})
        if entry.get("hasTrustDialogAccepted") is True:
            return ""
        bak = path + ".before-bridge"
        if not os.path.exists(bak):
            shutil.copyfile(path, bak)
        entry["hasTrustDialogAccepted"] = True
        save_json(path, cfg)                    # atomic, as it always was
        return key
    except (OSError, ValueError):
        return ""


def load(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            print("  ! settings.json is not valid JSON, leaving it alone")
            sys.exit(1)
    return {}


# The package was called `bridge` until 2026-08-19. An entry naming the old
# module is OURS and stale - not somebody else's hook to be preserved - and
# it has to be removed, not merely joined by a new one. Leaving it is what
# broke every pair that day: PYTHONPATH had been updated to the new folder
# while the hook entries still read `-m bridge.hook`, so every event in all
# four projects raised "No module named 'bridge'". install() kept it because
# it only recognised the new spelling as its own.
LEGACY_HOOK_ARGS = (["-m", "bridge.hook"],)
OUR_HOOK_ARGS = (["-m", "bridgecore.hook"],) + LEGACY_HOOK_ARGS


def drop_stale(group):
    """Remove hook entries that are ours under an older name."""
    before = list(group.get("hooks") or [])
    group["hooks"] = [h for h in before
                      if list(h.get("args") or []) not in LEGACY_HOOK_ARGS]
    return len(before) - len(group["hooks"])


def already_there(group, entry):
    """Is this entry already in this group?

    OUR HOOK IS IDENTIFIED BY ITS ARGS, NOT BY THE INTERPRETER THAT SPELLS
    THEM. This matched on command AND args, and `marks_missing` a few lines
    down matched on args alone - two readers of one fact, free to disagree,
    and on 2026-09-02 they did. A watched project had its bridge hooks
    written with `"command": "py"`; install builds its entry with
    `sys.executable`, an absolute path to the same interpreter, so
    `marks_missing` correctly said the hooks were present while this said
    they were absent, and a single `install` run left that project with
    **sixteen** bridge hooks - every event firing twice: two Stop events per
    turn, two status posts, two PreCompact samples.

    It was invisible because the two disagree only where the interpreter is
    spelled differently, and this bridge's own project carries the absolute
    path, so its install runs report "hooks added: 0" and always had.
    `ensure_marks()` inside `launch()` asks `marks_missing`, which is the
    correct reader, so the file was not doubling on every launch - only on
    an explicit install. -> DECISIONS.md 8.4

    `py` is not a mistake to be corrected, either: CLAUDE.md records that
    `python` is not on PATH on this machine and `py` is. So this recognises
    the entry and leaves it exactly as the owner wrote it.
    """
    want = list(entry.get("args") or [])
    ours = [list(a) for a in OUR_HOOK_ARGS]
    for h in group.get("hooks", []):
        if list(h.get("args") or []) != want:
            continue
        if want in ours:
            return True
        if h.get("command") == entry.get("command"):
            return True
    return False


_INTERP_OK = {}


def interp_ok(cmd):
    """Can this machine actually run that hook command? (ok, why not)

    Two questions, and the cheap one first. A command that is a PATH has to
    exist; a bare name has to be findable the way the OS would find it
    (`py` is written into a hook deliberately - see the paragraph in
    install() - and it is a name, not a path). Only if it resolves is the
    second question asked, once per distinct command and cached: can it
    import the package the hook is going to run? That catches the other
    half of the same failure - an interpreter that exists but knows
    nothing about bridgecore.

    Never raises: anything that cannot be established answers OK, because
    refusing to launch a pair over a check that itself broke is worse than
    the blindness this exists to catch.
    """
    cmd = (cmd or "").strip().strip('"')
    if not cmd:
        return False, "the hook names no command at all"
    if cmd in _INTERP_OK:
        return _INTERP_OK[cmd]
    ans = (True, "")
    try:
        if os.path.sep in cmd or (os.path.altsep and os.path.altsep in cmd):
            if not os.path.exists(cmd):
                ans = (False, "%s is not on this machine" % cmd)
        elif shutil.which(cmd) is None:
            ans = (False, "%s is not on PATH" % cmd)
        if ans[0]:
            env = dict(os.environ)
            env["PYTHONPATH"] = (os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))) + os.pathsep
                + env.get("PYTHONPATH", ""))
            r = subprocess.run([cmd, "-c", "import bridgecore"], env=env,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
            if r.returncode != 0:
                ans = (False, "%s runs but cannot import bridgecore" % cmd)
    except Exception:
        ans = (True, "")
    _INTERP_OK[cmd] = ans
    return ans


def hook_commands(cfg):
    """Every distinct command a BRIDGE hook in this config names."""
    out = []
    for _ev, groups in (cfg.get("hooks") or {}).items():
        for g in groups or []:
            for h in (g.get("hooks") or []):
                if list(h.get("args") or []) == ["-m", "bridgecore.hook"]:
                    c = (h.get("command") or "").strip().strip('"')
                    if c and c not in out:
                        out.append(c)
    return out


def repair_hook_python(project, python):
    """Point this project's bridge hooks at an interpreter that exists.

    Returns (repaired_from, path) or (None, ""). The backup is its own
    name, `.before-bridge-python`, and not the `.before-bridge` one that
    install already uses: they answer different questions - "what was here
    before the bridge" and "what did the bridge rewrite when it found a
    dead interpreter" - and one file cannot answer both.
    """
    s_path = os.path.join(project, ".claude", "settings.json")
    try:
        with open(s_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return None, ""
    dead = [c for c in hook_commands(cfg) if not interp_ok(c)[0]]
    if not dead:
        return None, ""
    # The FIRST backup is never written over: a second repair used to copy
    # the once-repaired file onto it, and the settings as they were before
    # the bridge touched them were gone. Later ones carry the time in their
    # name. -> DECISIONS 8.46
    bak = s_path + ".before-bridge-python"
    if os.path.exists(bak):
        bak = "%s.%s" % (bak, time.strftime("%Y%m%d-%H%M%S"))
    try:
        if not os.path.exists(bak):
            shutil.copyfile(s_path, bak)
    except Exception:
        pass
    n = 0
    for _ev, groups in (cfg.get("hooks") or {}).items():
        for g in groups or []:
            for h in (g.get("hooks") or []):
                if list(h.get("args") or []) == ["-m", "bridgecore.hook"] \
                        and (h.get("command") or "").strip().strip('"') \
                        in dead:
                    h["command"] = python
                    n += 1
    # The status line names its interpreter inside one string, so it is
    # rewritten by hand rather than by the same loop.
    line = ((cfg.get("statusLine") or {}).get("command") or "")
    for c in dead:
        if c and c in line:
            cfg["statusLine"]["command"] = line.replace(c, python)
            n += 1
            break
    try:
        save_json(s_path, cfg)
    except Exception:
        return None, ""
    return (dead[0], s_path) if n else (None, "")


def marks_missing(project):
    """Name every bridge mark this project is missing, or [] when whole.

    Inspection only - it writes nothing and it never raises. A pair whose
    project carries no marks starts BLIND: no hooks means no events reach
    the daemon, no `bridge` server in .mcp.json means no channel, and the
    window looks perfectly alive while none of it works. That is not a
    theory. On 2026-08-19 at 23:32:47 a watched project was removed from
    the watch list, `/remove-project` called uninstall() - correctly - and
    the marks went. The project came back into config.json afterwards without
    install ever running again, and on 2026-08-21 04:26:57 both windows
    launched into that state. The only thing anybody heard was the startup
    watchdog ten minutes later saying the windows "never came up", which
    named neither the cause nor the file.

    Each string is written to be read out in a warning: it says what is
    absent and which file it is absent from, because "not installed" sends
    the reader looking in the wrong place.
    """
    gone = []
    try:
        project = os.path.abspath(project)
        if not os.path.isdir(project):
            return ["the folder itself is gone: %s" % project]

        s_path = os.path.join(project, ".claude", "settings.json")
        cfg = {}
        if os.path.exists(s_path):
            try:
                with open(s_path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except Exception:
                # Unreadable is not the same as absent, and the difference
                # decides what a person does next: repair the file by hand,
                # or let install merge into it.
                return ["%s is not valid JSON, so nothing can be merged "
                        "into it" % s_path]
        else:
            gone.append("%s does not exist" % s_path)

        hooks = cfg.get("hooks") or {}
        absent = []
        for ev in EVENTS:
            groups = hooks.get(ev) or []
            if not any(list(h.get("args") or []) == ["-m", "bridgecore.hook"]
                       for g in groups for h in (g.get("hooks") or [])):
                absent.append(ev)
        if absent:
            gone.append("%s: no bridge hook on %s"
                        % (s_path, ", ".join(absent)))
        # AND A HOOK THAT NAMES AN INTERPRETER THIS MACHINE DOES NOT HAVE
        # IS AS ABSENT AS ONE THAT IS NOT THERE. A project carried from
        # another machine brings its settings.json with it, and until
        # 2026-09-04 this walk read only the ARGS - so eight hooks all
        # naming a Python that does not exist here counted as whole, and
        # the pair launched blind for the better part of an hour with
        # every report going nowhere. -> DECISIONS.md 8.10
        for c in hook_commands(cfg):
            ok, why = interp_ok(c)
            if not ok:
                gone.append("%s: the bridge hooks name %s - %s"
                            % (s_path, c, why))

        if "bridge" not in json.dumps(cfg.get("statusLine", "")):
            gone.append("%s: no bridge status line" % s_path)

        # the bridge tools allowed by name (8.59): a planner in auto that
        # meets one missing here stops and asks a person
        allowed = set((cfg.get("permissions") or {}).get("allow") or [])
        short = [n for n in BRIDGE_TOOLS_ALLOWED if n not in allowed]
        if short:
            gone.append("%s: permissions.allow lacks %s"
                        % (s_path, ", ".join(short)))

        env = cfg.get("env") or {}
        # normcase, because this is a Windows path and the same folder
        # arrives spelled several ways - the one comparison in the package
        # that was still doing it raw. Found 2026-08-28: the settings on
        # this machine can carry one capitalisation of the bridge's own folder
        # while the folder on disk carries another, so whether a project
        # read as "missing a mark" depended on the capitalisation of
        # whatever put the package on sys.path. `ensure_marks` warns and
        # re-runs install on the answer, so a false one means a warning and
        # an install at every single launch, for ever.
        if os.path.normcase(env.get("PYTHONPATH") or "") \
                != os.path.normcase(ROOT):
            gone.append("%s: env PYTHONPATH is not %s" % (s_path, ROOT))
        if env.get("PYTHONSAFEPATH") != "1":
            # Without it a stray bridgecore/ next to the session shadows the
            # installed one and the hook that runs is that copy.
            gone.append("%s: env PYTHONSAFEPATH is not 1" % s_path)

        m_path = os.path.join(project, ".mcp.json")
        data = {}
        if os.path.exists(m_path):
            try:
                with open(m_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                return gone + ["%s is not valid JSON, so the channel cannot "
                               "be declared" % m_path]
        else:
            gone.append("%s does not exist" % m_path)
        if "bridge" not in (data.get("mcpServers") or {}):
            gone.append("%s: no `bridge` server, so no channel" % m_path)

        # THE MARK THAT IS NOT IN THE PROJECT AT ALL. Everything above is a
        # file in the folder; this one lives in the client's own config, and
        # without it the window stops on the trust dialog before it runs a
        # single hook - see trust_folder. It belongs here because
        # ensure_marks is the one thing that looks before a launch, and a
        # launch is exactly when this bites.
        c_path = (os.environ.get("BRIDGE_CLAUDE_JSON")
                  or os.path.join(os.path.expanduser("~"), ".claude.json"))
        if os.path.exists(c_path):
            try:
                with open(c_path, "r", encoding="utf-8") as fh:
                    client = json.load(fh)
            except (OSError, ValueError):
                client = None
            if isinstance(client, dict):
                key = project.replace("\\", "/")
                ent = (client.get("projects") or {}).get(key) or {}
                if ent.get("hasTrustDialogAccepted") is not True:
                    gone.append("%s: projects[%s] is not marked trusted, so "
                                "a window stops on the trust dialog with "
                                "\"No, exit\" selected" % (c_path, key))
    except Exception as exc:                       # never break a launch
        return ["could not be checked: %s" % exc]
    return gone


def install(project, role=None, python=None, statusline=True):
    """Merge the bridge hooks into a project. Returns how many were added."""
    python = python or sys.executable
    project = os.path.abspath(project)
    if not os.path.isdir(project):
        raise ValueError("no such folder: %s" % project)

    # FIRST, AND BEFORE ANYTHING IS MERGED: a settings.json carried from
    # another machine names an interpreter that is not on this one, and
    # every hook in it is dead. install() merges by IDENTITY - a bridge
    # hook is one whose args are ["-m", "bridgecore.hook"] - so it would
    # find eight of them, call them present, and change nothing. The
    # repair is here rather than in the merge for that reason: what is
    # wrong is the command of a mark that already exists.
    # -> DECISIONS.md 8.10
    _dead, _where = repair_hook_python(project, python)
    if _dead:
        print("  ! the bridge hooks named %s, which is not usable on this "
              "machine" % _dead)
        print("    rewritten to %s; the file as it was is beside it, as "
              ".before-bridge-python" % python)

    claude = os.path.join(project, ".claude")
    os.makedirs(claude, exist_ok=True)
    path = os.path.join(claude, "settings.json")

    if os.path.exists(path) and not os.path.exists(path + ".before-bridge"):
        shutil.copyfile(path, path + ".before-bridge")

    cfg = load(path)
    hooks = cfg.setdefault("hooks", {})

    added = 0
    for ev in EVENTS:
        entry = hook_entry(python, ev)
        groups = hooks.setdefault(ev, [])
        target = None
        for g in groups:
            if not g.get("matcher"):
                target = g
                break
        if target is None:
            target = {"hooks": []}
            groups.append(target)
        for g in groups:
            drop_stale(g)
        if not already_there(target, entry):
            target.setdefault("hooks", []).append(dict(entry))
            added += 1

    write_mcp_json(project, python)
    approve_channel(project)

    if statusline:
        existing = json.dumps(cfg.get("statusLine", ""))
        # Somebody else's line is theirs; OURS is rewritten only when it
        # does not work here. `py -m bridgecore.statusline` works and was
        # rewritten anyway, to an absolute path, on every install - found
        # on a watched project 2026-09-25. -> DECISIONS.md 8.30
        if "statusLine" not in cfg or ("bridge" in existing and
                                       not status_line_ok(
                                           cfg.get("statusLine"))):
            cfg["statusLine"] = {
                "type": "command",
                "command": '"%s" -m bridgecore.statusline' % python,
                "padding": 0,
            }

    env = cfg.setdefault("env", {})
    # The role is a property of a window, not of a project: Claude Code
    # hands this env to every hook, status line and MCP server it spawns,
    # so a role written here would label both sessions the same. It is
    # passed per window at launch instead - and an older install that put
    # it here gets it removed.
    env.pop("BRIDGE_ROLE", None)
    # Ours, in any capitalisation Windows allows, stays as it is written.
    if not same_root(env.get("PYTHONPATH")):
        env["PYTHONPATH"] = ROOT
    # And keep the CURRENT DIRECTORY off sys.path.
    #
    # The hooks are spawned as `python -m bridgecore.hook`, and with -m Python
    # puts the working directory FIRST on sys.path - ahead of PYTHONPATH. So
    # any folder the session happens to be sitting in that contains a
    # `bridge/` package shadows the installed one, and the hook that runs is
    # that copy. Measured, not supposed: with PYTHONPATH pointing at the real
    # bridge, `import bridgecore.hook` from a folder holding a second copy loaded
    # the second copy, and left its __pycache__ there.
    #
    # It happened here because a public copy of this project was assembled in
    # a subfolder of a watched project. Nothing was harmed - the two copies
    # were identical - but the class of problem is: the bridge would silently
    # run code that is not the code that was installed.
    #
    # PYTHONSAFEPATH is 3.11+; on 3.9 and 3.10 it is ignored, which leaves
    # exactly today's behaviour rather than breaking anything.
    env["PYTHONSAFEPATH"] = "1"

    save_json(path, cfg)

    allow_verdict_tool(project)
    trusted = trust_folder(project)
    if trusted:
        print("  folder marked trusted for the client: %s" % trusted)
    if keep_autocompact_on(project):
        print("  auto-compaction written as explicitly on")

    gi = os.path.join(project, ".gitignore")
    line = "bridge-logs/"
    try:
        body = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
        if line not in body:
            with open(gi, "a", encoding="utf-8") as fh:
                fh.write(("\n" if body and not body.endswith("\n") else "") + line + "\n")
    except Exception:
        pass

    return added


def uninstall(project):
    """Take the bridge back out of a project, touching only its own marks.

    Hooks the bridge added, its status line, its channel entry, the channel
    approval and the verdict permission - each identified by what it is,
    never by position, so anything you added yourself stays. The logs and
    the .gitignore line are left alone; they are yours to keep or delete.
    """
    project = os.path.abspath(project)
    removed = []

    path = os.path.join(project, ".claude", "settings.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = None
        if cfg is not None:
            hooks = cfg.get("hooks") or {}
            for ev in list(hooks):
                groups = hooks.get(ev) or []
                for g in groups:
                    keep = [h for h in (g.get("hooks") or [])
                            if list(h.get("args") or [])
                            not in OUR_HOOK_ARGS]
                    if len(keep) != len(g.get("hooks") or []):
                        removed.append("hook %s" % ev)
                    g["hooks"] = keep
                groups = [g for g in groups if g.get("hooks")]
                if groups:
                    hooks[ev] = groups
                else:
                    hooks.pop(ev, None)
            if hooks:
                cfg["hooks"] = hooks
            else:
                cfg.pop("hooks", None)

            _sl = json.dumps(cfg.get("statusLine", ""))
            if ("bridgecore.statusline" in _sl
                    or "bridge.statusline" in _sl):
                cfg.pop("statusLine", None)
                removed.append("status line")

            env = cfg.get("env") or {}
            if env.get("PYTHONPATH") == ROOT:
                env.pop("PYTHONPATH", None)
                removed.append("PYTHONPATH")
            if env.get("PYTHONSAFEPATH") == "1":
                env.pop("PYTHONSAFEPATH", None)
                removed.append("PYTHONSAFEPATH")
            env.pop("BRIDGE_ROLE", None)
            if env:
                cfg["env"] = env
            else:
                cfg.pop("env", None)

            perms = cfg.get("permissions") or {}
            allow = [a for a in (perms.get("allow") or [])
                     if a not in BRIDGE_TOOLS_ALLOWED]
            if allow != (perms.get("allow") or []):
                removed.append("bridge tool permissions")
            if allow:
                perms["allow"] = allow
            else:
                perms.pop("allow", None)
            if perms:
                cfg["permissions"] = perms
            else:
                cfg.pop("permissions", None)

            names = [n for n in (cfg.get("enabledMcpjsonServers") or [])
                     if n != "bridge"]
            if names:
                cfg["enabledMcpjsonServers"] = names
            else:
                cfg.pop("enabledMcpjsonServers", None)

            save_json(path, cfg)

    local = os.path.join(project, ".claude", "settings.local.json")
    if os.path.exists(local):
        try:
            with open(local, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            names = [n for n in (data.get("enabledMcpjsonServers") or [])
                     if n != "bridge"]
            if names:
                data["enabledMcpjsonServers"] = names
            else:
                data.pop("enabledMcpjsonServers", None)
            if data:
                save_json(local, data)
            else:
                os.remove(local)
            removed.append("channel approval")
        except Exception:
            pass

    mcp = os.path.join(project, ".mcp.json")
    if os.path.exists(mcp):
        try:
            with open(mcp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            servers = data.get("mcpServers") or {}
            if servers.pop("bridge", None) is not None:
                removed.append("channel server")
            if servers:
                data["mcpServers"] = servers
            else:
                data.pop("mcpServers", None)
            if data:
                save_json(mcp, data)
            else:
                os.remove(mcp)
        except Exception:
            pass

    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--role", default="executor")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--no-statusline", action="store_true")
    args = ap.parse_args()

    try:
        added = install(args.project, role=args.role, python=args.python,
                        statusline=not args.no_statusline)
    except Exception as exc:
        print("  ! %s" % exc)
        sys.exit(1)
    print("  hooks added: %d" % added)
    print("  role: %s" % args.role)
    print("\n  Start Claude Code in that folder and the bridge will pick it up.")


if __name__ == "__main__":
    main()
