import sysconfig
import warnings
from pathlib import Path

import pytest

from glyphrift.indexer import ExtractedSymbol, compute_module_name, extract_symbols


def extract(source: str, module: str = "mod") -> tuple[ExtractedSymbol, ...]:
    result = extract_symbols(source, module)
    assert result.parse_error is None, result.parse_error
    return result.symbols


def summary(symbols: tuple[ExtractedSymbol, ...]) -> list[tuple[str, str, int, int]]:
    """(kind, qualified_name, line_start, line_end) for each symbol."""
    return [(s.kind, s.qualified_name, s.line_start, s.line_end) for s in symbols]


def parent_names(symbols: tuple[ExtractedSymbol, ...]) -> dict[str, str | None]:
    return {
        s.qualified_name: (symbols[s.parent].qualified_name if s.parent is not None else None)
        for s in symbols
    }


# --- module names -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "files", "expected"),
    [
        ("mod.py", {"mod.py"}, "mod"),
        ("pkg/mod.py", {"pkg/mod.py"}, "mod"),  # pkg is not a package: no __init__.py
        ("pkg/mod.py", {"pkg/__init__.py", "pkg/mod.py"}, "pkg.mod"),
        ("pkg/__init__.py", {"pkg/__init__.py"}, "pkg"),
        ("a/b/c.py", {"a/__init__.py", "a/b/__init__.py", "a/b/c.py"}, "a.b.c"),
        ("a/b/__init__.py", {"a/__init__.py", "a/b/__init__.py"}, "a.b"),
        # The chain of packages must be unbroken: a/ is not a package, so we start at b/.
        ("a/b/c.py", {"a/b/__init__.py", "a/b/c.py"}, "b.c"),
        # src layout: src/ is not a package, so the module is glyphrift.cli, not src.glyphrift.cli
        (
            "src/glyphrift/cli.py",
            {"src/glyphrift/__init__.py", "src/glyphrift/cli.py"},
            "glyphrift.cli",
        ),
        ("tests/unit/test_x.py", {"tests/unit/test_x.py"}, "test_x"),
        ("pkg/stubs.pyi", {"pkg/__init__.pyi", "pkg/stubs.pyi"}, "pkg.stubs"),
        ("__init__.py", {"__init__.py"}, "__init__"),  # repository root is itself a package
    ],
)
def test_compute_module_name(path: str, files: set[str], expected: str) -> None:
    assert compute_module_name(path, files) == expected


# --- basic extraction ---------------------------------------------------------------------


def test_function_class_and_method() -> None:
    source = (
        "def top():\n"  # 1
        "    return 1\n"  # 2
        "\n"  # 3
        "class Greeter:\n"  # 4
        "    def hello(self):\n"  # 5
        "        return 'hi'\n"  # 6
        "\n"  # 7
        "    async def later(self):\n"  # 8
        "        return 2\n"  # 9
    )
    symbols = extract(source, "pkg.mod")
    assert summary(symbols) == [
        ("module", "pkg.mod", 1, 9),
        ("function", "pkg.mod.top", 1, 2),
        ("class", "pkg.mod.Greeter", 4, 9),
        ("method", "pkg.mod.Greeter.hello", 5, 6),
        ("method", "pkg.mod.Greeter.later", 8, 9),
    ]
    assert symbols[0].name == "mod"
    assert symbols[2].name == "Greeter"
    assert parent_names(symbols) == {
        "pkg.mod": None,
        "pkg.mod.top": "pkg.mod",
        "pkg.mod.Greeter": "pkg.mod",
        "pkg.mod.Greeter.hello": "pkg.mod.Greeter",
        "pkg.mod.Greeter.later": "pkg.mod.Greeter",
    }


def test_parent_indexes_point_backwards_so_parents_come_first() -> None:
    symbols = extract("class A:\n    class B:\n        def m(self):\n            pass\n")
    for index, symbol in enumerate(symbols):
        assert symbol.parent is None or symbol.parent < index


def test_nested_classes_and_functions() -> None:
    source = (
        "class Outer:\n"  # 1
        "    class Inner:\n"  # 2
        "        def deep(self):\n"  # 3
        "            def helper():\n"  # 4
        "                pass\n"  # 5
        "            return helper\n"  # 6
        "\n"  # 7
        "def outer_fn():\n"  # 8
        "    def inner_fn():\n"  # 9
        "        pass\n"  # 10
        "    class Local:\n"  # 11
        "        def meth(self):\n"  # 12
        "            pass\n"  # 13
    )
    assert summary(extract(source)) == [
        ("module", "mod", 1, 13),
        ("class", "mod.Outer", 1, 6),
        ("class", "mod.Outer.Inner", 2, 6),
        ("method", "mod.Outer.Inner.deep", 3, 6),
        ("function", "mod.Outer.Inner.deep.helper", 4, 5),  # nested in a method: a function
        ("function", "mod.outer_fn", 8, 13),
        ("function", "mod.outer_fn.inner_fn", 9, 10),
        ("class", "mod.outer_fn.Local", 11, 13),
        ("method", "mod.outer_fn.Local.meth", 12, 13),  # in a class inside a function
    ]


