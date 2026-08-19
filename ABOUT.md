# About this project

Each section below is meant to be copied on its own.

---

## One line, for the GitHub About field

Three wordings; pick one.

1. A local daemon running one or more Claude Code pairs — one session works,
   the other reviews every finished turn and keeps the thread.

2. Executor and planner, one pair per project, several projects at once:
   review loop, acceptance gates, context accounting and session rotation.

3. Runs your projects in pairs of Claude Code sessions — one edits, one
   judges — and replaces a session before its context runs out. In active
   development.

---

## Paragraph, for the top of the repository

Claude Code Bridge runs two Claude Code sessions on the same project: one has
the hands — it edits files, runs commands and reports what it did — and the
other holds the plan, reviews every finished turn and answers with a verdict.
One daemon runs as many of those pairs as you have projects, a pair per
project, side by side. It carries reports one way and verdicts the other, keeps
each project's log with the project, and replaces a session before its context
runs out so the thread is not lost. The split is what makes long unattended
runs possible: the reviewer's
context stays small because it never does the work, so it can keep judging
clearly for hours after a single session would have filled up. Everything
binds to `127.0.0.1`, there are no dependencies beyond the Python standard
library, and the only outbound call is an optional Telegram notification.

---

## Status

In active development. The shape of it and the rules it enforces change as new
ways around them are found — most of what is in here arrived that way. The
panel, the config keys and the format of what the bridge writes into a project
can change between versions, and backward compatibility is not promised.

---

## Why this exists

A single Claude Code session has a context window, and long work fills it. The
client compacts, and compaction is lossy in the way that hurts most: the
details survive better than the intent. You come back after two hours and the
session is still working, correctly, on a slightly different problem than the
one you set.

The other half of the problem is that nobody can sit and check every step. A
session that reviews itself accepts itself, and it does so most eagerly when
it is furthest from the point. What it needs is a second reader that never
touched the work.

So: two sessions. The executor does, the planner judges. Because the planner
never edits anything, its context grows slowly, and it can keep reviewing long
after the executor has been replaced twice. The bridge measures the executor's
window from the numbers the client actually reports, decides from those
numbers when a session can no longer finish a piece of work, writes a handoff,
starts a replacement and hands it the thread. That decision is arithmetic, not
a feeling.

The rest of it came out of running this for a month and writing down what went
wrong. Reports that said "done" with nothing to open. Verdicts accepting work
nobody had looked at. Temporary workarounds nobody was counting, which is how
forty-five of them accumulate one reasonable exception at a time. Each of
those turned into something that refuses, rather than into another paragraph
of advice — the acceptance gate that checks the paths in a verdict actually
exist on disk, the debt register, the requirement to say where a fix lives.

It is a personal tool, made public because the shape of it may be useful. It
is not a framework and it does not try to be one.

---

## What is in the box

- **Several pairs at once.** One pair per project, running side by side. Each
  has its own loop, pause, note, event feed and archive; the account's
  five-hour limit is the one thing measured across all of them.
- **Pair overview.** A local web panel: a row per pair with how far through its
  life each session is, then everything about the one project you picked — what
  it is doing, its feed, and the buttons that start, pause, hand over and test
  the link.
- **Context accounting and rotation.** Window size, carried context, the
  measured compaction point and the distance to the wall — read from what the
  client reports, per model and per project, with the ceiling calibrated as it
  goes. A session is handed over when its cycle can no longer hold five turns.
- **The review loop.** Every finished executor turn becomes a report the
  planner must answer: `continue`, `done`, `wait` or `stop`. Each iteration is
  committed to git when the project is a repository, and appended to a log
  that lives with the project.
- **Acceptance gates.** `done` and `stop` are refused unless the verdict names
  artefacts that exist on disk, and unless a report that changed code says
  where the fix lives. Refusal costs the report nothing.
- **Debt register.** A declared temporary solution is written to
  `bridge-logs/DEBT.md`, counted, and shown until it is explicitly closed.
- **Archive and search over it.** Everything the pair said is kept per project
  and indexed; a headless agent can be asked questions about it.
- **Telegram, optional.** Only what needs a human: something is stuck, a run
  finished, the five-hour limit is close.
- **A written set of rules** handed to both halves at every session start and
  put in front of every task and every report.

---

## Who made it

"Claude Code Bridge" is made by AMDsyc and Claude, 2026.

---

## Licence

GNU Affero General Public License v3.0 (AGPL-3.0). Free software: use it,
change it, share it - and publish your changes under the same licence.
Running a modified version as a network service counts as distribution, so
the source of what is running stays available. It cannot be closed and resold.

---

## Repository name

`claude-code-bridge` - it says what is inside, and it repeats none of the
names used within it (`bridgecore`, `source`, `releases`). One name meaning
one thing is a rule here rather than a preference: see rule 28 in HONESTY.md.

## Topics for GitHub

```
claude-code
ai-agents
multi-agent
code-review
orchestration
automation
python
no-dependencies
windows
localhost
developer-tools
llm-tooling
```
