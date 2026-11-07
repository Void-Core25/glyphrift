"""Schema migrations for the GLYPHRIFT index database.

Rules:

* Migrations are append-only. Never edit or reorder a migration that has been released;
  add a new one instead.
* Versions are consecutive integers starting at 1. The database's ``PRAGMA user_version``
  records the last applied version.
* Each migration runs in a single transaction together with the version bump, so a failed
  migration leaves the database unchanged.
* Write a note in the README Decisions log for every schema change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    description: str
    sql: str


_V1_INITIAL_SCHEMA = """
CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL UNIQUE,            -- POSIX path relative to the repo root
    language    TEXT,                               -- e.g. 'python'; NULL if unsupported
    sha256      TEXT    NOT NULL,
    size        INTEGER NOT NULL CHECK (size >= 0),
    indexed_at  TEXT    NOT NULL,                   -- ISO 8601 UTC timestamp
    parse_error TEXT                                -- NULL if parsed fine, else the reason
);

CREATE TABLE symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL
                   CHECK (kind IN ('module', 'class', 'function', 'method')),
    name           TEXT    NOT NULL,
    qualified_name TEXT    NOT NULL,                -- e.g. 'pkg.mod.Class.method'; not unique
    line_start     INTEGER NOT NULL CHECK (line_start >= 1),
    line_end       INTEGER NOT NULL,
    parent_id      INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    complexity     INTEGER CHECK (complexity IS NULL OR complexity >= 1),
    CHECK (line_end >= line_start)
);
CREATE INDEX idx_symbols_file_id        ON symbols(file_id);
CREATE INDEX idx_symbols_parent_id      ON symbols(parent_id);
CREATE INDEX idx_symbols_name           ON symbols(name);
CREATE INDEX idx_symbols_qualified_name ON symbols(qualified_name);

-- An edge is one piece of evidence that src refers to dst.
-- Invariant: an edge has a destination symbol if and only if it is not 'unresolved'.
-- Edges disappear automatically when either end symbol (or the file) is deleted, so the
-- incremental indexer must re-index files that depended on a changed file.
CREATE TABLE edges (
    id            INTEGER PRIMARY KEY,
    src_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    dst_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    dst_name      TEXT    NOT NULL,                 -- the name as written in the source
    kind          TEXT    NOT NULL
                  CHECK (kind IN ('imports', 'calls', 'inherits', 'references')),
    confidence    TEXT    NOT NULL
                  CHECK (confidence IN ('resolved', 'inferred', 'heuristic', 'unresolved')),
    how           TEXT    NOT NULL,                 -- how the edge was derived (evidence)
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    line          INTEGER NOT NULL CHECK (line >= 1),
    CHECK ((confidence = 'unresolved') = (dst_symbol_id IS NULL))
);
CREATE INDEX idx_edges_src_symbol_id ON edges(src_symbol_id);
CREATE INDEX idx_edges_dst_symbol_id ON edges(dst_symbol_id);
CREATE INDEX idx_edges_file_id       ON edges(file_id);
CREATE INDEX idx_edges_kind          ON edges(kind);

CREATE TABLE findings (
    id            INTEGER PRIMARY KEY,
    type          TEXT NOT NULL,                    -- 'cycle', 'complexity', ...
    title         TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high')),
    confidence    TEXT NOT NULL
                  CHECK (confidence IN ('resolved', 'inferred', 'heuristic', 'unresolved')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_findings_type ON findings(type);
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema: files, symbols, edges, findings", _V1_INITIAL_SCHEMA),
)

SCHEMA_VERSION: int = MIGRATIONS[-1].version