def test_symbols_are_in_source_order() -> None:
    symbols = extract("def a():\n    pass\n\ndef b():\n    pass\n\nclass C:\n    pass\n")
    assert [s.name for s in symbols[1:]] == ["a", "b", "C"]


def test_decorated_definitions_start_at_the_first_decorator() -> None:
    source = (
        "import functools\n"  # 1
        "\n"  # 2
        "@functools.cache\n"  # 3
        "@staticmethod\n"  # 4
        "def f():\n"  # 5
        "    pass\n"  # 6
        "\n"  # 7
        "@functools.total_ordering\n"  # 8
        "class C:\n"  # 9
        "    @property\n"  # 10
        "    def p(self):\n"  # 11
        "        return 1\n"  # 12
    )
    assert summary(extract(source)) == [
        ("module", "mod", 1, 12),
        ("function", "mod.f", 3, 6),
        ("class", "mod.C", 8, 12),
        ("method", "mod.C.p", 10, 12),
    ]


def test_multiline_signature_and_body_range() -> None:
    source = "def f(\n    a,\n    b,\n):\n    x = 1\n    return x\n"
    assert summary(extract(source))[1] == ("function", "mod.f", 1, 6)


# --- definitions inside compound statements -----------------------------------------------


def test_definitions_inside_compound_statements_belong_to_the_enclosing_scope() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"  # 1
        "if TYPE_CHECKING:\n"  # 2
        "    class OnlyForTypes:\n"  # 3
        "        pass\n"  # 4
        "try:\n"  # 5
        "    from fast import speedy\n"  # 6
        "except ImportError:\n"  # 7
        "    def speedy():\n"  # 8
        "        pass\n"  # 9
        "else:\n"  # 10
        "    def fallback():\n"  # 11
        "        pass\n"  # 12
        "finally:\n"  # 13
        "    def cleanup():\n"  # 14
        "        pass\n"  # 15
        "with open('x') as handle:\n"  # 16
        "    def in_with():\n"  # 17
        "        pass\n"  # 18
        "for i in range(3):\n"  # 19
        "    def in_for():\n"  # 20
        "        pass\n"  # 21
        "while False:\n"  # 22
        "    def in_while():\n"  # 23
        "        pass\n"  # 24
    )
    symbols = extract(source)
    assert [s.qualified_name for s in symbols[1:]] == [
        "mod.OnlyForTypes",
        "mod.speedy",
        "mod.fallback",
        "mod.cleanup",
        "mod.in_with",
        "mod.in_for",
        "mod.in_while",
    ]
    assert {s.parent for s in symbols[1:]} == {0}  # all direct children of the module


def test_method_defined_inside_an_if_in_a_class_is_still_a_method() -> None:
    source = (
        "import sys\n"
        "class C:\n"
        "    if sys.platform == 'win32':\n"
        "        def where(self):\n"
        "            return 'windows'\n"
        "    else:\n"
        "        def where(self):\n"
        "            return 'other'\n"
    )
    symbols = extract(source)
    assert [(s.kind, s.qualified_name) for s in symbols[2:]] == [
        ("method", "mod.C.where"),
        ("method", "mod.C.where"),
    ]
    assert {s.parent for s in symbols[2:]} == {1}


def test_match_statement_cases_are_traversed() -> None:
    source = (
        "def f(x):\n"
        "    match x:\n"
        "        case 1:\n"
        "            def inside_case():\n"
        "                pass\n"
    )
    assert "mod.f.inside_case" in [s.qualified_name for s in extract(source)]


def test_same_name_defined_twice_keeps_both_definitions() -> None:
    source = (
        "class C:\n"  # 1
        "    @property\n"  # 2
        "    def value(self):\n"  # 3
        "        return 1\n"  # 4
        "    @value.setter\n"  # 5
        "    def value(self, v):\n"  # 6
        "        pass\n"  # 7
    )
    assert summary(extract(source))[2:] == [
        ("method", "mod.C.value", 2, 4),
        ("method", "mod.C.value", 5, 7),
    ]


def test_lambdas_and_variables_create_no_symbols() -> None:
    symbols = extract("square = lambda x: x * x\nVALUE = 3\nitems = [lambda: 1]\n")
    assert len(symbols) == 1  # just the module


# --- module symbol and line counting ------------------------------------------------------


