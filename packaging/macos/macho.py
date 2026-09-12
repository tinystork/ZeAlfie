"""Mach-O/static-archive classification, ARM64-only audit, and thinning.

This module is the packaging-time binary brain of the macOS bundle:

* :func:`read_macho_info` parses a Mach-O header and reports whether a file
  is Mach-O, whether it is thin or a FAT (universal) container, and which
  CPU architectures it contains;
* :func:`read_archive_info` recognises **static archives by CONTENT** (the
  ``!<arch>\\n`` magic, never the ``.a`` suffix), validates the container
  structure and inspects the architectures of its members;
* :func:`detect_kind` classifies a file as ``macho`` / ``fat-macho`` /
  ``archive`` / ``fat-archive`` / ``data`` from content only;
* :func:`audit_tree` recursively inventories a tree with SEPARATE evidence
  for Mach-O images and static archives, and reports ``ARM64_ONLY`` /
  ``X86_64_RESIDUES`` across both categories;
* :func:`thin_to_arm64` normalises a universal2 file down to its arm64
  slice with ``lipo`` (atomically, preserving mode) and FAILS CLOSED on any
  lipo error or on a result that is not exactly arm64.

Two result kinds are accepted after thinning, strictly separated:

* a **Mach-O image** result must be thin arm64 (``is_macho=True``,
  ``is_fat=False``, ``archs=("arm64",)``) — the original, unchanged gate;
* a **static archive** result (magic ``!<arch>\\n``) must be a structurally
  valid ``ar`` container whose native architecture inspection reports
  exactly arm64 (no x86_64, not empty, not both) AND for which ``ar -t``
  succeeds.

It is stdlib-only and platform-neutral: the FAT/Mach-O/``ar`` parsers work
identically on Linux (so they are hermetically testable) and on macOS.  Only
:func:`thin_to_arm64` needs the external ``lipo`` (and ``ar`` for archive
validation) tools, which exist on every macOS host; both are injectable so
the gates can be tested on hosts without them.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

__all__ = [
    "MachOError",
    "ThinningError",
    "MachOInfo",
    "ArchiveInfo",
    "ARCH_ARM64",
    "ARCH_X86_64",
    "ARCHIVE_MAGIC",
    "KIND_DATA",
    "KIND_MACHO",
    "KIND_FAT_MACHO",
    "KIND_ARCHIVE",
    "KIND_FAT_ARCHIVE",
    "read_macho_info",
    "read_archive_info",
    "is_macho",
    "is_static_archive",
    "detect_kind",
    "iter_macho_files",
    "audit_tree",
    "thin_to_arm64",
    "thin_tree_to_arm64",
    "validate_thin_arm64_archive",
    "format_audit",
]


class MachOError(RuntimeError):
    """Base class for every fail-closed Mach-O/archive error."""


class ThinningError(MachOError):
    """A universal binary could not be normalised to thin arm64."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARCH_ARM64 = "arm64"
ARCH_X86_64 = "x86_64"

#: Content magic of a Unix static archive (BSD/GNU ``ar``).
ARCHIVE_MAGIC = b"!<arch>\n"

KIND_DATA = "data"
KIND_MACHO = "macho"
KIND_FAT_MACHO = "fat-macho"
KIND_ARCHIVE = "archive"
KIND_FAT_ARCHIVE = "fat-archive"

#: Mach-O CPU type constants (mach/machine.h).
_CPU_TYPE_X86 = 7
_CPU_TYPE_X86_64 = 7 | 0x01000000  # 0x01000007
_CPU_TYPE_ARM = 12
_CPU_TYPE_ARM64 = 12 | 0x01000000  # 0x0100000c
_CPU_TYPE_ARM64_32 = 12 | 0x02000000

_CPU_NAMES: dict[int, str] = {
    _CPU_TYPE_X86: "i386",
    _CPU_TYPE_X86_64: ARCH_X86_64,
    _CPU_TYPE_ARM: "arm",
    _CPU_TYPE_ARM64: ARCH_ARM64,
    _CPU_TYPE_ARM64_32: "arm64_32",
}

