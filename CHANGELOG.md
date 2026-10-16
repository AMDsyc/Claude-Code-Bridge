<!-- rule 36: the index's own lines, not the record -->
# Claude Code Bridge — changes before the newest release

Part of [README.md](README.md), moved out whole under rule 36.

**How entries move.** The README keeps only the newest release's entry and
stays at or under 27,000 characters, so that the next entry fits without a
cut. When a release comes, the README's previous entry moves here whole, at
the top. When this file would pass 30,000 characters, its oldest entries
move whole to the top of [CHANGELOG-1.md](CHANGELOG-1.md); when that one
would, its oldest go to a new `CHANGELOG-2.md`, linked at the end of the
file before it and listed in `make_public.py`'s `EXTRA`, or it is never
published.

<!-- /rule 36 -->
**2026-10-16 — what is new since the morning release of 2026-10-15 (67112c0)**

- **A Mac mounts the PC's share without a password dialog.**
  `bridge.command` asks the keychain first and opens the share by name
  or by a proven address, whichever holds the password; with none it
  says how to mount it once and starts anyway. A share mounted by address
  counts as its server's, and one that drops while the bridge runs is
  mounted back.
- **needs_you finds a person**: the planner of `home_pair`, the pair you
  talk to, hears every call of another pair the chat did not carry; a
  call about a planner never goes into that planner's own channel; an
  optional Mac notification (`mac_notify`), with no sound. A call that
  closes within 45 s goes no further than the chat and the panel.
- **A turn running over 45 minutes is told to its planner**, as news.
- **Nothing waits on the network at the wrong moment**: a hook's reply
  no longer waits on Telegram, a restart calls back only once the bridge
  answers, and moved history is read on its own thread.
- **A project on a network volume is not committed unless asked**
  (`commit_on_share`), and a commit with no author set goes as
  `bridge <bridge@invalid>`. `/config` says what it changed.
- **A helper's compaction is not the window's**, and a routine
  compaction does not lower the ceiling.
- **A job handed to a new window at a swap is not ended by that
  window's Stop**, so a `wait` calls nobody while the old window's run
  goes on.
- **No wall replacement for a pair nobody works**: with the loop off or
  paused it is asked again at the next Stop with the loop on.

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

**2026-10-13 — what is new since the release of midday 2026-10-12 (6fe3d8c)**

- **The bridge runs on a Mac.** `bridge.command` beside `bridge.bat` starts
  it: it names what is missing - python3, tmux, Claude Code - with its
  install command, mounts the PC's network folder by name
  (`smb://<account>@<PC>.local/<share>`, the password left to the
  keychain), and starts the bridge, which keeps the Mac awake while it
  runs. Each window is a tmux session of its own, shown in a Terminal
  window; closing that window leaves the session running, and *show* on
  its row opens it again. A live loop with a real Claude Code has run
  there. [MAC.md](MAC.md) is the quick start, how to carry a pair's work
  over from another machine and back, and an honest list of what is not
  done on a Mac yet.
- **It restarts itself there through the same gate.** `python3 -m
  bridgecore.relayout --now` refuses, naming the pair, while any pair is
  busy; otherwise it stops the bridge on SIGTERM and starts it again in a
  Terminal window, as a double-click does.
- **One bridge per folder, across machines.** A bridge holds
  `<project>/bridge-logs/daemon-lock.json` while it works the folder, gives
  it back within 10 seconds when the project's loop is turned off or
  paused, and takes over one whose holder's heartbeat has been still for
  150 seconds. Adding a project in the panel takes the lock before it
  writes anything, so a folder another machine works is refused, the
  holder named, and left untouched.
- **Windows sign in as the client does.** By default every window and
  every `claude -p` the bridge runs uses the client's own sign-in. A token
  file, `~/.bridge-claude-token`, is read only with `"use_token_file":
  true` in the config - for a machine where every session should be that
  one account.
- **A stop off Windows ends the window's own process group, then kills
  what is left.** A window that does not lead its group is stopped alone,
  so no other process is signalled with it.
- **The bridge's own project is known by its tree, not by where the
  daemon runs.** A bridge started from an installed copy elsewhere still
  runs the suites for its own project, on that project's files. A check
  that ran nothing says *nothing to run* - never *passed*.
