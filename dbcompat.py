"""dbcompat.py - lets the app run on SQLite (local/dev) or PostgreSQL (Neon) from one code base.

Set DATABASE_URL (e.g. the Neon connection string) to use PostgreSQL. Leave it unset to keep
using the local SQLite file exactly as before.

The rest of the app keeps writing SQLite-style SQL (``?`` placeholders, INSERT OR IGNORE/REPLACE,
``cur.lastrowid`` ...). In PostgreSQL mode this module translates that SQL on the fly.
"""
import os
import re
import sqlite3
import threading
from contextlib import contextmanager

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if DATABASE_URL.startswith("postgres://"):  # some providers still use the old scheme
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = bool(DATABASE_URL)

# Tables whose primary key is an auto-increment "id" column (needed to emulate cursor.lastrowid).
ID_TABLES: set[str] = set()
# Conflict target used to emulate INSERT OR REPLACE.
REPLACE_KEYS = {
    "descriptions": ("report_id",),
    "branding": ("company_id",),
    "heartbeat": ("name",),
    "file_blobs": ("path",),
}

if USE_PG:
    import psycopg


class IntegrityViolation(Exception):
    """Raised in PostgreSQL mode; the message mimics SQLite ('UNIQUE constraint failed: users.username')
    so the existing error handling in db.py keeps working."""


# Catch this in `except IntegrityError:` - it works for both databases.
IntegrityError = (sqlite3.IntegrityError, IntegrityViolation)


# --------------------------------------------------------------------------- #
# Row type: behaves like sqlite3.Row (access by name or index, dict(row) works)
# --------------------------------------------------------------------------- #
class Row:
    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        self._names = names
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._names.index(key)]
        except ValueError:
            raise IndexError(f"No item with that key: {key!r}") from None

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


def _row_factory(cursor):
    names = [c.name for c in (cursor.description or [])]
    return lambda values: Row(names, list(values))


# --------------------------------------------------------------------------- #
# SQL translation  (SQLite dialect -> PostgreSQL)
# --------------------------------------------------------------------------- #
_INSERT_RE = re.compile(r"^\s*INSERT\s+(OR\s+(IGNORE|REPLACE)\s+)?INTO\s+(\w+)\s*(\(([^)]*)\))?", re.I)


def _qmarks_to_pct(sql: str) -> str:
    """Replace ? with %s outside string literals and escape literal % for psycopg."""
    out, in_str = [], False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "?" and not in_str:
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


def translate_sql(sql: str) -> tuple[str, str | None]:
    """Return (postgres_sql, table_name_if_returning_id_was_added)."""
    stripped = sql.strip().rstrip(";")
    if re.match(r"^BEGIN(\s+IMMEDIATE|\s+EXCLUSIVE|\s+DEFERRED)?$", stripped, re.I):
        return "", None  # psycopg already runs everything inside a transaction
    returning_table = None
    m = _INSERT_RE.match(stripped)
    if m:
        mode, table, cols = (m.group(2) or "").upper(), m.group(3).lower(), m.group(5)
        if mode:
            stripped = _INSERT_RE.sub(lambda mm: f"INSERT INTO {mm.group(3)} " + (f"({mm.group(5)})" if mm.group(4) else ""),
                                      stripped, count=1)
        if mode == "IGNORE":
            stripped += " ON CONFLICT DO NOTHING"
        elif mode == "REPLACE":
            keys = REPLACE_KEYS[table]
            columns = [c.strip() for c in (cols or "").split(",")]
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in keys)
            stripped += f" ON CONFLICT ({', '.join(keys)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
        if table in ID_TABLES and " returning " not in stripped.lower():
            stripped += " RETURNING id"
            returning_table = table
    return _qmarks_to_pct(stripped), returning_table


# Schema translation: SQLite DDL -> PostgreSQL DDL
def translate_schema(script: str) -> list[str]:
    s = script
    s = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"(\w+)\s+TEXT\s+NOT NULL\s+UNIQUE\s+COLLATE\s+NOCASE", r"\1 CITEXT NOT NULL UNIQUE", s, flags=re.I)
    s = re.sub(r"(\w+)\s+TEXT\s+NOT NULL\s+COLLATE\s+NOCASE", r"\1 CITEXT NOT NULL", s, flags=re.I)
    s = re.sub(r"telegram_id\s+INTEGER", "telegram_id BIGINT", s, flags=re.I)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
    s = re.sub(r"\bBLOB\b", "BYTEA", s, flags=re.I)
    return [part.strip() for part in s.split(";") if part.strip()]


def _register_id_tables(script: str) -> None:
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(\s*id\s+INTEGER PRIMARY KEY AUTOINCREMENT", script, re.I):
        ID_TABLES.add(m.group(1).lower())


# --------------------------------------------------------------------------- #
# Connection wrappers
# --------------------------------------------------------------------------- #
def _integrity_from_pg(e) -> IntegrityViolation:
    diag = getattr(e, "diag", None)
    table = getattr(diag, "table_name", None) or ""
    column = ""
    detail = getattr(diag, "message_detail", "") or ""
    m = re.match(r"Key \(([^)]*)\)=", detail)
    if m:
        column = m.group(1).split(",")[0].strip().lower()
    if not column:
        constraint = getattr(diag, "constraint_name", "") or ""
        column = constraint.replace(f"{table}_", "", 1).replace("_key", "")
    return IntegrityViolation(f"UNIQUE constraint failed: {table}.{column}")


class _PgCursor:
    def __init__(self, cur, returning: bool):
        self._cur = cur
        self.lastrowid = None
        if returning:
            row = cur.fetchone()
            self.lastrowid = row[0] if row else None

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConnection:
    def __init__(self, con):
        self._con = con

    def execute(self, sql, params=()):
        pg_sql, returning_table = translate_sql(sql)
        if not pg_sql:
            return _Noop()
        try:
            cur = self._con.cursor(row_factory=_row_factory)
            cur.execute(pg_sql, tuple(params))
        except psycopg.errors.IntegrityError as e:
            raise _integrity_from_pg(e) from e
        return _PgCursor(cur, returning_table is not None)

    def executescript(self, script: str) -> None:
        _register_id_tables(script)
        for statement in translate_schema(script):
            self._con.execute(statement)


class _Noop:
    lastrowid = None
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(
                DATABASE_URL, min_size=1, max_size=int(os.getenv("DB_POOL_MAX", "5")),
                kwargs={"prepare_threshold": None, "connect_timeout": 20},  # safe with Neon's pooled endpoint
                check=ConnectionPool.check_connection, max_idle=240, open=True)
            _pool.wait(timeout=45)
    return _pool


def _configure(con):
    # NUMERIC -> float so JSON/pandas code behaves like it did on SQLite
    from psycopg.adapt import Loader

    class _FloatLoader(Loader):
        def load(self, data):
            return float(bytes(data).decode())

    con.adapters.register_loader("numeric", _FloatLoader)


@contextmanager
def connect_pg():
    pool = _get_pool()
    with pool.connection() as con:  # commits on success, rolls back on error
        _configure(con)
        yield _PgConnection(con)


def ensure_citext() -> None:
    with _get_pool().connection() as con:
        con.execute("CREATE EXTENSION IF NOT EXISTS citext")


def ensure_column(con, table: str, column: str, decl: str) -> None:
    decl = re.sub(r"\bINTEGER\b", "INTEGER", decl)
    con._con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {decl}")
