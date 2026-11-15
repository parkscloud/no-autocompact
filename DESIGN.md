# no-autocompact — Design Specification

**Status: v0.1.0 released 2026-11-15; the long live checks in §7.2 are still to run.** The
design was approved by the owner on 2026-11-15. The background in §2 was checked against
Claude Code 2.1.325's code and docs, with live probes. Decisions D1 to D8, taken with the
owner one at a time, are in §4; the design in §6 follows from them.

## 1. Summary

`no-autocompact` is a Claude Code skill that keeps a chosen session from being compacted
while its user is away, using as little model time as possible.

Since 2.1.299, Claude Code compacts an attended session holding 200K tokens or more once it
has sat idle for 54 minutes on the one-hour prompt cache. The summary saves tokens on the
next message, but the working detail is gone when the user comes back. Claude Code's own
switch for this, `"idleCompaction": false`, applies to every session at once.

### Goals

- **Per session**: arm the session you choose; every other session keeps Claude Code's defaults.
- **Least LLM use**: no model request where a hook or a setting can do the job, and where a
  request is the cheaper path (§3), only as many as the timing rules require.
- **Never strand a session**: compaction near the context limit keeps working, so an armed
  session can't run into the wall.
- **Verified, and re-checkable**: every behavior relied on is checked against the installed
  Claude Code, and the checks can be rerun after each release, because the internals move.

### Non-goals

- Stopping compaction at the context limit. Claude Code already offers that
  (`autoCompactEnabled: false`, `DISABLE_AUTO_COMPACT=1`), with a warning before the hard limit.
- Agents other than Claude Code. Codex and the others don't compact this way.
- The VS Code panel and the desktop app's Code tab, which write the summary while you're
  away and swap it in later. Not examined yet; the terminal comes first.

## 2. How Claude Code 2.1.325 compacts (verified)

Sources: the code inside the 2.1.325 native binary (searched with `tools/cc-context.py`;
minified names change every release, so search by the strings listed in that script), the
docs at `code.claude.com/docs/en/` (`settings-reference`, `prompt-caching`, `hooks`,
`scheduled-tasks`, `skills`, `env-vars`, `costs`, `interactive-mode`), and the transcript of
the session in §2.4.

### 2.1 Three triggers

1. **Threshold**: checked before every model request, mid-turn included. It fires near the
   window less the smaller of the model's maximum output and 20K, less another 13K, which is
   about 967K on a 1M window. Context grows only through requests, so this can't fire while
   nobody sends one.
2. **Reactive**: after the API refuses a request as too long.
3. **Idle** (2.1.299 and later): the only one that fires with no request at all (§2.2).

### 2.2 Idle compaction

- **Arming.** The timer is set whenever a turn ends, provided the session is attended (an
  interactive terminal, the VS Code panel or the desktop Code tab; not `-p`, and not a
  `CLAUDE_CODE_SESSION_KIND` of `bg`, `daemon` or `daemon-worker`) and the last main-thread
  request used the one-hour cache TTL. On the five-minute TTL it never arms.
