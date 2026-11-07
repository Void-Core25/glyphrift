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

- Phase: **1, Indexer and evaluation harness** (3 of 10 items done)
- Done in Phase 0: name (GLYPHRIFT), license (MIT, "Void Core"), skeleton, CI (green), eval repos pinned in `docs/eval-repos.md`. Repo is public at github.com/Void-Core25/glyphrift
- Done in Phase 1:
  - File walker (`indexer/walker.py`) and SQLite schema + migrations (`store/`). Verified on the user's Windows machine and CI (40 passed, 1 skipped)
  - `ast` symbol extraction (`indexer/extract.py`) and `index_repo` (`indexer/pipeline.py`) storing files + symbols; demo `scripts/show_symbols.py <repo> [--file REL_PATH] [--errors]`. Verified in Claude's sandbox only: ruff clean, pytest 90 passed / 1 skipped; indexed the Python 3.13 standard library (538 files, 16,303 symbols) in 1.5 s with 0 parse failures. NOT yet run on the user's Windows machine or in CI
- Next: import extraction + import resolver (absolute, relative, packages, `__init__`, `from x import *`), producing `imports` edges
- Not yet done: any edges (imports/calls/inherits), the `glyphrift index` CLI command, eval harness and ground truth
- Open: user to confirm PyPI/GitHub/npm/domain/USPTO name checks before publishing a release
- Working notes: user is on Windows (PowerShell) with `uv`. Claude's sandbox has no network for installs and cannot clone GitHub repos, so anything needing new packages or the eval repos must be run by the user locally (or the user pastes output). Zip downloads do not include `.git`. Windows gotcha already hit once: `Path.write_text` converts `\n` to `\r\n`; tests that compare byte sizes must write bytes. The parser only understands the grammar of the Python running GLYPHRIFT, so run the eval harness under one recent Python (3.12+) for comparable results

## Decisions log

*(Append, never rewrite. Format: `YYYY-MM-DD: decision, reason`.)*

- 2026-11-07: Local-first CLI before any hosted site, to avoid auth, storage cost and untrusted-code risk.
- 2026-11-07: Python-only MVP, to get accuracy right on one language.
- 2026-11-07: SQLite instead of PostgreSQL; MVP runs locally.
- 2026-11-07: Single package layout instead of apps/packages monorepo.
- 2026-11-07: Every edge carries a confidence label; this is the core of the evidence principle.
- 2026-11-07: License: MIT, copyright "Void Core". Free, simple, compatible with all planned dependencies; no need for a stricter license on a portfolio project.
- 2026-11-07: Skeleton uses hatchling build backend, `src/` layout, ruff (E,F,I,UP,B; line length 100), pytest with `pythonpath=src`, CI matrix Python 3.11-3.13 via uv. CLI test uses `importorskip` so it skips where typer is absent.
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
