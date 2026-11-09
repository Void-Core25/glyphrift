"""Draw a sample of edges from a repository for hand labelling.

    uv run python scripts/eval_sample.py eval_repos/click

Writes tests/eval/labels/<name>.json (use --out to change). Running it again with the same seed
on the same commit gives the same sample and keeps every label already filled in. Open the file,
fill in "label" ("correct", "wrong" or "unsure"), "by" (who judged) and optionally "note" for
each entry. The meaning of the labels is in glyphrift/evaluation.py.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from glyphrift.evaluation import DEFAULT_QUOTAS, build_samples, merge_labels
from glyphrift.indexer import index_repo
from glyphrift.store import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repo", help="path to the repository checkout")
    parser.add_argument("--name", help="repository name (default: folder name)")
    parser.add_argument("--out", help="output file (default: tests/eval/labels/<name>.json)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--scale", type=float, default=1.0, help="multiply every sample size")
    parser.add_argument("--include-tests", action="store_true", help="also sample test/docs code")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    name = args.name or root.name
    out = Path(args.out) if args.out else Path("tests/eval/labels") / f"{name}.json"
    quotas = {key: max(1, round(n * args.scale)) for key, n in DEFAULT_QUOTAS.items()}

    conn = connect(":memory:")
    started = time.perf_counter()
    stats = index_repo(root, conn)
    print(f"Indexed {stats.files_added} files in {time.perf_counter() - started:.2f}s")

    entries, counts = build_samples(
        conn, root, name, quotas=quotas, seed=args.seed, include_tests=args.include_tests
    )
    if out.exists():
        existing = json.loads(out.read_text(encoding="utf-8"))
        entries = merge_labels(entries, existing)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(entries, handle, indent=1, ensure_ascii=False)
        handle.write("\n")

    print(f"\n{'kind':10} {'confidence':11} {'available':>9} {'sampled':>8}")
    for (kind, confidence), (available, sampled) in sorted(counts.items()):
        print(f"{kind:10} {confidence:11} {available:>9} {sampled:>8}")
    labelled = sum(1 for e in entries if e["label"] is not None)
    print(f"\n{len(entries)} entries written to {out} ({labelled} already labelled)")


if __name__ == "__main__":
    main()
