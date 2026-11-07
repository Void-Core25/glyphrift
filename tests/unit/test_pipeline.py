import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from glyphrift.indexer import index_repo
from glyphrift.store import connect


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def write(root: Path, rel: str, content: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def make_repo(root: Path) -> None:
    write(root, "pkg/__init__.py", "")
    write(
        root,
        "pkg/mod.py",
        "def top():\n    pass\n\nclass Greeter:\n    def hello(self):\n        pass\n",
    )
    write(root, "README.md", "# not python")
    write(root, ".venv/lib/ignored.py", "def ignored():\n    pass\n")


def file_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["path"]: r for r in conn.execute("SELECT * FROM files")}


def qualified_names(conn: sqlite3.Connection, path: str) -> list[str]:
    rows = conn.execute(
        "SELECT s.qualified_name FROM symbols s JOIN files f ON f.id = s.file_id "
        "WHERE f.path = ? ORDER BY s.id",
        (path,),
    )
    return [r[0] for r in rows]


def test_indexes_python_files_and_symbols(tmp_path: Path, conn: sqlite3.Connection) -> None:
    make_repo(tmp_path)

    stats = index_repo(tmp_path, conn)

    assert set(file_rows(conn)) == {"pkg/__init__.py", "pkg/mod.py"}  # no README, no .venv
    assert qualified_names(conn, "pkg/__init__.py") == ["pkg"]
    assert qualified_names(conn, "pkg/mod.py") == [
        "pkg.mod",
        "pkg.mod.top",
        "pkg.mod.Greeter",
        "pkg.mod.Greeter.hello",
    ]
    assert (stats.files_added, stats.files_updated, stats.files_unchanged) == (2, 0, 0)
    assert stats.symbols_written == 5
    assert stats.parse_errors == []


def test_stored_file_metadata_and_parent_links(tmp_path: Path, conn: sqlite3.Connection) -> None:
    make_repo(tmp_path)
    index_repo(tmp_path, conn)

    row = file_rows(conn)["pkg/mod.py"]
    assert row["language"] == "python"
    assert row["size"] == (tmp_path / "pkg/mod.py").stat().st_size
    assert len(row["sha256"]) == 64
    assert row["parse_error"] is None
    assert row["indexed_at"].endswith("+00:00")

    parents = {
        r["qualified_name"]: r["parent"]
        for r in conn.execute(
            "SELECT s.qualified_name, p.qualified_name AS parent FROM symbols s "
            "LEFT JOIN symbols p ON p.id = s.parent_id"
        )
    }
    assert parents["pkg.mod"] is None
    assert parents["pkg.mod.top"] == "pkg.mod"
    assert parents["pkg.mod.Greeter.hello"] == "pkg.mod.Greeter"


def test_rerun_without_changes_touches_nothing(tmp_path: Path, conn: sqlite3.Connection) -> None:
    make_repo(tmp_path)
    index_repo(tmp_path, conn)
    ids_before = {p: r["id"] for p, r in file_rows(conn).items()}

    stats = index_repo(tmp_path, conn)

    assert (stats.files_added, stats.files_updated, stats.files_unchanged) == (0, 0, 2)
    assert stats.symbols_written == 0
    assert {p: r["id"] for p, r in file_rows(conn).items()} == ids_before


def test_changed_file_is_reindexed_and_others_are_untouched(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    make_repo(tmp_path)
    index_repo(tmp_path, conn)
    init_id = file_rows(conn)["pkg/__init__.py"]["id"]

    write(tmp_path, "pkg/mod.py", "def renamed():\n    pass\n")
    stats = index_repo(tmp_path, conn)

    assert (stats.files_added, stats.files_updated, stats.files_unchanged) == (0, 1, 1)
    assert qualified_names(conn, "pkg/mod.py") == ["pkg.mod", "pkg.mod.renamed"]
    assert file_rows(conn)["pkg/__init__.py"]["id"] == init_id


def test_new_file_is_added(tmp_path: Path, conn: sqlite3.Connection) -> None:
    make_repo(tmp_path)
    index_repo(tmp_path, conn)

    write(tmp_path, "pkg/extra.py", "def extra():\n    pass\n")
    stats = index_repo(tmp_path, conn)

    assert (stats.files_added, stats.files_unchanged) == (1, 2)
    assert qualified_names(conn, "pkg/extra.py") == ["pkg.extra", "pkg.extra.extra"]


def test_deleted_file_is_removed_with_its_symbols(tmp_path: Path, conn: sqlite3.Connection) -> None:
    make_repo(tmp_path)
    index_repo(tmp_path, conn)

    (tmp_path / "pkg/mod.py").unlink()
    stats = index_repo(tmp_path, conn)

    assert stats.files_removed == 1
    assert set(file_rows(conn)) == {"pkg/__init__.py"}
    assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 1  # just pkg


def test_syntax_error_is_recorded_and_does_not_stop_indexing(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "good.py", "def ok():\n    pass\n")
    write(tmp_path, "bad.py", "def broken(:\n    pass\n")

    stats = index_repo(tmp_path, conn)

    assert [path for path, _ in stats.parse_errors] == ["bad.py"]
    assert file_rows(conn)["bad.py"]["parse_error"].startswith("SyntaxError")
    assert file_rows(conn)["good.py"]["parse_error"] is None
    assert qualified_names(conn, "bad.py") == ["bad"]  # module symbol only
    assert qualified_names(conn, "good.py") == ["good", "good.ok"]


def test_fixing_a_syntax_error_clears_it(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "mod.py", "def broken(:\n    pass\n")
    index_repo(tmp_path, conn)
    assert file_rows(conn)["mod.py"]["parse_error"] is not None

    write(tmp_path, "mod.py", "def fixed():\n    pass\n")
    stats = index_repo(tmp_path, conn)

    assert stats.parse_errors == []
    assert file_rows(conn)["mod.py"]["parse_error"] is None
    assert qualified_names(conn, "mod.py") == ["mod", "mod.fixed"]


def test_adding_an_init_file_renames_modules_of_unchanged_siblings(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/mod.py", "def f():\n    pass\n")
    index_repo(tmp_path, conn)
    assert qualified_names(conn, "pkg/mod.py") == ["mod", "mod.f"]  # pkg is not a package yet

    write(tmp_path, "pkg/__init__.py", "")
    stats = index_repo(tmp_path, conn)

    assert stats.files_updated == 1  # mod.py was re-indexed although its content is unchanged
    assert qualified_names(conn, "pkg/mod.py") == ["pkg.mod", "pkg.mod.f"]


def test_non_utf8_source_with_encoding_declaration_is_indexed(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(
        tmp_path,
        "legacy.py",
        "# -*- coding: latin-1 -*-\ndef caf\xe9():\n    return 1\n".encode("latin-1"),
    )
    # latin-1 'é' is not a valid identifier byte sequence in utf-8, but the declaration wins
    stats = index_repo(tmp_path, conn)
    assert stats.parse_errors == []
    assert qualified_names(conn, "legacy.py") == ["legacy", "legacy.café"]


def test_failed_run_leaves_the_index_unchanged(tmp_path: Path, conn: sqlite3.Connection) -> None:
    make_repo(tmp_path)
    index_repo(tmp_path, conn)
    before = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    # A repository path that does not exist must fail before touching the database.
    with pytest.raises(NotADirectoryError):
        index_repo(tmp_path / "missing", conn)

    assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == before
    assert not conn.in_transaction
