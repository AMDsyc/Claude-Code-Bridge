# Claude Code Bridge

**Several Claude Code pairs at once — one per project, side by side.** In each
pair two sessions share a project: one does the work, the other reviews every
finished turn. One small Python daemon runs all of them.

Four things it does that are worth knowing before anything else:

- **The reviewer cannot say "looks good".** A verdict that accepts or judges
  work is refused unless it names artefacts — and the daemon opens them. A path
  that is not on disk is refused by name. A report that changed code needs a
  line saying where the fix lives; where a project names the checks its code is
  accepted by, `done` also needs a successful test run made *after* that report.
  This is not advice in a prompt, it is a refusal at the moment of acting.
- **Context is measured, not guessed.** The window, the point compaction fires
  at, the floor it leaves behind, and how many turns are left in the current
  cycle — read from what the client actually reports, per model and per project.
  A session is replaced before it runs out, and the replacement is handed the
  thread in writing.
- **Several projects at the same time.** A pair each, from one daemon. Loop,
  pause, iteration count, event feed, notes and archive belong to the pair; the
  five-hour plan limit belongs to your account, so that one is watched across
  all of them.
- **No pip, no Node, no build step.** Python 3.9+ and its standard library.
  Everything binds to `127.0.0.1`; the only call that leaves the machine is an
  optional Telegram notification. Written on Windows first, with POSIX
  fallbacks.

The **executor** has the hands: it edits files, runs commands and reports what
it did. The **planner** holds the plan, reads every finished turn and answers
with a verdict. A small Python daemon carries reports one way and verdicts the
other, keeps the log, watches how much context each session has left, and
replaces a session before it runs out — writing a handoff so the thread
survives.

One daemon, one pair per project, several projects at a time. Everything that
belongs to a pair is its own: the loop, the iteration count, the pause, the
note you leave it, the event feed, the archive and its search. What is shared
is the account: the five-hour plan limit belongs to you, not to a project, so
the bridge watches it across all of them.

**Status: active development.** The shape of this and the rules it enforces
change as new ways around them turn up — most of what is here arrived that
way. The panel, the config keys and the format of what the bridge writes into
a project can change between versions; backward compatibility is not promised.

## How this differs

Two Claude Code sessions, one working and one reviewing, is not a new idea.
If you have already looked at **TandemKit**, **autonomy-loop**, **claudex**,
**hyperclaude** or one of the several others, that shape will be familiar —
it is the common part, and it is a good idea, which is why everybody arrives
at it.

So the shape is not the thing to compare. These four are, and they are written
as questions you can put to any of them, this one included, by opening the
repository:

- **What happens when the reviewer just says yes?** Here the verdict is
  refused: no artefacts named, no acceptance; a named path that is not on disk
  is refused by name; a code change with nowhere to point is refused; where the
  project says which tests accept its code, a stale test run is refused with
  both timestamps. Look for the place that *says no*, and whether anything
  would notice if it were removed.
- **What happens when the window fills up?** Here the numbers come from what
  the client reports, and the session is replaced before it stops being able to
  finish a turn, with the thread handed over in writing. Look for whether the
  answer is arithmetic or "restart it when it breaks".
- **What happens with a second project?** Here each pair has its own loop,
  pause, feed, notes and archive, and nothing global is left to leak between
  them; the account's five-hour limit is the one thing deliberately shared.
- **What does it cost to install?** Here: Python and its standard library.

I have not audited the others and this section deliberately claims nothing
about them — it would be worth very little if I had guessed. The comparison is
yours to make; what is offered is the list of things worth comparing.

One more, which is not a feature: the rules the pair works under were not
written in advance. Each came from something that went wrong here, and the
ones that matter are enforced by code rather than asked for politely, because
a rule nothing refuses lasts until the first inconvenient day.

## Why two sessions

A single session fills its window and the client compacts it. Compaction keeps
the details better than it keeps the intent, so a long run drifts: two hours
later the session is still working, correctly, on a slightly different problem
than the one you set.

And a session that reviews its own work accepts it. It does so most confidently
when it is furthest from the point.

The split answers both. The planner never edits anything, so its context grows
slowly and it can keep judging for hours after the executor has been replaced
twice. And the executor is judged by a reader that did not do the work.

## What you need

- **Python 3.9 or newer.** Standard library only — nothing to install, no
  virtualenv, no build step.
- **Claude Code**, run once by hand and signed in with your own account: the
  bridge's windows use that sign-in.
- **Windows or macOS.** Each starts its own way, below. It was written on
  Windows first; other POSIX systems have not been tried.

