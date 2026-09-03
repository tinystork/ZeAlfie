"""Pure provisioning logic for the ZeAlfie Windows standalone bootstrap.

ZA-WIN-BOOT-01 + ZA-WIN-BOOT-03B: establish the Windows proof that ZeAlfie
runs as a standalone application on a **private, pinned CPython 3.13**
runtime with NO dependency on the GitHub runner's preinstalled Python once
provisioning has completed.  Since ZA-WIN-BOOT-03B the substrate is the
**python-build-standalone** (astral-sh) ``install_only`` tarball — plain
relocatable CPython files — instead of the python.org executable installer
(whose Burn MajorUpgrade semantics broke same-minor host isolation).

This module is deliberately:

* **stdlib-only** — no ZeAlfie import (so it can never be entangled with
  the shared runtime), no third-party import (``tomllib``, ``tarfile``,
  ``urllib.parse`` are stdlib on Python 3.11+);
* **pure where it matters** — record validation, argv construction and
  layout/provenance assertions are pure transformations with injectable
  seams; the only I/O is reading the reproducibility record
  (``load_record``) and the fail-closed tarball digest + safe extraction
  (``verify_archive_sha256`` / ``extract_python_tarball``);
* **hermetically testable on Linux** — Windows path semantics are handled
  with :mod:`ntpath`, which behaves identically on every platform, so the
  Windows witness logic is unit-tested without a real private Python and
  without Windows.

Layout produced (conceptual, under a per-user CI witness root)::

    <root>/python/    private pinned CPython 3.13 (extracted standalone
                      runtime: python.exe + pythonw.exe + Lib/ + pip)
    <root>/appenv/    dedicated venv containing the installed ZeAlfie

The existing shared runtime (``%LOCALAPPDATA%\\zealfie\\runtime`` with
slots/state/cache) is a SEPARATE concern: nothing here reads, writes, or
imports it.

Fail-closed posture: every assertion raises a typed exception; there is
never a silent fallback to a runner/system Python.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

__all__ = [
    "WindowsBootstrapError",
    "RecordError",
    "HashMismatchError",
    "ExtractionError",
    "ProvenanceError",
    "RunnerPythonError",
    "ReproducibilityRecord",
    "default_record_path",
    "load_record",
    "sha256_file",
    "verify_archive_sha256",
    "private_python_dir",
    "private_python_exe",
    "private_pythonw_exe",
    "private_python_lib",
    "missing_private_python_files",
    "extract_python_tarball",
    "appenv_dir",
    "appenv_python_exe",
    "appenv_scripts_dir",
    "venv_create_argv",
    "pip_install_wheel_argv",
    "pip_install_wheel_offline_argv",
    "appenv_launcher_names",
    "missing_appenv_launchers",
    "interpreter_probe_argv",
    "parse_pyvenv_cfg",
    "pyvenv_cfg_home",
    "forbidden_python_roots",
    "detect_runner_python_violations",
    "assert_no_runner_python",
    "assert_appenv_provenance",
    "assert_child_venv_provenance",
    "normalise_path",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: python-build-standalone install_only tarball of a single release:
#: cpython-<version>+<YYYYMMDD>-<triple>-install_only.tar.gz
_PBS_FILENAME_RE = re.compile(
    r"^cpython-\d+\.\d+\.\d+\+\d{8}-[a-z0-9_.-]+-install_only\.tar\.gz$"
)
_RELEASE_TAG_RE = re.compile(r"^\d{8}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SUBSTRATE_NAME = "python-build-standalone"
#: Canonical top-level directory inside the install_only tarball.
_TARBALL_TOP_DIR = "python"


# ---------------------------------------------------------------------------
# Errors (typed, fail-closed)
# ---------------------------------------------------------------------------


class WindowsBootstrapError(RuntimeError):
    """Base class for every fail-closed bootstrap/provenance error."""


class RecordError(WindowsBootstrapError):
    """The reproducibility record is missing, malformed, or inconsistent."""


class HashMismatchError(WindowsBootstrapError):
    """Downloaded archive SHA-256 does not match the pinned record."""


class ExtractionError(WindowsBootstrapError):
    """The standalone tarball is unsafe or its layout is incomplete."""


class ProvenanceError(WindowsBootstrapError):
    """An interpreter's path provenance does not match the private layout."""


