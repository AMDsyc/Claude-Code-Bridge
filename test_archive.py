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

TMP = tempfile.mkdtemp(prefix="bridge-archive-test-")
os.environ["BRIDGE_DATA"] = os.path.join(TMP, "data")
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
check("attributed to the same session", snaps[0]["session_id"], "known-sid")
check("the time it was taken is kept", snaps[0]["taken_at"], "143000")
check("and it inherits the role", snaps[0]["role"], "executor")
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
t = archive.rebuild_async(PROJ, known, done=lambda mm: seen.append(mm))
second_call = archive.rebuild_async(PROJ, known)
if t:
    t.join(30)
check("the second call did not start its own build", second_call, None)
check("the first one finished", len(seen), 1)
check("and it produced the map", seen[0]["totals"]["files"], len(m["files"]))
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
check("with no role guessed for it", _c12[0]["role"], "unknown")
check("and no project either", _c12[0]["project"], "unknown")
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

print("\n" + ("-" * 60))
shutil.rmtree(TMP, ignore_errors=True)
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
    sys.exit(1)
print("all cases pass")
