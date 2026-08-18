"""Acquisition and staging of resolved remote product sources.

Acquire a :class:`~zealfie.sources.ResolvedSource` (exact immutable SHA)
by downloading the source archive then safely extracting it into a
controlled staging directory.

Acquisition is *not* trust.  The staged source is a controlled local
copy — deployment, activation, and runtime mutation happen in later
phases.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import zipfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from zealfie.net import NetworkReasonCode

from zealfie.sources import ResolvedSource


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AcquisitionError(RuntimeError):
    """Base class for acquisition errors.

    Carries an optional machine-readable :attr:`reason_code` (a
    :class:`~zealfie.net.NetworkReasonCode`) and an optional
    :attr:`proxy_hint` diagnostic string.  Both default to ``None`` so
    existing raise sites and callers are unaffected.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: "NetworkReasonCode | None" = None,
        proxy_hint: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.proxy_hint = proxy_hint
        super().__init__(message)


class StagingError(AcquisitionError):
    """Failed to create the staging area for acquired source content."""


class ArchiveError(AcquisitionError):
    """Archive is invalid, corrupt, or structurally malformed."""


class ArchiveSecurityError(ArchiveError):
    """Archive contains unsafe or malicious content."""


# ---------------------------------------------------------------------------
# Fetcher protocol
# ---------------------------------------------------------------------------


class ArchiveFetcher(Protocol):
    """Protocol for fetching a source archive as raw bytes.

    Receives ``(owner, repo, commit_sha)`` and returns the archive
    bytes.  Unit tests inject a mock; real implementations may fetch
    from GitHub, a local cache, or any other byte source.

    The *commit_sha* is always an exact 40-character hex SHA — never
    a branch name, tag, or abbreviated ref.
    """

    def __call__(self, owner: str, repo: str, commit_sha: str) -> bytes:
        """Fetch the source archive for the given commit.

        Args:
            owner: Repository owner (user or org).
            repo: Repository name.
            commit_sha: Exact 40-character hex commit SHA.

        Returns:
            Raw bytes of the source archive (ZIP format).

        Raises:
            AcquisitionError: If the archive cannot be fetched.
        """


