"""Dependency acquisition contract layer (M1-2D.4.2A — YAGNI-simplified).

PURE contract models and request-building helpers.  No pip, no
subprocess, no network, no mutation — this is architecture plumbing
for the future ``PipWheelhouseAcquirer`` (D.4.2B).

Design invariants:

* Authority for ``Provides-Extra`` is the METADATA inside the verified
  product wheel — never catalog text or a second source.
* Pre-flight validation MUST reject extras not declared by
  ``Provides-Extra`` before any future transport.
* The acquirer later must NOT use ``--no-deps``.
* Staging lifecycle: acquirer does not self-clean after successful
  acquisition; service orchestration owns cleanup after its own
  lock/plan/apply/TOCTOU/install/validation/activation window.
* The result wheelhouse contains only dependencies (not the product
  wheel itself).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from packaging.metadata import parse_email
from packaging.utils import canonicalize_name, parse_wheel_filename

from zealfie.building import read_wheel_metadata_raw

from .models import ExtraNotFound, MetadataError


# ---------------------------------------------------------------------------
# Structured acquisition errors
# ---------------------------------------------------------------------------


class DependencyAcquisitionError(RuntimeError):
    """Base class for all dependency acquisition failures.

    Every sub-error carries structured data; callers must never
    silently swallow an acquisition error.
    """


class AcquisitionTransportError(DependencyAcquisitionError):
    """A transport-level failure prevented dependency acquisition.

    Covers pip invocation failures, subprocess timeouts, missing pip
    executable, or filesystem permission errors during staging.
    Carries enough context for callers to produce diagnostic output.

    *stage* labels *when* the failure occurred (e.g. ``"download"``,
    ``"build"``, ``"pip-invoke"``).  *detail* is a human-readable
    description.
    """

    def __init__(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"acquisition transport error ({stage}): {detail}")


# ---------------------------------------------------------------------------
# Acquired wheel record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcquiredWheel:
    """A dependency wheel acquired into the staging wheelhouse.

    Every field is derived from the actual wheel file — never from
    pip stdout, catalog text, or untrusted external metadata.

    Identity is taken from the wheel *filename* (PEP 427) only.
    The resolver remains the authority for metadata identity checks,
    tags, constraints, extras, markers, and lock selection.
    """

    name: str       # canonicalised (PEP 503)
    version: str    # PEP 440 normalised version string
    wheel_path: Path  # absolute path to the acquired wheel file
    filename: str   # basename of the wheel file
    size: int       # file size in bytes
    sha256: str     # hex digest

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("AcquiredWheel.name must not be empty")
        if not self.version or not self.version.strip():
            raise ValueError("AcquiredWheel.version must not be empty")
        if self.size < 0:
            raise ValueError("AcquiredWheel.size must be non-negative")
        if not self.sha256 or len(self.sha256) != 64:
            raise ValueError(
                "AcquiredWheel.sha256 must be a 64-character hex digest"
            )

    @classmethod
    def from_wheel_file(
        cls,
        wheel_path: Path,
        *,
        hash_algorithm: str = "sha256",
    ) -> "AcquiredWheel":
        """Construct an ``AcquiredWheel`` from a verified wheel file.

        Reads the wheel filename for identity, then computes file size
        and hash from the actual wheel content.  Does NOT read
        METADATA or duplicate resolver identity/tags/constraints logic.

        Parameters
        ----------
        wheel_path:
            Absolute or relative path to a ``.whl`` file that must
            exist on disk.
        hash_algorithm:
            Hash algorithm name passed to :func:`hashlib.new`.

        Returns
        -------
        AcquiredWheel
            An immutable record derived from the wheel file.

        Raises
        ------
        MetadataError
            If the filename cannot be parsed as a valid wheel filename.
        FileNotFoundError
            If *wheel_path* does not exist.
        """
        wheel_path = wheel_path.resolve(strict=True)

        # --- Parse filename -------------------------------------------------
        try:
            parsed = parse_wheel_filename(wheel_path.name)
            name = canonicalize_name(parsed[0])
            version = str(parsed[1])
        except Exception as exc:
            raise MetadataError(
                wheel_path,
                f"cannot parse wheel filename for acquisition record: {exc}",
            ) from exc

        # --- Hash and size --------------------------------------------------
        size = wheel_path.stat().st_size
        h = hashlib.new(hash_algorithm)
        with open(wheel_path, "rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
        digest = h.hexdigest()

        return cls(
            name=name,
            version=version,
            wheel_path=wheel_path,
            filename=wheel_path.name,
            size=size,
            sha256=digest,
        )


# ---------------------------------------------------------------------------
# Acquisition request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyAcquisitionRequest:
    """A validated, ready-to-execute dependency acquisition request.

    Constructed via :func:`build_acquisition_request` which ensures
    all pre-flight validations pass (extras declared, METADATA
    readable).

    *product_wheel_path* is the LOCAL verified product wheel file.
    The METADATA inside this file is the sole authority for
    ``Provides-Extra``.

    *active_extras* is the canonical set of extras to activate.
    Must be a subset of ``Provides-Extra`` in the product wheel.
    """

    product_wheel_path: Path
    active_extras: frozenset[str]


# ---------------------------------------------------------------------------
# Acquisition result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyAcquisitionResult:
    """The result of a completed dependency acquisition.

    **Staging lifecycle**: the acquirer does NOT self-clean after
    successful acquisition.  The service orchestration owns cleanup
    after its own lock/plan/apply/TOCTOU/install/validation/activation
    window.  Callers MUST NOT assume the staging directory persists
    indefinitely.

    *staging_wheelhouse* contains ONLY dependencies (not the product
    wheel itself).  The product wheel may be copied into staging by
    pip during acquisition and subsequently removed; the result never
    includes it.

    *acquired* lists every dependency wheel acquired, each backed by
    an :class:`AcquiredWheel` record derived from the actual file.
    """

    staging_wheelhouse: Path
    acquired: tuple[AcquiredWheel, ...]
    # ZA-M1-3A.3 LOT E: True when the wheelhouse was seeded from the shared
    # verified artifact cache (find-links candidates from proven
    # identities).  Observational only - the seeded candidates are never
    # an authority for what was actually installed.
    seeded_from_cache: bool = False


# ---------------------------------------------------------------------------
# Request builder (pre-flight validation)
# ---------------------------------------------------------------------------


def build_acquisition_request(
    product_wheel_path: Path,
    active_extras: frozenset[str] | None = None,
) -> DependencyAcquisitionRequest:
    """Construct and validate a :class:`DependencyAcquisitionRequest`.

    Pre-flight checks (zero-network; all local):

    1. *product_wheel_path* must exist and be a ``.whl`` file.
    2. METADATA inside the wheel must be readable.
    3. Requested *active_extras* must each be declared in
       ``Provides-Extra``.

    The returned request is immutable and ready for the transport
    layer (D.4.2B).

    Parameters
    ----------
    product_wheel_path:
        LOCAL verified product ``.whl`` file.
    active_extras:
        Canonicalised set of extra names to activate.  Must be a
        subset of the wheel's ``Provides-Extra``.

    Returns
    -------
    DependencyAcquisitionRequest
        Validated, immutable, ready for transport.

    Raises
    ------
    ExtraNotFound
        If any requested extra is not declared in ``Provides-Extra``
        of the product wheel METADATA.
    MetadataError
        If the wheel's METADATA cannot be read or parsed.
    FileNotFoundError
        If *product_wheel_path* does not exist.
    """
    product_wheel_path = product_wheel_path.resolve(strict=True)

    # Canonicalise extras: lowercase, underscores → dashes (PEP 685).
    if active_extras is None:
        active_extras = frozenset()
    else:
        active_extras = frozenset(
            canonicalize_name(e) for e in active_extras
        )

    # --- Read METADATA (sole authority for Provides-Extra) ------------------
    try:
        raw_metadata = read_wheel_metadata_raw(product_wheel_path)
        meta = parse_email(raw_metadata)[0]
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise MetadataError(
            product_wheel_path,
            f"cannot read product wheel METADATA: {exc}",
        ) from exc

    # --- Validate extras against Provides-Extra ----------------------------
    provides_extra_raw: list[str] | None = meta.get("provides_extra")  # type: ignore[assignment]

    if provides_extra_raw:
        provides_extra = frozenset(
            canonicalize_name(v.strip()) for v in provides_extra_raw if v.strip()
        )
    else:
        provides_extra = frozenset()

    meta_name = canonicalize_name(meta.get("name", ""))

    for extra in active_extras:
        if extra not in provides_extra:
            raise ExtraNotFound(meta_name, extra, provides_extra)

    return DependencyAcquisitionRequest(
        product_wheel_path=product_wheel_path,
        active_extras=active_extras,
    )
