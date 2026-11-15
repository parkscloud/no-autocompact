# AGENTS.md

Guidance for AI coding agents working in this repository. Read by Claude Code (2.1.277 or
later) and Codex alike; don't add a `CLAUDE.md` beside it, or Claude Code will ignore this file.

## What this is

`no-autocompact` is a public Claude Code plugin (MIT, `github.com/parkscloud/no-autocompact`)
that keeps a chosen session from being compacted while its user is away, with the least model
use. Skills arm, disarm and report; a PreCompact hook vetoes idle compaction; a
UserPromptSubmit hook, with Stop and StopFailure for turn tracking, lets a 30-minute keep-warm
schedule through only during absences. **Status: v0.1.0, released 2026-11-15; the long live
checks in `DESIGN.md` §7.2 are still to run before 1.0.0.** The README is the public face and
explains that the project exists because of the idle compaction Claude Code 2.1.299 added.

## Next steps (as of 2026-11-15, after v0.1.0)

1. **The second live run started at 17:49 local on 2026-11-15**
   (`build/live-idle-20261115T174942/`): `tools/live-idle-check.py`, covering `DESIGN.md`
   §7.2 checks 2 and 3 in three throwaway sessions (A guard only, B unarmed control, C
   keep-warm). The first run (16:21) was invalid, for the two reasons in §7.2. Results, and an
   `events` timeline written as it goes, are in `build/live-idle-<time>/result.json`
   (gitignored). If control B shows no compaction, the run proves nothing: the machine slept,
   or idle compaction gave up for a reason of its own (`DESIGN.md` §2.2, such as a usage-pace
   warning). Rerun it then.
2. **If it passes**: with the owner's OK, install from the marketplace at user scope
   (`/plugin marketplace add parkscloud/no-autocompact`, then
   `/plugin install no-autocompact@no-autocompact`) and confirm `/no-autocompact:on` works
   there; that copy's data directory differs from the `--plugin-dir` one. Then run §7.2
   checks 4 (near-limit compaction in an armed session, with
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`) and 5 (an interrupt, `/clear`), which no script
   covers yet. When everything passes, release 1.0.0 (see Releases).
3. **If it fails**: fix it, add a unit test that pins the failure, release 0.1.x and rerun.
4. **After the install**, offer the owner a one-line pointer to the plugin in the global note
   "Claude Code — when a session compacts" (his dotfiles repo, `agents/AGENTS.md`). That file
   sits near its 30,000-byte budget, and the owner commits the dotfiles repo himself.

## Layout and source of truth

- `DESIGN.md`: the specification. Verified background (§2), cost model (§3), decisions D1 to D8
  made with the owner (§4), the design (§6), stages and checks (§7), versioning (§8). Read the
  relevant section before changing behavior; a change of behavior is a new decision row.
- `scripts/nac.py`: all logic (Python 3.8+, standard library only). `scripts/hook.sh`: the
  shell gate in front of every hook, so sessions never armed don't start Python.
- `skills/{on,off,status}/SKILL.md`, `hooks/hooks.json`, `.claude-plugin/{plugin,marketplace}.json`.
- `tests/test_nac.py`: unit tests. `tools/`: `cc-context.py` (search the Claude Code binary),
  `ptysession.py` (drive a throwaway session), `smoke-plugin.py` (about 3 minutes),
  `live-idle-check.py` (about 2¼ hours), `probe-cron-hooks.py` (the original hook probe).

## Working rules

- **Spec first.** New design questions go to the owner one at a time, each with two to four
  options and a recommended one first; answers become `Dnn` rows in `DESIGN.md` §4.
- **Claude Code's internals move every release.** Before relying on a behavior, check it
  against the installed build (`claude --version`; native builds live in
  `~/.local/share/claude/versions/<version>`) with `tools/cc-context.py`, and against the docs
  (`https://code.claude.com/docs/en/<page>.md` serves Markdown). Minified names (`gRn`, `GWr`,
  `yKe`, …) hold for one release only; search by the stable strings listed at the top of
  `tools/cc-context.py`. On the owner's machine the system `grep` is ugrep, which rejects long
  context patterns, hence the script.
- **Live probes touch the user's real config.** The scripts in `tools/` start real Claude Code
  sessions that share `~/.claude` and `~/.claude.json`. `ptysession.start()` confirms only the
  folder-trust dialog, cancels every other dialog with Esc, and keeps the user's preference
  variables (`CLAUDE_CODE_NO_FLICKER` and the like); the scripts compare
  `~/.claude/settings.json` before and after. On 2026-11-15 an early probe's stray Enter
  accepted the fullscreen-renderer offer and wrote `"tui": "fullscreen"` there, which had to be
  removed by hand.
- **One copy of the plugin at a time.** Don't run the live tools while the marketplace copy
  is enabled: its hooks load beside the `--plugin-dir` copy, don't know that copy armed the
  session, and skip its ticks as "not armed". Disable the installed plugin first and enable it
  again afterwards.
