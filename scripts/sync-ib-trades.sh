#!/usr/bin/env bash
# Daily sync of IBKR trade-confirmation emails (Gmail label:IB-trades) into the
# Transactions Google spreadsheet, via a headless Claude Code run.
# Installed as a weekday 1:30pm local cron job (see `crontab -l`).
# Logs to ~/.cron-logs/sync-ib-trades.log.
set -u

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
HERE="$(cd "$(dirname "$0")" && pwd)"
PROMPT_FILE="$HERE/sync-ib-trades.prompt.md"
LOG="$HOME/.cron-logs/sync-ib-trades.log"
mkdir -p "$(dirname "$LOG")"

# Keep the log bounded: rotate when it exceeds ~1 MB, keeping one previous file.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv -f "$LOG" "$LOG.1"
fi

cd "$HOME/tony-stock" || { echo "cannot cd to repo" >>"$LOG"; exit 1; }

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  claude -p "$(cat "$PROMPT_FILE")" \
    --model sonnet \
    --dangerously-skip-permissions \
    2>&1
  echo
} >>"$LOG" 2>&1
