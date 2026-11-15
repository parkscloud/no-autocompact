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

## Layout and source of truth

- `DESIGN.md`: the specification. Verified background (§2), cost model (§3), decisions D1 to D8
  made with the owner (§4), the design (§6), stages and checks (§7), versioning (§8). Read the
  relevant section before changing behavior; a change of behavior is a new decision row.
- `scripts/nac.py`: all logic (Python 3.8+, standard library only). `scripts/hook.sh`: the
  shell gate in front of every hook, so sessions never armed don't start Python.
- `skills/{on,off,status}/SKILL.md`, `hooks/hooks.json`, `.claude-plugin/{plugin,marketplace}.json`.
- `tests/test_nac.py`: unit tests. `tools/`: `cc-context.py` (search the Claude Code binary),
  `ptysession.py` (drive a throwaway session), `smoke-plugin.py`, `probe-cron-hooks.py`.

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
python3 -I tools/smoke-plugin.py --model haiku   # live: about 3 minutes, a few cheap requests
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
