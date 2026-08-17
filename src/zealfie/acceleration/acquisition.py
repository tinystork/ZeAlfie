"""Real accelerated artifact acquisition (ZA-M1-2J Phase D).

Curated, human-gated source of truth for the concrete wheels that make
up the accelerated NVIDIA_CUDA closure: the packaged
``manifests/accelerated_artifacts.toml`` manifest plus the
fail-closed acquirer that turns planned dependencies into verified
local wheel files.

Contract — the acquirer NEVER selects, guesses, or falls back:

* every planned distribution must have exactly one manifest entry for
  the plan backend + host platform + running Python tag — otherwise
  :class:`MissingArtifact` / :class:`PlatformMismatch`;
* the manifest version must satisfy the merged specifier of the
  planned dependency (prereleases allowed) — otherwise
  :class:`VersionMismatch`;
* every download is verified byte-for-byte against the manifest
  ``sha256`` + ``size`` — otherwise :class:`Sha256Mismatch`.  A local
  file is reused ONLY after the same re-verification (never on
  presence alone);
* downloads stream into ``work_root/artifacts/<filename>`` (nothing is
  ever written outside ``work_root``), honour cooperative cancellation
  per chunk, and retry transient transport failures a short fixed
  number of times (:class:`TransportError` when exhausted).

No pip, no subprocess, no installation: acquisition only deposits
verified wheels.  ``file://`` URLs are supported so tests can exercise
the exact production path hermetically; ``http(s)://`` is the
production transport.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from zealfie.acceleration.deployment import (
    AcceleratedAcquisitionError,
    AcceleratedDeploymentPlan,
    AcquiredAcceleratedVariant,
)
from zealfie.acceleration.models import KNOWN_BACKENDS
from zealfie.acceleration.variants import (
    AcceleratedVariant,
    AcceleratedVariantCatalog,
    AmbiguousVariantError,
)
from zealfie.releases.model import HostTarget

ARTIFACT_MANIFEST_PACKAGE = "zealfie.manifests"
ARTIFACT_MANIFEST_RESOURCE = "accelerated_artifacts.toml"
SUPPORTED_ARTIFACT_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PYTHON_TAG_RE = re.compile(r"^(cp|py)[0-9]+$")
_URL_SCHEMES = frozenset({"file", "http", "https"})
_CHUNK_SIZE = 1 << 20  # 1 MiB


# ---------------------------------------------------------------------------
# Acquisition errors — all fail-closed, all AcceleratedAcquisitionError
# ---------------------------------------------------------------------------


class InvalidArtifactManifestError(AcceleratedAcquisitionError):
    """The artifact manifest is malformed (loader refuses it)."""


class MissingArtifact(AcceleratedAcquisitionError):
    """No manifest entry matches the planned dependency."""


class Sha256Mismatch(AcceleratedAcquisitionError):
    """Downloaded / cached artifact fails integrity re-verification."""


class VersionMismatch(AcceleratedAcquisitionError):
    """The manifest artifact version does not satisfy the plan specifier."""


class PlatformMismatch(AcceleratedAcquisitionError):
    """The manifest artifact does not target the running interpreter."""


class TransportError(AcceleratedAcquisitionError):
    """The artifact could not be fetched (retries exhausted)."""


# ---------------------------------------------------------------------------
# Manifest value objects (validated at construction — fail-closed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcceleratedArtifactEntry:
    """One declared accelerated wheel.

    ``distribution`` is canonicalized (PEP 503).  ``sha256`` is
    mandatory and must be exactly 64 hex characters.  ``python`` is the
    wheel's Python tag (e.g. ``cp313``) or ``None`` for
    Python-independent artifacts; ``requires_python`` is the upstream
    ``Requires-Python`` metadata or ``None``.
    """

    distribution: str
    version: str
    backend: str
    platform: str
    filename: str
    url: str
    size: int
    sha256: str
    python: str | None = None
    requires_python: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.distribution, str) or not self.distribution.strip():
            raise InvalidArtifactManifestError(
                "artifact distribution must be a non-empty string"
            )
        object.__setattr__(
            self,
            "distribution",
            canonicalize_name(self.distribution.strip()),
        )

        if not isinstance(self.version, str) or not self.version.strip():
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} version must be a non-empty string"
            )
        object.__setattr__(self, "version", self.version.strip())

        backend = self.backend
        if not isinstance(backend, str) or not backend.strip():
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} backend must be a non-empty string"
            )
        backend = backend.strip()
        if backend not in KNOWN_BACKENDS:
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r}: unsupported backend {backend!r}"
            )
        object.__setattr__(self, "backend", backend)

        platform = self.platform
        if not isinstance(platform, str) or not platform.strip():
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} platform must be a non-empty string"
            )
        object.__setattr__(self, "platform", platform.strip())

        python = self.python
        if python is not None:
            if not isinstance(python, str) or not _PYTHON_TAG_RE.match(python.strip()):
                raise InvalidArtifactManifestError(
                    f"artifact {self.distribution!r}: python tag {python!r} "
                    "must match (cp|py)<digits>"
                )
            object.__setattr__(self, "python", python.strip())

        requires_python = self.requires_python
        if requires_python is not None:
            if not isinstance(requires_python, str):
                raise InvalidArtifactManifestError(
                    f"artifact {self.distribution!r} requires_python must be a string"
                )
            requires_python = requires_python.strip()
            if requires_python:
                try:
                    SpecifierSet(requires_python)
                except InvalidSpecifier as exc:
                    raise InvalidArtifactManifestError(
                        f"artifact {self.distribution!r} requires_python is "
                        f"invalid: {exc}"
                    ) from exc
            object.__setattr__(
                self,
                "requires_python",
                requires_python or None,
            )

        filename = self.filename
        if not isinstance(filename, str) or not filename.strip():
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} filename must be a non-empty string"
            )
        filename = filename.strip()
        if (
            filename in (".", "..")
            or "/" in filename
            or "\\" in filename
            or not filename.endswith(".whl")
        ):
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r}: filename {filename!r} must "
                "be a .whl basename"
            )
        object.__setattr__(self, "filename", filename)

        url = self.url
        if not isinstance(url, str) or not url.strip():
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} url must be a non-empty string"
            )
        url = url.strip()
        scheme = url.split(":", 1)[0] if ":" in url else ""
        if scheme.lower() not in _URL_SCHEMES:
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r}: unsupported url scheme "
                f"{scheme!r} (expected one of {sorted(_URL_SCHEMES)})"
            )
        object.__setattr__(self, "url", url)

        size = self.size
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} size must be a positive int"
            )
        object.__setattr__(self, "size", size)

        sha256 = self.sha256
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256.strip()):
            raise InvalidArtifactManifestError(
                f"artifact {self.distribution!r} sha256 is mandatory and must "
                "be exactly 64 hex characters"
            )
        object.__setattr__(self, "sha256", sha256.strip().lower())


def _entry_key(entry: AcceleratedArtifactEntry) -> tuple[str, str, str, str | None]:
    return (entry.distribution, entry.backend, entry.platform, entry.python)


@dataclass(frozen=True, slots=True)
class AcceleratedArtifactManifest:
    """Immutable collection of declared accelerated artifacts.

    Duplicate ``(distribution, backend, platform, python)`` keys are
    rejected at construction (fail-closed).  Duplicate filenames are
    rejected too — except when the entries reference the identical
    immutable bytes (same validated ``url``, ``size`` and lowercased
    ``sha256``), e.g. a ``py3-none-any`` wheel shared by multiple
    platform rows.
    """

    entries: tuple[AcceleratedArtifactEntry, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        seen_keys: dict[tuple[str, str, str, str | None], AcceleratedArtifactEntry] = {}
        seen_filenames: dict[str, AcceleratedArtifactEntry] = {}
        for entry in entries:
            if not isinstance(entry, AcceleratedArtifactEntry):
                raise InvalidArtifactManifestError(
                    "entries must contain AcceleratedArtifactEntry values, "
                    f"got {type(entry).__qualname__}"
                )
            key = _entry_key(entry)
            if key in seen_keys:
                raise InvalidArtifactManifestError(
                    f"duplicate artifact entry for distribution "
                    f"{key[0]!r} backend {key[1]!r} platform {key[2]!r} "
                    f"python {key[3]!r}"
                )
            seen_keys[key] = entry
            if entry.filename in seen_filenames:
                prior = seen_filenames[entry.filename]
                same_immutable_bytes = (
                    prior.url == entry.url
                    and prior.size == entry.size
                    and prior.sha256 == entry.sha256
                )
                if not same_immutable_bytes:
                    raise InvalidArtifactManifestError(
                        f"duplicate artifact filename {entry.filename!r} "
                        f"(distributions {prior.distribution!r} "
                        f"and {entry.distribution!r})"
                    )
            else:
                seen_filenames[entry.filename] = entry
        object.__setattr__(self, "entries", entries)

    def find(
        self,
        distribution: str,
        backend: str,
        platform: str,
        python_tag: str | None = None,
    ) -> AcceleratedArtifactEntry | None:
        """Return the single matching entry, fail-closed.

        Zero matches → ``None``.  More than one candidate differing
        only by Python tag is resolved by *python_tag*: a tag-specific
        match wins; ambiguity without a resolvable tag raises
        :class:`~zealfie.acceleration.variants.AmbiguousVariantError`
        (never pick arbitrarily).
        """
        if not isinstance(distribution, str) or not distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        canon = canonicalize_name(distribution.strip())
        backend = str(backend or "").strip()
        platform = str(platform or "").strip()

        candidates = [
            entry
            for entry in self.entries
            if entry.distribution == canon
            and entry.backend == backend
            and entry.platform == platform
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        if python_tag:
            tagged = [
                entry for entry in candidates if entry.python == python_tag
            ]
            if len(tagged) == 1:
                return tagged[0]
            if len(tagged) > 1:
                raise AmbiguousVariantError(
                    f"multiple artifact entries match distribution {canon!r} "
                    f"backend {backend!r} platform {platform!r} python "
                    f"{python_tag!r}"
                )
            generic = [entry for entry in candidates if entry.python is None]
            if len(generic) == 1:
                return generic[0]
        raise AmbiguousVariantError(
            f"multiple artifact entries match distribution {canon!r} "
            f"backend {backend!r} platform {platform!r}: "
            + ", ".join(sorted(e.python or "any" for e in candidates))
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def default_accelerated_artifact_manifest() -> AcceleratedArtifactManifest:
    """Load the packaged accelerated artifact manifest (real source)."""
    try:
        resource = importlib.resources.files(ARTIFACT_MANIFEST_PACKAGE).joinpath(
            ARTIFACT_MANIFEST_RESOURCE
        )
        return load_accelerated_artifact_manifest(
            resource.read_text(encoding="utf-8")
        )
    except AcceleratedAcquisitionError:
        raise
    except Exception as exc:
        raise InvalidArtifactManifestError(
            f"artifact manifest resource could not be read: {exc}"
        ) from exc


def load_accelerated_artifact_manifest(text: str) -> AcceleratedArtifactManifest:
    """Parse an accelerated artifact manifest from TOML text.

    Fail-closed: schema version, entry fields (including the mandatory
    64-hex ``sha256``), duplicates and unknown keys are all validated.
    """
    import tomllib

    if not isinstance(text, str):
        raise InvalidArtifactManifestError("manifest text must be a string")
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidArtifactManifestError(
            f"artifact manifest TOML is invalid: {exc}"
        ) from exc
    return _manifest_from_payload(payload)


def load_accelerated_artifact_manifest_file(
    path: str | Path,
) -> AcceleratedArtifactManifest:
    """Parse an accelerated artifact manifest from a file path."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        raise InvalidArtifactManifestError(
            f"artifact manifest file could not be read: {exc}"
        ) from exc
    return load_accelerated_artifact_manifest(text)


