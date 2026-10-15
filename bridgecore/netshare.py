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

"""Find the machine that serves a project share by its NAME, and prove it.

A Mac working a project that lives on the PC reaches it as an SMB share,
and the PC's address is the router's to hand out - it changes. So the key
is the server's name, never an address: an address is used only after the
server AT it has said its own name back (NetBIOS node status, `smbutil
status`), and an address that answers with ANOTHER name is not mounted
from, whatever the resolver said. The order: the last address that worked,
then the name through mDNS (`<name>.local`, the system resolver), then
NetBIOS (`smbutil lookup`). Every outcome but the plain one - found where
it was - is one journal line. -> DECISIONS.md 8.88

It FINDS and PROVES, and at a Mac bridge's start it brings back a share a
project lives on (daemon.shares_at_start, M4, 8.90): learned from the
mount table while it is mounted - who, which server, which share - and,
when it is not, the server found by name here and the mount asked of the
system by name, `open smb://<who>@<server>.local/<share>`, which takes the
password from the login keychain. Only when the keychain HAS one: without
it the system puts a password dialog on the screen, and the bridge does
not. A user process cannot make /Volumes/<share> itself; asking is all
there is. At the start only - a share that drops while the bridge runs
is the project lock's to say (projlock.beat).

macOS only for the commands; the logic takes them as arguments, so a case
runs it anywhere.
"""

import json
import os
import re
import socket
import subprocess
import time
import urllib.parse

from . import store

SMBUTIL = "/usr/bin/smbutil"
MOUNT = "/sbin/mount"
SECURITY = "/usr/bin/security"
OPEN = "/usr/bin/open"
ASK_SEC = 8
MOUNT_WAIT_SEC = 30
KNOWN = None           # test seam: None is <data>/netshare.json

# //[who@]server/share on /Volumes/X (smbfs, ...) - the mount table's line
_SMB = re.compile(r"^//(?:([^@\s]+)@)?([^/\s]+)/(\S+) on (.+?) \(smbfs[,)]",
                  re.M)


def _run(cmd, timeout=ASK_SEC):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def resolve(name, run=_run, lookup=None):
    """IPv4 candidates for a name: mDNS first, then NetBIOS. Never raises."""
    found = []
    try:
        for fam, _t, _p, _c, addr in (lookup or socket.getaddrinfo)(
                name + ".local", 445, socket.AF_INET, socket.SOCK_STREAM):
            if addr[0] not in found:
                found.append(addr[0])
    except (OSError, UnicodeError):
        pass
    r = run([SMBUTIL, "lookup", name])
    if r is not None and r.returncode == 0:
        for m in re.finditer(r"IP address of [^:]+:\s*([0-9.]+)", r.stdout):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found


def server_name(addr, run=_run):
    """The name the server at addr gives itself, or "" when it gives none."""
    r = run([SMBUTIL, "status", addr])
    if r is None or r.returncode != 0:
        return ""
    m = re.search(r"^Server:\s*(\S+)", r.stdout, re.M)
    return m.group(1) if m else ""


def find(name, last="", ask=server_name, candidates=resolve, say=None):
    """(the address of the server that IS `name`, what happened).

    "" when none is: not found, or every address found answers as some
    other server - in which case nothing may be mounted from it."""
    tried, notes = [], []
    for addr in ([last] if last else []) + [None]:
        pool = [addr] if addr else [a for a in candidates(name)
                                     if a not in tried]
        for a in pool:
            tried.append(a)
            got = ask(a)
            if got.upper() == name.upper():
                if a == last:
                    return a, ""
                text = "%s found at %s by name%s" % (
                    name, a, "; %s" % "; ".join(notes) if notes else "")
                _said(say, text, "log")
                return a, text
            notes.append("%s %s" % (a, "answers as server %s, not %s - "
                                       "nothing is mounted from it" % (got, name)
                         if got else "does not answer"))
    text = ("%s not found: %s" % (name, "; ".join(notes))
            if notes else "%s not found: no address for the name" % name)
    _said(say, text, "warn")
    return "", text


def mounted(run=_run):
    """{mount point: (who, server, share)} of the SMB mounts there are now.
    The server is its NAME, as find() asks for it: mDNS's .local and the
    service's ._smb._tcp taken off."""
    r = run([MOUNT])
    out = {}
    if r is None or r.returncode != 0:
        return out
    for m in _SMB.finditer(r.stdout or ""):
        who, host, share, point = m.groups()
        server = re.sub(r"(\._smb\._tcp)?\.local\.?$", "", host, flags=re.I)
        out[point] = (urllib.parse.unquote(who or ""), server,
                      urllib.parse.unquote(share))
    return out


