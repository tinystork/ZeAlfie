"""Tests for M1-2D.2 Source Acquisition & Staging.

Tests cover:

- Successful extraction of single-root archive for exact SHA
- Acquisition called with exact SHA, never mutable ref
- Empty archive -> ArchiveError
- Corrupt archive -> ArchiveError
- Path traversal ../evil -> ArchiveSecurityError
- Absolute path -> ArchiveSecurityError
- Cross-platform absolute paths (Windows drive/UNC) rejected on POSIX
- Windows drive-qualified relative paths (C:foo) rejected
- Symlink entry rejection -> ArchiveSecurityError
- Stale reuse rejection (deterministic unique staging dirs)
- Deterministic staged source path/result shape
- No real network in tests (mock fetcher only)
- Context manager cleanup
- Stage root guard (must exist)
- ResolvedSource provenance is preserved
- Malicious double-dot variants blocked
- Multiple-root unwinding (3+ top-level dirs left as-is)
- Archive with hidden dotfiles + single root NO LONGER normalized (tightened)
- Archive with top-level file + wrapper dir NOT normalized
- Archive with hidden dir + wrapper dir NOT normalized
- Build helper delegates to building.build_wheel (monkeypatch)
- inspect_wheel_from_staged requires output_dir (raises if None)
- _validate_zip_entry unit tests for zip bomb / large entry rejection
- Per-file uncompressed size cap (validation + extraction)
- Total extracted bytes cap across archive
- Streaming bounded extraction (no whole-file read into RAM)
- No partial output survives on extraction rejection
- Duplicate archive entry rejection (D2-JR2)
- File/directory conflict rejection (D2-JR3)
- _is_cross_platform_absolute unit tests
- Duplicate entry normalisation edge cases (slashes, trailing slash)
- File/directory conflict edge cases (both orders)
- Ambiguous ./ path segments rejected before extraction
- Whole archive size cap enforced before extraction
- Single-root normalization refuses top-level overwrite conflicts
"""

from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from zealfie.building import (
    InspectedEntryPoint,
    InspectedWheel,
)
from zealfie.sources import RemoteSource, ResolvedSource
from zealfie.sources.acquisition import (
    AcquisitionError,
    ArchiveError,
    ArchiveFetcher,
    ArchiveSecurityError,
    StagedSource,
    StagingError,
    _MAX_ARCHIVE_SIZE,
    _MAX_EXPANSION_RATIO,
    _MAX_PER_FILE_SIZE,
    _MAX_TOTAL_EXTRACTED_SIZE,
    _is_cross_platform_absolute,
    _normalize_entry_path,
    _validate_zip_entry,
    _validate_no_duplicate_entries,
    _validate_no_path_conflicts,
    _normalize_single_root,
    _portable_collision_key,
    _validate_portable_segment,
    acquire_source,
    build_wheel_from_staged,
    inspect_wheel_from_staged,
)


# ===========================================================================
# Constants
# ===========================================================================

VALID_SHA = "a" * 40


# ===========================================================================
# Helpers -- archive builders (all in-memory, no disk)
# ===========================================================================


def _make_remote(owner="tinystork", repo="ZeSolver", ref="main") -> RemoteSource:
    return RemoteSource(owner=owner, repo=repo, ref=ref)


def _make_resolved(
    owner="tinystork", repo="ZeSolver", ref="main", sha=VALID_SHA,
) -> ResolvedSource:
    return ResolvedSource(
        source=_make_remote(owner, repo, ref), commit_sha=sha,
    )


def _make_single_root_zip_bytes(
    wrapper_name: str = "tinystork-ZeSolver-abc123",
    files: dict[str, str] | None = None,
) -> bytes:
    """Build an in-memory single-root ZIP (GitHub-style)."""
    if files is None:
        files = {
            "pyproject.toml": "[project]\nname = 'ZeSolver'\nversion = '1.0.0'\n",
            "src/zesolver/__init__.py": "__version__ = '1.0.0'\n",
        }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        dir_info = zipfile.ZipInfo(f"{wrapper_name}/")
        dir_info.external_attr = (0o40755 << 16)
        zf.writestr(dir_info, "")
        for relpath, content in files.items():
            info = zipfile.ZipInfo(f"{wrapper_name}/{relpath}")
            zf.writestr(info, content)
    return buf.getvalue()


def _make_flat_zip_bytes(files: dict[str, str] | None = None) -> bytes:
    """Build an in-memory flat ZIP (no wrapper directory)."""
    if files is None:
        files = {"pyproject.toml": "[project]\nname = 'test'\nversion = '1.0.0'\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for relpath, content in files.items():
            zf.writestr(relpath, content)
    return buf.getvalue()


def _make_malicious_zip_bytes(
    entries: list[tuple[str, bytes, int | None]],
) -> bytes:
    """Build a ZIP with arbitrary filenames and external_attr values.

    Each tuple is ``(filename, content, external_attr_high16)``.
    If *external_attr_high16* is None, it defaults to 0o100644.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content, ext_attr in entries:
            info = zipfile.ZipInfo(filename)
            if ext_attr is not None:
                info.external_attr = ext_attr << 16
            zf.writestr(info, content)
    return buf.getvalue()


def _make_empty_zip_bytes() -> bytes:
    """Build a valid but empty ZIP archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        pass
    return buf.getvalue()


def _make_custom_zip_bytes(
    entries: list[tuple[str, bytes | None]],
) -> bytes:
    """Build a ZIP with arbitrary entries (file or directory).

    Each tuple is ``(filename, content_bytes_or_None_for_dir)``.
    If content is None, the entry is written as a directory marker.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in entries:
            if content is None:
                # Directory entry
                info = zipfile.ZipInfo(filename if filename.endswith("/") else filename + "/")
                info.external_attr = 0o40755 << 16
                zf.writestr(info, "")
            else:
                info = zipfile.ZipInfo(filename)
                zf.writestr(info, content)
    return buf.getvalue()


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def stage_root(tmp_path: Path) -> Path:
    """A clean staging root directory."""
    d = tmp_path / "staging"
    d.mkdir()
    return d


@pytest.fixture
def mock_fetcher():
    """Return a fetcher that returns single-root ZIP bytes for any call."""

    class _Fetcher:
        def __init__(self):
            self.calls: list[tuple[str, str, str]] = []
            self._bytes = _make_single_root_zip_bytes()

        def __call__(self, owner: str, repo: str, commit_sha: str) -> bytes:
            self.calls.append((owner, repo, commit_sha))
            return self._bytes

        def set_bytes(self, data: bytes) -> None:
            self._bytes = data

    return _Fetcher()


# ===========================================================================
# 1) Successful extraction of single-root archive
# ===========================================================================


def test_successful_acquisition_single_root(stage_root, mock_fetcher):
    """Acquire a single-root archive and get normalized content."""
    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)

    assert len(mock_fetcher.calls) == 1
    assert mock_fetcher.calls[0] == ("tinystork", "ZeSolver", VALID_SHA)

    assert staged.stage_dir.parent == stage_root
    assert staged.resolved == resolved

    entries = list(staged.stage_dir.iterdir())
    names = {e.name for e in entries}
    assert "pyproject.toml" in names
    assert "src" in names
    assert "tinystork-ZeSolver-abc123" not in names

    pyproject = (staged.stage_dir / "pyproject.toml").read_text()
    assert "ZeSolver" in pyproject


def test_successful_acquisition_flat_archive(stage_root, mock_fetcher):
    """Acquire a flat (already normalized) archive."""
    mock_fetcher.set_bytes(_make_flat_zip_bytes())
    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    assert staged.stage_dir.is_dir()
    assert (staged.stage_dir / "pyproject.toml").read_text() == "[project]\nname = 'test'\nversion = '1.0.0'\n"


# ===========================================================================
# 1b) Multi-root archive left as-is
# ===========================================================================


def test_acquisition_multi_root_left_as_is(stage_root, mock_fetcher):
    """An archive with multiple top-level dirs is not normalized."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("dir_a/"), "")
        zf.writestr("dir_a/a.txt", "a")
        zf.writestr(zipfile.ZipInfo("dir_b/"), "")
        zf.writestr("dir_b/b.txt", "b")
    mock_fetcher.set_bytes(buf.getvalue())

    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    entries = sorted(p.name for p in staged.stage_dir.iterdir())
    assert entries == ["dir_a", "dir_b"]


# ===========================================================================
# 1c) Hidden dotfile with single root — no longer normalized (tightened)
# ===========================================================================


def test_acquisition_hidden_dotfile_with_single_root_not_normalized(
    stage_root, mock_fetcher,
):
    """Hidden dotfile + single visible dir → NOT normalized (tightened rule).

    Under the tightened _normalize_single_root, normalization only
    fires when there is exactly one top-level entry total.  A
    .gitattributes file alongside the wrapper dir counts as a second
    entry, so no flattening happens.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".gitattributes", "export-ignore")
        zf.writestr(zipfile.ZipInfo("repo-abc123/"), "")
        zf.writestr("repo-abc123/pyproject.toml", "[project]\nname='x'\n")
    mock_fetcher.set_bytes(buf.getvalue())

    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    entries = sorted(p.name for p in staged.stage_dir.iterdir())
    # Both .gitattributes and repo-abc123 should stay at top level.
    assert ".gitattributes" in entries
    assert "repo-abc123" in entries
    assert "pyproject.toml" not in entries  # Still inside wrapper


def test_acquisition_top_level_file_plus_wrapper_not_normalized(
    stage_root, mock_fetcher,
):
    """Top-level file + wrapper dir → NOT normalized."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", "# readme")
        zf.writestr(zipfile.ZipInfo("repo-abc123/"), "")
        zf.writestr("repo-abc123/pyproject.toml", "[project]\nname='x'\n")
    mock_fetcher.set_bytes(buf.getvalue())

    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    entries = sorted(p.name for p in staged.stage_dir.iterdir())
    assert "README.md" in entries
    assert "repo-abc123" in entries
    assert "pyproject.toml" not in entries


