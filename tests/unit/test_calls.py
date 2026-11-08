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


def write(root: Path, rel: str, content: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def calls(conn: sqlite3.Connection, src: str | None = None) -> list[sqlite3.Row]:
    return [
        row
        for row in conn.execute(
            "SELECT s.qualified_name AS src, e.dst_name, d.qualified_name AS dst, "
            "e.confidence, e.how, e.line "
            "FROM edges e JOIN symbols s ON s.id = e.src_symbol_id "
            "LEFT JOIN symbols d ON d.id = e.dst_symbol_id "
            "WHERE e.kind = 'calls' ORDER BY e.id"
        )
        if src is None or row["src"] == src
    ]


def found(conn: sqlite3.Connection, src: str) -> set[tuple[str | None, str]]:
    return {(row["dst"], row["confidence"]) for row in calls(conn, src)}


def run(tmp_path: Path, conn: sqlite3.Connection, files: dict[str, str]) -> None:
    for rel, content in files.items():
        write(tmp_path, rel, content)
    index_repo(tmp_path, conn)


def test_call_to_function_in_same_module(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "def a(): pass\ndef b():\n    a()\n"})
    assert found(conn, "m.b") == {("m.a", "resolved")}
    assert calls(conn)[0]["line"] == 3


def test_call_through_from_import_and_reexport(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {
            "pkg/__init__.py": "from .util import helper\n",
            "pkg/util.py": "def helper(): pass\n",
            "app.py": "from pkg import helper\ndef go():\n    helper()\n",
        },
    )
    rows = calls(conn, "app.go")
    assert [(r["dst"], r["confidence"]) for r in rows] == [("pkg.util.helper", "resolved")]
    assert "re-exported" in rows[0]["how"]


def test_call_through_module_attribute(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(): pass\n",
            "app.py": "import pkg.util\ndef go():\n    pkg.util.helper()\n",
        },
    )
    assert found(conn, "app.go") == {("pkg.util.helper", "resolved")}


def test_class_instantiation_and_class_method_call(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "class Box:\n    def close(self): pass\n"
            "def go(b):\n    Box()\n    Box.close(b)\n",
        },
    )
    assert found(conn, "m.go") == {("m.Box", "resolved"), ("m.Box.close", "resolved")}
    assert any("class instantiation" in r["how"] for r in calls(conn, "m.go"))


def test_self_method_is_inferred(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "class A:\n    def x(self):\n        self.y()\n    def y(self): pass\n"},
    )
    assert found(conn, "m.A.x") == {("m.A.y", "inferred")}


def test_self_method_not_in_class_is_unresolved(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "class A:\n    def x(self):\n        self.nope()\n"})
    assert found(conn, "m.A.x") == {(None, "unresolved")}


def test_parameter_shadows_module_function(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "def a(): pass\ndef b(a):\n    a()\n"})
    rows = calls(conn, "m.b")
    assert [(r["dst"], r["confidence"]) for r in rows] == [(None, "unresolved")]
    assert "local variable or parameter" in rows[0]["how"]


def test_nested_function_and_local_import(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {
            "u.py": "def other(): pass\n",
            "m.py": "def f():\n    def inner(): pass\n    inner()\n"
            "    from u import other\n    other()\n",
        },
    )
    assert found(conn, "m.f") == {("m.f.inner", "resolved"), ("u.other", "resolved")}


def test_unknown_receiver_is_heuristic_by_method_name(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "class A:\n    def save(self): pass\ndef go(x):\n    x.save()\n"},
    )
    rows = calls(conn, "m.go")
    assert [(r["dst"], r["confidence"]) for r in rows] == [("m.A.save", "heuristic")]
    assert "name only" in rows[0]["how"]


def test_unknown_receiver_with_too_many_candidates_is_unresolved(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    classes = "".join(f"class C{i}:\n    def save(self): pass\n" for i in range(6))
    run(tmp_path, conn, {"m.py": classes + "def go(x):\n    x.save()\n"})
    assert found(conn, "m.go") == {(None, "unresolved")}


def test_builtin_method_names_and_unknown_names_are_not_guessed(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "class A:\n    def append(self): pass\ndef go(x):\n    x.append(1)\n    len(x)\n"},
    )
    assert calls(conn, "m.go") == []