def _manifest_from_payload(payload: object) -> AcceleratedArtifactManifest:
    if not isinstance(payload, dict):
        raise InvalidArtifactManifestError("manifest root must be a table")
    schema_version = payload.get("schema_version")
    if schema_version is None:
        raise InvalidArtifactManifestError("schema_version is required")
    if not isinstance(schema_version, int):
        raise InvalidArtifactManifestError("schema_version must be an integer")
    if schema_version != SUPPORTED_ARTIFACT_SCHEMA_VERSION:
        raise InvalidArtifactManifestError(
            f"unsupported schema_version: {schema_version}"
        )

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise InvalidArtifactManifestError("artifacts must be an array of tables")
    if not raw_artifacts:
        raise InvalidArtifactManifestError("artifacts must not be empty")

    known_keys = {
        "distribution",
        "version",
        "backend",
        "platform",
        "python",
        "requires_python",
        "filename",
        "url",
        "size",
        "sha256",
    }
    entries: list[AcceleratedArtifactEntry] = []
    for idx, raw in enumerate(raw_artifacts):
        if not isinstance(raw, dict):
            raise InvalidArtifactManifestError(
                f"artifacts[{idx}] must be a table"
            )
        unknown = sorted(set(raw) - known_keys)
        if unknown:
            raise InvalidArtifactManifestError(
                f"artifacts[{idx}] contains unknown key(s): "
                + ", ".join(repr(key) for key in unknown)
            )
        for required in (
            "distribution",
            "version",
            "backend",
            "platform",
            "filename",
            "url",
            "size",
            "sha256",
        ):
            if required not in raw:
                raise InvalidArtifactManifestError(
                    f"artifacts[{idx}].{required} is required"
                )
        entries.append(
            AcceleratedArtifactEntry(
                distribution=raw["distribution"],
                version=raw["version"],
                backend=raw["backend"],
                platform=raw["platform"],
                filename=raw["filename"],
                url=raw["url"],
                size=raw["size"],
                sha256=raw["sha256"],
                python=raw.get("python"),
                requires_python=raw.get("requires_python"),
            )
        )
    return AcceleratedArtifactManifest(tuple(entries))


