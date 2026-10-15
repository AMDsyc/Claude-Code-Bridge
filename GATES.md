<!-- rule 36: the index's own lines, not the record -->
# Claude Code Bridge — the acceptance gates

Part of [README.md](README.md), moved out whole under rule 36.

<!-- /rule 36 -->
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

