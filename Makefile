# Claude Code Bridge - a familiar entry point where make exists.
#
# Read this before assuming it makes anything portable: it does not. make is
# not installed on Windows by default, and this project is Windows-first -
# the machine it is developed on has no make at all. What this file adds is
# the entry point POSIX users expect, spelled the way they expect it. On
# Windows use bridge.bat and add-project.bat, which need nothing installed.
#
# Every target below is a thin wrapper over a command that already exists and
# is documented in the README. There is no build logic here and there must
# never be any: a second implementation of anything in this file would be a
# second place for the same fact to be wrong.

PY ?= python3

.PHONY: help start install test verify clean

help:
	@echo "make start                 - run the daemon and open the panel"
	@echo "make install PROJECT=path  - install the hooks into a project"
	@echo "make test                  - the five suites and py_compile"
	@echo "make verify ZIP=z UNPACKED=d - sha256 across repo, zip, unpacked"
	@echo "make clean                 - remove __pycache__"
	@echo ""
	@echo "PY=$(PY)  (override with: make PY=python start)"

start:
	$(PY) -m bridgecore.daemon

install:
	@test -n "$(PROJECT)" || (echo "give it a folder: make install PROJECT=/path/to/project"; exit 1)
	$(PY) -m bridgecore.install "$(PROJECT)" --role executor

# Each suite is a flat script that exits non-zero on failure, so a plain
# sequence is the whole runner. There is no test framework to configure.
test:
	$(PY) -m py_compile bridgecore/*.py
	$(PY) test_handover.py
	$(PY) test_archive.py
	$(PY) test_search.py
	$(PY) test_wall_handover.py
	$(PY) test_multipair.py

# Checks a PACKAGE, not a checkout: it compares the sha256 of every listed
# file across the repository, the archive entry and the unpacked copy.
verify:
	@test -n "$(ZIP)" -a -n "$(UNPACKED)" || (echo "usage: make verify ZIP=bridge.zip UNPACKED=/tmp/unpacked"; exit 1)
	$(PY) verify_package.py . "$(ZIP)" "$(UNPACKED)" bytes.txt

clean:
	rm -rf bridgecore/__pycache__ __pycache__
