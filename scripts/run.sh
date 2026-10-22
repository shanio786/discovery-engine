#!/usr/bin/env bash
# Cron wrapper: run one `ode` command in the app container, log it, and
# record the exit code. Usage: scripts/run.sh daily | scripts/run.sh collect --source wikipedia
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
log="logs/ode-$(date -u +%Y-%m-%d).log"
{
  echo "=== $(date -u +%FT%TZ) ode $* ==="
  set +e
  docker compose run --rm -T app ode "$@"
  rc=$?
  set -e
  echo "=== exit $rc ==="
} >> "$log" 2>&1
find logs -name 'ode-*.log' -mtime +30 -delete
exit "$rc"
