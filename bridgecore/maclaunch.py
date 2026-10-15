"""The Mac's launcher: what bridge.command runs (8.91).

bridge.bat's twin for a Mac, for a person who sits down at one and starts
the bridge with a double-click in Finder:

  (a) tmux and claude are looked for where they are installed, not only on
      PATH - Finder starts Terminal with a short one - and whatever is
      missing is named in ONE line with the command that installs it;
  (b) the PC's network folder is mounted BY NAME, smb://<account>@<PC>.local
      /<share>, so a new address of the PC changes nothing - decided as the
      bridge decides a share asked back (netshare.host_for, 132b): by the
      name while it leads to the server proven to be the PC and the keychain
      holds the password under it, else by that server's address with one
      there. Name, share and account are asked on the first run and kept in
      bridge-mac.json beside bridge.command. No password is ever asked or
      kept, and none is asked for on the screen: with none in the keychain
      nothing is mounted, and the line says how to mount it once in Finder
      with the password remembered;
  (c) the layout move is finished and the daemon started, which opens the
      panel and keeps the Mac awake itself (sessions.keep_awake).

`--check` does (a) and (b) and starts nothing: no daemon, no window, no
mount asked - it says whether the share is mounted. That is the form for
ssh. python3 itself is bridge.command's to check: without it this file
cannot run at all.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

from . import netshare

SETTINGS = "bridge-mac.json"
OPEN = "/usr/bin/open"
MOUNT_WAIT_SEC = 60     # a person may be typing the password into Finder
# Where the installers put them. Finder's Terminal has none of these on
# PATH for certain, and ssh has neither.
PLACES = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin",
          "~/.claude/local")
TOOLS = (
    ("tmux", "brew install tmux  (Homebrew first, from https://brew.sh, if "
             "there is no `brew` either)"),
    ("claude", "curl -fsSL https://claude.ai/install.sh | bash  (then run "
               "`claude` once in Terminal and sign in)"),
)
ASKED = (
    ("server", "The PC's name on the network (as in smb://<name>.local)"),
    ("share", "The shared folder's name on it"),
    ("account", "The account the share is opened with"),
)


def find_tool(name, path=None, places=PLACES, isexe=None):
    """The full path of `name`, or "": PATH first, then where installers
    put it."""
    isexe = isexe or (lambda p: os.path.isfile(p) and os.access(p, os.X_OK))
    hit = shutil.which(name, path=path if path is not None
                       else os.environ.get("PATH", ""))
    if hit:
        return hit
    for d in places:
        p = os.path.join(os.path.expanduser(d), name)
        if isexe(p):
            return p
    return ""


def tools(find=find_tool):
    """({name: full path}, the one line naming what is missing, or "")."""
    found = {name: find(name) for name, _how in TOOLS}
    gone = ["%s - install: %s" % (name, how) for name, how in TOOLS
            if not found[name]]
    return found, ("Missing: " + "; ".join(gone) if gone else "")


def settings_path(home):
    return os.path.join(home, SETTINGS)


def load_settings(home):
    """The three answers, or None while any is missing."""
    try:
        with open(settings_path(home), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    d = {k: str(d.get(k) or "").strip() for k, _q in ASKED}
    return d if all(d.values()) else None


def ask_settings(home, ask=input, say=print):
    """Ask the three on the first run, keep them beside the launcher.
    Nothing else is written there - never a password."""
    say("First run: where is the PC's network folder? (no password - the "
        "mount takes it from the keychain, and says how to put it there "
        "when it is not)")
    d = {}
    for key, question in ASKED:
        while not d.get(key):
            d[key] = ask("  %s: " % question).strip().strip("/")
            if key == "server":
                d[key] = d[key].replace("smb://", "")
                if d[key].lower().endswith(".local"):
                    d[key] = d[key][:-len(".local")]
    with open(settings_path(home), "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")
    say("Kept in %s - delete it to be asked again" % settings_path(home))
    return d


def smb_url(d, host=""):
    """The share's URL by `host` - by default by name, <server>.local."""
    return "smb://%s@%s/%s" % (
        urllib.parse.quote(d["account"]), host or d["server"] + ".local",
        urllib.parse.quote(d["share"]))


def last_address(d):
    """The address the bridge last found the share's server at (kept by
    netshare.at_start), or "": find() asks it first, as remount does."""
    for rec in netshare.known().values():
        if isinstance(rec, dict) and rec.get("addr") \
                and str(rec.get("server", "")).upper() == d["server"].upper() \
                and str(rec.get("share", "")).lower() == d["share"].lower():
            return rec["addr"]
    return ""


