# What stands in the way of the action

A section of the canon, `HONESTY.md`, moved out whole under rule 36 so
that the canon stays under 15 000 characters: where it stood, the canon
links here. It is delivered to nobody; it is read when it is needed.

Three of these rules live in the bridge, not in this text, and fire at the
moment of the action:

- **A verdict that passes judgement** — `done`, `stop` and `continue` — is
  not taken without a `Checked:` block of paths the bridge opens itself.
  Only `wait` is free: it judges nothing, it says a process is still
  running. A path that is not there is refused by name. A refusal costs the
  report nothing: it stays unanswered, and still cannot be answered twice.
  Work with nothing to open has a way out — `Checked: no artifacts —
  <reason>` — but each use is counted and shown.
- **A report that changed code** is not accepted without
  `Residence: file:function` (rule 26). Where a project names
  the checks its code is accepted by, `done`/`stop` on such a report also
  need a **successful `check` run made after the report**. Bash, PowerShell
  and the edit tools are denied to the planner (Monitor measures; rule 11);
  a run older than the report does not count. A failed check refuses and
  names what failed.
- **A temporary solution** is declared as `Debt: <what is temporary> —
  <what closes it>`; the bridge writes it into `bridge-logs/DEBT.md` and
  counts it. Only `Debt closed: <what> — <what closed it>` puts it out. It
  blocks nothing.

- **A silent planner stops the pair** (rule 27): a person is told, and no
  more reports are made — nobody reads them. A live verdict or a person
  lifts it; the returning planner learns in one line how many reports it
  missed, and where.

Also: a `[FRAMES]` task whose report names no existing image file
reaches the planner headed `NO FRAMES`.
