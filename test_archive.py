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

"""Regression suite for the archive map.

The map's whole value is that it can be trusted: a role in it came from a
record, a token figure came from named fields, and a rebuild that changes
nothing changes nothing. These cases are built from synthetic transcripts
whose right answers are known by construction - no live daemon, no network,
nothing outside a temp folder.

Run:  python test_archive.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# first: it reads nothing from the package, and the folder it makes
# is the only one this run may remove (DECISIONS.md 8.43, 8.45)
from bridgecore import owntemp                 # noqa: E402
TMP = owntemp.make("bridge-archive-test-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
# The client's own config is isolated too: install() marks a project trusted
# there, and without this a suite would merge its throwaway temp projects into
# the real ~/.claude.json on this machine.
os.environ["BRIDGE_CLAUDE_JSON"] = os.path.join(TMP, ".claude.json")
# and the user-level settings approve_channel merges into - never the
# real one (DECISIONS.md 8.35)
os.environ["BRIDGE_CLAUDE_SETTINGS"] = os.path.join(TMP, "user-settings.json")
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgecore import archive                        # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %-4s %s\n       got %r, want %r" % ("ok" if ok else "FAIL",
                                                 name, got, want))
    if not ok:
        FAILED.append(name)


PROJ = os.path.join(TMP, "proj")
DAY = "2026-08-01"


def day_dir(kind="raw"):
    d = os.path.join(PROJ, "bridge-logs", DAY, kind)
    os.makedirs(d, exist_ok=True)
    return d


def write(rows, sid, kind="raw", name=None):
    path = os.path.join(day_dir(kind), name or ("%s.jsonl" % sid))
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def assistant(at, usage=None, model="claude-opus-5"):
    return {"type": "assistant", "timestamp": at, "cwd": r"C:\path\to\project",
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "done"}],
                        "usage": usage or {"input_tokens": 1,
                                           "cache_read_input_tokens": 10,
                                           "output_tokens": 5}}}


def user(at, content):
    return {"type": "user", "timestamp": at,
            "message": {"role": "user", "content": content}}


# The shape the client really writes: the same tokens appear three times -
# once by name, once inside a cache_creation breakdown, once again under
# iterations. Summing every key containing "token" counts them all.
NESTED_USAGE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 249,
    "cache_read_input_tokens": 169765,
    "output_tokens": 164,
    "cache_creation": {"ephemeral_1h_input_tokens": 249,
                       "ephemeral_5m_input_tokens": 0},
    "iterations": [{"input_tokens": 2, "output_tokens": 164,
                    "cache_read_input_tokens": 169765,
                    "cache_creation_input_tokens": 249}],
}

print("\n1. a session id the bridge has a record of is attributed from it")
write([
    user("2026-08-01T10:00:00Z", "<command-message>init</command-message>"),
    user("2026-08-01T10:00:01Z", "Build the thing, and do not touch the rest."),
    assistant("2026-08-01T10:00:02Z"),
    assistant("2026-08-01T10:05:00Z", NESTED_USAGE),
], "known-sid")
known = {"known-sid": {"role": "executor", "project": "Thing",
                       "how": "bridge launched it as the executor"}}
m = archive.build(PROJ, known)
f = m["files"][0]
check("role from the record", f["role"], "executor")
check("project from the record", f["project"], "Thing")
check("and it says where that came from",
      "bridge launched it" in f["attribution"], True)
check("turns are the assistant messages", f["turns"], 2)
check("first timestamp", f["first_at"], "2026-08-01T10:00:00Z")
check("last timestamp", f["last_at"], "2026-08-01T10:05:00Z")

print("\n2. the carried figure is the named input fields and nothing else")
check("input + cache_creation + cache_read, by name",
      f["carried_tokens"], 2 + 249 + 169765)
check("the fields it was built from are named",
      f["token_fields"], ["input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens"])
check("output is not in it", f["carried_tokens"] == 2 + 249 + 169765 + 164,
      False)
print("   the nested breakdown and the iterations are not counted again")
wildcard = sum(v for k, v in NESTED_USAGE.items()
               if isinstance(v, int) and "token" in k)
check("a wildcard sum would have been larger",
      f["carried_tokens"] < wildcard + 1, True)
check("carried_tokens on a usage block with no named field",
      archive.carried_tokens({"output_tokens": 9}), (None, []))

print("\n3. the first thing a human typed is the excerpt")
check("the slash-command wrapper is not it",
      f["excerpt"], "Build the thing, and do not touch the rest.")

print("\n4. an unknown session id is marked unknown, never guessed")
write([
    user("2026-08-01T11:00:00Z", "who am I"),
    assistant("2026-08-01T11:00:01Z"),
], "stranger-sid")
m = archive.build(PROJ, known)
byfile = {x["session_id"]: x for x in m["files"]}
check("two files now", len(m["files"]), 2)
check("the stranger has no role", byfile["stranger-sid"]["role"], "unknown")
check("and no project", byfile["stranger-sid"]["project"], "unknown")
check("and it says why", "no record" in byfile["stranger-sid"]["attribution"],
      True)
print("   being the newest file, or next to a known one, attributes nothing")
check("the known one is still the known one",
      byfile["known-sid"]["role"], "executor")
check("the map counts what it could not attribute",
      m["totals"]["unknown_files"], 1)
print("   the transcript's own cwd is recorded as a fact, not promoted")
check("cwd is kept", byfile["known-sid"]["cwd_recorded"], r"C:\path\to\project")
check("but a stranger with a cwd is still unknown",
      byfile["stranger-sid"]["project"], "unknown")

print("\n5. compactions are read from the client's own boundary rows")
write([
    user("2026-08-01T12:00:00Z", "keep going"),
    assistant("2026-08-01T12:00:01Z"),
    {"type": "system", "subtype": "compact_boundary",
     "timestamp": "2026-08-01T12:30:00Z", "content": "Conversation compacted",
     "compactMetadata": {"trigger": "auto", "preTokens": 1000742,
                         "postTokens": 15577}},
    assistant("2026-08-01T12:31:00Z"),
], "compacted-sid")
known["compacted-sid"] = {"role": "planner", "project": "Thing",
                          "how": "bridge launched it as the planner"}
m = archive.build(PROJ, known)
c = {x["session_id"]: x for x in m["files"]}["compacted-sid"]
check("one compaction seen", len(c["compactions"]), 1)
check("with the size it went in at", c["compactions"][0]["pre_tokens"], 1000742)
check("and the floor it came out on", c["compactions"][0]["post_tokens"], 15577)
check("and what triggered it", c["compactions"][0]["trigger"], "auto")

print("\n6. iteration numbers are found in the reports themselves")
write([
    user("2026-08-01T13:00:00Z",
         "<channel source=\"bridge\" kind=\"report\" report=\"7\">"),
    user("2026-08-01T13:00:01Z", "Executor report 8:\n\nI did the thing."),
    user("2026-08-01T13:00:02Z", "Executor report 9:\n\nAnd the next."),
    assistant("2026-08-01T13:00:03Z"),
], "reports-sid")
m = archive.build(PROJ, known)
r = {x["session_id"]: x for x in m["files"]}["reports-sid"]
check("every number, sorted", r["iterations"], [7, 8, 9])
check("a channel event is never the excerpt",
      r["excerpt"].startswith("<channel"), False)
check("the first row that is not one is",
      r["excerpt"], "Executor report 8: I did the thing.")
check("runs of numbers read as a range", archive._rng([7, 8, 9]), "7-9")
check("and gaps do not", archive._rng([1, 2, 5]), "1-2, 5")

print("\n6a. an executor is never spoken to by a human, so the task the")
print("    bridge handed it is the excerpt - marked as what it is")
write([
    {"type": "user", "timestamp": "2026-08-01T13:30:00Z", "isMeta": True,
     "message": {"role": "user", "content":
                 '<channel source="bridge" kind="task">\nTask from the '
                 'planner:\n\nFix the wall arithmetic.\n</channel>'}},
    assistant("2026-08-01T13:30:01Z"),
], "worker-sid")
known["worker-sid"] = {"role": "executor", "project": "Thing",
                       "how": "bridge launched it as the executor"}
m = archive.build(PROJ, known)
w = {x["session_id"]: x for x in m["files"]}["worker-sid"]
check("the task is quoted", "Fix the wall arithmetic." in w["excerpt"], True)
check("the channel envelope is not", w["excerpt"].startswith("<channel"),
      False)
check("and the source is named, not passed off as a human",
      w["excerpt_source"], "bridge task")
print("    a human's own words still outrank a relayed task")
check("the planner's excerpt is still the human one",
      {x["session_id"]: x for x in m["files"]}["known-sid"]["excerpt_source"],
      "human")

print("\n7. a snapshot is the same session, taken at a moment")
write([user("2026-08-01T14:00:00Z", "x"), assistant("2026-08-01T14:00:01Z")],
      "known-sid", kind="snapshots", name="known-sid-143000.jsonl")
m = archive.build(PROJ, known)
snaps = [x for x in m["files"] if x["kind"] == "snapshot"]
check("one snapshot", len(snaps), 1)
_snap0 = snaps[0] if snaps else {}
check("attributed to the same session", _snap0.get("session_id"), "known-sid")
check("the time it was taken is kept", _snap0.get("taken_at"), "143000")
check("and it inherits the role", _snap0.get("role"), "executor")
print("   the session row gathers its files")
sess = {s["session_id"]: s for s in m["sessions"]}["known-sid"]
check("two files for that session", sess["files"], 2)

print("\n8. the same archive renders the same MAP.md")
p = os.path.join(PROJ, "bridge-logs", "MAP.md")
first = open(p, encoding="utf-8").read()
archive.build(PROJ, known)
second = open(p, encoding="utf-8").read()
# by digest: a mismatch here is a diff nobody wants printed twice in full
check("byte for byte", hashlib.sha1(second.encode()).hexdigest(),
      hashlib.sha1(first.encode()).hexdigest())
check("and it is not empty", len(first) > 400, True)
check("no clock in it", "generated" in first.lower(), False)
check("but map.json does carry one",
      bool(json.load(open(os.path.join(PROJ, "bridge-logs", "map.json"),
                          encoding="utf-8")).get("generated")), True)
print("   and per-date index.json sits with the files it describes")
idx = json.load(open(os.path.join(PROJ, "bridge-logs", DAY, "index.json"),
                     encoding="utf-8"))
check("the day is indexed", idx["date"], DAY)
check("with every file of that day", idx["count"], len(m["files"]))

print("\n9. a rebuild never runs twice at once, and never loses a request")
seen = []
# THE FIRST BUILD IS HELD UNTIL THE SECOND CALL HAS BEEN MADE - the fact
# this check is about, owned rather than assumed. A small fixture builds in
# milliseconds, and a build that has finished rightly lets the next call
# start its own: on 2026-09-28, under seven suites at once, the worker ran
# to the end before this thread made its second call, and the check went
# red on correct code (DECISIONS 8.48)
import threading as _th9                                  # noqa: E402
_gate9 = _th9.Event()
_build9 = archive.build_now


def _held9(project_dir, known=None):
    _gate9.wait(30)
    return _build9(project_dir, known)


archive.build_now = _held9
try:
    t = archive.rebuild_async(PROJ, known, done=lambda mm: seen.append(mm))
    second_call = archive.rebuild_async(PROJ, known)
finally:
    archive.build_now = _build9
    _gate9.set()
if t:
    t.join(30)
check("the second call did not start its own build", second_call, None)
check("the first one finished", len(seen), 1)
check("and it produced the map",
      ((seen[0].get("totals") or {}).get("files") if seen else None),
      len(m["files"]))
check("which is remembered for the next reader",
      archive.last_map(PROJ)["totals"]["files"], len(m["files"]))

print("\n10. a damaged line costs that line, not the file")
path = write([user("2026-08-01T15:00:00Z", "good"),
              assistant("2026-08-01T15:00:01Z")], "torn-sid")
with open(path, "a", encoding="utf-8") as fh:
    fh.write('{"type": "assistant", "message": {truncated\n')
rec = archive.scan_file(path, known)
check("the good rows are still read", rec["turns"], 1)
check("and the excerpt survives", rec["excerpt"], "good")

print("\n11. an empty archive says so rather than failing")
empty = os.path.join(TMP, "empty-proj")
os.makedirs(os.path.join(empty, "bridge-logs"), exist_ok=True)
m2 = archive.build(empty, known)
check("no files", m2["totals"]["files"], 0)
check("and the map says it plainly",
      "Nothing archived yet." in archive.render_md(m2), True)

print("\n12. history carried from another machine, and the map after it")
print("    ANALYSIS-portable-history.md step 4. A folder brought here from")
print("    another computer carries raw/ files whose session ids this bridge")
print("    never launched. known_sessions() is built from the bridge's own")
print("    bookkeeping, so those ids are in none of it - and the invariant")
print("    (SS1.5) says such a file is mapped UNKNOWN and never attributed")
print("    from the folder it sits in or the file beside it")
CARRIED_SID = "carried-from-another-machine"
write([
    user("2026-08-01T14:00:00Z", "work done on the old machine"),
    assistant("2026-08-01T14:00:01Z"),
], CARRIED_SID)
_known12 = {"known-sid": {"role": "executor", "project": "proj",
                          "how": "bridge launched it as the executor"}}
m12 = archive.build(PROJ, _known12)
_c12 = [f for f in m12["files"] if f["session_id"] == CARRIED_SID]
check("the carried file is in the map", len(_c12), 1)
_c120 = _c12[0] if _c12 else {}
check("with no role guessed for it", _c120.get("role"), "unknown")
check("and no project either", _c120.get("project"), "unknown")
check("it is counted as unknown rather than hidden",
      m12["totals"]["unknown_files"] >= 1, True)
check("and MAP.md says so in words rather than quietly",
      "listed as unknown rather than guessed at" in archive.render_md(m12),
      True)

print("   THE SABOTAGE (rule 19): give the bridge a record of that id and")
print("   the same file must stop being unknown - the invariant is about")
print("   having no record, not about the file")
_known12b = dict(_known12)
_known12b[CARRIED_SID] = {"role": "planner", "project": "elsewhere",
                          "how": "last recorded planner of that project"}
m12b = archive.build(PROJ, _known12b)
_c12b = [f for f in m12b["files"]
         if f["session_id"] == CARRIED_SID]
check("now it is attributed", _c12b[0]["role"], "planner")
check("from the record and from nothing else", _c12b[0]["project"],
      "elsewhere")

print("   the first rebuild after an import changes MAP.md legitimately -")
print("   there are files in it that were not there before. The SECOND must")
print("   be a no-op on disk: MAP.md carries no clock precisely so that a")
print("   rebuild nobody asked for does not rewrite a file every time")
_map12 = os.path.join(archive.logs_root(PROJ), "MAP.md")
_h12a = hashlib.sha256(open(_map12, "rb").read()).hexdigest()
archive.build(PROJ, _known12b)
_h12b = hashlib.sha256(open(_map12, "rb").read()).hexdigest()
check("a repeated rebuild leaves MAP.md byte-identical", _h12a, _h12b)
print("   map.json is a DIFFERENT promise and does not make it: it carries")
print("   a `generated` stamp, so it is rewritten every build. That is the")
print("   whole reason the no-clock rule is about MAP.md - the file a")
print("   person reads and a repository would show as changed")
_map_json12 = archive.last_map(PROJ)
check("map.json does carry a clock", bool(_map_json12.get("generated")),
      True)
check("and MAP.md does not carry that stamp",
      _map_json12["generated"] in open(_map12, encoding="utf-8").read(),
      False)

print("   and the import really did move the file - the earlier build had")
print("   fewer, so the first rebuild was not a no-op and the check above")
print("   is not passing on an empty change")
check("the carried file added to the count",
      m12["totals"]["files"] > 0 and len(_c12) == 1, True)

print("\n13. a pass reads only what changed (DECISIONS.md 8.133)")
print("    Until 2026-10-14 every pass opened every archived file of every")
print("    day, and a pass follows every copy the bridge makes. On a share")
print("    that was 46.26 GB for one project, again and again. The reads are")
print("    counted at the one door they go through - the module's `open` -")
print("    so the case asks what was OPENED, not what the map says it did")
import builtins as _bi13                                  # noqa: E402
PROJ13 = os.path.join(TMP, "proj13")
_opens13 = []


def _open13(file, mode="r", *a, **kw):
    _opens13.append((os.path.basename(str(file)), mode))
    return _bi13.open(file, mode, *a, **kw)


def _read13():
    """The archived files the last build opened to read, and a reset."""
    got = sorted(n for n, md in _opens13
                 if n.endswith(".jsonl") and not set(md) & set("wax+"))
    del _opens13[:]
    return got


def _wrote13(name):
    return [n for n, md in _opens13 if n == name and set(md) & set("wax+")]


def _w13(day, sid, rows, kind="raw"):
    d = os.path.join(PROJ13, "bridge-logs", day, kind)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.jsonl" % sid)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _pass13(m):
    lp = m.get("last_pass") or {}
    return (lp.get("read_files"), lp.get("read_bytes"), lp.get("kept_files"))


D13A, D13B = "2026-08-02", "2026-08-03"
_p13 = {
    "a13": _w13(D13A, "a13", [user("2026-08-02T10:00:00Z", "first a"),
                              assistant("2026-08-02T10:00:01Z")]),
    "b13": _w13(D13A, "b13", [user("2026-08-02T11:00:00Z", "first b"),
                              assistant("2026-08-02T11:00:01Z")]),
    "c13": _w13(D13A, "c13", [user("2026-08-02T12:00:00Z", "first c"),
                              assistant("2026-08-02T12:00:01Z")]),
    "a13-120000": _w13(D13A, "a13-120000",
                       [user("2026-08-02T10:00:00Z", "first a"),
                        assistant("2026-08-02T10:00:01Z")], "snapshots"),
    "d13": _w13(D13B, "d13", [user("2026-08-03T09:00:00Z", "first d"),
                              assistant("2026-08-03T09:00:01Z")]),
}
_all13 = sorted("%s.jsonl" % k for k in _p13)
_size13 = sum(os.path.getsize(p) for p in _p13.values())
_k13 = {"a13": {"role": "executor", "project": "proj13",
                "how": "bridge launched it as the executor"}}
archive.open = _open13
try:
    print("   THE CONTROL first: the first pass opens every file, so the")
    print("   counter at the door is seen to count")
    m13a = archive.build(PROJ13, _k13)
    check("the first pass opened all five", _read13(), _all13)
    check("and says so: five read, their bytes, none kept", _pass13(m13a),
          (5, _size13, 0))

    print("   a second pass over an archive that did not change opens nothing")
    _md13 = os.path.join(archive.logs_root(PROJ13), "MAP.md")
    _h13 = hashlib.sha256(open(_md13, "rb").read()).hexdigest()
    m13b = archive.build(PROJ13, _k13)
    check("no archived file was opened", _read13(), [])
    check("and the map says it: none read, 0 bytes, five kept",
          _pass13(m13b), (0, 0, 5))
    check("yet the map is the same map", m13b["files"] == m13a["files"],
          True)
    check("and so are its totals", m13b["totals"], m13a["totals"])
    check("MAP.md is byte-identical",
          hashlib.sha256(open(_md13, "rb").read()).hexdigest(), _h13)
    print("   (the day's index.json was not rewritten either)")
    m13b2 = archive.build(PROJ13, _k13)
    check("nothing opened index.json to write", _wrote13("index.json"), [])
    _read13()

    print("   a row appended to one file: that file is read, and only it")
    with open(_p13["b13"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(assistant("2026-08-02T11:05:00Z")) + "\n")
    m13c = archive.build(PROJ13, _k13)
    check("only b13 was opened", _read13(), ["b13.jsonl"])
    check("one read, four kept", (_pass13(m13c)[0], _pass13(m13c)[2]),
          (1, 4))
    _b13 = [f for f in m13c["files"] if f["file"] == "b13.jsonl"][0]
    check("and the new turn is in the map", _b13["turns"], 2)
    check("the bytes read are that file's size", _pass13(m13c)[1],
          os.path.getsize(_p13["b13"]))

    print("   THE SIZE ALONE IS NOT THE KEY: the same number of bytes with")
    print("   other words in them and a newer time is a file to read again")
    _st13 = os.stat(_p13["c13"])
    _t13 = open(_p13["c13"], encoding="utf-8").read()
    with open(_p13["c13"], "w", encoding="utf-8") as fh:
        fh.write(_t13.replace("first c", "FIRST C"))
    os.utime(_p13["c13"], ns=(_st13.st_atime_ns,
                              _st13.st_mtime_ns + 2000000000))
    check("the size did not change", os.path.getsize(_p13["c13"]),
          _st13.st_size)
    m13d = archive.build(PROJ13, _k13)
    check("c13 was read again", _read13(), ["c13.jsonl"])
    _c13 = [f for f in m13d["files"] if f["file"] == "c13.jsonl"][0]
    check("and the map has its new words", _c13["excerpt"], "FIRST C")
    print("   and the bound of the rule, written down rather than hidden:")
    print("   the same size AND the same time is the same file. The client")
    print("   only ever appends to a transcript, which moves both")
    with open(_p13["c13"], "w", encoding="utf-8") as fh:
        fh.write(_t13.replace("first c", "lower c"))
    _st13d = os.stat(_p13["c13"])
    os.utime(_p13["c13"], ns=(_st13d.st_atime_ns,
                              _c13.get("mtime_ns") or _st13d.st_mtime_ns))
    m13d2 = archive.build(PROJ13, _k13)
    check("a file with the recorded size and time is kept", _read13(), [])

    print("   a session the bridge learns of later stops being unknown, and")
    print("   its file is not read to learn it")
    _k13e = dict(_k13)
    _k13e["d13"] = {"role": "planner", "project": "proj13",
                    "how": "last recorded planner of that project"}
    m13e = archive.build(PROJ13, _k13e)
    check("nothing was opened", _read13(), [])
    _d13 = [f for f in m13e["files"] if f["file"] == "d13.jsonl"][0]
    check("and d13 is attributed from the record",
          (_d13["role"], _d13["attribution"]),
          ("planner", "last recorded planner of that project"))
    m13e2 = archive.build(PROJ13, _k13)
    _d13 = [f for f in m13e2["files"] if f["file"] == "d13.jsonl"][0]
    check("and a record that is gone takes it back to unknown",
          (_d13["role"], _d13["attribution"]),
          ("unknown", "no record of this session id"))
    _read13()

    print("   a new file: it is read, and only it")
    _p13["e13"] = _w13(D13B, "e13", [user("2026-08-03T10:00:00Z", "first e"),
                                     assistant("2026-08-03T10:00:01Z")])
    m13g = archive.build(PROJ13, _k13)
    check("only e13 was opened", _read13(), ["e13.jsonl"])

    print("   an empty file is never kept: it is asked again every pass")
    _p13["z13"] = _w13(D13B, "z13", [])
    archive.build(PROJ13, _k13)
    check("the empty file was read", _read13(), ["z13.jsonl"])
    m13f = archive.build(PROJ13, _k13)
    check("and read again on the next pass", _read13(), ["z13.jsonl"])
    _z13 = [f for f in m13f["files"] if f["file"] == "z13.jsonl"][0]
    check("its record is not complete", _z13.get("complete"), False)
    os.remove(_p13["z13"])
    del _p13["z13"]

    print("   an index written before 8.133 carries no time: its day is read")
    print("   once more, and kept from then on")
    _ix13 = os.path.join(PROJ13, "bridge-logs", D13B, "index.json")
    archive.build(PROJ13, _k13)
    _read13()
    _old13 = json.load(open(_ix13, encoding="utf-8"))
    for f in _old13["files"]:
        f.pop("mtime_ns", None)
        f.pop("complete", None)
    with open(_ix13, "w", encoding="utf-8") as fh:
        json.dump(_old13, fh, ensure_ascii=False, indent=1, sort_keys=True)
    archive.build(PROJ13, _k13)
    check("that day's files were read", _read13(),
          ["d13.jsonl", "e13.jsonl"])
    archive.build(PROJ13, _k13)
    check("and on the next pass kept", _read13(), [])
finally:
    del archive.open

print("\n" + ("-" * 60))
owntemp.finish(TMP, bool(FAILED))
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
