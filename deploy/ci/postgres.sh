#!/usr/bin/env bash
set -euo pipefail
# Host networking is intentional: existing tests verify both peer addresses,
# the port, role privileges, cluster_name and HBA rejection of the primary DB.
docker run -d --name fishsite-tests --network host \
  -e POSTGRES_USER=fishsite_test_admin -e POSTGRES_PASSWORD=ci-admin-only \
  -v "$PWD/deploy/ci/postgres-init.sh:/docker-entrypoint-initdb.d/10-tests.sh:ro" \
  postgres:18.6 -c listen_addresses=127.0.0.1 -c port=15432 -c cluster_name=fishsite-tests
for attempt in $(seq 1 60); do
  if docker exec fishsite-tests pg_isready -h 127.0.0.1 -p 15432 -U fishsite_tester >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
exit 1