def test_empty_module() -> None:
    assert summary(extract("")) == [("module", "mod", 1, 1)]


def test_module_range_covers_trailing_comments_and_blank_lines() -> None:
    source = "def f():\n    pass\n\n# trailing comment\n\n"
    symbols = extract(source)
    assert symbols[0].line_end == 5
    assert symbols[1].line_end == 2


@pytest.mark.parametrize(
    ("source", "lines"),
    [("x = 1", 1), ("x = 1\n", 1), ("x = 1\ny = 2", 2), ("x = 1\r\ny = 2\r\n", 2), ("a\rb\rc", 3)],
)
def test_line_counting(source: str, lines: int) -> None:
    assert extract_symbols(source.encode(), "m").symbols[0].line_end == lines


def test_unusual_unicode_line_separators_do_not_count_as_newlines() -> None:
    # str.splitlines() would split at U+2028, but Python source does not.
    source = "x = 'a\u2028b'\ny = 2\n"
    assert extract_symbols(source, "m").symbols[0].line_end == 2
    assert extract_symbols(source.encode(), "m").symbols[0].line_end == 2


def test_dotted_module_name_gives_short_name() -> None:
    (module,) = extract("", "a.b.c")
    assert (module.name, module.qualified_name) == ("c", "a.b.c")


# --- input encodings and errors -----------------------------------------------------------


def test_bytes_with_utf8_bom_and_non_ascii_names() -> None:
    source = "\ufeffdef café():\n    return 'ü'\n".encode()
    result = extract_symbols(source, "m")
    assert result.parse_error is None
    assert [s.name for s in result.symbols] == ["m", "café"]


def test_encoding_declaration_is_honoured() -> None:
    source = "# -*- coding: latin-1 -*-\ndef f():\n    return 'caf\xe9'\n".encode("latin-1")
    result = extract_symbols(source, "m")
    assert result.parse_error is None
    assert [s.name for s in result.symbols] == ["m", "f"]


def test_syntax_error_is_reported_not_raised() -> None:
    source = "def ok():\n    pass\n\ndef broken(:\n    pass\n"
    result = extract_symbols(source, "pkg.bad")
    assert result.parse_error is not None
    assert result.parse_error.startswith("SyntaxError")
    assert "line 4" in result.parse_error
    # The module symbol is still produced so the file appears in the graph.
    assert summary(result.symbols) == [("module", "pkg.bad", 1, 5)]


def test_invalid_escape_sequences_parse_without_emitting_warnings() -> None:
    source = "import re\nPATTERN = re.compile('\\d+\\s')\nBAD = '\\d'\n\ndef f():\n    pass\n"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = extract_symbols(source, "m")
    assert caught == []
    assert result.parse_error is None
    assert [s.name for s in result.symbols] == ["m", "f"]


def test_null_bytes_are_reported_not_raised() -> None:
    result = extract_symbols(b"x = 1\x00\n", "m")
    assert result.parse_error is not None
    assert len(result.symbols) == 1


def test_invalid_utf8_is_reported_not_raised() -> None:
    result = extract_symbols(b"x = '\xff\xfe'\n", "m")
    assert result.parse_error is not None
    assert len(result.symbols) == 1


def test_very_long_elif_chain_does_not_crash() -> None:
    chain = "".join(f"    elif x == {i}:\n        return {i}\n" for i in range(1, 3000))
    source = f"def f(x):\n    if x == 0:\n        return 0\n{chain}\ndef after():\n    pass\n"
    result = extract_symbols(source, "m")
    if result.parse_error is None:
        assert [s.name for s in result.symbols] == ["m", "f", "after"]
    else:  # some Python versions refuse to build an AST this deep; that must be reported
        assert len(result.symbols) == 1


# --- robustness on real code --------------------------------------------------------------


def test_invariants_hold_on_real_standard_library_code() -> None:
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    files = sorted(stdlib.glob("*.py"))[:150]
    if len(files) < 20:
        pytest.skip("standard library sources not found")

    failures = 0
    for path in files:
        result = extract_symbols(path.read_bytes(), path.stem)
        if result.parse_error is not None:
            failures += 1
        symbols = result.symbols
        assert symbols[0].kind == "module"
        for index, symbol in enumerate(symbols):
            assert 1 <= symbol.line_start <= symbol.line_end, (path.name, symbol)
            assert symbol.parent is None or symbol.parent < index, (path.name, symbol)
            assert symbol.qualified_name.startswith(path.stem), (path.name, symbol)
            if symbol.parent is not None:
                parent = symbols[symbol.parent]
                assert parent.line_start <= symbol.line_start, (path.name, symbol)
                assert symbol.line_end <= parent.line_end, (path.name, symbol)
    assert failures <= len(files) * 0.02