def test_stdlib_calls_are_not_recorded_but_third_party_are_unresolved(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "import os\nimport requests\n"
            "def go():\n    os.path.join('a')\n    requests.get('u')\n"
        },
    )
    rows = calls(conn, "m.go")
    assert [(r["dst"], r["confidence"]) for r in rows] == [(None, "unresolved")]
    assert "requests" in rows[0]["how"]


def test_undefined_name_is_unresolved(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "def go():\n    mystery()\n"})
    assert found(conn, "m.go") == {(None, "unresolved")}


def test_super_call_resolves_to_base_class_method(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "class A:\n    def f(self): pass\n"
            "class B(A):\n    def f(self):\n        super().f()\n"
        },
    )
    rows = calls(conn, "m.B.f")
    assert [(r["dst"], r["confidence"]) for r in rows] == [("m.A.f", "inferred")]
    assert "base class m.A" in rows[0]["how"]


def test_super_call_skips_the_own_class_and_reports_external_bases(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "import threading\n"
            "class B(threading.Thread):\n    def run(self):\n        super().run()\n"
        },
    )
    rows = calls(conn, "m.B.run")
    assert [(r["dst"], r["confidence"]) for r in rows] == [(None, "unresolved")]
    assert "threading.Thread" in rows[0]["how"]


def test_self_call_finds_method_inherited_through_several_levels(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {
            "base.py": "class Root:\n    def helper(self): pass\n",
            "mid.py": "from base import Root\nclass Mid(Root): pass\n",
            "leaf.py": "import mid\nclass Leaf(mid.Mid):\n"
            "    def go(self):\n        self.helper()\n",
        },
    )
    rows = calls(conn, "leaf.Leaf.go")
    assert [(r["dst"], r["confidence"]) for r in rows] == [("base.Root.helper", "inferred")]
    assert "inherited from base class base.Root" in rows[0]["how"]


def test_own_method_wins_over_inherited_one(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {
            "m.py": "class A:\n    def f(self): pass\n"
            "class B(A):\n    def f(self): pass\n    def g(self):\n        self.f()\n"
        },
    )
    assert found(conn, "m.B.g") == {("m.B.f", "inferred")}


def test_class_attribute_call_finds_inherited_method(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "class A:\n    def f(self): pass\nclass B(A): pass\ndef go(b):\n    B.f(b)\n"},
    )
    rows = calls(conn, "m.go")
    assert [(r["dst"], r["confidence"]) for r in rows] == [("m.A.f", "inferred")]


def test_missing_self_method_mentions_external_bases(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "class E(Exception):\n    def go(self):\n        self.with_traceback(None)\n"},
    )
    rows = calls(conn, "m.E.go")
    assert [(r["dst"], r["confidence"]) for r in rows] == [(None, "unresolved")]
    assert "external base class(es): Exception" in rows[0]["how"]


def test_cyclic_inheritance_does_not_hang(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(
        tmp_path,
        conn,
        {"m.py": "class A(B):\n    def f(self):\n        self.nope()\nclass B(A): pass\n"},
    )
    assert found(conn, "m.A.f") == {(None, "unresolved")}


def test_invariant_and_rebuild_on_change(tmp_path: Path, conn: sqlite3.Connection) -> None:
    run(tmp_path, conn, {"m.py": "def a(): pass\ndef b():\n    a()\n"})
    first = len(calls(conn))
    write(tmp_path, "m.py", "def a(): pass\ndef b():\n    a()\n    a()\n")
    index_repo(tmp_path, conn)

    assert len(calls(conn)) == first + 1
    bad = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE (confidence = 'unresolved') != (dst_symbol_id IS NULL)"
    ).fetchone()[0]
    assert bad == 0


def test_stats_report_call_edges(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "m.py", "def a(): pass\ndef b():\n    a()\n    mystery()\n")
    stats = index_repo(tmp_path, conn)
    assert (stats.call_edges, stats.unresolved_calls) == (2, 1)
