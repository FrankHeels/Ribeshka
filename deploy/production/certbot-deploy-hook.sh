#!/bin/sh
set -eu
umask 077
# Compose's per-release variables are owned by the deployer. The certificate
# hook needs only the one stable, running proxy and uses the same operation lock.
exec 9>/opt/fishsite/shared/operations.lock
flock -w 600 9
container=$(docker ps -q --filter label=com.docker.compose.project=fishsite --filter label=com.docker.compose.service=nginx)
test -n "$container"
test "$(printf '%s\n' "$container" | wc -l)" -eq 1
docker exec "$container" nginx -t
docker exec "$container" nginx -s reload
date -u +%FT%TZ > /opt/fishsite/shared/certbot-last-reload.txt
