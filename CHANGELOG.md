# Changelog

All notable changes to no-autocompact are recorded here. Versions follow semantic
versioning; 0.x releases come before the live checks in DESIGN.md §7.2 have all passed.

## 1.0.0 (2026-11-15)

First stable release; the plugin behaves as in 0.1.1. Every live check in DESIGN.md §7.2
passed on Claude Code 2.1.325:
- the guard vetoed idle compaction, and the session kept its context;
- keep-warm held the cache for two hours, with no compaction;
- compaction near the context limit still ran in an armed session;
- an interrupt left the turn open until the next one ended, and after `/clear` a leftover
  schedule was skipped, mentioned once and removed by `/no-autocompact:off`;
- installs from this marketplace, from a local folder and from GitHub, passed the smoke test.

Tools: `tools/check-edges.py` runs checks 4 and 5, and `tools/smoke-plugin.py --installed`
tests the installed copy.

## 0.1.1 (2026-11-15)

- When an armed session's keep-warm schedule has stopped (Claude Code ends it after seven
  days, and a restart can drop it), Claude now tells you in one line that
  `/no-autocompact:on` restarts it, at most once a day, instead of recreating it in the
  middle of your turn. In auto mode the classifier had refused that call. `/no-autocompact:on`
  now restarts a stopped schedule (before, it could answer "already on"), and
  `/no-autocompact:status` shows it as stopped. The guard keeps vetoing meanwhile.
- A leftover schedule firing in a session that isn't armed (after `/clear`, say) gets the
  same treatment: Claude points you to `/no-autocompact:off` once.
- Checked: 51 unit tests; the live two-hour run (`tools/live-idle-check.py`), in which the
  unarmed control compacted while idle, the guard vetoed and the session kept its context,
  and keep-warm passed four ticks on a warm cache; and the smoke test, now with the stopped
  schedule path, in auto mode.
- Test tools: full model IDs (the alias `haiku` turns idle compaction off), the autocompact
  offer suppressed in probe sessions, no typing into an open dialog, and the live check
  stops at the first step that doesn't take.

## 0.1.0 (2026-11-15)

First release, as a Claude Code plugin with its own marketplace.

- `/no-autocompact:on` (or plain words) arms a session and has Claude create one 30-minute
  keep-warm schedule; `/no-autocompact:off` disarms it and deletes the schedule;
  `/no-autocompact:status` reports.
- A `PreCompact` hook vetoes idle compaction in an armed session, and only between turns.
- A `UserPromptSubmit` hook lets a keep-warm firing reach Claude only 20 to 53 minutes after
  the last successful turn, and skips it at no token cost otherwise. It also asks Claude to
  replace a schedule that lapsed after seven days or stopped, and to delete one left over in
  a session that isn't armed.
- `Stop` and `StopFailure` hooks track turns; a shell gate keeps sessions that were never
  armed from starting Python at all.
- Checked: 47 unit tests, and a smoke test in a live Claude Code 2.1.325 session
  (`tools/smoke-plugin.py`): arming, a skipped firing, `/compact` by hand, status and off.
  Not yet checked live: the hour-long idle veto and hours of keep-warm (DESIGN.md §7.2).
