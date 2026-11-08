"""Index a repository into the SQLite store.

Records files, their symbols and their raw import statements (incremental: only new or
changed files are parsed), then rebuilds every ``imports`` edge from those stored facts
whenever anything changed (see ``resolve.py``), then does the same for ``calls`` edges (see
``calls.py``), so files that depend on a changed file get correct edges without being
re-parsed. Inheritance edges come in a later roadmap item and will follow the same pattern:
store per-file facts, derive edges globally.

Only files with a supported language (currently Python) are stored.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from glyphrift.indexer.calls import resolve_calls
from glyphrift.indexer.extract import compute_module_name, extract_symbols
from glyphrift.indexer.resolve import resolve_imports
from glyphrift.indexer.walker import FileRecord, walk_repo
from glyphrift.store import transaction

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexStats:
    files_added: int = 0
    files_updated: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    symbols_written: int = 0
    import_edges: int = 0  # import edges written by this run (0 if nothing changed)
    unresolved_imports: int = 0
    call_edges: int = 0  # call edges written by this run (0 if nothing changed)
    unresolved_calls: int = 0
    parse_errors: list[tuple[str, str]] = field(default_factory=list)  # (path, message)


def index_repo(root: str | Path, conn: sqlite3.Connection) -> IndexStats:
    """Bring the index in ``conn`` up to date with the repository at ``root``.

    A file is re-indexed when it is new, its content hash changed, or its module name changed
    (adding or removing an ``__init__.py`` renames the modules below it). Files that no longer
    exist (or are now ignored) are removed. The whole run is one transaction.
    """
    root_path = Path(root)
    records = [r for r in walk_repo(root_path) if r.language is not None]
    python_files = {r.path for r in records if r.language == "python"}
    stats = IndexStats()

    existing = {
        row["path"]: (row["id"], row["sha256"], row["module_name"])
        for row in conn.execute(
            "SELECT f.path, f.id, f.sha256, m.qualified_name AS module_name "
            "FROM files f LEFT JOIN symbols m ON m.file_id = f.id AND m.kind = 'module'"
        )
    }
    current_paths = {r.path for r in records}
    indexed_at = datetime.now(UTC).isoformat(timespec="seconds")

    with transaction(conn):
        for path in sorted(set(existing) - current_paths):
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
            stats.files_removed += 1

        for record in records:
            module_name = compute_module_name(record.path, python_files)
            previous = existing.get(record.path)
            if previous is not None and previous[1] == record.sha256 and previous[2] == module_name:
                stats.files_unchanged += 1
                continue
            written = _index_file(root_path, record, module_name, previous, conn, indexed_at, stats)
            if written and previous is None:
                stats.files_added += 1
            elif written:
                stats.files_updated += 1

        if stats.files_added or stats.files_updated or stats.files_removed:
            resolution = resolve_imports(conn)
            stats.import_edges = resolution.edges
            stats.unresolved_imports = resolution.unresolved
            call_resolution = resolve_calls(conn)
            stats.call_edges = call_resolution.edges
            stats.unresolved_calls = call_resolution.unresolved
    return stats


def _index_file(
    root: Path,
    record: FileRecord,
    module_name: str,
    previous: tuple[int, str, str | None] | None,
    conn: sqlite3.Connection,
    indexed_at: str,
    stats: IndexStats,
) -> bool:
    try:
        data = (root / record.path).read_bytes()
    except OSError as exc:
        logger.warning("Skipping unreadable file %s: %s", record.path, exc)
        return False

    result = extract_symbols(data, module_name)
    if result.parse_error is not None:
        stats.parse_errors.append((record.path, result.parse_error))

    if previous is not None:
        conn.execute("DELETE FROM files WHERE id = ?", (previous[0],))  # cascades to symbols
    cursor = conn.execute(
        "INSERT INTO files (path, language, sha256, size, indexed_at, parse_error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (record.path, record.language, record.sha256, len(data), indexed_at, result.parse_error),
    )
    file_id = cursor.lastrowid

    row_ids: list[int] = []
    for symbol in result.symbols:  # parents always precede their children
        parent_id = row_ids[symbol.parent] if symbol.parent is not None else None
        cursor = conn.execute(
            "INSERT INTO symbols (file_id, kind, name, qualified_name, line_start, line_end, "
            "parent_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                symbol.kind,
                symbol.name,
                symbol.qualified_name,
                symbol.line_start,
                symbol.line_end,
                parent_id,
            ),
        )
        assert cursor.lastrowid is not None
        row_ids.append(cursor.lastrowid)
    stats.symbols_written += len(row_ids)

    conn.executemany(
        "INSERT INTO import_facts (file_id, scope_symbol_id, kind, level, module, name, asname, "
        "line, type_checking) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                file_id,
                row_ids[imp.scope],
                imp.kind,
                imp.level,
                imp.module,
                imp.name,
                imp.asname,
                imp.line,
                int(imp.type_checking),
            )
            for imp in result.imports
        ],
    )
    conn.executemany(
        "INSERT INTO call_facts (file_id, scope_symbol_id, root, attrs, line, "
        "opaque_in_symbol_id, receiver_class_symbol_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                file_id,
                row_ids[call.scope],
                call.root,
                call.attrs,
                call.line,
                row_ids[call.opaque_in] if call.opaque_in is not None else None,
                row_ids[call.receiver_class] if call.receiver_class is not None else None,
            )
            for call in result.calls
        ],
    )
    return True
