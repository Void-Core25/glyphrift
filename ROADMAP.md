# Roadmap

Goals: learning, portfolio, fun. Track A (code graph + UI) first, then Track B (PR evidence).
One phase at a time. A phase is done only when its **exit criteria** are met. Time estimates assume part-time work with Claude writing most of the code and you reviewing, testing and deciding. They are guesses; revise after Phase 1.
New ideas go in the **Parking lot**, not into the current phase.

---

# TRACK A: Code graph (the fun part)

## Phase 0: Setup (about 2-3 days)

- [x] Project name: GLYPHRIFT (still verify PyPI, GitHub, npm, domain, USPTO before publishing)
- [x] Choose a license: MIT, copyright "Void Core" (`LICENSE` file written)
- [x] `git init`; `pyproject.toml`; `src/` skeleton; pytest + ruff; GitHub Actions CI workflow written (not yet run on GitHub; first push will confirm)
- [ ] Pick 5 eval repos and pin commit hashes (suggested: `click`, `requests`, `flask`, `rich`, plus one you know well)
- [ ] Rough sketch of the main UI screen (paper is fine)

**Exit:** `uv run pytest` passes on the empty skeleton in CI; name, license and eval repos decided.

## Phase 1: Indexer and evaluation harness (about 2 weeks)

Goal: reliably extract Python structure, and be able to *measure* correctness.

- [ ] File walker: respects `.gitignore`, skips venvs/build dirs, records sha256
- [ ] SQLite schema + migrations (`files`, `symbols`, `edges`, `findings`)
- [ ] `ast` extraction: modules, classes, functions, methods, line ranges
- [ ] Import extraction + **import resolver** (absolute, relative, packages, `__init__`, `from x import *`)
- [ ] Call extraction with confidence labels (`resolved` / `inferred` / `heuristic` / `unresolved`)
- [ ] Inheritance edges; `self.method()` resolution through class hierarchy
- [ ] Graceful handling of syntax errors and unparseable files
- [ ] Hand-written fixture projects in `tests/fixtures/` with known correct graphs
- [ ] **Eval harness**: for each eval repo, hand-verify ground truth for ~30-50 symbols (true callers) in YAML; report precision/recall per confidence level
- [ ] `glyphrift index <path>` works end to end

**Exit:** all 5 eval repos index without crashing; eval report exists; `resolved` edges have **>=95% precision** on ground truth (if lower, fix before moving on); index times recorded.

## Phase 2: Graph UI (about 2 weeks) FUN

Goal: see the thing. Build this as soon as the index has edges.

- [ ] FastAPI app: read-only endpoints (`/symbols`, `/symbol/{id}`, `/graph?focus=...`, `/files`)
- [ ] React + Vite + TypeScript scaffold served by `glyphrift serve`
- [ ] Symbol explorer: tree + search
- [ ] Interactive graph (React Flow or Cytoscape): focus on a symbol, expand callers/callees, zoom/pan
- [ ] Confidence shown visually (solid / dashed / dotted edges; legend; filter toggle)
- [ ] Symbol panel: definition, callers, callees, evidence file:line, click to jump
- [ ] Module-level view (imports) and symbol-level view (calls)
- [ ] Dark mode and a design you like; this is your portfolio's face

**Exit:** you can open any eval repo in the UI and explore it comfortably. Take screenshots.

## Phase 3: Analysis and findings (about 1-2 weeks)

- [ ] `callers` / `callees` / `dependents` CLI commands, with confidence shown
- [ ] `glyphrift impact <symbol>`: transitive callers, affected modules, probable affected tests (test association by reachability from `test_*` files, labelled `inferred`, never "covered")
- [ ] `glyphrift cycles`: circular imports with the full path as evidence
- [ ] Cyclomatic complexity per function; `hotspots` ranks by complexity x fan-in
- [ ] Dead-code **candidates** (no inbound edges, not exported, not an entry point, not dunder), always labelled "candidate" with reasons
- [ ] Incremental indexing; test that incremental == full re-index
- [ ] `--json` flag on every command
- [ ] Surface findings and impact in the UI (findings tab, "show blast radius" on a symbol)

**Exit:** all commands work on eval repos; incremental == full in a test; cycles and dead-code candidates hand-reviewed for false positives on 2 repos and recorded.

## Phase 4: Polish and ship Track A (about 1 week)

