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

"""Make sessions show up in the Claude app.

Remote Control is off by default: a session only appears at claude.ai/code
or in the phone app when it is on. The documented way to make that
permanent is the "Enable Remote Control for all sessions" toggle in
/config. The CLI also reads a key from ~/.claude.json, which is what this
sets so you do not have to type anything.

If the key stops working in a future release, the panel says so and points
at the /config toggle instead — it never silently pretends it worked.
"""

import json
import os
import shutil

HOME = os.path.expanduser("~")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
KEY = "remoteControlAtStartup"


def read():
    try:
        with open(CLAUDE_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def status():
    data = read()
    if data is None:
        return {"file": CLAUDE_JSON, "readable": False, "enabled": None}
    return {"file": CLAUDE_JSON, "readable": True, "enabled": bool(data.get(KEY))}


def set_enabled(enabled=True):
    data = read()
    if data is None:
        if os.path.exists(CLAUDE_JSON):
            raise RuntimeError(
                "%s exists but could not be read as JSON, so it was left alone. "
                "Turn the setting on by hand: run /config inside Claude Code and "
                "set 'Enable Remote Control for all sessions' to true."
                % CLAUDE_JSON)
        data = {}

    if os.path.exists(CLAUDE_JSON) and not os.path.exists(CLAUDE_JSON + ".before-bridge"):
        shutil.copyfile(CLAUDE_JSON, CLAUDE_JSON + ".before-bridge")

    data[KEY] = bool(enabled)
    tmp = CLAUDE_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CLAUDE_JSON)
    return status()
