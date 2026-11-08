# GLYPHRIFT

> A code graph that is honest about what it knows, and later, a PR checker that judges changes by what they *do*, not how they look.

> **Name status:** GLYPHRIFT finalised Nov 2026. A quick web search found no existing project with this name, but PyPI, GitHub, npm, domain and USPTO were NOT directly verified: check them before publishing. Rejected names: Cartog (existing code-graph tool, github.com/jrollin/cartog), Synterra (active environmental consulting firm of that name).

---

## Project goals (read this to understand every trade-off)

1. **Learning**: parsing, import resolution, graphs, SQLite, a graph UI, git internals, CI, evaluation.
2. **Portfolio**: a finished, demo-able project with measured results beats a big unfinished one.
3. **Fun first**: build the code graph and UI first (low risk, visual, enjoyable). Move to the harder "PR evidence" track afterwards.
4. **Not a startup.** We are not trying to beat existing tools on features. We aim for a clear idea, honest engineering, and published accuracy numbers.

The project has two tracks:

- **Track A, Code graph (fun, safe):** index a Python repo, show an interactive import/call graph with confidence labels, answer "what breaks if I change this?"
- **Track B, PR evidence (serious, distinctive):** given a pull request, report facts about what it does: do its new tests fail before and pass after, do the symbols it uses exist, what is the blast radius. Reuses Track A's index.

**Defining principle:** never just say something is wrong. Show *why*, show the *evidence* (file:line), state the *confidence*, and never present a hypothesis as a confirmed fact. A passing test run is not proof the project is bug-free.

### Why Track B exists (research summary, Nov 2026)
- An LF/OpenSSF survey estimates ~25% of incoming open-source PRs are low-quality "AI slop" (~31% on large projects); maintainers ask for better PR-management tools.
- Existing tools (e.g. peakoss/anti-slop, SlopGuard) judge PRs by *surface signals* (metadata, text, account history). They do not run code, run tests, or analyse impact. Behaviour-based evidence is the gap we want to try.
- Not verified: there may be other tools (paid products, newer GitHub features). Re-check before claiming novelty.
- Dependency reachability for Python already exists (`ca9` on PyPI, alpha, import-level). Not our focus.
- Competitor to study for Track A: `cartog` (Rust code-graph indexer with impact + MCP).

---

## FOR CLAUDE: read this first (session protocol)

This file is the project's memory. Claude does not remember earlier sessions, so:

1. Read this README, then `ROADMAP.md`, then **Current status** and **Decisions log** below.
2. Find the first unchecked item in the current roadmap phase. Work on that, nothing else, unless the user redirects.
3. Before ending a session, update **Current status** and append to the **Decisions log**. Keep both short and factual.
4. Do not add scope. New ideas go in the Parking lot in ROADMAP.md.
5. Run tests and the eval harness before saying something works. Report real output.
6. Prefer boring, standard tools. Don't add a dependency or infrastructure without recording the reason in the Decisions log.
7. The user wants this to be **fun**. In Track A, favour visible progress (something they can run and look at) over invisible infrastructure.

### Working agreement
- Small steps. Each step ends with something runnable and tested.
- Every finding carries: `evidence` (file, line), `confidence` (`resolved` | `inferred` | `heuristic` | `unresolved`), and `how` (how it was derived).
- Analysis is **read-only**. Track A never modifies or executes the user's code. Track B executes PR code only inside an isolated environment (see ROADMAP Phase 5 safety notes).
- Honest limits: if static analysis can't know something (dynamic dispatch, `getattr`, monkey-patching), say "unknown".

---

## Scope

### Track A (MVP)
- Python only; CLI + local web UI; SQLite index in `.glyphrift/` inside the analysed repo
- Files, symbols (module/class/function/method), imports
- Call/reference/inheritance edges with confidence labels
- Findings: circular imports, complexity hotspots, dead-code *candidates*
- Impact analysis: callers, callees, dependents, probable affected tests
- Interactive graph UI; exportable report
- Evaluation harness with hand-verified ground truth

