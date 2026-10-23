#!/usr/bin/env bash
# Pull the latest code, rebuild the app image, apply migrations, self-check.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
mkdir -p reports logs backups && chmod 0777 reports   # scheduler (uid 10001) writes reports
docker compose up -d db
docker compose build app
docker compose run --rm -T app ode initdb
docker compose run --rm -T app ode doctor
docker compose up -d scheduler   # (re)start the always-on engine with the new code