- **A skill's `!` line must match its `allowed-tools` rule.** Outside auto mode, Claude Code
  aborts a skill whose injected command the permission rules don't allow, so each SKILL.md
  pre-approves `Bash(python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py *)`. Keep the `!`
  commands unquoted and starting with exactly that prefix, or the rule stops matching.
- **The owner's machine has no tmux**; `tools/ptysession.py` drives sessions through a Python
  pseudo-terminal instead. Probe sessions run on Haiku to stay cheap, and they appear in the
  session list as `no-autocompact-*`.
- **Probes name the model by its full ID** (`claude-haiku-5-5`). With the alias `haiku` (or
  `opusplan`) idle compaction never runs, which voided the first live run.
- **Dialogs can open mid-session**, not only at startup. The autocompact offer (*We recommend
  compacting earlier*) took the first live run's keystrokes, and confirming it would have
  saved a 400K window for Haiku 5.5 to the owner's user settings. `ptysession.clean_env()`
  sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000`, which suppresses the offer, and
  `type_line()` raises rather than type into a dialog. Keep both.
- **Background work doesn't hold off idle compaction.** A session over 200K tokens that waits
  on its own background command or subagent still compacts 54 minutes after its last request
  (`DESIGN.md` §2.2, §2.6). Before leaving a long background job running in a big session,
  keep the session warm or accept the compaction.
- **After a Claude Code update**, skim its changelog for idle compaction, PreCompact,
  UserPromptSubmit, Stop hooks and scheduled tasks (`CronCreate`). Rerun
  `tools/smoke-plugin.py`, and `tools/live-idle-check.py` when compaction changed. Also check
  the cached server flag `tengu_jiggly_blossom` in `~/.claude.json` (under
  `cachedGrowthBookFeatures`): `off` on 2026-11-15. If it turns `on`, Claude Code starts
  clearing old tool results when a session returns after its cache went cold, which the guard
  can't stop (`DESIGN.md` §2.5, §7.3).
- **Record what was verified and how** (code reading, docs or a live run) in `DESIGN.md` §2
  and §7.2.
- **Public repository.** Keep personal paths, account details and private repo names out of
  tracked files; this file holds the few pointers needed.
- **Writing**: no strikethrough; mark superseded text in words. Plain, concrete wording.

## Releases

Bump these together, then tag: `.claude-plugin/plugin.json` `version`, its entry in
`.claude-plugin/marketplace.json`, a `CHANGELOG.md` section, the README's status line, and an
annotated tag `vX.Y.Z` on the released commit. 0.x until every check in `DESIGN.md` §7.2 has
passed on an install from the marketplace; then 1.0.0.

Pushing: the remote is HTTPS under the owner's personal account `parkscloud`, with no git
credential helper. When the active `gh` account isn't `parkscloud`, push with
`GH_TOKEN_PC="$(gh auth token -u parkscloud)"` exported and
`git -c credential.helper= -c 'credential.helper=!f(){ echo username=parkscloud; echo "password=$GH_TOKEN_PC"; }; f' push`.
This repository pins `user.name`/`user.email` to the personal identity in its local git config.

## Test

```bash
python3 -I -m unittest discover -s tests -v      # unit tests, a fraction of a second
claude plugin validate .                         # manifests (one warning: no listing icon)
python3 -I tools/smoke-plugin.py                 # live: about 3 minutes on claude-haiku-5-5
```

## Project memory

- **Origin (2026-11-15).** Started at the owner's request, relayed by another Claude Code
  session after an idle compaction it had said couldn't happen; the case is in `DESIGN.md`
  §2.4. The owner's wider notes on when Claude Code compacts live in his dotfiles repo
  (`agents/claude-code-compaction.md`).
- **Verified against 2.1.325.** By code reading: idle compaction runs the PreCompact hooks with
  trigger `auto` before any model request, and a block leaves the conversation uncompacted.
  Live: UserPromptSubmit hooks run on scheduled-task firings, and a block stops the firing
  before any request, at the cost of a one-line notice (`DESIGN.md` §2.3, §2.5).
- **Plugin data.** A session's state is `${CLAUDE_PLUGIN_DATA}/sessions/<session-id>.json`;
  with `--plugin-dir` that's `~/.claude/plugins/data/no-autocompact-inline/`.
- **Costs** (Opus 5.5, claude-api skill prices cached 2026-11-03): at 425K tokens a keep-warm
  tick costs about $0.10 and a cold return about $3.40 (`DESIGN.md` §3). Arming takes three or
  four short requests, as Claude loads the deferred scheduling tools first.
- **The owner's setup.** An interactive terminal with Remote Control, on a Claude subscription,
  so the main conversation gets the one-hour cache TTL and idle compaction applies.
