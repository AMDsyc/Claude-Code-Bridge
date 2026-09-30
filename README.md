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
- **Claude Code**, working, on your PATH.
- Windows or a POSIX system. It was written on Windows first (console windows,
  `taskkill`), with POSIX fallbacks throughout.

Everything binds to `127.0.0.1`. The only call that leaves the machine is the
optional Telegram notification.

## Install and first run

```
git clone https://github.com/<you>/claude-code-bridge
cd claude-code-bridge
```

Then:

```
bridge.bat
```

on Windows, or

```
python -m bridgecore.daemon
```

anywhere. It starts the daemon and opens the panel at
`http://127.0.0.1:8765/`. Add `--no-browser` if you would rather open it
yourself.

Keep that window open — closing it, or Ctrl+C, stops the bridge.

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
  `PYTHONSAFEPATH=1`. The second keeps the working directory off `sys.path`:
  the hooks run as `python -m bridgecore.hook`, and with `-m` Python puts the
  current directory *first*, so a second copy of this package in whatever
  folder the session is sitting in would shadow the installed one. On Python
  3.9 and 3.10 the variable is ignored and the behaviour is what it was.

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

## The acceptance gates

`done` and `stop` accept work, so they are gated. So is `continue`: it carries
a judgement, and a judgement made from the words of a report is acceptance by
hearsay under another name. Only `wait` is free of it - it judges nothing, it
says a process is still running.

**Artefacts.** An accepting verdict needs a `Checked:` block naming what the
planner opened, and the daemon checks those paths exist:

```
Checked: out/run-2026-01-01/handover.txt, out/render.png
```

A path that is not there is refused **by name**. That is the point of checking
existence rather than asking for a sentence: it turns "I checked it" from
something said into something done.

If a piece genuinely has nothing to open — an analysis, an answer, a refusal —
there is a way out:

```
Checked: no artifacts — this was a read-only investigation of the logs,
nothing was changed and there is nothing to open
```

It is accepted, and every use is written to the log at warning level, counted
per project, and shown in the panel. It cannot be used quietly.

**Residence.** If the report changed code — a named source file, a diff, a
commit — the verdict also needs

```
Residence: bridgecore/store.py:norm
```

Where the fix lives. A fix nobody can point at is a patch: it works today, the
next full run does not produce it, and the next person finds the symptom back
with no record of what was done.

**The run.** The planner cannot execute anything: Bash, PowerShell and every
edit tool are denied to it, and a deny beats every permission mode. So "I
verified the fix" could only ever mean "I read that it was fixed" - a rule
about behaviour with nothing under it.

The `check` tool is what is under it. The bridge copies the sources somewhere
isolated - its own data directory, its own client config, hooks switched off -
and runs the compile step, the test suites and a package byte check there,
then hands the planner one line per command with its exit code and a folder
holding the whole output. It changes nothing and never touches the live tree.

Where a project names the checks its code is accepted by, `done` and `stop` on
a report that changed code are refused unless a check **passed after that
report arrived**. An older run says nothing about the work in front of you, so
the daemon compares the two times and says both of them when it refuses. A
failed check refuses too, naming which suite broke.

The tool takes no command, and is built so it never can: the only argument is
the *name* of one suite, matched against a fixed list, and an unknown name is
refused. A tool that ran what it was handed would be a way for the planner to
execute anything at all - the very thing its permission set exists to prevent.

The requirement is not applied everywhere, on purpose. These suites test the
bridge; demanding them before accepting a report about someone's shader would
block that pair for ever on evidence that could never become relevant. A
project opts in:

```json
"projects": {"<path>": {"checks": ["suites"]}}
```

That list is a vocabulary the daemon matches against known kinds. Nothing in
it is ever executed as text.

**A refusal costs the report nothing.** It stays unanswered, the executor
stays blocked, no iteration number is spent, and answering the same report
twice is still impossible. The planner calls `verdict` again with the block
filled in.

The same question is asked one step earlier by a `PreToolUse` hook, inside the
planner's own window, so the refusal arrives as a denied tool call. Both
levels share one implementation.

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

