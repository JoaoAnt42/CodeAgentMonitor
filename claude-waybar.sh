#!/usr/bin/env bash
# claude-waybar.sh — waybar module for CLI code agent instance status (Claude Code, OpenCode, etc.)

CLAUDE_PROJECTS="$HOME/.claude/projects"
OPENCODE_DB="$HOME/.local/share/opencode/opencode.db"
NOW=$(date +%s)
TOTAL=0
NEEDS_INPUT=0
TOOLTIP=""

# Count running claude and opencode processes
PIDS=$(ps -eo pid,args --no-headers | grep -E '(^| )(claude|opencode)( |$)' | grep -v claude-ps | grep -v grep | awk '{print $1}')
for PID in $PIDS; do
    [ -d "/proc/$PID" ] && TOTAL=$((TOTAL + 1))
done

if [ "$TOTAL" -eq 0 ]; then
    echo '{"text": "", "class": "none"}'
    exit 0
fi

# Check Claude session states
for PROJ_DIR in "$CLAUDE_PROJECTS"/*/; do
    [ -d "$PROJ_DIR" ] || continue
    for JSONL in "$PROJ_DIR"*.jsonl; do
        [ -f "$JSONL" ] || continue
        MTIME=$(stat -c %Y "$JSONL" 2>/dev/null) || continue
        AGE=$((NOW - MTIME))
        [ "$AGE" -gt 300 ] && continue

        # Read tail for state detection
        LAST_LINE=$(tail -c 8192 "$JSONL" | grep '"role"' | tail -1)
        if echo "$LAST_LINE" | grep -q '"assistant"' && echo "$LAST_LINE" | grep -q '"tool_use"'; then
            [ "$AGE" -gt 5 ] && NEEDS_INPUT=$((NEEDS_INPUT + 1))
        fi
    done
done

# Check OpenCode session states (from SQLite)
if [ -f "$OPENCODE_DB" ]; then
    NOW_MS=$((NOW * 1000))
    CUTOFF=$((NOW_MS - 300000))
    OC_PENDING=$(sqlite3 "$OPENCODE_DB" "
        SELECT COUNT(*) FROM part p
        JOIN session s ON p.session_id = s.id
        WHERE s.time_updated > $CUTOFF
          AND json_extract(p.data, '\$.type') = 'tool'
          AND json_extract(p.data, '\$.state.status') = 'pending'
          AND p.time_created = (
              SELECT MAX(p2.time_created) FROM part p2 WHERE p2.session_id = s.id
          )
    " 2>/dev/null || echo 0)
    NEEDS_INPUT=$((NEEDS_INPUT + OC_PENDING))
fi

if [ "$NEEDS_INPUT" -gt 0 ]; then
    TEXT="󰚩 $NEEDS_INPUT!"
    CLASS="attention"
    TOOLTIP="$TOTAL instances, $NEEDS_INPUT waiting for input"
else
    TEXT="󰚩 $TOTAL"
    CLASS="ok"
    TOOLTIP="$TOTAL instances, all working"
fi

printf '{"text": "%s", "tooltip": "%s", "class": "%s"}\n' "$TEXT" "$TOOLTIP" "$CLASS"