- **When it fires.** Last cache activity plus 0.9 of the TTL: 54 minutes.
- **When it gives up.** Automatic or idle compaction is off (both settings are read at that
  moment); the timer fired more than 60 seconds late (the machine slept) or the cache isn't
  warm; a newer request was made; the context is under max(100K, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`
  or 200K); the rate-limit status isn't `allowed`; the prompt prefix changed. In the terminal
  it also refuses while a turn or a question is in progress, when another process holds the
  session, when the transcript moved on, or when the user touched the session within the last 60 seconds.
- **Result.** The terminal compacts in place and shows *Compacted while idle, before the
  prompt cache expired · turn off with "idleCompaction": false in settings.json*.
- **History.** Added in the terminal in 2.1.299, a `/config` row in 2.1.302, the VS Code
  panel and the desktop Code tab in 2.1.314, and a fix in 2.1.318. The README's table quotes
  each changelog entry, as re-checked against the live changelog on 2026-11-15.

### 2.3 A PreCompact hook can veto every compaction path, idle included

The idle run calls the terminal's own compaction routine, the one `/compact` uses, with the
trigger `auto`. That routine runs the PreCompact hooks first. When any hook blocks (exit code
2, or `{"decision":"block"}` on stdout), it returns *skipped*, the idle run records
*not_compacted*, and Claude Code shows the warning *compaction blocked by PreCompact hook*.
The hooks run before the routine makes any model request: in parallel with them it only
assembles the system prompt and context. A veto therefore costs no tokens, and on this path
even a summary computed earlier in the background is applied only after the hooks agree.
Threshold, reactive, manual and partial compactions consult the same hooks; a summary
precomputed near the threshold consults them when it is computed.

A hook can't tell an idle run from a threshold run by its input: both report `auto`, and the
input adds only `custom_instructions` and the common fields (`session_id`, `transcript_path`,
`cwd`, …). Timing separates them. At an idle run the transcript's newest entry is many
minutes old (51 in §2.4, where a recap landed three minutes after the last reply). A
threshold or reactive run happens while a turn is writing to the transcript.

Read from the code only; the live test is in §7.

### 2.4 The case that prompted this project (2026-11-15)

Claude Opus 5.5 on a 1M window, in an interactive terminal with Remote Control on. The last
request went out at 18:36:02Z holding about 424.5K tokens, 422,550 of them read from cache
and the rest written on the one-hour TTL. A recap followed at 18:39:16Z. Idle compaction
started at 19:30:02Z, 54 minutes after the request, and finished at 19:31:36Z, taking the
conversation from 424,768 tokens to 13,202. The user's next message came at 19:33:02Z. Either
a request every 30 minutes or a PreCompact veto would have kept the full context.

### 2.5 Other facts the design leans on

- **Cache lifetime.** Each request that reads the cache resets its lifetime at no extra cost,
  and the idle timer is set again after every turn. Claude Code asks for the one-hour TTL for
  the main conversation on a Claude subscription within its plan's included usage. On usage
  credits, an API key, a cloud provider or a gateway it uses five minutes unless the user
  chooses the hour, and idle compaction arms only on the one-hour TTL (2.1.314 extended it to
  those providers once that TTL is on).
- **Scheduled tasks** (`/loop`, `CronCreate`) fire only while Claude Code is running and
  idle, between turns. A recurring task runs late by up to half its gap, 30 minutes at most,
  and by the same amount every run when the runs are evenly spaced. Every 30 minutes therefore
  keeps 30-minute gaps; hourly can't beat a 54-minute timer. Recurring tasks expire after seven
  days. A self-paced `/loop` (`ScheduleWakeup`, 60 to 3,600 seconds) has to reschedule with a
  tool call each round, so each of its ticks costs at least two requests.
- **A hook can skip a scheduled turn for free** (documented, and confirmed live on 2.1.325
  with `tools/probe-cron-hooks.py`, 2026-11-15). A scheduled task's firing is queued as a
  meta prompt with slash commands skipped, so a tick can't invoke a skill, but it does pass
  through the UserPromptSubmit hooks. A block (`{"decision":"block"}`) stops it before any
  model request: the probe's blocked tick left only a *Running scheduled task* line and the
  block notice in the transcript, with no request and no reply. The notice shows the hook's
  `reason` to the user and isn't added to Claude's context; `suppressOriginalPrompt` keeps
  the tick's own text out of it. Every skipped tick still prints that notice. The hook's
  input has no field marking a prompt as scheduled (`prompt`, `prompt_id`, `session_title`
  and the common fields), so a tick is recognized by its text. The same hooks also run, with
  a 30-second default timeout, on every typed prompt, background-subagent report and
  cross-session message.
- **Session identity.** Hook and Bash subprocesses get `CLAUDE_CODE_SESSION_ID`, the same
  value as the hook input's `session_id` (it changes on `/clear`). A skill can use
  `${CLAUDE_SESSION_ID}`, and its `` !`command` `` lines run before the model sees the skill,
  except in skills synced from claude.ai, which a terminal session shows as literal text.
- **Idle-time model use Claude Code already makes**: session recaps (at least three minutes
  after a turn, while the terminal is unfocused, never twice in a row), scheduled tasks,
  cross-session messages (each delivered as a full turn) and goal check-ins.
- **Tool-result clearing after idle.** An experiment behind the server flag
  `tengu_jiggly_blossom` (cached as `off` on the owner's machine, 2026-11-15) can drop old
  tool results through the API's `clear_tool_uses_20250919` edit when a session returns after
  its cache went cold. It isn't a compaction, so a PreCompact hook presumably can't stop it
  (untested). A warm cache never triggers it.
- **`CLAUDE_CODE_LOOP_KEEPALIVE`** only restores self-paced `/loop`'s old 20-minute fallback
  wakeup; it doesn't keep the prompt cache alive.

## 3. Options and what they cost

Claude Opus 5.5 prices, per million tokens: input $4, output $20, cache read $0.20 (0.05× input),
five-minute cache write $5, one-hour write $8 (the claude-api skill bundled with Claude Code
2.1.325, prices cached 2026-11-03). A subscription spends plan usage rather than dollars. I
found no statement of how a plan weighs cache reads against writes, so list prices serve as
the yardstick.

For a session of C tokens, with C = 425K as in §2.4:

| Approach | While you're away | When you come back |
|---|---|---|
| **Default** (let idle compaction run) | one summary request at 54 minutes, about $0.35 | small context; the detail is gone |
| **Guard**: a PreCompact hook vetoes the idle run | no requests | the whole context is cached again at the one-hour write rate, C × $8/M ≈ $3.40, and the first reply is slow |
| **Keep-warm**: a short turn every 30 minutes | two requests an hour, each C × $0.20/M plus about $0.02 ≈ $0.10 | warm: fast and cheap |
| **Guard plus capped keep-warm** | keep-warm until the ticks have cost one cold return (about 34 ticks, 17 hours), then the guard alone | warm if you're back within the cap |
| **`"idleCompaction": false`** in user settings | no requests, in every session | as Guard, in every session |

Total cost at 425K by length of absence:

| Away for | Guard | Keep-warm, uncapped | Guard plus capped keep-warm |
|---|---|---|---|
| 1 hour | $3.40 | $0.20 | $0.20 |
| 3 hours | $3.40 | $0.60 | $0.60 |
| 8 hours | $3.40 | $1.60 | $1.60 |
| 24 hours | $3.40 | $4.80 | $6.80 (34 ticks, then one cold return) |
| 7 days (the cron's expiry) | $3.40 | $33.60 | $6.80 |

**Break-even.** On Opus 5.5 a cold one-hour write costs 2× input and a cache read 0.05×, so
one cold return buys about 40 reads; with each tick's small extras, about 34 ticks, or 17 hours
at two an hour. Shorter absences favor keep-warm, longer ones the guard alone. Capping the
ticks at break-even keeps the total within about twice the cheaper of the two, whenever the
user returns. The cap scales with the model: about 20 reads where cache reads cost 0.1× input,
about 80 on Fable 5.2 (0.025×).

**Chosen**: the guard plus keep-warm without a cap (D2, D6), so a long absence costs about
$0.20 an hour at 425K rather than staying within twice the minimum.

**What only the hook can do.** Keep-warm alone fails open: a tick that errors, a cap that
runs out, or a stopped schedule lets the next idle run compact. The guard costs nothing and
catches all three.

## 4. Decision log

Decisions made with the owner, one at a time. Each question in §5 becomes a `Dnn` row here
once answered.

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Name | `no-autocompact`: repo, plugin and skill. **The command form is AMENDED by D7**: as a plugin's skills it becomes `/no-autocompact:on`, `:off` and `:status`, not `/no-autocompact`. | Given: the project folder and the session that started it carry this name; lowercase-kebab like the owner's other skill repos. 2026-11-15. |
| D2 | Mechanism | **The cap described here is SUPERSEDED by D6 (no cap); the rest stands.** **Guard plus capped keep-warm.** A PreCompact hook vetoes the idle compaction of an armed session at no token cost, and a short session-scoped turn every 30 minutes keeps the prompt cache warm until those turns have cost about one cold return; after that the guard works alone. | Fewest billed tokens for any absence under about 17 hours at 425K, and within about twice the minimum for longer ones (§3); a warm, fast first reply on return; the hook catches what the turns can miss (a failed turn, the cap, a schedule that stops). The owner accepted the costs: two short turns an hour while away, and more to build and test than the guard alone. Decided 2026-11-15. |
| D3 | What the guard vetoes | **Idle runs only.** The guard blocks an automatic compaction of an armed session only when the transcript's newest entry is more than about ten minutes old. Compaction near the context limit, after a too-long error and by hand (`/compact`) always runs. | The near-limit and too-long paths exist to keep a full session working; vetoing them would run the session into the hard limit mid-task, and Claude Code's `autoCompactEnabled: false` already offers that for anyone who wants it. Decided 2026-11-15. |
| D4 | Visibility | **A public GitHub repository.** The README explains the purpose to the public: that the project exists for Claude Code, following the idle compaction introduced in 2.1.299 and widened in 2.1.302, 2.1.314 and 2.1.318 (re-checked against the live changelog and docs). | The owner's instruction. The license is still open (§5). Decided 2026-11-15. |
| D5 | How long arming lasts | **The whole session, with keep-warm automatic across absences.** Once armed, the guard stays on until `/no-autocompact off` or the session ends. One recurring 30-minute schedule serves the whole session: a hook lets a tick through only after 20 or more idle minutes while the cache is still warm, and skips it at no token cost otherwise. | Arm once and every absence is covered, including ones you don't announce. The owner accepted the costs: a one-line notice every 30 minutes while working (each skipped tick prints one), and the schedule's seven-day expiry, after which the guard holds alone. Decided 2026-11-15. |
| D6 | Keep-warm cap | **No cap.** Within an absence, ticks keep the cache warm for as long as the absence lasts, until the schedule's seven-day expiry. Ticks still skip, at no cost, whenever the cache has already gone cold (after the machine slept, say), since warming it again then costs a full re-read that the first message back would pay anyway. | The owner preferred a warm return over the cost bound: about $0.20 an hour at 425K, which passes the price of one full re-read after about 17 hours (a week away comes to about $34). Supersedes the cap in D2. Decided 2026-11-15. |
| D7 | Packaging | **A Claude Code plugin with its own marketplace**, as okf-author does: `/plugin marketplace add parkscloud/no-autocompact`, then `/plugin install no-autocompact@no-autocompact`. The skills and hooks ship, update (auto-update is opt-in) and uninstall together, and nothing edits the user's `settings.json`. Commands: `/no-autocompact:on`, `/no-autocompact:off`, `/no-autocompact:status`, and plain words for arming. | One install carries every part, and a plugin is Claude Code's only native way to update a skill. The cost is the namespaced command form (amends D1). Decided 2026-11-15. |
| D8 | License | **MIT**, as okf-author uses; `LICENSE` holds the standard text. | Short and the most common license for a small developer tool; the repository holds only the owner's own code. Decided 2026-11-15. |

## 5. Open decisions

None. D1 to D8 settle every question raised so far. The design in §6 follows from them and
waits for the owner's sign-off.

## 6. Design

### 6.1 Repository layout

```
.claude-plugin/plugin.json        manifest: name no-autocompact, version, MIT license
.claude-plugin/marketplace.json   the self-hosted marketplace, listing this plugin (source "./")
skills/on/SKILL.md                /no-autocompact:on arms the session (plain words work too)
skills/off/SKILL.md               /no-autocompact:off disarms it (plain words work too)
skills/status/SKILL.md            /no-autocompact:status reports (command only)
hooks/hooks.json                  the four hook entries of 6.4 to 6.6
scripts/hook.sh                   a shell gate in front of every hook; never-armed sessions stop here
scripts/nac.py                    arm, disarm, status and the hook handlers (Python 3 standard library)
tests/                            unit tests (stdlib unittest) on synthetic transcripts and state
tools/                            cc-context.py and the live probes
README.md  DESIGN.md  AGENTS.md  CHANGELOG.md  LICENSE
```

### 6.2 State

One small JSON file per session that has been armed, at
`${CLAUDE_PLUGIN_DATA}/sessions/<session-id>.json`. It records whether the session is armed
and since when; the schedule's cron line and creation time; whether a turn is open; when the
last prompt arrived and when the last successful turn ended; and tick and veto counts, with
the latest of each. Writes go to a temporary file renamed into place, so a hook never reads
half a file. Arming prunes files untouched for 30 days.

### 6.3 Arming: `/no-autocompact:on`, or plain words

- The skill's description lets Claude invoke it whenever the user asks, in any words, to keep
  the session from compacting while they're away. Its frontmatter allows `CronCreate`.
- A `` !`…` `` line runs `nac.py arm` with `${CLAUDE_SESSION_ID}` and `${CLAUDE_PLUGIN_DATA}`
  before Claude sees the skill. The script writes the state and prints the exact `CronCreate`
  call: recurring, `M,M+30 * * * *` with M from 1 to 29 derived from the session ID (clear of
  :00 and :30, and spreading sessions apart), and the tick prompt
  `[no-autocompact keep-warm] Reply with only: ok`. If the session already has a schedule
  under seven days old, it says so and asks for none.
- Claude makes that call and confirms in one line: three or four short requests in all,
  since the scheduling tools are deferred and Claude loads them with `ToolSearch` first (seen
  in the smoke test, 2026-11-15).

### 6.4 Keep-warm: the UserPromptSubmit hook

It runs on every prompt Claude Code submits: typed ones, scheduled firings, background
subagents' reports and cross-session messages.

- A tick (a prompt starting with the marker) **passes** only when the session is armed, no
  turn is open, and the last successful turn ended between 20 and 53 minutes ago. The hook
  records the pass and opens a turn.
- Any other tick is **skipped** with `{"decision":"block","reason":…}` and
  `suppressOriginalPrompt`, so the terminal shows one line and Claude sees nothing:
  *no-autocompact: keep-warm skipped* followed by the reason in brackets: *(you're active)*,
  *(the cache has already expired; your next message re-reads the conversation once)* or
  *(this session isn't armed)*. A tick never meets a running turn, since Claude Code fires
  scheduled prompts only between turns, so the hook also closes any turn an interrupt left
  open.
- Any other prompt opens a turn. In an armed session whose schedule is more than about 6.9
  days old, the hook also adds `additionalContext` asking Claude to recreate the schedule with
  the exact call, one tool call a week. It asks the same, at most once a day, when the session
  has just sat idle long enough for a tick without one arriving: 32 minutes, or 46 before the
  schedule's first tick, which can take up to 45 minutes (30-minute spacing plus up to 15 of
  jitter). In a session that isn't armed but has seen a skipped tick (a schedule left over
  after `/clear`, say), it asks Claude to delete that schedule.

Why 20 and 53 minutes: ticks come 30 minutes apart, so the window from 20 to 50 minutes after
a turn ends always holds exactly one. That tick passes, the cache's hour starts over, and the
54-minute idle timer never fires. The upper bound of 53 rather than 50 leaves room for the
last request's own run time inside the cache's 60 minutes. Past it, a tick would pay to cache
the whole conversation again, which the user's own return pays anyway.

### 6.5 Turn tracking: the Stop and StopFailure hooks

- **Stop** (a turn finished) closes the turn and records the time as the last successful
  turn end.
- **StopFailure** (a turn ended on an API error) closes the turn but leaves that time alone,
  since a failed request doesn't refresh the cache.
- A user's interrupt fires neither; the guard's freshness test (6.6) covers the turn it
  leaves open.

### 6.6 Guard: the PreCompact hook (matcher `auto`)

In an armed session, an automatic compaction **goes ahead** only when it happens inside a
turn: a turn is open and the transcript's newest entry is under ten minutes old, or the
transcript's newest prompt or tool result is under two minutes old. The second test catches a
turn started by something that skips UserPromptSubmit; a skipped tick writes neither kind of
entry. **Anything else is an idle run, and the guard vetoes it** with
`{"decision":"block","reason":"no-autocompact: idle compaction vetoed; this session keeps its full context"}`,
recording the veto. The guard reads only the transcript's last 256 KB, and only entry types
and timestamps. On any error it lets the compaction go ahead: a wrongly allowed idle run costs
detail, while a wrongly blocked near-limit run could stall the session. Manual `/compact`
reports the trigger `manual` and never reaches the guard.

### 6.7 Disarming and status

- `/no-autocompact:off`, or plain words: the script marks the session disarmed and tells
  Claude to delete the keep-warm schedule, finding it with `CronList` by its marker when the
  ID isn't at hand. If the deletion is missed, the hook still skips every tick of a disarmed
  session, at no cost.
- `/no-autocompact:status` (command only): the script prints the state in plain lines, and
  Claude relays them.

### 6.8 Edge cases

- **`/clear`** starts a new session ID, which isn't armed. A schedule left running is skipped
  tick by tick and deleted at the next prompt (6.4).
- **`--resume`** keeps the session ID, so the arming carries over. The scheduled-tasks docs
  say Claude Code restores tasks on resume, while `CronCreate`'s own description in 2.1.325
  says jobs end with the process; either way, the lost-schedule check in 6.4 asks for a new
  one when no tick arrives.
- **The machine sleeps**: the ticks and the idle timer both stop, and the timer gives up once
  it's late. After waking, a tick finds the cache cold and is skipped; the first message back
  re-reads the conversation once.
- **Near a usage limit**, idle compaction gives up by itself, and a tick that passes but fails
  ends in StopFailure (6.5).
- **Disabling or uninstalling the plugin** removes the hooks: idle compaction returns, and a
  leftover schedule's ticks would reach Claude. The README asks for `/no-autocompact:off` first.

### 6.9 Performance

The hooks run on every prompt, turn end and automatic compaction in every session while the
plugin is enabled. `scripts/hook.sh` checks for the session's state file and for the tick
marker, so a session that was never armed costs one shell start, a few milliseconds, and no
Python. An armed session adds one Python start (about 30 ms) and a small file read and write;
the guard reads at most 256 KB of transcript.

## 7. Build and verification plan

### 7.1 Stages

1. Plugin skeleton: manifests and CHANGELOG; `claude plugin validate` passes.
2. `scripts/nac.py` and `scripts/hook.sh`, with unit tests on synthetic transcripts and state
   covering every tick, guard and arming decision in §6.
3. Skills and `hooks/hooks.json`, loaded in a throwaway session with `claude --plugin-dir`.
4. The live checks in 7.2, each scripted in `tools/` so it can be rerun.
5. Publish the public repository, install from its marketplace as a user would, and release
   v0.1.0; 1.0.0 follows once 7.2 passes on that install (§8).

### 7.2 Checks

- **Done, static**: the code paths in §2.2 and §2.3, read in 2.1.325.
- **Done, live**: a UserPromptSubmit hook sees a scheduled task's firing and can block it
  before any model request (`tools/probe-cron-hooks.py`, 2.1.325, 2026-11-15; §2.5).
- **Done, live: the smoke test** (`tools/smoke-plugin.py`, 2.1.325, Haiku 5.5, 2026-11-15).
  `/no-autocompact:on` made the exact `CronCreate` call and wrote the state, and the Stop
  hook recorded the turn's end; a firing of the marker prompt was skipped (*you're active*)
  with no reply from Claude; `/compact` by hand compacted (check 1 below); status answered;
  `/no-autocompact:off` disarmed and deleted both marker tasks; `~/.claude/settings.json`
  was unchanged. 47 unit tests pass (`python3 -I -m unittest discover -s tests`).
- **Before 1.0.0, live**, in throwaway sessions on a cheap model. They never confirm a dialog
  other than folder trust, and `~/.claude/settings.json` is compared before and after.
  1. `/compact` by hand still works in an armed session.
  2. The idle veto: started with `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=100000` and holding about
     110K tokens, with keep-warm off, an armed session left for 55 minutes shows the guard's
     veto, gains no `compact_boundary` entry and keeps its full context. Control: an unarmed
     session compacts.
  3. Keep-warm over two hours away: ticks pass about every 30 minutes, the idle timer never
     fires, and the next typed prompt starts the skipping again.
  4. Near-limit compaction still runs in an armed session: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
     at its 100K minimum puts the threshold near 67K tokens.
  5. Turn tracking after an interrupt, and the leftover-schedule path after `/clear`.
- **After each Claude Code update** whose changelog touches compaction, scheduled tasks or
  prompt hooks: rerun 2 and 3, and re-read the strings listed in `tools/cc-context.py`.
- **Scripts**: `tools/smoke-plugin.py` covers check 1, and `tools/live-idle-check.py` covers
  checks 2 and 3 in one run of about 2¼ hours. Checks 4 and 5 have no script yet.

### 7.3 Known gaps

- **The VS Code panel and the desktop app's Code tab** write the idle summary while the user is
  away and swap it in at the next message (2.1.314). The code suggests that path also consults
  the PreCompact hooks, since a background summary runs them with the trigger `auto` (§2.3),
  but it hasn't been traced, and nothing here has been run there.
- **Windows** is untested: the gate needs `bash`, and `nac.py` skips file locking without
  `fcntl`.
- **Tool-result clearing after idle** (the server flag `tengu_jiggly_blossom`, §2.5) isn't a
  compaction, so the guard can't stop it. Keep-warm avoids it, since the cache never goes cold
  while the ticks run.
- **Anthropic's plugin directory** would need a square icon of 512 to 2048 pixels
  (`claude plugin validate` warns that none exists). Not needed for the self-hosted
  marketplace.
- **A typed prompt that starts with the marker** `[no-autocompact keep-warm]` is treated as a
  tick.
- **Two copies of the plugin** (an installed one and a `--plugin-dir` one) don't share state,
  so the installed copy skips the other's ticks; this matters only to development.

## 8. Versioning

0.x until every live check in §7.2 passes on an install from the marketplace, then 1.0.0.
After that, each user-facing change gets a semantic version bump, a `CHANGELOG.md` entry and
an annotated git tag. The version lives in `.claude-plugin/plugin.json`, in its entry in
`.claude-plugin/marketplace.json` and in the README's status line; all three move together
with the tag. v0.1.0 was released on 2026-11-15.
