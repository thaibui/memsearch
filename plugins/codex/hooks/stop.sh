#!/usr/bin/env bash
# Stop hook: extract the last turn, hand it to the centralized summarization service,
# and append the returned summary to the daily memory file.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_latest_user_prompt_from_history() {
  local session_id="$1"
  local history_file="${CODEX_HOME:-$HOME/.codex}/history.jsonl"
  if [ -z "$session_id" ] || [ ! -f "$history_file" ]; then
    return 0
  fi

  python3 -c "
import json, sys
session_id = sys.argv[1]
history_file = sys.argv[2]
latest = ''
with open(history_file) as f:
    for line in f:
        try:
            obj = json.loads(line)
            if obj.get('session_id') == session_id:
                text = (obj.get('text') or '').strip()
                if text:
                    latest = text
        except Exception:
            pass
print(latest)
" "$session_id" "$history_file" 2>/dev/null || true
}

source "$SCRIPT_DIR/common.sh"

# Prevent infinite loop: if this Stop was triggered by a previous Stop hook, bail out
STOP_HOOK_ACTIVE=$(_json_val "$INPUT" "stop_hook_active" "false")
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  echo '{}'
  exit 0
fi

# Extract transcript path from hook input
TRANSCRIPT_PATH=$(_json_val "$INPUT" "transcript_path" "")

ensure_memory_dir

# Determine today's date and current time
TODAY=$(date +%Y-%m-%d)
NOW=$(date +%H:%M)
MEMORY_FILE="$MEMORY_DIR/$TODAY.md"

# Extract session ID for progressive disclosure anchors
SESSION_ID=$(_json_val "$INPUT" "session_id" "")
if [ -z "$SESSION_ID" ] && [ -n "$TRANSCRIPT_PATH" ]; then
  SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl | sed 's/^rollout-//')
fi

# Extract user question and last assistant message before going async
LAST_MSG=$(_json_val "$INPUT" "last_assistant_message" "")
# Cap before it flows into the payload JSON.
# Unbounded transcripts risk ARG_MAX overflow on execve.
if [ ${#LAST_MSG} -gt 4000 ]; then
  LAST_MSG="${LAST_MSG:0:4000}...(truncated)"
fi
USER_QUESTION=""
PARSED=""

if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  LINE_COUNT=$(wc -l < "$TRANSCRIPT_PATH" 2>/dev/null || echo "0")
  if [ "$LINE_COUNT" -lt 3 ]; then
    echo '{}'
    exit 0
  fi

  USER_QUESTION=$(python3 -c "
import json, sys
last_q = ''
with open(sys.argv[1]) as f:
    for line in f:
        try:
            obj = json.loads(line)
            if obj.get('type') == 'event_msg':
                p = obj.get('payload', {})
                if p.get('type') == 'user_message':
                    msg = p.get('message', '').strip()
                    if msg:
                        last_q = msg
        except:
            pass
# Truncate to first line, max 200 chars
first_line = last_q.split('\n')[0][:200] if last_q else ''
print(first_line)
" "$TRANSCRIPT_PATH" 2>/dev/null || true)

  # Parse the last turn before returning. Codex may clean up transient rollout
  # files as soon as the hook completes, so the submit step should work from
  # cached content instead of reopening the transcript path later.
  PARSED=$("$SCRIPT_DIR/../scripts/parse-rollout.sh" "$TRANSCRIPT_PATH" 2>/dev/null || true)
fi

if [ -z "$USER_QUESTION" ]; then
  USER_QUESTION=$(_latest_user_prompt_from_history "$SESSION_ID")
fi

CONTENT=""
if [ -n "$PARSED" ] && [ "$PARSED" != "(empty rollout)" ] && [ "$PARSED" != "(no user message found)" ] && [ "$PARSED" != "(empty turn)" ]; then
  CONTENT="$PARSED"
elif [ -n "$LAST_MSG" ] && [ -n "$USER_QUESTION" ]; then
  CONTENT="[Human]: ${USER_QUESTION}
[Codex]: ${LAST_MSG}"
elif [ -n "$LAST_MSG" ]; then
  CONTENT="[Codex]: ${LAST_MSG}"
else
  echo '{}'
  exit 0
fi

if [ ${#CONTENT} -gt 4000 ]; then
  CONTENT="${CONTENT:0:4000}...(truncated)"
fi

if ! memsearch_available; then
  echo '{}'
  exit 0
fi

WORK_FILE="$(mktemp "${TMPDIR:-/tmp}/memsearch-stop.XXXXXX.json")"
python3 - "$WORK_FILE" "$NOW" "$MEMORY_FILE" "$SESSION_ID" "$TRANSCRIPT_PATH" "$CONTENT" "$USER_QUESTION" "$LAST_MSG" <<'PY'
from pathlib import Path
import json
import sys

payload = {
    "now": sys.argv[2],
    "memory_file": sys.argv[3],
    "session_id": sys.argv[4],
    "transcript_path": sys.argv[5],
    "content": sys.argv[6],
    "user_question": sys.argv[7],
    "last_msg": sys.argv[8],
}
Path(sys.argv[1]).write_text(json.dumps(payload))
PY

if "${MEMSEARCH_CMD[@]}" submit-turn "$WORK_FILE" >/dev/null 2>&1; then
  run_memsearch index "$MEMORY_DIR" >/dev/null
fi

rm -f "$WORK_FILE"
echo '{}'
