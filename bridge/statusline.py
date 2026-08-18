"""Status line for Claude Code, doing two jobs at once.

It prints the line Claude Code shows at the bottom of the session, and it
posts the same payload to the daemon. That payload is the only place the
real context and limit numbers come from, so the status line is also the
bridge's telemetry tap.
"""

import io
import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))


def bar(pct, width=10):
    if pct is None:
        return "-" * width
    pct = max(0, min(100, int(pct)))
    return "#" * round(pct * width / 100) + "." * (width - round(pct * width / 100))


def main():
    # Not part of the pair (see hook.py): its telemetry would be one more
    # session's numbers arriving under a role nobody launched.
    if os.environ.get("BRIDGE_NO_HOOKS"):
        sys.exit(0)
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8",
                                 errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", write_through=True)
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/status" % PORT,
            data=json.dumps({
                "payload": data,
                "role": (os.environ.get("BRIDGE_ROLE") or "").strip().lower(),
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass

    model = (data.get("model") or {}).get("display_name", "?")
    cw = data.get("context_window") or {}
    used = cw.get("used_percentage")
    limits = data.get("rate_limits") or {}
    five = (limits.get("five_hour") or {}).get("used_percentage")

    parts = ["%s" % model]
    if used is not None:
        parts.append("ctx %s %d%%" % (bar(used), int(used)))
    if five is not None:
        parts.append("5h %d%%" % int(five))
    print("  ".join(parts))


if __name__ == "__main__":
    main()