# Thin Mach-O magics (as raw on-disk byte sequences).
_THIN_MAGICS: dict[bytes, bool] = {
    b"\xce\xfa\xed\xfe": True,   # MH_MAGIC (little-endian host)
    b"\xcf\xfa\xed\xfe": True,   # MH_MAGIC_64 (little-endian host)
    b"\xfe\xed\xfa\xce": False,  # MH_CIGAM (big-endian)
    b"\xfe\xed\xfa\xcf": False,  # MH_CIGAM_64
}

# FAT container magics -> (is_64, big_endian).
_FAT_MAGICS: dict[bytes, tuple[bool, bool]] = {
    b"\xca\xfe\xba\xbe": (False, True),   # FAT_MAGIC
    b"\xbe\xba\xfe\xca": (False, False),  # FAT_CIGAM
    b"\xca\xfe\xba\xbf": (True, True),    # FAT_MAGIC_64
    b"\xbf\xba\xfe\xca": (True, False),   # FAT_CIGAM_64
}

#: Fat header is bounded, but be liberal with the declared arch count.
_MAX_FAT_ARCHS = 64

#: ``ar`` member header is a fixed 60-byte ASCII record.
_AR_HEADER_SIZE = 60
_AR_FMAG = b"`\n"
#: BSD long-name prefix: the member data starts with the N-byte filename.
_AR_BSD_LONGNAME = "#1/"
#: Darwin symbol-table member names (never object files).
_AR_SYMDEF_NAMES = ("__.SYMDEF",)


@dataclass(frozen=True, slots=True)
class MachOInfo:
    """Classification of one Mach-O image."""

    filename: str
    is_macho: bool
    is_fat: bool
    archs: tuple[str, ...]

    @property
    def is_thin(self) -> bool:
        return self.is_macho and not self.is_fat

    def contains(self, arch: str) -> bool:
        return arch in self.archs


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    """Classification of one static archive (``!<arch>`` container)."""

    filename: str
    is_archive: bool
    valid: bool
    archs: tuple[str, ...]
    members: int

    def contains(self, arch: str) -> bool:
        return arch in self.archs


# ---------------------------------------------------------------------------
# Low-level header helpers (bytes-based, fail closed)
# ---------------------------------------------------------------------------


def _cpu_name(cputype: int) -> str:
    return _CPU_NAMES.get(cputype & 0xFFFFFFFF, f"cputype:{cputype}")


