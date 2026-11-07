import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from glyphrift.indexer import FileRecord, detect_language, walk_repo

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def write(root: Path, rel: str, content: str | bytes = "x = 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # Always write exact bytes: write_text() would translate "\n" to "\r\n" on Windows.
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def paths(root: Path, **kwargs: object) -> list[str]:
    return [record.path for record in walk_repo(root, **kwargs)]  # type: ignore[arg-type]


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


# --- language detection -------------------------------------------------------------------


def test_detect_language() -> None:
    assert detect_language("pkg/mod.py") == "python"
    assert detect_language("pkg/mod.pyi") == "python"
    assert detect_language("PKG/MOD.PY") == "python"
    assert detect_language("README.md") is None
    assert detect_language("Makefile") is None


# --- basic behaviour (filesystem mode) ----------------------------------------------------


def test_returns_records_with_size_hash_and_language(tmp_path: Path) -> None:
    content = "def f():\n    return 1\n"
    write(tmp_path, "a.py", content)
    write(tmp_path, "notes.txt", "hello")

    records = list(walk_repo(tmp_path, use_git=False))

    by_path = {r.path: r for r in records}
    assert set(by_path) == {"a.py", "notes.txt"}
    a = by_path["a.py"]
    assert isinstance(a, FileRecord)
    assert a.size == len(content.encode())
    assert a.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert a.language == "python"
    assert by_path["notes.txt"].language is None


def test_results_are_sorted_and_use_posix_separators(tmp_path: Path) -> None:
    write(tmp_path, "b.py")
    write(tmp_path, "a/z.py")
    write(tmp_path, "a/b/c.py")

    assert paths(tmp_path, use_git=False) == ["a/b/c.py", "a/z.py", "b.py"]


def test_empty_file_is_recorded(tmp_path: Path) -> None:
    write(tmp_path, "empty.py", "")
    (record,) = walk_repo(tmp_path, use_git=False)
    assert record.size == 0
    assert record.sha256 == hashlib.sha256(b"").hexdigest()


def test_large_file_hash_is_correct_across_chunks(tmp_path: Path) -> None:
    data = os.urandom(3 * 1024 * 1024 + 123)
    write(tmp_path, "big.bin", data)
    (record,) = walk_repo(tmp_path, use_git=False)
    assert record.size == len(data)
    assert record.sha256 == hashlib.sha256(data).hexdigest()


def test_identical_content_gives_identical_hash_and_change_gives_new_hash(tmp_path: Path) -> None:
    write(tmp_path, "one.py", "same")
    write(tmp_path, "two.py", "same")
    first = {r.path: r.sha256 for r in walk_repo(tmp_path, use_git=False)}
    assert first["one.py"] == first["two.py"]

    write(tmp_path, "two.py", "different")
    second = {r.path: r.sha256 for r in walk_repo(tmp_path, use_git=False)}
    assert second["one.py"] == first["one.py"]
    assert second["two.py"] != first["two.py"]


def test_not_a_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        list(walk_repo(tmp_path / "does_not_exist"))
    file_path = write(tmp_path, "f.py")
    with pytest.raises(NotADirectoryError):
        list(walk_repo(file_path))


# --- skipped directories ------------------------------------------------------------------


@pytest.mark.parametrize("use_git", [False, True])
def test_skips_default_directories(tmp_path: Path, use_git: bool) -> None:
    if use_git:
        if shutil.which("git") is None:
            pytest.skip("git is not installed")
        git_init(tmp_path)
    write(tmp_path, "src/app.py")
    write(tmp_path, ".venv/lib/site.py")
    write(tmp_path, "venv/lib/site.py")
    write(tmp_path, "node_modules/pkg/index.js")
    write(tmp_path, "src/__pycache__/app.cpython-312.pyc", b"\x00")
    write(tmp_path, "build/out.py")
    write(tmp_path, "dist/pkg.py")
    write(tmp_path, "mypkg.egg-info/PKG-INFO")
    write(tmp_path, ".glyphrift/index.db", b"\x00")

    assert paths(tmp_path, use_git=use_git) == ["src/app.py"]


def test_skip_dirs_can_be_overridden(tmp_path: Path) -> None:
    write(tmp_path, "build/keep.py")
    write(tmp_path, "src/app.py")
    assert paths(tmp_path, use_git=False, skip_dirs=frozenset()) == ["build/keep.py", "src/app.py"]


def test_skip_applies_to_directory_names_only_not_files(tmp_path: Path) -> None:
    write(tmp_path, "build")  # a *file* called build
    assert paths(tmp_path, use_git=False) == ["build"]


# --- symlinks -----------------------------------------------------------------------------


def test_symlinks_are_not_followed_or_reported(tmp_path: Path) -> None:
    real = write(tmp_path, "real/mod.py")
    try:
        os.symlink(real, tmp_path / "link.py")
        os.symlink(tmp_path / "real", tmp_path / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this system")

    assert paths(tmp_path, use_git=False) == ["real/mod.py"]


# --- git mode -----------------------------------------------------------------------------


@requires_git
def test_git_mode_respects_gitignore(tmp_path: Path) -> None:
    git_init(tmp_path)
    write(tmp_path, ".gitignore", "ignored.py\ngenerated/\n*.log\n")
    write(tmp_path, "keep.py")
    write(tmp_path, "ignored.py")
    write(tmp_path, "generated/gen.py")
    write(tmp_path, "debug.log", "log")
    write(tmp_path, "pkg/inner.py")

    assert paths(tmp_path) == [".gitignore", "keep.py", "pkg/inner.py"]


@requires_git
def test_git_mode_respects_nested_gitignore_and_negation(tmp_path: Path) -> None:
    git_init(tmp_path)
    write(tmp_path, ".gitignore", "*.tmp\n!important.tmp\n")
    write(tmp_path, "sub/.gitignore", "local.py\n")
    write(tmp_path, "a.tmp")
    write(tmp_path, "important.tmp")
    write(tmp_path, "sub/local.py")
    write(tmp_path, "sub/other.py")

    assert paths(tmp_path) == [
        ".gitignore",
        "important.tmp",
        "sub/.gitignore",
        "sub/other.py",
    ]


@requires_git
def test_git_mode_includes_tracked_and_untracked_files(tmp_path: Path) -> None:
    git_init(tmp_path)
    write(tmp_path, "tracked.py")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    write(tmp_path, "untracked.py")

    assert paths(tmp_path) == ["tracked.py", "untracked.py"]


@requires_git
def test_git_mode_skips_tracked_files_deleted_from_disk(tmp_path: Path) -> None:
    git_init(tmp_path)
    gone = write(tmp_path, "gone.py")
    write(tmp_path, "stays.py")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    gone.unlink()

    assert paths(tmp_path) == ["stays.py"]


@requires_git
def test_git_mode_works_when_root_is_a_subdirectory(tmp_path: Path) -> None:
    git_init(tmp_path)
    write(tmp_path, ".gitignore", "ignored.py\n")
    write(tmp_path, "pkg/a.py")
    write(tmp_path, "pkg/ignored.py")
    write(tmp_path, "outside.py")

    assert paths(tmp_path / "pkg") == ["a.py"]


def test_falls_back_to_filesystem_walk_outside_a_git_repo(tmp_path: Path) -> None:
    # tmp_path is not inside a git repository, so git mode must fall back gracefully.
    write(tmp_path, "a.py")
    write(tmp_path, ".venv/x.py")
    assert paths(tmp_path) == ["a.py"]