- **An iteration commit keeps line endings.** Where no `core.autocrlf` is
  set at any level, the commit adds with `input`, so a Mac committing in a
  folder a Windows checkout made does not rewrite its files.
- **`bridge.command` is runnable where it lands.** The repository stores
  it executable; a package zip built with `verify_package.py`'s recipe is
  written as made on Unix with the bit, and the bit is checked in the zip
  and in the unpacked copy.
- **The public scan refuses more.** Any address in a home or office
  network, beside this machine's own names, accounts, addresses and keys;
  each class has a failing case of its own in the suite.
- **The suites leave no tmux sockets behind.** Each points `TMUX_TMPDIR`
  at a folder of its own, stops its servers there by path, and removes
  the folder.

**2026-10-12 — what is new since the release just after midnight (a74b6bb)**

- **The canon is shorter, and its rules are the same.** `HONESTY.md` went
  from 14,983 to 13,156 characters, under the 15,000 it is held to. The
  section on the bridge's gates at the moment of an action moved whole into
  `HONESTY_GATES.md` beside it, and rule 29's short account of how a window
  is kept out of sight into `QUIET.md`, word for word. All thirty-six rules
  keep their norm and their check. The text changed, so each session is
  sent the full canon once more.
- **Rule 36 has one exception: an archive.** A file that is searched and
  never read whole - a register, raw records - says so on its first line:
  `<!-- rule 36: archive -->`. The bridge then neither measures it nor
  follows its links, and counts it by name under "Archives" in its walk's
  journal line, so the exception is never silent. The mark anywhere but on
  the first line does nothing, and CLAUDE.md is never an archive. Beside
  the link to an archive in CLAUDE.md, write "an archive: search it, never
  read it whole". A living document that is read whole is cut as before.
- **The changelog is cut the same way.** Its entries from 2026-09-28
  back to the first release moved whole into `CHANGELOG-1.md`, linked from
  the end of `CHANGELOG.md`.

**2026-10-11, evening — what is new since the morning release of 2026-10-11 (6a886ad)**

- **Rule 36: a project's CLAUDE.md and everything it links stay within
  30,000 characters.** The canon has thirty-six rules now (`HONESTY.md`,
  `HONESTY.en.md`). A block that has grown moves out whole into a file of
  its own, leaving a two- or three-line anchor with the link where it
  stood, and that file lives by the same rule.
- **And the bridge measures it.** Every watched project is walked from its
  CLAUDE.md through the Markdown files it names, inside the project, each
  once, in PowerShell's measure (UTF-16 units). A file over the limit is a
  warning in the journal, once per file and size, and one line to the
  pair's reviewing half with its next delivery when a file crosses the
  limit; the gate names and does not refuse. What CLAUDE.md names itself
  is measured wherever in the project it lies; below that the walk does
  not go into `bridge-logs`, `test-results` or a file with a date in its
  name - history written once does not grow - and what it passed over is
  counted by kind in one journal line. Files the bridge writes are neither
  measured nor walked.
- **The thread handed back after a compaction is cut only where it is
  handed back, and says so.** The last task and the last verdict were
  stored already cut to the hand-back limit, so a verdict longer than
  2,000 characters - about one in nine - came back after a compaction cut
  mid-sentence, with no mark. Both are now kept whole (up to 20,000
  characters) with their length, and the cut is made, marked and counted
  when the thread is handed back.
- **A compaction that lands at the wall says so.** When a compaction brings
  the working half to the wall it is handed over at, the thread handed back
  opens with a line telling it to finish the step, update its state file
  and end the turn - the handover happens at that stop - and the journal
  says so. The request for a handoff names the reason the handover was
  decided on, instead of a fixed "wall of five compactions".
- **A window's remote-control link comes only from the client's own record
  of it.** The newest session link anywhere in a transcript used to be
  taken, so a file read or a command output quoting another window's link
  could put the wrong window - even another pair's - in the pinned list.
  At its start the bridge checks every saved link again by that rule: one
  the window's own record confirms stays, another one found there replaces
  it, and one nothing confirms is taken off, each with a journal line; the
  pin and the panel are brought in line at once, and when no link is left
  the pin says so instead of keeping the old list.
