"""Index every project in tests/fixtures and compare with the expectations in its comments.

See tests/fixtures/README.md for the marker format.
"""

import re
import shutil
import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

from glyphrift.indexer import index_repo
from glyphrift.store import connect

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MARKER = re.compile(r"#\s*=>\s*(?P<edges>.+?)\s*$")

# (relative path, line, kind, target, confidence)
Edge = tuple[str, int, str, str, str]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


def expected_edges(root: Path) -> Counter[Edge]:
    expected: Counter[Edge] = Counter()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = MARKER.search(line)
            if match is None:
                continue
            for entry in match.group("edges").split(";"):
                parts = entry.split()
                assert len(parts) == 3, f"{rel}:{number}: bad marker entry {entry!r}"
                kind, target, confidence = parts
                expected[(rel, number, kind, target, confidence)] += 1
    return expected


def actual_edges(conn: sqlite3.Connection) -> Counter[Edge]:
    found: Counter[Edge] = Counter()
    for row in conn.execute(
        "SELECT f.path, e.line, e.kind, dst.qualified_name AS dst, e.dst_name, e.confidence "
        "FROM edges e JOIN files f ON f.id = e.file_id "
        "LEFT JOIN symbols dst ON dst.id = e.dst_symbol_id"
    ):
        target = row["dst"] if row["dst"] is not None else f"?{row['dst_name']}"
        found[(row["path"], row["line"], row["kind"], target, row["confidence"])] += 1
    return found


def render(edges: Counter[Edge]) -> str:
    return "\n".join(
        f"  {path}:{line}  {kind} {target} {confidence}"
        for (path, line, kind, target, confidence), count in sorted(edges.items())
        for _ in range(count)
    )


def test_there_are_fixtures() -> None:
    assert len(fixture_names()) >= 5


@pytest.mark.parametrize("name", fixture_names())
def test_fixture_edges_match_the_expectations_in_the_source(
    name: str, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)

    stats = index_repo(root, conn)

    assert stats.parse_errors == []
    expected = expected_edges(root)
    actual = actual_edges(conn)
    missing = expected - actual
    unexpected = actual - expected
    assert not missing and not unexpected, (
        f"fixture {name!r}\nexpected but not found:\n{render(missing) or '  (none)'}\n"
        f"found but not expected:\n{render(unexpected) or '  (none)'}"
    )


def test_every_edge_in_every_fixture_obeys_the_invariant(conn: sqlite3.Connection) -> None:
    for name in fixture_names():
        index_repo(FIXTURES / name, conn)
    bad = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE (confidence = 'unresolved') != (dst_symbol_id IS NULL)"
    ).fetchone()[0]
    assert bad == 0