# ---------------------------------------------------------------------------
# Variant catalog derived from the manifest
# ---------------------------------------------------------------------------


def variant_catalog_from_artifact_manifest(
    manifest: AcceleratedArtifactManifest,
) -> AcceleratedVariantCatalog:
    """Build an :class:`AcceleratedVariantCatalog` from a manifest.

    One variant per entry, keyed ``(distribution, backend, platform)``.
    Entries that differ only by Python tag would collide on that key —
    rejected here (a variant catalog must be unambiguous per platform).
    """
    if not isinstance(manifest, AcceleratedArtifactManifest):
        raise ValueError(
            "manifest must be an AcceleratedArtifactManifest, "
            f"got {type(manifest).__qualname__}"
        )
    seen: set[tuple[str, str, str]] = set()
    variants: list[AcceleratedVariant] = []
    for entry in manifest.entries:
        key = (entry.distribution, entry.backend, entry.platform)
        if key in seen:
            raise InvalidArtifactManifestError(
                f"artifact manifest is ambiguous for the variant catalog: "
                f"multiple python variants for distribution {key[0]!r} "
                f"backend {key[1]!r} platform {key[2]!r}"
            )
        seen.add(key)
        variants.append(
            AcceleratedVariant(
                distribution=entry.distribution,
                version=entry.version,
                backend=entry.backend,
                platform=entry.platform,
                sha256=entry.sha256,
            )
        )
    return AcceleratedVariantCatalog(tuple(variants))