Everything binds to `127.0.0.1`. The only call that leaves the machine is the
optional Telegram notification.

## Starting it on Windows

1. Python 3.9 or newer from python.org, with its `py` launcher, and Claude
   Code. Run `claude` once in a console and sign in.
2. Clone the repository into a folder of its own, or download it from its
   page as a ZIP and unpack it there:
   `git clone <this repository's URL> C:\path\to\my-bridge`.
3. Double-click `bridge.bat`, or run it in `cmd`. It finds `py` or `python`,
   starts the daemon and opens the panel at `http://127.0.0.1:8765/`.
4. Keep that console window open — closing it, or Ctrl+C, stops the bridge.
5. Add a project (below) and press *start* on its row: two windows come up.

Each Claude Code window the bridge starts is a console window of its own.

## Starting it on a Mac

1. python3 (`xcode-select --install`), tmux (`brew install tmux`) and Claude
   Code. Run `claude` once in Terminal **at the Mac's own screen**: the
   sign-in is kept in the keychain.
2. Clone the repository, or download and unpack its ZIP. If macOS refuses
   a downloaded `bridge.command` at first: right-click → Open (macOS 14
   and older), or after that refusal System Settings → Privacy & Security
   → Open Anyway (macOS 15+), or once in Terminal
   `xattr -d com.apple.quarantine bridge.command`; `chmod +x` it if it
   lost its execute bit.
3. **Projects on this Mac:** in Terminal, in the folder that holds
   `bridgecore` - the clone itself - run
   `python3 -m bridgecore.daemon`. **Projects on another computer's network
   folder:** double-click `bridge.command`. It names whatever is missing
   with its install command, mounts that folder by name,
   `smb://<account>@<PC-name>.local/<share>` - it asks the three on the
   first run, and leaves the password to the keychain - and starts the
   bridge.
4. Either way the panel opens at `http://127.0.0.1:8765/` and the Mac stays
   awake while the bridge runs. Keep that Terminal window open — closing it,
   or Ctrl+C, stops the bridge.
5. Add a project (below) and press *start* on its row: two Terminal
   windows come up.

Each Claude Code window is a tmux session shown in a Terminal window of its
own. Closing that Terminal window leaves the session running; *show* on its
row in the panel opens it again. [MAC.md](MAC.md) has the rest: the network
folder, one project to one bridge, moving a project between computers and
back, and what is not done on a Mac yet.

`python -m bridgecore.daemon --no-browser` (`python3` on a Mac) starts the
bridge without opening the panel.

## Adding a project

Easiest in the panel: the projects tab lists folders it found and adds one in
a click. Add a second and a third the same way — they run at the same time,
each with its own pair, and nothing about one reaches another. From the command
line:

```
add-project.bat C:\path\to\project        (Windows, the short way)
python -m bridgecore.install /path/to/project --role executor
python -m bridgecore.install --help
```

`add-project.bat` is a wrapper around the same installer, and it is the only
command-line way in that you need to remember.

Installing **merges** — it never overwrites. Existing hooks are kept, the
previous `settings.json` is backed up next to it as `settings.json.before-bridge`,
only the `bridge` MCP server is approved by name (never
`enableAllProjectMcpServers`), and only two of its tools are allowed without
asking. `uninstall` removes what it added by identity, leaving your own hooks
alone.

What it writes into the project:

- `.claude/settings.json` — the bridge's hooks and its status line
- `.mcp.json` — the `bridge` channel server
- approvals, by exact name, for the bridge's own tools: `mcp__bridge__verdict`,
  `mcp__bridge__task`, `mcp__bridge__check` and `mcp__bridge__loop`
- two environment entries: `PYTHONPATH`, pointing at the bridge, and
  `PYTHONSAFEPATH=1`. The hooks, the status line and the channel run as
  scripts by their path (`python /path/to/bridgecore/hook.py`), never as
  `python -m`: with `-m`, Python searches the folder the session sits in
  before anything else, so a second copy of this package there would be the
  code that ran, and a project on a slow network share made every hook wait
  on the share until the client cancelled it. By path the working directory
  is never searched, on any Python from 3.9; `PYTHONSAFEPATH=1` keeps even
  the script's own folder off `sys.path` on 3.11 and later.
- on a Mac, for a project on a network share only,
  `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`: without it the client runs
  `git status` over the share before the first answer of every new
  conversation, helpers included, which took minutes for a large repository.

Then start the pair from the panel. Two Claude Code windows come up, one per
role.

