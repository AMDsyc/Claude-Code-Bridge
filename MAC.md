# The bridge on a Mac

The bridge runs on a Mac the way it runs on Windows: one file starts it, the
panel opens in the browser, and each Claude Code window is a tmux session
shown in a Terminal window of its own. This page is the Mac's quick start,
how to carry a pair's work over from another machine and back, and an honest
list of what is not done yet on a Mac.

## Quick start on a Mac

**1. What it needs.** Each is named by `bridge.command` with its install
command if it is missing, in one line:

| Needs | Install |
|---|---|
| python3, 3.9 or newer | `xcode-select --install` |
| tmux | `brew install tmux` (Homebrew first, from https://brew.sh) |
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` |

Then run `claude` once in Terminal, **at the Mac's own screen**, and sign in
with your own account. The bridge's windows use that sign-in; it is kept in
the keychain, which is why it is done at the screen and not over ssh.

**2. Get the bridge.** Clone the repository, or unpack the release zip. If
macOS refuses a downloaded `bridge.command` at first: right-click → Open
(macOS 14 and older), or after that refusal System Settings → Privacy &
Security → Open Anyway (macOS 15+), or once in Terminal
`xattr -d com.apple.quarantine bridge.command`; `chmod +x` it if it lost its
execute bit.

**3. Start it.** Double-click `bridge.command` in Finder; it opens Terminal.
It does three things:

- checks python3, tmux and claude, and stops naming what is missing;
- mounts the PC's network folder **by name**, `smb://<account>@<PC>.local/
  <share>`, so a new address of the PC changes nothing. On the first run it
  asks the PC's name, the share and the account, and keeps them in
  `bridge-mac.json` beside it. **No password is asked or kept there**: Finder
  asks for it once - tick *Remember this password in my keychain* - and takes
  it from the keychain after that. Delete `bridge-mac.json` to be asked again;
- starts the bridge, which opens the panel at `http://127.0.0.1:8765/` and
  keeps the Mac awake while it runs. The panel opens in the default browser,
  or in the one `"browser"` in the bridge's `data/config.json` names by its
  application name - `"Firefox"`, `"Google Chrome"`, `"Safari"`. One that is
  not installed opens the default browser instead, with a line in the journal.

Keep that Terminal window open; closing it, or Ctrl+C, stops the bridge.
`./bridge.command --check` only checks the tools and the share and starts
nothing - the form to use over ssh.

**Projects on this Mac, and no other computer's folder.** `bridge.command`
always asks for a network folder, so start the bridge in Terminal instead:
`python3 -m bridgecore.daemon`, in the folder that holds `bridgecore` (the
clone itself, or `source` in the unpacked zip). It opens the panel and keeps
the Mac awake as above; the folder in step 4 is then the project's own path.

**4. Add a project** in the panel: *projects*, the card *add a folder that is
not in the list*, the folder's path, *watch this folder*. On the network
folder the path is `/Volumes/<share>/<project>`. Launch the executor and the
planner and start the loop, as on Windows.

**One project, one bridge.** A bridge holds a project's lock,
`<project>/bridge-logs/daemon-lock.json`, while it works the folder. Adding
a project takes the lock first: if another machine's bridge holds it, the
add is refused, the holder is named, and nothing in the project is touched.
The lock is given back within 10 s when the project's loop is turned off or
paused, and a holder that died is taken over after 150 s of a still
heartbeat.

**The lid.** A closed lid sleeps a Mac laptop whatever the bridge does, and
the pairs stand still until it is opened. After a sleep the network folder
may be gone: start `bridge.command` again and it mounts it.

**Looking at a window.** Each window the bridge starts opens in Terminal,
in the background, without taking the focus. That Terminal window only
SHOWS it: closing it leaves the Claude Code window running, and *show* on
the window's row in the panel opens it again. The window keeps its own size,
160 by 48, whatever the Terminal window's size. When a window ends, its
Terminal window is closed by the bridge only if macOS already lets the
bridge's python3 control Terminal. The bridge asks macOS without putting up
a dialog and never asks you, so without that permission the window stays as
*[Process completed]* until you close it. From a shell,
`ls /tmp/tmux-$(id -u)` lists the windows' sessions, named
`bridge-<role>-<id>`; `tmux -L <name> attach` shows one, and Ctrl-b then d
leaves it running.

**Restarting the bridge.** `python3 -m bridgecore.relayout --now` from
`source`, or the panel's restart button. It refuses, naming the pair, while
any pair is busy; otherwise it stops the bridge cleanly and starts it again
in a new Terminal window, as a double-click on `bridge.command` does, and
waits until the panel answers. The windows of the pairs are not touched.
The Terminal window the bridge ran in before is left as *[Process
completed]*, and the new run closes it within a minute, with the same
permission as above. While the screen is locked, macOS closes only the tab:
Terminal keeps the window, empty, and a second close does not take. The
bridge says so in its journal and closes that empty window once the screen
is unlocked. The new window usually takes the same tty, say
`/dev/ttysN`, that the old one let go of. That is why no tty is ever taken
as proof of which window it is: only a one-tab window with nothing left
running in it is closed, and a window you have a shell in never is.

**Whose account.** By default every window and every `claude -p` the bridge
runs logs in with the client's own sign-in, the account signed in at step 1.
`"use_token_file": true` in the bridge's `data/config.json` makes it read a
token from `~/.bridge-claude-token` (from `claude setup-token`) instead: only
on a machine where every session should be that account.

## Carrying on a pair's work from another machine

The project lives in the PC's shared folder; the work moves to the Mac.

1. **On the PC**, in the panel, turn the project's loop off. Within 10 s the
   PC's bridge gives the project's lock back, and its journal says so.
2. **On the Mac**, run `bridge.command` and add the project from the network
   folder, `/Volumes/<share>/<project>`, in *add a folder that is not in the
   list*.
3. **The old sessions stayed on the PC** - their conversations are in that
   machine's Claude data - so the Mac raises new ones: launch the executor and
   the planner. They start without the old conversation; the project's
   handoff carries the thread.
4. **Read first: the pointer in the project's `CLAUDE.md`** - the handoff it
   names. Give it to the new pair as its first task.

### Moving it back, and over again

A project can go back and forth; nothing either machine needs is deleted.

1. **On the machine that has it**, turn the project's loop off in the panel.
   Within 10 s its bridge gives the lock back, and its journal says so.
2. **On the other machine**, start the bridge (`bridge.bat` on the PC,
   `bridge.command` on the Mac), launch the project's windows and turn its
   loop on. The launch takes the lock first; while the first machine
   still holds it, the launch is refused, names that machine, and changes
   nothing in the project.
3. With the lock, the launch puts this machine's python and bridge folder
   back into the project's hooks, status line and `.mcp.json` - the other
   machine wrote its own there. The file as it came is kept beside it, as
   `.claude/settings.json.before-bridge-python` the first time and with the
   date after that; the project's own hooks and servers are kept.
4. As in step 3 of the move above, each machine raises its own sessions,
   and the handoff carries the thread.

## Known on a Mac

What is not done or not proven on a Mac in this release, each with where it
lives and how to see it.

- **A handover has not been seen on a Mac.** A live loop runs there: a
  real executor and planner, each in its tmux window, with reports, verdicts
  and compactions through the bridge. Not yet seen: a window replaced at its
  wall, with the old window closed after the new one comes up. Where:
  `daemon.handover`, `daemon.close_leftovers`. To see it: *hand over* on a
  window's row in the panel's sessions, and watch both windows in Terminal.
- **`bridge.command` always asks for a network folder.** Its three
  questions (`ASKED` in `bridgecore/maclaunch.py`) have no answer for
  projects on the Mac's own disk; start those with `python3 -m
  bridgecore.daemon`, as in step 3.
- **Two checks of `test_multipair.py` are Windows-only** and are named as
  skipped at the end of a Mac's run, with the reason: a POSIX rename does
  not care who holds the old file open, so there is no loss to count. The
  suites run on the Mac's own `python3` (3.9); its thread names and frames
  differ from 3.10 and later, and the bridge and the suites are written for
  both.
- **The share is brought back at the bridge's start only with its password
  in the keychain** (`netshare.at_start`, `netshare.remount`) - without one,
  mounting would put a dialog on the screen, so it says why instead. Not
  exercised live: the test Mac had no keychain item for its share. A share
  lost while the bridge runs is said in the journal, not mounted again.
  `bridge.command` mounts it itself at every start. To see it: mount the
  share in Finder with the password remembered, stop the bridge, eject the
  share, and start the bridge with `python3 -m bridgecore.daemon`: the
  journal says *mounted again*.
- **A project on a network share runs without the client's git context.**
  `install` writes `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` into its
  `.claude/settings.json` (`install.on_network_share`, DECISIONS 8.121):
  otherwise the client runs `git status` over the share before the first
  answer of every new conversation, helpers included, which took minutes
  for a large repository. A project on the Mac's own disk does not get it.
  And the hooks, the status line and the channel run by path, never with
  `-m` (8.122): this `python3` is 3.9, which ignores `PYTHONSAFEPATH`, and
  with `-m` every hook searched the share first and was cancelled at the
  client's 30 s.
- **On a network share the bridge does not rebuild the archive map**
  (`daemon.archive_map_off`, the key `archive_map_on_share`, off; DECISIONS
  8.133, 8.136). A pass read a project's whole archive over the share after
  every archived turn - 46 GB for the largest - and slowed every pair. The
  archive is still copied; `/archive-map` answers with the last map built,
  from before this release, and says why. A search (`/archive-search`,
  which the panel's search asks) builds none either: it reads the last map
  and says so, with the map's age, in its answer - anything copied since
  is in the folders and not in the map.
- **The panel makes no tone on a Mac** unless `panel_sound` is set (the
  checkbox in the panel's notifications card, or `/config`; DECISIONS
  8.135). With no Telegram chat set up, every needs_you rang the panel's
  two 880 Hz tones; the banner and the word to the planner stay.
- **Installing from the command line takes no project lock.**
  `python3 -m bridgecore.install <project>` writes the hooks whoever holds
  the folder; only adding in the panel (`daemon.handle_add_project`) takes
  the lock first. To see it: run it on a folder whose
  `bridge-logs/daemon-lock.json` names another machine.
- **A case-sensitive volume.** The bridge keys a folder by its path with the
  case folded (`store.caseless`), as APFS and a PC's share both ignore case.
  Two folders whose names differ only in case on a case-sensitive volume
  would be one project to it.
- **The archive's search and the model probe** (`sessions.headless_client`)
  use the client's full path and its sign-in, as a window does; checked with
  a stand-in client only, not against a real one.
