"""Index a repository into an in-memory database and print what GLYPHRIFT found.

Usage:
    uv run python scripts/show_symbols.py <repo-path>                 # summary only
    uv run python scripts/show_symbols.py <repo-path> --file pkg/mod.py   # symbol tree of a file
    uv run python scripts/show_symbols.py <repo-path> --errors        # list unparseable files
"""

from __future__ import annotations

import argparse
import time

from glyphrift.indexer import index_repo
from glyphrift.store import connect


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("repo", help="path to the repository to index")
    parser.add_argument("--file", help="print the symbol tree of this file (path relative to repo)")
    parser.add_argument("--errors", action="store_true", help="list files that failed to parse")
    parser.add_argument(
        "--unresolved",
        action="store_true",
        help="list imports and calls that could not be resolved",
    )
    args = parser.parse_args()
    if args.file:
        args.file = args.file.replace("\\", "/")  # stored paths always use forward slashes

    conn = connect(":memory:")
    started = time.perf_counter()
    stats = index_repo(args.repo, conn)
    elapsed = time.perf_counter() - started

    kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM symbols GROUP BY kind").fetchall())
    print(f"Indexed {stats.files_added} Python files in {elapsed:.2f}s")
    print(
        "Symbols: "
        + ", ".join(
            f"{kinds.get(kind, 0)} {plural}"
            for kind, plural in (
                ("module", "modules"),
                ("class", "classes"),
                ("function", "functions"),
                ("method", "methods"),
            )
        )
        + f" ({stats.symbols_written} total)"
    )
    print(f"Files that failed to parse: {len(stats.parse_errors)}")

    for kind, label in (("imports", "Import"), ("calls", "Call")):
        by_confidence = dict(
            conn.execute(
                "SELECT confidence, COUNT(*) FROM edges WHERE kind = ? GROUP BY confidence",
                (kind,),
            ).fetchall()
        )
        print(
            f"{label} edges: {sum(by_confidence.values())} ("
            + ", ".join(
                f"{by_confidence.get(level, 0)} {level}"
                for level in ("resolved", "inferred", "heuristic", "unresolved")
            )
            + ")"
        )

    if args.errors:
        for path, message in stats.parse_errors:
            print(f"  {path}: {message}")

    if args.unresolved:
        for kind in ("imports", "calls"):
            rows = conn.execute(
                "SELECT f.path, e.line, e.dst_name, e.how FROM edges e "
                "JOIN files f ON f.id = e.file_id "
                "WHERE e.kind = ? AND e.confidence = 'unresolved' ORDER BY f.path, e.line",
                (kind,),
            ).fetchall()
            print(f"\nUnresolved {kind} ({len(rows)}):")
            for row in rows[:200]:
                print(f"  {row['path']}:{row['line']}  {row['dst_name']}  - {row['how']}")
            if len(rows) > 200:
                print(f"  ... and {len(rows) - 200} more")

    if args.file:
        rows = conn.execute(
            "SELECT s.id, s.kind, s.name, s.line_start, s.line_end, s.parent_id "
            "FROM symbols s JOIN files f ON f.id = s.file_id WHERE f.path = ? ORDER BY s.id",
            (args.file,),
        ).fetchall()
        if not rows:
            print(f"\nNo symbols found for {args.file!r} (is the path relative to the repo?)")
            return
        print(f"\n{args.file}")
        depth: dict[int, int] = {}
        for row in rows:
            depth[row["id"]] = depth.get(row["parent_id"], -1) + 1
            indent = "  " * depth[row["id"]]
            label = f"{row['kind']:8} {row['name']}"
            print(f"  {indent}{label}  (lines {row['line_start']}-{row['line_end']})")

        for kind, title in (("imports", "Imports"), ("calls", "Calls")):
            edge_rows = conn.execute(
                "SELECT e.line, e.confidence, e.dst_name, dst.qualified_name AS dst, e.how "
                "FROM edges e JOIN files f ON f.id = e.file_id "
                "LEFT JOIN symbols dst ON dst.id = e.dst_symbol_id "
                "WHERE e.kind = ? AND f.path = ? ORDER BY e.line, e.id",
                (kind, args.file),
            ).fetchall()
            print(f"\n  {title} ({len(edge_rows)}):")
            for edge in edge_rows:
                target = edge["dst"] or f"({edge['dst_name']})"
                print(f"    line {edge['line']:<4} {edge['confidence']:10} {target}")


if __name__ == "__main__":
    main()
