# Test fixtures

Each folder is a tiny Python project that stands for a repository. `tests/unit/test_fixtures.py`
copies it, indexes it, and compares the edges found with the expectations written **in the
source itself** as comments:

```python
from util import helper  # => imports util resolved; imports util.helper resolved
    helper()  # => calls util.helper resolved
```

Format: `# => <kind> <target> <confidence>`, several separated by `;`.

* `kind` is `imports`, `inherits` or `calls`.
* `target` is the qualified name of the destination symbol. For an `unresolved` edge there is no
  symbol: write `?` followed by the text GLYPHRIFT records (`?requests.get`, `?threading.Thread`).
* The test fails for a missing edge **and** for an edge on a line with no marker, so a line
  without a marker means "no edge here".

Expectations are written from Python's semantics, not copied from the tool's output. When the
tool disagrees, decide which side is wrong; if the tool is wrong, fix the tool.

| Fixture | What it covers |
|---|---|
| `calls_basic` | functions, methods, `self.x()`, instantiation, nested functions, shadowing, builtins, third-party calls |
| `shop` | packages, relative imports, re-exports, submodule imports, star import, `TYPE_CHECKING`, import cycle |
| `zoo` | single/multiple inheritance, `super()`, inherited `self.x()`, external and dynamic bases, `Generic[T]` |
| `limits` | what static analysis cannot know: guesses are `heuristic`, the rest `unresolved` or absent |
| `src_layout` | `src/` layout and tests importing the package |
