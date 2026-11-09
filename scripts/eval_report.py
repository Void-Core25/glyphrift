"""Compute precision from labelled samples and write docs/eval-report.md.

    uv run python scripts/eval_report.py
    uv run python scripts/eval_report.py --repos-dir eval_repos   # also index timings

Reads every tests/eval/labels/*.json. With --repos-dir it also indexes each checkout (twice, to
time a cold and an unchanged run) and checks that it is at the commit pinned in
docs/eval-repos.md, because labels are only valid for that exact commit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from collections import Counter
from datetime import date
from pathlib import Path

from glyphrift.evaluation import Stratum, summarize, validate_labels
from glyphrift.indexer import index_repo
from glyphrift.store import connect

REQUIRED_PRECISION = 0.95
MIN_DECIDED_FOR_VERDICT = 100
PINNED_ROW = re.compile(r"^\|\s*(\w+)\s*\|\s*`([0-9a-f]{40})`", re.MULTILINE)


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def strata_table(strata: list[Stratum]) -> list[str]:
    lines = [
        "| kind | confidence | correct | wrong | unsure | unlabelled | precision | 95% interval |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for s in strata:
        interval = s.interval
        text = "n/a" if interval is None else f"{percent(interval[0])} to {percent(interval[1])}"
        lines.append(
            f"| {s.kind} | {s.confidence} | {s.correct} | {s.wrong} | {s.unsure} | "
            f"{s.unlabelled} | {percent(s.precision)} | {text} |"
        )
    return lines


def pinned_commits(doc: Path) -> dict[str, str]:
    return dict(PINNED_ROW.findall(doc.read_text(encoding="utf-8"))) if doc.exists() else {}


def checkout_commit(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def performance_section(repos_dir: Path, pinned: dict[str, str]) -> list[str]:
    lines = [
        "## Index size and speed",
        "",
        "| repo | commit pinned? | files | symbols | lines | cold index | unchanged re-index | "
        "import edges | inherit edges | call edges | parse errors |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for path in sorted(p for p in repos_dir.iterdir() if p.is_dir()):
        head = checkout_commit(path)
        if path.name not in pinned:
            status = "not in docs/eval-repos.md"
        elif head is None:
            status = "unknown (git failed)"
        else:
            status = "yes" if head == pinned[path.name] else f"NO ({head[:8]})"
        conn = connect(":memory:")
        started = time.perf_counter()
        stats = index_repo(path, conn)
        cold = time.perf_counter() - started
        started = time.perf_counter()
        index_repo(path, conn)
        warm = time.perf_counter() - started
        symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        code_lines = conn.execute(
            "SELECT COALESCE(SUM(line_end), 0) FROM symbols WHERE kind = 'module'"
        ).fetchone()[0]
        edge_counts = Counter(
            {k: n for k, n in conn.execute("SELECT kind, COUNT(*) FROM edges GROUP BY kind")}
        )
        lines.append(
            f"| {path.name} | {status} | {stats.files_added} | {symbols} | {code_lines} | "
            f"{cold:.2f}s | {warm:.2f}s | {edge_counts['imports']} | {edge_counts['inherits']} | "
            f"{edge_counts['calls']} | {len(stats.parse_errors)} |"
        )
        conn.close()
    return lines + [""]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--labels", default="tests/eval/labels")
    parser.add_argument("--out", default="docs/eval-report.md")
    parser.add_argument("--repos-dir", help="folder with the repository checkouts")
    parser.add_argument("--pins", default="docs/eval-repos.md")
    args = parser.parse_args()

    per_repo: dict[str, list[dict]] = {}
    for file in sorted(Path(args.labels).glob("*.json")):
        per_repo[file.stem] = json.loads(file.read_text(encoding="utf-8"))
    if not per_repo:
        raise SystemExit(f"No label files in {args.labels}")
    everything = [entry for entries in per_repo.values() for entry in entries]

    problems = [
        f"{name}: {p}" for name, entries in per_repo.items() for p in validate_labels(entries)
    ]
    if problems:
        raise SystemExit("Label problems:\n  " + "\n  ".join(problems))

    pooled = summarize(everything)
    resolved_calls = next(
        (s for s in pooled if (s.kind, s.confidence) == ("calls", "resolved")), None
    )
    if resolved_calls is None or resolved_calls.decided < MIN_DECIDED_FOR_VERDICT:
        decided = 0 if resolved_calls is None else resolved_calls.decided
        verdict = f"NOT ENOUGH LABELS ({decided} decided, need {MIN_DECIDED_FOR_VERDICT})"
    else:
        interval = resolved_calls.interval
        ok = resolved_calls.precision >= REQUIRED_PRECISION
        verdict = (
            f"{'MET' if ok else 'NOT MET'}: {percent(resolved_calls.precision)} "
            f"(95% interval {percent(interval[0])} to {percent(interval[1])}) "
            f"against the required {percent(REQUIRED_PRECISION)}"
        )

    by = Counter(e["by"] for e in everything if e.get("label") is not None)
    out = [
        "# Evaluation report",
        "",
        f"Generated by `scripts/eval_report.py` on {date.today().isoformat()}. "
        "Do not edit by hand.",
        "",
        "Precision is measured on random samples of edges, labelled by reading the source "
        "(label meanings: `glyphrift/evaluation.py`). It estimates how often an edge is right; "
        "it is **not** recall. For `unresolved` edges, 'correct' means the reference really is "
        "outside the repository or undecidable from the source, and 'wrong' means a reader could "
        "name the repository symbol (a miss).",
        "",
        f"**Phase 1 exit criterion** (calls, `resolved`, pooled over all repos): {verdict}.",
        "",
        "Labelled by: "
        + (", ".join(f"{who} ({n})" for who, n in sorted(by.items())) or "nobody yet"),
        "",
        "## All repositories",
        "",
        *strata_table(pooled),
        "",
    ]
    for name, entries in per_repo.items():
        out += [f"## {name}", "", *strata_table(summarize(entries)), ""]
    if args.repos_dir:
        out += performance_section(Path(args.repos_dir), pinned_commits(Path(args.pins)))

    Path(args.out).write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {args.out}\nPhase 1 criterion: {verdict}")


if __name__ == "__main__":
    main()