## Carrying a project to another computer

A folder brought from another machine brings its history and not its key.
Everything the bridge measures is keyed by the project's path, and
`E:\projects\game` and `C:\projects\game` are two keys for one piece of work.
The pair starts with nothing measured, and the arithmetic falls back on figures
nobody took here — 70% of the window for the compaction point, `window - 33000`
for the wall. On a 1M window both are wrong by hundreds of thousands of tokens,
and a session gets replaced for a wall it never reached.

`bridge-logs/` travels with the project and every line in it records the path it
was written under, so the bridge can see this without guessing. It merges those
lines into the feed — a row inside a project's own logs is about that project by
construction — and says so on the strip: *carries history written under another
path: E:\… (1894 lines). Nothing has been changed.*

It stops there, deliberately. A path is not a project: saying two paths are the
same work is a claim about identity, and a wrong one mixes two histories with no
way back. The button on the finding — **this project moved here** — is the only
place that claim is made, and only pressing it re-keys the state and the
calibration onto the current path.

What comes across is marked with the path it was measured under and the day it
arrived, because a figure from another computer is about that computer's client
and its window. A local measurement is never replaced by a carried one, however
much richer the carried one looks; a local entry that has measured nothing is
replaced, because an initial estimate is not evidence. What the move did is
written down and kept.

## Removing a project

The **remove** button on a strip row takes the project out of the bridge's list
— the config and the live state — and touches nothing in the folder: not the
hooks, not `.mcp.json`, not the `.gitignore` line, and above all not
`bridge-logs/`, which is carried history and belongs to the folder rather than
to this machine. Taking the hooks back out is `uninstall` — *stop watching* on
the projects tab — a different decision with a different button. The
calibration is kept as well: a measurement's fate is decided when a project is
adopted, not when a list is tidied.

It works for a project the config does not know about at all, which is the case
that made it necessary: a machine that has moved shows rows for pairs that
survive only as leftover session records. A pair with a window still running is
refused by name — *the executor window (pid 8124) is still running* — because
emptying those records would not stop the window, it would orphan it. Two
presses: the first arms the button and says what will and will not happen.

Add the project again later and the carried-history offer comes back. That is
the same evidence in the same untouched folder producing the same offer, not a
fault.

## The panel

At `http://127.0.0.1:8765/`.

**The strip at the top** is one row per pair — the only part of the panel that
shows more than one project at a time, which is why every row is labelled.
Each row shows how far through its life each half is: not how full its window
is, but how close it is to being replaced. Window fill resets at every
compaction and only tells you where you are inside one cycle; the panel keeps
it in the hover. Click a row to bring that project below. With one project the
strip is hidden — there is nothing to choose between, unless that one project
is carrying history under another path, or is a leftover the config no longer
knows about: both are questions the row is the only place to ask.

Each row also carries a **remove** button, which takes that project out of the
bridge's list. It is described under *Removing a project* below; it is on the
row rather than on a settings page because the owner asked for one per project,
and because a button that acts on a project belongs beside the name of the
project it acts on.

**Everything below the strip is one project**, the one selected in the strip
or the dropdown. State, buttons, gauges, the note box and the feed all belong
to it. That rule exists because an earlier version showed a global "loop is
on" over a project the loop was off for.

**The gauges** per live session: the model, the size of the window, how much
context is carried, and the distance to the wall — the point at which the
bridge replaces the session. All of it comes from what the client itself
reports, not from an estimate.

**Carried lines** appear under the state card when a pair owes something: how
many temporary solutions are still open, and how many pieces were accepted
with nothing to open. Neither blocks anything; they are there so a pile cannot
accumulate unseen.

**The feed** shows the selected project by default and can be switched to all
of them; in that mode every line names the pair it belongs to. That rule holds
everywhere: no claim about state without the project it is about beside it.

## The loop

1. The executor finishes a turn. Its `Stop` hook posts to the daemon and
   blocks.
2. The daemon builds "Executor report N" and delivers it to the planner's
   channel, where it appears in the planner's conversation.
3. The planner answers with the `verdict` tool. The blocked hook returns, and
   the feedback lands in the executor's next turn.
4. The iteration is committed to git if the project is a repository, and
   appended to `bridge-logs/INDEX.md` inside the project.

The four verdicts:

| verdict | means |
|---|---|
| `continue` | keep going on this piece; say what to fix |
| `done` | this piece is accepted — the loop stays on, hand over the next piece with the `task` tool |
| `wait` | a long process is still running |
| `stop` | the whole job is finished and the loop should end. Rare, and the only verdict that stops the run |