### Track B (after Track A ships)
- Map a PR diff to changed symbols
- Symbol-existence check (catch hallucinated APIs/imports)
- Blast-radius report (from Track A graph)
- Fail-on-base / pass-on-PR check for tests the PR adds
- GitHub Action + CLI producing a Markdown report/comment (evidence, never auto-close)
- Evaluation on 30-50 real merged/closed PRs, honest write-up

### Out of scope for now
Accounts and auth, hosted service, multi-language support, MCP server, VS Code extension, AI chat UI, vulnerability scanning, PULSE analytics, plugins, team features, test generation.

---

## Architecture

Single Python package, local-first, no external services.

```
 repo on disk
     │
     ▼
 Indexer ──► SQLite (.glyphrift/index.db)
 (ast + import resolver)        │
                                ▼
                       Query / Analysis layer
              (callers, impact, cycles, complexity)
                      │            │
                     CLI      FastAPI (local) ──► React UI
                                   │
                         Track B: PR evidence engine
                    (git diff → symbols → checks → report)
```

### Data model (SQLite)
- `files(id, path, language, sha256, size, indexed_at, parse_error)`; `parse_error` is NULL when parsing succeeded
- `symbols(id, file_id, kind, name, qualified_name, line_start, line_end, parent_id, complexity)`; `kind`: `module | class | function | method`
- `edges(id, src_symbol_id, dst_symbol_id NULL, dst_name, kind, confidence, how, file_id, line)`; `kind`: `imports | calls | inherits | references`
- `findings(id, type, title, severity, confidence, evidence_json, created_at)`; `type`: `cycle | complexity | dead_code_candidate | ...`; `severity`: `info | low | medium | high`
- `import_facts(id, file_id, scope_symbol_id, kind, level, module, name, asname, line, type_checking)`: raw import statements, one row per imported name (schema v2). `imports` edges are *derived* from these and rebuilt by `resolve_imports()` whenever any file changes
- `call_facts(id, file_id, scope_symbol_id, root, attrs, line, opaque_in_symbol_id, receiver_class_symbol_id)`: raw call sites, `root.attrs(...)` split into the root name and dotted attribute path (schema v3). `opaque_in_symbol_id` is the function in which the root is a local variable/parameter; `receiver_class_symbol_id` is set for `self.x()`/`cls.x()`. `calls` edges are *derived* from these by `resolve_calls()` (rebuilt whenever any file changes)
- `base_facts(id, file_id, class_symbol_id, scope_symbol_id, root, attrs, text, line, opaque_in_symbol_id)`: raw base-class expressions in source order (schema v4); `root` is NULL for a base that is not a plain dotted name. `inherits` edges (class -> base class) are *derived* from these by `resolve_inheritance()` and rebuilt whenever any file changes
- Source of truth for the schema: `src/glyphrift/store/migrations.py`. Invariants enforced by the database: an edge has `dst_symbol_id` if and only if its confidence is not `unresolved`; deleting a file, or a symbol, deletes the edges that touch it (so the incremental indexer must re-index files that depended on a changed file); `line_start >= 1` and `line_end >= line_start` (an empty module is lines 1-1)

### Confidence meaning
| Level | Meaning | Example |
|---|---|---|
| `resolved` | Statically certain | `from a import f; f()` where `a.f` exists |
| `inferred` | Likely, by reasoning | `self.method()` via class hierarchy |
| `heuristic` | Name-based guess | `obj.save()` matched by name only |
| `unresolved` | Target not found | Dynamic or external call |

UI and CLI must show these distinctly (e.g. solid / dashed / dotted edges). Never merge them into one unlabeled "dependency."

### Incremental indexing
Store `sha256` per file; re-parse only changed files and recompute edges touching them. A test must prove incremental == full re-index.

---

