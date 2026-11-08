"""Resolve import statements into ``imports`` edges.

Input: the ``import_facts`` and ``symbols`` tables. Output: every row of ``edges`` with
``kind = 'imports'``, rebuilt from scratch (no source parsing, so it is cheap to re-run and a
change in one file correctly updates the edges of files that depend on it).

For each import statement we create:

* one edge to the imported **module** (``import a.b`` and ``from a.b import x`` both depend on
  module ``a.b``); an ``unresolved`` edge when the module is not in the repository, with the
  reason (standard library vs. not found) in ``how``;
* for ``from M import name``, an additional edge to the **symbol** the name refers to, found by
  looking in ``M`` for (1) a class/function defined there, (2) a module-level import binding
  (a re-export, followed through any number of hops), (3) a submodule of package ``M``,
  (4) names provided by ``from X import *`` in ``M``. A name that is none of these (usually a
  variable) produces only the module edge.

Confidence: ``resolved`` when statically certain, ``inferred`` when several definitions exist
(conditional definitions) or the name arrives via a star import (we do not evaluate ``__all__``),
``heuristic`` when several modules in the repository share the imported name, ``unresolved``
when no target exists.

Known limitations: ``import a.b.c`` does not add edges for the implicitly imported parents
``a`` and ``a.b``; ``__all__`` is ignored; namespace packages (no ``__init__.py``) are not
recognised; dynamic imports (``importlib``, ``__import__``) are not seen.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

_RANK = {"resolved": 3, "inferred": 2, "heuristic": 1}
_MAX_DEPTH = 25


def weakest(*levels: str) -> str:
    return min(levels, key=_RANK.__getitem__)


@dataclass(frozen=True, slots=True)
class Module:
    symbol_id: int
    file_id: int
    name: str
    is_package: bool


@dataclass(frozen=True, slots=True)
class ImportFact:
    file_id: int
    scope_symbol_id: int
    scope_kind: str
    kind: str
    level: int
    module: str | None
    name: str | None
    asname: str | None
    line: int
    type_checking: bool

    @property
    def written_module(self) -> str:
        return "." * self.level + (self.module or "")

    def statement(self, *, with_names: bool = True) -> str:
        alias = f" as {self.asname}" if self.asname else ""
        if self.kind == "import":
            return f"import {self.module}{alias}"
        imported = f"{self.name}{alias}" if with_names else ("*" if self.name == "*" else "...")
        return f"from {self.written_module} import {imported}"


@dataclass(frozen=True, slots=True)
class Hit:
    symbol_id: int
    confidence: str
    note: str
    via: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.via:
            return f"{self.note}, re-exported via {' > '.join(self.via)}"
        return self.note


@dataclass(slots=True)
class ImportResolutionStats:
    edges: int = 0
    unresolved: int = 0
    by_confidence: dict[str, int] = field(default_factory=dict)


def target_module_name(fact: ImportFact, importer: Module) -> tuple[str | None, str | None]:
    """Absolute module name an import statement refers to, or (None, reason) if invalid."""
    if fact.level == 0:
        return fact.module, None
    package = importer.name if importer.is_package else importer.name.rpartition(".")[0]
    parts = package.split(".") if package else []
    if not parts:
        return None, "relative import in a top-level module (no parent package)"
    up = fact.level - 1
    if up >= len(parts):
        return None, "relative import goes beyond the top-level package"
    base = parts[: len(parts) - up]
    return ".".join([*base, fact.module] if fact.module else base), None


def external_reason(module_name: str) -> str:
    top = module_name.split(".")[0]
    if top in sys.stdlib_module_names:
        return f"standard library module '{top}'"
    return f"'{top}' not found in the repository (third-party, generated or missing)"


class NamespaceResolver:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.modules_by_name: dict[str, list[Module]] = {}
        self.module_of_file: dict[int, Module] = {}
        for row in conn.execute(
            "SELECT s.id, s.file_id, s.qualified_name, f.path FROM symbols s "
            "JOIN files f ON f.id = s.file_id WHERE s.kind = 'module' ORDER BY s.id"
        ):
            module = Module(
                symbol_id=row["id"],
                file_id=row["file_id"],
                name=row["qualified_name"],
                is_package=PurePosixPath(row["path"]).stem == "__init__",
            )
            self.modules_by_name.setdefault(module.name, []).append(module)
            self.module_of_file[module.file_id] = module

        # module symbol id -> name -> ids of classes/functions defined directly in the module
        self.defs: dict[int, dict[str, list[int]]] = {}
        for row in conn.execute(
            "SELECT parent_id, id, name FROM symbols WHERE kind IN ('class', 'function') "
            "AND parent_id IN (SELECT id FROM symbols WHERE kind = 'module') "
            "ORDER BY line_start, id"
        ):
            self.defs.setdefault(row["parent_id"], {}).setdefault(row["name"], []).append(row["id"])

        self.module_by_symbol: dict[int, Module] = {
            m.symbol_id: m for modules in self.modules_by_name.values() for m in modules
        }

        self.facts: list[ImportFact] = []
        # Imports inside functions/classes: scope symbol -> bound name -> facts
        self.local_bindings: dict[int, dict[str, list[ImportFact]]] = {}
        self.bindings: dict[
            int, dict[str, list[ImportFact]]
        ] = {}  # module symbol -> bound name -> facts
        self.stars: dict[int, list[ImportFact]] = {}  # module symbol -> `from X import *` facts
        for row in conn.execute(
            "SELECT i.file_id, i.scope_symbol_id, s.kind AS scope_kind, i.kind, i.level, "
            "i.module, i.name, i.asname, i.line, i.type_checking "
            "FROM import_facts i JOIN symbols s ON s.id = i.scope_symbol_id "
            "ORDER BY i.file_id, i.id"
        ):
            fact = ImportFact(
                file_id=row["file_id"],
                scope_symbol_id=row["scope_symbol_id"],
                scope_kind=row["scope_kind"],
                kind=row["kind"],
                level=row["level"],
                module=row["module"],
                name=row["name"],
                asname=row["asname"],
                line=row["line"],
                type_checking=bool(row["type_checking"]),
            )
            self.facts.append(fact)
            if fact.scope_kind != "module":
                # Function/class-level imports bind names only inside that scope.
                if fact.name != "*":
                    bound_local = fact.asname or (
                        fact.module.split(".")[0]
                        if fact.kind == "import" and fact.module
                        else fact.name
                    )
                    if bound_local:
                        self.local_bindings.setdefault(fact.scope_symbol_id, {}).setdefault(
                            bound_local, []
                        ).append(fact)
                continue
            if fact.kind == "import":
                assert fact.module is not None
                bound = fact.asname or fact.module.split(".")[0]
            elif fact.name == "*":
                self.stars.setdefault(fact.scope_symbol_id, []).append(fact)
                continue
            else:
                assert fact.name is not None
                bound = fact.asname or fact.name
            self.bindings.setdefault(fact.scope_symbol_id, {}).setdefault(bound, []).append(fact)

    # -- name resolution ---------------------------------------------------------------

    def resolve_name(
        self,
        module: Module,
        name: str,
        seen: frozenset[tuple[int, str]] = frozenset(),
        depth: int = 0,
        *,
        allow_submodule: bool = True,
    ) -> list[Hit]:
        """Symbols that ``from <module> import <name>`` can refer to.

        With ``allow_submodule=False`` this is the lookup of a bare name used *inside* the
        module (its own definitions, import bindings and star imports), where a submodule is
        not an implicit name.
        """
        key = (module.symbol_id, name)
        if key in seen or depth > _MAX_DEPTH:
            return []
        seen = seen | {key}

        defined = self.defs.get(module.symbol_id, {}).get(name)
        if defined:
            if len(defined) == 1:
                return [Hit(defined[0], "resolved", f"defined in {module.name}")]
            note = f"defined {len(defined)} times in {module.name} (conditional definitions)"
            return [Hit(symbol_id, "inferred", note) for symbol_id in defined]

        hits: list[Hit] = []
        for fact in self.bindings.get(module.symbol_id, {}).get(name, ()):
            hits.extend(self.follow_binding(module, fact, seen, depth))
        if hits:
            if len({hit.symbol_id for hit in hits}) > 1:  # alternatives (try/except imports)
                hits = [replace(h, confidence=weakest(h.confidence, "inferred")) for h in hits]
            return hits

        if module.is_package and allow_submodule:
            submodules = self.modules_by_name.get(f"{module.name}.{name}", [])
            if submodules:
                conf = "resolved" if len(submodules) == 1 else "heuristic"
                return [Hit(m.symbol_id, conf, f"submodule of {module.name}") for m in submodules]

        for fact in self.stars.get(module.symbol_id, ()):
            target, _ = target_module_name(fact, module)
            for candidate in self.modules_by_name.get(target or "", []):
                for sub in self.resolve_name(candidate, name, seen, depth + 1):
                    hits.append(
                        replace(
                            sub,
                            confidence=weakest(sub.confidence, "inferred"),
                            note=f"{sub.note} (name arrives via a star import)",
                            via=(module.name, *sub.via),
                        )
                    )
        return hits

    def follow_binding(
        self, module: Module, fact: ImportFact, seen: frozenset[tuple[int, str]], depth: int
    ) -> list[Hit]:
        """Where a module-level import in ``module`` sends the name it binds."""
        if fact.kind == "import":
            assert fact.module is not None
            target = fact.module if fact.asname else fact.module.split(".")[0]
            candidates = self.modules_by_name.get(target, [])
            conf = "resolved" if len(candidates) == 1 else "heuristic"
            note = f"module imported by `{fact.statement()}` in {module.name}"
            return [Hit(m.symbol_id, conf, note) for m in candidates]

        target, _ = target_module_name(fact, module)
        if target is None or fact.name is None:
            return []
        candidates = self.modules_by_name.get(target, [])
        hits: list[Hit] = []
        for candidate in candidates:
            for sub in self.resolve_name(candidate, fact.name, seen, depth + 1):
                conf = (
                    sub.confidence if len(candidates) == 1 else weakest(sub.confidence, "heuristic")
                )
                hits.append(replace(sub, confidence=conf, via=(module.name, *sub.via)))
        return hits

    def external_target(self, fact: ImportFact, importer: Module) -> str | None:
        """Absolute module name of an import fact if that module is not in the repository."""
        if fact.kind == "from" and fact.name == "*":
            return None
        target, _ = target_module_name(fact, importer)
        if target is None:
            return None
        if fact.kind == "import" and not fact.asname:
            target = target.split(".")[0]
        return target if target not in self.modules_by_name else None

    # -- edge generation ---------------------------------------------------------------

    def build_edges(self) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        seen: set[tuple[int, int | None, str, int]] = set()

        def add(
            fact: ImportFact, dst: int | None, dst_name: str, confidence: str, how: str
        ) -> None:
            key = (fact.scope_symbol_id, dst, dst_name, fact.line)
            if key in seen:
                return
            seen.add(key)
            suffix = " [under TYPE_CHECKING]" if fact.type_checking else ""
            rows.append(
                (
                    fact.scope_symbol_id,
                    dst,
                    dst_name,
                    "imports",
                    confidence,
                    how + suffix,
                    fact.file_id,
                    fact.line,
                )
            )

        for fact in self.facts:
            importer = self.module_of_file[fact.file_id]
            target, error = target_module_name(fact, importer)
            written = fact.written_module
            module_stmt = fact.statement(with_names=False)

            if target is None:
                add(fact, None, written, "unresolved", f"{module_stmt}: {error}")
                continue

            candidates = self.modules_by_name.get(target, [])
            if not candidates:
                add(fact, None, written, "unresolved", f"{module_stmt}: {external_reason(target)}")
                continue

            unique = len(candidates) == 1
            for candidate in candidates:
                if candidate.file_id == fact.file_id:
                    continue  # a module "importing itself" (`from . import x` in __init__.py)
                if unique:
                    how = f"{module_stmt}: imports module {candidate.name}"
                else:
                    how = f"{module_stmt}: ambiguous, {len(candidates)} modules named {target}"
                add(fact, candidate.symbol_id, written, "resolved" if unique else "heuristic", how)

            if fact.kind == "from" and fact.name not in (None, "*"):
                assert fact.name is not None
                for candidate in candidates:
                    for hit in self.resolve_name(candidate, fact.name):
                        conf = hit.confidence if unique else weakest(hit.confidence, "heuristic")
                        add(
                            fact,
                            hit.symbol_id,
                            fact.name,
                            conf,
                            f"{fact.statement()}: {hit.describe()}",
                        )
        return rows


def resolve_imports(conn: sqlite3.Connection) -> ImportResolutionStats:
    """Rebuild all ``imports`` edges. Call inside a transaction."""
    rows = NamespaceResolver(conn).build_edges()
    conn.execute("DELETE FROM edges WHERE kind = 'imports'")
    conn.executemany(
        "INSERT INTO edges (src_symbol_id, dst_symbol_id, dst_name, kind, confidence, how, "
        "file_id, line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    stats = ImportResolutionStats(edges=len(rows))
    for row in rows:
        confidence = str(row[4])
        stats.by_confidence[confidence] = stats.by_confidence.get(confidence, 0) + 1
    stats.unresolved = stats.by_confidence.get("unresolved", 0)
    return stats
