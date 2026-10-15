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

It FINDS and PROVES, and on a Mac it brings back a share a project lives
on (daemon.shares_at_start, M4, 8.90): learned from the mount table while
it is mounted - who, which server, which share - and, when it is not, the
server found by name here and the mount asked of the system,
`open smb://<who>@<host>/<share>`, which takes the password from the login
keychain. Only when the keychain HAS one for that host: without it the
system puts a password dialog on the screen, and the bridge does not. A
user process cannot make /Volumes/<share> itself; asking is all there is.

A share mounted by ADDRESS is the server's too (132a, 8.150): the table's
IPv4 host is named by what the server there says it is - the proof find()
asks of an address - and an address that says no name stays an address,
which no name takes for its own and which is never kept as the server.
The host asked is the name while it leads to the proven address and the
keychain holds the password under it, else the address when the keychain
holds it there: Finder keeps the one of a share mounted by address under
the address (host_for - the Mac's launcher asks it too, 132b). At the
start, and while the bridge runs (daemon.share_watch): a share a worked
project lives on that dropped is asked back no more often than RETRY_SEC,
and every ask is a journal line.

macOS only for the commands; the logic takes them as arguments, so a case
runs it anywhere.
"""

import ipaddress
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
RETRY_SEC = 300        # a share that dropped while the bridge runs: asked
                       # back no more often than this (132a)
NAMES = {}             # address -> the name its server gave itself
TRIED = {}             # volume folded -> monotonic of its last ask back

# //[who@]server/share on /Volumes/X (smbfs, ...) - the mount table's line
_SMB = re.compile(r"^//(?:([^@\s]+)@)?([^/\s]+)/(\S+) on (.+?) \(smbfs[,)]",
                  re.M)


def _run(cmd, timeout=ASK_SEC):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def mdns(name, lookup=None):
    """The IPv4 addresses `<name>.local` resolves to - where a URL by name
    goes. Never raises."""
    try:
        return [addr[0] for _f, _t, _p, _c, addr in (
            lookup or socket.getaddrinfo)(
                name + ".local", 445, socket.AF_INET, socket.SOCK_STREAM)]
    except (OSError, UnicodeError):
        return []


def resolve(name, run=_run, lookup=None):
    """IPv4 candidates for a name: mDNS first, then NetBIOS. Never raises."""
    found = []
    for a in mdns(name, lookup):
        if a not in found:
            found.append(a)
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


def is_address(host):
    """Is `host` an IPv4 address, and not a name?"""
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        return False


def name_at(addr, run=_run, ask=None):
    """The name the server at `addr` gives itself, asked once an address and
    kept in NAMES; "" when it gives none - and asked again the next time, as
    a server that was busy may answer then."""
    if addr not in NAMES:
        got = (ask or (lambda a: server_name(a, run)))(addr)
        if not got:
            return ""
        NAMES[addr] = got
    return NAMES[addr]


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


def mounts(run=_run, ask=None):
    """{mount point: (who, server, share, address)} of the SMB mounts there
    are now. The server is its NAME, as find() asks for it: mDNS's .local and
    the service's ._smb._tcp taken off, and a share mounted by IPv4 address
    named by what the server there says it is (name_at - the proof find()
    asks of an address, 132a). The address is the table's host when that is
    one, else "". An address whose server says no name stays an address."""
    r = run([MOUNT])
    out = {}
    if r is None or r.returncode != 0:
        return out
    for m in _SMB.finditer(r.stdout or ""):
        who, host, share, point = m.groups()
        server = re.sub(r"(\._smb\._tcp)?\.local\.?$", "", host, flags=re.I)
        addr = server if is_address(server) else ""
        if addr:
            server = name_at(addr, run, ask) or addr
        out[point] = (urllib.parse.unquote(who or ""), server,
                      urllib.parse.unquote(share), addr)
    return out


def mounted(run=_run, ask=None):
    """{mount point: (who, server, share)}: mounts() without the address."""
    return {p: v[:3] for p, v in mounts(run, ask).items()}


def volume_of(path):
    """The /Volumes/<name> a path lives on, or "". Without case: a Mac's
    keys are folded (8.89), the mount point is not."""
    parts = (path or "").replace("\\", "/").split("/")
    if len(parts) > 2 and parts[0] == "" and parts[1].lower() == "volumes" \
            and parts[2]:
        return "/Volumes/" + parts[2]
    return ""


def has_password(who, server, run=_run, addr=""):
    """Does the login keychain hold an SMB password for who@server - or for
    who at `addr`, the proven address, under which Finder keeps the one of a
    share mounted by address (132a)? A server of "" asks of the address
    alone. Asked for the item's ATTRIBUTES only - never -g or -w, so no
    secret is read and nothing on the screen asks to allow it."""
    hosts = [server + ".local", server, server + "._smb._tcp.local"] \
        if server else []
    for host in hosts + ([addr] if addr else []):
        r = run([SECURITY, "find-internet-password", "-a", who, "-s", host,
                 "-r", "smb "])
        if r is not None and r.returncode == 0:
            return True
    return False


def host_for(who, server, addr, run=_run, lookup=None):
    """(the host a mount of who's share on `server`, proven at `addr`, is
    asked of; why it may NOT be asked, "" when it may). By NAME while the
    name leads to the proven address and the keychain holds the password
    under it; else by the ADDRESS when it holds one there - Finder's record
    of a share mounted by address (132a, 8.150). Never a host it holds
    nothing for: that is a dialog on the screen, so the reason comes back
    with the host to mount it by in Finder. remount asks it, and so does
    the Mac's launcher for its own mount (maclaunch.mount, 132b)."""
    named = addr in mdns(server, lookup)
    if not who:
        return (server + ".local" if named else addr), ""
    if named and has_password(who, server, run):
        return server + ".local", ""
    if has_password(who, "", run, addr):
        return addr, ""
    return (server + ".local" if named else addr), (
        "%s answers at %s, but the keychain holds no password for %s, and "
        "mounting without one puts a password dialog on the screen - so it "
        "is not asked" % (server, addr,
                          "%s@%s nor %s@%s" % (who, server, who, addr)
                          if named else "%s@%s (%s.local does not lead "
                          "there)" % (who, addr, server)))


def remount(point, rec, finder=None, run=_run, ismount=os.path.ismount,
            wait=None, sleep=time.sleep, say=None, lookup=None, now=None):
    """(the server's address, what happened) for a share that is not
    mounted. "" when it was not asked: the same share mounted at another
    point, no server by that name, or no password in the keychain for the
    host there is to ask - the system would have asked on the screen."""
    who, server, share = rec.get("who", ""), rec["server"], rec["share"]
    for there, got in sorted((now or (lambda: mounts(run)))().items()):
        if there != point and got[1].upper() == server.upper() \
                and got[2].lower() == share.lower():
            # asked again, the system only opens a Finder window on it
            text = ("%s is not mounted, but //%s/%s is, at %s - so nothing is "
                    "asked: what lives under %s is not there until the share "
                    "is mounted at it" % (point, server, share, there, point))
            _said(say, text, "warn")
            return "", text
    addr, why = (finder or find)(server, rec.get("addr", ""), say=say)
    if not addr:
        text = ("%s is not mounted, and it is not mounted again: %s"
                % (point, why))
        _said(say, text, "warn")
        return "", text
    host, why = host_for(who, server, addr, run, lookup)
    if why:
        text = ("%s is not mounted: %s. Mount it once in Finder with "
                "\"Remember this password in my keychain\"" % (point, why))
        _said(say, text, "warn")
        return "", text
    url = "smb://%s%s/%s" % (
        urllib.parse.quote(who) + "@" if who else "", host,
        urllib.parse.quote(share))
    shown = "//%s%s/%s" % (who + "@" if who else "",
                           addr if host == addr else server, share)
    how = ("by address: the server there says it is %s" % server
           if host == addr else "the server by name, at %s" % addr)
    run([OPEN, "-g", url])
    end = time.monotonic() + (MOUNT_WAIT_SEC if wait is None else wait)
    while not ismount(point) and time.monotonic() < end:
        sleep(0.5)
    if ismount(point):
        text = "%s mounted again: %s, %s" % (point, shown, how)
        _said(say, text, "log")
        return addr, text
    text = ("%s: the mount of %s, %s, was asked and is not there after %d s"
            % (point, shown, how, MOUNT_WAIT_SEC if wait is None else wait))
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


def volumes(projects):
    """{volume folded: /Volumes/<name>} of the projects that live on one."""
    vols = {}
    for p in projects:
        v = volume_of(p)
        if v:
            vols.setdefault(v.lower(), v)
    return vols


def at_start(projects, now=mounts, again=remount, say=None):
    """Every share a project lives on: learned while mounted - the server's
    NAME and the address it was mounted from, never an address as the
    server (132a) - and asked back when not. Returns the lines said."""
    vols = volumes(projects)
    if not vols:
        return []
    up = {k.lower(): (k, v) for k, v in now().items()}
    seen, said = known(), []
    for key in sorted(vols):
        if key in up:
            point, got = up[key]
            who, server, share = got[:3]
            if is_address(server):
                text = ("%s is mounted from %s, and the server there says no "
                        "name, so it is not kept as the share's server: the "
                        "record of the share stays as it was" % (point, server))
                _said(say, text, "warn")
                said.append(text)
                continue
            seen[key] = dict(seen.get(key) or {}, point=point, who=who,
                             server=server, share=share)
            if len(got) > 3 and got[3]:
                seen[key]["addr"] = got[3]
            continue
        rec = seen.get(key)
        if not rec:
            text = ("%s is not mounted, and this machine has never seen it "
                    "mounted, so the bridge does not know from where: mount "
                    "it once in Finder" % vols[key])
            _said(say, text, "warn")
            said.append(text)
            continue
        TRIED[key] = time.monotonic()   # the watch asks again RETRY_SEC on
        addr, text = again(rec.get("point") or vols[key], rec, say=say)
        if addr:
            rec["addr"] = addr
        said.append(text)
    keep(seen)
    return said


def while_running(projects, again=remount, ismount=os.path.ismount,
                  clock=time.monotonic, say=None):
    """A share a worked project lives on that dropped while the bridge runs,
    asked back as at the start (132a, 8.150): by the record kept, no more
    often than RETRY_SEC a share, and only through remount - the password in
    the keychain or nothing asked, never a dialog. Every ask is a journal
    line: remount says one in each outcome. A share never seen mounted is
    left to the start's word. Returns the lines said."""
    seen, said, kept = known(), [], False
    for key, vol in sorted(volumes(projects).items()):
        rec = seen.get(key)
        if not rec:
            continue
        point = rec.get("point") or vol
        if ismount(point):
            TRIED.pop(key, None)    # back: a next drop is asked at once
            continue
        last = TRIED.get(key)
        if last is not None and clock() - last < RETRY_SEC:
            continue
        TRIED[key] = clock()
        addr, text = again(point, rec, say=say)
        if addr and addr != rec.get("addr"):
            rec["addr"] = addr
            kept = True
        said.append(text)
    if kept:
        keep(seen)
    return said


def _said(say, text, level):
    try:
        (say or (lambda t, lv: store.journal("bridge", t, level=lv)))(
            text, level)
    except Exception:
        pass

