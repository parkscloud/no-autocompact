---
name: on
description: Keep this Claude Code session from being compacted while the user is away (the no-autocompact plugin). Use when the user asks, in any words, to keep this session's context or history, to stop it compacting or summarizing while they step away, or to keep it warm until they're back.
allowed-tools:
  - Bash(python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py *)
  - CronCreate
  - CronList
  - CronDelete
---

# no-autocompact: arm this session

!`python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py arm --session ${CLAUDE_SESSION_ID} --data ${CLAUDE_PLUGIN_DATA}`

The lines above come from the plugin's arming script. Follow them exactly.