def mounted_at(d, now=None):
    """Where the share is mounted now, or "". By server name and share,
    without case; the account it was opened with does not matter."""
    for point, (_who, server, share) in sorted(
            (now if now is not None else netshare.mounted()).items()):
        if server.upper() == d["server"].upper() \
                and share.lower() == d["share"].lower():
            return point
    return ""


def mount(d, check=False, now=None, run=subprocess.call,
          wait=MOUNT_WAIT_SEC, sleep=time.sleep, query=None, lookup=None):
    """(where it is mounted or "", the line that says so). With `check`
    nothing is asked: it only says. Else asked as netshare.remount asks a
    share back (132b): of the server proven by name - the address the
    bridge last found it at, then the name - by the host netshare.host_for
    gives, and not at all while the keychain holds no password for it: the
    system would put a password dialog on the screen. `query` is what asks
    the system (netshare's run), `lookup` what resolves a name."""
    now = now or netshare.mounted
    point = mounted_at(d, now())
    if point:
        return point, "The share is mounted: %s at %s" % (smb_url(d), point)
    if check:
        return "", ("The share is NOT mounted: %s - bridge.command would "
                    "ask for it if the keychain holds its password"
                    % smb_url(d))
    query = query or netshare._run
    addr, why = netshare.find(
        d["server"], last_address(d),
        ask=lambda a: netshare.server_name(a, query),
        candidates=lambda name: netshare.resolve(name, query, lookup),
        say=lambda text, level: None)
    if not addr:
        return "", ("The share is NOT mounted, and nothing is asked: %s - is "
                    "the PC on, and is %s its name? The bridge starts anyway; "
                    "projects on that share are missing until it is mounted"
                    % (why, d["server"]))
    host, why = netshare.host_for(d["account"], d["server"], addr, query,
                                  lookup)
    url = smb_url(d, host)
    if why:
        return "", ("The share is NOT mounted: %s. To mount it once: Finder, "
                    "Go > Connect to Server, %s, with \"Remember this "
                    "password in my keychain\" ticked - after that "
                    "bridge.command mounts it itself. The bridge starts "
                    "anyway; projects on that share are missing until it is "
                    "mounted" % (why, url))
    how = ("by name" if host != addr else
           "by address: the server there says it is %s" % d["server"])
    run([OPEN, url])
    end = time.monotonic() + wait
    while time.monotonic() < end:
        point = mounted_at(d, now())
        if point:
            return point, "The share is mounted: %s at %s, %s" % (
                url, point, how)
        sleep(1)
    return "", ("The share did not mount in %d s: %s - is the PC on and is "
                "%s its name? The bridge starts anyway; projects on that "
                "share are missing until it is mounted" % (
                    wait, url, d["server"]))


def main(argv=None, home=None, find=find_tool, now=None, run=subprocess.call,
         ask=input, say=print, tty=None, start=None, query=None, lookup=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    argv = [a for a in argv if a != "--check"]
    if "--home" in argv:
        i = argv.index("--home")
        home = argv[i + 1]
        del argv[i:i + 2]
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = home or src
    found, gone = tools(find)
    for name, path in sorted(found.items()):
        if path:
            say("%s: %s" % (name, path))
    if gone:
        say(gone)
        return 1
    d = load_settings(home)
    if d is None:
        if check or not (sys.stdin.isatty() if tty is None else tty):
            say("No settings yet in %s: run bridge.command once at this "
                "Mac's screen, it asks for them" % settings_path(home))
            return 1
        d = ask_settings(home, ask, say)
    point, line = mount(d, check=check, now=now, run=run, query=query,
                        lookup=lookup)
    say(line)
    if check:
        say("check: %s" % ("everything is in place" if point else
                           "the share is not mounted"))
        return 0 if point else 1
    env = dict(os.environ)
    dirs = []
    for p in found.values():
        if os.path.dirname(p) not in dirs:
            dirs.append(os.path.dirname(p))
    env["PATH"] = os.pathsep.join(
        dirs + [p for p in env.get("PATH", "").split(os.pathsep)
                if p and p not in dirs])
    say("Starting the bridge. The panel opens in the browser when it is "
        "ready. Keep this window open; close it or press Ctrl+C to stop.")
    if start:
        return start(src, env, argv)
    subprocess.call([sys.executable, "-m", "bridgecore.relayout"], cwd=src,
                    env=env)
    os.chdir(src)
    os.execve(sys.executable, [sys.executable, "-m", "bridgecore.daemon"]
              + argv, env)


if __name__ == "__main__":
    sys.exit(main())
