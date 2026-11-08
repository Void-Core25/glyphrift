import sqlite3
import sys
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


def edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT src.qualified_name AS src, e.dst_name, dst.qualified_name AS dst, "
        "e.confidence, e.how, e.line "
        "FROM edges e JOIN symbols src ON src.id = e.src_symbol_id "
        "LEFT JOIN symbols dst ON dst.id = e.dst_symbol_id "
        "WHERE e.kind = 'imports' ORDER BY e.id"
    ).fetchall()


def targets(conn: sqlite3.Connection, src: str | None = None) -> set[tuple[str | None, str]]:
    """(destination qualified name or None, confidence) for edges, optionally from one source."""
    return {(e["dst"], e["confidence"]) for e in edges(conn) if src is None or e["src"] == src}


def index(root: Path, conn: sqlite3.Connection) -> None:
    index_repo(root, conn)


# --- absolute imports ---------------------------------------------------------------------


def test_import_of_a_module_in_the_repo(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/util.py", "def helper(): pass\n")
    write(tmp_path, "app.py", "import pkg.util\nimport pkg.util as u\n")
    index(tmp_path, conn)

    found = [e for e in edges(conn) if e["src"] == "app"]
    assert {(e["dst"], e["dst_name"], e["confidence"]) for e in found} == {
        ("pkg.util", "pkg.util", "resolved")
    }
    assert len(found) == 2  # one per statement (different lines)
    assert "imports module pkg.util" in found[0]["how"]


def test_from_import_of_a_function_gives_module_and_symbol_edges(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/util.py", "def helper(): pass\nclass Tool: pass\n")
    write(tmp_path, "app.py", "from pkg.util import helper, Tool as T\n")
    index(tmp_path, conn)

    found = [e for e in edges(conn) if e["src"] == "app"]
    assert {(e["dst"], e["dst_name"], e["confidence"]) for e in found} == {
        ("pkg.util", "pkg.util", "resolved"),  # the module (one edge, not one per name)
        ("pkg.util.helper", "helper", "resolved"),
        ("pkg.util.Tool", "Tool", "resolved"),
    }
    helper = next(e for e in found if e["dst"] == "pkg.util.helper")
    assert helper["how"] == "from pkg.util import helper: defined in pkg.util"
    assert helper["line"] == 1


def test_from_import_of_a_variable_gives_only_the_module_edge(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "config.py", "DEBUG = True\n")
    write(tmp_path, "app.py", "from config import DEBUG\n")
    index(tmp_path, conn)

    assert targets(conn, "app") == {("config", "resolved")}


def test_from_package_import_submodule(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/util.py")
    write(tmp_path, "app.py", "from pkg import util\n")
    index(tmp_path, conn)

    found = {(e["dst"], e["confidence"]): e["how"] for e in edges(conn) if e["src"] == "app"}
    assert set(found) == {("pkg", "resolved"), ("pkg.util", "resolved")}
    assert found[("pkg.util", "resolved")].endswith("submodule of pkg")


def test_multiple_names_from_one_statement_give_one_module_edge(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "m.py", "def a(): pass\ndef b(): pass\n")
    write(tmp_path, "app.py", "from m import a, b\n")
    index(tmp_path, conn)

    module_edges = [e for e in edges(conn) if e["src"] == "app" and e["dst"] == "m"]
    assert len(module_edges) == 1


# --- relative imports ---------------------------------------------------------------------


def test_relative_imports(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/core.py", "class Core: pass\n")
    write(tmp_path, "pkg/sub/__init__.py")
    write(tmp_path, "pkg/sub/other.py")
    write(
        tmp_path,
        "pkg/sub/mod.py",
        "from . import other\nfrom .other import something\nfrom ..core import Core\n"
        "from .. import core\n",
    )
    index(tmp_path, conn)

    found = {(e["dst_name"], e["dst"]) for e in edges(conn) if e["src"] == "pkg.sub.mod"}
    assert found >= {
        (".", "pkg.sub"),  # from . import other: depends on the package itself
        ("other", "pkg.sub.other"),  # ...and the submodule
        (".other", "pkg.sub.other"),
        ("..core", "pkg.core"),
        ("Core", "pkg.core.Core"),
        ("..", "pkg"),
        ("core", "pkg.core"),
    }
    assert all(e["confidence"] == "resolved" for e in edges(conn) if e["src"] == "pkg.sub.mod")


def test_relative_import_in_an_init_file_does_not_create_a_self_edge(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/__init__.py", "from . import util\n")
    write(tmp_path, "pkg/util.py")
    index(tmp_path, conn)

    assert targets(conn, "pkg") == {("pkg.util", "resolved")}  # util only, not pkg -> pkg


def test_relative_import_beyond_the_top_level_is_unresolved_with_a_reason(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/mod.py", "from .. import nothing\n")
    write(tmp_path, "script.py", "from . import sibling\n")
    index(tmp_path, conn)

    by_src = {e["src"]: e for e in edges(conn)}
    assert by_src["pkg.mod"]["confidence"] == "unresolved"
    assert "beyond the top-level package" in by_src["pkg.mod"]["how"]
    assert by_src["script"]["confidence"] == "unresolved"
    assert "top-level module" in by_src["script"]["how"]


# --- re-exports ---------------------------------------------------------------------------


def test_name_re_exported_by_a_package_init_is_followed_to_its_definition(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/__init__.py", "from .core import Thing\n")
    write(tmp_path, "pkg/core.py", "class Thing: pass\n")
    write(tmp_path, "app.py", "from pkg import Thing\n")
    index(tmp_path, conn)

    thing = next(e for e in edges(conn) if e["src"] == "app" and e["dst"] == "pkg.core.Thing")
    assert thing["confidence"] == "resolved"
    assert thing["how"] == "from pkg import Thing: defined in pkg.core, re-exported via pkg"


def test_re_export_chain_with_alias(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "a/__init__.py", "from .b import Renamed\n")
    write(tmp_path, "a/b/__init__.py", "from .c import Original as Renamed\n")
    write(tmp_path, "a/b/c.py", "class Original: pass\n")
    write(tmp_path, "app.py", "from a import Renamed\n")
    index(tmp_path, conn)

    hit = next(e for e in edges(conn) if e["src"] == "app" and e["dst"] == "a.b.c.Original")
    assert hit["confidence"] == "resolved"
    assert "re-exported via a > a.b" in hit["how"]


def test_name_arriving_via_star_import_is_inferred(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/__init__.py", "from .core import *\n")
    write(tmp_path, "pkg/core.py", "class Thing: pass\n")
    write(tmp_path, "app.py", "from pkg import Thing\n")
    index(tmp_path, conn)

    hit = next(e for e in edges(conn) if e["src"] == "app" and e["dst"] == "pkg.core.Thing")
    assert hit["confidence"] == "inferred"
    assert "star import" in hit["how"]
    # the star import itself is an ordinary module edge
    assert ("pkg.core", "resolved") in targets(conn, "pkg")


def test_circular_re_exports_terminate(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "a.py", "from b import x\n")
    write(tmp_path, "b.py", "from a import x\n")
    write(tmp_path, "app.py", "from a import x\n")
    index(tmp_path, conn)  # must not hang or crash

    assert targets(conn, "app") == {("a", "resolved")}  # no symbol defines x


def test_conditional_definitions_give_an_inferred_edge_to_each_definition(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(
        tmp_path,
        "compat.py",
        "try:\n    from fast import speedy\nexcept ImportError:\n    def speedy(): pass\n"
        "if True:\n    def other(): pass\nelse:\n    def other(): pass\n",
    )
    write(tmp_path, "app.py", "from compat import other\n")
    index(tmp_path, conn)

    found = [e for e in edges(conn) if e["src"] == "app" and e["dst"] == "compat.other"]
    assert len(found) == 2
    assert {e["confidence"] for e in found} == {"inferred"}
    assert "defined 2 times" in found[0]["how"]


# --- external and ambiguous ---------------------------------------------------------------


def test_external_modules_are_unresolved_with_a_reason(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "app.py", "import os\nimport numpy\nfrom collections import OrderedDict\n")
    index(tmp_path, conn)

    found = {e["dst_name"]: e for e in edges(conn)}
    assert set(found) == {"os", "numpy", "collections"}
    for edge in found.values():
        assert edge["dst"] is None
        assert edge["confidence"] == "unresolved"
    assert "standard library module 'os'" in found["os"]["how"]
    assert "'numpy' not found in the repository" in found["numpy"]["how"]
    assert "standard library" in found["collections"]["how"]
    assert "os" in sys.stdlib_module_names


def test_future_imports_create_no_edges(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "app.py", "from __future__ import annotations\n")
    index(tmp_path, conn)

    assert edges(conn) == []


def test_ambiguous_module_names_give_heuristic_edges_to_every_candidate(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "one/helpers.py")  # neither directory is a package, so both are `helpers`
    write(tmp_path, "two/helpers.py")
    write(tmp_path, "app.py", "import helpers\n")
    index(tmp_path, conn)

    found = [e for e in edges(conn) if e["src"] == "app"]
    assert len(found) == 2
    assert {e["confidence"] for e in found} == {"heuristic"}
    assert "ambiguous, 2 modules named helpers" in found[0]["how"]


# --- scopes and flags ---------------------------------------------------------------------


def test_function_level_import_is_attributed_to_the_function(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "heavy.py")
    write(tmp_path, "app.py", "def run():\n    import heavy\n")
    index(tmp_path, conn)

    (edge,) = edges(conn)
    assert (edge["src"], edge["dst"]) == ("app.run", "heavy")


def test_type_checking_imports_are_marked_in_the_evidence(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "types_only.py")
    write(
        tmp_path,
        "app.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import types_only\n",
    )
    index(tmp_path, conn)

    edge = next(e for e in edges(conn) if e["dst"] == "types_only")
    assert edge["how"].endswith("[under TYPE_CHECKING]")


# --- incremental behaviour ----------------------------------------------------------------


def test_edges_follow_changes_in_files_that_were_not_themselves_modified(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "lib.py", "def tool(): pass\n")
    write(tmp_path, "app.py", "from lib import tool\n")
    index(tmp_path, conn)
    assert ("lib.tool", "resolved") in targets(conn, "app")

    # app.py is untouched; the function it imports is renamed.
    write(tmp_path, "lib.py", "def renamed(): pass\n")
    index(tmp_path, conn)
    assert targets(conn, "app") == {("lib", "resolved")}  # module edge only, tool is gone

    # ...and now the whole module disappears.
    (tmp_path / "lib.py").unlink()
    index(tmp_path, conn)
    assert targets(conn, "app") == {(None, "unresolved")}


def test_a_new_module_resolves_previously_unresolved_imports(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "app.py", "import later\n")
    index(tmp_path, conn)
    assert targets(conn) == {(None, "unresolved")}

    write(tmp_path, "later.py")
    index(tmp_path, conn)
    assert targets(conn) == {("later", "resolved")}


def test_unchanged_rerun_keeps_edges_and_skips_resolution(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "m.py", "def f(): pass\n")
    write(tmp_path, "app.py", "from m import f\n")
    first = index_repo(tmp_path, conn)
    assert first.import_edges == 2
    before = [tuple(e) for e in edges(conn)]

    second = index_repo(tmp_path, conn)

    assert second.import_edges == 0  # nothing changed, resolution was not re-run
    assert [tuple(e) for e in edges(conn)] == before


def test_every_edge_belongs_to_the_file_of_its_source_symbol(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(tmp_path, "pkg/__init__.py", "from .core import Thing\n")
    write(tmp_path, "pkg/core.py", "import os\nclass Thing: pass\n")
    write(tmp_path, "app.py", "from pkg import Thing\nimport pkg.core\n")
    index(tmp_path, conn)

    mismatches = conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN symbols s ON s.id = e.src_symbol_id "
        "WHERE e.file_id != s.file_id"
    ).fetchone()[0]
    assert mismatches == 0
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] > 0


def test_stats_report_edge_counts(tmp_path: Path, conn: sqlite3.Connection) -> None:
    write(tmp_path, "m.py")
    write(tmp_path, "app.py", "import m\nimport os\n")

    stats = index_repo(tmp_path, conn)

    assert stats.import_edges == 2
    assert stats.unresolved_imports == 1
