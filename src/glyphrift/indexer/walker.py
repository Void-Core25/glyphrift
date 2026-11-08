"""Repository file walker.

Lists the files of a repository with their size, SHA-256 and detected language.

How files are discovered:

* In a git repository we ask git (`git ls-files --cached --others --exclude-standard`), so
  `.gitignore` rules (nested files, negations, global excludes) are applied exactly as git
  applies them. Tracked and untracked-but-not-ignored files are both included.
* Otherwise (not a git repo, git missing, or ``use_git=False``) we walk the directory tree.
  Only ``skip_dirs`` is applied in that mode; `.gitignore` files are NOT interpreted.

In both modes, any file inside a directory named in ``skip_dirs`` (virtualenvs, caches, build
output, ``*.egg-info``...) is skipped, and symbolic links are never followed or reported.
Files that cannot be read are skipped with a logged warning rather than crashing the walk.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".glyphrift",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        "site-packages",
        "dist-packages",
        "build",
        "dist",
    }
)

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
}

_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One file found in a repository."""

    path: str  # POSIX-style path relative to the repository root
    size: int  # size in bytes
    sha256: str  # hex digest of the file contents
    language: str | None  # e.g. "python"; None when not a supported language


def detect_language(path: str) -> str | None:
    """Return the language for a path based on its extension, or None."""
    return LANGUAGE_BY_EXTENSION.get(PurePosixPath(path).suffix.lower())


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, reading it in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _is_skipped_dir_name(name: str, skip_dirs: Iterable[str]) -> bool:
    return name in skip_dirs or name.endswith(".egg-info")


def _in_skipped_dir(rel_path: str, skip_dirs: frozenset[str]) -> bool:
    parts = PurePosixPath(rel_path).parts
    return any(_is_skipped_dir_name(part, skip_dirs) for part in parts[:-1])


def _git_files(root: Path) -> list[str] | None:
    """List files via git, or None if git is unavailable or root is not in a git repo."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [os.fsdecode(raw) for raw in proc.stdout.split(b"\0") if raw]


def _filesystem_files(root: Path, skip_dirs: frozenset[str]) -> list[str]:
    """List files by walking the directory tree (no .gitignore handling)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _is_skipped_dir_name(d, skip_dirs))
        for name in filenames:
            found.append((Path(dirpath) / name).relative_to(root).as_posix())
    return found


def walk_repo(
    root: str | Path,
    *,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
    use_git: bool = True,
) -> Iterator[FileRecord]:
    """Yield a FileRecord for every file in the repository, sorted by path.

    Raises NotADirectoryError if ``root`` is not a directory.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {root_path}")

    relative_paths = _git_files(root_path) if use_git else None
    if relative_paths is None:
        relative_paths = _filesystem_files(root_path, skip_dirs)

    for rel in sorted(set(relative_paths)):
        if _in_skipped_dir(rel, skip_dirs):
            continue
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            logger.warning("Skipping file whose name is not valid UTF-8: %r", rel)
            continue
        full = root_path / rel
        # Skips symlinks, directories (e.g. git submodules) and files deleted from disk.
        if full.is_symlink() or not full.is_file():
            continue
        try:
            size = full.stat().st_size
            digest = hash_file(full)
        except OSError as exc:
            logger.warning("Skipping unreadable file %s: %s", rel, exc)
            continue
        yield FileRecord(path=rel, size=size, sha256=digest, language=detect_language(rel))
