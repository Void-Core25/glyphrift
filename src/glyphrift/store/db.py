"""SQLite connection handling and migration runner."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from glyphrift.store.migrations import MIGRATIONS, Migration

INDEX_DIR_NAME = ".glyphrift"
INDEX_FILE_NAME = "index.db"


class MigrationError(RuntimeError):
    """A schema migration failed (the database is left at its previous version)."""


class DatabaseTooNewError(RuntimeError):
    """The database was created by a newer version of GLYPHRIFT."""


def default_db_path(repo_root: str | Path) -> Path:
    """Where the index for a repository lives: ``<repo>/.glyphrift/index.db``."""
    return Path(repo_root) / INDEX_DIR_NAME / INDEX_FILE_NAME


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (creating if needed) the index database and bring it up to date.

    The connection is in autocommit mode with foreign keys enforced and rows accessible by
    column name. Use :func:`transaction` to group writes atomically.
    """
    is_memory = str(db_path) == ":memory:"
    if not is_memory:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if not is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _check_consecutive(migrations: Sequence[Migration]) -> None:
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(
                f"Migrations must be numbered 1, 2, 3...; found version {migration.version} "
                f"where {expected} was expected"
            )


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Apply all pending migrations; return the resulting schema version."""
    _check_consecutive(migrations)
    latest = len(migrations)
    current = get_schema_version(conn)
    if current > latest:
        raise DatabaseTooNewError(
            f"Database schema version {current} is newer than this GLYPHRIFT supports "
            f"({latest}). Upgrade GLYPHRIFT or delete the index and rebuild it."
        )
    for migration in migrations[current:]:
        _apply(conn, migration)
    return latest


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        f"PRAGMA user_version = {int(migration.version)};\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(
            f"Migration {migration.version} ({migration.description}) failed: {exc}"
        ) from exc


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one write transaction: commit on success, roll back on any error.

    Transactions cannot be nested.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
