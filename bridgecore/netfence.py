# Claude Code Bridge - a review loop for Claude Code sessions
# Copyright (C) 2026  AMDsyc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Nothing a test run does reaches past this machine - and an attempt to is
written down, so the run can fail on it.

2026-10-15 (DECISIONS 8.142): test_handover's case 17 leaves a token and
a chat id in the config on disk, store.save_config puts them back into the
live config, and from then on every refresh_pin of that suite was a POST to
the real api.telegram.org, with the token "new". Nothing said so: the call
failed, telegram.py never raises, and the run stayed green. A suite that can
reach the network is a defect whether or not the call succeeds.

  fence(record)   refuse every host but this machine, in this process:
                  urllib's opener by the URL's host (a proxy cannot carry
                  it out), name lookups, and socket connects. Each refusal
                  is a line in `record`, and BRIDGE_NETFENCE names that file
                  to every child, so a child daemon that imports the package
                  is fenced from its first import and writes into the same
                  file.
  refused()       every refusal so far, this process's and its children's.
  telegram(delay) a Bot API on 127.0.0.1 that records each call; it sets
                  BRIDGE_TELEGRAM_API, which bridgecore.telegram reads at
                  import, so it is called before that import.

What it does not reach: a program that is not Python - git, curl, ssh,
smbutil - goes round it, and a suite that runs one of those against a remote
has to say so itself. hook.py, statusline.py and channel.py are not fenced
either: run by path they never import the package, and 8.122 keeps them to
the standard library (test_handover case 153); the one address each of them
has is 127.0.0.1, written in it.

It reads nothing from the package, like owntemp: a suite imports it before
the modules that read BRIDGE_DATA and BRIDGE_TELEGRAM_API.
"""

import json
import os
import re
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = "BRIDGE_NETFENCE"
REFUSED = []            # this process's own refusals, newest last
_STATE = {"up": False, "record": None}
_lock = threading.Lock()
_STDLIB = os.path.dirname(os.__file__)


def local(host):
    """Is this host this machine? No name at all is (a bind to every
    interface, a lookup of nothing); so are the loopback names and addresses.
    """
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    h = str(host).strip().strip("[]").lower()
    return (h in ("", "localhost", "::1", "::", "0.0.0.0")
            or h.startswith("127.") or h.endswith(".localhost"))


def _mask(text):
    return re.sub(r"/bot[^/]+/", "/bot***/", str(text))


def _where():
    """The three calls nearest the refusal that are neither the standard
    library nor this file - telegram.py:79 <- daemon.py:25206 <- ..."""
    out = []
    for fr in reversed(traceback.extract_stack()[:-3]):
        f = fr.filename
        if f.startswith(_STDLIB) or os.path.basename(f) == "netfence.py":
            continue
        out.append("%s:%d" % (os.path.basename(f), fr.lineno))
        if len(out) == 3:
            break
    return " <- ".join(out)


def _refuse(what, target):
    entry = {"at": time.strftime("%H:%M:%S"), "pid": os.getpid(),
             "what": what, "target": _mask(target), "where": _where()}
    with _lock:
        REFUSED.append(entry)
        path = _STATE["record"]
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError:
                pass
    return entry


def _why(target):
    return ("netfence: %s refused - nothing in a test run reaches past this "
            "machine" % _mask(target))


def up():
    return _STATE["up"]


def fence(record=None):
    """Refuse every host but this machine from here on, in this process, and
    name `record` to the children. A second call only changes the record."""
    if record:
        _STATE["record"] = record
        os.environ[ENV] = record
    if _STATE["up"]:
        return
    _STATE["up"] = True

    def _lookup(orig, what):
        def look(host, *a, **k):
            if not local(host):
                _refuse(what, host)
                raise socket.gaierror(socket.EAI_NONAME, _why(host))
            return orig(host, *a, **k)
        return look

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr"):
        setattr(socket, name, _lookup(getattr(socket, name), name))

    inet = (socket.AF_INET, getattr(socket, "AF_INET6", socket.AF_INET))

    def _connect(orig, what):
        def conn(self, address, *a, **k):
            if self.family in inet and isinstance(address, tuple) \
                    and not local(address[0]):
                _refuse(what, "%s:%s" % address[:2])
                raise ConnectionRefusedError(_why(address[0]))
            return orig(self, address, *a, **k)
        return conn

    socket.socket.connect = _connect(socket.socket.connect, "connect")
    socket.socket.connect_ex = _connect(socket.socket.connect_ex,
                                        "connect_ex")

    o_open = urllib.request.OpenerDirector.open

    def _open(self, fullurl, *a, **k):
        url = getattr(fullurl, "full_url", fullurl)
        parts = urllib.parse.urlsplit(str(url))
        if parts.scheme in ("http", "https", "ftp") \
                and not local(parts.hostname):
            _refuse("urlopen", "%s %s" % (
                getattr(fullurl, "get_method", lambda: "GET")(), url))
            raise urllib.error.URLError(_why(parts.hostname))
        return o_open(self, fullurl, *a, **k)

    urllib.request.OpenerDirector.open = _open


def refused():
    """Every refusal so far: the record's lines when there is a record -
    this process's and its children's, written as they happened - and this
    process's own list when there is none."""
    path = _STATE["record"]
    if not path:
        return list(REFUSED)
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []


def forget(entries):
    """Drop exactly these refusals - the ones a case provoked on purpose and
    has checked - from the list and the record. Nothing else is touched, so a
    refusal another thread made in the meantime still fails the run."""
    gone = [json.dumps(e, sort_keys=True) for e in entries]
    with _lock:
        REFUSED[:] = [e for e in REFUSED
                      if json.dumps(e, sort_keys=True) not in gone]
        path = _STATE["record"]
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                keep = [ln for ln in fh if ln.strip() and json.dumps(
                    json.loads(ln), sort_keys=True) not in gone]
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(keep)


class _Bot(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _answer(self):
        tg = self.server.tg
        method = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            payload = {}
        with tg.lock:
            tg.calls.append((method, payload))   # on arrival, before the wait
            tg.ids += 1
            mid = tg.ids
        if tg.delay:
            time.sleep(tg.delay)
        result = ([] if method == "getUpdates" else
                  {} if method == "getChat" else {"message_id": mid})
        body = json.dumps({"ok": True, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer


class Telegram:
    """A Bot API that answers ok to everything: getUpdates with nothing,
    getChat with nothing pinned, the rest with a fresh message_id. `delay`
    holds each answer that long - a Telegram that is slow, not down."""

    def __init__(self, delay=0.0):
        self.calls, self.delay, self.ids = [], delay, 1000
        self.lock = threading.Lock()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Bot)
        self.srv.daemon_threads = True
        self.srv.tg = self
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def methods(self):
        with self.lock:
            return [m for m, _ in self.calls]

    def reset(self):
        with self.lock:
            del self.calls[:]


def telegram(delay=0.0):
    """A recording Bot API on this machine, and BRIDGE_TELEGRAM_API pointing
    at it - for this process's later import of bridgecore.telegram and for
    every child. Already imported, telegram.API has to be pointed by hand:
    API_BASE is read once, at import."""
    tg = Telegram(delay)
    os.environ["BRIDGE_TELEGRAM_API"] = tg.base
    return tg
