#!/usr/bin/env bash
# Write today's action plan + shortlist to reports/ on the host:
#   reports/plan-YYYY-MM-DD.md  (latest: reports/plan-latest.md)  <- read this first
#   reports/gems-YYYY-MM-DD.md / .csv  (latest: reports/latest.md)
# The container only prints; the host writes the files (no permission issues).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p reports
day=$(date -u +%Y-%m-%d)
docker compose run --rm -T app ode plan --format md > "reports/plan-$day.md"
docker compose run --rm -T app ode gems --format md --limit 40 > "reports/gems-$day.md"
docker compose run --rm -T app ode gems --format csv --limit 200 > "reports/gems-$day.csv"
ln -sf "gems-$day.md" reports/latest.md
ln -sf "plan-$day.md" reports/plan-latest.md
find reports \( -name 'gems-*' -o -name 'plan-*' \) -mtime +60 -delete
echo "written: reports/plan-$day.md reports/gems-$day.md"
