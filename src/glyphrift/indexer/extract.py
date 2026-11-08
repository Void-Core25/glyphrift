"""Symbol extraction from Python source, using the standard-library ``ast`` module.

Produces the modules, classes, functions and methods defined in a file, with line ranges,
plus the raw facts of every import statement (resolved into edges by ``resolve.py``).
Calls and inheritance edges are extracted by later steps.

Conventions:

* Every file yields exactly one ``module`` symbol (always first in the result), even when the
  file cannot be parsed. A parse failure is reported in ``parse_error``; nothing is raised.
* Definitions nested in ``if`` / ``try`` / ``with`` / loops (e.g. ``if TYPE_CHECKING:``) belong
  to the enclosing module, class or function; those statements create no symbols themselves.
* A function defined directly in a class body is a ``method``. Any other function, including
  one nested in a method or in another function, is a ``function``.
* Qualified names are plain dotted paths (``pkg.mod.Class.method``). A function nested in a
  function is ``pkg.mod.outer.inner`` (no ``<locals>``). Names are not guaranteed unique,
  because Python lets a name be defined twice (property setters, conditional definitions).
* ``line_start`` of a decorated definition is the line of its first decorator, so a change to
  a decorator maps to the right symbol. ``line_end`` is the last line of the definition.
* Each imported name is one ``ExtractedImport``: ``import a.b, c`` gives two records and
  ``from x import y, z`` gives two. ``scope`` is the innermost enclosing module, class or
  function, so an import inside a function is attributed to that function. Imports under
  ``if TYPE_CHECKING:`` are flagged. ``from __future__ import ...`` is not an import of
  anything and is skipped.
* Only the grammar of the Python running GLYPHRIFT can be parsed: code using newer syntax
  than the running interpreter is reported as a ``SyntaxError`` parse error.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

SymbolKind = Literal["module", "class", "function", "method"]

_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
# Nodes that can contain further statements. Expressions never contain definitions.
_STATEMENT_CONTAINERS = (ast.stmt, ast.excepthandler, ast.match_case)

_BYTES_NEWLINE = re.compile(rb"\r\n|\r|\n")
_TEXT_NEWLINE = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True, slots=True)
class ExtractedSymbol:
    kind: SymbolKind
    name: str
    qualified_name: str
    line_start: int
    line_end: int
    parent: int | None  # index of the parent symbol in ExtractionResult.symbols


@dataclass(frozen=True, slots=True)
class ExtractedImport:
    """One imported name from one import statement."""

    scope: int  # index in ExtractionResult.symbols of the enclosing module/class/function
    kind: Literal["import", "from"]
    level: int  # number of leading dots of a relative import (0 = absolute)
    module: str | None  # 'a.b' in `import a.b` / `from a.b import x`; None for `from . import x`
    name: str | None  # the imported name for `from` imports ("*" for a star import)
    asname: str | None  # the alias after `as`, if any
    line: int
    type_checking: bool  # inside an `if TYPE_CHECKING:` block


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    symbols: tuple[ExtractedSymbol, ...]  # symbols[0] is always the module symbol
    parse_error: str | None
    imports: tuple[ExtractedImport, ...] = ()


@dataclass(frozen=True, slots=True)
class _Scope:
    index: int
    qualified_name: str
    kind: SymbolKind


def compute_module_name(rel_path: str, python_files: AbstractSet[str]) -> str:
    """Return the dotted module name for a file, given all Python files in the repository.

    The name starts at the outermost directory of the unbroken chain of packages (directories
    with an ``__init__.py``) that contains the file. So ``src/pkg/mod.py`` is ``pkg.mod`` when
    ``src/`` is not a package, and ``tests/unit/test_x.py`` is ``test_x`` when ``tests/unit/``
    is not a package. ``pkg/__init__.py`` is ``pkg``. Namespace packages (directories without
    ``__init__.py``) are not recognised.
    """
    path = PurePosixPath(rel_path)
    stem = path.stem
    parts = [] if stem == "__init__" else [stem]

    directory = path.parent
    while directory != PurePosixPath("."):
        prefix = directory.as_posix()
        if f"{prefix}/__init__.py" in python_files or f"{prefix}/__init__.pyi" in python_files:
            parts.insert(0, directory.name)
            directory = directory.parent
        else:
            break
    return ".".join(parts) if parts else stem


def _count_lines(source: bytes | str) -> int:
    """Number of lines as Python's tokenizer counts them (\\n, \\r\\n and \\r only)."""
    if isinstance(source, bytes):
        pieces: list[bytes] | list[str] = _BYTES_NEWLINE.split(source)
        last_is_empty = pieces[-1] == b""
    else:
        pieces = _TEXT_NEWLINE.split(source)
        last_is_empty = pieces[-1] == ""
    return len(pieces) - (1 if last_is_empty else 0)


