"""Symbol extraction from Python source, using the standard-library ``ast`` module.

Produces the modules, classes, functions and methods defined in a file, with line ranges,
plus the raw facts of every import statement and every call site (resolved into edges by
``resolve.py`` and ``calls.py``). Inheritance edges are extracted by a later step.

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
* Each call site is one ``ExtractedCall``: the callee written as ``root`` (the base name) plus
  ``attrs`` (the dotted attribute path), so ``f()`` is root ``f``, ``a.b.c()`` is root ``a``
  with attrs ``b.c``, and ``make().run()`` has no root (unknown receiver) with attrs ``run``.
  Calls through subscripts, lambdas or other calls (``handlers[k]()``, ``f()()``) are not
  recorded. A call is attributed to the scope that *executes* it: decorators, default values
  and base classes belong to the enclosing scope, not to the function they decorate.
* For each call we also record whether its root name is a local variable or parameter of an
  enclosing function (``opaque_in``), because such a name shadows any module-level function
  of the same name, and whether the root is the ``self``/``cls`` parameter of an enclosing
  method (``receiver_class``). For ``super().x()`` the receiver class is the class of the
  nearest enclosing method.
* Each base-class expression of a class definition is one ``ExtractedBase`` (in source order);
  ``Generic[T]`` is recorded as ``Generic``; keyword arguments such as ``metaclass=`` are not
  bases. A base that is not a plain dotted name (a call, say) has no root.
* Only the grammar of the Python running GLYPHRIFT can be parsed: code using newer syntax
  than the running interpreter is reported as a ``SyntaxError`` parse error.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

SymbolKind = Literal["module", "class", "function", "method"]

_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
# Nodes that can contain further statements. Expressions never contain definitions.
_STATEMENT_CONTAINERS = (ast.stmt, ast.excepthandler, ast.match_case)

_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

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
class ExtractedCall:
    """One call site."""

    scope: int  # index in ExtractionResult.symbols of the scope that executes the call
    root: str | None  # base name; "super()" for super().x(); None for an unknown base expression
    attrs: str  # dotted attribute path after the root ("" for a bare call such as f())
    line: int
    opaque_in: int | None  # nearest enclosing function that binds `root` as a local variable
    receiver_class: int | None  # class symbol index when `root` is the self/cls parameter


@dataclass(frozen=True, slots=True)
class ExtractedBase:
    """One base-class expression of a class definition (in source order)."""

    cls: int  # index in ExtractionResult.symbols of the class
    scope: int  # the scope the class is defined in: where the base expression is evaluated
    root: str | None  # base name; None when the base is not a plain (dotted) name
    attrs: str  # dotted attribute path after the root ("" for a plain name)
    text: str  # the expression as written, shortened (for messages)
    line: int
    opaque_in: int | None  # nearest enclosing function that binds `root` as a local variable


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    symbols: tuple[ExtractedSymbol, ...]  # symbols[0] is always the module symbol
    parse_error: str | None
    imports: tuple[ExtractedImport, ...] = ()
    calls: tuple[ExtractedCall, ...] = ()
    bases: tuple[ExtractedBase, ...] = ()


@dataclass(frozen=True, slots=True)
class _Scope:
    index: int
    qualified_name: str
    kind: SymbolKind
    outer: _Scope | None = None
    local_names: frozenset[str] = frozenset()  # parameters and assigned variables (functions)
    first_param: str | None = None  # name of the first positional parameter (functions)
    is_static: bool = False  # decorated with @staticmethod


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


def _parameter_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "staticmethod":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "staticmethod":
            return True
    return False


def _opaque_locals(func: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Names a function binds as local variables: parameters and assigned names.

    Names bound only by ``def``/``class``/``import`` statements are not included: those are
    symbols or import facts that the resolvers understand. Names declared ``global`` or
    ``nonlocal`` are excluded. Nested function, class and lambda bodies are other scopes.
    """
    names = set(_parameter_names(func.args))
    declared_outer: set[str] = set()
    stack: list[ast.AST] = list(reversed(func.body))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared_outer.update(node.names)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                names.add(node.id)
        elif isinstance(node, _COMPREHENSIONS):
            # Comprehension variables are local to the comprehension, but a walrus inside
            # binds in the enclosing function, so the rest of the expression is still scanned.
            elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for generator in node.generators:
                stack.append(generator.iter)
                stack.extend(generator.ifs)
            stack.extend(elements)
        else:
            if isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                names.add(node.rest)
            stack.extend(ast.iter_child_nodes(node))
    return frozenset(names - declared_outer)


def _binding_info(
    root: str, scope: _Scope, inner_names: frozenset[str]
) -> tuple[int | None, int | None]:
    """(nearest function binding `root` locally, class if `root` is its self/cls parameter)."""
    if root in inner_names:  # parameter or variable of an enclosing lambda/comprehension
        return scope.index, None
    current: _Scope | None = scope
    while current is not None:
        if current.kind in ("function", "method") and root in current.local_names:
            receiver_class = None
            if (
                current.kind == "method"
                and current.first_param == root
                and not current.is_static
                and current.outer is not None
            ):
                receiver_class = current.outer.index
            return current.index, receiver_class
        current = current.outer
    return None, None


