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

Clones go in `eval_repos/` (git-ignored). To check out a pinned commit:

```
git clone <url> eval_repos/<name>
git -C eval_repos/<name> checkout <commit>
```

A script to automate this will be added with the Phase 1 eval harness.

## Ground truth

Hand-verified ground truth (true callers of ~30-50 symbols per repo) goes in
`tests/eval/ground_truth/<name>.yaml`. Not started; Phase 1.
