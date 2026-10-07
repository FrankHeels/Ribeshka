#!/usr/bin/env python3
"""Manage the dedicated local PostgreSQL 18.6 cluster; never touch primary PGDATA."""
import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess

import psycopg
from psycopg import sql
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".test-postgres"
DATA = STATE / "data"
SOCKET = STATE / "socket"
ADMIN = "fishsite_test_admin"
VERSION = "18.6"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    pg_ctl = shutil.which("pg_ctl")
    initdb = shutil.which("initdb")
    if not pg_ctl or not initdb:
        parser.error("PostgreSQL 18.6 binaries (pg_ctl, initdb) are required on PATH.")
    for binary in (pg_ctl, initdb):
        version = run(binary, "--version", capture_output=True, text=True).stdout
        if version.split()[2] != VERSION:
            parser.error("The test cluster requires PostgreSQL 18.6, matching the local primary version.")
    if args.action != "start":
        if not (DATA / "PG_VERSION").exists():
            parser.error("Test cluster has not been initialized.")
        command = [pg_ctl, "-D", str(DATA), args.action]
        if args.action == "stop":
            command += ["-m", "fast", "-w"]
        return subprocess.run(command).returncode

    env_file = ROOT / ".env.test"
    if not env_file.exists():
        with env_file.open("x", encoding="utf-8") as stream:
            os.chmod(env_file, 0o600)
            stream.write((ROOT / ".env.test.example").read_text().replace(
                "replace-with-a-local-test-password", secrets.token_urlsafe(32)
            ))
    values = dotenv_values(env_file)
    password = os.environ.get("FISHSITE_TEST_DB_PASSWORD", values.get("FISHSITE_TEST_DB_PASSWORD"))
    if not password or password == "replace-with-a-local-test-password":
        parser.error("Set a local test password in .env.test; no password was changed.")
    STATE.mkdir(mode=0o700, exist_ok=True)
    SOCKET.mkdir(mode=0o700, exist_ok=True)
    if not (DATA / "PG_VERSION").exists():
        run(initdb, "-D", str(DATA), "-U", ADMIN, "--encoding=UTF8", "--locale=C", "--auth-local=trust", "--auth-host=scram-sha-256")
        with (DATA / "postgresql.conf").open("a") as stream:
            stream.write(
                "\n# Dedicated FishSite synthetic test cluster\n"
                "listen_addresses = '127.0.0.1'\nport = 15432\n"
                "cluster_name = 'fishsite-tests'\n"
                f"unix_socket_directories = '{str(SOCKET).replace(chr(39), chr(39)*2)}'\n"
            )
        (DATA / "pg_hba.conf").write_text(
            f"local all {ADMIN} trust\n"
            "host postgres,test_fishsite fishsite_tester 127.0.0.1/32 scram-sha-256\n"
            "host all all 0.0.0.0/0 reject\n"
            "host all all ::0/0 reject\n"
            "local all all reject\n"
        )
    status = subprocess.run([pg_ctl, "-D", str(DATA), "status"], capture_output=True)
    if status.returncode:
        run(pg_ctl, "-D", str(DATA), "-l", str(STATE / "postgres.log"), "-w", "start")
    with psycopg.connect(host=str(SOCKET), port=15432, user=ADMIN, dbname="postgres", autocommit=True) as connection:
        identity = connection.execute("SELECT current_setting('cluster_name'), current_setting('server_version'), inet_server_addr()").fetchone()
        if identity[0] != "fishsite-tests" or not identity[1].startswith(VERSION) or identity[2] is not None:
            raise SystemExit("Refusing to configure an unexpected PostgreSQL instance.")
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fishsite_tester'").fetchone():
            connection.execute("CREATE ROLE fishsite_tester LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION")
        connection.execute(sql.SQL("ALTER ROLE fishsite_tester PASSWORD {}").format(sql.Literal(password)))
    print("Isolated PostgreSQL 18.6 ready at 127.0.0.1:15432 (fishsite_tester, CREATEDB, no superuser).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
