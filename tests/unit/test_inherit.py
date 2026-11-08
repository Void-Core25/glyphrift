import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from glyphrift.indexer import extract_symbols, index_repo
from glyphrift.store import connect


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def write(root: Path, rel: str, content: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def inherits(conn: sqlite3.Connection, src: str | None = None) -> list[sqlite3.Row]:
    return [
        row
        for row in conn.execute(
            "SELECT s.qualified_name AS src, e.dst_name, d.qualified_name AS dst, "
            "e.confidence, e.how, e.line "
            "FROM edges e JOIN symbols s ON s.id = e.src_symbol_id "
            "LEFT JOIN symbols d ON d.id = e.dst_symbol_id "
            "WHERE e.kind = 'inherits' ORDER BY e.id"
        )
        if src is None or row["src"] == src
    ]


def run(tmp_path: Path, conn: sqlite3.Connection, files: dict[str, str]) -> None:
    for rel, content in files.items():
        write(tmp_path, rel, content)
    index_repo(tmp_path, conn)


# --- extraction ---------------------------------------------------------------------------


def test_extraction_records_bases_in_order() -> None:
    source = "class A(B, m.C, Generic[T], make(), metaclass=M): pass\n"
    result = extract_symbols(source, "mod")

    assert [(b.root, b.attrs, b.text) for b in result.bases] == [
        ("B", "", "B"),
        ("m", "C", "m.C"),
        ("Generic", "", "Generic[T]"),
        (None, "", "make()"),
    ]
    assert all(b.cls == 1 and b.scope == 0 and b.line == 1 for b in result.bases)


def test_extraction_notes_when_a_base_is_a_local_variable() -> None:
    result = extract_symbols("def f(P):\n    class Q(P): pass\n", "mod")
    assert [(b.root, b.opaque_in) for b in result.bases] == [("P", 1)]


def test_extraction_records_super_receiver_class() -> None:
    source = "class A:\n    def f(self):\n        super().f()\n"
    result = extract_symbols(source, "mod")
    call = next(c for c in result.calls if c.root == "super()")
    assert (call.receiver_class, call.opaque_in) == (1, 2)


def test_super_outside_a_method_has_no_receiver_class() -> None:
    result = extract_symbols("def f():\n    super().g()\n", "mod")
    call = next(c for c in result.calls if c.root == "super()")
    assert call.receiver_class is None


# --- resolution ---------------------------------------------------------------------------


def test_base_in_same_module(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "class A: pass\nclass B(A): pass\n"})
    rows = inherits(conn)
    assert [(r["src"], r["dst"], r["confidence"], r["line"]) for r in rows] == [
        ("m.B", "m.A", "resolved", 2)
    ]
    assert "class m.B inherits from `A`" in rows[0]["how"]


def test_base_through_import_module_attribute_and_reexport(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "pkg/__init__.py": "from .base import Base\n",
            "pkg/base.py": "class Base: pass\n",
            "a.py": "from pkg import Base\nclass A(Base): pass\n",
            "b.py": "import pkg.base\nclass B(pkg.base.Base): pass\n",
        },
    )
    assert {(r["src"], r["dst"], r["confidence"]) for r in inherits(conn)} == {
        ("a.A", "pkg.base.Base", "resolved"),
        ("b.B", "pkg.base.Base", "resolved"),
    }


def test_edges_keep_base_order(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "class A: pass\nclass B: pass\nclass C(B, A): pass\n"})
    assert [r["dst"] for r in inherits(conn, "m.C")] == ["m.B", "m.A"]


def test_generic_subscript_base_is_looked_through(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "class Box: pass\nclass Sub(Box[int]): pass\n"})
    assert [(r["dst"], r["dst_name"]) for r in inherits(conn, "m.Sub")] == [("m.Box", "Box[int]")]


def test_stdlib_third_party_and_builtin_bases_are_unresolved_with_reasons(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "import threading\nimport requests\n"
            "class A(threading.Thread): pass\n"
            "class B(requests.Session): pass\n"
            "class C(Exception): pass\n"
        },
    )
    by_src = {r["src"]: r for r in inherits(conn)}
    assert {r["confidence"] for r in by_src.values()} == {"unresolved"}
    assert "threading" in by_src["m.A"]["how"]
    assert "requests" in by_src["m.B"]["how"]
    assert "builtin class" in by_src["m.C"]["how"]


def test_object_base_is_skipped(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "class A(object): pass\n"})
    assert inherits(conn) == []


def test_dynamic_bases_are_unresolved(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "def f(P):\n    class Q(P): pass\nclass R(make()): pass\nclass S(nope): pass\n"},
    )
    by_src = {r["src"]: r for r in inherits(conn)}
    assert "parameter" in by_src["m.f.Q"]["how"]
    assert "dynamic expression" in by_src["m.R"]["how"]
    assert "not defined in the repository" in by_src["m.S"]["how"]
    assert all(r["dst"] is None for r in by_src.values())


def test_base_that_is_not_a_class_is_unresolved(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "def helper(): pass\nclass A(helper): pass\n"})
    (row,) = inherits(conn)
    assert row["dst"] is None
    assert "not a class" in row["how"]


def test_conditional_base_definitions_are_inferred(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "import sys\nif sys.platform == 'x':\n    class Base: pass\n"
            "else:\n    class Base: pass\nclass A(Base): pass\n"
        },
    )
    assert {r["confidence"] for r in inherits(conn, "m.A")} == {"inferred"}
    assert len(inherits(conn, "m.A")) == 2


def test_inherits_edges_are_rebuilt_when_a_file_changes(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {"base.py": "class Base: pass\n", "a.py": "from base import Base\nclass A(Base): pass\n"},
    )
    assert [r["dst"] for r in inherits(conn, "a.A")] == ["base.Base"]

    write(tmp_path, "base.py", "class Other: pass\n")  # Base disappears
    index_repo(tmp_path, conn)

    (row,) = inherits(conn, "a.A")
    assert row["dst"] is None
    bad = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE (confidence = 'unresolved') != (dst_symbol_id IS NULL)"
    ).fetchone()[0]
    assert bad == 0


def test_stats_report_inheritance_edges(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "m.py", "class A: pass\nclass B(A, Exception): pass\n")
    stats = index_repo(tmp_path, conn)
    assert (stats.inherit_edges, stats.unresolved_bases) == (2, 1)