def _dedupe(values: list[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return tuple(unique)


def _parse_fat_container(
    blob: bytes,
) -> tuple[tuple[str, ...], list[tuple[int, int]]]:
    """Parse a FAT container header from *blob*.

    Returns ``(archs, slices)`` where ``slices`` is a list of
    ``(cputype, offset, size)`` in declared order.  Raises
    :class:`MachOError` on a malformed/truncated header.
    """
    if len(blob) < 8:
        raise MachOError("truncated FAT header")
    magic = blob[:4]
    fat = _FAT_MAGICS.get(magic)
    if fat is None:
        raise MachOError(f"not a FAT container: magic={magic!r}")
    is_64, big_endian = fat
    endian = ">" if big_endian else "<"
    nfat = struct.unpack(endian + "I", blob[4:8])[0]
    if nfat > _MAX_FAT_ARCHS:
        raise MachOError(f"implausible FAT nfat_arch={nfat}")
    entry_size = 32 if is_64 else 20
    archs: list[str] = []
    slices: list[tuple[int, int, int]] = []
    try:
        for index in range(nfat):
            start = 8 + index * entry_size
            chunk = blob[start:start + entry_size]
            if len(chunk) < entry_size:
                raise MachOError("truncated FAT architecture table")
            cputype = struct.unpack(endian + "i", chunk[:4])[0]
            if is_64:
                offset, size = struct.unpack(endian + "QQ", chunk[8:24])
            else:
                offset, size = struct.unpack(endian + "II", chunk[8:16])
            archs.append(_cpu_name(cputype))
            slices.append((cputype & 0xFFFFFFFF, offset, size))
    except struct.error as exc:
        raise MachOError(f"malformed FAT header: {exc}") from exc
    return _dedupe(archs), slices


def _member_archs(member: bytes) -> list[str]:
    """Architectures declared by one archive member (object file)."""
    if len(member) < 8:
        return []
    magic = member[:4]
    if magic in _THIN_MAGICS:
        little = _THIN_MAGICS[magic]
        cputype = struct.unpack("<i" if little else ">i", member[4:8])[0]
        return [_cpu_name(cputype)]
    if magic in _FAT_MAGICS:
        archs, _slices = _parse_fat_container(member)
        return list(archs)
    return []


def _parse_ar_members(blob: bytes) -> tuple[bool, list[str], int]:
    """Parse an ``!<arch>`` archive *blob*.

    Returns ``(valid, archs, member_count)``.  Structural validation is
    strict (fail closed): every 60-byte header must carry the ``\\x60\\n``
    FMAG, a decimal size and data that fits in the file.  BSD long names
    (``#1/<len>``) and GNU long names (``//`` string table) are resolved,
    and Darwin symbol tables (``__.SYMDEF*``) are excluded from the object
    count; the architecture set is the union of the OBJECT members.
    """
    if not blob.startswith(ARCHIVE_MAGIC):
        return False, [], 0
    offset = len(ARCHIVE_MAGIC)
    archs: list[str] = []
    members = 0
    gnu_longnames: bytes | None = None
    while offset < len(blob):
        if blob[offset:] == b"\n":  # trailing newline padding
            break
        header = blob[offset:offset + _AR_HEADER_SIZE]
        if len(header) < _AR_HEADER_SIZE:
            return False, [], 0
        if header[58:60] != _AR_FMAG:
            return False, [], 0
        try:
            size = int(header[48:58].decode("ascii").strip() or "-1")
        except ValueError:
            return False, [], 0
        if size < 0:
            return False, [], 0
        data_start = offset + _AR_HEADER_SIZE
        data_end = data_start + size
        if data_end > len(blob):
            return False, [], 0
        name = header[0:16].decode("ascii", "replace").strip()
        data = blob[data_start:data_end]

        if name == "//":
            gnu_longnames = data  # GNU long-name string table
        elif name == "/" or name.startswith("/SYM64/"):
            pass  # GNU symbol table
        elif name.startswith(_AR_BSD_LONGNAME):
            try:
                namelen = int(name[len(_AR_BSD_LONGNAME):])
            except ValueError:
                return False, [], 0
            if namelen > len(data):
                return False, [], 0
            real_name = (
                data[:namelen].split(b"\x00", 1)[0].decode("ascii", "replace")
            )
            if not real_name.startswith(_AR_SYMDEF_NAMES):
                members += 1
                archs.extend(_member_archs(data[namelen:]))
        elif name.startswith("/") and name[1:].strip().isdigit():
            if gnu_longnames is None:
                return False, [], 0
            gnu_offset = int(name[1:].strip())
            if gnu_offset >= len(gnu_longnames):
                return False, [], 0
            end = gnu_longnames.find(b"\n", gnu_offset)
            real_name = gnu_longnames[
                gnu_offset:end if end != -1 else None
            ].decode("ascii", "replace").rstrip("/\n")
            members += 1
            archs.extend(_member_archs(data))
        else:
            real_name = name.rstrip("/")
            if real_name.startswith(_AR_SYMDEF_NAMES):
                pass
            elif real_name:
                members += 1
                archs.extend(_member_archs(data))

        offset = data_end + (size & 1)
    return True, archs, members


# ---------------------------------------------------------------------------
# Public classification
# ---------------------------------------------------------------------------


def _read_header(path: Path, size: int = 8) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError as exc:  # unreadable file is a hard packaging error
        raise MachOError(f"cannot read {path}: {exc}") from exc


def read_macho_info(path: str | os.PathLike[str]) -> MachOInfo:
    """Classify *path* as Mach-O / FAT / non-Mach-O from its header.

    Never guesses from the filename: only the file CONTENT is inspected.
    Note that a universal (FAT) static archive also presents a FAT header;
    use :func:`detect_kind` to distinguish ``fat-macho`` from
    ``fat-archive``.
    """
    p = Path(path)
    header = _read_header(p)
    if len(header) < 4:
        return MachOInfo(str(p), False, False, ())

    magic = header[:4]
    if magic in _THIN_MAGICS:
        little = _THIN_MAGICS[magic]
        cputype = struct.unpack("<i" if little else ">i", header[4:8])[0]
        return MachOInfo(str(p), True, False, (_cpu_name(cputype),))

    if magic in _FAT_MAGICS:
        try:
            archs, _slices = _parse_fat_container(p.read_bytes()[: 8 + 32 * _MAX_FAT_ARCHS])
        except MachOError:
            raise
        return MachOInfo(str(p), True, True, archs)

    return MachOInfo(str(p), False, False, ())


def read_archive_info(path: str | os.PathLike[str]) -> ArchiveInfo:
    """Recognise + inspect a static archive by CONTENT.

    ``is_archive`` is True only when the file starts with the exact
    ``!<arch>\\n`` magic.  ``valid`` is True only when the container parses
    strictly; ``archs`` is the union of the member architectures (empty when
    the members are not Mach-O objects).
    """
    p = Path(path)
    header = _read_header(p)
    if header[:8] != ARCHIVE_MAGIC:
        return ArchiveInfo(str(p), False, False, (), 0)
    blob = p.read_bytes()
    valid, archs, members = _parse_ar_members(blob)
    return ArchiveInfo(str(p), True, valid, _dedupe(archs), members)


def is_macho(path: str | os.PathLike[str]) -> bool:
    """True when *path* is a Mach-O image or FAT container."""
    return read_macho_info(path).is_macho


def is_static_archive(path: str | os.PathLike[str]) -> bool:
    """True when *path* starts with the exact ``!<arch>\\n`` magic."""
    return read_archive_info(path).is_archive


def _fat_first_slice_magic(p: Path) -> bytes:
    """Magic bytes at the first FAT slice offset ('' when malformed)."""
    with open(p, "rb") as fh:
        head = fh.read(8)
        if len(head) < 8 or head[:4] not in _FAT_MAGICS:
            return b""
        is_64, big_endian = _FAT_MAGICS[head[:4]]
        endian = ">" if big_endian else "<"
        nfat = struct.unpack(endian + "I", head[4:8])[0]
        if nfat == 0:
            return b""
        entry = fh.read(32 if is_64 else 20)
        if len(entry) < (32 if is_64 else 20):
            return b""
        if is_64:
            offset = struct.unpack(endian + "Q", entry[8:16])[0]
        else:
            offset = struct.unpack(endian + "I", entry[8:12])[0]
        fh.seek(offset)
        return fh.read(8)


def detect_kind(path: str | os.PathLike[str]) -> str:
    """Classify *path* by content: one of the ``KIND_*`` constants."""
    p = Path(path)
    header = _read_header(p)
    if header[:8] == ARCHIVE_MAGIC:
        return KIND_ARCHIVE
    if header[:4] in _THIN_MAGICS:
        return KIND_MACHO
    if header[:4] in _FAT_MAGICS:
        first = _fat_first_slice_magic(p)
        return KIND_FAT_ARCHIVE if first[:8] == ARCHIVE_MAGIC else KIND_FAT_MACHO
    return KIND_DATA


# ---------------------------------------------------------------------------
# Tree inventory
# ---------------------------------------------------------------------------

#: Directories that can never contain packaging-relevant binaries.
_SKIP_DIR_NAMES = frozenset({"__pycache__"})


def iter_macho_files(root: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield every regular (non-symlink) Mach-O/FAT file under *root*.

    Applicability is decided by CONTENT (header magic) only.  Thin static
    archives are NOT yielded (they cannot be thinned); they are handled by
    the audit.
    """
    root_path = Path(root)
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                continue
            if not candidate.is_file():
                continue
            if read_macho_info(candidate).is_macho:
                yield candidate


def audit_tree(root: str | os.PathLike[str]) -> dict:
    """Recursively audit *root* for ARM64-only cleanliness.

    Mach-O images and static archives are inventoried SEPARATELY.  The
    overall verdict is ``PASS`` only when BOTH categories are clean:

    * no Mach-O image carries x86_64, and every Mach-O image carries arm64;
    * no static archive carries x86_64, every archive is structurally valid
      and its architecture inspection is exactly arm64.

    ``X86_64_RESIDUES`` counts x86_64-bearing files across both categories.
    """
    root_path = Path(root)
    files_checked = 0
    macho_files: list[str] = []
    fat_files: list[str] = []
    archive_files: list[str] = []
    fat_archive_files: list[str] = []
    x86_64_macho: list[str] = []
    x86_64_archives: list[str] = []
    non_arm64_macho: list[str] = []
    non_arm64_archives: list[str] = []
    invalid_archives: list[str] = []
    arch_histogram: dict[str, int] = {}
    archive_arch_histogram: dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            files_checked += 1
            rel = str(candidate.relative_to(root_path))
            kind = detect_kind(candidate)

            if kind in (KIND_ARCHIVE, KIND_FAT_ARCHIVE):
                archive_files.append(rel)
                if kind == KIND_FAT_ARCHIVE:
                    # A universal archive presents a FAT header; it belongs to
                    # the archive category, not the Mach-O image category.  If
                    # one survives thinning it is still judged by its declared
                    # architectures (never silently tolerated).
                    fat_archive_files.append(rel)
                    archs = read_macho_info(candidate).archs
                    valid = True
                else:
                    info = read_archive_info(candidate)
                    valid = info.valid
                    archs = info.archs
                if not valid:
                    invalid_archives.append(rel)
                    continue
                for arch in archs:
                    archive_arch_histogram[arch] = (
                        archive_arch_histogram.get(arch, 0) + 1
                    )
                if ARCH_X86_64 in archs:
                    x86_64_archives.append(rel)
                if archs != (ARCH_ARM64,):
                    non_arm64_archives.append(rel)
                continue

            if kind not in (KIND_MACHO, KIND_FAT_MACHO):
                continue
            info = read_macho_info(candidate)
            macho_files.append(rel)
            if info.is_fat:
                fat_files.append(rel)
            for arch in info.archs:
                arch_histogram[arch] = arch_histogram.get(arch, 0) + 1
            if info.contains(ARCH_X86_64):
                x86_64_macho.append(rel)
            if not info.contains(ARCH_ARM64):
                non_arm64_macho.append(rel)

    x86_total = len(x86_64_macho) + len(x86_64_archives)
    clean = not (
        x86_64_macho
        or x86_64_archives
        or non_arm64_macho
        or non_arm64_archives
        or invalid_archives
    )
    return {
        "root": str(root_path),
        "files_checked": files_checked,
        # Mach-O image category
        "macho_files": sorted(macho_files),
        "fat_files": sorted(fat_files),
        "non_arm64_macho_files": sorted(non_arm64_macho),
        "x86_64_files": sorted(x86_64_macho),
        "arch_histogram": dict(sorted(arch_histogram.items())),
        # static archive category (separate evidence)
        "archive_files": sorted(archive_files),
        "fat_archive_files": sorted(fat_archive_files),
        "non_arm64_archive_files": sorted(non_arm64_archives),
        "x86_64_archive_files": sorted(x86_64_archives),
        "invalid_archive_files": sorted(invalid_archives),
        "archive_arch_histogram": dict(sorted(archive_arch_histogram.items())),
        # overall verdict (both categories)
        "x86_64_residue_files": sorted(x86_64_macho + x86_64_archives),
        "X86_64_RESIDUES": x86_total,
        "ARM64_ONLY": "PASS" if clean else "FAIL",
    }


def format_audit(audit: dict) -> str:
    """Compact human-readable one-block rendering of an :func:`audit_tree`."""
    lines = [
        f"files_checked={audit['files_checked']}",
        f"macho_files={len(audit['macho_files'])}",
        f"fat_macho_files={len(audit['fat_files'])}",
        f"arch_histogram={audit['arch_histogram']}",
        f"archive_files={len(audit.get('archive_files', []))}",
        f"fat_archive_files={len(audit.get('fat_archive_files', []))}",
        f"archive_arch_histogram={audit.get('archive_arch_histogram', {})}",
        f"ARM64_ONLY={audit['ARM64_ONLY']}",
        f"X86_64_RESIDUES={audit['X86_64_RESIDUES']}",
    ]
    if audit["x86_64_files"]:
        lines.append("x86_64 mach-o residue: " + ", ".join(audit["x86_64_files"]))
    if audit.get("x86_64_archive_files"):
        lines.append(
            "x86_64 archive residue: "
            + ", ".join(audit["x86_64_archive_files"])
        )
    if audit.get("invalid_archive_files"):
        lines.append(
            "malformed archives: " + ", ".join(audit["invalid_archive_files"])
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thinning
# ---------------------------------------------------------------------------


def _lipo_thin(source: Path, destination: Path, arch: str) -> None:
    """Run ``lipo -thin <arch> -output <destination> <source>`` (fail closed)."""
    lipo = shutil.which("lipo")
    if lipo is None:
        raise ThinningError(
            "`lipo` was not found on PATH — the ARM64-only normalisation "
            "cannot run; refusing to continue"
        )
    proc = subprocess.run(
        [lipo, "-thin", arch, "-output", str(destination), str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise ThinningError(
            f"lipo -thin {arch} failed for {source} (rc={proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ThinningError(f"lipo produced no output for {source}")


def _ar_list(path: Path) -> None:
    """Run ``ar -t <archive>``; fail closed on any error or empty listing."""
    ar = shutil.which("ar")
    if ar is None:
        raise ThinningError(
            f"`ar` was not found on PATH — cannot validate the thinned "
            f"static archive {path}"
        )
    proc = subprocess.run(
        [ar, "-t", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise ThinningError(
            f"ar -t failed for {path} (rc={proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    if not proc.stdout.strip():
        raise ThinningError(f"ar -t listed no members for {path}")


def validate_thin_arm64_archive(
    path: str | os.PathLike[str],
    *,
    _run_ar: Callable[[Path], None] | None = None,
) -> ArchiveInfo:
    """Validate that *path* is a structurally valid, exactly-arm64 archive.

    Fail closed when the container magic is absent, the container is
    malformed, the native architecture inspection is not exactly arm64
    (empty / unknown / x86_64-only / arm64+x86_64), or ``ar -t`` fails.
    """
    target = Path(path)
    info = read_archive_info(target)
    if not info.is_archive:
        raise ThinningError(
            f"{target}: lipo result is not a static archive "
            f"(missing the {ARCHIVE_MAGIC!r} content magic)"
        )
    if not info.valid:
        raise ThinningError(f"{target}: malformed static archive container")
    if info.archs != (ARCH_ARM64,):
        raise ThinningError(
            f"{target}: static archive architecture inspection is not exactly "
            f"arm64 (archs={info.archs})"
        )
    runner = _run_ar if _run_ar is not None else _ar_list
    runner(target)
    return info


def thin_to_arm64(
    path: str | os.PathLike[str],
    *,
    _run_lipo: Callable[[Path, Path, str], None] | None = None,
    _run_ar: Callable[[Path], None] | None = None,
) -> str:
    """Normalise one file to thin arm64.

    Returns ``"skipped"`` (not a binary), ``"unchanged"`` (already thin
    arm64 or an already-arm64 archive) or ``"thinned"`` (universal →
    arm64).  FAILS CLOSED (:class:`ThinningError`) when:

    * a thin Mach-O image is not arm64 (an x86_64 residue);
    * a universal container contains no arm64 slice;
    * a thin static archive is not exactly arm64;
    * ``lipo`` fails, or the thinned result is neither a thin arm64 Mach-O
      image (for image sources) nor a valid exactly-arm64 static archive
      (for archive sources);
    * ``ar -t`` fails on a thinned archive result.

    The replacement is atomic (``os.replace`` from a sibling temp file) and
    preserves the original file mode.
    """
    source = Path(path)
    kind = detect_kind(source)

    if kind == KIND_DATA:
        return "skipped"

    if kind == KIND_ARCHIVE:
        info = read_archive_info(source)
        if not info.valid:
            raise ThinningError(f"{source}: malformed static archive container")
        if info.archs == (ARCH_ARM64,):
            return "unchanged"
        raise ThinningError(
            f"{source}: static archive is not exactly arm64 (archs="
            f"{info.archs}); a thin archive cannot be re-thinned"
        )

    if kind == KIND_MACHO:
        info = read_macho_info(source)
        if info.contains(ARCH_ARM64):
            return "unchanged"
        raise ThinningError(
            f"{source}: thin Mach-O with no arm64 slice (archs={info.archs})"
        )

    # Universal container (fat Mach-O image or fat static archive).
    info = read_macho_info(source)
    if not info.contains(ARCH_ARM64):
        raise ThinningError(
            f"{source}: universal container without an arm64 slice "
            f"(archs={info.archs})"
        )

    mode = source.stat().st_mode
    runner = _run_lipo if _run_lipo is not None else _lipo_thin
    fd, tmp_name = tempfile.mkstemp(
        prefix=source.name + ".", suffix=".thin", dir=str(source.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        runner(source, tmp, ARCH_ARM64)
        if kind == KIND_FAT_ARCHIVE:
            validate_thin_arm64_archive(tmp, _run_ar=_run_ar)
        else:
            result = read_macho_info(tmp)
            if not result.is_macho or result.is_fat or result.archs != (ARCH_ARM64,):
                raise ThinningError(
                    f"{source}: lipo result is not thin arm64 "
                    f"(is_macho={result.is_macho}, fat={result.is_fat}, "
                    f"archs={result.archs})"
                )
        os.chmod(tmp, mode)
        os.replace(tmp, source)
    finally:
        if tmp.exists():
            tmp.unlink()
    return "thinned"


def thin_tree_to_arm64(
    root: str | os.PathLike[str],
    *,
    _run_lipo: Callable[[Path, Path, str], None] | None = None,
    _run_ar: Callable[[Path], None] | None = None,
) -> dict:
    """Thin every universal Mach-O / static archive under *root*.

    Returns ``{"files_thinned": [...], "files_unchanged": [...],
    "files_skipped": N, "archives_thinned": [...]}``.  ``_run_lipo`` and
    ``_run_ar`` are injectable seams (hermetic tests on hosts without the
    Apple toolchain).
    """
    thinned: list[str] = []
    unchanged: list[str] = []
    archives_thinned: list[str] = []
    skipped = 0
    for candidate in iter_macho_files(root):
        was_archive = detect_kind(candidate) == KIND_FAT_ARCHIVE
        outcome = thin_to_arm64(candidate, _run_lipo=_run_lipo, _run_ar=_run_ar)
        if outcome == "thinned":
            thinned.append(str(candidate))
            if was_archive:
                archives_thinned.append(str(candidate))
        elif outcome == "unchanged":
            unchanged.append(str(candidate))
        else:
            skipped += 1
    return {
        "files_thinned": sorted(thinned),
        "files_unchanged": sorted(unchanged),
        "files_skipped": skipped,
        "archives_thinned": sorted(archives_thinned),
    }
