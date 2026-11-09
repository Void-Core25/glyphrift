"""Measure how accurate the indexer's edges are, by sampling and hand labelling.

Workflow (see ``docs/eval-repos.md``):

1. ``build_samples`` draws a seeded random sample of edges from an indexed repository, for each
   (kind, confidence) stratum, and attaches the source context needed to judge each one.
2. A person (or Claude, recorded in ``by``) fills in ``label`` for every sampled edge:

   * ``correct``: for a resolved/inferred/heuristic edge, the call/import/base really refers to
     the target symbol. For an ``unresolved`` edge, the reference is really outside the
     repository or cannot be determined from the source alone.
   * ``wrong``: the edge points at the wrong symbol; or, for an ``unresolved`` edge, a careful
     reader can name the repository symbol it refers to (a miss).
   * ``unsure``: cannot be decided; excluded from the percentages but counted and reported.

3. ``summarize`` turns labelled samples into precision per stratum with a 95% Wilson interval.

Sampling gives precision (how often an edge is right) and the share of avoidable ``unresolved``
edges. It does not measure recall over all true calls; that needs the per-symbol caller check.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# (kind, confidence) -> sample size per repository.
DEFAULT_QUOTAS: dict[tuple[str, str], int] = {
    ("calls", "resolved"): 30,
    ("calls", "inferred"): 20,
    ("calls", "heuristic"): 20,
    ("calls", "unresolved"): 20,
    ("inherits", "resolved"): 8,
    ("inherits", "inferred"): 8,
    ("inherits", "unresolved"): 8,
    ("imports", "resolved"): 8,
}

# Edges from files below these directories are not sampled (they are still indexed, so that
# everything else resolves as it would in real use): test code mostly calls the test framework.
EXCLUDED_DIRS = frozenset({"tests", "test", "docs", "doc", "examples", "example", "benchmarks"})

LABELS = ("correct", "wrong", "unsure")
CONTEXT_LINES = 2
_IMPORT_LINE = re.compile(r"^\s*(?:import|from)\s")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class _Row:
    path: str
    line: int
    kind: str
    confidence: str
    src: str
    dst_name: str
    dst: str | None
    dst_path: str | None
    dst_line: int | None
    how: str


def edge_id(repo: str, row: _Row) -> str:
    """Stable id of an edge: the same edge in the same commit always gets the same id.

    The target's location is part of the key because different symbols can share a qualified
    name (conditional redefinitions).
    """
    target = f"{row.dst or ''}@{row.dst_path}:{row.dst_line}"
    key = "|".join([repo, row.path, str(row.line), row.kind, row.src, row.dst_name, target])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _in_excluded_dir(path: str) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.split("/")[:-1])


class _Sources:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, list[str]] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self._cache:
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._cache[rel] = text.splitlines()
        return self._cache[rel]


def _context(lines: list[str], line: int) -> list[str]:
    first = max(1, line - CONTEXT_LINES)
    last = min(len(lines), line + CONTEXT_LINES)
    return [
        f"{number}{'>' if number == line else ':'} {lines[number - 1].rstrip()[:200]}"
        for number in range(first, last + 1)
    ]


def _import_lines(lines: list[str], dst_name: str) -> list[str]:
    """Import statements in the file that mention the first name of the reference."""
    match = None if dst_name.startswith("<") else _IDENTIFIER.match(dst_name.lstrip("."))
    root = match.group(0) if match else ""
    if not root or root in {"self", "cls", "super"}:
        return []
    word = re.compile(rf"\b{re.escape(root)}\b")
    found = [
        f"{number}: {text.strip()[:160]}"
        for number, text in enumerate(lines, start=1)
        if _IMPORT_LINE.match(text) and word.search(text)
    ]
    return found[:4]


def _fetch_rows(conn: sqlite3.Connection) -> list[_Row]:
    rows = conn.execute(
        "SELECT f.path AS path, e.line AS line, e.kind AS kind, e.confidence AS confidence, "
        "src.qualified_name AS src, e.dst_name AS dst_name, dst.qualified_name AS dst, "
        "df.path AS dst_path, dst.line_start AS dst_line, e.how AS how "
        "FROM edges e JOIN files f ON f.id = e.file_id "
        "JOIN symbols src ON src.id = e.src_symbol_id "
        "LEFT JOIN symbols dst ON dst.id = e.dst_symbol_id "
        "LEFT JOIN files df ON df.id = dst.file_id"
    ).fetchall()
    return [_Row(**{key: row[key] for key in row.keys()}) for row in rows]  # noqa: SIM118


def build_samples(
    conn: sqlite3.Connection,
    repo_root: Path,
    repo: str,
    *,
    quotas: dict[tuple[str, str], int] | None = None,
    seed: int = 1,
    include_tests: bool = False,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], tuple[int, int]]]:
    """Sample edges per stratum. Returns ``(entries, {stratum: (available, sampled)})``."""
    quotas = DEFAULT_QUOTAS if quotas is None else quotas
    sources = _Sources(repo_root)
    by_stratum: dict[tuple[str, str], list[tuple[str, _Row]]] = {}
    for row in _fetch_rows(conn):
        if (row.kind, row.confidence) not in quotas:
            continue
        if not include_tests and _in_excluded_dir(row.path):
            continue
        by_stratum.setdefault((row.kind, row.confidence), []).append((edge_id(repo, row), row))

    entries: list[dict[str, Any]] = []
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    for stratum in sorted(quotas):
        candidates = sorted(by_stratum.get(stratum, []), key=lambda item: item[0])
        rng = random.Random(f"{seed}:{stratum[0]}:{stratum[1]}")
        chosen = rng.sample(candidates, min(quotas[stratum], len(candidates)))
        counts[stratum] = (len(candidates), len(chosen))
        for identifier, row in chosen:
            lines = sources.lines(row.path)
            target_def = ""
            if row.dst_path is not None and row.dst_line is not None:
                target_lines = sources.lines(row.dst_path)
                target_def = " ".join(
                    text.strip() for text in target_lines[row.dst_line - 1 : row.dst_line + 1]
                )[:200]
            entries.append(
                {
                    "id": identifier,
                    "repo": repo,
                    "kind": row.kind,
                    "confidence": row.confidence,
                    "file": row.path,
                    "line": row.line,
                    "in": row.src,
                    "reference": row.dst_name,
                    "target": row.dst,
                    "target_at": (
                        f"{row.dst_path}:{row.dst_line}" if row.dst_path is not None else None
                    ),
                    "target_def": target_def,
                    "how": row.how,
                    "code": _context(lines, row.line),
                    "imports": _import_lines(lines, row.dst_name),
                    "label": None,
                    "by": None,
                    "note": "",
                }
            )
    entries.sort(key=lambda e: (e["kind"], e["confidence"], e["file"], e["line"], e["id"]))
    return entries, counts


def merge_labels(
    fresh: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Carry labels from ``existing`` into ``fresh``; keep labelled edges no longer sampled."""
    labelled = {e["id"]: e for e in existing if e.get("label") is not None}
    merged = []
    seen = set()
    for entry in fresh:
        old = labelled.get(entry["id"])
        if old is not None:
            entry = {
                **entry,
                "label": old["label"],
                "by": old.get("by"),
                "note": old.get("note", ""),
            }
        merged.append(entry)
        seen.add(entry["id"])
    merged.extend(e for identifier, e in labelled.items() if identifier not in seen)
    merged.sort(key=lambda e: (e["kind"], e["confidence"], e["file"], e["line"], e["id"]))
    return merged


