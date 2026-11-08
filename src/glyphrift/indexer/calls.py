"""Resolve call sites into ``calls`` edges.

Input: the ``call_facts``, ``symbols`` and ``import_facts`` tables and the ``inherits`` edges.
Output: every row of ``edges`` with ``kind = 'calls'``, rebuilt from scratch on every run that
changed anything.

How a call ``root.attrs(...)`` is resolved, in order:

1. ``super().m()``: searched in the base classes of the enclosing class (``inferred``).
2. Unknown base expression (``make().run()``): unknown receiver, see below.
3. ``self.m()`` / ``cls.m()``: searched in the enclosing class, then its base classes,
   ``inferred`` (a subclass may override the method).
4. Otherwise the root name is looked up like Python does (see ``lookup.py``): nested definitions
   and local imports of the enclosing functions, then the module. A root that is a local
   variable or parameter shadows everything outside the function, so such a call is *not*
   linked to a same-named function.
5. The attribute path is then followed through modules and into classes (including their base
   classes: a method found in a base class is ``inferred``).

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

import io
import sqlite3
import sys
from dataclasses import dataclass, field

from glyphrift.indexer.lookup import BUILTIN_NAMES, Hierarchy, Lookup, NameLookup, dedupe
from glyphrift.indexer.resolve import NamespaceResolver, external_reason, weakest

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
class CallResolutionStats:
    edges: int = 0
    unresolved: int = 0
    by_confidence: dict[str, int] = field(default_factory=dict)


class _CallResolver:
    def __init__(self, conn: sqlite3.Connection, namespace: NamespaceResolver) -> None:
        self.names = NameLookup(conn, namespace)
        self.names.hierarchy = Hierarchy(conn)
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

    @staticmethod
    def _callee_text(fact: _CallFact) -> str:
        if fact.root is None:
            return f"<expression>.{fact.attrs}"
        return f"{fact.root}.{fact.attrs}" if fact.attrs else fact.root

    # -- resolution of one call site ----------------------------------------------------

    def resolve(self, fact: _CallFact) -> list[Outcome]:
        callee = self._callee_text(fact)
        parts = fact.attrs.split(".") if fact.attrs else []

        if fact.root == "super()":
            return self._call_on_super(fact, callee, parts)
        if fact.root is None:
            return self._unknown_receiver(callee, parts[-1])
        if fact.receiver_class is not None:
            return self._call_on_self(fact, callee, parts)

        lookup = self.names.lookup(fact.file_id, fact.scope_symbol_id, fact.root, fact.opaque_in)
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
                    (h.symbol_id, h.confidence, f"call `{callee}`: {self.names.describe(h)}")
                    for h in lookup.hits
                ]
            found, failure = self.names.follow(lookup.hits, parts)
            if not found:
                return [(None, "unresolved", f"call `{callee}`: {failure}")]
            return [
                (h.symbol_id, h.confidence, f"call `{callee}`: {self.names.describe(h)}")
                for h in dedupe(found)
            ]
        return self._unbound(fact, callee, parts, lookup)

    def _unbound(
        self, fact: _CallFact, callee: str, parts: list[str], lookup: Lookup
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

    def _call_on_self(self, fact: _CallFact, callee: str, parts: list[str]) -> list[Outcome]:
        if not parts:
            return []  # self() calls __call__; not tracked
        if len(parts) > 1:  # self.attr.method(): the attribute's type is unknown
            return self._unknown_receiver(callee, parts[-1])
        assert fact.receiver_class is not None
        name = parts[0]
        class_name = self.names.qualified_of[fact.receiver_class]
        members, external = self.names.class_members(fact.receiver_class, name, ("method",))
        if not members:
            reason = self.names.missing_member(name, class_name, external)
            return [(None, "unresolved", f"call `{callee}`: {reason}")]
        outcomes: list[Outcome] = []
        for member, owner, path_confidence in members:
            where = (
                f"method of enclosing class {class_name}"
                if owner == class_name
                else f"inherited from base class {owner} (enclosing class {class_name})"
            )
            how = f"call `{callee}`: {where}; subclass overrides are not considered"
            outcomes.append((member, weakest("inferred", path_confidence), how))
        return outcomes

    def _call_on_super(self, fact: _CallFact, callee: str, parts: list[str]) -> list[Outcome]:
        if fact.receiver_class is None:
            return [(None, "unresolved", f"call `{callee}`: super() outside of a method")]
        if len(parts) > 1:  # super().attr.method(): the attribute's type is unknown
            return self._unknown_receiver(callee, parts[-1])
        name = parts[0]
        class_name = self.names.qualified_of[fact.receiver_class]
        members, external = self.names.class_members(
            fact.receiver_class, name, ("method",), skip_self=True
        )
        if not members:
            reason = f"'{name}' is not defined in the repository base classes of {class_name}"
            if external:
                reason += f"; it may come from external base class(es): {', '.join(external)}"
            return [(None, "unresolved", f"call `{callee}`: {reason}")]
        return [
            (
                member,
                weakest("inferred", path_confidence),
                f"call `{callee}`: method of base class {owner} "
                f"(super() in {class_name}); the runtime class order may differ",
            )
            for member, owner, path_confidence in members
        ]

    def _unknown_receiver(self, callee: str, name: str) -> list[Outcome]:
        if name in BUILTIN_TYPE_METHODS:
            return []
        candidates = self.names.methods_by_name.get(name, [])
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
