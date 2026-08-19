# Posts

Ready to send as they are. Every claim here is something a reader can check by
opening the repository; nothing is promised that the code does not already do.

---

## Show HN

**Title**

    Show HN: A review loop for Claude Code where the reviewer can't just say "looks good"

**Body**

Two Claude Code sessions on one project: one does the work, the other reviews
every finished turn. That part is not new. What I kept running into was the
next problem — the reviewer accepts everything, and most confidently when it
understands the work least.

So acceptance is not a prompt here, it is a refusal. A verdict that accepts
work must name artefacts, and the daemon opens them; a path that is not on
disk is refused by name. A report that changed code must say where the fix
lives. Where a project declares which tests accept its code, `done` also needs
a successful run made after that report — the daemon runs it, because the
reviewer has no shell by design.

The other half is context. Window size, where compaction fires, the floor it
leaves, how many turns are left — measured from what the client reports, then
the session is replaced before it runs out and the replacement is handed the
thread in writing.

One daemon runs a pair per project, several at once. Python 3.9+, standard
library only, no pip, no Node, no build step; everything on 127.0.0.1, with
one optional Telegram notification. Windows first, POSIX fallbacks.

Active development, and the honest warnings: the panel, the config keys and
the on-disk formats change between versions, backward compatibility is not
promised, and there are no tests over the web panel itself.

<repository link>

---

## Reddit — r/ClaudeAI

**Title**

    I stopped trusting Claude to review its own work, so I made the acceptance step refuse

**Body**

A session that reviews its own work accepts it. I knew that in theory. What
convinced me was watching it happen: the more thoroughly a session had lost
the thread, the more confident the "done, all working as expected" got. Being
far from the point does not make a reviewer cautious, it makes it breezy.

So I run two sessions on the same project. One has the hands — edits files,
runs commands, reports what it did. The other holds the plan, reads every
finished turn and answers with a verdict. A small Python daemon carries the
reports one way and the verdicts the other.

The part I would actually defend is not the two sessions, it is that the
reviewer *cannot* wave things through:

- a verdict that accepts or judges work has to name artefacts, and the daemon
  opens them — a path that is not there is refused by name;
- a report that changed code has to say where the fix lives, or it is not
  accepted;
- where the project declares which tests accept its code, `done` needs a
  successful run made *after* the report. The reviewer has no shell — Bash and
  every edit tool are denied to it — so the daemon runs the tests and hands
  back exit codes. Otherwise "I verified the fix" can only ever mean "I read
  that it was fixed".

Every one of those rules exists because it was broken here first, and each is
enforced in code rather than asked for in a prompt.

It also measures context instead of guessing: window, compaction point, the
floor after it, turns remaining — then replaces a session before it runs out
and hands the replacement the thread in writing. One daemon runs a pair per
project, several projects at once.

Python 3.9+, standard library, no pip and no Node, everything local except an
optional Telegram message. It is in active development and formats change
between versions.

<repository link>

---

## X / Twitter — three variants, one point each

**1 — the gate**

    Two Claude Code sessions: one works, one reviews. The part that took
    effort wasn't the pairing, it was making the reviewer unable to say
    "looks good" - a verdict that accepts work has to name files, and the
    daemon opens them. Missing path, refused by name.

    <link>

*(256 characters, measured)*

**2 — context**

    Most agent loops handle a full context window by telling you to restart
    when it breaks. This one measures: window, where compaction fires, the
    floor after it, turns left in the cycle - then replaces the session
    before it runs out and hands the next one the thread in writing.

    <link>

*(275 characters, measured)*

**3 — the install**

    A Claude Code review loop with no pip, no Node and no build step -
    Python's standard library and nothing else. Runs a pair of sessions per
    project, several projects at once, all on 127.0.0.1. The only call that
    leaves the machine is an optional Telegram note.

    <link>

*(259 characters, measured)*

---

## One line, for awesome-claude-code style lists

    Claude Code Bridge - runs a pair of Claude Code sessions per project,
    several projects at once: one session works, the other reviews every
    finished turn, and the acceptance gate refuses a verdict whose named
    artefacts are not on disk. Measures context and rotates sessions before
    they run out. Python standard library only, local, Windows-first.

---

## Notes for whoever sends these

- `<repository link>` and `<link>` are placeholders. Fill them in before
  posting.
- There is no demo recording, and none is promised in any of the text above.
  If one is made later, the Show HN body is the place for it — one line, after
  the first paragraph.
- The character counts on the X variants include the text only, not the link.
  Check them again after the URL is added.
- Nothing here says "revolutionary", "game-changing" or "powerful", and it
  should stay that way: every sentence is meant to be a statement about
  behaviour that a reader can go and verify.