def wilson(correct: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; None when there is no data."""
    if total == 0:
        return None
    p = correct / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


@dataclass(frozen=True, slots=True)
class Stratum:
    kind: str
    confidence: str
    correct: int
    wrong: int
    unsure: int
    unlabelled: int

    @property
    def decided(self) -> int:
        return self.correct + self.wrong

    @property
    def precision(self) -> float | None:
        return self.correct / self.decided if self.decided else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.correct, self.decided)


def summarize(entries: list[dict[str, Any]]) -> list[Stratum]:
    tally: dict[tuple[str, str], Counter[str]] = {}
    for entry in entries:
        counter = tally.setdefault((entry["kind"], entry["confidence"]), Counter())
        counter[entry["label"] if entry.get("label") in LABELS else "unlabelled"] += 1
    return [
        Stratum(kind, confidence, c["correct"], c["wrong"], c["unsure"], c["unlabelled"])
        for (kind, confidence), c in sorted(tally.items())
    ]


def validate_labels(entries: list[dict[str, Any]]) -> list[str]:
    """Problems that make a label file unusable (bad label values, duplicate ids)."""
    problems = []
    seen: set[str] = set()
    for entry in entries:
        if entry["id"] in seen:
            problems.append(f"duplicate id {entry['id']}")
        seen.add(entry["id"])
        label = entry.get("label")
        if label is not None and label not in LABELS:
            problems.append(f"{entry['id']} ({entry['file']}:{entry['line']}): bad label {label!r}")
        if label is not None and not entry.get("by"):
            problems.append(f"{entry['id']} ({entry['file']}:{entry['line']}): label without 'by'")
    return problems
