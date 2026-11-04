#!/usr/bin/env bash
# Dump do banco em ~/backups (guarda 14 dias). Agende no crontab da EC2:
#   0 3 * * * bash ~/rotacerta/rota-certa-backend-django/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ~/backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > ~/backups/rotacerta-$(date +%F).sql.gz
find ~/backups -name 'rotacerta-*.sql.gz' -mtime +14 -delete