## Configuration

`data/config.json`, created on first run. The keys most worth knowing:

| key | default | what it does |
|---|---|---|
| `port` | `8765` | the local port |
| `projects` | `{}` | watched projects, keyed by path |
| `role_modes` | `executor: bypassPermissions`, `planner: auto` | permission mode per role. A project can override it |
| `role_effort` | `planner: max` | effort level a role's window starts with, passed as `--effort`; a role not named starts at the client's own level. A project can override it in `projects[path].effort` |
| `telegram` | empty | token and chat id |
| `thresholds.idle_hold` | `1200` | how long an idling pair is held rather than answered, in seconds. `0` disables |
| `thresholds.review_timeout` | `1200` | how long a report may wait for a verdict |
| `archive_parallel` | `2` | archive searches at once across all projects |
| `archive_model` | `sonnet` | the model the search agent runs on |
| `retention.days` | `7` | how long the bridge keeps its own logs |

### One trap worth an hour of your evening: the two compaction settings multiply

**The bridge sets a compaction threshold again, per project, and it is the
composition of two settings** — see below for what it composes with. It hands
out `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` only for a project that has both halves,
and strips an inherited one, so there is exactly one source for it. Between
2026-09-01 and 2026-09-02 it set none at all, and this paragraph said so; the
reasoning for setting one again is at the end of this section. The trap is
unchanged, and it cost a fortnight to find.

`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` is a **percent**. Claude Code also has its own
`autoCompactWindow` setting in a project's `.claude/settings.json`, in
**tokens**.

They do not override one another. They **compose**: `autoCompactWindow` becomes
the window the percent is taken of. Set it to 700000 believing you have asked
for a 700k threshold and you have actually asked for 70% of 700k — and
compaction will fire around **477k**, on a window that is really a million.

Nothing warns you, because nothing is broken: every part is doing its job. What
you see is sessions being replaced about **1.5× more often than they should
be**, each replacement paying for a fresh context, and no obvious reason why.
Measured here over nine compactions: 476,221–482,049 tokens, against a control
project with no `autoCompactWindow` that ran to the full million.

**So set `autoCompactWindow` to the real window size, or leave it out
entirely.** If you want to know where your sessions actually compact rather
than where you asked them to, the client writes it down itself: search a
transcript under `~/.claude/projects/` for `compact_boundary` and read
`preTokens`. That is the client's own number, not the bridge's, which is why it
settles the question.

**What this project sets, and why.** `autoCompactWindow` is the real window
size, 1,000,000, in each watched project's own `.claude/settings.json`, and
`autocompact_pct` is 90 in the bridge's config — so the threshold is 900,000.
Both halves or neither: the bridge sends no percentage to a project without the
key, because that would record a threshold the window is not actually running
under. The reasoning is worth having before you copy it. Reaching the ceiling
is survivable — the API answers *prompt is too long*, the client summarises
from there, and measured here about 10k of context survives each time — but it
looks like a death every time, and anything watching a session is capable of
interfering with the recovery. A threshold a little below the ceiling ends the
cycle deliberately instead. It is not free: the threshold decides how much work
a session gets through before it is replaced — roughly 2.3M tokens at a 477k
point, 3.4M at 700k, 5M at the ceiling — and each replacement pays for a fresh
context. About 4% of cycles still end at the ceiling anyway, on the measured
width of a last turn; buying that down to zero would cost another 100k of
threshold. Lower is not safer for free, and neither is higher.

Environment variables:

| variable | what it does |
|---|---|
| `BRIDGE_PORT` | override the port |
| `BRIDGE_DATA` | put the bridge's own files somewhere else — the suites use this |
| `BRIDGE_DEBUG=1` | write a watch log to the temp folder |
| `BRIDGE_NO_HOOKS=1` | a process the bridge spawned that is not half of a pair |
| `BRIDGE_ROLE` | the role of a window. Set per window at launch, never in project settings |

## The rules

