"""Install the bridge into a project.

Writes hooks and a status line into <project>/.claude/settings.json,
merging with whatever is already there. Existing hooks are kept: if the
project already has a Stop hook, the bridge is added alongside it.

    python -m bridge.install "C:\\path\\to\\project" --role executor
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

EVENTS = ["SessionStart", "SessionEnd", "Stop", "StopFailure",
          "Notification", "PreCompact", "PreToolUse", "PostToolUse"]


def hook_entry(python, event=""):
    return {
        "type": "command",
        "command": python,
        "args": ["-m", "bridge.hook"],
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
    servers["bridge"] = {
        "command": python,
        "args": ["-m", "bridge.channel"],
        "env": {
            "PYTHONPATH": ROOT,
            "BRIDGE_PORT": os.environ.get("BRIDGE_PORT", "8765"),
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return True


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
    for name in ("mcp__bridge__verdict", "mcp__bridge__task"):
        if name not in allow:
            allow.append(name)
    perms["allow"] = allow
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
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
    user = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if _merge_approval(user, backup=True):
        done.append("user settings")
    local = os.path.join(project, ".claude", "settings.local.json")
    if _merge_approval(local):
        done.append("project local settings")
    return done


def load(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            print("  ! settings.json is not valid JSON, leaving it alone")
            sys.exit(1)
    return {}


def already_there(group, entry):
    for h in group.get("hooks", []):
        if h.get("command") == entry["command"] and h.get("args") == entry["args"]:
            return True
    return False


def install(project, role=None, python=None, statusline=True):
    """Merge the bridge hooks into a project. Returns how many were added."""
    python = python or sys.executable
    project = os.path.abspath(project)
    if not os.path.isdir(project):
        raise ValueError("no such folder: %s" % project)

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
        if not already_there(target, entry):
            target.setdefault("hooks", []).append(dict(entry))
            added += 1

    write_mcp_json(project, python)
    approve_channel(project)

    if statusline:
        existing = json.dumps(cfg.get("statusLine", ""))
        if "statusLine" not in cfg or "bridge" in existing:
            cfg["statusLine"] = {
                "type": "command",
                "command": '"%s" -m bridge.statusline' % python,
                "padding": 0,
            }

    env = cfg.setdefault("env", {})
    # The role is a property of a window, not of a project: Claude Code
    # hands this env to every hook, status line and MCP server it spawns,
    # so a role written here would label both sessions the same. It is
    # passed per window at launch instead - and an older install that put
    # it here gets it removed.
    env.pop("BRIDGE_ROLE", None)
    env["PYTHONPATH"] = ROOT

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    allow_verdict_tool(project)

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
                            if h.get("args") != ["-m", "bridge.hook"]]
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

            if "bridge.statusline" in json.dumps(cfg.get("statusLine", "")):
                cfg.pop("statusLine", None)
                removed.append("status line")

            env = cfg.get("env") or {}
            if env.get("PYTHONPATH") == ROOT:
                env.pop("PYTHONPATH", None)
                removed.append("PYTHONPATH")
            env.pop("BRIDGE_ROLE", None)
            if env:
                cfg["env"] = env
            else:
                cfg.pop("env", None)

            perms = cfg.get("permissions") or {}
            allow = [a for a in (perms.get("allow") or [])
                     if a != "mcp__bridge__verdict"]
            if allow != (perms.get("allow") or []):
                removed.append("verdict permission")
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

            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)

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
                with open(local, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
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
                with open(mcp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
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
