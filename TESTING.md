<!-- rule 36: the index's own lines, not the record -->
# Claude Code Bridge — tests and packages

Part of [README.md](README.md), moved out whole under rule 36.

<!-- /rule 36 -->
## Running the tests

Seven suites, no runner, no dependencies. Each is a flat script that exits 1
on failure, and each puts its own state in a temp folder, so none of them
touch anything real. Off Windows that folder holds the
sockets of the tmux servers its windows start too, and a suite stops its
own servers by those sockets and takes the folder with it.

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

It also checks that `bridge.command` can be run where it lands: the archive
entry made on Unix with its execute bit, and the unpacked file executable.
Build the archive with its own recipe, `write_zip`, and unpack it with
`unpack` - Python's `extractall` drops every mode.