`HONESTY.md` holds thirty-five rules both halves are handed at every session
start, and which are put in front of every task and every report. They are not
advice: each one came from something that actually went wrong, and several of
them are the gates described above rather than text — a rule nothing refuses
holds until the first inconvenient day, which is itself one of the rules.

The full canon goes to a session once, on its first delivery — which includes
the first delivery after every handover, since a replacement has been told
nothing. After that each delivery carries the rule titles alone. Editing the
file reaches the next delivery without a restart.

## Running the tests

Seven suites, no runner, no dependencies. Each is a flat script that exits 1
on failure, and each puts its own state in a temp folder, so none of them
touch anything real.

```
python test_handover.py         # the main suite
python test_archive.py          # the archive map
python test_search.py           # the search agent, against a stub
python test_wall_handover.py    # a handover simulated end to end
python test_multipair.py        # three pairs on one throwaway daemon
python test_wake_sim.py         # seeded runs of a pair through faults
python test_recovery_sim.py     # seeded runs of the whole recovery block
python -m py_compile bridgecore/*.py
```

`test_wake_sim.py` runs the same pair ten times with the order of the faults
shuffled by a seed, so a fix that only works when things go wrong in one
particular order is caught. It also carries sabotage modes: switch one on and
the run must go red, which is how a test that cannot fail gets found.

`test_recovery_sim.py` does the same for the repairs themselves - a dead turn,
a silent window, a handover that never arrives - ten seeds, every situation in
each. It takes over ten minutes on its own, which is why it was argued about
before being made part of the acceptance run rather than an extra somebody
remembers.

A run leaves `__pycache__` behind, and a `.pyc` carries `co_filename` - the
absolute path of the source on the machine that compiled it. `.gitignore`
keeps it out of commits, and `check_public.py` refuses a tree that contains
one, but if you are preparing something to publish it is simpler to run the
suites in a copy, or to delete `__pycache__` afterwards and check `git status`
is empty.

## Checking a package

This checks a **package**, not a checkout. The file list it carries names the
layout a package is unpacked into - `bridge.bat` at the top and everything else
under `source/` - which is one level deeper than this repository, where
`bridgecore/` sits beside the launcher. Point it at an archive you built and
the folder you unpacked it into:

```
python verify_package.py <repo> <zip> <unpacked> bytes.txt
```

It compares the sha256 of every file across the repository, the archive entry
and the unpacked copy. Running the tests from an unpacked copy proves that
copy works; only comparing bytes proves it is the code you reviewed.

## What it does not do

Written down because finding out afterwards is worse than reading it here.

- **Windows first.** It was written on Windows — console windows, `taskkill`,
  `SetConsoleCtrlHandler` — with POSIX fallbacks throughout. The POSIX paths
  are the less travelled ones.
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

**2026-09-30 — what is new since the evening release of 2026-09-28 (0e00027)**

- **The reviewing half starts in `auto`, not `plan`.** It is kept from
  writing by what was always its real protection: its edit tools and its
  shell are denied outright, and a deny outranks every mode. At start the
  bridge moves a saved `plan` that was only the default of its day - the
  bridge-wide setting, a project's saved mode, a preset - and leaves any
  other choice alone, naming it. The bridge's own `check` and `loop` tools
  are now allowed by name beside `verdict` and `task`, and a project missing
  one is repaired the next time a window of it starts: a reviewer used to
  stop and ask a person before running its own acceptance check.
- **The reviewing half starts with `max` effort**, on every path that opens
  its window - a start, a restart after a death, a replacement, a resume.
  The working half starts at the client's own level, as before.
- **A short command leaves no lines in the log.** Every command used to
  write a start and an end line, and most take a second or two. A command
  still running after 30 seconds is written when the watch sees it, with how
  long it has run, and its end is written too; a background job and a
  failure are always written. A project can set the threshold, or 0 to write
  every line as before.
- **Two pairs never share a colour.** A colour counts as taken only when a
  configured project holds it, not when a folder that is no project does;
  two configured pairs found sharing one are told apart at start, and the
  chat is told once.
- **An old number is said once.** Lines a project carries from before log
  lines named their project are counted, and the count is said only when it
  changes, not at every start.

