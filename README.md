# no-autocompact

A Claude Code plugin that keeps the session you choose from being compacted while you're
away, using as little model time as possible.

**Status: v0.1.0, an early release (November 2026).** It passes its unit tests and a smoke
test in a live Claude Code 2.1.325 session. The longer live checks (an hour-long idle veto,
hours of keep-warm) are listed in [DESIGN.md §7.2](DESIGN.md#72-checks); 1.0.0 waits for
them.

## Why this exists

This project is specifically for [Claude Code](https://code.claude.com/docs), Anthropic's
coding agent, and answers one change in it: **idle compaction**, added in version 2.1.299.

Claude Code has long compacted a conversation as it nears the model's context window,
summarizing the history so the session can go on. Since 2.1.299 it also compacts a
conversation you have stepped away from. When an interactive session holding 200,000 tokens
or more has sat idle for about 54 minutes, Claude Code summarizes it shortly before its
one-hour prompt cache would expire, so that your next message doesn't have to reprocess the
whole conversation. Later releases widened it:

| Version | Change |
|---|---|
| 2.1.299 | Idle compaction in the terminal, with `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` to move its 200K minimum and `"idleCompaction": false` to turn it off (both read from 2.1.290 on) |
| 2.1.302 | An **Idle compaction** row in `/config`; the notice after an idle compaction says how to turn it off |
| 2.1.314 | The VS Code panel and the desktop app's Code tab too, where the summary is written while you're away and used on your next message if the cache has expired. Also runs for subscribers with telemetry off, and on Bedrock, Vertex, Foundry and LLM gateways once the one-hour cache is on |
| 2.1.318 | Fixed idle compaction replacing a conversation with a reply that wasn't a summary |

Sources: Claude Code's [changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
and its documentation of the
[`idleCompaction` setting](https://code.claude.com/docs/en/settings-reference#idlecompaction).

The summary makes your first message back cheaper, but the conversation itself is gone.
CLAUDE.md files, memory, skills and the current plan come back from disk, and up to five
recently changed files are re-read; the exact messages, tool output and reasoning that built
up over the session don't return
([what survives compaction](https://code.claude.com/docs/en/context-window#what-survives-compaction)).
After a long investigation, that working detail is often what you came back for.

Idle compaction also runs while a session waits on its own work. A background command or subagent that is
still running doesn't count as activity, since only Claude's requests reset the clock, so a
session that started an hour-long job is summarized before the job reports back. Anthropic
has said that's intended, because the cache would have expired by then anyway
([anthropics/claude-code#106402](https://github.com/anthropics/claude-code/issues/106402)).

Claude Code's own off switches are coarse:

- `"idleCompaction": false`, or the `/config` row, applies to every session that reads that
  settings file.
- `autoCompactEnabled: false` or `DISABLE_AUTO_COMPACT=1` also stops compaction near the
  context limit.
- With it off, your first message after an hour away re-sends the whole conversation at the
  cache-write price, the very cost idle compaction exists to avoid.

`no-autocompact` works per session and keeps that cost down.

## Install

```
/plugin marketplace add parkscloud/no-autocompact
/plugin install no-autocompact@no-autocompact
```

It needs Claude Code 2.1.299 or later (checked against 2.1.325), `bash` and Python 3.8 or
later on the `PATH`. It has been tried on Linux; macOS should work, and Windows hasn't been
tried.

## Use

In a session you want to keep, run `/no-autocompact:on`, or just ask Claude to keep the
session from compacting while you're away. The session stays armed until it ends or you run
`/no-autocompact:off`; every other session keeps Claude Code's defaults.
`/no-autocompact:status` shows what the plugin has done in the session.

Run `/no-autocompact:off` in an armed session before you disable or uninstall the plugin, so
no keep-warm schedule is left behind.

## How it works

- **Guard.** A `PreCompact` hook vetoes the idle compaction of an armed session, at no token
  cost. Compaction near the context limit, after a too-long error or by `/compact` carries
  on, so a session never runs into the wall.
- **Keep-warm.** Arming asks Claude to create one schedule that fires every 30 minutes. A
  `UserPromptSubmit` hook lets a firing through as a short turn only when the session has sat
  idle 20 minutes or more and its prompt cache is still warm, so the cache stays alive and
  your first reply back is fast. Otherwise the hook skips the firing before it reaches the
  model, at no token cost, leaving a one-line notice in the terminal (so expect one every 30
  minutes while you work in an armed session).
- **Turn tracking.** `Stop` and `StopFailure` hooks record when turns end, which tells the
  guard an idle run from one inside a turn.
- **Upkeep.** Claude Code ends a recurring schedule after seven days; at your next message
  after that, the plugin asks Claude to set up a new one. The same happens if the schedule
  stops for another reason, such as a restart.

Sessions that were never armed pay only for a quick shell check on each prompt, turn end and
compaction; nothing else runs. [DESIGN.md](DESIGN.md) has the full specification, the
research behind it and the decisions taken.

## What it costs

At Claude Opus 5.5 list prices, for a 425K-token conversation:

| | While you're away | When you come back |
|---|---|---|
| Idle compaction runs (Claude Code's default) | about $0.35 for the summary | the detail is gone |
| The guard alone | nothing | about $3.40 to re-cache the conversation, and a slow first reply |
| The guard with keep-warm (this plugin) | about $0.20 an hour | warm: fast and cheap |

Keep-warm stays below that $3.40 for any absence under about 17 hours. Arming itself takes
three or four short requests. On a Claude subscription these are plan usage rather than
dollars. The [cost model](DESIGN.md#3-options-and-what-they-cost) has the details.

## Why only Claude Code

Idle compaction is Claude Code's own behavior, and the fix leans on Claude Code's own parts:
its `PreCompact` hook event, its session-scoped scheduled tasks and its skill format. Other
coding agents compact on their own terms, if at all, and this project doesn't target them.

## Limitations

- Interactive terminal sessions only, so far. The VS Code panel and the desktop app's Code
  tab handle idle compaction differently and haven't been examined.
- The guard reads timestamps from the session's transcript, an internal file format that a
  Claude Code release could change; the checks in [DESIGN.md §7.2](DESIGN.md#72-checks) are
  rerun after releases that touch compaction.
- A prompt you type that starts with `[no-autocompact keep-warm]` is treated as a tick.

## License

[MIT](LICENSE).
