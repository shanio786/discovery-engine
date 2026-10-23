#!/usr/bin/env bash
# Write today's shortlist to reports/gems-YYYY-MM-DD.md (+ .csv) on the host.
# The container only prints; the host writes the files (no permission issues).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p reports
day=$(date -u +%Y-%m-%d)
docker compose run --rm -T app ode gems --format md --limit 40 > "reports/gems-$day.md"
docker compose run --rm -T app ode gems --format csv --limit 200 > "reports/gems-$day.csv"
ln -sf "gems-$day.md" reports/latest.md
find reports -name 'gems-*' -mtime +60 -delete
echo "written: reports/gems-$day.md"
