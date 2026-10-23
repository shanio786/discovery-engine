#!/usr/bin/env bash
# Nightly compressed logical backup of the ODE database; keeps 14 days locally.
#
# Off-site copy (recommended — a dead VPS must not take your history and
# everything the engine learned with it). In .env:
#   RCLONE_REMOTE=gdrive:ode-backups     any rclone remote: Google Drive, Backblaze B2,
#                                        Cloudflare R2, S3, Dropbox, OneDrive …
#   BACKUP_PASSPHRASE=<long random>      dumps are AES-256 encrypted before upload
#   BACKUP_REMOTE_KEEP_DAYS=30
# One-time remote setup (interactive):   scripts/backup.sh --setup
# Restore:                               scripts/restore.sh
# A failed backup sends a Telegram alert (if Telegram is configured).
set -euo pipefail
cd "$(dirname "$0")/.."

RCLONE_IMAGE="rclone/rclone:1.68.2"
RCLONE_CONF="${RCLONE_CONFIG_DIR:-$PWD/deploy/rclone}"
env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
rclone() { docker run --rm -v "$RCLONE_CONF:/config/rclone" -v "$PWD/backups:/data" "$RCLONE_IMAGE" "$@"; }

if [[ "${1:-}" == "--setup" ]]; then
  mkdir -p "$RCLONE_CONF" && chmod 700 "$RCLONE_CONF"
  docker run --rm -it -v "$RCLONE_CONF:/config/rclone" "$RCLONE_IMAGE" config
  exit 0
fi

fail() {
  echo "BACKUP FAILED: $*" >&2
  docker compose run --rm -T app ode notify "🛑 ODE backup failed: $*" >/dev/null 2>&1 || true
  exit 1
}
trap 'fail "unexpected error on line $LINENO"' ERR

mkdir -p backups
out="backups/ode-$(date -u +%Y-%m-%d).dump"
docker compose exec -T db pg_dump -U ode -d ode -Fc > "$out.tmp"
[[ -s "$out.tmp" ]] || fail "pg_dump produced an empty file"
mv "$out.tmp" "$out"
find backups -name 'ode-*.dump*' -mtime +14 -delete
echo "backup written: $out ($(du -h "$out" | cut -f1))"

remote="$(env_get RCLONE_REMOTE)"
if [[ -n "$remote" ]]; then
  pass="$(env_get BACKUP_PASSPHRASE)"
  [[ -n "$pass" ]] || fail "RCLONE_REMOTE is set but BACKUP_PASSPHRASE is empty (never uploading unencrypted)"
  enc="$out.enc"
  BACKUP_PASSPHRASE="$pass" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "$out" -out "$enc" -pass env:BACKUP_PASSPHRASE
  rclone copy "/data/$(basename "$enc")" "$remote" || fail "upload to $remote failed"
  keep="$(env_get BACKUP_REMOTE_KEEP_DAYS)"
  rclone delete --min-age "${keep:-30}d" --include 'ode-*.dump.enc' "$remote" || true
  rm -f "$enc"
  echo "off-site copy: $remote/$(basename "$enc") (AES-256 encrypted)"
fi
