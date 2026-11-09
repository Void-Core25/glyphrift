# Evaluation repositories

Pinned on 2026-11-08 (latest commit of each default branch at that time). The eval harness
(`tests/eval/`) must always analyse exactly these commits so results are comparable over time.
Do not change a hash without recording why in the README Decisions log; changing it invalidates
the ground truth.

| Repo | Commit | URL | Why it's here |
|---|---|---|---|
| click | `8abb77a8432e03ed14a95f972b44f905279415a7` | https://github.com/pallets/click | Decorator-heavy CLI library |
| requests | `3c7baaa05ab4aa49cc819ce3a819d260de19c157` | https://github.com/psf/requests | Classic class-based library |
| flask | `49943b5a94413a530761350f04e896f76e8d620e` | https://github.com/pallets/flask | Web framework with proxies and decorators (hard cases) |
| rich | `3a109ccdffe5b2d0e30684a304fbd388e99ce57b` | https://github.com/Textualize/rich | Large, class-heavy rendering library |
| httpx | `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` | https://github.com/encode/httpx | Fully type-annotated; type hints give solid ground truth for `inferred` edges |

## Local checkout

Clones go in `eval_repos/` (git-ignored). `scripts/clone_eval_repos.ps1` clones every repo in the
table above and checks out the pinned commit (safe to re-run; it prints `OK` or `MISMATCH` per
repo):

```
powershell -ExecutionPolicy Bypass -File scripts\clone_eval_repos.ps1
```

## Measuring accuracy

Accuracy is measured by sampling, not by judging every edge:

1. `uv run python scripts/eval_sample.py eval_repos/<name>` indexes the repo and draws a seeded
   random sample of edges per kind and confidence (calls: 30 resolved, 20 each inferred /
   heuristic / unresolved; inherits and imports: 8 each; tests and docs code is not sampled).
   It writes `tests/eval/labels/<name>.json`. Re-running with the same seed and commit gives the
   same sample and keeps existing labels.
2. Every entry gets `label` (`correct` / `wrong` / `unsure`), `by` (who judged) and optionally
   `note`. Meanings are in `src/glyphrift/evaluation.py`.
3. `uv run python scripts/eval_report.py --repos-dir eval_repos` writes `docs/eval-report.md`:
   precision per kind and confidence with a 95% Wilson interval, whether each checkout is at its
   pinned commit, index size and timings, and the Phase 1 verdict (`resolved` call edges >= 95%
   precision, needs at least 100 decided labels).

Precision from sampling says how often an edge is right. It does not say how many true calls are
missing (recall); that is a separate check on the callers of ~30 chosen symbols per repo.
Labels are only valid for the pinned commit: do not change a hash without discarding the labels.