`done` does **not** end the run. Only `stop` does, and `loop` turns it back on.

**A verdict's words reach the executor for `done` as well as `continue`.** Worth
saying because it was not always true: `done` built its feedback and sent it
nowhere, so every acceptance arrived as silence and the next piece of work
written into it was lost. `stop` and `wait` deliberately deliver nothing —
there is nothing to act on and nothing to move.

**An acceptance that asks for nothing is held rather than spent on a wake of its
own.** A message costs the size of the window it lands in, not its own length,
so waking the executor to say "accepted" and nothing else costs a full round
trip: the wake, the turn it ends, the report that turn fires, and the planner
woken to read that. Those words are kept and ride with the next thing the
executor is woken for anyway; if nothing wakes it within the hour they go
alone. A `done` carrying the next piece is never held, and neither is
`continue` — the executor is blocked waiting for those words by name.

The planner also has `task`, to hand the executor new work, and `check`, to
run this project's acceptance.

When a long command the executor started finishes, the bridge writes a line in
the log and leaves the planner alone. It used to deliver the news instead, and
that was expensive for nothing: a delivery costs whatever the receiving window
is already carrying, not the length of the message, and the notice asked the
planner for no decision. It reads the line when it next wakes for a reason of
its own.

<!-- rule 36: the index's own lines, not the record -->
## The acceptance gates

What a report has to pass before the planner sees it: [GATES.md](GATES.md).
The section moved there whole, so that this README stays
under 30,000 characters (rule 36).

<!-- /rule 36 -->
## Debt

A temporary solution is allowed — declared:

```
Debt: the exception list is hard-coded — closed by moving it into config.json
```

The bridge writes it into `<project>/bridge-logs/DEBT.md`, counts it, and
shows the count until it is put out with

```
Debt closed: the exception list — moved into config.json
```

The closed line is **kept**. The pile is the evidence, not the balance. None
of this blocks anything: blocking would only teach a pair to stop saying the
word.

## Frames

Mark a task `[FRAMES]` when the result is something to look at. A report that
then names no image or video file that exists on disk reaches the planner
headed `NO FRAMES`, so it can be sent back without reading the prose. Without
the marker nothing is imposed — the bridge does not guess whether work is
visual.

## Telegram, optional