def volume_of(path):
    """The /Volumes/<name> a path lives on, or "". Without case: a Mac's
    keys are folded (8.89), the mount point is not."""
    parts = (path or "").replace("\\", "/").split("/")
    if len(parts) > 2 and parts[0] == "" and parts[1].lower() == "volumes" \
            and parts[2]:
        return "/Volumes/" + parts[2]
    return ""


def has_password(who, server, run=_run):
    """Does the login keychain hold an SMB password for who@server? Asked
    for the item's ATTRIBUTES only - never -g or -w, so no secret is read
    and nothing on the screen asks to allow it."""
    for host in (server + ".local", server, server + "._smb._tcp.local"):
        r = run([SECURITY, "find-internet-password", "-a", who, "-s", host,
                 "-r", "smb "])
        if r is not None and r.returncode == 0:
            return True
    return False


def remount(point, rec, finder=None, run=_run, ismount=os.path.ismount,
            wait=None, sleep=time.sleep, say=None):
    """(the server's address, what happened) for a share that is not
    mounted. "" when it was not asked: no server by that name, or no
    password in the keychain - the system would have asked on the screen."""
    who, server, share = rec.get("who", ""), rec["server"], rec["share"]
    addr, why = (finder or find)(server, rec.get("addr", ""), say=say)
    if not addr:
        text = ("%s is not mounted, and it is not mounted again: %s"
                % (point, why))
        _said(say, text, "warn")
        return "", text
    if who and not has_password(who, server, run):
        text = ("%s is not mounted: %s answers at %s, but the keychain holds "
                "no password for %s@%s, and mounting without one puts a "
                "password dialog on the screen - so it is not asked. Mount "
                "it once in Finder with \"Remember this password in my "
                "keychain\"" % (point, server, addr, who, server))
        _said(say, text, "warn")
        return "", text
    url = "smb://%s%s.local/%s" % (
        urllib.parse.quote(who) + "@" if who else "", server,
        urllib.parse.quote(share))
    run([OPEN, "-g", url])
    end = time.monotonic() + (MOUNT_WAIT_SEC if wait is None else wait)
    while not ismount(point) and time.monotonic() < end:
        sleep(0.5)
    if ismount(point):
        text = "%s mounted again: //%s%s/%s, the server by name, at %s" % (
            point, who + "@" if who else "", server, share, addr)
        _said(say, text, "log")
        return addr, text
    text = ("%s: the mount of //%s%s/%s (%s) was asked and is not there "
            "after %d s" % (point, who + "@" if who else "", server, share,
                            addr, MOUNT_WAIT_SEC if wait is None else wait))
    _said(say, text, "warn")
    return addr, text


def _known_path():
    return KNOWN or os.path.join(store.DATA, "netshare.json")


def known():
    """What this machine has seen mounted: {mount point folded: record}."""
    try:
        with open(_known_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def keep(d):
    try:
        tmp = _known_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _known_path())
    except OSError:
        pass


def at_start(projects, now=mounted, again=remount, say=None):
    """Every share a project lives on: learned while mounted, asked back
    by name when not. Returns the lines said."""
    vols = {}
    for p in projects:
        v = volume_of(p)
        if v:
            vols.setdefault(v.lower(), v)
    if not vols:
        return []
    up = {k.lower(): (k, v) for k, v in now().items()}
    seen, said = known(), []
    for key in sorted(vols):
        if key in up:
            point, (who, server, share) = up[key]
            seen[key] = dict(seen.get(key) or {}, point=point, who=who,
                             server=server, share=share)
            continue
        rec = seen.get(key)
        if not rec:
            text = ("%s is not mounted, and this machine has never seen it "
                    "mounted, so the bridge does not know from where: mount "
                    "it once in Finder" % vols[key])
            _said(say, text, "warn")
            said.append(text)
            continue
        addr, text = again(rec.get("point") or vols[key], rec, say=say)
        if addr:
            rec["addr"] = addr
        said.append(text)
    keep(seen)
    return said


def _said(say, text, level):
    try:
        (say or (lambda t, lv: store.journal("bridge", t, level=lv)))(
            text, level)
    except Exception:
        pass