## Tech choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Matches target language |
| Packaging | `uv` + `pyproject.toml` | Simple, fast |
| CLI | Typer | Minimal boilerplate |
| Parsing | stdlib `ast` | No dependency; Tree-sitter only if adding languages |
| Storage | SQLite | Local-first, zero setup |
| API | FastAPI | Local server for the UI |
| UI | React + TypeScript + Vite + React Flow (consider Cytoscape.js if graphs get large) | Interactive graph |
| Lint/format | Ruff | One tool |
| Tests | pytest | Standard |
| Git ops (Track B) | `git` CLI via subprocess, `git worktree` | Simple, reliable |

---

## Repository layout

```
glyphrift/
├── README.md
├── ROADMAP.md
├── LICENSE
├── pyproject.toml
├── src/glyphrift/
│   ├── cli.py
│   ├── indexer/        # file walk, ast extraction, import resolution
│   ├── store/          # SQLite schema + queries
│   ├── analysis/       # cycles, complexity, dead code, impact
│   ├── api/            # FastAPI app
│   └── pr/             # Track B: diff mapping, checks, report
├── web/                # React UI
├── tests/
│   ├── unit/
│   ├── fixtures/       # tiny Python projects with known structure
│   └── eval/           # ground-truth evaluation harness
├── docs/
└── scripts/
```

---

## Commands (target; update as built)

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
uv run glyphrift index <path>
uv run glyphrift callers <symbol>
uv run glyphrift impact <symbol>
uv run glyphrift cycles
uv run glyphrift serve                    # local UI + API
uv run python -m tests.eval              # accuracy vs ground truth
# Track B (later)
uv run glyphrift pr-check --base <ref> --head <ref>
```

---

## Conventions
- Type hints everywhere; `ruff` clean; tests for every analysis feature.
- Fixtures: tiny hand-written projects in `tests/fixtures/` with known correct answers.
- No hidden network calls. Nothing leaves the user's machine in Track A.
- Unparseable files are recorded and reported, never crash the index.
- Schema changes need a migration note in the Decisions log.

---

## Current status

*(Update at the end of every session.)*

- Phase: **1, Indexer and evaluation harness** (8 of 10 items done)
- Done in Phase 0: name (GLYPHRIFT), license (MIT, "Void Core"), skeleton, CI (green), eval repos pinned in `docs/eval-repos.md`. Repo is public at github.com/Void-Core25/glyphrift
- Done in Phase 1:
  - File walker, SQLite schema + migrations, `ast` symbol extraction + `index_repo`. Verified on the user's Windows machine and CI
  - Import extraction + import resolver (schema v2 adds `import_facts`; `indexer/resolve.py`). Verified in Claude's sandbox only: ruff clean, pytest 124 passed / 1 skipped; on the Python 3.13 stdlib (538 files) it produced 4,033 import edges (3,202 resolved, 34 inferred, 0 heuristic, 797 unresolved: 781 stdlib, 16 genuine externals), 0 invariant violations. NOT yet run on the user's Windows machine or in CI
  - Call extraction + call resolver (schema v3 adds `call_facts`; `indexer/calls.py`). Verified on the user's Windows machine and in CI (3.11/3.12/3.13 green; one CI run failed once with a GitHub 504 while downloading `uv`, re-run was green). On the Python 3.13 stdlib it produced 30,589 call edges in about 4 s, 0 invariant violations; 14 randomly sampled edges hand-checked against source, all correct. Precision is NOT measured yet (that is the eval harness)
  - Inheritance (schema v4 adds `base_facts`; `indexer/inherit.py`, shared name lookup in `indexer/lookup.py`): `inherits` edges; `self.x()`, `cls.x()`, `Class.x()` and `super().x()` search the class hierarchy. Verified on the user's Windows machine and in CI; sandbox: pytest 165 passed / 1 skipped; stdlib: 2,191 inherits edges (1,808 resolved, 2 inferred, 381 unresolved = stdlib/third-party/builtin bases) and 30,601 call edges (6,239 unresolved, down from 7,216), 0 invariant violations; 15 sampled edges hand-checked
  - Robustness (`tests/unit/test_robustness.py`, 24 tests): probed with null bytes, bad encodings, UTF-16, Python 2 code, absurd nesting, symlinks, FIFOs, directories named `x.py`, odd file names, a 3 MB file. Found and fixed two real bugs: `RecursionError` escaping from deeply nested base-class expressions, and a crash on file names that are not valid UTF-8 (now skipped with a warning). New: files over 2 MiB are recorded with a `parse_error` ("file too large") and not parsed; any `RecursionError`/`MemoryError` during extraction becomes a `parse_error` for that file. Verified on the user's Windows machine (the two failures there were test assumptions about Python's parser recursion limit, fixed)
  - Fixture projects (`tests/fixtures/`, 5 projects, 18 files; harness `tests/unit/test_fixtures.py`; format in `tests/fixtures/README.md`): expected edges are written as `# => kind target confidence` comments in the fixture source and compared exactly (missing and extra edges both fail). Expectations were written from Python semantics before running the tool. Result: 4 of 5 fixtures matched first time once the layout was right; the mismatches were a fixture layout mistake and a method named `add` (suppressed by design, see Decisions). Verified in Claude's sandbox only: pytest 196 passed / 1 skipped. NOT yet run on the user's machine or in CI
  - Demo: `scripts/show_symbols.py <repo> [--file REL_PATH] [--errors] [--unresolved]`
