<!-- rule 36: the index's own lines, not the record -->
# Claude Code Bridge — configuration

Part of [README.md](README.md), moved out whole under rule 36.

<!-- /rule 36 -->
## Configuration

`data/config.json`, created on first run. The keys most worth knowing:

| key | default | what it does |
|---|---|---|
| `port` | `8765` | the local port |
| `projects` | `{}` | watched projects, keyed by path |
| `role_modes` | `executor: bypassPermissions`, `planner: auto` | permission mode per role. A project can override it |
| `role_effort` | `planner: max`, `executor: xhigh` | effort level a role's window starts with, passed as `--effort`; a role not named starts at the client's own level. A project can override it in `projects[path].effort` |
| `role_window` | `executor: 222000`, `planner: 1000000` | the auto-compact window a role's window starts with, in tokens, handed to that window alone as `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (the client reads it before any settings file), so the project's shared `.claude/settings.json` is never edited to give the two halves different windows. A role not named gets none and the project's own settings decide. A project can override it in `projects[path].window` |
| `role_idle_compaction` | `planner: false` | `false` switches the client's idle compaction off in that role's window, passed as `--settings {"idleCompaction":false}` (the flag source outranks the project's settings files). The client otherwise compacts a long conversation that sits idle, and a reviewer waits for reports most of its life. A project can override it in `projects[path].idle_compaction`; `true` there leaves it on |
| `telegram` | empty | token and chat id |
| `thresholds.idle_hold` | `1200` | how long an idling pair is held rather than answered, in seconds. `0` disables |
| `thresholds.review_timeout` | `1200` | how long a report may wait for a verdict |
| `archive_parallel` | `2` | archive searches at once across all projects |
| `archive_model` | `sonnet` | the model the search agent runs on |
| `retention.days` | `7` | how long the bridge keeps its own logs |
| `browser` | empty | the browser the panel opens in at the start, by its application name - `"Firefox"`, `"Google Chrome"`, `"Safari"` on a Mac, where it is opened with `open -a`; a Python `webbrowser` name elsewhere. Empty: the default browser. One that is not installed opens the default browser instead, with a line in the journal |

### One trap worth an hour of your evening: the two compaction settings multiply

**The bridge sets a compaction threshold again, per project, and it is the
composition of two settings** — see below for what it composes with. It hands
out `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` only for a project that has both halves,
and strips an inherited one, so there is exactly one source for it. Between
2026-09-01 and 2026-09-02 it set none at all, and this paragraph said so; the
reasoning for setting one again is at the end of this section. The trap is
unchanged, and it cost a fortnight to find.

`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` is a **percent**. Claude Code also has its own
`autoCompactWindow` setting in a project's `.claude/settings.json`, in
**tokens**.

They do not override one another. They **compose**: `autoCompactWindow` becomes
the window the percent is taken of. Set it to 700000 believing you have asked
for a 700k threshold and you have actually asked for 70% of 700k — and
compaction will fire around **477k**, on a window that is really a million.

Nothing warns you, because nothing is broken: every part is doing its job. What
you see is sessions being replaced about **1.5× more often than they should
be**, each replacement paying for a fresh context, and no obvious reason why.
Measured here over nine compactions: 476,221–482,049 tokens, against a control
project with no `autoCompactWindow` that ran to the full million.

**So set `autoCompactWindow` to the real window size, or leave it out
entirely.** If you want to know where your sessions actually compact rather
than where you asked them to, the client writes it down itself: search a
transcript under `~/.claude/projects/` for `compact_boundary` and read
`preTokens`. That is the client's own number, not the bridge's, which is why it
settles the question.

**What this project sets, and why.** `autoCompactWindow` is the real window
size, 1,000,000, in each watched project's own `.claude/settings.json`, and
`autocompact_pct` is 90 in the bridge's config — so the threshold is about
900,000 (the client takes a summary buffer off first: its own records put the
compactions at about 882k). Since 2026-10-10 the bridge also gives each half
its own window, `role_window`: the working half 222,000, which compacts at
about 182k, the reviewing half 1,000,000. The file is left as it is; the
window rides in that one window's environment.
Both halves or neither: the bridge sends no percentage to a project without the
key, because that would record a threshold the window is not actually running
under. The reasoning is worth having before you copy it. Reaching the ceiling
is survivable — the API answers *prompt is too long*, the client summarises
from there, and measured here about 10k of context survives each time — but it
looks like a death every time, and anything watching a session is capable of
interfering with the recovery. A threshold a little below the ceiling ends the
cycle deliberately instead. It is not free: the threshold decides how much work
a session gets through before it is replaced — roughly 2.3M tokens at a 477k
point, 3.4M at 700k, 5M at the ceiling — and each replacement pays for a fresh
context. About 4% of cycles still end at the ceiling anyway, on the measured
width of a last turn; buying that down to zero would cost another 100k of
threshold. Lower is not safer for free, and neither is higher.

Environment variables:

| variable | what it does |
|---|---|
| `BRIDGE_PORT` | override the port |
| `BRIDGE_DATA` | put the bridge's own files somewhere else — the suites use this |
| `BRIDGE_DEBUG=1` | write a watch log to the temp folder |
| `BRIDGE_NO_HOOKS=1` | a process the bridge spawned that is not half of a pair |
| `BRIDGE_ROLE` | the role of a window. Set per window at launch, never in project settings |

