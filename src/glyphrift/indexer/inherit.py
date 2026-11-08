"""Resolve base-class expressions into ``inherits`` edges.

Input: ``base_facts`` (the base expressions of every class, in source order), plus symbols and
import facts. Output: every row of ``edges`` with ``kind = 'inherits'``, rebuilt from scratch on
every run that changed anything. The edge goes from the class to its base class; edges of one
class are inserted in base order, which ``Hierarchy`` relies on to search bases left to right.

A base is looked up like any other name (see ``lookup.py``), in the scope the class is defined
in. ``Generic[T]`` counts as ``Generic``. Only classes are accepted as targets.

* ``resolved``: the base is a class in the repository and the lookup was unambiguous.
* ``inferred`` / ``heuristic``: as for imports (conditional redefinitions, same-named modules).
* ``unresolved``: no repository class. The reason is in ``how``: a standard-library or
  third-party class, a builtin such as ``Exception``, a local variable, or a dynamic expression
  such as ``with_metaclass(M)``. These edges matter: a method that is not found in the
  repository may still be inherited from such a base, and call resolution says so.

``object`` is skipped (every class has it). ``metaclass=`` and other keywords are not bases.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from glyphrift.indexer.lookup import BUILTIN_NAMES, NameLookup, dedupe
from glyphrift.indexer.resolve import NamespaceResolver, external_reason

Outcome = tuple[int | None, str, str]


@dataclass(slots=True)
class InheritanceStats:
    edges: int = 0
    unresolved: int = 0
    by_confidence: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _BaseFact:
    file_id: int
    class_id: int
    scope_id: int
    root: str | None
    attrs: str
    text: str
    line: int
    opaque_in: int | None


def _resolve_base(names: NameLookup, fact: _BaseFact) -> list[Outcome]:
    prefix = f"class {names.qualified_of[fact.class_id]} inherits from `{fact.text}`"
    if fact.root is None:
        return [(None, "unresolved", f"{prefix}: not a plain class name (dynamic expression)")]
    parts = fact.attrs.split(".") if fact.attrs else []
    lookup = names.lookup(fact.file_id, fact.scope_id, fact.root, fact.opaque_in)
    if lookup.opaque:
        return [(None, "unresolved", f"{prefix}: a local variable or parameter (dynamic)")]
    if lookup.hits:
        hits = lookup.hits
        failure = ""
        if parts:
            hits, failure = names.follow(hits, parts)
        classes = [h for h in dedupe(hits) if names.kind_of[h.symbol_id] == "class"]
        if classes:
            return [(h.symbol_id, h.confidence, f"{prefix}: {h.describe()}") for h in classes]
        if hits:
            kinds = sorted({names.kind_of[h.symbol_id] for h in hits})
            return [(None, "unresolved", f"{prefix}: refers to a {'/'.join(kinds)}, not a class")]
        return [(None, "unresolved", f"{prefix}: {failure}")]
    if lookup.external is not None:
        return [(None, "unresolved", f"{prefix}: {external_reason(lookup.external)}")]
    if fact.root in BUILTIN_NAMES:
        if fact.root == "object" and not parts:
            return []
        return [(None, "unresolved", f"{prefix}: builtin class")]
    return [
        (
            None,
            "unresolved",
            f"{prefix}: name '{fact.root}' is not defined in the repository "
            "(variable, dynamic or from an external star import)",
        )
    ]


def resolve_inheritance(conn: sqlite3.Connection) -> InheritanceStats:
    """Rebuild all ``inherits`` edges. Call inside a transaction."""
    names = NameLookup(conn, NamespaceResolver(conn))
    rows: list[tuple[object, ...]] = []
    seen: set[tuple[int, int | None, str, int]] = set()
    for row in conn.execute(
        "SELECT file_id, class_symbol_id, scope_symbol_id, root, attrs, text, line, "
        "opaque_in_symbol_id FROM base_facts ORDER BY file_id, id"
    ).fetchall():
        fact = _BaseFact(
            file_id=row["file_id"],
            class_id=row["class_symbol_id"],
            scope_id=row["scope_symbol_id"],
            root=row["root"],
            attrs=row["attrs"],
            text=row["text"],
            line=row["line"],
            opaque_in=row["opaque_in_symbol_id"],
        )
        for dst, confidence, how in _resolve_base(names, fact):
            key = (fact.class_id, dst, fact.text, fact.line)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    fact.class_id,
                    dst,
                    fact.text,
                    "inherits",
                    confidence,
                    how,
                    fact.file_id,
                    fact.line,
                )
            )

    conn.execute("DELETE FROM edges WHERE kind = 'inherits'")
    conn.executemany(
        "INSERT INTO edges (src_symbol_id, dst_symbol_id, dst_name, kind, confidence, how, "
        "file_id, line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    stats = InheritanceStats(edges=len(rows))
    for row in rows:
        confidence = str(row[4])
        stats.by_confidence[confidence] = stats.by_confidence.get(confidence, 0) + 1
    stats.unresolved = stats.by_confidence.get("unresolved", 0)
    return stats
