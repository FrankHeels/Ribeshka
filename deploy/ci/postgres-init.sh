#!/usr/bin/env bash
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -p 15432 <<'SQL'
CREATE ROLE fishsite_tester LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION PASSWORD 'ci-tests-only';
SQL
cat > "$PGDATA/pg_hba.conf" <<'HBA'
local all fishsite_test_admin trust
host postgres,test_fishsite fishsite_tester 127.0.0.1/32 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
local all all reject
HBA
