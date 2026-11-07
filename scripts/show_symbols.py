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
    args = parser.parse_args()

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

    if args.errors:
        for path, message in stats.parse_errors:
            print(f"  {path}: {message}")

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


if __name__ == "__main__":
    main()
