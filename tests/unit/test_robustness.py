"""Hostile and odd inputs must never abort an indexing run."""

import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from glyphrift.indexer import extract_symbols, index_repo, walk_repo
from glyphrift.store import connect


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def write(root: Path, rel: str, content: bytes | str = b"") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def file_rows(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        r["path"]: r["parse_error"] for r in conn.execute("SELECT path, parse_error FROM files")
    }


# --- extraction: bad source is reported, never raised --------------------------------------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"x = 1\x00\ndef f(): pass\n", "null bytes"),
        (b"x = '\xff\xfe'\n", "unicode error"),
        (b"# -*- coding: nonsense -*-\ndef f(): pass\n", "unknown encoding"),
        ("def f(): pass\n".encode("utf-16"), "null bytes"),
        (b"print 'python 2'\n", "SyntaxError"),
        (b"def f(:\n", "SyntaxError"),
        ("x = " + "(" * 500 + "1" + ")" * 500 + "\n", "nested"),
        ("x = " + "+".join(["1"] * 30000) + "\n", "RecursionError"),
        ("x = a" + ".b()" * 8000 + "\n", "RecursionError"),
    ],
)
def test_unparseable_source_gives_a_module_symbol_and_an_error(
    source: bytes | str, message: str
) -> None:
    result = extract_symbols(source, "m")

    assert result.parse_error is not None
    assert message in result.parse_error
    assert [s.kind for s in result.symbols] == ["module"]
    assert result.imports == result.calls == result.bases == ()


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"# only a comment",
        b"\xef\xbb\xbfdef f(): pass\n",  # UTF-8 BOM
        "# -*- coding: latin-1 -*-\ndef f(): x = 'é'\n".encode("latin-1"),
        b"def f():\r\n\tpass\r\n\x0c\r\ndef g(): pass\r\n",  # CRLF, tab, form feed
    ],
)
def test_odd_but_valid_source_parses(source: bytes) -> None:
    assert extract_symbols(source, "m").parse_error is None


def test_deeply_nested_base_expressions_do_not_raise() -> None:
    # How deep Python's own parser can go depends on the Python version and platform: it may
    # report a RecursionError itself. Either way, extraction must return a result, never raise.
    subscripts = "class A(B" + "[int]" * 3000 + "): pass\n"
    attributes = "class A(" + ".".join(["a"] * 5000) + "): pass\n"
    for source in (subscripts, attributes):
        result = extract_symbols(source, "m")
        if result.parse_error is None:
            assert [s.name for s in result.symbols] == ["m", "A"]
            assert len(result.bases) == 1
        else:
            assert "RecursionError" in result.parse_error
            assert [s.name for s in result.symbols] == ["m"]


def test_long_elif_chain_does_not_overflow_the_stack() -> None:
    source = "def f(a):\n    if a: pass\n" + "    elif a: g()\n" * 3000
    result = extract_symbols(source, "m")  # must not raise, whatever the parser's limits are
    if result.parse_error is None:
        assert len(result.calls) == 3000
    else:
        assert "RecursionError" in result.parse_error


# --- pipeline -------------------------------------------------------------------------------


def test_broken_file_is_recorded_and_others_still_get_edges(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "broken.py", "def f(:\n")
    write(tmp_path, "good.py", "import broken\ndef g(): pass\n")

    stats = index_repo(tmp_path, conn)

    assert [path for path, _ in stats.parse_errors] == ["broken.py"]
    rows = file_rows(conn)
    assert rows["broken.py"] is not None and rows["good.py"] is None
    edge = conn.execute(
        "SELECT dst.qualified_name FROM edges e JOIN symbols dst ON dst.id = e.dst_symbol_id "
        "WHERE e.kind = 'imports'"
    ).fetchone()
    assert edge[0] == "broken"  # the module exists even though its contents could not be read


def test_fixing_a_broken_file_clears_its_error(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "m.py", "def f(:\n")
    index_repo(tmp_path, conn)
    assert file_rows(conn)["m.py"] is not None

    write(tmp_path, "m.py", "def f(): pass\n")
    stats = index_repo(tmp_path, conn)

    assert stats.files_updated == 1
    assert file_rows(conn)["m.py"] is None


def test_oversized_file_is_recorded_but_not_parsed(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "big.py", "def f(): pass\n" + "# padding\n" * 200)
    write(tmp_path, "small.py", "def g(): pass\n")

    stats = index_repo(tmp_path, conn, max_file_bytes=500)

    rows = file_rows(conn)
    assert "too large" in (rows["big.py"] or "")
    assert rows["small.py"] is None
    names = {r["qualified_name"] for r in conn.execute("SELECT qualified_name FROM symbols")}
    assert names == {"big", "small", "small.g"}
    assert [path for path, _ in stats.parse_errors] == ["big.py"]
    size = conn.execute("SELECT size FROM files WHERE path = 'big.py'").fetchone()[0]
    assert size == (tmp_path / "big.py").stat().st_size

    # Unchanged on the next run, and parsed once the limit allows it.
    assert index_repo(tmp_path, conn, max_file_bytes=500).files_unchanged == 2
    index_repo(tmp_path, conn)  # default limit: still the same bytes, so still "unchanged"
    assert "too large" in (file_rows(conn)["big.py"] or "")


def test_directories_named_like_python_files_are_ignored(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    (tmp_path / "dir.py").mkdir()
    write(tmp_path, "dir.py/inner.py", "def f(): pass\n")

    index_repo(tmp_path, conn)

    assert set(file_rows(conn)) == {"dir.py/inner.py"}


def test_symlinks_are_not_followed(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "real.py", "def f(): pass\n")
    try:
        os.symlink(tmp_path / "real.py", tmp_path / "link.py")
        os.symlink(tmp_path / "missing.py", tmp_path / "broken.py")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")

    index_repo(tmp_path, conn)

    assert set(file_rows(conn)) == {"real.py"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_named_pipes_are_skipped_without_blocking(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "real.py", "def f(): pass\n")
    os.mkfifo(tmp_path / "pipe.py")

    index_repo(tmp_path, conn)

    assert set(file_rows(conn)) == {"real.py"}


def test_unusual_but_valid_file_names_are_indexed(tmp_path: Path, conn: sqlite3.Connection) -> None:
    for name in ("my-file.py", "2fast.py", "a b.py", "ünï.py", ".hidden.py"):
        write(tmp_path, name, "def f(): pass\n")

    index_repo(tmp_path, conn)

    assert set(file_rows(conn)) == {"my-file.py", "2fast.py", "a b.py", "ünï.py", ".hidden.py"}


def test_file_name_that_is_not_valid_utf8_is_skipped_with_a_warning(
    tmp_path: Path, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    write(tmp_path, "good.py", "def f(): pass\n")
    try:
        with open(os.fsencode(tmp_path) + b"/bad\xffname.py", "wb") as handle:
            handle.write(b"def z(): pass\n")
    except (OSError, ValueError, UnicodeError):
        pytest.skip("file system does not allow non-UTF-8 names")
    try:
        for name in os.listdir(tmp_path):
            name.encode("utf-8")
    except UnicodeEncodeError:
        pass  # the file system kept the invalid name: this is the case under test
    else:
        pytest.skip("the OS converted the name to valid text (e.g. Windows)")

    with caplog.at_level(logging.WARNING):
        index_repo(tmp_path, conn)

    assert set(file_rows(conn)) == {"good.py"}
    assert "not valid UTF-8" in caplog.text
    assert [r.path for r in walk_repo(tmp_path)] == ["good.py"]
