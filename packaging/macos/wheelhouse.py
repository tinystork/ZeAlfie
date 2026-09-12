"""Deterministic macOS ARM64 wheelhouse lock (ZA-MAC-BOOT-01).

The bundle installs its application tree with the PRIVATE bundled Python
from an offline wheelhouse (``pip --no-index --find-links``), so this lock —
plus the SHA-256 verification performed at acquisition time — is the
supply-chain contract of the bundle.

The lock records the EXACT macOS arm64 closure of the ``zealfie`` wheel's
runtime dependencies (PySide6 meta + Essentials + Addons + shiboken6, plus
the build backend/runtime tools ``packaging``, ``build``, ``pyproject_hooks``,
``setuptools``, ``wheel``).  Every SHA-256 was computed from a REAL download
(see ``wheelhouse.lock.toml``); none is guessed.

stdlib-only; no ZeAlfie import.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = [
    "WheelhouseLockError",
    "LockError",
    "WheelhouseVerificationError",
    "WheelEntry",
    "WheelhouseLock",
    "PLATFORM_TAG",
    "PYTHON_TAG",
    "ABI_TAG",
    "CPYTHON_VERSION",
    "default_lock_path",
    "load_lock",
    "expected_filenames",
    "pinned_filenames",
    "pinned_download_specs",
    "verify_pinned_subset",
    "verify_wheelhouse_dir",
    "zealfie_wheel_filename",
    "parse_wheel_filename",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZEALFIE_WHEEL_RE = re.compile(r"^zealfie-\d+\.\d+\.\d+-py3-none-any\.whl$")

#: The wheel tags the macOS closure MUST be resolved for.  Universal2 is the
#: real tag of the official Qt macOS wheels (hence the ARM64 thinning step).
PLATFORM_TAG = "macosx_13_0_universal2"
PYTHON_TAG = "cp313"
ABI_TAG = "cp313"
CPYTHON_VERSION = "3.13.15"


class WheelhouseLockError(RuntimeError):
    """Base class for every wheelhouse-lock failure."""


class LockError(WheelhouseLockError):
    """The lock is missing, malformed, or internally inconsistent."""


class WheelhouseVerificationError(WheelhouseLockError):
    """An on-disk wheelhouse does not match the lock exactly."""


@dataclass(frozen=True, slots=True)
class WheelEntry:
    """One pinned wheel in the lock."""

    name: str
    version: str
    filename: str
    sha256: str | None
    size: int


@dataclass(frozen=True, slots=True)
class WheelhouseLock:
    """Validated content of ``wheelhouse.lock.toml``."""

    zealfie_version: str
    source_commit: str
    platform_tag: str
    python_tag: str
    abi_tag: str
    generated: str
    cpython_version: str
    requirements: tuple[str, ...]
    pip_command: str
    zealfie_wheel: WheelEntry
    wheels: tuple[WheelEntry, ...]

    def wheel_map(self) -> dict[str, WheelEntry]:
        return {entry.filename: entry for entry in self.wheels}


def default_lock_path() -> Path:
    return Path(__file__).resolve().parent / "wheelhouse.lock.toml"


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 65536) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_wheel_filename(filename: str) -> tuple[str, str]:
    """Return ``(distribution, version)`` parsed from a wheel filename."""
    if not filename.endswith(".whl"):
        raise LockError(f"not a wheel filename: {filename!r}")
    stem = filename[: -len(".whl")]
    parts = stem.rsplit("-", 3)
    if len(parts) != 4:
        raise LockError(f"cannot parse wheel filename: {filename!r}")
    dist_version, python, abi, platform = parts
    if not python or not abi or not platform:
        raise LockError(f"cannot parse wheel filename: {filename!r}")
    for index, ch in enumerate(dist_version):
        if ch != "-":
            continue
        dist = dist_version[:index]
        version = dist_version[index + 1:]
        if not dist or not version or not version[0].isdigit():
            continue
        if re.fullmatch(r"[A-Za-z0-9_.]+", dist) and re.fullmatch(
            r"[A-Za-z0-9_.!+]+", version
        ):
            return dist, version
    raise LockError(f"cannot parse distribution/version: {dist_version!r}")


def zealfie_wheel_filename(version: str) -> str:
    return f"zealfie-{version}-py3-none-any.whl"


def _expect(d: dict, key: str, what: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LockError(f"wheelhouse lock missing/invalid {what}: {key!r}")
    return value.strip()


def _expect_table(d: dict, key: str, what: str) -> dict:
    table = d.get(key)
    if not isinstance(table, dict):
        raise LockError(f"wheelhouse lock missing/invalid {what}: {key!r}")
    return table


def load_lock(path: str | os.PathLike[str] | None = None) -> WheelhouseLock:
    """Load and validate ``wheelhouse.lock.toml`` (fail closed)."""
    lock_path = Path(path) if path is not None else default_lock_path()
    try:
        with open(lock_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise LockError(f"wheelhouse lock not found: {lock_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LockError(
            f"wheelhouse lock unreadable/invalid: {lock_path}: {exc}"
        ) from exc

    metadata = _expect_table(data, "metadata", "lock metadata")
    zealfie = _expect_table(data, "zealfie", "[zealfie] wheel")
    wheel_tables = _expect_table(data, "wheel", "[wheel.*] entries")

    zealfie_version = _expect(metadata, "zealfie_version", "zealfie version")
    source_commit = _expect(metadata, "source_commit", "source commit")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit.lower()):
        raise LockError(
            "wheelhouse lock source_commit must be a full 40-char git commit "
            f"SHA: {source_commit!r}"
        )
    platform_tag = _expect(metadata, "platform_tag", "platform tag")
    if platform_tag != PLATFORM_TAG:
        raise LockError(
            f"wheelhouse lock platform_tag must be {PLATFORM_TAG!r}, got "
            f"{platform_tag!r}"
        )
    python_tag = _expect(metadata, "python_tag", "python tag")
    if python_tag != PYTHON_TAG:
        raise LockError(
            f"wheelhouse lock python_tag must be {PYTHON_TAG!r}, got {python_tag!r}"
        )
    abi_tag = _expect(metadata, "abi_tag", "abi tag")
    if abi_tag != ABI_TAG:
        raise LockError(
            f"wheelhouse lock abi_tag must be {ABI_TAG!r}, got {abi_tag!r}"
        )
    cpython_version = _expect(metadata, "cpython_version", "cpython version")
    if cpython_version != CPYTHON_VERSION:
        raise LockError(
            f"wheelhouse lock cpython_version must be {CPYTHON_VERSION!r}, got "
            f"{cpython_version!r}"
        )
    generated = _expect(metadata, "generated", "generation date")
    requirements = metadata.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise LockError(
            "wheelhouse lock metadata.requirements must be a non-empty list"
        )
    pip_command = metadata.get("pip_command")
    if not isinstance(pip_command, str) or not pip_command.strip():
        raise LockError("wheelhouse lock missing metadata.pip_command")

    zealfie_filename = _expect(zealfie, "wheel_filename", "zealfie wheel filename")
    if not _ZEALFIE_WHEEL_RE.match(zealfie_filename):
        raise LockError(
            f"wheelhouse lock zealfie wheel_filename invalid: {zealfie_filename!r}"
        )
    if zealfie_filename != zealfie_wheel_filename(zealfie_version):
        raise LockError(
            "wheelhouse lock zealfie wheel_filename does not match "
            f"zealfie-<version>: expected {zealfie_wheel_filename(zealfie_version)!r}, "
            f"got {zealfie_filename!r}"
        )
    zealfie_commit = _expect(zealfie, "source_commit", "zealfie source commit")
    if zealfie_commit != source_commit:
        raise LockError(
            "wheelhouse lock [zealfie] source_commit differs from "
            f"metadata.source_commit: {zealfie_commit!r} vs {source_commit!r}"
        )
    if zealfie.get("sha256") is not None:
        raise LockError(
            "wheelhouse lock [zealfie] must NOT pin a sha256 (the wheel is "
            "built locally at acquisition time)"
        )
    if zealfie.get("source", "local-build") != "local-build":
        raise LockError(
            "wheelhouse lock [zealfie] source must be 'local-build', got "
            f"{zealfie.get('source')!r}"
        )
    zealfie_entry = WheelEntry(
        name="zealfie",
        version=zealfie_version,
        filename=zealfie_filename,
        sha256=None,
        size=0,
    )

    entries: list[WheelEntry] = []
    for filename, table in wheel_tables.items():
        if not isinstance(table, dict):
            raise LockError(f"wheelhouse lock [wheel.{filename}] is malformed")
        name = _expect(table, "name", f"wheel {filename} name")
        version = _expect(table, "version", f"wheel {filename} version")
        digest = _expect(table, "sha256", f"wheel {filename} sha256").lower()
        if not _SHA256_RE.match(digest):
            raise LockError(
                f"wheelhouse lock wheel {filename} sha256 is not a 64-char "
                f"lowercase hex digest: {digest!r}"
            )
        size = table.get("size")
        if not isinstance(size, int) or size <= 0:
            raise LockError(
                f"wheelhouse lock wheel {filename} size invalid: {size!r}"
            )
        parsed_name, parsed_version = parse_wheel_filename(filename)
        if parsed_name.lower().replace("_", "-") != name.lower().replace("_", "-"):
            raise LockError(
                f"wheelhouse lock wheel {filename}: entry name {name!r} does not "
                f"match the filename distribution {parsed_name!r}"
            )
        if parsed_version != version:
            raise LockError(
                f"wheelhouse lock wheel {filename}: entry version {version!r} "
                f"does not match the filename version {parsed_version!r}"
            )
        entries.append(
            WheelEntry(
                name=name, version=version, filename=filename,
                sha256=digest, size=size,
            )
        )
    if not entries:
        raise LockError("wheelhouse lock contains no pinned wheels")
    entries.sort(key=lambda e: e.filename)

    return WheelhouseLock(
        zealfie_version=zealfie_version,
        source_commit=source_commit,
        platform_tag=platform_tag,
        python_tag=python_tag,
        abi_tag=abi_tag,
        generated=generated,
        cpython_version=cpython_version,
        requirements=tuple(str(r) for r in requirements),
        pip_command=pip_command,
        zealfie_wheel=zealfie_entry,
        wheels=tuple(entries),
    )


def expected_filenames(lock: WheelhouseLock) -> set[str]:
    return {e.filename for e in lock.wheels} | {lock.zealfie_wheel.filename}


def pinned_filenames(lock: WheelhouseLock) -> set[str]:
    return {e.filename for e in lock.wheels}


def pinned_download_specs(lock: WheelhouseLock) -> list[str]:
    """Deterministic ``name==version`` download specs (with ``--no-deps``)."""
    return [f"{entry.name}=={entry.version}" for entry in lock.wheels]


def _collect_wheels(
    wh_dir: Path, _list_wheels: Callable[[Path], list[Path]] | None
) -> list[Path]:
    if _list_wheels is not None:
        return sorted(_list_wheels(wh_dir))
    if not wh_dir.is_dir():
        raise WheelhouseVerificationError(f"wheelhouse directory missing: {wh_dir}")
    return sorted(wh_dir.glob("*.whl"))


def _verify_pinned_hashes(
    wh_dir: Path,
    lock: WheelhouseLock,
    _hash_file: Callable[[Path], str] | None,
) -> int:
    total_size = 0
    for entry in lock.wheels:
        path = wh_dir / entry.filename
        digest = _hash_file(path) if _hash_file is not None else sha256_file(path)
        if digest != entry.sha256:
            raise WheelhouseVerificationError(
                f"wheel SHA-256 mismatch: {entry.filename}: expected "
                f"{entry.sha256}, computed {digest}"
            )
        actual_size = path.stat().st_size
        if actual_size != entry.size:
            raise WheelhouseVerificationError(
                f"wheel size mismatch: {entry.filename}: expected {entry.size}, "
                f"got {actual_size}"
            )
        total_size += entry.size
    return total_size


def _check_set(present: set[str], expected: set[str], wh_dir: Path) -> None:
    missing = expected - present
    extra = present - expected
    problems: list[str] = []
    if missing:
        problems.append("missing wheels: " + ", ".join(sorted(missing)))
    if extra:
        problems.append("unexpected wheels: " + ", ".join(sorted(extra)))
    if problems:
        raise WheelhouseVerificationError(
            f"wheelhouse {wh_dir} does not match the locked wheel set: "
            + "; ".join(problems)
        )


def verify_pinned_subset(
    wheelhouse: str | os.PathLike[str],
    lock: WheelhouseLock,
    *,
    _list_wheels: Callable[[Path], list[Path]] | None = None,
    _hash_file: Callable[[Path], str] | None = None,
) -> dict:
    """Verify a download staging dir holds EXACTLY the pinned wheels."""
    wh_dir = Path(wheelhouse)
    present = {p.name for p in _collect_wheels(wh_dir, _list_wheels)}
    _check_set(present, pinned_filenames(lock), wh_dir)
    total = _verify_pinned_hashes(wh_dir, lock, _hash_file)
    return {"wheel_count": len(lock.wheels), "total_locked_bytes": total}


def verify_wheelhouse_dir(
    wheelhouse: str | os.PathLike[str],
    lock: WheelhouseLock,
    *,
    _list_wheels: Callable[[Path], list[Path]] | None = None,
    _hash_file: Callable[[Path], str] | None = None,
) -> dict:
    """Verify an on-disk wheelhouse matches the lock EXACTLY (fail closed)."""
    wh_dir = Path(wheelhouse)
    present = {p.name for p in _collect_wheels(wh_dir, _list_wheels)}
    _check_set(present, expected_filenames(lock), wh_dir)
    total = _verify_pinned_hashes(wh_dir, lock, _hash_file)
    return {
        "wheel_count": len(lock.wheels),
        "files": sorted(expected_filenames(lock)),
        "total_locked_bytes": total,
    }