**2026-09-28, evening — what is new since the morning release (b300e42)**

The machine lost power in the middle of the day, and most of this is what
that showed: how windows come back after they die, and what the bridge says
about them while it happens.

- **A half whose window died comes back in its own conversation.** Both halves
  are raised with `--resume` of that half's newest session that ever finished
  a turn. The newest session is often a raise that failed before it answered
  anything, and resuming that one brought a window back with no memory of the
  work; the sessions passed over are named in the log. A new session is the
  third try, after two resumes that ended right after they started — the
  working half then starts with `/init`, and the reviewer writes it a handoff
  from the log. After that the bridge stops trying and asks a person. A half
  that a live window already holds is not raised, and the pair is no longer
  paused because a window died, since the half is coming straight back. A
  project can switch automatic raising off; then a death pauses the pair and
  asks a person, as before.
- **After a power cut or a killed bridge, the start raises the windows the stop
  took down**, and its own message says which. It used to say "nothing was
  restarted on its own" and then raise them a few minutes later anyway; for a
  few hours of the same day it held them until a person pressed continue
  instead — that hold is gone. After a clean stop the windows stay up and
  nothing needs raising. A window opened by hand while the bridge was down is
  not seen at that moment: it has had no chance to register yet.
- **When a session ends, the client's own reason is written down**, with how
  long its window had been open. Six sessions ended within seconds of starting
  on the day of the power cut, and the one field that named the reason was not
  kept anywhere.
- **Only the windows a restart touched are told about it.** Every window that
  started after a restart used to be told that the bridge had been restarted
  in the middle of its turn — read from a flag of the whole bridge that only a
  person clears, so a window that had come through untouched was told its turn
  had been cut off. The message now goes once, to the session of the window
  that was up through the stop, and a turn finished after the restart ends it.
  That a turn was cut off is said only when the stop recorded something in
  flight: a report awaiting its review, or a running command. What happened to
  the window is said as it is — up through the stop, or not survived.
- **A long command is not reported as stuck while it is working.** The usual
  length of a command is now learned per program it runs, not per first word
  of the line, and before waking anyone the bridge looks at the CPU used by
  that call's own process tree. A person is rung only after the pair was asked
  and the tree has stood still.
- **A subagent's command is not the working half's.** A command a subagent
  runs after its parent ended its turn no longer holds the restart gate or
  wakes the pair or a person as if the half were stuck. Its outcome is in the
  subagent's own transcript, and the log line that starts it names the
  subagent.
- **Words follow what is done.** Raising a half says whether it is resumed or
  started new, and from which session. Continue on the resume tab checks both
  halves and logs every refusal with its reason. A task held until a new
  window's `/init` ends is logged as held, not as delivered, and the hold
  belongs only to the window that was opened with `/init`. Silence is counted
  within the current session, so a new session does not inherit the silence
  of the one before it.
- **The reviewer's own acceptance run no longer rides on one long request.**
  It starts in the background and is asked about until it ends. A call that
  gets no answer says so — "no result on this call" — instead of reporting a
  run that was going, and passed, as "failed, nothing ran".
- **A test run never starts the real client.** The model probe and the archive
  search started it past the guard that keeps tests from opening a client
  window; in a test copy inside a watched project, the client found that
  project's channel settings and registered with the live bridge. Both now ask
  the same guard. At start, the bridge drops channel records and pair colours
  of folders that are not projects and are gone or under the temporary
  folder.
- **A dead window counts only for the pair whose window it was.** A process
  number that had once been another project's window, and was later given to
  a short-lived shell, was read as "this pair's window is gone" and closed a
  running command's record in a pair it had never belonged to. A number now
  counts only for the half, and the time, that its record was written about.

**2026-09-28 — what is new since the state of 2026-09-13**

More of the same kind of repair: a record standing in for a fact it did not
hold, replaced by asking the side that would know. And one incident of our
own, at the end, with what changed because of it.