# ---------------------------------------------------------------------------
# Acquirer
# ---------------------------------------------------------------------------


class ManifestAcceleratedArtifactAcquirer:
    """Acquire planned accelerated wheels from the artifact manifest.

    Complies with the
    :class:`~zealfie.acceleration.deployment.AcceleratedArtifactAcquirer`
    protocol: one :class:`AcquiredAcceleratedVariant` per planned
    dependency, versions checked against the merged specifier, every
    download byte-verified against the manifest sha256/size, reuse
    gated on the same re-verification, and all writes confined to
    ``work_root``.
    """

    def __init__(
        self,
        manifest: AcceleratedArtifactManifest | None = None,
        *,
        platform_tag: str | None = None,
        python_tag: str | None = None,
        timeout: float = 60.0,
        retries: int = 2,
        retry_delay: float = 0.5,
        urlopen: Callable | None = None,
    ) -> None:
        self._manifest = manifest or default_accelerated_artifact_manifest()
        host = HostTarget.from_current_host()
        self._platform_tag = (platform_tag or host.platform_tag).strip()
        if not self._platform_tag:
            raise ValueError("platform_tag must be a non-empty string")
        self._python_tag = python_tag or (
            f"cp{sys.version_info.major}{sys.version_info.minor}"
        )
        self._timeout = float(timeout)
        self._retries = max(0, int(retries))
        self._retry_delay = float(retry_delay)
        self._urlopen = urlopen or urllib.request.urlopen

    # -- acquire -------------------------------------------------------------

    def acquire(
        self,
        plan: AcceleratedDeploymentPlan,
        work_root: Path,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> tuple[AcquiredAcceleratedVariant, ...]:
        """Acquire every planned dependency, fail-closed.

        Raises the structured acquisition errors above; never returns a
        partial tuple.  Writes only under ``work_root/artifacts/``.
        """
        work_root = Path(work_root)
        backend = plan.backend
        if not backend:
            raise AcceleratedAcquisitionError(
                "accelerated plan has no backend; nothing to acquire"
            )

        planned = tuple(plan.added_requirements)
        seen: set[str] = set()
        for entry in planned:
            if entry.distribution in seen:
                raise AcceleratedAcquisitionError(
                    f"duplicate planned distribution {entry.distribution!r}"
                )
            seen.add(entry.distribution)

        if not planned:
            return ()

        artifacts_dir = work_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        acquired: list[AcquiredAcceleratedVariant] = []
        for planned_entry in planned:
            manifest_entry = self._resolve_entry(planned_entry.distribution, backend)
            self._check_python_target(planned_entry.distribution, manifest_entry)
            self._check_version(planned_entry, manifest_entry)
            wheel_path = self._materialize(
                manifest_entry, artifacts_dir, cancel_check=cancel_check
            )
            acquired.append(
                AcquiredAcceleratedVariant(
                    distribution=planned_entry.distribution,
                    version=manifest_entry.version,
                    wheel_path=wheel_path,
                    size=manifest_entry.size,
                    sha256=manifest_entry.sha256,
                )
            )
        return tuple(acquired)

    # -- internal resolution helpers ----------------------------------------

    def _resolve_entry(
        self, distribution: str, backend: str
    ) -> AcceleratedArtifactEntry:
        entry = self._manifest.find(
            distribution,
            backend,
            self._platform_tag,
            python_tag=self._python_tag,
        )
        if entry is None:
            raise MissingArtifact(
                f"no accelerated artifact for distribution {distribution!r} "
                f"backend {backend!r} platform {self._platform_tag!r}"
            )
        return entry

    def _check_python_target(
        self, distribution: str, entry: AcceleratedArtifactEntry
    ) -> None:
        if entry.python is not None and not _python_tag_compatible(
            entry.python, self._python_tag
        ):
            raise PlatformMismatch(
                f"artifact for {distribution!r} targets python tag "
                f"{entry.python!r} but this interpreter is "
                f"{self._python_tag!r}"
            )

    def _check_version(
        self,
        planned_entry,
        manifest_entry: AcceleratedArtifactEntry,
    ) -> None:
        specifier = planned_entry.specifier
        if specifier is not None and not SpecifierSet(specifier).contains(
            manifest_entry.version, prereleases=True
        ):
            raise VersionMismatch(
                f"artifact version {manifest_entry.version!r} for "
                f"{planned_entry.distribution!r} does not satisfy declared "
                f"specifier {specifier!r}"
            )

    # -- materialization ------------------------------------------------------

    def _materialize(
        self,
        entry: AcceleratedArtifactEntry,
        artifacts_dir: Path,
        *,
        cancel_check: Callable[[], None] | None,
    ) -> Path:
        dest = artifacts_dir / entry.filename
        if dest.is_file():
            # Reuse ONLY after byte-level re-verification — never on
            # presence alone.
            size, digest = _file_size_and_sha256(dest)
            if size != entry.size or digest != entry.sha256:
                raise Sha256Mismatch(
                    f"existing local artifact {entry.filename} for "
                    f"{entry.distribution!r} fails re-verification: expected "
                    f"size {entry.size} sha256 {entry.sha256}, got size "
                    f"{size} sha256 {digest}"
                )
            return dest
        self._download(entry, dest, cancel_check=cancel_check)
        return dest

    def _download(
        self,
        entry: AcceleratedArtifactEntry,
        dest: Path,
        *,
        cancel_check: Callable[[], None] | None,
    ) -> None:
        part = dest.parent / (dest.name + ".part")
        for attempt in range(self._retries + 1):
            _remove_best_effort(part)
            try:
                with self._urlopen(entry.url, timeout=self._timeout) as response:
                    with open(part, "wb") as out:
                        while True:
                            chunk = response.read(_CHUNK_SIZE)
                            if not chunk:
                                break
                            if cancel_check is not None:
                                cancel_check()
                            out.write(chunk)
                size, digest = _file_size_and_sha256(part)
                if size != entry.size:
                    raise Sha256Mismatch(
                        f"downloaded artifact {entry.filename} for "
                        f"{entry.distribution!r} has size {size}; manifest "
                        f"declares {entry.size}"
                    )
                if digest != entry.sha256:
                    raise Sha256Mismatch(
                        f"downloaded artifact {entry.filename} for "
                        f"{entry.distribution!r} has sha256 {digest}; manifest "
                        f"declares {entry.sha256}"
                    )
                os.replace(part, dest)
                return
            except Sha256Mismatch:
                _remove_best_effort(part)
                raise
            except urllib.error.HTTPError as exc:
                _remove_best_effort(part)
                if exc.code >= 500 and attempt < self._retries:
                    self._pause(attempt)
                    continue
                raise TransportError(
                    f"download failed for {entry.distribution!r} "
                    f"({entry.filename}): HTTP {exc.code} {exc.reason}"
                ) from exc
            except (socket.timeout, TimeoutError, ConnectionError,
                    urllib.error.URLError, OSError) as exc:
                _remove_best_effort(part)
                if attempt < self._retries:
                    self._pause(attempt)
                    continue
                raise TransportError(
                    f"download failed for {entry.distribution!r} "
                    f"({entry.filename}): {type(exc).__name__}: {exc}"
                ) from exc

    def _pause(self, attempt: int) -> None:
        if self._retry_delay > 0:
            time.sleep(self._retry_delay * (attempt + 1))


def _file_size_and_sha256(path: Path) -> tuple[int, str]:
    """Return ``(size, sha256_hex)`` of a file (chunked read)."""
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            size += len(chunk)
            sha.update(chunk)
    return size, sha.hexdigest()


def _python_tag_compatible(entry_tag: str, interpreter_tag: str) -> bool:
    """PEP 425 python-tag compatibility (conservative).

    An exact match always works.  A ``py3`` component in the entry tag
    matches any CPython 3 interpreter (``py3-none`` wheels are
    version-independent); everything else must match exactly.  Used by
    the acquirer so the Phase F closure's ``py3`` nvidia-*-cu12 wheels
    are accepted on a ``cp313`` interpreter.
    """
    if not entry_tag or not interpreter_tag:
        return False
    if entry_tag == interpreter_tag:
        return True
    for component in entry_tag.split("."):
        if component == "py3" and interpreter_tag.startswith("cp3"):
            return True
    return False


def _remove_best_effort(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Production defaults (wired by the service layer)
# ---------------------------------------------------------------------------


def default_manifest_variant_catalog() -> AcceleratedVariantCatalog:
    """Return the variant catalog built from the packaged manifest.

    This is the production default for accelerated deployment planning.
    The pure empty :func:`~zealfie.acceleration.variants.default_variant_catalog`
    remains available for unit tests.
    """
    return variant_catalog_from_artifact_manifest(
        default_accelerated_artifact_manifest()
    )


def default_manifest_artifact_acquirer() -> ManifestAcceleratedArtifactAcquirer:
    """Return the manifest-backed production acquirer.

    The explicit fail-closed
    :func:`~zealfie.acceleration.deployment.default_accelerated_artifact_acquirer`
    (always raises) remains available when no source is configured at all.
    """
    return ManifestAcceleratedArtifactAcquirer(
        default_accelerated_artifact_manifest()
    )