class RunnerPythonError(ProvenanceError):
    """An interpreter resolves to a GitHub-runner/system preinstalled Python."""


# ---------------------------------------------------------------------------
# Reproducibility record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReproducibilityRecord:
    """Pinned record describing the exact private runtime to provision.

    Since ZA-WIN-BOOT-03B the substrate is a python-build-standalone
    ``install_only`` tarball (plain relocatable CPython files) rather than
    the python.org executable installer — there is therefore no installer
    property/switch surface; the archive is verified and extracted.
    """

    zealfie_version: str
    zealfie_revision: str
    cpython_version: str
    substrate: str
    upstream_repo: str
    release_tag: str
    target_triple: str
    installer_filename: str
    installer_url: str
    sha256: str
    size: int
    per_user: bool = True
    silent: bool = True

    @property
    def python_dir_name(self) -> str:
        """Directory name of the private CPython tree under a witness root."""
        return "python"

    @property
    def appenv_dir_name(self) -> str:
        """Directory name of the dedicated ZeAlfie venv under a witness root."""
        return "appenv"


def default_record_path() -> Path:
    """Path of ``reproducibility.toml`` next to this module."""
    return Path(__file__).resolve().parent / "reproducibility.toml"


def load_record(path: str | os.PathLike[str] | None = None) -> ReproducibilityRecord:
    """Load and validate the pinned reproducibility record (fail closed).

    Raises :class:`RecordError` when the file is unreadable or any pinned
    field is missing/inconsistent.  Field names follow the TOML sections
    ``[zealfie]``, ``[cpython]``, ``[install]``.

    Fail-closed consistency contract:

    * ``sha256`` is a 64-char lowercase hex digest;
    * the substrate fields describe a python-build-standalone install_only
      tarball whose FILENAME derives exactly from ``version`` +
      ``release_tag`` + ``target_triple`` and whose URL derives exactly
      from ``upstream_repo`` + ``release_tag`` + the (URL-quoted)
      filename — any drift fails the record;
    * ``[install]`` stays per-user/silent (booleans, never strings).
    """
    record_path = Path(path) if path is not None else default_record_path()
    try:
        with open(record_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise RecordError(f"reproducibility record not found: {record_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RecordError(
            f"reproducibility record unreadable/invalid: {record_path}: {exc}"
        ) from exc

    def _section(name: str) -> dict:
        section = data.get(name)
        if not isinstance(section, dict):
            raise RecordError(
                f"reproducibility record missing section [{name}]"
            )
        return section

    zealfie = _section("zealfie")
    cpython = _section("cpython")
    install = _section("install")

    def _text(section: dict, key: str) -> str:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RecordError(
                f"reproducibility record field {key!r} missing or empty"
            )
        return value.strip()

    sha256 = _text(cpython, "sha256").lower()
    if not _SHA256_RE.match(sha256):
        raise RecordError(
            "reproducibility record sha256 is not a 64-char lowercase hex "
            f"digest: {sha256!r}"
        )

    version = _text(cpython, "version")
    substrate = _text(cpython, "substrate")
    if substrate != _SUBSTRATE_NAME:
        raise RecordError(
            "reproducibility record substrate must be "
            f"{_SUBSTRATE_NAME!r}, got {substrate!r}"
        )
    repo = _text(cpython, "upstream_repo")
    if not _REPO_RE.match(repo):
        raise RecordError(
            "reproducibility record upstream_repo is not an owner/repo "
            f"pair: {repo!r}"
        )
    release_tag = _text(cpython, "release_tag")
    if not _RELEASE_TAG_RE.match(release_tag):
        raise RecordError(
            "reproducibility record release_tag must be a YYYYMMDD tag, "
            f"got {release_tag!r}"
        )
    target_triple = _text(cpython, "target_triple")
    if not re.fullmatch(r"[a-z0-9_.-]+", target_triple):
        raise RecordError(
            "reproducibility record target_triple has an invalid shape: "
            f"{target_triple!r}"
        )

    filename = _text(cpython, "installer_filename")
    if not _PBS_FILENAME_RE.match(filename):
        raise RecordError(
            "reproducibility record installer_filename does not match the "
            "python-build-standalone install_only pattern "
            f"(cpython-<v>+<YYYYMMDD>-<triple>-install_only.tar.gz): {filename!r}"
        )
    expected_filename = (
        f"cpython-{version}+{release_tag}-{target_triple}-install_only.tar.gz"
    )
    if filename != expected_filename:
        raise RecordError(
            "reproducibility record installer_filename is inconsistent with "
            "the pinned version/release_tag/target_triple: expected "
            f"{expected_filename!r}, got {filename!r}"
        )

    url = _text(cpython, "installer_url")
    expected_url = (
        f"https://github.com/{repo}/releases/download/"
        f"{release_tag}/{quote(filename, safe='')}"
    )
    if url != expected_url:
        raise RecordError(
            "reproducibility record installer_url is inconsistent with the "
            "pinned repo/tag/filename: expected "
            f"{expected_url!r}, got {url!r}"
        )

    size = cpython.get("size")
    if not isinstance(size, int) or size <= 0:
        raise RecordError(
            "reproducibility record cpython.size must be a positive integer, "
            f"got {size!r}"
        )

    revision = _text(zealfie, "revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision.lower()):
        raise RecordError(
            "reproducibility record zealfie.revision must be a full 40-char "
            f"git commit SHA: {revision!r}"
        )

    def _flag(section: dict, key: str, default: bool) -> bool:
        value = section.get(key, default)
        if not isinstance(value, bool):
            raise RecordError(
                f"reproducibility record field {key!r} must be a boolean"
            )
        return value

    per_user = _flag(install, "per_user", True)
    silent = _flag(install, "silent", True)
    if not per_user:
        raise RecordError(
            "reproducibility record requests a non-per-user install; the "
            "bootstrap is per-user only"
        )

    return ReproducibilityRecord(
        zealfie_version=_text(zealfie, "version"),
        zealfie_revision=revision.lower(),
        cpython_version=version,
        substrate=substrate,
        upstream_repo=repo,
        release_tag=release_tag,
        target_triple=target_triple,
        installer_filename=filename,
        installer_url=url,
        sha256=sha256,
        size=size,
        per_user=per_user,
        silent=silent,
    )


# ---------------------------------------------------------------------------
# SHA-256 verification (fail closed)
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

    Either ``expected_sha256`` or ``record`` must be supplied (record wins
    when both are given).  Returns the computed digest on match and raises
    :class:`HashMismatchError` otherwise — provisioning never proceeds on a
    hash mismatch.
    """
    if record is not None:
        expected_sha256 = record.sha256
    if not expected_sha256 or not _SHA256_RE.match(expected_sha256):
        raise RecordError(
            "verify_archive_sha256 requires a 64-char lowercase hex "
            f"expected digest, got {expected_sha256!r}"
        )
    actual = sha256_file(archive_path)
    if actual != expected_sha256:
        raise HashMismatchError(
            f"archive SHA-256 mismatch: expected {expected_sha256}, "
            f"computed {actual}"
        )
    return actual


# ---------------------------------------------------------------------------
# Private layout path helpers (pure)
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


def normalise_path(path: str | os.PathLike[str]) -> str:
    """Platform-normalise a path for provenance comparison.

    On Windows this lowercases and normalises separators via
    :mod:`ntpath`; elsewhere it uses :mod:`os.path` (identity on POSIX).
    """
    raw = os.fspath(path)
    if _IS_WINDOWS:
        return ntpath.normcase(ntpath.normpath(raw))
    return os.path.normcase(os.path.normpath(raw))


def _nt_normalise(path: str | os.PathLike[str]) -> str:
    """Normalise a *Windows* path regardless of the host platform."""
    return ntpath.normcase(ntpath.normpath(os.fspath(path)))


def private_python_dir(witness_root: str | os.PathLike[str]) -> Path:
    """``<witness-root>/python`` — the extracted private CPython tree."""
    return Path(witness_root) / "python"


def private_python_exe(witness_root: str | os.PathLike[str]) -> Path:
    """``<witness-root>/python/python.exe`` — expected console interpreter."""
    return private_python_dir(witness_root) / "python.exe"


def private_pythonw_exe(witness_root: str | os.PathLike[str]) -> Path:
    """``<witness-root>/python/pythonw.exe`` — expected windowed interpreter
    (hard functional requirement of the windowed GUI launcher)."""
    return private_python_dir(witness_root) / "pythonw.exe"


def private_python_lib(witness_root: str | os.PathLike[str]) -> Path:
    """``<witness-root>/python/Lib`` — the extracted stdlib directory."""
    return private_python_dir(witness_root) / "Lib"


#: Files/dirs that define a COMPLETE private standalone python tree: both
#: interpreters (console + windowed) and the stdlib ``Lib`` directory.
_PRIVATE_PYTHON_PARTS: tuple[str, ...] = ("python.exe", "pythonw.exe", "Lib")


def missing_private_python_files(
    witness_root: str | os.PathLike[str],
    *,
    _exists: Callable[[Path], bool] | None = None,
) -> list[str]:
    """Return the private-runtime parts that are MISSING under
    ``<root>\\python`` (``python.exe``, ``pythonw.exe``, ``Lib``).

    An empty list means the extracted standalone runtime is complete.
    ``_exists`` is an injectable seam for hermetic tests; by default the
    real filesystem is consulted.
    """
    private_dir = private_python_dir(witness_root)
    missing: list[str] = []
    for name in _PRIVATE_PYTHON_PARTS:
        path = private_dir / name
        exists = _exists(path) if _exists is not None else path.exists()
        if not exists:
            missing.append(ntpath.join("python", name))
    return missing


def appenv_dir(witness_root: str | os.PathLike[str]) -> Path:
    """``<witness-root>/appenv`` — the dedicated ZeAlfie venv directory."""
    return Path(witness_root) / "appenv"


def appenv_scripts_dir(witness_root: str | os.PathLike[str]) -> Path:
    """Scripts directory of the appenv venv (Windows layout)."""
    return appenv_dir(witness_root) / "Scripts"


def appenv_python_exe(witness_root: str | os.PathLike[str]) -> Path:
    """``<witness-root>/appenv/Scripts/python.exe`` — the appenv interpreter."""
    return appenv_scripts_dir(witness_root) / "python.exe"


# ---------------------------------------------------------------------------
# Command builders (pure argv, executed by the thin Windows entrypoint)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Standalone tarball extraction (stdlib tarfile, fail closed)
# ---------------------------------------------------------------------------


def _is_safe_tarball_member(name: str) -> bool:
    """True when *name* is a safe member inside the archive's ``python/``
    top-level directory.

    Fail-closed rules: the member must be the top directory ``python`` or
    live strictly under it; absolute paths, Windows-style separators and
    any ``..`` component are rejected (defence in depth — the payload is
    SHA-256-pinned, but extraction must never escape the destination).
    """
    norm = name.replace("\\", "/")
    parts = norm.split("/")
    if not parts or parts[0] != _TARBALL_TOP_DIR:
        return False
    return all(part not in ("", ".", "..") for part in parts[1:])


def _filtered_tar_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Return the safe ``python/`` members of *tar* (fail closed)."""
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        if member.name != _TARBALL_TOP_DIR and not member.name.startswith(
            _TARBALL_TOP_DIR + "/"
        ):
            raise ExtractionError(
                "standalone tarball has an unexpected top-level member: "
                f"{member.name!r} (expected only {_TARBALL_TOP_DIR}/)"
            )
        if not _is_safe_tarball_member(member.name):
            raise ExtractionError(
                f"standalone tarball member is unsafe: {member.name!r}"
            )
        members.append(member)
    return members


def extract_python_tarball(
    archive_path: str | os.PathLike[str],
    dest_root: str | os.PathLike[str],
) -> Path:
    """Extract the pinned install_only tarball under *dest_root*.

    Produces ``<dest_root>/python/`` containing at least ``python.exe``,
    ``pythonw.exe`` and ``Lib/`` (the full standalone runtime).  Fail
    closed:

    * only members inside the archive's top-level ``python/`` directory are
      extracted (no absolute path / ``..`` escape);
    * after extraction the COMPLETE private layout must exist — both
      interpreters and the stdlib — otherwise :class:`ExtractionError`.

    Returns the extracted ``python/`` directory.  Extraction happens at CI
    BUILD time on the driver python; the end-user Setup never extracts
    anything (Inno embeds these already-extracted files).
    """
    archive_path = Path(archive_path)
    dest_root = Path(dest_root)
    if not archive_path.is_file():
        raise ExtractionError(f"standalone archive missing: {archive_path}")
    dest_root.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = _filtered_tar_members(tar)
            if not members:
                raise ExtractionError(
                    f"standalone archive {archive_path.name} is empty"
                )
            # Belt-and-braces on top of the explicit member whitelist: the
            # data filter (3.12+) also strips modes/ownership that could be
            # hostile on unpack; on 3.11 the whitelist alone protects us.
            if sys.version_info >= (3, 12):
                tar.extractall(path=dest_root, members=members,
                               filter="data")
            else:
                tar.extractall(path=dest_root, members=members)
    except tarfile.TarError as exc:
        raise ExtractionError(
            f"standalone archive extraction failed: {exc}"
        ) from exc

    private_dir = dest_root / _TARBALL_TOP_DIR
    missing = missing_private_python_files(dest_root)
    if missing:
        raise ExtractionError(
            "standalone tarball does not contain the complete private "
            "runtime (python.exe + pythonw.exe + Lib) after extraction — "
            "missing: " + ", ".join(missing)
        )
    return private_dir


def venv_create_argv(
    python_exe: str | os.PathLike[str],
    venv_directory: str | os.PathLike[str],
    *,
    with_pip: bool = True,
) -> list[str]:
    """Build ``python -m venv <dir>`` — the appenv/child-venv mechanism.

    Mirrors the shared runtime's child-venv mechanism
    (``venv.create(path, with_pip=True, clear=False)`` in
    ``src/zealfie/runtime/deployment.py``) at the subprocess level: the
    private python's own ``venv`` module creates the environment, so pip
    and the interpreter provenance always derive from the private CPython.
    """
    argv = [str(python_exe), "-m", "venv"]
    if not with_pip:
        argv.append("--without-pip")
    argv.append(str(venv_directory))
    return argv


def pip_install_wheel_argv(
    venv_python: str | os.PathLike[str],
    wheel_path: str | os.PathLike[str],
) -> list[str]:
    """Build the appenv pip command that installs the built ZeAlfie wheel.

    Dependencies are resolved from PyPI (the wheel depends on PySide6 and
    friends).  ``--no-cache-dir`` keeps the witness root self-contained.
    """
    return [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        str(wheel_path),
    ]


def pip_install_wheel_offline_argv(
    venv_python: str | os.PathLike[str],
    wheel_path: str | os.PathLike[str],
    wheelhouse_dir: str | os.PathLike[str],
) -> list[str]:
    """Build the OFFLINE appenv pip command (ZA-WIN-BOOT-02).

    Equivalent to the installer contract::

        <appenv python> -m pip install \\
            --no-index --find-links <bundled-wheelhouse> <zealfie wheel>

    ``--no-index`` forbids PyPI entirely and ``--find-links`` restricts the
    only allowed source to the bundled (SHA-256-verified) wheelhouse, so the
    user installation is fully offline — no network is ever touched and a
    missing wheel is a hard failure, never a silent index fallback.  The
    extra flags mirror :func:`pip_install_wheel_argv` (deterministic pip,
    self-contained install).
    """
    return [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        str(wheelhouse_dir),
        str(wheel_path),
    ]


#: The four launchers whose presence defines a COMPLETE appenv for the
#: standalone installer contract (ZA-WIN-BOOT-02): the console and windowed
#: interpreters plus the console and windowed ZeAlfie entry points.
_APPENV_LAUNCHER_NAMES: tuple[str, ...] = (
    "python.exe",
    "pythonw.exe",
    "zealfie.exe",
    "zealfie-gui.exe",
)


def appenv_launcher_names() -> tuple[str, ...]:
    """Names of the four launchers a complete Windows appenv must contain."""
    return _APPENV_LAUNCHER_NAMES


def missing_appenv_launchers(
    witness_root: str | os.PathLike[str],
    *,
    _exists: Callable[[Path], bool] | None = None,
) -> list[str]:
    """Return the appenv ``Scripts`` launchers that are MISSING.

    Checks the four installer-contract launchers under the Windows venv
    layout ``<root>\\appenv\\Scripts\\.``  An empty list means the appenv
    is complete.  ``_exists`` is an injectable seam for hermetic tests;
    by default the real filesystem is consulted (Windows-only layout — on a
    POSIX host this always reports missing, which is correct because a POSIX
    venv has no ``Scripts`` directory and is never a Windows appenv).
    """
    scripts = appenv_scripts_dir(witness_root)
    missing: list[str] = []
    for name in _APPENV_LAUNCHER_NAMES:
        path = scripts / name
        exists = _exists(path) if _exists is not None else path.is_file()
        if not exists:
            missing.append(ntpath.join("Scripts", name))
    return missing


def interpreter_probe_argv(python_exe: str | os.PathLike[str]) -> list[str]:
    """Build argv that prints JSON interpreter provenance from *python_exe*."""
    probe = (
        "import json,sys;"
        "print(json.dumps({"
        "'executable': sys.executable,"
        "'prefix': sys.prefix,"
        "'base_prefix': sys.base_prefix,"
        "'base_executable': getattr(sys, '_base_executable', None),"
        "'version': sys.version.split()[0]}))"
    )
    return [str(python_exe), "-c", probe]


# ---------------------------------------------------------------------------
# pyvenv.cfg parsing (pure)
# ---------------------------------------------------------------------------


def parse_pyvenv_cfg(text: str) -> dict[str, str]:
    """Parse a ``pyvenv.cfg`` body into a ``{key: value}`` mapping.

    Accepts the real-world superset (``key = value`` lines and bare flag
    lines such as ``include-system-site-packages``) and is robust to both
    CRLF and LF line endings.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
        else:
            result[line] = ""
    return result


def pyvenv_cfg_home(
    venv_directory: str | os.PathLike[str], *, _exists: Callable[[Path], bool] | None = None
) -> str | None:
    """Return the ``home`` value of a venv's ``pyvenv.cfg`` (or ``None``).

    ``_exists`` is an injectable seam for hermetic tests; by default the
    file must exist on disk.
    """
    cfg = Path(venv_directory) / "pyvenv.cfg"
    exists = _exists(cfg) if _exists is not None else cfg.is_file()
    if not exists:
        return None
    with open(cfg, "r", encoding="utf-8") as fh:
        return parse_pyvenv_cfg(fh.read()).get("home")


# ---------------------------------------------------------------------------
# Runner/system Python detection and rejection (pure, ntpath-based)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ForbiddenRule:
    """One forbidden interpreter-root rule.

    ``nt_root`` is the nt-normalised root directory.  When
    ``component_prefix`` is ``None`` the rule forbids the root itself and
    everything under it (exact containment).  When set (e.g. ``Python``
    for ``C:\\Program Files``), the rule forbids any path under ``nt_root``
    whose FIRST descendant component starts with ``component_prefix`` —
    this expresses the wildcard family ``C:\\Program Files\\Python*``
    without any glob/version-string comparison.
    """

    nt_root: str
    component_prefix: str | None = None

    @property
    def display(self) -> str:
        root = _nt_normalise(self.nt_root)
        if self.component_prefix is None:
            return root
        return root + "\\" + self.component_prefix.lower() + "*"


#: Canonical forbidden rules for the GitHub-hosted Windows runner's
#: preinstalled Pythons (ZA-WIN-BOOT-01): the runner tool cache, the
#: machine-wide ``C:\\Program Files\\Python*`` family, and the default
#: per-user install location.  The private bootstrap runtime NEVER lives
#: under any of these (its extraction target is the witness root), so
#: rejecting
#: them can never reject the private python.
_CANONICAL_FORBIDDEN_RULES: tuple[_ForbiddenRule, ...] = (
    _ForbiddenRule(r"C:\hostedtoolcache\windows\Python"),
    _ForbiddenRule(r"C:\Program Files", "Python"),
)


def _forbidden_rules(
    localappdata: str | None = None,
    extra_roots: Iterable[str] = (),
) -> tuple[_ForbiddenRule, ...]:
    """Resolve the effective forbidden rules.

    ``localappdata`` is the runner's ``%LOCALAPPDATA%`` (injected; on a
    real runner it is ``C:\\Users\\runneradmin\\AppData\\Local``); when
    omitted the per-user Programs\\Python root is not expanded (a real
    Windows runner always supplies it).  ``extra_roots`` are appended as
    exact-containment rules.
    """
    rules = list(_CANONICAL_FORBIDDEN_RULES)
    if localappdata:
        rules.append(
            _ForbiddenRule(
                _nt_normalise(
                    ntpath.join(str(localappdata), "Programs", "Python")
                )
            )
        )
    rules.extend(
        _ForbiddenRule(_nt_normalise(str(root))) for root in extra_roots
    )
    seen: set[str] = set()
    unique: list[_ForbiddenRule] = []
    for rule in rules:
        if rule.nt_root and rule.nt_root not in seen:
            seen.add(rule.nt_root)
            unique.append(rule)
    return tuple(unique)


def forbidden_python_roots(
    localappdata: str | None = None,
    extra_roots: Iterable[str] = (),
) -> tuple[str, ...]:
    """Human-readable forbidden roots (for diagnostics and tests)."""
    return tuple(
        rule.display
        for rule in _forbidden_rules(
            localappdata=localappdata, extra_roots=extra_roots
        )
    )


def _matches_forbidden_rule(path: str, rule: _ForbiddenRule) -> bool:
    """``True`` when nt-normalised *path* matches one forbidden rule."""
    norm_path = _nt_normalise(path)
    norm_root = _nt_normalise(rule.nt_root).rstrip("\\")
    if not (
        norm_path == norm_root or norm_path.startswith(norm_root + "\\")
    ):
        return False
    if rule.component_prefix is None:
        return True
    relative = norm_path[len(norm_root):].lstrip("\\")
    first_component = relative.split("\\", 1)[0]
    return first_component.startswith(rule.component_prefix.lower())


def detect_runner_python_violations(
    *,
    executable: str | os.PathLike[str],
    prefix: str | os.PathLike[str],
    base_prefix: str | os.PathLike[str],
    base_executable: str | os.PathLike[str] | None,
    pyvenv_cfg_home: str | os.PathLike[str] | None,
    localappdata: str | None = None,
    extra_roots: Iterable[str] = (),
) -> list[str]:
    """Return human-readable runner-python violations for an interpreter.

    Pure, path-provenance-only (never version-string comparison).  Every
    observable interpreter anchor (``sys.executable``, ``sys.prefix``,
    ``sys.base_prefix``, ``sys._base_executable`` and the venv's
    ``pyvenv.cfg home``) is checked against the forbidden rules; an empty
    list means the interpreter is clean.
    """
    rules = _forbidden_rules(
        localappdata=localappdata, extra_roots=extra_roots
    )
    violations: list[str] = []
    anchors: list[tuple[str, object]] = [
        ("sys.executable", executable),
        ("sys.prefix", prefix),
        ("sys.base_prefix", base_prefix),
        ("sys._base_executable", base_executable),
        ("pyvenv.cfg home", pyvenv_cfg_home),
    ]
    for label, value in anchors:
        if value is None:
            continue
        for rule in rules:
            if _matches_forbidden_rule(str(value), rule):
                violations.append(
                    f"{label} resolves to a forbidden runner/system Python "
                    f"root: {value!s} matches {rule.display!r}"
                )
    return violations


def assert_no_runner_python(
    *,
    executable: str | os.PathLike[str],
    prefix: str | os.PathLike[str],
    base_prefix: str | os.PathLike[str],
    base_executable: str | os.PathLike[str] | None,
    pyvenv_cfg_home: str | os.PathLike[str] | None,
    localappdata: str | None = None,
    extra_roots: Iterable[str] = (),
) -> None:
    """Fail closed (:class:`RunnerPythonError`) on any runner-python anchor."""
    violations = detect_runner_python_violations(
        executable=executable,
        prefix=prefix,
        base_prefix=base_prefix,
        base_executable=base_executable,
        pyvenv_cfg_home=pyvenv_cfg_home,
        localappdata=localappdata,
        extra_roots=extra_roots,
    )
    if violations:
        raise RunnerPythonError("; ".join(violations))


# ---------------------------------------------------------------------------
# Provenance assertions (pure, layout-aware)
# ---------------------------------------------------------------------------


def assert_venv_provenance(
    *,
    sys_base_prefix: str | os.PathLike[str],
    pyvenv_cfg_home: str | os.PathLike[str] | None,
    expected_base_dir: str | os.PathLike[str],
    expected_home_dir: str | os.PathLike[str],
    label: str = "venv",
    _norm: Callable[[str], str] = normalise_path,
) -> None:
    """Assert a venv's base provenance equals the expected private layout.

    * ``sys_base_prefix`` must equal ``expected_base_dir`` — on Windows
      that is the private CPython install directory itself; on POSIX the
      real prefix of the throwaway base interpreter.
    * ``pyvenv_cfg_home`` (when observable) must equal
      ``expected_home_dir`` — on Windows the private CPython install
      directory (home of ``python.exe``); on POSIX the directory of the
      base interpreter's executable.

    Raises :class:`ProvenanceError` on mismatch (fail closed).  Comparison
    is path-normalised and case-insensitive on Windows.
    """
    observed_base = _norm(sys_base_prefix)
    want_base = _norm(expected_base_dir)
    if observed_base != want_base:
        raise ProvenanceError(
            f"{label}: sys.base_prefix {observed_base!r} does not match "
            f"the expected base {want_base!r}"
        )
    if pyvenv_cfg_home is not None:
        observed_home = _norm(pyvenv_cfg_home)
        want_home = _norm(expected_home_dir)
        if observed_home != want_home:
            raise ProvenanceError(
                f"{label}: pyvenv.cfg home {observed_home!r} does not match "
                f"the expected home {want_home!r}"
            )


def _win_expected(*parts: str) -> str:
    """nt-normalised expected path assembled from witness-root parts."""
    return _nt_normalise(ntpath.join(*(str(p) for p in parts)))


def assert_appenv_provenance(
    *,
    sys_executable: str | os.PathLike[str],
    sys_prefix: str | os.PathLike[str],
    sys_base_prefix: str | os.PathLike[str],
    witness_root: str | os.PathLike[str],
    label: str = "appenv",
) -> None:
    """Assert the appenv interpreter is a venv of the private CPython.

    Fail closed when:

    * ``sys.base_prefix`` is not the private install dir
      (``<witness-root>/python``);
    * ``sys.prefix`` is not ``<witness-root>/appenv``;
    * ``sys.executable`` is not inside the appenv's own ``Scripts``
      directory (Windows).

    Path provenance only; no version-string comparison.  All comparisons
    use :mod:`ntpath` semantics so the logic is identical on Windows and
    in hermetic Linux tests.
    """
    root = str(witness_root)
    want_base = _win_expected(root, "python")
    want_prefix = _win_expected(root, "appenv")
    want_scripts = _win_expected(root, "appenv", "Scripts")

    observed_base = _nt_normalise(sys_base_prefix)
    if observed_base != want_base:
        raise ProvenanceError(
            f"{label}: sys.base_prefix {observed_base!r} is not the private "
            f"CPython install {want_base!r} — the appenv is NOT running on "
            "the private runtime"
        )
    observed_prefix = _nt_normalise(sys_prefix)
    if observed_prefix != want_prefix:
        raise ProvenanceError(
            f"{label}: sys.prefix {observed_prefix!r} is not the appenv "
            f"{want_prefix!r}"
        )
    observed_exe = _nt_normalise(sys_executable)
    if observed_exe != _win_expected(root, "appenv", "Scripts", "python.exe"):
        raise ProvenanceError(
            f"{label}: sys.executable {observed_exe!r} is not the appenv "
            f"interpreter {want_scripts}\\python.exe"
        )


def assert_child_venv_provenance(
    *,
    pyvenv_cfg_home: str | os.PathLike[str] | None,
    sys_base_prefix: str | os.PathLike[str],
    child_scripts_python: str | os.PathLike[str],
    private_python_dir_path: str | os.PathLike[str],
    label: str = "child venv",
) -> None:
    """Assert a runtime-style child venv derives from the private CPython.

    Mirrors the structural claim the Windows witness records for a child
    venv created with ``venv.create(..., with_pip=True)`` (the same
    mechanism the shared runtime uses): on the Windows layout both its
    ``pyvenv.cfg home`` and ``sys.base_prefix`` must resolve to the private
    CPython install directory (never to a runner/system Python), and the
    child interpreter must be the venv's own ``Scripts\\python.exe``.

    The base/home equality is delegated to :func:`assert_venv_provenance`
    (with :mod:`ntpath` normalisation) so the Linux end-to-end validation
    exercises the exact same assertion core with POSIX layout
    expectations.
    """
    want = str(private_python_dir_path)
    assert_venv_provenance(
        sys_base_prefix=sys_base_prefix,
        pyvenv_cfg_home=pyvenv_cfg_home,
        expected_base_dir=want,
        expected_home_dir=want,
        label=label,
        _norm=_nt_normalise,
    )
    scripts_python = _nt_normalise(child_scripts_python)
    if not scripts_python.lower().endswith(r"\scripts\python.exe"):
        raise ProvenanceError(
            f"{label}: the child interpreter {scripts_python!r} is not the "
            r"expected Scripts\python.exe of the child venv"
        )
