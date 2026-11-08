"""Name lookup shared by base-class resolution (``inherit.py``) and call resolution (``calls.py``).

A name used inside some scope is looked up like Python does: nested definitions and local imports
of the enclosing functions first, then the module (its definitions, import bindings and star
imports). A root that is a local variable or parameter shadows everything outside its function.
Attribute paths are then followed through modules (definitions, re-exports, submodules) and into
classes (members of the class, then of its base classes).
"""

from __future__ import annotations

import builtins
import sqlite3
from dataclasses import dataclass, field, replace

from glyphrift.indexer.resolve import Hit, NamespaceResolver, weakest

BUILTIN_NAMES = frozenset(dir(builtins))

CLASS_MEMBER_KINDS = ("method", "class")


@dataclass(slots=True)
class Lookup:
    opaque: bool = False  # the name is a local variable/parameter
    hits: list[Hit] = field(default_factory=list)
    external: str | None = None  # name comes from an import of a module outside the repo


def dedupe(hits: list[Hit]) -> list[Hit]:
    """One hit per symbol; when reached several ways, keep the weakest claim."""
    best: dict[int, Hit] = {}
    for hit in hits:
        known = best.get(hit.symbol_id)
        if known is None or weakest(known.confidence, hit.confidence) != known.confidence:
            best[hit.symbol_id] = hit
    return list(best.values())


class Hierarchy:
    """Base classes of every class, from the ``inherits`` edges (in base order)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        # class id -> [(base class id or None, base name as written, confidence)]
        self.bases: dict[int, list[tuple[int | None, str, str]]] = {}
        for row in conn.execute(
            "SELECT src_symbol_id, dst_symbol_id, dst_name, confidence FROM edges "
            "WHERE kind = 'inherits' ORDER BY id"
        ):
            self.bases.setdefault(row["src_symbol_id"], []).append(
                (row["dst_symbol_id"], row["dst_name"], row["confidence"])
            )

    def linearize(self, class_id: int) -> tuple[list[tuple[int, str]], list[str]]:
        """The class and its in-repo ancestors, depth first, left to right, each once.

        This approximates Python's method resolution order (it is exact for single inheritance
        and for most multiple inheritance; diamonds can differ). Returns the classes with the
        weakest confidence along the path to them, and the names of external base classes
        (standard library, third party or dynamic) met on the way.
        """
        order: list[tuple[int, str]] = []
        external: list[str] = []
        seen: set[int] = set()
        stack: list[tuple[int, str]] = [(class_id, "resolved")]
        while stack:
            current, confidence = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            order.append((current, confidence))
            pending: list[tuple[int, str]] = []
            for dst, name, edge_confidence in self.bases.get(current, []):
                if dst is None:
                    if name not in external:
                        external.append(name)
                else:
                    pending.append((dst, weakest(confidence, edge_confidence)))
            stack.extend(reversed(pending))
        return order, external


class NameLookup:
    """Symbol tables plus the lookup rules described in the module docstring."""

    def __init__(self, conn: sqlite3.Connection, namespace: NamespaceResolver) -> None:
        self.ns = namespace
        self.hierarchy: Hierarchy | None = None  # set once `inherits` edges exist
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

    # -- helpers ---------------------------------------------------------------------

    def describe(self, hit: Hit) -> str:
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

    def class_members(
        self, class_id: int, name: str, kinds: tuple[str, ...], *, skip_self: bool = False
    ) -> tuple[list[tuple[int, str, str]], list[str]]:
        """Members called ``name`` found by searching the class, then its base classes.

        Returns ``(found, external_bases)``. ``found`` lists ``(symbol id, owner class
        qualified name, confidence of the path to the owner)`` for the *first* class in the
        search order that defines the name (later bases are overridden by it).
        """
        if self.hierarchy is None:
            order, external = [(class_id, "resolved")], []
        else:
            order, external = self.hierarchy.linearize(class_id)
        for owner, confidence in order[1:] if skip_self else order:
            members = [
                i for i in self.children.get(owner, {}).get(name, []) if self.kind_of[i] in kinds
            ]
            if members:
                return [(i, self.qualified_of[owner], confidence) for i in members], external
        return [], external

    # -- name lookup ------------------------------------------------------------------

    def lookup(self, file_id: int, scope_id: int, name: str, opaque_in: int | None) -> Lookup:
        """Find what the bare name ``name``, used in scope ``scope_id``, refers to."""
        importer = self.ns.module_of_file[file_id]

        current: int | None = scope_id
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
                    return Lookup(hits=[Hit(i, conf, note) for i in defined])
                bound = self.ns.local_bindings.get(current, {}).get(name)
                if bound:
                    hits: list[Hit] = []
                    for import_fact in bound:
                        for hit in self.ns.follow_binding(importer, import_fact, frozenset(), 0):
                            hits.append(
                                self._imported_here(hit, importer.name, self.qualified_of[current])
                            )
                    if hits:
                        return Lookup(hits=dedupe(hits))
                    for import_fact in bound:
                        target = self.ns.external_target(import_fact, importer)
                        if target is not None:
                            return Lookup(external=target)
                    return Lookup()
            if current == opaque_in:
                return Lookup(opaque=True)
            first = False
            current = self.parent_of[current]

        hits = [
            self._imported_here(hit, importer.name, importer.name)
            for hit in self.ns.resolve_name(importer, name, allow_submodule=False)
        ]
        if hits:
            return Lookup(hits=dedupe(hits))
        for import_fact in self.ns.bindings.get(importer.symbol_id, {}).get(name, []):
            target = self.ns.external_target(import_fact, importer)
            if target is not None:
                return Lookup(external=target)
        return Lookup()

    def follow(self, hits: list[Hit], parts: list[str]) -> tuple[list[Hit], str]:
        """Follow the attribute path ``parts`` from ``hits``. Returns ``(hits, failure text)``."""
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
                    members, external = self.class_members(hit.symbol_id, attr, CLASS_MEMBER_KINDS)
                    if members:
                        for member, owner, path_confidence in members:
                            if owner == qualified:
                                confidence = "resolved" if len(members) == 1 else "inferred"
                                note = f"'{attr}' defined in class {qualified}"
                            else:
                                confidence = "inferred"
                                note = f"'{attr}' inherited from class {owner} (via {qualified})"
                            following.append(
                                Hit(
                                    member,
                                    weakest(hit.confidence, weakest(confidence, path_confidence)),
                                    note,
                                    hit.via,
                                )
                            )
                    else:
                        failure = self.missing_member(attr, qualified, external)
                else:
                    failure = f"'{attr}' is an attribute of function {qualified}"
            current = following
            if not current:
                break
        return current, failure

    @staticmethod
    def missing_member(name: str, class_name: str, external: list[str]) -> str:
        if external:
            return (
                f"'{name}' is not defined in class {class_name} or its repository base classes; "
                f"it may be inherited from external base class(es): {', '.join(external)}"
            )
        return f"'{name}' is not defined in class {class_name} or its base classes"