- **A spent allowance is told apart from a passing rate limit.** Both arrive
  as the same error category, and the sentence is what tells them apart. When
  a model's allowance runs out, its pair is held until a turn finishes or a
  status line shows another model. When the account's allowance runs out, the
  whole bridge holds until the reset time the client names, or until any
  window finishes a turn. Either is said once, and nothing is re-sent into a
  window that cannot answer — one exhausted weekly allowance used to mean
  thirty hours of revivals into dead windows and 456 messages in a day.
- **A command's record ends with its own call.** A call that fails sends only
  a failure event, and a call another hook refused sends no after-event at
  all; both used to leave the pair counted as busy for up to an hour. The
  record now ends on the failure event, at the turn's end, when its session is
  gone, or when that call's own result appears in the transcript.
- **A window that is asking a person says so, and says what it asks.** A
  permission dialog, or a question put to the person, used to be reported as
  "the model is not answering". What is asked is now read from the half's own
  open call — a question, or a permission for a named tool — and said in one
  message. The message is closed by that window's own record when the question
  is answered or the call refused, never by another window's.
- **The panel does what it shows.** A model picked in a list is the model that
  is launched — it used to be replaced, silently, by the head of the chain;
  "start both" is one start rather than two; and the panel's state is built
  from a copy, so a window registering in the middle of a read no longer
  leaves the panel without an answer.
- **The log loses no lines.** Appends went without a lock, and on Windows an
  append is a seek followed by a write: eight threads writing at once lost
  3–4 % of their lines. Every append now goes through one writer, under its
  own lock.
- **The bridge knows which window holds which session.** In a live window the
  client runs hooks through a shell, so a hook's parent is not the window; the
  hook now finds the client process above it by name, and the bridge keeps a
  book of which session each window holds. A pair held because a window took a
  report and opened no turn is released only by a turn in that same window,
  and a turn-end hook the client itself cancelled is let go at once and
  counted as no silence.
- **The installer leaves alone what works.** A merge that changes nothing
  writes nothing: the file keeps every byte.
- **The last turn before a replacement is reviewed**, and its verdict goes to
  the replacement. A replaced window's background jobs are handed to the
  replacement instead of dying with the old window.
- **The rules no longer tell the reviewing half it cannot run anything.** It
  may watch with a monitor, which measures and accepts nothing; its own
  acceptance run is still the only thing that accepts.
- **The reviewing half's own acceptance run stops the review clock**, so a run
  that takes longer than a review is not read as the reviewer going silent.
  The run writes straight into its own file, so a timeout keeps the output,
  and a process it leaves behind holds nothing open.
- **No wake-up nudge goes into a window that its own monitor is about to
  wake.** A gate that would have held idle reports back was measured and
  rejected: "something is still running" does not tell an interim report from
  a final one.
- **A reviewer's replacement names the window it replaces before it launches**,
  and opens nothing while the half is alive and no window can be named as
  the one it replaces. A window left over
  after a replacement is closed only once its own turn is over; one stuck
  mid-turn for more than an hour rings a person, once. The half being
  replaced is replaced only between its own turns.
- **"The task was taken" is read from every session of the working half since
  the task was written**, so a task already taken in a window that has since
  been replaced is not handed over a second time. The task is recognised
  however its envelope is written.
- **The test suites no longer read and write the user's own client
  settings**; each one points the client at a settings file it owns.
- **A test run removes only what it made itself.** A test that broke the
  deletion safeguard on purpose was run against the real temporary folder and
  deleted folders there that were not its own; that can no longer happen.
  There is no automatic clean-up of the temporary folder any more. A run
  deletes only the folder it created, by its exact path, and only when it
  passed — a failing run keeps it and prints where it is. A test that weakens
  code which deletes things runs only in a disposable copy, and before it
  runs, it is proven that nothing in it can delete outside that copy.
