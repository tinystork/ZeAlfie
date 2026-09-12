"""Pure packaging logic for the ZeAlfie macOS ARM64 application bundle.

ZA-MAC-BOOT-01: establish a reproducible, native **arm64** unsigned
``ZeAlfie.app`` bundle that contains its own private pinned CPython and the
ZeAlfie application tree, with NO dependency on the system Python, Homebrew,
the source checkout, a runner venv, or the current working directory.

This module owns the *shape* of the bundle and the *pins* that describe the
private interpreter:

* :func:`load_record` — parse + validate ``reproducibility.toml`` and fail
  closed on any drift (wrong substrate, wrong triple, a freethreaded or
  stripped variant, a URL that does not derive from the pinned repo/tag,
  …);
* :func:`verify_archive_sha256` — fail closed BEFORE extraction;
* :func:`extract_python_tarball` — safe extraction of the archive's
  top-level ``python/`` directory into ``Contents/Resources`` (producing
  ``Contents/Resources/python``);
* the bundle layout helpers + :func:`assert_bundle_layout`;
* :func:`render_plist` — the exact ``Info.plist`` contract;
* :func:`zip_app` — produce the engineering RC artifact with a top-level
  ``ZeAlfie.app``.

It is stdlib-only and never imports ZeAlfie (the packaging layer must stay
decoupled from the product it packages).
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

__all__ = [
    "MacPackError",
    "RecordError",
    "HashMismatchError",
    "ExtractionError",
    "BundleLayoutError",
    "ReproducibilityRecord",
    "default_record_path",
    "load_record",
    "sha256_file",
    "verify_archive_sha256",
    "download_archive",
    # layout
    "APP_NAME",
    "BUNDLE_EXECUTABLE",
    "ICNS_FILENAME",
    "PLIST_FILENAME",
    "CONTENTS_MACOS",
    "CONTENTS_RESOURCES",
    "RESOURCES_PYTHON",
    "RESOURCES_APP",
    "bundle_contents",
    "bundle_macos_dir",
    "bundle_resources_dir",
    "bundle_python_dir",
    "bundle_app_dir",
    "bundle_launcher",
    "bundle_plist",
    "bundle_icns",
    "required_bundle_paths",
    "bundle_layout_problems",
    "assert_bundle_layout",
    # extraction
    "extract_python_tarball",
    "bundled_interpreter",
    # plist
    "PLIST_VALUES",
    "render_plist",
    # zip
    "zip_app",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RELEASE_TAG_RE = re.compile(r"^\d{8}$")

#: The one substrate this packager accepts.
SUBSTRATE_NAME = "python-build-standalone"
#: The one upstream repository this packager accepts (official PBS).
UPSTREAM_REPO = "astral-sh/python-build-standalone"
#: The one target triple this packager accepts (native Apple silicon).
TARGET_TRIPLE = "aarch64-apple-darwin"
#: install_only tarball filename pattern.
_ARCHIVE_FILENAME_RE = re.compile(
    r"^cpython-\d+\.\d+\.\d+\+\d{8}-[a-z0-9_.-]+-install_only\.tar\.gz$"
)
#: Top-level directory inside the install_only tarball.
_TARBALL_TOP_DIR = "python"

# ---------------------------------------------------------------------------
# Bundle layout constants (the mandatory layout contract)
# ---------------------------------------------------------------------------

APP_NAME = "ZeAlfie.app"
BUNDLE_EXECUTABLE = "ZeAlfie"
ICNS_FILENAME = "zealfie.icns"
PLIST_FILENAME = "Info.plist"

CONTENTS = "Contents"
CONTENTS_MACOS = "Contents/MacOS"
CONTENTS_RESOURCES = "Contents/Resources"
RESOURCES_PYTHON = "Contents/Resources/python"
RESOURCES_APP = "Contents/Resources/app"

#: Product version pinned into the plist (never bumped by packaging).
BUNDLE_VERSION = "0.1.1"


# ---------------------------------------------------------------------------
# Errors (typed, fail-closed)
# ---------------------------------------------------------------------------


class MacPackError(RuntimeError):
    """Base class for every fail-closed macOS packaging error."""


class RecordError(MacPackError):
    """The reproducibility record is missing, malformed, or inconsistent."""


class HashMismatchError(MacPackError):
    """Downloaded archive SHA-256 does not match the pinned record."""


class ExtractionError(MacPackError):
    """The standalone tarball is unsafe or its layout is incomplete."""


class BundleLayoutError(MacPackError):
    """The produced bundle does not match the mandatory layout."""


# ---------------------------------------------------------------------------
# Reproducibility record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReproducibilityRecord:
    """Pinned description of the exact private runtime to bundle."""

    zealfie_version: str
    zealfie_revision: str
    cpython_version: str
    substrate: str
    upstream_repo: str
    release_tag: str
    target_triple: str
    archive_filename: str
    archive_url: str
    sha256: str
    size: int
    per_user: bool = True

    @property
    def python_dir_name(self) -> str:
        return _TARBALL_TOP_DIR


def default_record_path() -> Path:
    """Path of ``reproducibility.toml`` next to this module."""
    return Path(__file__).resolve().parent / "reproducibility.toml"


def load_record(
    path: str | os.PathLike[str] | None = None,
) -> ReproducibilityRecord:
    """Load + validate the pinned reproducibility record (fail closed).

    Beyond shape validation, this is the anti-substitution gate: the
    substrate must be python-build-standalone, the upstream repo must be the
    official one, the target triple must be aarch64-apple-darwin, the
    filename must be the install_only variant (NOT freethreaded and NOT
    stripped) and both the filename and the URL must derive EXACTLY from the
    pinned version/repo/tag/triple.
    """
    record_path = Path(path) if path is not None else default_record_path()
    try:
        with open(record_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise RecordError(
            f"reproducibility record not found: {record_path}"
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RecordError(
            f"reproducibility record unreadable/invalid: {record_path}: {exc}"
        ) from exc

    def _section(name: str) -> dict:
        section = data.get(name)
        if not isinstance(section, dict):
            raise RecordError(f"reproducibility record missing section [{name}]")
        return section

    def _text(section: dict, key: str) -> str:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RecordError(
                f"reproducibility record field {key!r} missing or empty"
            )
        return value.strip()

    zealfie = _section("zealfie")
    cpython = _section("cpython")
    install = _section("install")

    sha256 = _text(cpython, "sha256").lower()
    if not _SHA256_RE.match(sha256):
        raise RecordError(
            "reproducibility record sha256 is not a 64-char lowercase hex "
            f"digest: {sha256!r}"
        )

    version = _text(cpython, "version")
    substrate = _text(cpython, "substrate")
    if substrate != SUBSTRATE_NAME:
        raise RecordError(
            f"reproducibility record substrate must be {SUBSTRATE_NAME!r}, "
            f"got {substrate!r}"
        )
    repo = _text(cpython, "upstream_repo")
    if not _REPO_RE.match(repo):
        raise RecordError(
            f"reproducibility record upstream_repo is not an owner/repo pair: "
            f"{repo!r}"
        )
    if repo != UPSTREAM_REPO:
        raise RecordError(
            f"reproducibility record upstream_repo must be {UPSTREAM_REPO!r} "
            f"(the official python-build-standalone repository), got {repo!r}"
        )
    release_tag = _text(cpython, "release_tag")
    if not _RELEASE_TAG_RE.match(release_tag):
        raise RecordError(
            "reproducibility record release_tag must be a YYYYMMDD tag, got "
            f"{release_tag!r}"
        )
    target_triple = _text(cpython, "target_triple")
    if target_triple != TARGET_TRIPLE:
        raise RecordError(
            f"reproducibility record target_triple must be {TARGET_TRIPLE!r}, "
            f"got {target_triple!r}"
        )

    filename = _text(cpython, "archive_filename")
    if not _ARCHIVE_FILENAME_RE.match(filename):
        raise RecordError(
            "reproducibility record archive_filename does not match the "
            "python-build-standalone install_only pattern "
            f"(cpython-<v>+<YYYYMMDD>-<triple>-install_only.tar.gz): {filename!r}"
        )
    if "freethreaded" in filename or "stripped" in filename:
        raise RecordError(
            "reproducibility record archive_filename must be the plain "
            f"install_only variant (not freethreaded/stripped): {filename!r}"
        )
    expected_filename = (
        f"cpython-{version}+{release_tag}-{target_triple}-install_only.tar.gz"
    )
    if filename != expected_filename:
        raise RecordError(
            "reproducibility record archive_filename is inconsistent with the "
            f"pinned version/release_tag/target_triple: expected "
            f"{expected_filename!r}, got {filename!r}"
        )

    url = _text(cpython, "archive_url")
    expected_url = (
        f"https://github.com/{repo}/releases/download/{release_tag}/"
        f"{quote(filename, safe='')}"
    )
    if url != expected_url:
        raise RecordError(
            "reproducibility record archive_url is inconsistent with the "
            f"pinned repo/tag/filename: expected {expected_url!r}, got {url!r}"
        )

    size = cpython.get("size")
    if not isinstance(size, int) or size <= 0:
        raise RecordError(
            f"reproducibility record cpython.size must be a positive integer, "
            f"got {size!r}"
        )

    revision = _text(zealfie, "revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision.lower()):
        raise RecordError(
            "reproducibility record zealfie.revision must be a full 40-char "
            f"git commit SHA: {revision!r}"
        )

    per_user = install.get("per_user", True)
    if not isinstance(per_user, bool):
        raise RecordError("reproducibility record install.per_user must be a boolean")
    if not per_user:
        raise RecordError(
            "reproducibility record requests a non-per-user install; the "
            "bundle is per-user only"
        )

    return ReproducibilityRecord(
        zealfie_version=_text(zealfie, "version"),
        zealfie_revision=revision.lower(),
        cpython_version=version,
        substrate=substrate,
        upstream_repo=repo,
        release_tag=release_tag,
        target_triple=target_triple,
        archive_filename=filename,
        archive_url=url,
        sha256=sha256,
        size=size,
        per_user=per_user,
    )


# ---------------------------------------------------------------------------
# SHA-256 verification + acquisition (fail closed)
# ---------------------------------------------------------------------------


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 65536) -> str:
    """Compute the lowercase hex SHA-256 of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive_sha256(
    archive_path: str | os.PathLike[str],
    expected_sha256: str | None = None,
    record: ReproducibilityRecord | None = None,
) -> str:
    """Verify a downloaded substrate archive against the pinned digest.

    Returns the computed digest on match and raises
    :class:`HashMismatchError` otherwise — extraction never proceeds on a
    mismatch.
    """
    if record is not None:
        expected_sha256 = record.sha256
    if not expected_sha256 or not _SHA256_RE.match(expected_sha256):
        raise RecordError(
            "verify_archive_sha256 requires a 64-char lowercase hex expected "
            f"digest, got {expected_sha256!r}"
        )
    actual = sha256_file(archive_path)
    if actual != expected_sha256:
        raise HashMismatchError(
            f"archive SHA-256 mismatch: expected {expected_sha256}, "
            f"computed {actual}"
        )
    return actual


