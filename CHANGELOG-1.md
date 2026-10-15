<!-- rule 36: the index's own lines, not the record -->
# Claude Code Bridge — changes from 2026-09-28 back to the first release

Part of [CHANGELOG.md](CHANGELOG.md), moved out whole under rule 36.

<!-- /rule 36 -->
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

