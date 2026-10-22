#!/usr/bin/env bash
# Pull the latest code, rebuild the app image, apply migrations, self-check.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d db
docker compose build app
docker compose run --rm -T app ode initdb
docker compose run --rm -T app ode doctor