- Next: the eval harness on the 5 pinned repos (hand-verified ground truth; precision/recall per confidence level), then the `glyphrift index <path>` CLI command. Needs the eval repos cloned locally by the user (`eval_repos/`, see `docs/eval-repos.md`)
- Not yet done: `glyphrift index` CLI command, eval harness and ground truth
- Open: user to confirm PyPI/GitHub/npm/domain/USPTO name checks before publishing a release
- Working notes: user is on Windows (PowerShell) with `uv`. Claude's sandbox has no network for installs and cannot clone GitHub repos, so anything needing new packages or the eval repos must be run by the user locally (or the user pastes output). Zip downloads do not include `.git`. Windows gotcha already hit once: `Path.write_text` converts `\n` to `\r\n`; tests that compare byte sizes must write bytes. The parser only understands the grammar of the Python running GLYPHRIFT, so run the eval harness under one recent Python (3.12+) for comparable results
- Design note for the UI phase: confidence `unresolved` currently also covers legitimate external imports (stdlib/third-party; the reason is in `how`). Consider a separate "external" classification (needs a schema migration) so the UI does not show `import os` as a failure. Eval precision/recall must be computed on in-repo edges only

## Decisions log

*(Append, never rewrite. Format: `YYYY-MM-DD: decision, reason`.)*