def test_acquisition_hidden_dir_plus_wrapper_not_normalized(
    stage_root, mock_fetcher,
):
    """Hidden dir (.git) + wrapper dir → NOT normalized."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo(".git/"), "")
        zf.writestr(".git/HEAD", "ref: refs/heads/main")
        zf.writestr(zipfile.ZipInfo("repo-abc123/"), "")
        zf.writestr("repo-abc123/pyproject.toml", "[project]\nname='x'\n")
    mock_fetcher.set_bytes(buf.getvalue())

    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    entries = sorted(p.name for p in staged.stage_dir.iterdir())
    assert ".git" in entries
    assert "repo-abc123" in entries
    assert "pyproject.toml" not in entries


def test_acquisition_single_top_level_file_not_normalized(
    stage_root, mock_fetcher,
):
    """A single top-level file (no dir) should be left as-is."""
    mock_fetcher.set_bytes(_make_flat_zip_bytes({"README.md": "# hello"}))
    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    entries = sorted(p.name for p in staged.stage_dir.iterdir())
    assert entries == ["README.md"]


# ===========================================================================
# 2) Acquisition called with exact SHA, never mutable ref
# ===========================================================================


def test_acquisition_uses_exact_sha_not_ref(stage_root):
    """The fetcher receives commit_sha, never source.ref as the SHA."""
    captured: list[tuple[str, str, str]] = []

    def fetcher(owner, repo, commit_sha):
        captured.append((owner, repo, commit_sha))
        return _make_single_root_zip_bytes()

    resolved = _make_resolved(ref="main", sha=VALID_SHA)
    acquire_source(resolved, fetcher=fetcher, stage_root=stage_root)

    assert len(captured) == 1
    assert captured[0][2] == VALID_SHA
    assert captured[0][2] != "main"
    assert len(captured[0][2]) == 40


def test_acquisition_cannot_be_called_with_ref_as_sha():
    """ResolvedSource construction rejects branch names as commit_sha."""
    with pytest.raises(Exception, match="40-character hex"):
        ResolvedSource(source=_make_remote(), commit_sha="main")


# ===========================================================================
# 3) Empty archive
# ===========================================================================


def test_empty_archive_bytes_raises(stage_root):
    """Zero-byte archive raises ArchiveError."""
    resolved = _make_resolved()
    with pytest.raises(ArchiveError, match="empty"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: b"",
            stage_root=stage_root,
        )


def test_archive_bytes_over_global_size_cap_rejected(stage_root):
    """Whole archive payloads over the global cap are rejected before parsing."""
    resolved = _make_resolved()
    oversized = b"0" * (_MAX_ARCHIVE_SIZE + 1)
    with pytest.raises(ArchiveSecurityError, match="archive too large"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: oversized,
            stage_root=stage_root,
        )


def test_empty_zip_with_no_entries_raises(stage_root):
    """A valid ZIP with zero entries raises ArchiveError."""
    resolved = _make_resolved()
    with pytest.raises(ArchiveError, match="no entries"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: _make_empty_zip_bytes(),
            stage_root=stage_root,
        )


def test_archive_with_only_dir_entries_no_files_raises(stage_root):
    """A ZIP containing only directory entries raises."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("only-dir/"), "")
    resolved = _make_resolved()
    with pytest.raises(ArchiveError, match="no files after extraction"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: buf.getvalue(),
            stage_root=stage_root,
        )


# ===========================================================================
# 4) Corrupt archive
# ===========================================================================


def test_corrupt_archive_raises(stage_root):
    """Random bytes that aren't a valid ZIP raise ArchiveError."""
    resolved = _make_resolved()
    with pytest.raises(ArchiveError, match="corrupt"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: b"not a zip file at all\x00\xff",
            stage_root=stage_root,
        )


