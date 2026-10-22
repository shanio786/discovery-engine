#!/usr/bin/env bash
# Nightly compressed logical backup of the ODE database; keeps 14 days.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
out="backups/ode-$(date -u +%Y-%m-%d).dump"
docker compose exec -T db pg_dump -U ode -d ode -Fc > "$out.tmp"
mv "$out.tmp" "$out"
find backups -name 'ode-*.dump' -mtime +14 -delete
echo "backup written: $out ($(du -h "$out" | cut -f1))"
