#!/bin/sh
# Daily backups of the PostgreSQL database for docker-compose.yml. Same file format as backend/app/backup.py:
# /data/backups/rootiq-<UTC time>.tar.gz with rootiq.pgdump and the two settings files . Secrets encrypted.
# The encryption key is NOT in the backup: keep a copy of it apart from the backups. /data/secret.key.
set -eu
KEEP="${ROOTIQ_BACKUP_KEEP:-14}"
EVERY="${ROOTIQ_BACKUP_SECONDS:-86400}"
mkdir -p /data/backups
while true; do
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  work="$(mktemp -d)"
  if pg_dump --format=custom --no-owner --file="$work/rootiq.pgdump"; then
    for f in data_sources.json model_settings.json; do [ -f "/data/$f" ] && cp "/data/$f" "$work/"; done
    tar -czf "/data/backups/rootiq-$stamp.partial" -C "$work" .
    mv "/data/backups/rootiq-$stamp.partial" "/data/backups/rootiq-$stamp.tar.gz"
    chmod 600 "/data/backups/rootiq-$stamp.tar.gz"
    echo "Backup rootiq-$stamp.tar.gz"
    ls -1t /data/backups/rootiq-*.tar.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
  else
    echo "Backup failed. Trying again in 10 minutes." >&2
    rm -rf "$work"; sleep 600; continue
  fi
  rm -rf "$work"
  sleep "$EVERY"
done
