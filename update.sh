#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Pull first so a registry/network failure leaves the running site untouched.
docker compose pull web
if [ -n "$(docker compose ps --status running -q web)" ]; then
  sh backup.sh
fi
docker compose up -d --no-build web
docker compose ps
