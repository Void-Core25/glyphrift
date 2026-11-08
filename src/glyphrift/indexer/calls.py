"""Resolve call sites into ``calls`` edges.

Input: the ``call_facts``, ``symbols`` and ``import_facts`` tables. Output: every row of
``edges`` with ``kind = 'calls'``, rebuilt from scratch on every run that changed anything.

How a call ``root.attrs(...)`` is resolved, in order:

1. ``super().x()``: unresolved for now (base classes arrive with the inheritance step).
2. Unknown base expression (``make().run()``): unknown receiver, see below.
3. ``self.m()`` / ``cls.m()``: looked up in the enclosing class only, ``inferred`` (a subclass
   may override it; base classes are not searched yet).
4. Otherwise the root name is looked up like Python does: nested definitions and local imports
   of the enclosing functions, then the module (its definitions, import bindings and star
   imports). A root that is a local variable or parameter shadows everything outside the
   function, so such a call is *not* linked to a same-named function.
5. The attribute path is then followed: through modules (definitions, re-exports, submodules)
   and into classes (methods defined directly in the class).

Unknown receivers (``obj.save()``, where the type of ``obj`` is not known): a ``heuristic``
edge to every repository method with that name, if there are at most
``MAX_HEURISTIC_CANDIDATES``; one ``unresolved`` edge if there are more. Nothing is recorded
when no repository method has that name, or when the name is also a method of builtin types
(``append``, ``get``, ``join``...): such calls are overwhelmingly not calls into the repository.

Confidence follows ``resolve.py``: ``resolved`` is statically certain, ``inferred`` has a
known caveat, ``heuristic`` is a name-based guess, ``unresolved`` has no target.

Not recorded at all: calls to builtins, calls into the standard library (calls into other
external modules are recorded as ``unresolved`` with the module named), calls of ``self()``,
and calls through subscripts, lambdas or call results. Calls via variables holding instances
(``x = Foo(); x.bar()``) are unknown receivers: there is no type inference.
"""

from __future__ import annotations

import builtins
import io
import sqlite3
import sys
from dataclasses import dataclass, field, replace

from glyphrift.indexer.resolve import (
    Hit,
    NamespaceResolver,
    external_reason,
    weakest,
)

BUILTIN_NAMES = frozenset(dir(builtins))

BUILTIN_TYPE_METHODS = frozenset(
    name
    for typ in (
        str,
        bytes,
        bytearray,
        list,
        dict,
        set,
        frozenset,
        tuple,
        int,
        float,
        complex,
        object,
        memoryview,
        io.IOBase,
        io.TextIOWrapper,
        io.BufferedReader,
        io.BytesIO,
        io.StringIO,
    )
    for name in dir(typ)
    if not name.startswith("__")
)

MAX_HEURISTIC_CANDIDATES = 4

# (destination symbol id or None, confidence, evidence text)
Outcome = tuple[int | None, str, str]


@dataclass(frozen=True, slots=True)
class _CallFact:
    file_id: int
    scope_symbol_id: int
    root: str | None
    attrs: str
    line: int
    opaque_in: int | None
    receiver_class: int | None


@dataclass(slots=True)
class _Lookup:
    opaque: bool = False  # the name is a local variable/parameter
    hits: list[Hit] = field(default_factory=list)
    external: str | None = None  # name comes from an import of a module outside the repo


@dataclass(slots=True)
class CallResolutionStats:
    edges: int = 0
    unresolved: int = 0
    by_confidence: dict[str, int] = field(default_factory=dict)


def _dedupe(hits: list[Hit]) -> list[Hit]:
    """One hit per symbol; when reached several ways, keep the weakest claim."""
    best: dict[int, Hit] = {}
    for hit in hits:
        known = best.get(hit.symbol_id)
        if known is None or weakest(known.confidence, hit.confidence) != known.confidence:
            best[hit.symbol_id] = hit
    return list(best.values())