- 2026-11-07: Local-first CLI before any hosted site, to avoid auth, storage cost and untrusted-code risk.
- 2026-11-07: Python-only MVP, to get accuracy right on one language.
- 2026-11-07: SQLite instead of PostgreSQL; MVP runs locally.
- 2026-11-07: Single package layout instead of apps/packages monorepo.
- 2026-11-07: Every edge carries a confidence label; this is the core of the evidence principle.
- 2026-11-07: License: MIT, copyright "Void Core". Free, simple, compatible with all planned dependencies; no need for a stricter license on a portfolio project.
- 2026-11-07: Skeleton uses hatchling build backend, `src/` layout, ruff (E,F,I,UP,B; line length 100), pytest with `pythonpath=src`, CI matrix Python 3.11-3.13 via uv. CLI test uses `importorskip` so it skips where typer is absent.
- 2026-11-08: Fixture convention: expectations live next to the code as `# => kind target confidence` markers; every edge on a line without a marker is a test failure, so a fixture documents the complete graph. Known gaps surfaced while writing fixtures: (1) a repository whose root directory itself contains `__init__.py` (the repo *is* a package) loses its top-level package name, because module names come from directories below the root; real projects use a subfolder or `src/`; (2) calls like `obj.add(...)` or `items.append(...)` through a variable are not linked, because the method name also exists on builtin types (`set.add`, `list.append`); this trades recall for precision and is demonstrated in `tests/fixtures/limits`. Revisit both when the eval harness measures real recall.
- 2026-11-08: Failure policy: a file the indexer cannot read or parse never aborts a run. Unparseable source (any `SyntaxError`/`ValueError`, or a `RecursionError`/`MemoryError` while parsing or walking) is stored with `files.parse_error` and only its module symbol; the `ast` walkers are iterative, and only resource-limit errors are caught during extraction so genuine bugs still surface in tests. Files over `max_file_bytes` (default 2 MiB) are stored with a "too large" `parse_error` and not parsed. Unreadable files, symlinks, directories, named pipes and file names that are not valid UTF-8 are skipped (a warning is logged for unreadable and non-UTF-8 ones).
- 2026-11-08: Inheritance rules (`indexer/inherit.py`, `indexer/lookup.py`): each base expression is resolved like any name in the scope the class is defined in; `Generic[T]` counts as `Generic`; keywords (`metaclass=`) are not bases; `object` is skipped; only classes are valid targets. Every other base (stdlib, third-party, builtin like `Exception`, local variable, dynamic expression) is recorded as an `unresolved` edge with the reason, because a method not found in the repo may be inherited from it and call resolution says so. Edges are inserted in base order; `Hierarchy.linearize` walks them depth-first left-to-right, each class once (approximates Python's MRO: exact for single inheritance, diamonds can differ; cycles are safe). Method lookup: `self.m()`/`cls.m()` searches the class then its bases (`inferred`, subclass overrides are not considered); `super().m()` searches the bases only (`inferred`); `Class.m()` found in the class itself is `resolved`, found in a base `inferred`; confidence is also capped by the weakest base edge on the path. Name lookup was moved out of `calls.py` into `lookup.py` so bases and calls share it. Migration 4 clears the derived index again.
- 2026-11-08: Call edge rules (`indexer/calls.py`): the callee `root.attrs(...)` is resolved like Python resolves names (enclosing functions' nested defs and local imports, then module defs, import bindings, star imports); a root that is a local variable/parameter shadows outer names and is never linked to a same-named function. Attribute paths are followed through modules and into classes (methods defined directly in the class). Calling a class links to the class (instantiation, noted in `how`), not to `__init__`. `self.m()` is `inferred` (enclosing class only). Unknown receiver (`obj.save()`): `heuristic` edges to every repo method with that name if at most 4 candidates, else one `unresolved` edge; nothing is recorded if no repo method has the name or the name is also a method of builtin types (`append`, `get`, `close`...), to avoid noise. Not recorded at all: builtins, stdlib calls, `self()`. Third-party calls are `unresolved` with the module named. No type inference (`x = Foo(); x.bar()` is an unknown receiver). Dedupe key: (caller, target, callee text, line). Migration 3 clears the derived index again.
- 2026-11-08: Architecture for all derived edges: extraction stores raw per-file *facts* (symbols, `import_facts`; later call sites) incrementally; edges are then *rebuilt globally from facts* whenever any file changed (`resolve_imports`: DELETE + INSERT of all `imports` edges, no parsing). This keeps dependants correct when a file they import from changes, with no dependency tracking. Revisit only if measurements show resolution is slow on big repos. Migrations that change what is extracted clear the derived index (migration 2 does `DELETE FROM files`) so it is rebuilt on the next run.
- 2026-11-08: Import edge rules: one edge to the imported module per statement (`import a.b`, `from a.b import x`) plus, for `from M import name`, an edge to the symbol the name refers to, looked up in M as (1) class/function defined there, (2) module-level import binding (re-exports followed through any number of hops, cycle-safe), (3) submodule of package M, (4) names via `from X import *` (confidence `inferred`; `__all__` is not evaluated). Name that is a variable gives the module edge only. Duplicate definitions (conditional) give one `inferred` edge each; several modules with the same name (e.g. two `helpers.py` in non-package dirs) give `heuristic` edges to every candidate. Imports inside functions/classes are attributed to that function/class; imports under `if TYPE_CHECKING:` are flagged in `how`. `from __future__` is ignored. Known gaps: implicit parent-package imports (`import a.b.c` does not add edges to `a`, `a.b`), `__all__`, namespace packages, dynamic imports.
- 2026-11-08: Symbol extraction rules: one `module` symbol per file, always first, even when parsing fails (file gets `parse_error`, nothing raised); definitions inside if/try/with/for/while/match belong to the enclosing scope; a function directly in a class body is a `method`, otherwise a `function`; qualified names are plain dotted (`pkg.mod.outer.inner`, no `<locals>`) and not unique; a decorated definition starts at its first decorator (so Track B can map changed decorator lines to the right symbol). The walk is iterative so deep nesting cannot overflow the stack. Source is parsed from bytes so encoding declarations and BOMs work; parser SyntaxWarnings/DeprecationWarnings are suppressed (old code is full of invalid escapes).
- 2026-11-08: Module names: start at the outermost directory of the unbroken chain of packages (dirs with `__init__.py`/`.pyi`) containing the file, so `src/pkg/mod.py` is `pkg.mod` and `tests/unit/test_x.py` is `test_x`. Namespace packages (no `__init__.py`) are not recognised. Revisit when the import resolver needs source roots.
- 2026-11-08: `index_repo` stores only files with a supported language (Python); re-indexes a file when its sha256 or its computed module name changed (adding an `__init__.py` renames sibling modules); removes vanished/now-ignored files; whole run is one transaction. Symbols only so far; when edges exist it must also re-index dependants of changed files. `dist-packages` added to default skipped dirs.
- 2026-11-08: Schema v1 added beyond the original sketch: `files.parse_error` (record unparseable files instead of failing), `findings.severity` values (`info|low|medium|high`), CHECK constraints on enums, line ranges and `json_valid(evidence_json)`, and the invariant `(confidence = 'unresolved') = (dst_symbol_id IS NULL)` so an edge can never claim to be resolved without a target. All FKs cascade, including `edges.dst_symbol_id` (dangling edges are impossible; dependants must be re-indexed). Migrations are Python strings versioned by `PRAGMA user_version`, each applied atomically with its version bump; no ORM and no migration library. Connections: autocommit mode, foreign keys on, WAL for file databases, `transaction()` helper for grouped writes.
- 2026-11-08: File walker uses `git ls-files --cached --others --exclude-standard` in git repos so `.gitignore` semantics are exactly git's, with no new dependency (the `pathspec` library was the alternative). Fallback tree walk does not interpret `.gitignore` (documented). Default skipped dirs include `build` and `dist`; overridable via `skip_dirs`. Symlinks are never followed. Unreadable files are skipped with a logged warning.
- 2026-11-08: Eval repo commits pinned (see docs/eval-repos.md). Phase 0 required items complete.
- 2026-11-08: Eval repos chosen: click, requests, flask, rich, httpx (mix of decorator-heavy, classic class-based, and fully typed code). httpx picked because type hints give solid ground truth for `inferred` edges.
- 2026-11-08: Repo created public at github.com/Void-Core25/glyphrift (portfolio visibility; Actions free on public repos). CI green on first push.
- 2026-11-07: Name finalised: GLYPHRIFT (spelled G-L-Y-P-H-R-I-F-T; package/CLI `glyphrift`). Registry checks (PyPI, GitHub, npm, domain, USPTO) still to be done by the user before publishing.
- 2026-11-07: Dropped name "Cartog" (existing code-graph tool). Name pending.
- 2026-11-07: Goals are learning + portfolio + fun, not a startup. Scope trimmed accordingly (no MCP, no hosted site, no multi-language).
- 2026-11-07: Track A (code graph + UI) first for low risk and fun; Track B (PR evidence) second as the distinctive, harder layer.