- **Every window is named "<Project> <Role> NNNN".** The window and its
  remote-control session get one name: the project's name, the role, and a
  number per half of each pair that grows with each new session and is
  kept by a resume - four digits at least, then as many as needed. A
  handover's replacement is titled the same way; the opening line, the pin
  and the panel name it too; a name you give in answer to the handover's
  question still wins.
- **Small fixes.** The mark on a clipped tail says how much was really cut.
  A file whose name says "index" is never taken for a project's handoff. A
  resumed window names its model in the journal, on its command line and
  in the window record, as every other launch does.

**2026-10-11 — what is new since the release of 2026-09-30 (e8d0d7c)**

If you are updating: reload each panel tab that was open before the
update, once. A tab loaded by an earlier release does not send the
secret its requests now need, and cannot learn to by itself.

- **Each half gets its own compaction window, without anybody editing the
  project's settings.** The working half starts on a 222,000-token
  auto-compact window, the reviewing half on 1,000,000: the bridge hands
  each window its own `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, which the client
  reads before any settings file. The shared `.claude/settings.json` is not
  touched, and a window's record now says which window it was actually
  given and where that number came from - before, it repeated whatever the
  settings file said at the moment of the launch. With the bridge's 90 %
  the working half compacts at about 182k.
- **Opus first, for both halves.** The default chain of the reviewing half
  starts with Opus (the previous model stays behind it as the fallback),
  and the working half starts with `xhigh` effort beside the reviewer's
  `max`. At its first start the bridge applies this once to what a config
  already says - a chain headed by another model gets Opus at its head, a
  missing effort or window is filled in - names what it moved, and leaves
  any choice made after that alone.
- **The reviewing half is not compacted while it waits.** The client now
  compacts a long conversation that has sat idle, and the reviewing half
  sits idle between reports most of its life: on two reviewers it did so
  nine times in two days, at 203k-737k, each time throwing away context the
  million-token window was there to keep. Its window starts with the
  client's own switch for this turned off; the working half's is left as
  the client has it.
- **A half is replaced at its own wall, counted in compactions.** The
  working half is handed over two compactions before the climb of its own
  floors says the next one would not fit, and never later than its
  fifteenth; the reviewing half at its fifth. A compaction the client did
  while a window sat idle is not counted and is not taken as a size.
- **After a compaction the working half gets its thread back first** - its
  last task, the last verdict and the end of its own state file, word for
  word - and after its first delivery it is sent only the closing lines of
  the rules instead of their titles every time.
- **Measurements are kept per window.** A model's compaction samples on a
  222k window and on a 1M one no longer mix; samples already on disk are
  moved by the launch record of the session that made them.
- **The reviewer's `check` no longer reports "no run" about a run that
  passed**: the run's record is written before its seat is given up.
- **A window the bridge itself replaced is not called a stranger.** A
  record of a refused channel now vouches only for a window older than
  itself, so the bridge's own leftover is recognised and closed instead of
  a person being called about it.
- **A window stopped or raised from the panel says so in the journal, and
  why** - a restart is no longer recorded as a death.
- **A session the bridge took down at its wall is not raised again** by the
  recovery of a dead half; before, one session could be handed over three
  times in under an hour.
- **A lost write of `state.json` names the process holding the file.**
- **One pair's window cannot stop or raise another pair's windows.** A
  `/session` request is traced to the window it came from by its own TCP
  connection, and refused across projects, naming both.
- **Every POST but the two the hooks use asks for the shared secret**, and
  the panel sends it on every request; a tab refused for a missing secret
  reloads itself, at most once a minute.
- **The bridge answers only to its own address.** A request whose Host is
  not 127.0.0.1, localhost or ::1 on the bridge's port is refused before
  any route, and so is a POST from another page's Origin. This closes DNS
  rebinding: a page under a name of its own, rebound to 127.0.0.1, could
  otherwise read the panel - and with it the secret.

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

<!-- rule 36: the index's own lines, not the record -->
Older entries, newest first, from 2026-09-28 back to the first release of
2026-08-18: [CHANGELOG-1.md](CHANGELOG-1.md). They moved there whole, so that
this file stays under 30,000 characters (rule 36).

<!-- /rule 36 -->