- [ ] README with screenshots, demo GIF, 60-second quickstart
- [ ] Export report (self-contained HTML or Markdown)
- [ ] Short write-up: what it does, how accuracy was measured, what it gets wrong
- [ ] (Optional) Try `cartog` on a repo you know for ~1 hour and add a short "how this differs" note to the README (confidence labels, published eval numbers). Be fair about what it does well
- [ ] Publish to GitHub (issue templates, CONTRIBUTING); optionally PyPI
- [ ] Ask 3-5 friends to install it from the README and note where they get stuck

**Exit:** a stranger can install and view their repo's graph in under 5 minutes following the README. Track A is done: you have a finished portfolio project even if you stop here.

---

# TRACK B: PR evidence (the serious part)

Start only after Phase 4. Goal: given a pull request, produce an evidence report about what it *does*. Present facts and confidence, never a verdict, never auto-close.

## Phase 5: PR evidence engine (about 4-6 weeks)

### 5a. Validate first (2-3 days)
- [ ] Talk to 5-10 maintainers (GitHub issues/Discord/Reddit): "Would this report help you review PRs?" Record answers in `docs/validation.md`
- [ ] Re-check competitors (anti-slop, SlopGuard, CodeRabbit, new GitHub features); update the README "Why Track B" section
- [ ] Decide: continue, narrow (e.g. only fail-then-pass), or stop

### 5b. Diff to symbols
- [ ] Given `--base` and `--head`, use `git diff` to find changed files and lines
- [ ] Map changed lines to symbols using the index (index both refs, via `git worktree`)
- [ ] Report added / removed / modified symbols

### 5c. Symbol-existence check
- [ ] For names and imports a PR introduces, check they exist (in the repo, stdlib, or installed deps)
- [ ] Report unresolved references with file:line evidence; label confidence (dynamic code means false positives are possible)

### 5d. Blast radius
- [ ] From changed symbols, compute callers and probable affected tests using Track A impact analysis
- [ ] Keep confidence labels; show "unknown" where the graph can't tell

### 5e. Fail-on-base / pass-on-PR check (the hard, distinctive bit)
- [ ] Identify tests added or modified by the PR
- [ ] Run them on base (with the PR's test files overlaid) and on head; compare
- [ ] Classify: `fails-then-passes` (good evidence), `passes-both` (test may not test the change), `fails-both`, `cannot-run` (env problem; say so, don't guess)
- [ ] Never claim more than the evidence supports: "tests pass" is not "bug-free"

**Safety notes (required):**
- Treat PR code as untrusted. Default design: run inside the repo's own GitHub Actions workflow on a runner with **no secrets**, so GitHub's sandbox is the boundary. Do not use `pull_request_target` with checkout of untrusted code.
- Locally: run in a throwaway container with network disabled, resource limits, no mounted secrets. Docker alone is not a strong boundary for hostile code; document that limit.
- Reports must never print detected secrets.

### 5f. Report and Action
- [ ] Markdown report: summary table (check | result | confidence | evidence)
- [ ] `glyphrift pr-check --base --head` CLI
- [ ] GitHub Action that posts/updates one PR comment; read-only otherwise

### 5g. Evaluation and write-up
- [ ] Collect 30-50 real PRs (merged, closed-unmerged, and obviously low-quality) from public repos you chose in advance
- [ ] Run the tool; record what each check found; compare with the human outcome
- [ ] Report precision, false positives, cannot-run rate, honestly, including failures
- [ ] Write-up + demo GIF; update README

**Exit:** the Action runs on at least 2 real repos (with owners' permission); evaluation results published; limits documented.

---

## Metrics (check each phase)

- Index time per 1k files / 100k LOC
- `resolved` edge precision/recall on ground truth
- False-positive rate on cycles and dead-code candidates (hand-reviewed sample)
- Time from install to first useful graph
- Track B: cannot-run rate, false-positive rate, agreement with maintainer outcomes

A tool that gets five findings right beats fifty questionable warnings.

---

## Parking lot (not now)

- MCP server exposing the index to AI tools
- VS Code extension
- Multi-language support (Tree-sitter / SCIP)
- Hosted version, accounts, team dashboards
- AI chat UI; test generation
- Dependency vulnerability scanning (wrap OSV-Scanner/Semgrep/gitleaks; note `ca9` exists for Python reachability)
- PULSE-style health trends from git history
- Branch-to-branch architecture comparison
- A single "health score" (only with a transparent methodology)
