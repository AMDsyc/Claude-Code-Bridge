"""One entry point for every Claude Code hook.

Claude Code passes the event as JSON on stdin. This script forwards it to
the daemon and prints whatever the hook protocol needs on stdout.

It exits 0 whatever happens. A bug in the bridge should degrade the loop,
never kill the session you were working in.
"""

import io
import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
URL = "http://127.0.0.1:%d/event" % PORT


def post(payload):
    # A Stop event may block while the planner reviews the report, so it
    # gets a long timeout. Everything else stays snappy.
    timeout = 1500 if payload.get("hook_event_name") == "Stop" else 8
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    # A process the bridge spawned that is not half of the pair - the
    # archive search agent - runs with this set. It is a plain `claude -p`
    # in a folder that happens to sit inside a watched project, so the
    # project's hooks are its hooks; without this it would report a
    # SessionStart, take a seat in the panel and start being watched for
    # silence, for a one-off run that answers a question and exits.
    if os.environ.get("BRIDGE_NO_HOOKS"):
        sys.exit(0)
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8",
                                 errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", write_through=True)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    event["project_dir"] = os.environ.get("CLAUDE_PROJECT_DIR", event.get("cwd", ""))
    event["role"] = (os.environ.get("BRIDGE_ROLE") or "").strip().lower()

    try:
        reply = post(event)
    except Exception:
        # Daemon is down. Say nothing, let the session carry on.
        sys.exit(0)

    out = reply.get("hook_output")
    if out:
        print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