# ---------------------------------------------------------------------------
# Staged source
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedSource:
    """A resolved source that has been acquired and extracted to a local
    staging directory.

    The directory contains the extracted source tree.  GitHub-style
    single-root wrapping is unwound when the archive has exactly one
    top-level directory; flat, multi-root, or non-normalizable
    archives are left as-is.

    Use as a context manager for automatic cleanup, or call
    :meth:`cleanup` explicitly.
    """

    resolved: ResolvedSource
    stage_dir: Path

    def __enter__(self) -> StagedSource:
        return self

    def __exit__(self, *args: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Remove the staging directory and all its contents.

        Safe to call multiple times — subsequent calls are no-ops
        after the directory is gone.
        """
        if self.stage_dir.is_dir():
            shutil.rmtree(self.stage_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum compressed archive size (512 MiB).
_MAX_ARCHIVE_SIZE = 512 * 1024 * 1024

# Maximum number of entries in a single archive.
_MAX_ARCHIVE_FILES = 100_000

# Maximum expansion ratio (uncompressed / compressed) per entry.
# Beyond this the entry is treated as a potential zip bomb.
_MAX_EXPANSION_RATIO = 100

# Absolute per-file uncompressed size cap (100 MiB).
_MAX_PER_FILE_SIZE = 100 * 1024 * 1024

# Absolute total uncompressed extracted size cap across all file
# entries in the archive (1 GiB).
_MAX_TOTAL_EXTRACTED_SIZE = 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# Portable archive path validation
# ---------------------------------------------------------------------------

# Characters forbidden in portable cross-platform file names.
_PORTABLE_PATH_BANNED_CHARS: frozenset[str] = frozenset(
    {":", "<", ">", '"', "|", "?", "*"}
)

# Windows reserved device names (case-insensitive).
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


# ---------------------------------------------------------------------------
# Portable path validation
# ---------------------------------------------------------------------------


def _validate_portable_segment(segment: str) -> None:
    """Validate a single path segment for cross-platform portability.

    Rejects segments containing Windows-incompatible characters
    (``:``, ``<``, ``>``, ``"``, ``|``, ``?``, ``*``), ASCII
    control characters (codepoints 0x00–0x1F and 0x7F DEL), segments
    ending with a dot or space, and Windows reserved device names
    (including stems with file extensions, e.g. ``NUL.txt``).

    Raises :class:`ArchiveSecurityError` if the segment is unsafe.
    """
    if not segment:
        return

    for ch in segment:
        if ch in _PORTABLE_PATH_BANNED_CHARS:
            raise ArchiveSecurityError(
                f"archive entry contains character forbidden in "
                f"portable file names: {ch!r} in segment {segment!r}"
            )
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:
            raise ArchiveSecurityError(
                f"archive entry contains control character "
                f"(U+{cp:04X}) in segment {segment!r}"
            )

    # Reject trailing dot or space (Windows cannot create such files).
    if segment.endswith((".", " ")):
        raise ArchiveSecurityError(
            f"archive entry ends with dot or space: {segment!r}"
        )

    # Check for Windows reserved device names (case-insensitive,
    # including stems with file extensions).
    stem = segment.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ArchiveSecurityError(
            f"archive entry uses reserved device name: {segment!r}"
        )


# ---------------------------------------------------------------------------
# Cross-platform absolute-path detection
# ---------------------------------------------------------------------------


# Compiled once at module level.
# Matches any path starting with a Windows drive letter followed by
# colon, covering both rooted (C:\\foo, C:/foo) and drive-relative
# (C:foo) forms.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_cross_platform_absolute(path: str) -> bool:
    """Return True if *path* is absolute on **any** major platform.

    Covers POSIX absolute (``/foo``), Windows drive-letter absolute
    (``C:\\foo``, ``C:/foo``), Windows drive-relative (``C:foo``),
    Windows ``\\``-rooted (``\\foo``), and UNC paths
    (``\\\\server\\share``, ``//server/share``).

    Rejecting Windows-style paths on POSIX is intentional — the
    security policy must hold regardless of host OS.
    """
    # POSIX / Linux / macOS
    if path.startswith("/"):
        return True
    # Windows drive-letter or drive-relative (C:...)
    if _WINDOWS_DRIVE_RE.match(path):
        return True
    # Windows \-rooted or UNC (\\server\share or \foo)
    if path.startswith("\\"):
        return True
    return False


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def acquire_source(
    resolved: ResolvedSource,
    *,
    fetcher: ArchiveFetcher,
    stage_root: Path,
) -> StagedSource:
    """Acquire and stage a resolved source.

    Downloads the source archive via *fetcher* for the exact commit
    SHA in *resolved*, then safely extracts it into a new staging
    directory under *stage_root*.  GitHub-style single-root wrappers
    (``owner-repo-<sha>``) are automatically unwound.

    The staging directory is created fresh each time — stale content
    reuse is impossible by construction.

    Args:
        resolved: A resolved source with an exact immutable commit SHA.
            Must never be constructed from ``source.ref`` alone.
        fetcher: Injectable archive fetcher (mockable for tests).
        stage_root: Base directory under which to create the staging
            directory.  Must exist and be a directory.  Must not be
            the active runtime, ZeAlfie checkout, or user home.

    Returns:
        A :class:`StagedSource` whose ``stage_dir`` is the normalized
        project root.

    Raises:
        StagingError: If *stage_root* does not exist.
        ArchiveError: If the archive is empty, corrupt, or malformed.
        ArchiveSecurityError: If the archive contains unsafe content
            (path traversal, symlinks, absolute paths, zip bombs).
        AcquisitionError: If the fetcher raises.
    """
    # --- Guard: stage_root must exist and be a directory ---
    try:
        stage_root = stage_root.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise StagingError(f"stage_root is not a directory: {stage_root}") from exc
    if not stage_root.is_dir():
        raise StagingError(f"stage_root is not a directory: {stage_root}")

    # --- Fetch the archive bytes ---
    try:
        archive_bytes = fetcher(
            resolved.source.owner,
            resolved.source.repo,
            resolved.commit_sha,
        )
    except AcquisitionError:
        raise
    except Exception as exc:
        raise AcquisitionError(
            f"archive fetch failed for "
            f"{resolved.source.owner}/{resolved.source.repo} "
            f"@{resolved.commit_sha}: {exc}"
        ) from exc

    # --- Validate: archive is not empty ---
    if not archive_bytes:
        raise ArchiveError("archive is empty (zero bytes)")
    if len(archive_bytes) > _MAX_ARCHIVE_SIZE:
        raise ArchiveSecurityError(
            f"archive too large: {len(archive_bytes)} bytes "
            f"(max {_MAX_ARCHIVE_SIZE})"
        )

    # --- Create a fresh staging directory (no stale reuse) ---
    stage_dir = Path(
        tempfile.mkdtemp(
            prefix=f"zealfie-stage-{resolved.source.repo}-",
            dir=str(stage_root),
        )
    ).resolve(strict=True)

    try:
        # --- Extract with full security validation ---
        _extract_archive(archive_bytes, stage_dir)

        # --- Normalize GitHub-style single-root wrapper ---
        _normalize_single_root(stage_dir)
    except Exception:
        # Best-effort cleanup on any extraction/normalization failure.
        if stage_dir.is_dir():
            shutil.rmtree(stage_dir, ignore_errors=True)
        raise

    return StagedSource(resolved=resolved, stage_dir=stage_dir)


# ---------------------------------------------------------------------------
# ZIP extraction with security hardening
# ---------------------------------------------------------------------------


def _normalize_entry_path(filename: str) -> str:
    """Normalize an archive entry path.

    Backslashes are replaced with forward slashes, repeated forward
    slashes are collapsed, then trailing separators are stripped.
    The order matters: replace and collapse must happen before strip
    so that ``a\\\\b`` canonicalizes to ``a/b``, not ``a//b``.
    """
    return re.sub(r"/+", "/", filename.replace("\\", "/")).rstrip("/")


def _portable_collision_key(clean_path: str) -> str:
    """Return a case-folded NFC-normalized key for portable collision
    detection.

    Each path segment is Unicode-normalized to NFC then casefolded,
    producing a platform-agnostic key suitable for duplicate and
    path-conflict detection regardless of host filesystem case
    sensitivity or Unicode normalisation form.

    >>> _portable_collision_key("Foo.py")
    'foo.py'
    >>> _portable_collision_key("Caf\\u00e9/file.py")  # composed
    _portable_collision_key("Cafe\\u0301/file.py")     # decomposed
    True  # both produce the same key
    """
    if not clean_path:
        return ""
    segments = clean_path.split("/")
    key_segments = [
        unicodedata.normalize("NFC", seg).casefold() for seg in segments
    ]
    return "/".join(key_segments)


def _extract_archive(archive_bytes: bytes, destination: Path) -> None:
    """Extract a ZIP archive into *destination* with full security
    validation.

    Rejects:
    * Bad / corrupt / empty archives
    * Archives with no files (zero entries)
    * Absolute paths (cross-platform: POSIX and Windows forms)
    * Windows drive-qualified relative paths (C:foo)
    * ``..`` path traversal (Zip Slip)
    * Entries escaping the extraction root (post-resolution check)
    * Symlinks (Unix ``external_attr`` high bits)
    * Zip bombs (suspicious expansion ratio)
    * Entries exceeding per-file uncompressed size cap
    * Archives exceeding total extracted bytes cap
    * Duplicate archive entries after path normalisation
    * File/directory conflicts (e.g. ``dir`` file + ``dir/child``)

    .. note::

        Direct callers may see partial destination contents on error.
        The public entry point :func:`acquire_source` cleans its staging
        directory on failure, so this is an implementation concern.
    """
    buf = io.BytesIO(archive_bytes)

    try:
        zf = zipfile.ZipFile(buf, "r")
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"corrupt or invalid ZIP archive: {exc}") from exc

    with zf:
        infos = zf.infolist()

        # --- Reject archives with zero entries ---
        if len(infos) == 0:
            raise ArchiveError("archive has no entries")

        # --- Reject archives with too many entries ---
        if len(infos) > _MAX_ARCHIVE_FILES:
            raise ArchiveError(
                f"archive has {len(infos)} entries "
                f"(max {_MAX_ARCHIVE_FILES})"
            )

        # --- Phase 1: validate every entry before extracting any ---
        for info in infos:
            _validate_zip_entry(info)

        # --- Phase 1b: duplicate entry detection ---
        _validate_no_duplicate_entries(infos)

        # --- Phase 1c: file/directory conflict detection ---
        _validate_no_path_conflicts(infos)

        # --- Phase 1d: preflight total extracted size ---
        # Sum announced ZipInfo.file_size for file entries and reject
        # immediately if the total exceeds the cap — before any
        # extraction or writes begin.  Runtime enforcement remains
        # active in Phase 2 as defense-in-depth.
        preflight_total = 0
        for info in infos:
            clean_name = _normalize_entry_path(info.filename)
            if clean_name == "" or info.is_dir():
                continue
            preflight_total += info.file_size
            if preflight_total > _MAX_TOTAL_EXTRACTED_SIZE:
                raise ArchiveSecurityError(
                    f"archive declared total extracted size "
                    f"({preflight_total} bytes) exceeds cap "
                    f"({_MAX_TOTAL_EXTRACTED_SIZE})"
                )

        # --- Phase 2: extract ---
        destination_str = str(destination.resolve())
        file_count = 0
        total_extracted = 0

        for info in infos:
            # Normalize path separators and strip trailing slashes.
            clean_name = _normalize_entry_path(info.filename)

            # Skip empty names after normalization (root-/dir-only markers).
            if clean_name == "":
                continue

            # Compute the resolved target path.
            member_path = (destination / clean_name).resolve()

            # Post-resolution path-traversal check.
            if (
                str(member_path) != destination_str
                and not str(member_path).startswith(destination_str + os.sep)
            ):
                raise ArchiveSecurityError(
                    f"archive entry escapes destination: "
                    f"{info.filename!r} → {member_path}"
                )

            # Directory entries (not counted toward total extracted).
            if info.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue

            # Ensure parent directory exists before writing file.
            member_path.parent.mkdir(parents=True, exist_ok=True)

            # Streaming bounded extraction: never load entire file
            # into RAM, and enforce absolute caps on the fly.
            file_bytes = 0
            with zf.open(info) as src:
                with open(member_path, "wb") as dst:
                    while True:
                        chunk = src.read(io.DEFAULT_BUFFER_SIZE)
                        if not chunk:
                            break
                        file_bytes += len(chunk)
                        if file_bytes > _MAX_PER_FILE_SIZE:
                            raise ArchiveSecurityError(
                                f"archive entry exceeds per-file size "
                                f"cap during extraction: {info.filename!r} "
                                f"({file_bytes} bytes, "
                                f"max {_MAX_PER_FILE_SIZE})"
                            )
                        total_extracted += len(chunk)
                        if total_extracted > _MAX_TOTAL_EXTRACTED_SIZE:
                            raise ArchiveSecurityError(
                                f"archive exceeds total extracted size "
                                f"cap ({total_extracted} bytes, "
                                f"max {_MAX_TOTAL_EXTRACTED_SIZE})"
                            )
                        dst.write(chunk)

            # Apply Unix permission bits from external_attr (low 9 bits
            # of the high 16-bit word), but strip any file-type bits
            # (symlink, etc.) first.
            if info.external_attr != 0:
                unix_mode = (info.external_attr >> 16) & 0o777
                if unix_mode != 0:
                    try:
                        member_path.chmod(unix_mode)
                    except OSError:
                        pass

            file_count += 1

        # --- Post-extraction: at least one file produced ---
        if file_count == 0:
            raise ArchiveError("archive produced no files after extraction")


def _validate_zip_entry(info: zipfile.ZipInfo) -> None:
    """Validate a single ZIP entry for security before any extraction.

    Raises :class:`ArchiveSecurityError` if the entry is unsafe.
    """
    filename = info.filename

    # --- Reject absolute paths (cross-platform) ---
    # os.path.isabs catches POSIX /foo on POSIX and C:\\foo on Windows,
    # but _is_cross_platform_absolute catches Windows forms on POSIX too.
    if _is_cross_platform_absolute(filename):
        raise ArchiveSecurityError(f"archive contains absolute path: {filename!r}")

    # --- Reject .. path traversal (Zip Slip) ---
    parts = filename.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ArchiveSecurityError(
            f"archive contains path traversal: {filename!r}"
        )
    if any(part == "." for part in parts):
        raise ArchiveSecurityError(
            f"archive contains ambiguous current-directory segment: {filename!r}"
        )

    # --- Validate portable path segments ---
    clean_name = _normalize_entry_path(filename)
    if clean_name:
        for segment in clean_name.split("/"):
            _validate_portable_segment(segment)

    # --- Reject symlinks via Unix external_attr ---
    # The high 16 bits of external_attr encode Unix st_mode.
    # S_IFLNK = 0o120000 is the symlink file-type bits.
    unix_type = (info.external_attr >> 16) & 0o170000
    if unix_type == stat.S_IFLNK:
        raise ArchiveSecurityError(
            f"archive contains symlink: {filename!r}"
        )

    # --- Reject entries with huge compressed size (coarse zip bomb) ---
    if info.compress_size > _MAX_ARCHIVE_SIZE:
        raise ArchiveSecurityError(
            f"archive entry too large: {filename!r} "
            f"({info.compress_size} bytes compressed)"
        )

    # --- Reject entries exceeding per-file uncompressed size cap ---
    if info.file_size > _MAX_PER_FILE_SIZE:
        raise ArchiveSecurityError(
            f"archive entry exceeds per-file size cap: {filename!r} "
            f"({info.file_size} bytes, max {_MAX_PER_FILE_SIZE})"
        )

    # --- Reject suspicious expansion ratio (zip bomb detection) ---
    if info.compress_size > 0 and info.file_size > _MAX_EXPANSION_RATIO * info.compress_size:
        raise ArchiveSecurityError(
            f"archive entry has suspicious expansion ratio: {filename!r} "
            f"({info.file_size} / {info.compress_size})"
        )


def _validate_no_duplicate_entries(infos: list[zipfile.ZipInfo]) -> None:
    """Reject archives that contain duplicate entry paths.

    Normalisation includes slash canonicalisation and trailing-slash
    stripping, followed by portable collision resolution (NFC
    normalisation + casefold) so that ``Foo.py`` and ``foo.py`` are
    recognized as the same entry regardless of host filesystem case
    sensitivity.

    Raises :class:`ArchiveSecurityError` on the first duplicate found.
    """
    seen: set[str] = set()
    for info in infos:
        clean = _normalize_entry_path(info.filename)
        if clean == "":
            continue
        key = _portable_collision_key(clean)
        if key in seen:
            raise ArchiveSecurityError(
                f"archive contains duplicate entry: {clean!r}"
            )
        seen.add(key)


def _validate_no_path_conflicts(infos: list[zipfile.ZipInfo]) -> None:
    """Reject archives where a file entry collides with a directory
    implied by another entry.

    Portable collision keys (NFC + casefold) are used so that
    case-only or Unicode-normalisation differences do not bypass
    conflict detection.

    Two forms of conflict are detected:

    1. The *same* portable key appears as both a file and a directory
       entry (e.g. ``dir`` file + ``dir/`` directory marker).

    2. A file's portable key is a prefix of another archive entry's
       key, meaning that path must simultaneously be a file and a
       directory at extraction time (e.g. ``dir`` file + ``dir/child``
       file → ``dir`` can't be both).

    Raises :class:`ArchiveSecurityError` on the first conflict found.
    """
    dir_keys: set[str] = set()
    file_keys: dict[str, str] = {}  # portable key -> display path

    for info in infos:
        clean = _normalize_entry_path(info.filename)
        if clean == "":
            continue
        key = _portable_collision_key(clean)
        if info.is_dir():
            dir_keys.add(key)
        else:
            file_keys.setdefault(key, clean)

    # --- Conflict form 1: same key is both file and dir ---
    file_key_set = set(file_keys)
    both = dir_keys & file_key_set
    if both:
        display = [file_keys[k] for k in sorted(both)]
        raise ArchiveSecurityError(
            f"archive contains conflicting file/directory paths: "
            f"{display!r}"
        )

    # --- Conflict form 2: file key is prefix of another entry ---
    all_keys = dir_keys | file_key_set
    for file_key, file_display in sorted(file_keys.items()):
        prefix = file_key + "/"
        for other_key in all_keys:
            if other_key.startswith(prefix):
                raise ArchiveSecurityError(
                    f"archive entry {file_display!r} is a file but "
                    f"another entry requires it to be a directory"
                )


# ---------------------------------------------------------------------------
# Single-root normalization (GitHub-style archives)
# ---------------------------------------------------------------------------


def _normalize_single_root(stage_dir: Path) -> None:
    """Normalize a GitHub-style single-root archive.

    GitHub ZIP archives wrap repository content inside a directory named
    like ``owner-repo-<sha>``.  Normalization is applied **only** when
    the archive contains *exactly one* top-level entry (hidden or
    visible) and that entry is a directory.

    Archives with multiple top-level entries, hidden files, hidden
    directories, or a top-level file are left as-is.
    """
    entries = list(stage_dir.iterdir())

    # Only normalize when there is exactly one top-level entry and it
    # is a directory.  Hidden files/dirs, top-level files, and
    # multi-root archives are all left intact.
    if len(entries) != 1:
        return  # Empty, flat, or multi-root — leave as-is.

    inner = entries[0]
    if not inner.is_dir():
        return  # Single top-level file — leave as-is.

    # Move all children of inner/ up into stage_dir.
    for child in sorted(inner.iterdir()):
        target = stage_dir / child.name
        if target.exists():
            raise ArchiveSecurityError(
                f"archive wrapper normalization would overwrite existing "
                f"top-level entry: {child.name!r}"
            )
        child.rename(target)

    # Remove the now-empty wrapper directory.
    if inner.is_dir():
        inner.rmdir()


# ---------------------------------------------------------------------------
# Wheel building helpers
# ---------------------------------------------------------------------------


def build_wheel_from_staged(
    staged: StagedSource,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Build a wheel from a staged source directory.

    Delegates to :func:`zealfie.building.build_wheel`.  The staged
    source is already normalized (single-root unwinding happened at
    acquisition time).

    Args:
        staged: A staged source from :func:`acquire_source`.
        output_dir: Optional output directory for the wheel file.

    Returns:
        Path to the built wheel.
    """
    from zealfie.building import build_wheel

    return build_wheel(staged.stage_dir, output_dir=output_dir)


def inspect_wheel_from_staged(
    staged: StagedSource,
    *,
    output_dir: Path | None = None,
) -> "zealfie.building.InspectedWheel":  # noqa: F821
    """Build and inspect a wheel from a staged source.

    Convenience that builds the wheel then inspects it, returning an
    :class:`~zealfie.building.InspectedWheel`.

    Args:
        staged: A staged source from :func:`acquire_source`.
        output_dir: Output directory for the built wheel.  **Required**
            — the returned ``InspectedWheel.wheel_path`` is guaranteed
            to point to a file that exists.

    Returns:
        Inspected wheel metadata.

    Raises:
        AcquisitionError: If *output_dir* is ``None``.
    """
    from zealfie.building import build_wheel, inspect_wheel

    if output_dir is None:
        raise AcquisitionError(
            "inspect_wheel_from_staged requires output_dir; "
            "pass a persistent Path to keep the wheel after inspection."
        )

    wheel = build_wheel(staged.stage_dir, output_dir=output_dir)
    return inspect_wheel(wheel)
