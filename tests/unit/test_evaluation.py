import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from glyphrift.evaluation import (
    build_samples,
    merge_labels,
    summarize,
    validate_labels,
    wilson,
)
from glyphrift.indexer import index_repo
from glyphrift.store import connect


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def make_repo(root: Path) -> None:
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/util.py", "def helper():\n    return 1\n")
    body = "".join(f"def f{i}():\n    helper()\n    mystery()\n" for i in range(12))
    write(root, "pkg/app.py", "from pkg.util import helper\n" + body)
    write(root, "tests/test_app.py", "from pkg.util import helper\n\n\ndef test():\n    helper()\n")
    write(root, "docs/conf.py", "from pkg.util import helper\nhelper()\n")


QUOTAS = {("calls", "resolved"): 5, ("calls", "unresolved"): 3, ("imports", "resolved"): 2}


def sample(tmp_path: Path, conn: sqlite3.Connection, **kwargs):  # type: ignore[no-untyped-def]
    make_repo(tmp_path)
    index_repo(tmp_path, conn)
    return build_samples(conn, tmp_path, "demo", quotas=QUOTAS, **kwargs)


def test_sampling_respects_quotas_and_reports_availability(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    entries, counts = sample(tmp_path, conn)

    assert counts[("calls", "resolved")] == (12, 5)  # tests/ and docs/ are excluded
    assert counts[("calls", "unresolved")] == (12, 3)
    assert counts[("imports", "resolved")] == (2, 2)  # a from-import is a module + a symbol edge
    assert len(entries) == 5 + 3 + 2


def test_tests_and_docs_are_sampled_only_when_asked(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    entries, counts = sample(tmp_path, conn, include_tests=True)

    assert counts[("calls", "resolved")] == (14, 5)
    assert counts[("imports", "resolved")] == (6, 2)
    assert len(entries) == 5 + 3 + 2


def test_sampling_is_deterministic_and_seed_dependent(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    first, _ = sample(tmp_path, conn, seed=1)
    again, _ = build_samples(conn, tmp_path, "demo", quotas=QUOTAS, seed=1)
    other, _ = build_samples(conn, tmp_path, "demo", quotas=QUOTAS, seed=2)

    assert first == again
    assert [e["id"] for e in first] != [e["id"] for e in other]


def test_entry_carries_the_context_needed_to_judge_it(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    entries, _ = sample(tmp_path, conn)
    call = next(e for e in entries if e["kind"] == "calls" and e["confidence"] == "resolved")

    assert call["target"] == "pkg.util.helper"
    assert call["target_at"] == "pkg/util.py:1"
    assert call["target_def"].startswith("def helper():")
    assert call["reference"] == "helper"
    assert call["imports"] == ["1: from pkg.util import helper"]
    assert any(line.startswith(f"{call['line']}> ") and "helper()" in line for line in call["code"])
    assert call["label"] is None and call["by"] is None
    unresolved = next(e for e in entries if e["confidence"] == "unresolved")
    assert unresolved["target"] is None and unresolved["target_def"] == ""
    assert unresolved["imports"] == []  # `mystery` is not imported anywhere


def test_ids_are_unique_and_stable_across_runs(tmp_path: Path, conn: sqlite3.Connection) -> None:
    entries, _ = sample(tmp_path, conn)
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    assert all(len(i) == 12 for i in ids)

    fresh = connect(":memory:")
    index_repo(tmp_path, fresh)
    entries2, _ = build_samples(fresh, tmp_path, "demo", quotas=QUOTAS)
    assert ids == [e["id"] for e in entries2]
    fresh.close()


def test_merge_keeps_existing_labels_and_unsampled_labelled_entries() -> None:
    def entry(identifier: str, label: str | None = None) -> dict:
        return {
            "id": identifier,
            "kind": "calls",
            "confidence": "resolved",
            "file": "a.py",
            "line": int(identifier),
            "label": label,
            "by": "claude" if label else None,
            "note": "n" if label else "",
        }

    fresh = [entry("1"), entry("2")]
    existing = [entry("1", "correct"), entry("3", "wrong"), entry("4")]

    merged = merge_labels(fresh, existing)

    assert [e["id"] for e in merged] == ["1", "2", "3"]  # unlabelled stale "4" is dropped
    assert merged[0]["label"] == "correct" and merged[0]["note"] == "n"
    assert merged[1]["label"] is None


def test_wilson_interval() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(100, 100)  # type: ignore[misc]
    assert 0.96 < low < 0.97 and high == pytest.approx(1.0)
    low, high = wilson(50, 100)  # type: ignore[misc]
    assert 0.40 < low < 0.41 and 0.59 < high < 0.60
    low, high = wilson(0, 10)  # type: ignore[misc]
    assert low == 0.0 and 0.27 < high < 0.29


def test_summary_counts_labels_per_stratum() -> None:
    def e(kind: str, confidence: str, label: str | None) -> dict:
        return {"kind": kind, "confidence": confidence, "label": label}

    strata = summarize(
        [
            e("calls", "resolved", "correct"),
            e("calls", "resolved", "correct"),
            e("calls", "resolved", "wrong"),
            e("calls", "resolved", "unsure"),
            e("calls", "resolved", None),
            e("imports", "resolved", "correct"),
        ]
    )

    calls = strata[0]
    assert (calls.kind, calls.confidence) == ("calls", "resolved")
    assert (calls.correct, calls.wrong, calls.unsure, calls.unlabelled) == (2, 1, 1, 1)
    assert calls.decided == 3 and calls.precision == pytest.approx(2 / 3)
    assert strata[1].precision == 1.0


def test_validation_flags_bad_labels_duplicates_and_missing_author() -> None:
    base = {"file": "a.py", "line": 1}
    problems = validate_labels(
        [
            {**base, "id": "a", "label": "correct", "by": "claude"},
            {**base, "id": "a", "label": None},
            {**base, "id": "b", "label": "mabye", "by": "claude"},
            {**base, "id": "c", "label": "wrong", "by": None},
        ]
    )

    assert len(problems) == 3
    assert any("duplicate id a" in p for p in problems)
    assert any("bad label 'mabye'" in p for p in problems)
    assert any("without 'by'" in p for p in problems)


def test_symbols_sharing_a_qualified_name_get_distinct_ids(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    write(
        tmp_path,
        "m.py",
        "import sys\nif sys.platform == 'x':\n    def f(): pass\nelse:\n    def f(): pass\n"
        "def g():\n    f()\n",
    )
    index_repo(tmp_path, conn)

    entries, _ = build_samples(conn, tmp_path, "demo", quotas={("calls", "inferred"): 10})

    assert len(entries) == 2  # one edge per definition of f
    assert len({e["id"] for e in entries}) == 2
    assert {e["target_at"] for e in entries} == {"m.py:3", "m.py:5"}
