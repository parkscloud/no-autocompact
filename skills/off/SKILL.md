---
name: off
description: Turn off no-autocompact in this Claude Code session, so idle compaction works normally again and the keep-warm schedule is deleted. Use only when the user asks to stop keeping this session from compacting.
allowed-tools:
  - Bash(python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py *)
  - CronList
  - CronDelete
---

# no-autocompact: disarm this session

!`python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py disarm --session ${CLAUDE_SESSION_ID} --data ${CLAUDE_PLUGIN_DATA}`

The lines above come from the plugin's disarming script. Follow them exactly.
