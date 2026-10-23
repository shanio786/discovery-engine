#!/usr/bin/env bash
# Restore the ODE database from a backup. REPLACES the current data.
#
#   scripts/restore.sh                       list local + off-site backups
#   scripts/restore.sh backups/ode-2026-10-01.dump
#   scripts/restore.sh ode-2026-10-01.dump.enc       (fetched from RCLONE_REMOTE, decrypted
#                                                     with BACKUP_PASSPHRASE)
set -euo pipefail
cd "$(dirname "$0")/.."

RCLONE_IMAGE="rclone/rclone:1.68.2"
RCLONE_CONF="${RCLONE_CONFIG_DIR:-$PWD/deploy/rclone}"
env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
rclone() { docker run --rm -v "$RCLONE_CONF:/config/rclone" -v "$PWD/backups:/data" "$RCLONE_IMAGE" "$@"; }
remote="$(env_get RCLONE_REMOTE)"

if [[ $# -eq 0 ]]; then
  echo "local:"; ls -1 backups/ode-*.dump 2>/dev/null || echo "  (none)"
  if [[ -n "$remote" ]]; then echo "off-site ($remote):"; rclone lsf "$remote" || true; fi
  exit 0
fi

file="$1"
mkdir -p backups
if [[ "$file" == *.enc ]]; then
  if [[ ! -f "$file" && ! -f "backups/$(basename "$file")" ]]; then
    [[ -n "$remote" ]] || { echo "RCLONE_REMOTE not set" >&2; exit 1; }
    rclone copy "$remote/$(basename "$file")" /data/
  fi
  [[ -f "$file" ]] || file="backups/$(basename "$file")"
  pass="$(env_get BACKUP_PASSPHRASE)"
  [[ -n "$pass" ]] || { echo "BACKUP_PASSPHRASE not set" >&2; exit 1; }
  plain="${file%.enc}"
  BACKUP_PASSPHRASE="$pass" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in "$file" -out "$plain" -pass env:BACKUP_PASSPHRASE
  file="$plain"
fi
[[ -s "$file" ]] || { echo "no such backup: $file" >&2; exit 1; }

echo "This REPLACES the current database with $file."
read -r -p "Type RESTORE to continue: " answer
[[ "$answer" == "RESTORE" ]] || { echo "cancelled"; exit 1; }

docker compose stop scheduler web >/dev/null 2>&1 || true
docker compose up -d --wait db
# TimescaleDB needs a fresh database and its pre/post-restore hooks around pg_restore.
docker compose exec -T db psql -U ode -d postgres -q -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS ode WITH (FORCE)" -c "CREATE DATABASE ode"
docker compose exec -T db psql -U ode -d ode -q -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
docker compose exec -T db pg_restore -U ode -d ode --no-owner < "$file" \
  || echo "pg_restore reported warnings (usually harmless TimescaleDB notices) — checking data"
docker compose exec -T db psql -U ode -d ode -q -v ON_ERROR_STOP=1 \
  -c "SELECT timescaledb_post_restore()" >/dev/null
docker compose exec -T db psql -U ode -d ode -t -c \
  "SELECT 'topics=' || count(*) FROM topics UNION ALL SELECT 'metrics=' || count(*) FROM topic_metrics"
docker compose up -d scheduler web
echo "restored from $file"