- **Everything else that deletes was made to reach only what its own code
  wrote.** Building the public tree keeps a witness file of what it wrote,
  removes only the files listed there, names anything else it finds and leaves
  it, and refuses a folder that holds files it does not write. Log archiving
  touches only the day folders the bridge writes, never writes over an
  archive that exists, and removes a day folder only after its archive has
  been read back whole — a folder that could not be removed whole used to
  have its archive rewritten from what was left, losing the files that had
  already gone. A restore that cannot remove
  the folder in its way says so instead of carrying on in silence. A second
  repair of a hook's interpreter keeps the first backup.
- **A window's record vouches only for the process it was written about.** A
  process number passes to another program as soon as its process ends, and
  the bridge asked only whether the number was alive: a start was refused
  with "a window is already up" fifteen seconds after that window had been
  stopped, and the same number handed to a stop could have ended whichever
  program held it by then. Every place that decides or acts on a recorded
  window now asks one question — is the number alive, and was its process
  born no later than the record was written — and a stop marks the record
  it stopped. The stop that follows a replacement asks again just before it
  acts.
- **The privacy scan refuses a folder it read nothing in.** Over a folder
  that did not exist it said "no personal data found" and passed.
- **The English rules say what the original says.** A verdict that passes
  judgement — done, stop or continue — needs its Checked block, and only
  wait is free; the English text still named two of the three. It is also
  shorter again, back under the size the suite allows for a text that is
  sent in front of every task.
- **The published suite passes in the published tree.** Checks that need
  files only the working repository has — the tool that builds this tree,
  its sources, the private suite — now say they are not counted there,
  instead of failing.

**2026-09-18 — what is new since the release of 2026-09-01**

Almost all of it is one kind of repair: the bridge was making claims about
windows, processes and work in hand from its own records, and the records were
not the thing they described. Each of these now asks the side that would know.

- **A compaction is the cure starting, not a session dying.** The bridge stands
  aside while a summary is being made and calls the compaction failed only when
  the time it allowed really runs out. A compaction the bridge itself
  interfered with is not admissible as evidence, and a size a session has
  survived may raise its ceiling but never lower it — one success at 475k used
  to set a 559k ceiling on a million-token window and replace healthy sessions.
- **A compaction threshold again, per project, at 900,000** — the composition
  of the two settings described under *Configuration*, sent only where both
  halves are present.
- **Only a compaction that LANDED counts.** A session is replaced at its fifth,
  and the client can announce one every few minutes under a rate limit without
  any of them landing; a count of announcements sent a fresh session to be
  replaced eight minutes after it started. What counts now is a floor recorded
  after the summary, and an announcement that did not land says so in the log.
- **A window that takes a report and opens no turn is noticed.** The witness is
  that window's own transcript, not the channel's acknowledgement; after two
  such reports the pair is held, and the window's screen is read and given one
  keystroke before a person is rung — into a bare prompt only, never into a
  dialog.
- **The consent dialog a new window can stop on is answered.** The bridge finds
  the prompt box from the bottom of the screen up, tells a working window from
  an idle one by the spinner above the box, and writes down what it saw either
  way.
- **Frozen windows were a console selection.** A console with quick-edit on
  blocks the application it belongs to as soon as anything is selected in it,
  and the earlier reading that excluded this could not see the flag at all. The
  bridge now switches quick-edit off for its own windows at every session
  start, and records the mode before and after.
- **The replacement comes up before the window it replaces is stopped**, and
  the old one is closed only once the new one has reported for duty; a
  replacement that has not come up is never vouched for by the channel of the
  window it is replacing; a replacement that would not close blocks the next
  one instead of letting two run; and a swap that never completed is undone,
  leaving the working window alone.
- **A half that died is raised without asking**, with a new session rebuilt
  from the log rather than resumed into whatever killed it.
- **The wall asks for a handoff and waits for the file.** For the reviewing
  half the demand is made once per session — asked at every turn end it
  produced hundreds of demands and hundreds of handoff files in one day — and
  the replacement happens on that half's own facts instead of being held by
  whatever the working half is doing.
- **A background command is not over when its call returns.** Its record
  survives, and it ends on the client's own notification of completion. The id
  that notification carries now comes from the hook's payload; the transcript
  is only a fallback, read around the mark rather than after it, because the
  client writes the line a few kilobytes BEFORE the hook runs — a forward-only
  read left records alive for hours and made the bridge call an idle half busy.
  A notification older than the tail it used to read is swept for once.
