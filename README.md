# Claude Code Bridge

Two Claude Code sessions on one project. One works, one reviews. And one daemon
runs as many of those pairs as you have projects.

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

Clone or unpack, then:

```
bridge.bat
```

on Windows, or

```
python -m bridge.daemon
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
python -m bridge.install /path/to/project --role executor
python -m bridge.install --help
```

Installing **merges** — it never overwrites. Existing hooks are kept, the
previous `settings.json` is backed up next to it as `settings.json.before-bridge`,
only the `bridge` MCP server is approved by name (never
`enableAllProjectMcpServers`), and only two of its tools are allowed without
asking. `uninstall` removes what it added by identity, leaving your own hooks
alone.

What it writes into the project:

- `.claude/settings.json` — the bridge's hooks and its status line
- `.mcp.json` — the `bridge` channel server
- approvals for `mcp__bridge__verdict` and `mcp__bridge__task`
- two environment entries: `PYTHONPATH`, pointing at the bridge, and
  `PYTHONSAFEPATH=1`. The second keeps the working directory off `sys.path`:
  the hooks run as `python -m bridge.hook`, and with `-m` Python puts the
  current directory *first*, so a second copy of this package in whatever
  folder the session is sitting in would shadow the installed one. On Python
  3.9 and 3.10 the variable is ignored and the behaviour is what it was.

Then start the pair from the panel. Two Claude Code windows come up, one per
role.

## The panel

At `http://127.0.0.1:8765/`.

**The strip at the top** is one row per pair — the only part of the panel that
shows more than one project at a time, which is why every row is labelled.
Each row shows how far through its life each half is: not how full its window
is, but how close it is to being replaced. Window fill resets at every
compaction and only tells you where you are inside one cycle; the panel keeps
it in the hover. Click a row to bring that project below. With one project the
strip is hidden — there is nothing to choose between.

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

The planner also has `task`, to hand the executor new work.

## The acceptance gates

`done` and `stop` accept work, so they are gated. `continue` and `wait` accept
nothing and are not.

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
Residence: bridge/store.py:norm
```

Where the fix lives. A fix nobody can point at is a patch: it works today, the
next full run does not produce it, and the next person finds the symptom back
with no record of what was done.

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
A session is handed over when its cycle can no longer hold five turns — not by
counting compactions and not by distance alone.

Rotation writes a handoff, starts the replacement, and gives it the thread.
Only the half whose own numbers ran out is replaced, and only in the pair whose
numbers they are — the other projects carry on untouched.

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
| `role_modes` | `executor: bypassPermissions`, `planner: plan` | permission mode per role. A project can override it |
| `telegram` | empty | token and chat id |
| `thresholds.idle_hold` | `1200` | how long an idling pair is held rather than answered, in seconds. `0` disables |
| `thresholds.review_timeout` | `1200` | how long a report may wait for a verdict |
| `archive_parallel` | `2` | archive searches at once across all projects |
| `archive_model` | `sonnet` | the model the search agent runs on |
| `retention.days` | `7` | how long the bridge keeps its own logs |

Environment variables:

| variable | what it does |
|---|---|
| `BRIDGE_PORT` | override the port |
| `BRIDGE_DATA` | put the bridge's own files somewhere else — the suites use this |
| `BRIDGE_DEBUG=1` | write a watch log to the temp folder |
| `BRIDGE_NO_HOOKS=1` | a process the bridge spawned that is not half of a pair |
| `BRIDGE_ROLE` | the role of a window. Set per window at launch, never in project settings |

## The rules

`HONESTY.md` holds twenty-six rules both halves are handed at every session
start, and which are put in front of every task and every report. They are not
advice: each one came from something that actually went wrong, and three of
them are the gates described above rather than text.

The full canon goes to a session once, on its first delivery — which includes
the first delivery after every handover, since a replacement has been told
nothing. After that each delivery carries the rule titles alone. Editing the
file reaches the next delivery without a restart.

## Running the tests

Five suites, no runner, no dependencies. Each is a flat script that exits 1 on
failure, and each puts its own state in a temp folder, so none of them touch
anything real.

```
python test_handover.py         # the main suite
python test_archive.py          # the archive map
python test_search.py           # the search agent, against a stub
python test_wall_handover.py    # a handover simulated end to end
python test_multipair.py        # three pairs on one throwaway daemon
python -m py_compile bridge/*.py
```

A run leaves `__pycache__` behind, and a `.pyc` carries `co_filename` - the
absolute path of the source on the machine that compiled it. `.gitignore`
keeps it out of commits, and `check_public.py` refuses a tree that contains
one, but if you are preparing something to publish it is simpler to run the
suites in a copy, or to delete `__pycache__` afterwards and check `git status`
is empty.

## Checking a package

If you build an archive of this, verify it rather than trusting it:

```
python verify_package.py <repo> <zip> <unpacked> bytes.txt
```

It compares the sha256 of every file across the repository, the archive entry
and the unpacked copy. Running the tests from an unpacked copy proves that
copy works; only comparing bytes proves it is the code you reviewed.

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

---

"Claude Code Bridge" is made by AMDsyc and Claude, 2026.