def _is_type_checking_guard(test: ast.expr) -> bool:
    """True for `TYPE_CHECKING` and `typing.TYPE_CHECKING` (any module prefix)."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, SyntaxError):
        where = f" (line {exc.lineno})" if exc.lineno else ""
        return f"SyntaxError: {exc.msg}{where}"
    return f"{type(exc).__name__}: {exc}"


def extract_symbols(source: bytes | str, module_name: str) -> ExtractionResult:
    """Extract the symbols defined in one Python source file.

    ``source`` may be bytes (preferred: encoding declarations and BOMs are honoured) or text.
    """
    module = ExtractedSymbol(
        kind="module",
        name=module_name.rsplit(".", 1)[-1],
        qualified_name=module_name,
        line_start=1,
        line_end=max(1, _count_lines(source)),
        parent=None,
    )
    try:
        with warnings.catch_warnings():
            # Old code is full of '\d'-style escapes; the parser would warn about each one.
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return ExtractionResult(symbols=(module,), parse_error=_describe_error(exc))

    symbols: list[ExtractedSymbol] = [module]
    imports: list[ExtractedImport] = []
    module_scope = _Scope(index=0, qualified_name=module_name, kind="module")

    # Iterative pre-order walk: deeply nested code (long elif chains) cannot overflow the stack.
    stack: list[tuple[ast.AST, _Scope, bool]] = [
        (child, module_scope, False) for child in reversed(list(ast.iter_child_nodes(tree)))
    ]
    while stack:
        node, scope, in_type_checking = stack.pop()
        if isinstance(node, _DEFINITIONS):
            kind: SymbolKind
            if isinstance(node, ast.ClassDef):
                kind = "class"
            elif scope.kind == "class":
                kind = "method"
            else:
                kind = "function"
            first_line = min([d.lineno for d in node.decorator_list], default=node.lineno)
            qualified = f"{scope.qualified_name}.{node.name}"
            symbols.append(
                ExtractedSymbol(
                    kind=kind,
                    name=node.name,
                    qualified_name=qualified,
                    line_start=first_line,
                    line_end=node.end_lineno if node.end_lineno is not None else node.lineno,
                    parent=scope.index,
                )
            )
            inner = _Scope(index=len(symbols) - 1, qualified_name=qualified, kind=kind)
            stack.extend(
                (child, inner, in_type_checking)
                for child in reversed(list(ast.iter_child_nodes(node)))
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ExtractedImport(
                        scope=scope.index,
                        kind="import",
                        level=0,
                        module=alias.name,
                        name=None,
                        asname=alias.asname,
                        line=node.lineno,
                        type_checking=in_type_checking,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level == 0 and node.module == "__future__":
                continue
            for alias in node.names:
                imports.append(
                    ExtractedImport(
                        scope=scope.index,
                        kind="from",
                        level=level,
                        module=node.module,
                        name=alias.name,
                        asname=alias.asname,
                        line=node.lineno,
                        type_checking=in_type_checking,
                    )
                )
        elif isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            # Only the body is type-checking-only; the else branch runs at runtime.
            stack.extend((child, scope, in_type_checking) for child in reversed(node.orelse))
            stack.extend((child, scope, True) for child in reversed(node.body))
        elif isinstance(node, _STATEMENT_CONTAINERS):
            stack.extend(
                (child, scope, in_type_checking)
                for child in reversed(list(ast.iter_child_nodes(node)))
            )

    return ExtractionResult(symbols=tuple(symbols), parse_error=None, imports=tuple(imports))