- **A pair parked on a person rings.** A reviewer answering "wait" while
  nothing is actually running means the pair is waiting for a human, and that
  is now the difference between a quiet log line and a notification.
- **The live data folder has one writer.** Any process that is not the daemon
  serving the port is refused the state file and the log while its data folder
  is the live one, and the refusal names the way out. A measurement had
  overwritten the daemon's own state with a copy fourteen minutes old.
- **The channel serves each call on its own thread.** Running the acceptance
  through the reviewer's own tool takes minutes, and every other call it made
  used to queue behind it — a verdict once reached the daemon the same second a
  check returned, ten minutes after it was issued, with the other half blocked
  the whole time.
- **Work the other half did not take comes first, by that half's own
  transcript.** A task delivered while a turn was running may be missed by it,
  so it is kept and handed back at the next acceptance — but a task delivered
  to a half that has finished its turn is read by the next one, and a record is
  now dropped when the transcript shows the task was taken up, in either of the
  two shapes the client writes.
- **A restart asks the daemon before stopping it**, in one call rather than two
  with a gap, and a shutdown that lost nothing is no longer reported as a
  crash.
- **The interpreter is part of a hook's mark.** A project carried from another
  machine used to pass as fully installed with every hook naming a Python that
  is not there, and ran for an hour with every report going nowhere.
- **A suite may not open a real client window.** Seventeen of them were once
  left open by a fixture; the refusal is now at the one place a process is
  started, not a flag each suite must remember.
- **The bridge runs its own acceptance only where it is relevant**, so a pair
  working on something unrelated is not blocked on evidence that can never
  apply to it.
- **The acceptance run is eleven commands**, the new one being the recovery
  simulation above.
- **Two more rules, and gates under them.** No claim about the state of
  anything without a witness opened in the same turn — with a check that reads
  what a turn actually opened — and the round-trip rule: an acknowledgement
  that asks for nothing is carried with the next message instead of spending a
  wake of its own.

**2026-08-30**
- A tracked process finishing no longer wakes the planner; it leaves a line in
  the log instead.
- A compaction sample is attributed only to the session that compacted. A
  neighbouring window's size is never borrowed, and where the session reported
  no size of its own nothing is written.
- The launch panel can choose which model a session *starts* on, and the head
  of the chain is labelled.
- A sixth suite, `test_wake_sim.py`: seeded runs of a pair through faults, with
  sabotage modes that must turn the run red.

**2026-08-28**
- A project can be carried to another computer: the bridge notices the old path
  and offers to adopt the history, which only the owner can confirm.
- A repeatedly refused channel registration now says once that the recorded
  window may be stale, instead of only logging the refusal.

**2026-08-23**
- Queued is not delivered: the bridge tracks whether a window has actually read
  what was handed to its channel.

**2026-08-22**
- Compaction is treated as recoverable rather than fatal, and a session is no
  longer replaced for being large when it has survived that size before.
- A session that can never compact is replaced early and calmly instead of at
  the wall.
- A pair waiting on a person is told apart from a pair that is busy, and only
  the first rings anybody.
- A stopped loop stops producing nudges.
- Four pairs proven running on one daemon.

**2026-08-21**
- The watchdog gained its tiers, and a dead turn is repaired by handing the
  work back rather than by waking a human.
- Several pairs going quiet at once is recognised as an outage rather than as
  several separate faults.
- Windowed runs are quiet: nothing the automation opens takes focus.
- A channel's seat is decided by which window spawned it, before age.

**2026-08-19**
- One name, one file, with a gate that refuses a repeated name.
- The planner runs the acceptance, because the planner cannot run anything
  else.
- Silence is not consent: the pair is held when the planner stops answering.

**2026-08-18**
- First public release. Two sessions on one project, one working, one
  reviewing; AGPL-3.0.

---

"Claude Code Bridge" is made by AMDsyc and Claude, 2026.
