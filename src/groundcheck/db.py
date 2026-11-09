"""Database layer. SQLite for dev/tests, Postgres for production (optional `psycopg`).
All SQL in the package is written once, with `?` placeholders and ON CONFLICT upserts, and
translated for Postgres by PgConn. Postgres support is NOT exercised by the test suite here."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_TEMPLATE = (Path(__file__).parent / "schema.sql").read_text()
_PK = {"sqlite": "INTEGER PRIMARY KEY", "postgres": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"}


def schema_for(dialect: str) -> str:
    return _TEMPLATE.replace("{PK}", _PK[dialect])


SCHEMA = schema_for("sqlite")


def translate(sql: str) -> str:
    """sqlite-style `?` placeholders -> psycopg `%s` (literal % escaped)."""
    return sql.replace("%", "%%").replace("?", "%s")


def split_statements(script: str):
    body = "\n".join(l for l in script.splitlines() if not l.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


class PgConn:
    """Minimal adapter giving a psycopg connection the sqlite3-style API used in this package."""
    dialect = "postgres"

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        cur = self.raw.cursor()
        cur.execute(translate(sql), params)
        return cur

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def connect(target: str = ":memory:", check_same_thread: bool = True):
    if target.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Postgres needs the optional driver: pip install 'groundcheck[postgres]'") from exc
        conn = PgConn(psycopg.connect(target, row_factory=dict_row))
        for stmt in split_statements(schema_for("postgres")):
            conn.execute(stmt)
        conn.commit()
        return conn
    conn = sqlite3.connect(target, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def insert_id(conn, sql: str, params=()) -> int:
    """INSERT ... RETURNING id (works on SQLite >= 3.35 and Postgres; lastrowid does not)."""
    return conn.execute(sql + " RETURNING id", params).fetchone()["id"]


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def get_or_create_title(conn, name: str, genre=None, studio=None, release_date=None) -> int:
    row = conn.execute("SELECT id FROM titles WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return insert_id(conn, "INSERT INTO titles (name, genre, studio, release_date) VALUES (?,?,?,?)",
                     (name, genre, studio, release_date))