def download_archive(
    record: ReproducibilityRecord,
    destination: str | os.PathLike[str],
    *,
    _urlopen: Callable | None = None,
) -> Path:
    """Download the pinned archive and verify it (fail closed).

    The URL is taken from the validated record, never constructed ad hoc.
    The downloaded file's SHA-256 must match the pin, and so must its size.
    """
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _urlopen is None:
        from urllib.request import urlopen as _urlopen  # local import: no net at import time

    with _urlopen(record.archive_url) as response, open(dest, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    actual_size = dest.stat().st_size
    if actual_size != record.size:
        raise HashMismatchError(
            f"archive size mismatch: expected {record.size}, got {actual_size}"
        )
    verify_archive_sha256(dest, record=record)
    return dest


# ---------------------------------------------------------------------------
# Bundle layout helpers (pure)
# ---------------------------------------------------------------------------


def bundle_contents(app: str | os.PathLike[str]) -> Path:
    return Path(app) / CONTENTS


def bundle_macos_dir(app: str | os.PathLike[str]) -> Path:
    return Path(app) / CONTENTS_MACOS


def bundle_resources_dir(app: str | os.PathLike[str]) -> Path:
    return Path(app) / CONTENTS_RESOURCES


def bundle_python_dir(app: str | os.PathLike[str]) -> Path:
    return Path(app) / RESOURCES_PYTHON


def bundle_app_dir(app: str | os.PathLike[str]) -> Path:
    return Path(app) / RESOURCES_APP


def bundle_launcher(app: str | os.PathLike[str]) -> Path:
    return Path(app) / CONTENTS_MACOS / BUNDLE_EXECUTABLE


def bundle_plist(app: str | os.PathLike[str]) -> Path:
    return Path(app) / CONTENTS / PLIST_FILENAME


def bundle_icns(app: str | os.PathLike[str]) -> Path:
    return Path(app) / CONTENTS_RESOURCES / ICNS_FILENAME


def bundled_interpreter(app: str | os.PathLike[str]) -> Path:
    """Absolute path of the private interpreter inside the bundle."""
    return bundle_python_dir(app) / "bin" / "python3.13"


def required_bundle_paths(app: str | os.PathLike[str]) -> tuple[Path, ...]:
    """The mandatory layout, exactly as the mission specifies."""
    return (
        bundle_plist(app),
        bundle_launcher(app),
        bundle_python_dir(app),
        bundle_app_dir(app),
        bundle_icns(app),
    )


def bundle_layout_problems(
    app: str | os.PathLike[str],
    *,
    _exists: Callable[[Path], bool] | None = None,
) -> list[str]:
    """Return the mandatory layout entries that are missing (empty is good)."""
    exists = _exists if _exists is not None else (lambda p: p.exists())
    problems: list[str] = []
    for required in required_bundle_paths(app):
        if not exists(required):
            problems.append(str(required))
    interpreter = bundled_interpreter(app)
    if not exists(interpreter):
        problems.append(str(interpreter))
    return problems


def assert_bundle_layout(app: str | os.PathLike[str]) -> None:
    """Fail closed unless the mandatory bundle layout is complete."""
    problems = bundle_layout_problems(app)
    if problems:
        raise BundleLayoutError(
            "bundle is missing mandatory layout entries: " + ", ".join(problems)
        )


# ---------------------------------------------------------------------------
# Safe extraction of the pinned install_only tarball
# ---------------------------------------------------------------------------


def _members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Return the safe ``python/`` members of *tar* (fail closed)."""
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        name = member.name
        if name != _TARBALL_TOP_DIR and not name.startswith(_TARBALL_TOP_DIR + "/"):
            raise ExtractionError(
                "standalone tarball has an unexpected top-level member: "
                f"{name!r} (expected only {_TARBALL_TOP_DIR}/)"
            )
        norm = name.replace("\\", "/")
        parts = norm.split("/")
        if parts[0] != _TARBALL_TOP_DIR or any(
            part in ("", ".", "..") for part in parts[1:]
        ):
            raise ExtractionError(f"standalone tarball member is unsafe: {name!r}")
        members.append(member)
    return members


def extract_python_tarball(
    archive_path: str | os.PathLike[str],
    resources_dir: str | os.PathLike[str],
) -> Path:
    """Extract the pinned install_only tarball into *resources_dir*.

    Produces ``<resources_dir>/python`` (i.e. ``Contents/Resources/python``
    for a bundle).  Fail closed when:

    * the archive is absent or is not the expected ``python/`` layout;
    * the extracted private interpreter ``bin/python3.13`` is missing;
    * that interpreter's Mach-O header is not thin arm64.

    Returns the extracted ``python`` directory.
    """
    archive = Path(archive_path)
    if not archive.is_file():
        raise ExtractionError(f"standalone archive missing: {archive}")
    dest = Path(resources_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = _members(tar)
            if not members:
                raise ExtractionError(f"standalone archive {archive.name} is empty")
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=dest, members=members, filter="data")
            else:  # pragma: no cover - Python < 3.12 fallback
                tar.extractall(path=dest, members=members)
    except tarfile.TarError as exc:
        raise ExtractionError(f"standalone archive extraction failed: {exc}") from exc

    python_dir = dest / _TARBALL_TOP_DIR
    interpreter = python_dir / "bin" / "python3.13"
    if not interpreter.is_file():
        raise ExtractionError(
            f"standalone tarball does not contain {_TARBALL_TOP_DIR}/bin/"
            "python3.13 after extraction"
        )
    _assert_arm64_thin(interpreter)
    return python_dir


def _assert_arm64_thin(interpreter: Path) -> None:
    """Fail closed unless *interpreter* is a thin arm64 Mach-O file."""
    # Local import so macpack stays importable without the sibling module on
    # sys.path until extraction actually needs to inspect a binary.
    import macho  # type: ignore[import-not-found]

    info = macho.read_macho_info(interpreter)
    if not info.is_macho:
        raise ExtractionError(
            f"private interpreter {interpreter} is not a Mach-O file"
        )
    if info.is_fat:
        raise ExtractionError(
            f"private interpreter {interpreter} is a universal binary "
            f"(archs={info.archs}); the pinned install_only archive must be "
            "a native thin arm64 build"
        )
    if info.archs != (macho.ARCH_ARM64,):
        raise ExtractionError(
            f"private interpreter {interpreter} is not thin arm64 "
            f"(archs={info.archs})"
        )


def verify_interpreter_is_bundled_python(
    interpreter: str | os.PathLike[str],
) -> None:
    """Read the Mach-O CPU type of *interpreter* and require arm64."""
    path = Path(interpreter)
    with open(path, "rb") as fh:
        header = fh.read(8)
    if len(header) < 8 or header[:4] not in (
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
    ):
        raise ExtractionError(f"{path} is not a 64-bit Mach-O file")
    little = header[:4] == b"\xcf\xfa\xed\xfe"
    cputype = struct.unpack("<I" if little else ">I", header[4:8])[0]
    if cputype != 0x0100000C:
        raise ExtractionError(
            f"{path} is not an arm64 Mach-O file (cputype=0x{cputype:08x})"
        )


# ---------------------------------------------------------------------------
# Info.plist contract
# ---------------------------------------------------------------------------

#: The exact, ordered Info.plist contract (mission-pinned; do not drift).
PLIST_VALUES: tuple[tuple[str, str | bool], ...] = (
    ("CFBundleIdentifier", "com.zesoftware.zealfie"),
    ("CFBundleExecutable", BUNDLE_EXECUTABLE),
    ("CFBundleDisplayName", "ZeAlfie"),
    ("CFBundleShortVersionString", BUNDLE_VERSION),
    ("CFBundleVersion", BUNDLE_VERSION),
    ("CFBundleIconFile", ICNS_FILENAME),
    ("CFBundlePackageType", "APPL"),
    ("LSMinimumSystemVersion", "13.0"),
    ("NSHighResolutionCapable", True),
)


def _plist_value(key: str, value: str | bool) -> str:
    if isinstance(value, bool):
        return "  <key>%s</key>\n  <true/>" % key
    return "  <key>%s</key>\n  <string>%s</string>" % (key, value)


def render_plist(
    values: Iterable[tuple[str, str | bool]] | None = None,
) -> str:
    """Render the bundle ``Info.plist`` (XML) from the exact contract."""
    entries = tuple(values) if values is not None else PLIST_VALUES
    keys = [key for key, _ in entries]
    expected_keys = [key for key, _ in PLIST_VALUES]
    if keys != expected_keys:
        raise MacPackError(
            "Info.plist keys must be exactly "
            f"{expected_keys}, got {keys}"
        )
    body = "\n".join(_plist_value(key, value) for key, value in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"{body}\n"
        "</dict>\n"
        "</plist>\n"
    )


# ---------------------------------------------------------------------------
# Artifact archive
# ---------------------------------------------------------------------------


def zip_app(
    app: str | os.PathLike[str],
    zip_path: str | os.PathLike[str],
    *,
    executable_relative_paths: Iterable[str] = (CONTENTS_MACOS + "/" + BUNDLE_EXECUTABLE,),
) -> dict:
    """Zip an ``.app`` bundle with ``ZeAlfie.app`` as the TOP-LEVEL entry.

    Zip perm bits are preserved for the launcher (and anything under
    ``Contents/MacOS``) so the extracted bundle is directly runnable.
    Returns ``{"name", "path", "size", "sha256", "entries"}``.
    """
    app_path = Path(app)
    if app_path.name != APP_NAME:
        raise MacPackError(
            f"bundle directory must be named {APP_NAME!r}, got {app_path.name!r}"
        )
    if not app_path.is_dir():
        raise MacPackError(f"bundle directory missing: {app_path}")
    out = Path(zip_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    executable_set = set(executable_relative_paths)
    entries = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(app_path, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                full = Path(dirpath) / name
                rel = full.relative_to(app_path)
                arcname = f"{APP_NAME}/{rel.as_posix()}"
                is_exec = (
                    rel.as_posix() in executable_set
                    or os.access(full, os.X_OK)
                )
                info = zipfile.ZipInfo(arcname)
                # Deterministic timestamp (the zip is an engineering RC,
                # not a byte-reproducible release artefact).
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = full.stat().st_mode & 0o777
                if is_exec:
                    mode |= 0o111
                info.external_attr = (mode & 0xFFFF) << 16
                with open(full, "rb") as src, zf.open(info, "w") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                entries += 1
    return {
        "name": out.name,
        "path": str(out),
        "size": out.stat().st_size,
        "sha256": sha256_file(out),
        "entries": entries,
    }
