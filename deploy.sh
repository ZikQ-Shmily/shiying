#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
if [ ! -f .env ]; then
  task_password=$(openssl rand -hex 12)
  task_secret=$(openssl rand -hex 32)
  cat > .env <<EOF
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$task_password
SECRET_KEY=$task_secret
PORT=8787
BIND_ADDRESS=0.0.0.0
COOKIE_SECURE=false
EOF
fi
chmod 600 .env
mkdir -p data data/backups
chown -R 10001:10001 data
chmod 700 data data/backups
docker compose pull web
docker compose up -d --no-build web
docker compose ps