def test_truncated_zip_raises(stage_root):
    """A truncated ZIP raises ArchiveError."""
    valid_full = _make_single_root_zip_bytes()
    truncated = valid_full[: len(valid_full) // 3]
    resolved = _make_resolved()
    with pytest.raises(ArchiveError, match="corrupt"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: truncated,
            stage_root=stage_root,
        )


# ===========================================================================
# 5) Path traversal (../evil)
# ===========================================================================


def test_path_traversal_dot_dot_slash_evil(stage_root):
    """Archive with ../evil entry is rejected."""
    malicious = _make_malicious_zip_bytes([("../evil", b"bad\n", 0o100644)])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="path traversal"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_path_traversal_deeply_nested(stage_root):
    """Deeply nested ../ traversal is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("deep/nested/../../../escape", b"evil\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="path traversal"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_path_traversal_backslash_variant(stage_root):
    """Path traversal using backslash separators is rejected."""
    malicious = _make_malicious_zip_bytes([("..\\evil", b"bad\n", 0o100644)])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="path traversal"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_path_traversal_mixed_slashes(stage_root):
    """Path traversal with mixed / and \\ is still detected."""
    malicious = _make_malicious_zip_bytes([
        ("normal/..\\evil", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="path traversal"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_path_traversal_bare_dot_dot(stage_root):
    """Bare '..' as a filename is rejected."""
    malicious = _make_malicious_zip_bytes([("..", b"", 0o100644)])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="path traversal"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_current_directory_segment_rejected(stage_root):
    """Entries containing ./ are rejected to prevent normalized collisions."""
    malicious = _make_custom_zip_bytes([
        ("root/a/./b.txt", b"first"),
        ("root/a/b.txt", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="current-directory"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_bare_current_directory_segment_rejected(stage_root):
    """Bare '.' as an archive entry is rejected as ambiguous."""
    malicious = _make_custom_zip_bytes([(".", b"bad")])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="current-directory"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


# ===========================================================================
# 6) Absolute path
# ===========================================================================


def test_absolute_path_rejected(stage_root):
    """Entry with absolute path like /etc/passwd is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("/etc/passwd", b"root:x:0:0\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


# ===========================================================================
# 6b) Cross-platform absolute paths (D2-JR1)
# ===========================================================================


def test_absolute_path_windows_drive_rejected_on_posix(stage_root):
    """C:\\Windows\\win.ini is rejected even on POSIX (cross-platform policy)."""
    malicious = _make_malicious_zip_bytes([
        ("C:\\Windows\\win.ini", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_absolute_path_windows_drive_forward_slash_rejected(stage_root):
    """C:/Windows/win.ini is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("C:/Windows/win.ini", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_absolute_path_windows_drive_lowercase_rejected(stage_root):
    """d:\\data\\evil.txt is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("d:\\data\\evil.txt", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_absolute_path_windows_backslash_root_rejected(stage_root):
    """\\evil\\file (Windows root of current drive) is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("\\evil\\file", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_absolute_path_unc_double_backslash_rejected(stage_root):
    """\\\\server\\share\\evil UNC path is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("\\\\server\\share\\evil", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_portable_path_colon_in_segment_rejected(stage_root):
    """Colon in any path segment is rejected (portable path hardening)."""
    archive = _make_custom_zip_bytes([
        ("path:with:colons.txt", b"ok\n"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="portable file name"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_relative_path_with_single_letter_file_accepted(stage_root):
    """A file named just 'c' (not C:\\) is accepted."""
    good = _make_malicious_zip_bytes([
        ("c", b"ok\n", 0o100644),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: good, stage_root=stage_root,
    )
    assert (staged.stage_dir / "c").read_text() == "ok\n"


# ===========================================================================
# 6c) Windows drive-qualified relative paths (D.2 hardening fix 4)
# ===========================================================================


def test_windows_drive_relative_path_rejected(stage_root):
    """C:foo (drive-qualified relative) is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("C:foo.txt", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_windows_drive_relative_path_lowercase_rejected(stage_root):
    """d:bar.txt is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("d:bar.txt", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_windows_drive_relative_bare_colon_rejected(stage_root):
    """A: (bare drive letter colon, no path) is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("A:", b"bad\n", 0o100644),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="absolute path"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_is_cross_platform_absolute_windows_drive_relative():
    """_is_cross_platform_absolute detects C:foo as absolute-style."""
    assert _is_cross_platform_absolute("C:foo") is True
    assert _is_cross_platform_absolute("d:bar") is True
    assert _is_cross_platform_absolute("Z:relative/path.txt") is True


def test_is_cross_platform_absolute_posix():
    """_is_cross_platform_absolute detects POSIX absolute paths."""
    assert _is_cross_platform_absolute("/etc/passwd") is True
    assert _is_cross_platform_absolute("/") is True
    assert _is_cross_platform_absolute("/home/user/file") is True


def test_is_cross_platform_absolute_windows_drive_backslash():
    """_is_cross_platform_absolute detects Windows drive-letter backslash paths."""
    assert _is_cross_platform_absolute("C:\\Windows\\System32") is True
    assert _is_cross_platform_absolute("D:\\data\\file.txt") is True
    assert _is_cross_platform_absolute("z:\\stuff") is True


def test_is_cross_platform_absolute_windows_drive_forward_slash():
    """_is_cross_platform_absolute detects Windows drive-letter forward-slash paths."""
    assert _is_cross_platform_absolute("C:/Windows/System32") is True
    assert _is_cross_platform_absolute("D:/data/file.txt") is True


def test_is_cross_platform_absolute_windows_root_backslash():
    """_is_cross_platform_absolute detects \\-rooted paths."""
    assert _is_cross_platform_absolute("\\Windows\\System32") is True
    assert _is_cross_platform_absolute("\\evil") is True


def test_is_cross_platform_absolute_unc():
    """_is_cross_platform_absolute detects UNC paths."""
    assert _is_cross_platform_absolute("\\\\server\\share\\file") is True
    assert _is_cross_platform_absolute("//server/share/file") is True


def test_is_cross_platform_absolute_relative():
    """_is_cross_platform_absolute returns False for relative paths."""
    assert _is_cross_platform_absolute("path/to/file.txt") is False
    assert _is_cross_platform_absolute("src/module.py") is False
    assert _is_cross_platform_absolute(".hidden") is False
    assert _is_cross_platform_absolute("file.txt") is False
    assert _is_cross_platform_absolute("path:with:colons") is False
    assert _is_cross_platform_absolute("c") is False  # single letter, not drive


# ===========================================================================
# 7) Symlink entry rejection
# ===========================================================================


def test_symlink_rejected_via_external_attr(stage_root):
    """A ZIP entry with symlink file-type bits in external_attr is rejected."""
    malicious = _make_malicious_zip_bytes([
        ("harmless_link", b"/etc/passwd", 0o120777),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="symlink"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_symlink_inside_nested_dir_rejected(stage_root):
    """Symlink inside a nested directory is also rejected."""
    malicious = _make_malicious_zip_bytes([
        ("project/__init__.py", b"pass\n", 0o100644),
        ("project/link_to_etc", b"/etc/passwd", 0o120777),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="symlink"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: malicious,
            stage_root=stage_root,
        )


def test_normal_file_with_rwx_permission_accepted(stage_root):
    """A regular file with rwx permission (not symlink) is accepted."""
    data = _make_malicious_zip_bytes([("script.sh", b"#!/bin/sh\necho ok\n", 0o100755)])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: data, stage_root=stage_root,
    )
    assert staged.stage_dir.is_dir()


# ===========================================================================
# 7b) Directory entry with flat archive (no single-root unwinding)
# ===========================================================================


def test_directory_entry_with_flat_archive_extracts_correctly(stage_root):
    """A flat archive with a directory and a file extracts to the expected path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Multiple dirs so normalization doesn't apply.
        zf.writestr(zipfile.ZipInfo("other_dir/"), "")
        zf.writestr("other_dir/x.txt", "x")
        zf.writestr(zipfile.ZipInfo("mydir/"), "")
        zf.writestr("mydir/file.txt", "hello\n")

    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: buf.getvalue(), stage_root=stage_root,
    )
    # Two top-level dirs -> no normalization, paths preserved.
    assert (staged.stage_dir / "mydir" / "file.txt").read_text() == "hello\n"


# ===========================================================================
# 7c) _validate_zip_entry unit tests (zip bomb / large entry)
# ===========================================================================


def test_validate_zip_entry_rejects_huge_compressed():
    """_validate_zip_entry rejects entry with compress_size > max."""
    info = zipfile.ZipInfo("huge.bin")
    info.compress_size = _MAX_ARCHIVE_SIZE + 1
    info.file_size = 1
    info.external_attr = 0o100644 << 16
    with pytest.raises(ArchiveSecurityError, match="too large"):
        _validate_zip_entry(info)


def test_validate_zip_entry_rejects_expansion_ratio():
    """_validate_zip_entry rejects suspicious expansion ratio."""
    info = zipfile.ZipInfo("bomb.txt")
    info.compress_size = 1
    info.file_size = (_MAX_EXPANSION_RATIO + 1)
    info.external_attr = 0o100644 << 16
    with pytest.raises(ArchiveSecurityError, match="expansion ratio"):
        _validate_zip_entry(info)


def test_validate_zip_entry_accepts_normal_ratio():
    """Entries with normal expansion ratio pass validation."""
    info = zipfile.ZipInfo("normal.txt")
    info.compress_size = 100
    info.file_size = 200
    info.external_attr = 0o100644 << 16
    _validate_zip_entry(info)  # Should not raise.


def test_validate_zip_entry_skips_zero_compress():
    """compress_size=0 skips expansion ratio check (STORED entries)."""
    info = zipfile.ZipInfo("stored.txt")
    info.compress_size = 0
    info.file_size = 1000000
    info.external_attr = 0o100644 << 16
    _validate_zip_entry(info)  # Should not raise.


# ===========================================================================
# 7d) Per-file uncompressed size cap (D.2 hardening fix 1a)
# ===========================================================================


def test_validate_zip_entry_rejects_per_file_size_cap():
    """_validate_zip_entry rejects entry exceeding _MAX_PER_FILE_SIZE."""
    info = zipfile.ZipInfo("huge_source.py")
    info.compress_size = 100
    info.file_size = _MAX_PER_FILE_SIZE + 1
    info.external_attr = 0o100644 << 16
    with pytest.raises(ArchiveSecurityError, match="per-file size cap"):
        _validate_zip_entry(info)


def test_acquisition_rejects_entry_over_per_file_cap_before_extraction(stage_root, monkeypatch):
    """An archive with a file > _MAX_PER_FILE_SIZE is rejected in Phase 1."""
    # Monkeypatch _MAX_PER_FILE_SIZE to a small value so we can create a
    # ZIP with real (not faked) file_size that exceeds the cap.
    # zf.writestr() overrides file_size with the actual data size, so we
    # can't just set info.file_size in the ZipInfo.
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_PER_FILE_SIZE", 50,
    )
    # Build a ZIP with one normal file and one oversized file.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("small.txt", b"hello")
        zf.writestr("huge.bin", b"x" * 100)
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="per-file size cap"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: buf.getvalue(),
            stage_root=stage_root,
        )
    # Staging directory must be cleaned up (no partial output).
    stage_dirs = list(stage_root.glob("zealfie-stage-*"))
    assert len(stage_dirs) == 0


def test_per_file_cap_exactly_at_limit_accepted(stage_root, monkeypatch):
    """A file exactly at _MAX_PER_FILE_SIZE is accepted (boundary uses < not ≤).

    Patches _MAX_PER_FILE_SIZE to 100 bytes and creates a STORED entry
    with exactly 100 bytes.  If the production check used ``>=``
    instead of ``>``, this entry would be wrongly rejected.
    """
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_PER_FILE_SIZE", 100,
    )
    # STORED compression: file_size == actual data size == 100 bytes.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("exact.bin", b"x" * 100)
    resolved = _make_resolved()
    # Should not raise (within cap, not over).
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: buf.getvalue(),
        stage_root=stage_root,
    )
    assert staged.stage_dir.is_dir()
    staged.cleanup()


# ===========================================================================
# 7e) Total extracted bytes cap (D.2 hardening fix 1b)
# ===========================================================================


def test_total_extracted_bytes_cap_enforced(stage_root, monkeypatch):
    """Extraction stops when total uncompressed exceeds cap."""
    # Monkeypatch _MAX_TOTAL_EXTRACTED_SIZE to a small value so the test
    # is practical.  Also monkeypatch _MAX_PER_FILE_SIZE high enough so
    # individual files pass the per-file check.
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_TOTAL_EXTRACTED_SIZE", 500,
    )
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_PER_FILE_SIZE", 1024 * 1024,
    )
    # Create files with STORED compression to avoid expansion-ratio issues.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i in range(10):
            zf.writestr(f"file_{i:04d}.txt", b"x" * 100)
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="declared total extracted"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: buf.getvalue(),
            stage_root=stage_root,
        )
    # Staging directory must be cleaned up (no partial output).
    stage_dirs = list(stage_root.glob("zealfie-stage-*"))
    assert len(stage_dirs) == 0


def test_total_extracted_bytes_below_cap_accepted(stage_root):
    """Archive under the total cap is extracted successfully."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", "hello")
        zf.writestr("b.txt", "world")
    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: buf.getvalue(),
        stage_root=stage_root,
    )
    assert (staged.stage_dir / "a.txt").read_text() == "hello"
    assert (staged.stage_dir / "b.txt").read_text() == "world"
    staged.cleanup()


def test_total_extracted_bytes_only_counts_files_not_dirs(stage_root, monkeypatch):
    """Directory entries are not counted toward total extracted bytes."""
    # Patch _MAX_TOTAL_EXTRACTED_SIZE to a tiny value so we can verify
    # directory markers don't contribute.
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_TOTAL_EXTRACTED_SIZE", 100,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("src/"), "")
        zf.writestr("src/module.py", "pass\n")
        zf.writestr(zipfile.ZipInfo("tests/"), "")
        zf.writestr("tests/test_x.py", "pass\n")
    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: buf.getvalue(),
        stage_root=stage_root,
    )
    assert staged.stage_dir.is_dir()
    staged.cleanup()


# ===========================================================================
# 7f) Streaming bounded extraction (D.2 hardening fix 1c)
# ===========================================================================


def test_cleanup_on_per_file_size_validation_rejection(stage_root, monkeypatch):
    """Staging directory is cleaned up when Phase 1 validation rejects a file.

    _validate_zip_entry catches a file exceeding _MAX_PER_FILE_SIZE
    during the pre-extraction pass.  The fresh staging directory must
    be removed with no partial content after the rejection.
    """
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_PER_FILE_SIZE", 50,
    )
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_TOTAL_EXTRACTED_SIZE",
        1024 * 1024 * 1024,  # effectively unlimited
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("oversized.bin", b"x" * 100)
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="per-file size cap"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: buf.getvalue(),
            stage_root=stage_root,
        )
    stage_dirs = list(stage_root.glob("zealfie-stage-*"))
    assert len(stage_dirs) == 0, f"leaked: {stage_dirs}"


def test_streaming_extraction_no_whole_file_in_ram(monkeypatch):
    """Extraction reads are chunked and bounded, never whole-file.

    Spies on ZipExtFile.read to assert every extraction read during
    acquire_source passes an explicit size ≤ io.DEFAULT_BUFFER_SIZE
    (patched to 128).  A file larger than the buffer requires multiple
    reads, proving streaming extraction.
    """
    monkeypatch.setattr(io, "DEFAULT_BUFFER_SIZE", 128)

    buf = io.BytesIO()
    content = b"x" * 500  # Bigger than buffer, needs ≥ 3 reads
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.bin", content)
    archive_bytes = buf.getvalue()

    # Spy on ZipExtFile.read to record the size argument of each call.
    _orig_read = zipfile.ZipExtFile.read
    read_sizes: list[int | None] = []

    def _spy_read(self, size: int = -1) -> bytes:
        read_sizes.append(size if size >= 0 else None)
        return _orig_read(self, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", _spy_read)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        stage_root = Path(tmp)
        resolved = _make_resolved()
        staged = acquire_source(
            resolved,
            fetcher=lambda o, r, s: archive_bytes,
            stage_root=stage_root,
        )
        assert (staged.stage_dir / "data.bin").read_bytes() == content
        staged.cleanup()

    # All explicit-size reads must be bounded by the buffer.
    bounded_reads = [s for s in read_sizes if s is not None and s > 0]
    assert len(bounded_reads) >= 1, (
        "Expected at least one bounded-size read during extraction"
    )
    for sz in bounded_reads:
        assert sz <= 128, f"Read size {sz} exceeds buffer cap 128"

    # No whole-file read: no call loaded all data at once.
    full_reads = [s for s in read_sizes if s is not None and s >= len(content)]
    assert not full_reads, (
        f"Whole-file read detected (sizes {full_reads}, "
        f"content is {len(content)} bytes)"
    )


# ===========================================================================
# 8) Stale reuse rejection (deterministic unique staging)
# ===========================================================================


def test_two_acquisitions_create_different_staging_dirs(stage_root):
    """Two acquisitions produce two distinct staging directories."""
    resolved = _make_resolved()
    staged_1 = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    staged_2 = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    assert staged_1.stage_dir != staged_2.stage_dir
    assert staged_1.stage_dir.is_dir()
    assert staged_2.stage_dir.is_dir()
    staged_1.cleanup()
    staged_2.cleanup()


def test_staging_never_reuses_previous_content(stage_root):
    """Each call gets a fresh directory with no stale content."""
    resolved = _make_resolved()
    staged_1 = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    (staged_1.stage_dir / "marker.txt").write_text("first")

    staged_2 = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    assert not (staged_2.stage_dir / "marker.txt").exists()
    staged_1.cleanup()
    staged_2.cleanup()


# ===========================================================================
# 9) Deterministic path shape
# ===========================================================================


def test_staged_source_path_is_under_stage_root(stage_root):
    """Staging directory is always a child of the provided stage_root."""
    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    assert stage_root in staged.stage_dir.parents or staged.stage_dir.parent == stage_root
    staged.cleanup()


def test_staged_source_path_contains_repo_name(stage_root):
    """The staging directory name includes the repo name for traceability."""
    resolved = _make_resolved(repo="ZeSolver")
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    assert "ZeSolver" in staged.stage_dir.name
    staged.cleanup()


def test_resolved_provenance_preserved_identity(stage_root):
    """The StagedSource.resolved matches the input exactly (identity)."""
    resolved = _make_resolved(sha=VALID_SHA)
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    assert staged.resolved is resolved
    staged.cleanup()


# ===========================================================================
# 10) No real network (proven by mock fetcher interface)
# ===========================================================================


def test_no_real_network_anywhere(stage_root, mock_fetcher):
    """Every acquisition test uses mock fetcher -- no real network."""
    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    assert len(mock_fetcher.calls) == 1
    assert mock_fetcher.calls[0][2] == VALID_SHA
    staged.cleanup()


# ===========================================================================
# 11) Context manager and cleanup
# ===========================================================================


def test_staged_source_context_manager_cleans_up(stage_root, mock_fetcher):
    """Using StagedSource as context manager cleans up the directory."""
    resolved = _make_resolved()
    stage_dir_path = None
    with acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root) as staged:
        stage_dir_path = staged.stage_dir
        assert stage_dir_path.is_dir()
    assert not stage_dir_path.is_dir()


def test_staged_source_explicit_cleanup(stage_root, mock_fetcher):
    """Explicit cleanup() removes the staging directory."""
    resolved = _make_resolved()
    staged = acquire_source(resolved, fetcher=mock_fetcher, stage_root=stage_root)
    path = staged.stage_dir
    assert path.is_dir()
    staged.cleanup()
    assert not path.is_dir()


def test_staged_source_double_cleanup_is_safe(stage_root, mock_fetcher):
    """Calling cleanup() multiple times does not raise."""
    staged = acquire_source(_make_resolved(), fetcher=mock_fetcher, stage_root=stage_root)
    staged.cleanup()
    staged.cleanup()


def test_cleanup_on_extraction_failure(stage_root):
    """If extraction fails, the staging directory is cleaned up."""
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="path traversal"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: _make_malicious_zip_bytes([
                ("../evil", b"bad", 0o100644),
            ]),
            stage_root=stage_root,
        )
    remaining = list(stage_root.iterdir())
    stage_dirs = [p for p in remaining if p.name.startswith("zealfie-stage-")]
    assert len(stage_dirs) == 0, f"staging directory leaked: {stage_dirs}"


# ===========================================================================
# 12) Stage root guard
# ===========================================================================


def test_stage_root_must_exist(tmp_path):
    """A non-existent stage_root raises StagingError."""
    nonexistent = tmp_path / "does-not-exist"
    resolved = _make_resolved()
    with pytest.raises(StagingError, match="not a directory"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
            stage_root=nonexistent,
        )


def test_stage_root_must_be_directory(tmp_path):
    """A file as stage_root raises StagingError."""
    file_path = tmp_path / "a_file"
    file_path.write_text("not a dir")
    resolved = _make_resolved()
    with pytest.raises(StagingError, match="not a directory"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
            stage_root=file_path,
        )


# ===========================================================================
# 13) Fetcher error propagation
# ===========================================================================


def test_fetcher_acquisition_error_propagates(stage_root):
    """AcquisitionError raised by fetcher propagates to caller."""
    resolved = _make_resolved()

    def bad_fetcher(owner, repo, sha):
        raise AcquisitionError("network unreachable")

    with pytest.raises(AcquisitionError, match="network unreachable"):
        acquire_source(resolved, fetcher=bad_fetcher, stage_root=stage_root)


def test_fetcher_generic_error_wrapped(stage_root):
    """Non-AcquisitionError from fetcher is wrapped in AcquisitionError."""
    resolved = _make_resolved()

    def crashy_fetcher(owner, repo, sha):
        raise OSError(13, "permission denied")

    with pytest.raises(AcquisitionError, match="archive fetch failed"):
        acquire_source(resolved, fetcher=crashy_fetcher, stage_root=stage_root)


# ===========================================================================
# 14) Build helper delegates to building primitives
# ===========================================================================


def test_build_wheel_from_staged_delegates_to_build_wheel(
    stage_root, tmp_path, monkeypatch,
):
    """build_wheel_from_staged calls zealfie.building.build_wheel."""
    import zealfie.building

    captured_args: list[tuple] = []

    def fake_build(source_dir, *, output_dir=None):
        captured_args.append((source_dir, output_dir))
        wheel = tmp_path / "fake_out" / "fake-0.0.1-py3-none-any.whl"
        wheel.parent.mkdir(parents=True, exist_ok=True)
        wheel.write_text("")
        return wheel

    monkeypatch.setattr(zealfie.building, "build_wheel", fake_build)

    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    result = build_wheel_from_staged(staged, output_dir=tmp_path / "out")
    assert len(captured_args) == 1
    assert captured_args[0][0] == staged.stage_dir
    staged.cleanup()


def test_inspect_wheel_from_staged_delegates(
    stage_root, tmp_path, monkeypatch,
):
    """inspect_wheel_from_staged calls build_wheel then inspect_wheel."""
    import zealfie.building

    build_calls: list[Path] = []
    inspect_calls: list[Path] = []

    def fake_build(source_dir, *, output_dir=None):
        build_calls.append(source_dir)
        wheel = (Path(output_dir) if output_dir else tmp_path) / "fake-0.0.1-py3-none-any.whl"
        wheel.parent.mkdir(parents=True, exist_ok=True)
        wheel.write_text("")
        return wheel

    def fake_inspect(wheel_path):
        inspect_calls.append(Path(wheel_path))
        return InspectedWheel(
            wheel_path=Path(wheel_path),
            top_level_packages=("fake_pkg",),
            dist_info_dir="fake_pkg-0.0.1.dist-info",
            distribution_name="fake-pkg",
            version="0.0.1",
            entry_points=(),
        )

    monkeypatch.setattr(zealfie.building, "build_wheel", fake_build)
    monkeypatch.setattr(zealfie.building, "inspect_wheel", fake_inspect)

    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )
    result = inspect_wheel_from_staged(staged, output_dir=tmp_path / "inspect_out")

    assert result.distribution_name == "fake-pkg"
    assert result.version == "0.0.1"
    assert len(build_calls) == 1
    assert build_calls[0] == staged.stage_dir
    assert len(inspect_calls) == 1
    staged.cleanup()


# ===========================================================================
# 14b) inspect_wheel_from_staged requires output_dir (D.2 hardening fix 3)
# ===========================================================================


def test_inspect_wheel_from_staged_raises_without_output_dir(
    stage_root, monkeypatch,
):
    """inspect_wheel_from_staged raises AcquisitionError when output_dir is None."""
    import zealfie.building

    def fake_build(source_dir, *, output_dir=None):
        # Should never be called because we raise before calling build_wheel.
        pytest.fail("build_wheel should not be called")
        return Path("/dev/null")

    monkeypatch.setattr(zealfie.building, "build_wheel", fake_build)

    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: _make_single_root_zip_bytes(),
        stage_root=stage_root,
    )

    with pytest.raises(AcquisitionError, match="requires output_dir"):
        inspect_wheel_from_staged(staged)

    staged.cleanup()


# ===========================================================================
# 15) Fetcher receives correct args for different products
# ===========================================================================


def test_fetcher_receives_correct_owner_repo_sha(stage_root):
    """Fetcher receives the exact (owner, repo, sha) from resolved source."""
    captured: list[tuple[str, str, str]] = []

    def fetcher(owner, repo, commit_sha):
        captured.append((owner, repo, commit_sha))
        return _make_single_root_zip_bytes(
            wrapper_name=f"{owner}-{repo}-abc123",
        )

    resolved = _make_resolved(owner="myorg", repo="MyProduct", ref="stable", sha="b" * 40)
    staged = acquire_source(resolved, fetcher=fetcher, stage_root=stage_root)
    assert captured == [("myorg", "MyProduct", "b" * 40)]
    staged.cleanup()


# ===========================================================================
# 16) _validate_zip_entry unit tests
# ===========================================================================


def test_validate_zip_entry_normal_file_passes():
    """A normal file entry passes validation."""
    info = zipfile.ZipInfo("src/module.py")
    info.external_attr = 0o100644 << 16
    _validate_zip_entry(info)


def test_validate_zip_entry_normal_dir_passes():
    """A directory entry passes validation."""
    info = zipfile.ZipInfo("src/")
    info.external_attr = 0o040755 << 16
    _validate_zip_entry(info)


def test_validate_zip_entry_hidden_file_passes():
    """A dotfile passes validation."""
    info = zipfile.ZipInfo(".gitignore")
    info.external_attr = 0o100644 << 16
    _validate_zip_entry(info)


# ===========================================================================
# 17) _normalize_single_root unit tests
# ===========================================================================


def test_normalize_single_root_moves_content_up(tmp_path):
    """Single wrapper dir -> content moved up, wrapper removed."""
    d = tmp_path / "stage"
    d.mkdir()
    wrapper = d / "repo-abc123"
    wrapper.mkdir()
    (wrapper / "pyproject.toml").write_text("[project]\nname='x'\n")
    (wrapper / "src").mkdir()
    (wrapper / "src" / "__init__.py").write_text("pass\n")

    _normalize_single_root(d)

    assert (d / "pyproject.toml").is_file()
    assert (d / "src").is_dir()
    assert (d / "src" / "__init__.py").is_file()
    assert not wrapper.exists()


def test_normalize_multi_root_no_op(tmp_path):
    """Multiple top-level dirs -> no normalization."""
    d = tmp_path / "stage"
    d.mkdir()
    (d / "dir_a").mkdir()
    (d / "dir_b").mkdir()

    _normalize_single_root(d)

    assert (d / "dir_a").is_dir()
    assert (d / "dir_b").is_dir()


def test_normalize_flat_no_op(tmp_path):
    """Flat content (no dirs) -> no normalization."""
    d = tmp_path / "stage"
    d.mkdir()
    (d / "pyproject.toml").write_text("[project]\nname='x'\n")

    _normalize_single_root(d)

    assert (d / "pyproject.toml").is_file()


def test_normalize_hidden_dirs_also_block_normalization(tmp_path):
    """Hidden dirs like .git alongside wrapper prevent normalization."""
    d = tmp_path / "stage"
    d.mkdir()
    (d / ".git").mkdir()
    wrapper = d / "repo-abc123"
    wrapper.mkdir()
    (wrapper / "setup.py").write_text("pass\n")

    _normalize_single_root(d)

    # Under tightened rules, .git + repo-abc123 = two entries → no flatten.
    assert (d / ".git").is_dir()
    assert wrapper.is_dir()
    assert (wrapper / "setup.py").is_file()


def test_normalize_single_root_rejects_self_collision(tmp_path):
    """Normalize rejects when a wrapper child collides with the wrapper itself.

    The overwrite guard in _normalize_single_root is defense-in-depth
    that catches a pathological case: a single wrapper directory
    containing a child entry whose name matches the wrapper's own
    name.  The rename would collide with the still-existing wrapper
    directory.
    """
    d = tmp_path / "stage"
    d.mkdir()
    wrapper = d / "repo-abc123"
    wrapper.mkdir()
    # Create a child inside the wrapper with the wrapper's own name.
    (wrapper / "repo-abc123").mkdir()

    with pytest.raises(ArchiveSecurityError, match="would overwrite"):
        _normalize_single_root(d)


def test_normalize_single_file_top_level_no_op(tmp_path):
    """A single top-level file (not dir) should not normalize."""
    d = tmp_path / "stage"
    d.mkdir()
    (d / "README.md").write_text("hello")
    _normalize_single_root(d)
    assert (d / "README.md").is_file()


# ===========================================================================
# 18) Duplicate archive entry rejection (D2-JR2)
# ===========================================================================


def test_duplicate_file_entry_rejected(stage_root):
    """Two entries with the same path are rejected."""
    archive = _make_custom_zip_bytes([
        ("a.txt", b"first"),
        ("a.txt", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root)


def test_duplicate_entry_with_trailing_slash_rejected(stage_root):
    """'dir' and 'dir/' (file vs dir) normalise to same path and are rejected."""
    archive = _make_custom_zip_bytes([
        ("dir", b"file content"),     # file
        ("dir/", None),                # directory marker
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root)


def test_duplicate_entry_with_backslash_normalisation_rejected(stage_root):
    """'a\\b.txt' and 'a/b.txt' are the same after normalisation."""
    archive = _make_custom_zip_bytes([
        ("a\\b.txt", b"first"),
        ("a/b.txt", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_duplicate_entry_three_identical_rejected(stage_root):
    """Three identical entries are rejected on the first duplicate."""
    archive = _make_custom_zip_bytes([
        ("x.py", b"v1"),
        ("x.py", b"v2"),
        ("x.py", b"v3"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root)


def test_no_duplicate_when_paths_are_distinct(stage_root):
    """Archives with distinct paths are accepted."""
    archive = _make_custom_zip_bytes([
        ("keep_a/", None),
        ("a.txt", b"a"),
        ("b.txt", b"b"),
        ("sub/c.txt", b"c"),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
    )
    assert (staged.stage_dir / "a.txt").is_file()
    assert (staged.stage_dir / "b.txt").is_file()
    assert (staged.stage_dir / "sub" / "c.txt").read_text() == "c"


def test_validate_no_duplicate_entries_unit_normal():
    """_validate_no_duplicate_entries passes on unique paths."""
    infos = [
        zipfile.ZipInfo("a.txt"),
        zipfile.ZipInfo("b.txt"),
        zipfile.ZipInfo("sub/"),
    ]
    _validate_no_duplicate_entries(infos)  # Should not raise.


def test_validate_no_duplicate_entries_unit_rejects():
    """_validate_no_duplicate_entries rejects duplicates."""
    infos = [
        zipfile.ZipInfo("a.txt"),
        zipfile.ZipInfo("a.txt"),
    ]
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        _validate_no_duplicate_entries(infos)


def test_validate_no_duplicate_entries_unit_trailing_slash():
    """Trailing-slash / no-trailing-slash normalises to same path."""
    infos = [
        zipfile.ZipInfo("dir"),       # treated as file (no trailing slash)
        zipfile.ZipInfo("dir/"),      # treated as directory marker
    ]
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        _validate_no_duplicate_entries(infos)


def test_validate_no_duplicate_entries_unit_backslash_normalised():
    """Backslash paths normalise to forward-slash and collide."""
    infos = [
        zipfile.ZipInfo("sub\\file.txt"),
        zipfile.ZipInfo("sub/file.txt"),
    ]
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        _validate_no_duplicate_entries(infos)


# ===========================================================================
# 19) File/directory conflict rejection (D2-JR3)
# ===========================================================================


def test_file_and_dir_same_path_conflict(stage_root):
    """'dir' as file + 'dir/' as directory -> conflict."""
    archive = _make_malicious_zip_bytes([
        ("dir", b"file content\n", 0o100644),
    ])
    # Append dir/ entry manually since _make_malicious_zip_bytes doesn't
    # handle directory entries well. Build a custom archive.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("dir")
        info.external_attr = 0o100644 << 16
        zf.writestr(info, b"file content\n")
        dir_info = zipfile.ZipInfo("dir/")
        dir_info.external_attr = 0o40755 << 16
        zf.writestr(dir_info, b"")

    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: buf.getvalue(), stage_root=stage_root,
        )


def test_file_is_dir_prefix_conflict_file_first(stage_root):
    """'dir' as file + 'dir/child' -> conflict ('dir' can't be both)."""
    archive = _make_custom_zip_bytes([
        ("dir", b"file content"),
        ("dir/child", b"child content"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_file_is_dir_prefix_conflict_dir_child_first(stage_root):
    """'dir/child' then 'dir' as file -> still a conflict (order-insensitive)."""
    archive = _make_custom_zip_bytes([
        ("dir/child", b"child content"),
        ("dir", b"file content"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_nested_file_prefix_conflict(stage_root):
    """'a/b' as file + 'a/b/c/d' as file -> conflict at 'a/b'."""
    archive = _make_custom_zip_bytes([
        ("a/b", b"file at a/b"),
        ("a/b/c/d", b"deep file"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_dir_and_file_under_it_is_fine(stage_root):
    """'dir/' directory + 'dir/child' file -> accepted (no conflict)."""
    archive = _make_custom_zip_bytes([
        ("dir/", None),
        ("dir/child", b"child content"),
        ("keep_b/", None),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
    )
    assert (staged.stage_dir / "dir" / "child").read_text() == "child content"


def test_file_and_dir_under_dir_is_fine(stage_root):
    """File at root + directory under it -> no conflict (different path)."""
    archive = _make_custom_zip_bytes([
        ("setup.py", b"# setup"),
        ("src/", None),
        ("src/__init__.py", b""),
        ("keep_c/", None),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
    )
    assert (staged.stage_dir / "setup.py").read_text() == "# setup"
    assert (staged.stage_dir / "src" / "__init__.py").read_text() == ""


def test_validate_no_path_conflicts_unit_form1():
    """Form 1: same path as both file and directory."""
    infos = []
    f = zipfile.ZipInfo("dir")
    f.external_attr = 0o100644 << 16  # regular file
    infos.append(f)
    d = zipfile.ZipInfo("dir/")
    d.external_attr = 0o40755 << 16  # directory
    infos.append(d)

    with pytest.raises(ArchiveSecurityError, match="conflicting"):
        _validate_no_path_conflicts(infos)


def test_validate_no_path_conflicts_unit_form2():
    """Form 2: file path is prefix of another entry."""
    infos = []
    f = zipfile.ZipInfo("dir")
    f.external_attr = 0o100644 << 16
    infos.append(f)
    child = zipfile.ZipInfo("dir/child.txt")
    child.external_attr = 0o100644 << 16
    infos.append(child)

    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        _validate_no_path_conflicts(infos)


def test_validate_no_path_conflicts_unit_all_ok():
    """Normal directory hierarchy passes validation."""
    infos = []
    d = zipfile.ZipInfo("src/")
    d.external_attr = 0o40755 << 16
    infos.append(d)
    f = zipfile.ZipInfo("src/module.py")
    f.external_attr = 0o100644 << 16
    infos.append(f)

    # No conflict expected.
    _validate_no_path_conflicts(infos)


def test_conflict_detection_with_backslash_normalised(stage_root):
    """Backslash entry 'dir\\child' normalized to 'dir/child' still detected."""
    archive = _make_custom_zip_bytes([
        ("dir", b"file"),
        ("dir\\child", b"child"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


# ===========================================================================
# 20) Nono regression: backslash-terminated entries (D.2 corrective pass 2)
# ===========================================================================


def test_nono_dot2_backslash_dir_with_forward_dir_duplicate(stage_root):
    """'dir\\' + 'dir/' normalize to the same path and are rejected.

    With the old normalization order (rstrip-then-replace), 'dir\\'
    would become 'dir/' (trailing slash preserved), so the duplicate
    check would miss it and a raw FileExistsError would leak at
    extraction time.
    """
    archive = _make_custom_zip_bytes([
        ("dir\\", None),    # backslash-terminated directory
        ("dir/", None),     # forward-slash directory
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_nono_dot2_backslash_file_with_dir_child_conflict(stage_root):
    """'dir\\' (file form) + 'dir/child' should be rejected as conflict.

    'dir\\' should normalize to 'dir' (a file), and 'dir/child' requires
    'dir' to be a directory — a path conflict. The old normalization
    order would leave 'dir/' after normalization, causing zipfile.is_dir()
    to return False and the conflict check to miss it, leading to a raw
    FileExistsError at extraction.
    """
    archive = _make_custom_zip_bytes([
        ("dir\\", b"pretend file content"),
        ("dir/child", b"child content"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_normalize_entry_path_replaces_backslash_before_stripping():
    """_normalize_entry_path must replace \\ before stripping /.

    This is the core invariant that prevents the Nono backslash bug.
    """
    assert _normalize_entry_path("dir\\") == "dir"
    assert _normalize_entry_path("dir/") == "dir"
    assert _normalize_entry_path("dir") == "dir"
    assert _normalize_entry_path("sub\\dir\\") == "sub/dir"
    assert _normalize_entry_path("sub/dir/") == "sub/dir"
    assert _normalize_entry_path("sub\\dir/file\\") == "sub/dir/file"
    assert _normalize_entry_path("") == ""
    assert _normalize_entry_path("/") == ""
    assert _normalize_entry_path("\\") == ""


# ===========================================================================
# 21) Nono regression: repeated separators bypass (D.2 corrective pass 3)
# ===========================================================================


def test_nono_dot3_double_backslash_file_duplicate_rejected(stage_root):
    """Double-backslash entry a\\b + forward-slash a/b are duplicates.

    Before the _normalize_entry_path fix, a\\b normalized to a//b
    (not collapsing repeated /), so it was treated as distinct from
    a/b, bypassing duplicate detection and allowing silent,
    ambiguous overwrite.
    """
    archive = _make_custom_zip_bytes([
        ("a\\b", b"first"),
        ("a/b", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_nono_dot3_double_backslash_file_with_child_conflict(stage_root):
    """Double-backslash file entry a\\b + child a/b/c -> conflict.

    Before the fix, a\\b normalized to a//b and the conflict
    check (prefix match) missed it, causing a raw FileExistsError
    at extraction time.
    """
    archive = _make_custom_zip_bytes([
        ("a\\b", b"file content"),
        ("a/b/c", b"child content"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*to be a directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_nono_dot3_backslash_trailing_file_with_same_dir_conflict(stage_root):
    """Trailing-backslash file entry a\\b\\ + forward-dir a/b -> conflict.

    Both normalize to a/b (after collapsing repeated separators), so
    duplicate detection fires first.  Either way it fails closed with
    ArchiveSecurityError — no raw IsADirectoryError escapes.
    """

    archive = _make_custom_zip_bytes([
        ("a\\b\\", b"file content"),
        ("a/b", None),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_normalize_entry_path_collapses_repeated_separators():
    """_normalize_entry_path collapses repeated forward slashes.

    All of these variants must normalize to the same canonical a/b.
    """
    assert _normalize_entry_path("a\\b") == "a/b"
    assert _normalize_entry_path("a//b") == "a/b"
    assert _normalize_entry_path("a\\/b") == "a/b"
    assert _normalize_entry_path("a///b") == "a/b"


def test_normalize_entry_path_multiple_repeated_groups():
    """_normalize_entry_path collapses multiple groups of repeated slashes."""
    assert _normalize_entry_path("a//b//c") == "a/b/c"
    assert _normalize_entry_path("a\\b////////c") == "a/b/c"
    assert _normalize_entry_path("a\\/b//c") == "a/b/c"


def test_normalize_entry_path_empty_and_root_still_safe():
    """Edge cases for empty and root-like names after repeated-slash collapse."""
    assert _normalize_entry_path("") == ""
    assert _normalize_entry_path("/") == ""
    assert _normalize_entry_path("///") == ""
    assert _normalize_entry_path("\\") == ""
    assert _normalize_entry_path("\\\\") == ""

# ===========================================================================
# 22) Portable archive path validation per segment (M1-2D.2 final)
# ===========================================================================


def test_portable_path_banned_chars_rejected(stage_root):
    """Characters : < > " | ? * in any segment are rejected."""
    banned = [":", "<", ">", '"', "|", "?", "*"]
    for ch in banned:
        archive = _make_custom_zip_bytes([
            (f"file{ch}name.txt", b"content"),
        ])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="portable file name"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_portable_path_banned_in_nested_segment(stage_root):
    """Banned char deep in a nested path is still rejected."""
    archive = _make_custom_zip_bytes([
        ("folder/sub/foo:bar.txt", b"bad"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="portable file name"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_portable_path_control_chars_rejected(stage_root):
    """ASCII control characters (< 0x20 and DEL) are rejected."""
    # Use control characters that zipfile preserves in filenames
    archive = _make_custom_zip_bytes([
        ("bad\x01name.txt", b""),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="control character"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_portable_path_del_rejected(stage_root):
    """DEL character (0x7F) is rejected."""
    archive = _make_custom_zip_bytes([
        ("bad\x7fname.txt", b""),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="control character"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_portable_path_trailing_dot_rejected(stage_root):
    """Segments ending with '.' are rejected."""
    cases = ["name.", "folder/foo.", "a.b."]
    for name in cases:
        archive = _make_custom_zip_bytes([(name, b"content")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="dot or space"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_portable_path_trailing_space_rejected(stage_root):
    """Segments ending with space are rejected."""
    cases = ["name ", "folder/foo ", "a b "]
    for name in cases:
        archive = _make_custom_zip_bytes([(name, b"content")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="dot or space"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_portable_path_valid_segments_accepted(stage_root):
    """Normal portable file names pass validation."""
    archive = _make_custom_zip_bytes([
        ("src/", None),
        ("setup.py", b"# setup"),
        ("src/module.py", b"pass\n"),
        ("src/sub_pkg/__init__.py", b""),
        ("README.md", b"# readme"),
        (".gitignore", b"*.pyc"),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
    )
    assert (staged.stage_dir / "setup.py").is_file()
    assert (staged.stage_dir / "src" / "module.py").is_file()
    staged.cleanup()


def test_validate_portable_segment_bare_colon():
    """_validate_portable_segment rejects ':' in a segment."""
    with pytest.raises(ArchiveSecurityError, match="portable file name"):
        _validate_portable_segment("file:stream")


def test_validate_portable_segment_angle_brackets():
    """_validate_portable_segment rejects < and >."""
    with pytest.raises(ArchiveSecurityError, match="portable file name"):
        _validate_portable_segment("a<b")
    with pytest.raises(ArchiveSecurityError, match="portable file name"):
        _validate_portable_segment("a>b")


def test_validate_portable_segment_pipe():
    """_validate_portable_segment rejects |."""
    with pytest.raises(ArchiveSecurityError, match="portable file name"):
        _validate_portable_segment("a|b")


def test_validate_portable_segment_trailing_dot():
    """_validate_portable_segment rejects trailing dot."""
    with pytest.raises(ArchiveSecurityError, match="dot or space"):
        _validate_portable_segment("name.")


def test_validate_portable_segment_trailing_space():
    """_validate_portable_segment rejects trailing space."""
    with pytest.raises(ArchiveSecurityError, match="dot or space"):
        _validate_portable_segment("name ")


def test_validate_portable_segment_normal_ok():
    """_validate_portable_segment accepts normal segments."""
    _validate_portable_segment("hello")
    _validate_portable_segment("file.txt")
    _validate_portable_segment("my_module.py")


# ===========================================================================
# 23) Windows reserved device names (M1-2D.2 final)
# ===========================================================================


def test_reserved_device_name_con_rejected(stage_root):
    """CON (case-insensitive) is rejected."""
    for name in ["CON", "con", "Con"]:
        archive = _make_custom_zip_bytes([(name, b"bad")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="reserved device name"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_reserved_device_name_prn_aux_nul_rejected(stage_root):
    """PRN, AUX, NUL are all rejected."""
    for name in ["PRN", "AUX", "NUL"]:
        archive = _make_custom_zip_bytes([(name, b"bad")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="reserved device name"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_reserved_device_name_com_series_rejected(stage_root):
    """COM1 through COM9 are rejected."""
    for i in range(1, 10):
        archive = _make_custom_zip_bytes([(f"COM{i}", b"bad")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="reserved device name"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_reserved_device_name_lpt_series_rejected(stage_root):
    """LPT1 through LPT9 are rejected."""
    for i in range(1, 10):
        archive = _make_custom_zip_bytes([(f"LPT{i}", b"bad")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="reserved device name"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_reserved_device_name_with_extension_rejected(stage_root):
    """Reserved stems with file extensions are rejected."""
    cases = ["nul.txt", "COM1.py", "Lpt9.dat", "aux.tar.gz"]
    for name in cases:
        archive = _make_custom_zip_bytes([(name, b"bad")])
        resolved = _make_resolved()
        with pytest.raises(ArchiveSecurityError, match="reserved device name"):
            acquire_source(
                resolved,
                fetcher=lambda o, r, s: archive,
                stage_root=stage_root,
            )


def test_reserved_device_name_in_subdirectory_rejected(stage_root):
    """Reserved name in a subdirectory is rejected."""
    archive = _make_custom_zip_bytes([
        ("folder/CON", b"bad"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="reserved device name"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_com0_com10_not_rejected(stage_root):
    """COM0 and COM10 are NOT reserved and should pass."""
    archive = _make_custom_zip_bytes([
        ("COM0", b"ok"),
        ("COM10", b"ok"),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
    )
    assert (staged.stage_dir / "COM0").read_text() == "ok"
    assert (staged.stage_dir / "COM10").read_text() == "ok"
    staged.cleanup()


def test_validate_portable_segment_reserved_con():
    """_validate_portable_segment rejects CON."""
    with pytest.raises(ArchiveSecurityError, match="reserved device name"):
        _validate_portable_segment("CON")


def test_validate_portable_segment_reserved_nul_casefold():
    """_validate_portable_segment rejects 'nul' (lowercase)."""
    with pytest.raises(ArchiveSecurityError, match="reserved device name"):
        _validate_portable_segment("nul")


# ===========================================================================
# 24) Cross-platform collision detection (M1-2D.2 final)
# ===========================================================================


def test_case_collision_plain_files_rejected(stage_root):
    """Foo.py and foo.py are rejected as duplicate (case-insensitive)."""
    archive = _make_custom_zip_bytes([
        ("Foo.py", b"first"),
        ("foo.py", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_case_collision_nested_rejected(stage_root):
    """folder/Foo.py + folder/foo.py nested case collision rejected."""
    archive = _make_custom_zip_bytes([
        ("folder/Foo.py", b"first"),
        ("folder/foo.py", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_case_collision_parent_segment_rejected(stage_root):
    """Folder/a + folder/a (parent case diff) rejected as duplicate."""
    archive = _make_custom_zip_bytes([
        ("Folder/a", b"first"),
        ("folder/a", b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_case_collision_parent_dir_marker(stage_root):
    """Folder/ and folder/ directory markers collide."""
    archive = _make_custom_zip_bytes([
        ("Folder/", None),
        ("folder/", None),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_case_collision_file_dir_conflict(stage_root):
    """Case-folded file+dir path conflict (Folder as file + folder/a)."""
    archive = _make_custom_zip_bytes([
        ("Folder", b"file content"),
        ("folder/a", b"child"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="requires.*directory"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_case_collision_distinct_paths_accepted(stage_root):
    """Case-insensitive keys for truly distinct paths do not collide."""
    archive = _make_custom_zip_bytes([
        ("Foo.py", b"foo"),
        ("Bar.py", b"bar"),
        ("baz/", None),
    ])
    resolved = _make_resolved()
    staged = acquire_source(
        resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
    )
    assert (staged.stage_dir / "Foo.py").read_text() == "foo"
    assert (staged.stage_dir / "Bar.py").read_text() == "bar"
    staged.cleanup()


def test_unicode_nfc_collision_rejected(stage_root):
    """Composed and decomposed forms of the same character collide.

    Uses LATIN SMALL LETTER E WITH ACUTE: composed U+00E9 vs
    decomposed U+0065 U+0301.  Both NFC-normalize to the same
    codepoint, so they must be detected as duplicates.
    """
    composed = "caf\u00e9.py"
    decomposed = "cafe\u0301.py"
    assert composed != decomposed, "precondition: raw filenames differ"
    archive = _make_custom_zip_bytes([
        (composed, b"first"),
        (decomposed, b"second"),
    ])
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="duplicate entry"):
        acquire_source(
            resolved, fetcher=lambda o, r, s: archive, stage_root=stage_root,
        )


def test_portable_collision_key_casefold():
    """_portable_collision_key produces casefolded keys."""
    assert _portable_collision_key("Foo.py") == "foo.py"
    assert _portable_collision_key("HELLO.TXT") == "hello.txt"
    assert _portable_collision_key("Src/Module.Py") == "src/module.py"


def test_portable_collision_key_nfc():
    """_portable_collision_key NFC-normalizes then casefolds."""
    composed = "caf\u00e9.py"
    decomposed = "cafe\u0301.py"
    assert _portable_collision_key(composed) == _portable_collision_key(decomposed)
    # The key should be the NFC + casefolded form.
    assert _portable_collision_key(composed) == "caf\u00e9.py"


def test_portable_collision_key_empty():
    """_portable_collision_key returns empty string for empty input."""
    assert _portable_collision_key("") == ""


def test_portable_collision_key_dotfile():
    """_portable_collision_key handles dotfiles correctly."""
    assert _portable_collision_key(".gitignore") == ".gitignore"
    # Case-insensitive even for dotfiles
    assert _portable_collision_key(".GitIgnore") == ".gitignore"


# ===========================================================================
# 25) Preflight total extracted size (M1-2D.2 final)
# ===========================================================================


def test_preflight_total_cap_rejects_before_extraction(stage_root, monkeypatch):
    """Preflight sum of announced file sizes rejects before any extraction.

    Patches _MAX_TOTAL_EXTRACTED_SIZE to a small value so that
    announced file sizes (not actual extracted bytes) trip the cap.
    The key property: rejection happens in Phase 1d, not during
    Phase 2 streaming.
    """
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_TOTAL_EXTRACTED_SIZE", 10,
    )
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_PER_FILE_SIZE", 1024 * 1024,
    )
    # Two small files whose announced file_size sums exceed the cap.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"hello")   # file_size=5
        zf.writestr("b.txt", b"world")   # file_size=5, total=10 at limit
        zf.writestr("c.txt", b"!")       # pushes to 11 > 10
    resolved = _make_resolved()
    with pytest.raises(ArchiveSecurityError, match="declared total extracted"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: buf.getvalue(),
            stage_root=stage_root,
        )
    # No staging directory should be left behind.
    stage_dirs = list(stage_root.glob("zealfie-stage-*"))
    assert len(stage_dirs) == 0


def test_preflight_rejection_prevents_any_file_write(stage_root, monkeypatch):
    """Preflight total cap rejection happens before any file is written
    to the staging directory.

    Uses a spy on open() to verify no 'wb' write occurs before
    the ArchiveSecurityError is raised.
    """
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_TOTAL_EXTRACTED_SIZE", 5,
    )
    monkeypatch.setattr(
        "zealfie.sources.acquisition._MAX_PER_FILE_SIZE", 1024 * 1024,
    )

    # Build archive with files whose summed file_size > cap.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"123456")    # file_size=6 > 5
    resolved = _make_resolved()

    # Spy on builtins.open to detect any write-open calls.
    _orig_open = open
    write_opens: list[str] = []

    def _spy_open(file, mode, *args, **kwargs):
        if "w" in mode:
            write_opens.append(str(file))
        return _orig_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _spy_open)

    with pytest.raises(ArchiveSecurityError, match="declared total extracted"):
        acquire_source(
            resolved,
            fetcher=lambda o, r, s: buf.getvalue(),
            stage_root=stage_root,
        )

    # No file should have been opened for writing inside the staging dir.
    stage_dir_prefix = str(stage_root)
    stage_writes = [
        f for f in write_opens
        if f.startswith(stage_dir_prefix)
    ]
    assert len(stage_writes) == 0, (
        f"Files were written before preflight rejection: {stage_writes}"
    )


def test_preflight_total_cap_passes_when_under(stage_root):
    """Preflight total check does not block archives under the cap."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", "hello")
        zf.writestr("b.txt", "world")
    resolved = _make_resolved()
    staged = acquire_source(
        resolved,
        fetcher=lambda o, r, s: buf.getvalue(),
        stage_root=stage_root,
    )
    assert staged.stage_dir.is_dir()
    staged.cleanup()