def _base_fact(expr: ast.expr, class_index: int, scope: _Scope) -> ExtractedBase:
    """Describe one base-class expression; subscripts (``Generic[T]``) are looked through."""
    text = ast.unparse(expr)
    if len(text) > 80:
        text = text[:77] + "..."
    target = expr
    while isinstance(target, ast.Subscript):
        target = target.value
    attrs: list[str] = []
    while isinstance(target, ast.Attribute):
        attrs.append(target.attr)
        target = target.value
    attrs.reverse()
    root: str | None = None
    opaque_in: int | None = None
    if isinstance(target, ast.Name):
        root = target.id
        opaque_in, _ = _binding_info(root, scope, frozenset())
    else:
        attrs = []
    return ExtractedBase(
        cls=class_index,
        scope=scope.index,
        root=root,
        attrs=".".join(attrs),
        text=text,
        line=expr.lineno,
        opaque_in=opaque_in,
    )


def _call_fact(call: ast.Call, scope: _Scope, inner_names: frozenset[str]) -> ExtractedCall | None:
    attrs: list[str] = []
    base: ast.expr = call.func
    while isinstance(base, ast.Attribute):
        attrs.append(base.attr)
        base = base.value
    attrs.reverse()

    root: str | None
    if isinstance(base, ast.Name):
        root = base.id
    elif (
        attrs
        and isinstance(base, ast.Call)
        and isinstance(base.func, ast.Name)
        and base.func.id == "super"
    ):
        root = "super()"
    elif attrs:
        root = None  # e.g. make().run(), items[0].run(), "text".join(): unknown receiver
    else:
        return None  # callee is a call result, subscript or lambda: not recorded

    opaque_in: int | None = None
    receiver_class: int | None = None
    if root == "super()":
        # The class whose base classes are searched: the nearest enclosing method's class.
        current: _Scope | None = scope
        while current is not None:
            if current.kind == "method" and current.outer is not None:
                opaque_in = current.index
                receiver_class = current.outer.index
                break
            current = current.outer
    elif root is not None:
        opaque_in, receiver_class = _binding_info(root, scope, inner_names)

    return ExtractedCall(
        scope=scope.index,
        root=root,
        attrs=".".join(attrs),
        line=call.lineno,
        opaque_in=opaque_in,
        receiver_class=receiver_class,
    )


def _collect_calls(roots: Iterable[ast.AST], scope: _Scope, out: list[ExtractedCall]) -> None:
    """Record every call in the given expression trees, attributed to ``scope``."""
    stack: list[tuple[ast.AST, frozenset[str]]] = [
        (root, frozenset()) for root in reversed(list(roots))
    ]
    while stack:
        node, inner = stack.pop()
        if isinstance(node, ast.Call):
            call = _call_fact(node, scope, inner)
            if call is not None:
                out.append(call)
        if isinstance(node, ast.Lambda):
            bound = inner | frozenset(_parameter_names(node.args))
            defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
            stack.append((node.body, bound))
            stack.extend((default, inner) for default in reversed(defaults))
        elif isinstance(node, _COMPREHENSIONS):
            targets = {
                n.id
                for generator in node.generators
                for n in ast.walk(generator.target)
                if isinstance(n, ast.Name)
            }
            bound = inner | frozenset(targets)
            elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            parts: list[tuple[ast.AST, frozenset[str]]] = []
            for index, generator in enumerate(node.generators):
                parts.append((generator.iter, inner if index == 0 else bound))
                parts.extend((condition, bound) for condition in generator.ifs)
            parts.extend((element, bound) for element in elements)
            stack.extend(reversed(parts))
        else:
            stack.extend((child, inner) for child in reversed(list(ast.iter_child_nodes(node))))


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
    calls: list[ExtractedCall] = []
    bases: list[ExtractedBase] = []
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
            # Decorators, defaults, annotations and base classes run in the enclosing scope.
            header = [
                child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)
            ]
            _collect_calls(header, scope, calls)
            if isinstance(node, ast.ClassDef):
                bases.extend(_base_fact(base, len(symbols) - 1, scope) for base in node.bases)
                inner = _Scope(
                    index=len(symbols) - 1, qualified_name=qualified, kind=kind, outer=scope
                )
            else:
                positional = [*node.args.posonlyargs, *node.args.args]
                inner = _Scope(
                    index=len(symbols) - 1,
                    qualified_name=qualified,
                    kind=kind,
                    outer=scope,
                    local_names=_opaque_locals(node),
                    first_param=positional[0].arg if positional else None,
                    is_static=_is_staticmethod(node),
                )
            stack.extend(
                (child, inner, in_type_checking)
                for child in reversed(list(ast.iter_child_nodes(node)))
                if isinstance(child, _STATEMENT_CONTAINERS)
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
            children = list(ast.iter_child_nodes(node))
            _collect_calls(
                [child for child in children if not isinstance(child, _STATEMENT_CONTAINERS)],
                scope,
                calls,
            )
            stack.extend(
                (child, scope, in_type_checking)
                for child in reversed(children)
                if isinstance(child, _STATEMENT_CONTAINERS)
            )

    return ExtractionResult(
        symbols=tuple(symbols),
        parse_error=None,
        imports=tuple(imports),
        calls=tuple(calls),
        bases=tuple(bases),
    )
