"""Repository indexing: file discovery, parsing and import/call extraction."""

from glyphrift.indexer.calls import CallResolutionStats, resolve_calls
from glyphrift.indexer.extract import (
    ExtractedBase,
    ExtractedCall,
    ExtractedImport,
    ExtractedSymbol,
    ExtractionResult,
    compute_module_name,
    extract_symbols,
)
from glyphrift.indexer.inherit import InheritanceStats, resolve_inheritance
from glyphrift.indexer.pipeline import IndexStats, index_repo
from glyphrift.indexer.resolve import ImportResolutionStats, resolve_imports
from glyphrift.indexer.walker import (
    DEFAULT_SKIP_DIRS,
    FileRecord,
    detect_language,
    hash_file,
    walk_repo,
)

__all__ = [
    "DEFAULT_SKIP_DIRS",
    "CallResolutionStats",
    "InheritanceStats",
    "ExtractedBase",
    "ExtractedCall",
    "ExtractedImport",
    "ExtractedSymbol",
    "ExtractionResult",
    "FileRecord",
    "ImportResolutionStats",
    "IndexStats",
    "compute_module_name",
    "detect_language",
    "extract_symbols",
    "hash_file",
    "index_repo",
    "resolve_calls",
    "resolve_imports",
    "resolve_inheritance",
    "walk_repo",
]
