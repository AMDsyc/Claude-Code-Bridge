#!/bin/sh
# The bridge on a Mac - bridge.bat's twin. Double-click it in Finder (that
# opens Terminal), or run it in Terminal. It checks python3, tmux and claude,
# mounts the PC's network folder by name, and starts the bridge, which opens
# the panel in the browser. `bridge.command --check` only checks: nothing is
# started and no window opens - the form for ssh.
# The steps themselves are bridgecore/maclaunch.py; this file finds a
# python3 to run it with, because without one nothing else can run.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
[ -f source/bridgecore/daemon.py ] && cd source
if [ ! -f bridgecore/daemon.py ]; then
    echo "  The folder layout is wrong: next to this file there must be"
    echo "  bridgecore/daemon.py, or source/bridgecore/daemon.py."
    exit 1
fi
PY=""
for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c \
        'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        >/dev/null 2>&1; then
        PY="$p"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "  Missing: python3 3.9 or newer - install: xcode-select --install"
    exit 1
fi
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
"$PY" -m bridgecore.maclaunch --home "$HERE" "$@"
code=$?
case " $* " in
    *" --check "*) ;;
    *) echo
       echo "  The bridge stopped. If that was not on purpose, the reason is above." ;;
esac
exit $code
