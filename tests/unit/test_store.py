import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from glyphrift.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    DatabaseTooNewError,
    Migration,
    MigrationError,
    connect,
    default_db_path,
    get_schema_version,
    migrate,
    transaction,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def add_file(conn: sqlite3.Connection, path: str = "a.py") -> int:
    cur = conn.execute(
        "INSERT INTO files (path, language, sha256, size, indexed_at) "
        "VALUES (?, 'python', 'abc', 10, '2026-01-01T00:00:00Z')",
        (path,),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def add_symbol(
    conn: sqlite3.Connection,
    file_id: int,
    name: str = "f",
    kind: str = "function",
    parent_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO symbols (file_id, kind, name, qualified_name, line_start, line_end, "
        "parent_id) VALUES (?, ?, ?, ?, 1, 5, ?)",
        (file_id, kind, name, f"mod.{name}", parent_id),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def add_edge(
    conn: sqlite3.Connection,
    file_id: int,
    src: int,
    dst: int | None,
    confidence: str = "resolved",
    kind: str = "calls",
) -> int:
    cur = conn.execute(
        "INSERT INTO edges (src_symbol_id, dst_symbol_id, dst_name, kind, confidence, how, "
        "file_id, line) VALUES (?, ?, 'g', ?, ?, 'test', ?, 3)",
        (src, dst, kind, confidence, file_id),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --- migrations ---------------------------------------------------------------------------


def test_migrations_are_numbered_consecutively() -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    assert SCHEMA_VERSION == len(MIGRATIONS)


def test_fresh_database_is_migrated_to_latest(conn: sqlite3.Connection) -> None:
    assert get_schema_version(conn) == SCHEMA_VERSION
    assert {"files", "symbols", "edges", "findings"} <= table_names(conn)


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    assert migrate(conn) == SCHEMA_VERSION
    assert migrate(conn) == SCHEMA_VERSION
    assert get_schema_version(conn) == SCHEMA_VERSION


def test_non_consecutive_migrations_are_rejected(conn: sqlite3.Connection) -> None:
    bad = (Migration(1, "a", "SELECT 1;"), Migration(3, "c", "SELECT 1;"))
    with pytest.raises(ValueError, match="numbered"):
        migrate(conn, bad)


def test_failed_migration_rolls_back_completely() -> None:
    raw = sqlite3.connect(":memory:", isolation_level=None)
    good = Migration(1, "create a", "CREATE TABLE a (x INTEGER);")
    bad = Migration(2, "create b then fail", "CREATE TABLE b (x INTEGER); CREATE TABLE a (x);")

    assert migrate(raw, (good,)) == 1
    with pytest.raises(MigrationError, match="Migration 2"):
        migrate(raw, (good, bad))

    assert get_schema_version(raw) == 1
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"a"}  # table b from the failed migration must not exist
    raw.close()


def test_incremental_upgrade_applies_only_pending_migrations() -> None:
    raw = sqlite3.connect(":memory:", isolation_level=None)
    m1 = Migration(1, "one", "CREATE TABLE one (x INTEGER);")
    m2 = Migration(2, "two", "CREATE TABLE two (x INTEGER);")
    migrate(raw, (m1,))
    raw.execute("INSERT INTO one VALUES (42)")

    assert migrate(raw, (m1, m2)) == 2
    assert raw.execute("SELECT x FROM one").fetchone()[0] == 42  # data survived
    assert raw.execute("SELECT COUNT(*) FROM two").fetchone()[0] == 0
    raw.close()


# --- connection ---------------------------------------------------------------------------


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        add_symbol(conn, file_id=999)


def test_rows_are_accessible_by_column_name(conn: sqlite3.Connection) -> None:
    add_file(conn, "x.py")
    row = conn.execute("SELECT * FROM files").fetchone()
    assert row["path"] == "x.py"
    assert row["language"] == "python"
    assert row["parse_error"] is None


def test_file_database_is_created_persists_and_uses_wal(tmp_path: Path) -> None:
    db_path = default_db_path(tmp_path)
    assert db_path == tmp_path / ".glyphrift" / "index.db"

    first = connect(db_path)
    assert db_path.exists()
    assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    add_file(first, "kept.py")
    first.close()

    second = connect(db_path)
    assert get_schema_version(second) == SCHEMA_VERSION
    assert [r["path"] for r in second.execute("SELECT path FROM files")] == ["kept.py"]
    second.close()


def test_database_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    raw.close()

    with pytest.raises(DatabaseTooNewError):
        connect(db_path)


# --- transactions -------------------------------------------------------------------------


def test_transaction_commits_on_success(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        add_file(conn, "a.py")
        add_file(conn, "b.py")
    assert count(conn, "files") == 2
    assert not conn.in_transaction


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="boom"), transaction(conn):
        add_file(conn, "a.py")
        raise RuntimeError("boom")
    assert count(conn, "files") == 0
    assert not conn.in_transaction


# --- schema constraints -------------------------------------------------------------------


def test_file_paths_are_unique(conn: sqlite3.Connection) -> None:
    add_file(conn, "a.py")
    with pytest.raises(sqlite3.IntegrityError):
        add_file(conn, "a.py")


def test_symbol_kind_must_be_known(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    for kind in ("module", "class", "function", "method"):
        add_symbol(conn, file_id, name=f"s_{kind}", kind=kind)
    with pytest.raises(sqlite3.IntegrityError):
        add_symbol(conn, file_id, kind="variable")


def test_symbol_line_range_must_be_valid(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    insert = (
        "INSERT INTO symbols (file_id, kind, name, qualified_name, line_start, line_end) "
        "VALUES (?, 'function', 'f', 'f', ?, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (file_id, 10, 5))  # end before start
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (file_id, 0, 5))  # lines are 1-based


def test_edge_kind_and_confidence_must_be_known(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    a, b = add_symbol(conn, file_id, "a"), add_symbol(conn, file_id, "b")
    for kind in ("imports", "calls", "inherits", "references"):
        add_edge(conn, file_id, a, b, kind=kind)
    for confidence in ("resolved", "inferred", "heuristic"):
        add_edge(conn, file_id, a, b, confidence=confidence)
    with pytest.raises(sqlite3.IntegrityError):
        add_edge(conn, file_id, a, b, kind="mentions")
    with pytest.raises(sqlite3.IntegrityError):
        add_edge(conn, file_id, a, b, confidence="certain")


def test_edge_destination_exists_if_and_only_if_not_unresolved(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    a, b = add_symbol(conn, file_id, "a"), add_symbol(conn, file_id, "b")

    add_edge(conn, file_id, a, None, confidence="unresolved")  # fine: no destination
    add_edge(conn, file_id, a, b, confidence="heuristic")  # fine: has destination

    with pytest.raises(sqlite3.IntegrityError):
        add_edge(conn, file_id, a, None, confidence="resolved")  # claims resolved, no target
    with pytest.raises(sqlite3.IntegrityError):
        add_edge(conn, file_id, a, b, confidence="unresolved")  # claims unresolved, has target


def test_finding_constraints(conn: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO findings (type, title, severity, confidence, evidence_json, created_at) "
        "VALUES ('cycle', 't', ?, ?, ?, '2026-01-01T00:00:00Z')"
    )
    conn.execute(insert, ("medium", "resolved", '{"path": ["a", "b", "a"]}'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("catastrophic", "resolved", "{}"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("low", "resolved", "{not json"))


# --- cascades -----------------------------------------------------------------------------


def test_deleting_a_file_removes_its_symbols_and_edges(conn: sqlite3.Connection) -> None:
    f1, f2 = add_file(conn, "a.py"), add_file(conn, "b.py")
    a, b = add_symbol(conn, f1, "a"), add_symbol(conn, f2, "b")
    add_edge(conn, f1, a, b)

    conn.execute("DELETE FROM files WHERE id = ?", (f1,))

    assert count(conn, "symbols") == 1  # only b remains
    assert count(conn, "edges") == 0


def test_deleting_a_destination_symbol_removes_edges_pointing_at_it(
    conn: sqlite3.Connection,
) -> None:
    f1, f2 = add_file(conn, "a.py"), add_file(conn, "b.py")
    a, b = add_symbol(conn, f1, "a"), add_symbol(conn, f2, "b")
    add_edge(conn, f1, a, b)

    conn.execute("DELETE FROM files WHERE id = ?", (f2,))

    assert count(conn, "symbols") == 1  # a remains
    assert count(conn, "edges") == 0  # no dangling edge left behind


def test_deleting_a_parent_symbol_removes_its_children(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    cls = add_symbol(conn, file_id, "C", kind="class")
    add_symbol(conn, file_id, "m", kind="method", parent_id=cls)

    conn.execute("DELETE FROM symbols WHERE id = ?", (cls,))

    assert count(conn, "symbols") == 0


# --- migration 2: import facts ------------------------------------------------------------


def add_import_fact(
    conn: sqlite3.Connection,
    file_id: int,
    scope: int,
    kind: str = "from",
    level: int = 0,
    module: str | None = "a",
    name: str | None = "x",
) -> None:
    conn.execute(
        "INSERT INTO import_facts (file_id, scope_symbol_id, kind, level, module, name, asname, "
        "line, type_checking) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, 0)",
        (file_id, scope, kind, level, module, name),
    )


def test_import_fact_shapes_are_validated(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    scope = add_symbol(conn, file_id, "mod", kind="module")

    add_import_fact(conn, file_id, scope, "import", 0, "a.b", None)  # import a.b
    add_import_fact(conn, file_id, scope, "from", 0, "a", "x")  # from a import x
    add_import_fact(conn, file_id, scope, "from", 1, None, "x")  # from . import x
    add_import_fact(conn, file_id, scope, "from", 2, "m", "*")  # from ..m import *

    bad_shapes = [
        ("import", 0, "a", "x"),  # plain import cannot have a name
        ("from", 0, "a", None),  # from-import needs a name
        ("import", 1, "a", None),  # plain imports cannot be relative
        ("from", 0, None, "x"),  # absolute from-import needs a module
        ("require", 0, "a", None),  # unknown kind
    ]
    for kind, level, module, name in bad_shapes:
        with pytest.raises(sqlite3.IntegrityError):
            add_import_fact(conn, file_id, scope, kind, level, module, name)


def test_deleting_a_file_removes_its_import_facts(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    scope = add_symbol(conn, file_id, "mod", kind="module")
    add_import_fact(conn, file_id, scope)

    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    assert count(conn, "import_facts") == 0


def test_migrating_from_v1_clears_the_derived_index() -> None:
    raw = sqlite3.connect(":memory:", isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    assert migrate(raw, MIGRATIONS[:1]) == 1
    raw.execute(
        "INSERT INTO files (path, language, sha256, size, indexed_at) "
        "VALUES ('old.py', 'python', 'abc', 1, '2026-01-01T00:00:00Z')"
    )

    assert migrate(raw, MIGRATIONS) == SCHEMA_VERSION

    assert raw.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0  # rebuilt next index run
    raw.close()


def test_call_fact_shapes_are_validated(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    scope = add_symbol(conn, file_id, "mod", kind="module")
    insert = (
        "INSERT INTO call_facts (file_id, scope_symbol_id, root, attrs, line, "
        "opaque_in_symbol_id, receiver_class_symbol_id) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(insert, (file_id, scope, "f", "", 1, None, None))  # valid
    bad_rows = [
        (file_id, scope, None, "", 1, None, None),  # no root and no attributes
        (file_id, scope, "f", "", 0, None, None),  # line < 1
        (file_id, scope, "self", "x", 1, None, scope),  # receiver class without opaque scope
    ]
    for row in bad_rows:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, row)


def test_deleting_a_file_removes_its_call_facts(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    scope = add_symbol(conn, file_id, "mod", kind="module")
    conn.execute(
        "INSERT INTO call_facts (file_id, scope_symbol_id, root, attrs, line) "
        "VALUES (?, ?, 'f', '', 1)",
        (file_id, scope),
    )

    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    assert count(conn, "call_facts") == 0


def test_base_fact_shapes_are_validated(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    scope = add_symbol(conn, file_id, "mod", kind="module")
    cls = add_symbol(conn, file_id, "mod.A", kind="class", parent_id=scope)
    insert = (
        "INSERT INTO base_facts (file_id, class_symbol_id, scope_symbol_id, root, attrs, text, "
        "line, opaque_in_symbol_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(insert, (file_id, cls, scope, "B", "", "B", 1, None))  # valid
    conn.execute(insert, (file_id, cls, scope, None, "", "make()", 1, None))  # dynamic base: valid
    bad_rows = [
        (file_id, cls, scope, None, "x", "x", 1, None),  # attributes without a root
        (file_id, cls, scope, "B", "", "B", 0, None),  # line < 1
    ]
    for row in bad_rows:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, row)


def test_deleting_a_class_removes_its_base_facts(conn: sqlite3.Connection) -> None:
    file_id = add_file(conn)
    scope = add_symbol(conn, file_id, "mod", kind="module")
    cls = add_symbol(conn, file_id, "mod.A", kind="class", parent_id=scope)
    conn.execute(
        "INSERT INTO base_facts (file_id, class_symbol_id, scope_symbol_id, root, attrs, text, "
        "line) VALUES (?, ?, ?, 'B', '', 'B', 1)",
        (file_id, cls, scope),
    )

    conn.execute("DELETE FROM symbols WHERE id = ?", (cls,))

    assert count(conn, "base_facts") == 0