Fill `telegram.token` and `telegram.chat_id` in `data/config.json` (or use the
panel's Telegram tab). Only things that need a person reach the chat: a pair
is stuck, a run finished, the account limit is close. Each pair gets its own
colour so several are readable at a glance.

You can answer from the chat: `/verdict continue …`, `/note`, `/pause`,
`/resume`, `/loop`, `/rotate`. Commands addressed by replying to a message,
or with `@name`.

## Archive and search

Everything the pair says is copied into `<project>/bridge-logs/<date>/`, and
indexed into a map. The panel can then ask questions of it: a headless
`claude -p` reads the transcripts with the map in hand, restricted to
read-only tools. One search at a time per project, and a limit across all of
them (`archive_parallel`, two by default) so several projects cannot between
them start a dozen agents at once.

## Context and rotation, briefly

The bridge reads the window size and the carried context from what the client
reports on every status-line redraw. From those it works out the compaction
point, the distance to the wall, and how much of its life a session has spent.
A session is handed over at its fifth compaction — one that actually landed,
not one the client only announced — with a written handoff. It goes earlier,
and calmly, if its own measurements show it can never compact at all, and
at once if it is past the wall with no ordinary compaction still coming.

A measurement is only ever attributed to the session it was taken from. When a
compaction fires, the size that goes into the calibration is that session's
own; if two windows of one role happen to be live, a neighbour's figure is not
borrowed, and where the compacting session has reported no size of its own the
sample is not written at all — with a line in the log saying why. A skipped
measurement is cheap; one attributed to the wrong window is not.

Rotation writes a handoff, starts the replacement, and gives it the thread.
Only the half whose own numbers ran out is replaced, and only in the pair whose
numbers they are — the other projects carry on untouched.

Those numbers are keyed by the project's path, so a folder carried between
machines arrives with none of them and is measured afresh — see *Carrying a
project to another computer*. Until the move is claimed the bridge says its
figures are assumptions and will not conclude from them that no compaction is
coming.

The account's five-hour limit is the one thing measured across all pairs
rather than per project, because that is what it belongs to.

Sessions are told, in every text that instructs them, that none of this is
theirs to act on: work the task to the natural end of the turn whatever the
figures say.

<!-- rule 36: the index's own lines, not the record -->
## Configuration

Every setting, its default and what it changes: [CONFIGURATION.md](CONFIGURATION.md).
The section moved there whole, so that this README stays
under 30,000 characters (rule 36).

<!-- /rule 36 -->
## The rules

`HONESTY.md` holds thirty-six rules both halves are handed at every session
start, and which are put in front of every task and every report. They are not
advice: each one came from something that actually went wrong, and several of
them are the gates described above rather than text — a rule nothing refuses
holds until the first inconvenient day, which is itself one of the rules.

The full canon goes to a session once, on its first delivery — which includes
the first delivery after every handover, since a replacement has been told
nothing. After that each delivery carries the rule titles alone. Editing the
file reaches the next delivery without a restart.

## Running the tests

Seven suites, no runner, no dependencies; how to run each, and how to check
a package you built: [TESTING.md](TESTING.md).

## What it does not do

Written down because finding out afterwards is worse than reading it here.

- **Windows first.** It was written on Windows — console windows, `taskkill`,
  `SetConsoleCtrlHandler` — with POSIX fallbacks throughout. The POSIX paths
  are the less travelled ones; what is not done on a Mac is listed in
  [MAC.md](MAC.md#known-on-a-mac).
- **The channel needs the research preview.** The planner receives reports
  through an MCP channel that some accounts are not enabled for. When it does
  not come up the bridge falls back on its own: the report is written to
  `<project>/bridge-logs/.../inbox/` and sent to Telegram, and the human
  answers with `/verdict …` from the chat. The loop keeps running; it just
  stops being unattended.
- **English only.** The panel, the messages and the rules are English. There
  is no localisation and no plan stated for one.
- **No tests over the panel itself.** The seven suites cover the daemon, the
  loop, the archive and the handover arithmetic. `panel.html` is checked only
  by a handful of assertions about its structure — nothing drives it in a
  browser.
- **One machine.** Everything binds to `127.0.0.1`. There is no remote mode
  and no multi-user story.

## When the bridge seems unreachable

Press **test the link** in the panel, or `POST /selftest`. It walks each hop
itself — the daemon answering its own endpoint, each channel registered, each
channel port answering, a message actually landing in the executor — and names
the first one that fails.

If every hop the bridge owns works, the break is between a window and its own
MCP subprocess: run `/mcp` in that window and reconnect `bridge`.

## Licence

GNU Affero General Public License v3.0. The full text is in `LICENSE`.

What that means in practice: you may use, study, change and share it, and if
you change it you have to publish your changes under the same licence. The
Affero part matters for a tool like this — running a modified version as a
service other people reach over a network counts, so the source of what is
running has to be available to them. It cannot be closed up and resold.

## Changes

Newest first. Short on purpose — what changed, not why in detail.

**2026-10-15 — what is new since the release of 2026-10-13**

- **A Mac on a PC's network share waits on it less.** Hooks, the status
  line and the channel run by path; such a project gets
  `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` and `MCP_TIMEOUT=120000`; a
  slow project folder no longer holds a reply; a port's owner comes from
  netstat. The archive map reads only what changed and is not built on a
  share: a search there reads the latest records and says why.
- **A slow share is waited out**: the restart gate asks again when the
  bridge answered late, and a commit killed for time names every
  iteration not saved.
- **A replacement is handed the thread only once its client listens**,
  and one that reports late still closes the window it replaced.
- **needs_you reaches you without Telegram**, in the panel; its tone is
  `panel_sound`, off on a Mac.
- **Fewer false calls**: a `wait` on a background task, or a helper stuck
  on the share, calls nobody.
- **The Bypass Permissions screen** is named with its keys, never answered.
- **A saved executor mode stays**, the last run's Terminal window closes
  after a restart, and `bridge.command` keeps LF in a Windows clone.
- **A replaced window's jobs outlive it on a Mac too**: it is killed
  alone, as `/F` does on Windows. The journal says they keep running only
  when its shells answer 10 s later.
- **The handoff a wall asks for** is the file the last one went into, else
  one the project's rules name - never the root's freshest.
- **A replaced channel no longer takes its successor's seat.**
- **The public scan's private words live outside the repository.**

<!-- rule 36: the index's own lines, not the record -->
Older entries, newest first, back to the first release of 2026-08-18:
[CHANGELOG.md](CHANGELOG.md). They moved there whole, so that this README
stays under 30,000 characters (rule 36).

<!-- /rule 36 -->
---

"Claude Code Bridge" is made by AMDsyc and Claude, 2026.
