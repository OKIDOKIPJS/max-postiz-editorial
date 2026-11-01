"""Project-local PostgreSQL lifecycle; no Windows service or global configuration changes."""
import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--port", type=int, default=55438)
    parser.add_argument("--runtime-root", type=Path,
                        help="ASCII junction к data этой копии для Windows PostgreSQL")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime = args.runtime_root or root / "data"
    if runtime.resolve() != (root / "data").resolve():
        raise RuntimeError("Runtime must resolve to this checkout's data directory")
    binary = runtime / "tools/pgsql/bin"
    cluster = runtime / "postgres"
    access = root / "data/postgres-access.json"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(name, *values):
        result = subprocess.run([str(binary / (name + (".exe" if os.name == "nt" else ""))), *map(str, values)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags, timeout=60)
        if result.returncode:
            raise RuntimeError(name + " failed; inspect data/postgres.log")

    if args.action in {"status", "stop"}:
        if args.action == "stop":
            run("pg_ctl", "-D", cluster, "-m", "fast", "-w", "stop")
        else:
            run("pg_ctl", "-D", cluster, "status")
        print(args.action + ": ok")
        return
    if not access.exists():
        access.parent.mkdir(parents=True, exist_ok=True)
        access.write_text(json.dumps({"password": secrets.token_urlsafe(32), "port": args.port}), encoding="utf-8")
    value = json.loads(access.read_text(encoding="utf-8"))
    if not cluster.exists():
        password_file = runtime / "postgres-password.tmp"
        password_file.write_text(value["password"], encoding="utf-8")
        try:
            run("initdb", "-D", cluster, "-U", "hub_admin", "-E", "UTF8", "--locale=C", "-A", "scram-sha-256", "--pwfile", password_file)
        finally:
            password_file.unlink(missing_ok=True)
        with (cluster / "postgresql.conf").open("a", encoding="utf-8") as f:
            f.write("\nlisten_addresses = '127.0.0.1'\nport = " + str(value["port"]) + "\n")
    status = subprocess.run([str(binary / "pg_ctl.exe"), "-D", str(cluster), "status"],
                            capture_output=True, creationflags=flags)
    if status.returncode:
        run("pg_ctl", "-D", cluster, "-l", runtime / "postgres.log", "-w", "start")
    with psycopg.connect(host="127.0.0.1", port=value["port"], user="hub_admin", password=value["password"], dbname="postgres", autocommit=True, connect_timeout=10) as conn:
        for db in ("editorial_hub", "editorial_hub_tests"):
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    env = root / ".env.local"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith(("DATABASE_URL=", "TEST_DATABASE_URL="))]
    for key, db in (("DATABASE_URL", "editorial_hub"), ("TEST_DATABASE_URL", "editorial_hub_tests")):
        lines.append(f"{key}=postgresql+psycopg://hub_admin:{value['password']}@127.0.0.1:{value['port']}/{db}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("PostgreSQL started on loopback; runtime and test databases ready")


if __name__ == "__main__":
    main()
