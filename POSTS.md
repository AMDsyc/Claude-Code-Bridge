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

## Reply to a review — Makefile, uv, folders, Docker

Ready to send as a comment reply.

> Thanks — all four are fair, and two of them landed. Answering each with
> what I actually checked rather than with an opinion.
>
> **Makefile.** Added. Worth being precise about what it buys, though: `make`
> is not installed on Windows by default — it is not on the machine this is
> developed on — so it does not make anything portable. What it gives is the
> entry point you expect if you are on POSIX, spelled the way you expect it:
> `make start`, `make install PROJECT=…`, `make test`, `make verify`. Every
> target is a thin wrapper over a command that already existed; there is no
> build logic in it, and there should not be. On Windows `bridge.bat` still
> needs nothing installed.
>
> **uv.** Not doing this one, and here is the number behind it: the project
> has **zero** third-party imports. Twenty-four files, twenty-six distinct
> modules imported, every one of them in the standard library — checked by
> reading the AST of each file and comparing against the interpreter's own
> stdlib list, not by reading the README. So uv would manage an empty
> dependency list, and to do that it would have to be installed, on a project
> whose current install cost is nothing at all. I also thought about a
> `pyproject.toml` for metadata without dependencies, and decided against it
> for a reason specific to this project: making it pip-installable puts a
> second importable copy of `bridgecore` on the machine, and the hooks name
> one folder explicitly via `PYTHONPATH`. A second copy that shadows the one
> the hooks use is a failure this project has already had — it is why
> `PYTHONSAFEPATH=1` is written into every installed project. I would rather
> not manufacture that ambiguity for metadata. Identity is covered another
> way: `verify_package.py` compares sha256 of every shipped file across the
> repository, the archive and the unpacked copy.
>
> **Folders.** Partly there already: the package is `bridgecore/`, and what
> sits beside it at the top is five test scripts and two tools. Moving the
> tests into `tests/` I costed out and decided against. The count: 15 files
> name a suite by its filename, 121 mentions in all. Most of those are prose
> and would only read oddly, but three would actually break — the explicit
> file list in `verify_package.py`, the runner in the daemon that shells out
> to each suite, and the public builder — and each suite would need its own
> `sys.path` line changed to find the package from one level down. The
> `verify_package.py` list is the part I care about: the shipped archive
> mirrors the repository, so moving the tests changes the shape of the
> package, and that archive's whole job is to prove the package is the code
> that was tested. For twenty-odd files in a flat tree that is a lot of churn
> against the one artefact you least want to churn. Happy to be argued out of
> it if you see a benefit I have not.
>
> **Docker.** Genuinely does not fit, and I want to say what I checked rather
> than just "no". The daemon does not only serve HTTP: it launches
> `claude` in its own console window on the host
> (`sessions.py`, `CREATE_NEW_CONSOLE`), reads session transcripts from
> `~/.claude/projects` (`sessions.py`, `discover.py`), and the hooks are run
> by the client on the host and POST to `127.0.0.1` (`hook.py`). Each session
> also runs its own MCP channel process, listening on a loopback port the
> daemon has to reach. A container would isolate the daemon from the very
> sessions it manages. The partial form — daemon in a container, windows on
> the host — is possible on paper with `--network=host` and the transcript
> directory mounted, and it buys nothing: you would still need Claude Code
> installed on the host, still need the hooks there, and you would have added
> a mount and a network mode to a program whose entire dependency list is the
> Python standard library. If someone wants it for a POSIX server with
> headless sessions, that is a different design and worth discussing on its
> own terms; as a wrapper around this one it adds moving parts without
> removing any.

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
