"""Mach-O classification, ARM64-only audit, and FAT thinning (ZA-MAC-BOOT-01).

This module is the packaging-time Mach-O brain of the macOS bundle:

* :func:`read_macho_info` parses a file header and reports whether it is a
  Mach-O file, whether it is thin or a FAT (universal) container, and which
  CPU architectures it contains.  It reads only the header bytes, so it is
  cheap and safe to run over a whole bundle;
* :func:`audit_tree` recursively inventories an installed tree, classifying
  files by CONTENT (Mach-O magic), never by extension — source, data,
  resources, plists, scripts and icons are therefore never mistaken for
  binaries;
* :func:`thin_to_arm64` normalises a universal2 Mach-O file down to its
  arm64 slice with ``lipo`` (atomically, preserving mode) and FAILS CLOSED
  on any lipo error or on a result that is not exactly thin arm64.

It is deliberately stdlib-only and platform-neutral: the FAT/Mach-O parser
works identically on Linux (so it is hermetically testable) and on macOS.
Only :func:`thin_to_arm64` needs the external ``lipo`` tool, which exists
on every macOS host.
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
    "ARCH_ARM64",
    "ARCH_X86_64",
    "read_macho_info",
    "is_macho",
    "iter_macho_files",
    "audit_tree",
    "thin_to_arm64",
    "thin_tree_to_arm64",
    "format_audit",
]


class MachOError(RuntimeError):
    """Base class for every fail-closed Mach-O error."""


class ThinningError(MachOError):
    """A universal Mach-O file could not be normalised to thin arm64."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARCH_ARM64 = "arm64"
ARCH_X86_64 = "x86_64"

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


@dataclass(frozen=True, slots=True)
class MachOInfo:
    """Classification of one file."""

    filename: str
    is_macho: bool
    is_fat: bool
    archs: tuple[str, ...]

    @property
    def is_thin(self) -> bool:
        return self.is_macho and not self.is_fat

    def contains(self, arch: str) -> bool:
        return arch in self.archs


