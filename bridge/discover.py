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

"""Find the projects you already use Claude Code in.

Claude Code keeps a transcript per session under ~/.claude/projects/. The
folder names there are encoded and lossy, so instead of decoding them this
reads the transcripts themselves: every line carries the working directory
it was recorded in.
"""

import json
import os
import time

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")


def _ours(cwd):
    """Is this working directory the bridge's own archive rather than work?

    The archive search agent runs `claude -p` with its working directory set
    to a project's bridge-logs folder, so the client writes a transcript for
    it like any other session - and this scan, which offers every working
    directory it finds as a project to watch, offered "bridge-logs" as one.
    Max saw two of them appear from a test run.

    A bridge-logs folder is never a project: the bridge writes it. This is a
    display decision and lives here, in the code that builds the list to
    show - never on the intake path, where a filter once made a real session
    invisible (§6).
    """
    parts = [p.lower() for p in
             os.path.normpath(cwd or "").replace("\\", "/").split("/")]
    return "bridge-logs" in parts


def _cwd_from_transcript(path, max_lines=8):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                cwd = row.get("cwd") or (row.get("workspace") or {}).get("current_dir")
                if cwd:
                    if "bridge-probe" in os.path.basename(cwd):
                        return None   # the model probe's scratch, not a project
                    if _ours(cwd):
                        return None   # the bridge's own log folder
                    return cwd
    except Exception:
        pass
    return None


def is_installed(project_dir):
    path = os.path.join(project_dir, ".claude", "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return "bridge.hook" in fh.read()
    except Exception:
        return False


def scan(limit=40):
    """Return the projects Claude Code has been used in, newest first."""
    found = {}
    if not os.path.isdir(PROJECTS):
        return []

    for entry in os.listdir(PROJECTS):
        folder = os.path.join(PROJECTS, entry)
        if not os.path.isdir(folder):
            continue
        try:
            files = [os.path.join(folder, f) for f in os.listdir(folder)
                     if f.endswith(".jsonl")]
        except Exception:
            continue
        if not files:
            continue
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

        cwd = None
        for candidate in files[:3]:
            cwd = _cwd_from_transcript(candidate)
            if cwd:
                break
        if not cwd:
            continue

        cwd = os.path.normpath(cwd)
        seen = os.path.getmtime(files[0])
        if cwd not in found or seen > found[cwd]["seen"]:
            found[cwd] = {"seen": seen, "sessions": len(files)}
        else:
            found[cwd]["sessions"] += len(files)

    rows = []
    for path, meta in found.items():
        rows.append({
            "path": path,
            "name": os.path.basename(path.rstrip("\\/")) or path,
            "exists": os.path.isdir(path),
            "installed": is_installed(path) if os.path.isdir(path) else False,
            "sessions": meta["sessions"],
            "last_used": time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(meta["seen"])),
            "seen": meta["seen"],
        })
    rows.sort(key=lambda r: r["seen"], reverse=True)
    return rows[:limit]
