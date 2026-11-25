"""Provision run-owned databases and invoke the PostgreSQL archive tools."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess

import psycopg
from psycopg import sql


def pg_tool(*args, check=True):
    return subprocess.run(
        args, check=check, capture_output=True, text=True, errors="replace",
        env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}, timeout=120,
    )


@contextmanager
def run_databases(run_id):
    if not os.environ.get("PGHOST") or not os.environ.get("PGUSER"):
        raise ValueError("Set PGHOST and PGUSER for the disposable lab cluster.")
    created = []
    with psycopg.connect(dbname="postgres", connect_timeout=5, autocommit=True) as admin:
        def create(label, *, template=None):
            name = f"rd_{run_id}_{label}"
            statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
            if template is not None:
                if template not in created:
                    raise ValueError("The template must belong to this run.")
                statement += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
            admin.execute(statement)
            created.append(name)
            return name

        try:
            yield create, admin.info.parameter_status("server_version")
        finally:
            # A name enters this list only after CREATE succeeds on this connection.
            for database in reversed(created):
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def dump(database, archive: Path):
    pg_tool("pg_dump", "--format=custom", "--no-owner", "--no-privileges",
            "--file", str(archive), "--dbname", database)


def restore(database, archive: Path):
    return pg_tool("pg_restore", "--single-transaction", "--exit-on-error", "--no-owner",
                   "--no-privileges", "--dbname", database, str(archive), check=False)