def _read_header(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(8)
    except OSError as exc:  # unreadable file is a hard packaging error
        raise MachOError(f"cannot read {path}: {exc}") from exc


def read_macho_info(path: str | os.PathLike[str]) -> MachOInfo:
    """Classify *path* as Mach-O / FAT / non-Mach-O from its header.

    Never guesses from the filename: only the file CONTENT is inspected.
    """
    p = Path(path)
    header = _read_header(p)
    if len(header) < 4:
        return MachOInfo(str(p), False, False, ())

    magic = header[:4]
    if magic in _THIN_MAGICS:
        little = _THIN_MAGICS[magic]
        cputype = struct.unpack("<i" if little else ">i", header[4:8])[0]
        arch = _CPU_NAMES.get(cputype & 0xFFFFFFFF, f"cputype:{cputype}")
        return MachOInfo(str(p), True, False, (arch,))

    fat = _FAT_MAGICS.get(magic)
    if fat is None:
        return MachOInfo(str(p), False, False, ())

    is_64, big_endian = fat
    archs: list[str] = []
    try:
        with open(p, "rb") as fh:
            endian = ">" if big_endian else "<"
            # ``header`` above holds magic(4) + nfat_arch(4); do NOT re-read
            # the magic as the arch count.
            nfat = struct.unpack(endian + "I", header[4:8])[0]
            if nfat > _MAX_FAT_ARCHS:
                raise MachOError(
                    f"{p}: implausible FAT nfat_arch={nfat}"
                )
            entry_size = 32 if is_64 else 20
            fh.seek(8)  # skip magic(4) + nfat_arch(4)
            raw = fh.read(entry_size * nfat)
            for index in range(nfat):
                chunk = raw[index * entry_size:(index + 1) * entry_size]
                if len(chunk) < 4:
                    raise MachOError(
                        f"{p}: truncated FAT architecture table"
                    )
                cputype = struct.unpack(endian + "i", chunk[:4])[0]
                archs.append(
                    _CPU_NAMES.get(cputype & 0xFFFFFFFF, f"cputype:{cputype}")
                )
    except struct.error as exc:
        raise MachOError(f"{p}: malformed FAT header: {exc}") from exc
    # Preserve declared order but drop duplicates.
    unique: list[str] = []
    for arch in archs:
        if arch not in unique:
            unique.append(arch)
    return MachOInfo(str(p), True, True, tuple(unique))


def is_macho(path: str | os.PathLike[str]) -> bool:
    """True when *path* is a Mach-O file (thin or FAT)."""
    return read_macho_info(path).is_macho


#: Directories that can never contain packaging-relevant Mach-O files and
#: would only slow the audit down.  Kept deliberately small: correctness
#: comes from the content sniff, not from skipping trees.
_SKIP_DIR_NAMES = frozenset({"__pycache__"})


def iter_macho_files(root: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield every regular (non-symlink) Mach-O file under *root*.

    Applicability is decided by CONTENT (header magic) only.
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
            try:
                if read_macho_info(candidate).is_macho:
                    yield candidate
            except MachOError:
                raise


def audit_tree(root: str | os.PathLike[str]) -> dict:
    """Recursively audit *root* for ARM64-only cleanliness.

    Returns a JSON-serialisable inventory with, among others, the keys the
    acceptance witness requires:

    * ``files_checked`` — every regular file inspected;
    * ``macho_files`` — the Mach-O subset;
    * ``x86_64_files`` — files containing an x86_64 slice (must be empty);
    * ``non_arm64_macho_files`` — Mach-O files with no arm64 slice;
    * ``ARM64_ONLY`` — ``PASS`` iff no x86_64 residue and every Mach-O file
      carries arm64;
    * ``X86_64_RESIDUES`` — count of x86_64-bearing files.
    """
    root_path = Path(root)
    files_checked = 0
    macho_files: list[str] = []
    fat_files: list[str] = []
    x86_64_files: list[str] = []
    non_arm64: list[str] = []
    arch_histogram: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            files_checked += 1
            info = read_macho_info(candidate)
            if not info.is_macho:
                continue
            rel = str(candidate.relative_to(root_path))
            macho_files.append(rel)
            if info.is_fat:
                fat_files.append(rel)
            for arch in info.archs:
                arch_histogram[arch] = arch_histogram.get(arch, 0) + 1
            if info.contains(ARCH_X86_64):
                x86_64_files.append(rel)
            if not info.contains(ARCH_ARM64):
                non_arm64.append(rel)
    return {
        "root": str(root_path),
        "files_checked": files_checked,
        "macho_files": sorted(macho_files),
        "fat_files": sorted(fat_files),
        "non_arm64_macho_files": sorted(non_arm64),
        "x86_64_files": sorted(x86_64_files),
        "arch_histogram": dict(sorted(arch_histogram.items())),
        "X86_64_RESIDUES": len(x86_64_files),
        "ARM64_ONLY": "PASS"
        if (not x86_64_files and not non_arm64)
        else "FAIL",
    }


def format_audit(audit: dict) -> str:
    """Compact human-readable one-block rendering of an :func:`audit_tree`."""
    lines = [
        f"files_checked={audit['files_checked']}",
        f"macho_files={len(audit['macho_files'])}",
        f"fat_files={len(audit['fat_files'])}",
        f"arch_histogram={audit['arch_histogram']}",
        f"ARM64_ONLY={audit['ARM64_ONLY']}",
        f"X86_64_RESIDUES={audit['X86_64_RESIDUES']}",
    ]
    if audit["x86_64_files"]:
        lines.append("x86_64 residue: " + ", ".join(audit["x86_64_files"]))
    return "\n".join(lines)


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
        raise ThinningError(
            f"lipo produced no output for {source}"
        )


def thin_to_arm64(
    path: str | os.PathLike[str],
    *,
    _run_lipo: Callable[[Path, Path, str], None] | None = None,
) -> str:
    """Normalise one file to thin arm64.

    Returns one of ``"skipped"`` (not Mach-O), ``"unchanged"`` (already
    thin arm64) or ``"thinned"`` (FAT → arm64).  FAILS CLOSED
    (:class:`ThinningError`) when:

    * a FAT file contains no arm64 slice;
    * a thin Mach-O file is not arm64 (an x86_64 residue);
    * ``lipo`` fails or the thinned result is not exactly thin arm64.

    The replacement is atomic (``os.replace`` from a sibling temp file) and
    preserves the original file mode.
    """
    source = Path(path)
    info = read_macho_info(source)
    if not info.is_macho:
        return "skipped"
    if info.is_thin:
        if info.contains(ARCH_ARM64):
            return "unchanged"
        raise ThinningError(
            f"{source}: thin Mach-O with no arm64 slice (archs={info.archs})"
        )
    if not info.contains(ARCH_ARM64):
        raise ThinningError(
            f"{source}: universal Mach-O without an arm64 slice "
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
) -> dict:
    """Thin every FAT Mach-O file under *root* to arm64 (fail closed).

    Returns ``{"files_thinned": [...], "files_unchanged": [...],
    "files_skipped": N}``.  ``_run_lipo`` is an injectable seam (hermetic
    tests on hosts without ``lipo``).
    """
    thinned: list[str] = []
    unchanged: list[str] = []
    skipped = 0
    for candidate in iter_macho_files(root):
        outcome = thin_to_arm64(candidate, _run_lipo=_run_lipo)
        if outcome == "thinned":
            thinned.append(str(candidate))
        elif outcome == "unchanged":
            unchanged.append(str(candidate))
        else:
            skipped += 1
    return {
        "files_thinned": sorted(thinned),
        "files_unchanged": sorted(unchanged),
        "files_skipped": skipped,
    }
