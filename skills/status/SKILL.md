---
name: status
description: Report whether no-autocompact is on in this Claude Code session, and what it has done so far.
disable-model-invocation: true
allowed-tools:
  - Bash(python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py *)
---

# no-autocompact: status

!`python3 -I ${CLAUDE_PLUGIN_ROOT}/scripts/nac.py status --session ${CLAUDE_SESSION_ID} --data ${CLAUDE_PLUGIN_DATA}`
