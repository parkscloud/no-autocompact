#!/usr/bin/env bash
# no-autocompact: the gate in front of every hook (DESIGN.md §6.9).
#
# Claude Code runs the plugin's hooks on every prompt, turn end and automatic compaction in
# every session while the plugin is enabled. A session that was never armed has no state
# file, so the gate stops here without starting Python. One exception: a keep-warm tick
# always goes on to nac.py, because a tick in a session that isn't armed (a schedule left
# over after /clear, say) must still be skipped, or it would reach Claude.
#
# Usage: hook.sh <prompt|stop|stop-failure|precompact>, with the hook's input JSON on stdin.
set -u
event="${1:-}"
input="$(cat)"

sid=""
if [[ $input =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([A-Za-z0-9_-]+)\" ]]; then
  sid="${BASH_REMATCH[1]}"
elif [[ ${CLAUDE_CODE_SESSION_ID:-} =~ ^[A-Za-z0-9_-]+$ ]]; then
  sid="$CLAUDE_CODE_SESSION_ID"
fi
data="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/plugins/data/no-autocompact}"

if [[ -n $sid && -f "$data/sessions/$sid.json" ]] ||
   [[ $event == prompt && $input == *'[no-autocompact keep-warm]'* ]]; then
  exec python3 -I "$(dirname "${BASH_SOURCE[0]}")/nac.py" hook "$event" --data "$data" <<<"$input"
fi
exit 0