class _CallResolver:
    def __init__(self, conn: sqlite3.Connection, namespace: NamespaceResolver) -> None:
        self.ns = namespace
        self.kind_of: dict[int, str] = {}
        self.qualified_of: dict[int, str] = {}
        self.parent_of: dict[int, int | None] = {}
        self.children: dict[int, dict[str, list[int]]] = {}
        self.methods_by_name: dict[str, list[int]] = {}
        for row in conn.execute(
            "SELECT id, kind, name, qualified_name, parent_id FROM symbols ORDER BY id"
        ):
            symbol_id = row["id"]
            self.kind_of[symbol_id] = row["kind"]
            self.qualified_of[symbol_id] = row["qualified_name"]
            self.parent_of[symbol_id] = row["parent_id"]
            if row["parent_id"] is not None:
                self.children.setdefault(row["parent_id"], {}).setdefault(row["name"], []).append(
                    symbol_id
                )
            if row["kind"] == "method":
                self.methods_by_name.setdefault(row["name"], []).append(symbol_id)

        self.facts = [
            _CallFact(
                file_id=row["file_id"],
                scope_symbol_id=row["scope_symbol_id"],
                root=row["root"],
                attrs=row["attrs"],
                line=row["line"],
                opaque_in=row["opaque_in_symbol_id"],
                receiver_class=row["receiver_class_symbol_id"],
            )
            for row in conn.execute(
                "SELECT file_id, scope_symbol_id, root, attrs, line, opaque_in_symbol_id, "
                "receiver_class_symbol_id FROM call_facts ORDER BY file_id, id"
            )
        ]

    # -- helpers ---------------------------------------------------------------------

    @staticmethod
    def _callee_text(fact: _CallFact) -> str:
        if fact.root is None:
            return f"<expression>.{fact.attrs}"
        return f"{fact.root}.{fact.attrs}" if fact.attrs else fact.root

    def _describe(self, hit: Hit) -> str:
        text = hit.describe()
        if self.kind_of[hit.symbol_id] == "class":
            text += " [class instantiation]"
        return text

    @staticmethod
    def _imported_here(hit: Hit, importer_name: str, where: str) -> Hit:
        """Rewrite a hit reached through the caller's own import so the evidence reads right."""
        if hit.via and hit.via[0] == importer_name:
            return replace(hit, via=hit.via[1:], note=f"{hit.note} (imported in {where})")
        return hit

    # -- name lookup ------------------------------------------------------------------

    def _lookup_bare(self, fact: _CallFact) -> _Lookup:
        assert fact.root is not None
        name = fact.root
        importer = self.ns.module_of_file[fact.file_id]

        current: int | None = fact.scope_symbol_id
        first = True
        while current is not None and self.kind_of[current] != "module":
            kind = self.kind_of[current]
            if kind in ("function", "method") or (first and kind == "class"):
                wanted = (
                    ("class", "function", "method") if kind == "class" else ("class", "function")
                )
                defined = [
                    i
                    for i in self.children.get(current, {}).get(name, [])
                    if self.kind_of[i] in wanted
                ]
                if defined:
                    conf = "resolved" if len(defined) == 1 else "inferred"
                    note = f"defined in {self.qualified_of[current]}"
                    return _Lookup(hits=[Hit(i, conf, note) for i in defined])
                bound = self.ns.local_bindings.get(current, {}).get(name)
                if bound:
                    hits: list[Hit] = []
                    for import_fact in bound:
                        for hit in self.ns.follow_binding(importer, import_fact, frozenset(), 0):
                            hits.append(
                                self._imported_here(hit, importer.name, self.qualified_of[current])
                            )
                    if hits:
                        return _Lookup(hits=_dedupe(hits))
                    for import_fact in bound:
                        target = self.ns.external_target(import_fact, importer)
                        if target is not None:
                            return _Lookup(external=target)
                    return _Lookup()
            if current == fact.opaque_in:
                return _Lookup(opaque=True)
            first = False
            current = self.parent_of[current]

        hits = [
            self._imported_here(hit, importer.name, importer.name)
            for hit in self.ns.resolve_name(importer, name, allow_submodule=False)
        ]
        if hits:
            return _Lookup(hits=_dedupe(hits))
        for import_fact in self.ns.bindings.get(importer.symbol_id, {}).get(name, []):
            target = self.ns.external_target(import_fact, importer)
            if target is not None:
                return _Lookup(external=target)
        return _Lookup()

    # -- resolution of one call site ----------------------------------------------------

    def resolve(self, fact: _CallFact) -> list[Outcome]:
        callee = self._callee_text(fact)
        parts = fact.attrs.split(".") if fact.attrs else []

        if fact.root == "super()":
            return [
                (
                    None,
                    "unresolved",
                    f"call `{callee}`: super() receiver; base classes are not resolved yet",
                )
            ]
        if fact.root is None:
            return self._unknown_receiver(callee, parts[-1])
        if fact.receiver_class is not None:
            return self._call_on_self(fact, callee, parts)

        lookup = self._lookup_bare(fact)
        if lookup.opaque:
            if not parts:
                return [
                    (
                        None,
                        "unresolved",
                        f"call `{callee}`: calls a local variable or parameter (dynamic)",
                    )
                ]
            return self._unknown_receiver(callee, parts[-1])
        if lookup.hits:
            if not parts:
                return [
                    (h.symbol_id, h.confidence, f"call `{callee}`: {self._describe(h)}")
                    for h in lookup.hits
                ]
            return self._follow_attributes(lookup.hits, parts, callee)
        return self._unbound(fact, callee, parts, lookup)

    def _unbound(
        self, fact: _CallFact, callee: str, parts: list[str], lookup: _Lookup
    ) -> list[Outcome]:
        if lookup.external is not None:
            if lookup.external.split(".")[0] in sys.stdlib_module_names:
                return []  # standard library calls are not recorded
            return [(None, "unresolved", f"call `{callee}`: {external_reason(lookup.external)}")]
        assert fact.root is not None
        if fact.root in BUILTIN_NAMES:
            return []
        if parts:  # method call on a name we know nothing about (e.g. a module-level object)
            return self._unknown_receiver(callee, parts[-1])
        return [
            (
                None,
                "unresolved",
                f"call `{callee}`: name '{fact.root}' is not defined in the repository "
                "(variable, dynamic or from an external star import)",
            )
        ]

    def _follow_attributes(self, hits: list[Hit], parts: list[str], callee: str) -> list[Outcome]:
        current = hits
        failure = ""
        for attr in parts:
            following: list[Hit] = []
            failure = ""
            for hit in current:
                kind = self.kind_of[hit.symbol_id]
                qualified = self.qualified_of[hit.symbol_id]
                if kind == "module":
                    module = self.ns.module_by_symbol[hit.symbol_id]
                    found = self.ns.resolve_name(module, attr)
                    if found:
                        following.extend(
                            replace(f, confidence=weakest(hit.confidence, f.confidence))
                            for f in found
                        )
                    else:
                        failure = (
                            f"'{attr}' is not a class, function or submodule of {qualified} "
                            "(variable or dynamic)"
                        )
                elif kind == "class":
                    members = [
                        i
                        for i in self.children.get(hit.symbol_id, {}).get(attr, [])
                        if self.kind_of[i] in ("method", "class")
                    ]
                    if members:
                        conf = weakest(
                            hit.confidence, "resolved" if len(members) == 1 else "inferred"
                        )
                        note = f"'{attr}' defined in class {qualified}"
                        following.extend(Hit(i, conf, note, hit.via) for i in members)
                    else:
                        failure = (
                            f"'{attr}' is not defined in class {qualified}; "
                            "base classes are not searched yet"
                        )
                else:
                    failure = f"'{attr}' is an attribute of function {qualified}"
            current = following
            if not current:
                break
        if not current:
            return [(None, "unresolved", f"call `{callee}`: {failure}")]
        return [
            (h.symbol_id, h.confidence, f"call `{callee}`: {self._describe(h)}")
            for h in _dedupe(current)
        ]

    def _call_on_self(self, fact: _CallFact, callee: str, parts: list[str]) -> list[Outcome]:
        if not parts:
            return []  # self() calls __call__; not tracked
        if len(parts) > 1:  # self.attr.method(): the attribute's type is unknown
            return self._unknown_receiver(callee, parts[-1])
        assert fact.receiver_class is not None
        name = parts[0]
        class_name = self.qualified_of[fact.receiver_class]
        members = [
            i
            for i in self.children.get(fact.receiver_class, {}).get(name, [])
            if self.kind_of[i] == "method"
        ]
        if members:
            how = (
                f"call `{callee}`: method of enclosing class {class_name} "
                "(subclass overrides and base classes are not considered yet)"
            )
            return [(i, "inferred", how) for i in members]
        return [
            (
                None,
                "unresolved",
                f"call `{callee}`: '{name}' is not defined in class {class_name}; "
                "base classes are not searched yet",
            )
        ]

    def _unknown_receiver(self, callee: str, name: str) -> list[Outcome]:
        if name in BUILTIN_TYPE_METHODS:
            return []
        candidates = self.methods_by_name.get(name, [])
        if not candidates:
            return []
        if len(candidates) > MAX_HEURISTIC_CANDIDATES:
            return [
                (
                    None,
                    "unresolved",
                    f"call `{callee}`: receiver type unknown; {len(candidates)} methods named "
                    f"'{name}' in the repository, too many to guess",
                )
            ]
        how = (
            f"call `{callee}`: receiver type unknown; matched by method name only "
            f"({len(candidates)} candidate{'s' if len(candidates) != 1 else ''} named '{name}')"
        )
        return [(i, "heuristic", how) for i in candidates]

    # -- edge generation ------------------------------------------------------------------

    def build_edges(self) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        seen: set[tuple[int, int | None, str, int]] = set()
        for fact in self.facts:
            callee = self._callee_text(fact)
            for dst, confidence, how in self.resolve(fact):
                key = (fact.scope_symbol_id, dst, callee, fact.line)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    (
                        fact.scope_symbol_id,
                        dst,
                        callee,
                        "calls",
                        confidence,
                        how,
                        fact.file_id,
                        fact.line,
                    )
                )
        return rows


def resolve_calls(conn: sqlite3.Connection) -> CallResolutionStats:
    """Rebuild all ``calls`` edges. Call inside a transaction."""
    rows = _CallResolver(conn, NamespaceResolver(conn)).build_edges()
    conn.execute("DELETE FROM edges WHERE kind = 'calls'")
    conn.executemany(
        "INSERT INTO edges (src_symbol_id, dst_symbol_id, dst_name, kind, confidence, how, "
        "file_id, line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    stats = CallResolutionStats(edges=len(rows))
    for row in rows:
        confidence = str(row[4])
        stats.by_confidence[confidence] = stats.by_confidence.get(confidence, 0) + 1
    stats.unresolved = stats.by_confidence.get("unresolved", 0)
    return stats
