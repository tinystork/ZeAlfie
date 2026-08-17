"""Transactional accelerated deployment engine (M1-2I).

Turns the read-only :class:`~zealfie.acceleration.planning.AcceleratedDeploymentPlan`
produced by M1-2H into a real new shared runtime, transactionally:

* **Acquire** concrete accelerated artifact wheels (fail-closed: the
  production default acquirer is the manifest-backed acquirer from
  :mod:`zealfie.acceleration.acquisition`; the unconditional
  fail-closed acquirer below remains available for callers with no
  source configured at all);
* **Resolve** the FULL closure by extending the base product
  ``RuntimeLock`` — every base entry is preserved verbatim (same
  ``LockedDependency`` objects, same insertion order) and the
  accelerated wheels ride as NON-PRIMARY entries;
* **Build / Validate / Gate / Persist / Activate** through the existing
  M0-8B engine (:func:`~zealfie.runtime.deployment.apply_deployment_plan`)
  via its two minimal optional hooks — the compatibility gate and the
  observational metadata record both run inside ``pre_activate``,
  strictly after the version-match checks and strictly before
  activation;
* **Rollback / preservation** — the active pointer is never touched
  before activation, the orchestrator NEVER installs into the active
  slot (all installs go to the fresh candidate slot created by
  ``apply_deployment_plan``), and every failure path leaves the
  previously active runtime intact and usable;
* **Cooperative cancellation** — caller-supplied ``cancel_check``
  raising :class:`CooperativeCancellationError` interrupts the
  deployment at deterministic checkpoints.

Architectural invariant — ZeAlfie NEVER selects a concrete accelerated
framework.  The gate performs stdlib-only distribution/version probes
inside the candidate venv and, when the plan's backend declares a
compute probe in the :mod:`zealfie.acceleration.backend_probe` registry,
runs that self-contained probe with the candidate interpreter as a
final pre-activation check (real import + device compute + JIT kernel —
the M1-2J.1 lesson: a green install is not a green compute path).  A
backend without a registered probe keeps the distribution/version-only
behaviour.  The probe scripts live in the registry only — this module
stays generic and never names a concrete framework.

Metadata note: the observational record is written under
``txn.candidate_slot_id`` inside ``pre_activate``.  That IS the final
activated slot id in the M0-6 architecture — slots are created directly
at their final path and are never renamed — so recording under the
candidate slot id before the pointer switch is correct.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from zealfie.common.subprocess_platform import technical_subprocess_platform_kwargs
from zealfie.acceleration.planning import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
)
from zealfie.acceleration.backend_probe import get_backend_compute_probe
from zealfie.common import normalise_distribution_name
from zealfie.dependencies.models import LockedDependency, RuntimeLock
from zealfie.runtime.deployment import (
    DeploymentCancelledError,
    apply_deployment_plan,
)
from zealfie.runtime.layout import RuntimeLayout, validate_slot_id
from zealfie.runtime.probe import probe_runtime_distribution

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zealfie.components.registry import ComponentRegistry
    from zealfie.runtime.deployment import DeploymentPlan
    from zealfie.runtime.manager import SharedRuntime
    from zealfie.runtime.transaction import RuntimeTransaction


# ---------------------------------------------------------------------------
# Acquisition errors (fail-closed)
# ---------------------------------------------------------------------------


class AcceleratedAcquisitionError(RuntimeError):
    """Base class for accelerated artifact acquisition failures."""


class AcceleratedAcquisitionUnavailable(AcceleratedAcquisitionError):
    """No accelerated artifact source is configured (fail-closed).

    Raised by the production default acquirer: real artifact sources
    arrive with the human-gated real witness.
    """


class CooperativeCancellationError(RuntimeError):
    """Canonical cancellation signal for accelerated deployments.

    Raised by a caller-supplied ``cancel_check`` to interrupt an
    accelerated deployment.  The active pointer is never touched before
    activation, so a cancelled deployment leaves the previously active
    runtime fully usable.
    """


# ---------------------------------------------------------------------------
# Acquired accelerated variant (integrity-verified at construction)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcquiredAcceleratedVariant:
    """One acquired accelerated artifact wheel, verified against disk.

    ``distribution`` is canonicalized (PEP 503).  ``wheel_path`` must be
    an existing file, and ``size`` / ``sha256`` must equal the actual
    file size / SHA-256 digest — both are re-verified in
    ``__post_init__`` against the on-disk artifact (chunked read), so a
    constructed instance always describes the real file.
    """

    distribution: str
    version: str
    wheel_path: Path
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.distribution, str) or not self.distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        object.__setattr__(
            self, "distribution", canonicalize_name(self.distribution.strip())
        )

        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        object.__setattr__(self, "version", self.version.strip())

        wheel_path = Path(self.wheel_path)
        if not wheel_path.is_file():
            raise ValueError(
                f"wheel_path is not an existing file: {wheel_path}"
            )
        object.__setattr__(self, "wheel_path", wheel_path)

        if not isinstance(self.size, int):
            raise ValueError("size must be an int")
        actual_size = wheel_path.stat().st_size
        if self.size != actual_size:
            raise ValueError(
                f"size mismatch: declared {self.size}, "
                f"actual {actual_size}"
            )
        object.__setattr__(self, "size", actual_size)

        if not isinstance(self.sha256, str) or not self.sha256.strip():
            raise ValueError("sha256 must be a non-empty string")
        object.__setattr__(self, "sha256", self.sha256.strip())
        actual_sha256 = _sha256_of_path(wheel_path)
        if self.sha256.lower() != actual_sha256:
            raise ValueError(
                f"sha256 mismatch: declared {self.sha256}, "
                f"actual {actual_sha256}"
            )


# ---------------------------------------------------------------------------
# Artifact acquisition
# ---------------------------------------------------------------------------


class AcceleratedArtifactAcquirer(Protocol):
    """Acquire concrete accelerated artifact wheels for a plan.

    ``acquire`` MUST return exactly one
    :class:`AcquiredAcceleratedVariant` per planned dependency
    (keyed by canonicalized distribution), with every version
    satisfying the merged specifier of its planned dependency (when one
    is declared).  Duplicate distributions are rejected.
    """

    def acquire(
        self,
        plan: AcceleratedDeploymentPlan,
        work_root: Path,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> tuple[AcquiredAcceleratedVariant, ...]: ...


class _UnavailableAcquirer:
    """Unconditional fail-closed acquirer (no artifact source at all).

    The production default is the manifest-backed acquirer
    (:mod:`zealfie.acceleration.acquisition`); this acquirer remains
    for callers that must refuse unconditionally.  Both callable and
    ``acquire``-method styles are supported; the protocol contract is
    the ``acquire`` method.
    """

    def acquire(
        self,
        plan: AcceleratedDeploymentPlan,
        work_root: Path,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> tuple[AcquiredAcceleratedVariant, ...]:
        raise AcceleratedAcquisitionUnavailable(
            "no accelerated artifact source configured"
        )

    def __call__(
        self,
        plan: AcceleratedDeploymentPlan,
        work_root: Path,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> tuple[AcquiredAcceleratedVariant, ...]:
        return self.acquire(plan, work_root, cancel_check=cancel_check)


def default_accelerated_artifact_acquirer() -> AcceleratedArtifactAcquirer:
    """Return the unconditional fail-closed acquirer.

    This is NOT the production default any more (ZA-M1-2J Phase D): the
    service wires the manifest-backed acquirer from
    :mod:`zealfie.acceleration.acquisition`.  The acquirer returned
    here remains for callers with no configured artifact source: it
    ALWAYS raises :class:`AcceleratedAcquisitionUnavailable`.
    """
    return _UnavailableAcquirer()


# ---------------------------------------------------------------------------
# Lock extension (pure)
# ---------------------------------------------------------------------------


def extend_runtime_lock_with_acceleration(
    base_lock: RuntimeLock,
    plan: AcceleratedDeploymentPlan,
    acquired: tuple[AcquiredAcceleratedVariant, ...],
    declaring_distributions: Mapping[str, str],
) -> RuntimeLock:
    """Return a NEW ``RuntimeLock``: base entries verbatim + accelerated entries.

    The returned lock contains every base entry VERBATIM (the same
    :class:`LockedDependency` objects, in the same insertion order) plus
    one new NON-PRIMARY entry per acquired variant:

    * ``name`` — the canonicalized distribution (the lock key);
    * ``version`` / ``wheel_path`` / ``size`` / ``sha256`` — from the
      acquired variant;
    * ``extras`` — the planned entry's extras;
    * ``required_by`` — the normalised distribution names of the plan
      entry's ``declaring_products``, resolved through
      *declaring_distributions* (a missing or empty mapping value raises
      :class:`ValueError`).

    ``primary_names`` is carried over unchanged, so accelerated entries
    are never primaries.  New entries are appended after the base
    entries in sorted-distribution order (deterministic).

    Rejections (all :class:`ValueError`, fail-closed, deterministic):

    * an acquired distribution is not in ``plan.added_requirements``;
    * a planned dependency has no acquired variant;
    * an acquired version does not satisfy the merged specifier
      (re-checked with ``SpecifierSet.contains(..., prereleases=True)``);
    * duplicate acquired distributions;
    * an accelerated distribution already present in the base lock
      (overwriting a base entry would violate the verbatim invariant).

    Pure — no I/O, no mutation of any input.
    """
    planned_by_distribution = {
        entry.distribution: entry for entry in plan.added_requirements
    }

    # -- Validate the acquired tuple against the plan -----------------------
    seen: set[str] = set()
    acquired_by_distribution: dict[str, AcquiredAcceleratedVariant] = {}
    for variant in acquired:
        if not isinstance(variant, AcquiredAcceleratedVariant):
            raise ValueError(
                "acquired must contain AcquiredAcceleratedVariant values, "
                f"got {type(variant).__qualname__}"
            )
        distribution = variant.distribution
        entry = planned_by_distribution.get(distribution)
        if entry is None:
            raise ValueError(
                f"acquired distribution {distribution!r} is not in the "
                f"accelerated plan"
            )
        if distribution in seen:
            raise ValueError(
                f"duplicate acquired distribution {distribution!r}"
            )
        seen.add(distribution)
        if entry.specifier is not None:
            if not SpecifierSet(entry.specifier).contains(
                variant.version, prereleases=True
            ):
                raise ValueError(
                    f"acquired version {variant.version!r} for "
                    f"{distribution!r} does not satisfy declared specifier "
                    f"{entry.specifier!r}"
                )
        acquired_by_distribution[distribution] = variant

    missing = sorted(
        distribution
        for distribution in planned_by_distribution
        if distribution not in seen
    )
    if missing:
        raise ValueError(
            "accelerated plan dependency without acquired variant: "
            + ", ".join(missing)
        )

    collisions = sorted(set(base_lock.locked) & set(acquired_by_distribution))
    if collisions:
        raise ValueError(
            "accelerated distribution already present in the base "
            "RuntimeLock: " + ", ".join(collisions)
        )

    # -- Base entries verbatim, then deterministic accelerated entries ------
    new_locked: dict[str, LockedDependency] = dict(base_lock.locked)

    for distribution in sorted(acquired_by_distribution):
        variant = acquired_by_distribution[distribution]
        entry = planned_by_distribution[distribution]

        required_by: set[str] = set()
        for product_id in entry.declaring_products:
            raw_distribution = declaring_distributions.get(product_id)
            if raw_distribution is None:
                raise ValueError(
                    f"no declaring distribution for product {product_id!r}"
                )
            if not isinstance(raw_distribution, str) or not raw_distribution.strip():
                raise ValueError(
                    f"declaring distribution for product {product_id!r} "
                    f"must be a non-empty string"
                )
            required_by.add(normalise_distribution_name(raw_distribution))

        new_locked[distribution] = LockedDependency(
            name=distribution,
            version=variant.version,
            wheel_path=variant.wheel_path,
            size=variant.size,
            sha256=variant.sha256,
            extras=frozenset(entry.extras),
            required_by=frozenset(required_by),
        )

    return RuntimeLock(
        locked=new_locked,
        primary_names=base_lock.primary_names,
    )


# ---------------------------------------------------------------------------
# Phases and result
# ---------------------------------------------------------------------------


class AcceleratedDeploymentPhase(str, Enum):
    """Lifecycle phase of an accelerated deployment."""

    PREPARE = "PREPARE"
    ACQUIRE = "ACQUIRE"
    RESOLVE = "RESOLVE"
    BUILD = "BUILD"
    VALIDATE = "VALIDATE"
    GATE = "GATE"
    PERSIST = "PERSIST"
    ACTIVATE = "ACTIVATE"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class AcceleratedDeploymentResult:
    """Result of applying an accelerated deployment plan.

    ``old_runtime_preserved`` is ``True`` whenever the previously active
    runtime was left intact and usable.  The M0-6 slot architecture
    never destroys the old slot — it becomes the rollback target on
    success and stays the active slot on failure/cancellation — so every
    outcome of this orchestrator preserves it; the field records that
    guarantee explicitly.
    """

    success: bool
    cancelled: bool
    phase: AcceleratedDeploymentPhase
    active_slot_id: str | None = None
    previous_slot_id: str | None = None
    reason: str | None = None
    old_runtime_preserved: bool = True


# ---------------------------------------------------------------------------
# Compatibility gate
# ---------------------------------------------------------------------------


class AcceleratedGate(Protocol):
    """Compatibility gate run inside the candidate venv before activation.

    ``check`` returns an error message when the gate fails, or ``None``
    when the candidate passes.
    """

    def check(
        self,
        candidate_python: str,
        plan: AcceleratedDeploymentPlan,
    ) -> str | None: ...


class _DefaultGate:
    """Default gate: distribution/version probes + backend compute probe.

    Runs entirely through the candidate's own Python interpreter with a
    standard-library-only probe script (no third-party imports),
    mirroring :func:`~zealfie.runtime.probe.probe_runtime_distribution`
    semantics, and verifies each planned accelerated distribution is
    installed at the planned (variant) version.

    After the distribution/version checks pass, when the plan's backend
    has a registered compute probe
    (:func:`~zealfie.acceleration.backend_probe.get_backend_compute_probe`),
    the gate executes that self-contained script with the candidate
    interpreter (stdin, bounded timeout, truncated output): the probe
    imports the accelerated framework, performs real device compute and
    compiles + launches a JIT kernel, printing ``BACKEND_COMPUTE_PROBE_OK``
    on success.  A non-zero exit, a timeout, or a missing OK marker
    fails the gate BEFORE activation (the M1-2J.1 lesson: a green
    install is not a green compute path).  A backend without a probe
    keeps the distribution/version-only behaviour.
    """

    def check(
        self, candidate_python: str, plan: AcceleratedDeploymentPlan
    ) -> str | None:
        """Return ``None`` when every planned accelerated distribution is
        installed at its planned version; otherwise an honest error
        string describing the first failure."""
        for entry in plan.added_requirements:
            variant = entry.variant
            if variant is None:
                return (
                    f"accelerated plan entry for {entry.distribution!r} "
                    f"has no selected variant"
                )
            try:
                probe = probe_runtime_distribution(
                    candidate_python, entry.distribution
                )
            except Exception as exc:
                return (
                    f"gate probe failed for {entry.distribution!r}: {exc}"
                )
            if not probe.get("installed"):
                return (
                    f"accelerated distribution {entry.distribution!r} "
                    f"not installed in candidate"
                )
            installed_version = probe.get("version")
            if installed_version != variant.version:
                return (
                    f"accelerated distribution {entry.distribution!r} "
                    f"version mismatch: expected {variant.version!r}, "
                    f"got {installed_version!r}"
                )
        if plan.backend is not None:
            probe = get_backend_compute_probe(plan.backend)
            if probe is not None:
                error = _run_backend_compute_probe(
                    candidate_python, plan.backend, probe
                )
                if error is not None:
                    return error
        return None

    def __call__(
        self, candidate_python: str, plan: AcceleratedDeploymentPlan
    ) -> str | None:
        return self.check(candidate_python, plan)


def default_accelerated_gate() -> AcceleratedGate:
    """Return the default accelerated compatibility gate."""
    return _DefaultGate()


# ---------------------------------------------------------------------------
# Backend compute probe execution (generic — no framework knowledge here)
# ---------------------------------------------------------------------------

#: Upper bound for one compute probe run inside the candidate venv.
#: The real CUDA probe finishes in seconds on supported hardware; the
#: bound only guards against pathological hangs (JIT compiler stalls,
#: deadlocked drivers) — a timeout fails the gate, never hangs the
#: deployment.
COMPUTE_PROBE_TIMEOUT_SECONDS: float = 300.0

#: Characters of combined stdout/stderr kept in gate error messages.
_COMPUTE_PROBE_OUTPUT_TAIL = 500


def _run_backend_compute_probe(
    candidate_python: str,
    backend: str,
    probe: Mapping[str, str],
    *,
    timeout: float = COMPUTE_PROBE_TIMEOUT_SECONDS,
) -> str | None:
    """Run a registered compute probe with the candidate interpreter.

    The script is fed through stdin (``<candidate_python> -``); nothing
    is written to disk and no ZeAlfie code is imported.  Returns
    ``None`` when the probe succeeded (exit 0 AND the
    ``BACKEND_COMPUTE_PROBE_OK`` marker present — an empty silent
    success is never trusted), or an honest error string otherwise.
    """
    label = str(probe.get("label") or "unnamed compute probe").strip()
    script = probe.get("script")
    if not isinstance(script, str) or not script.strip():
        return f"backend compute probe for {backend} ({label}) has no script"

    try:
        completed = subprocess.run(
            [candidate_python, "-"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            **technical_subprocess_platform_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return (
            f"backend compute probe timed out for {backend} ({label}) "
            f"after {timeout:g}s"
        )
    except OSError as exc:
        return (
            f"backend compute probe could not start for {backend} "
            f"({label}): {exc}"
        )

    combined = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if completed.returncode == 0 and "BACKEND_COMPUTE_PROBE_OK" in combined:
        return None
    return (
        f"backend compute probe failed for {backend} ({label}) "
        f"(exit {completed.returncode}): {_tail(combined)}"
    )


def _tail(text: str, limit: int = _COMPUTE_PROBE_OUTPUT_TAIL) -> str:
    """Keep the last *limit* characters of *text* (truncation marker)."""
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]

# ---------------------------------------------------------------------------
# Observational slot metadata
# ---------------------------------------------------------------------------

ACCELERATED_METADATA_SCHEMA_VERSION = 1
ACCELERATED_METADATA_FILENAME = "accelerated-metadata.json"


@dataclass(frozen=True, slots=True)
class AcceleratedSlotMetadata:
    """Observational record of one slot's accelerated closure.

    ``backend`` is the accelerator backend the closure was built for.
    ``variants`` is a sorted tuple of ``(distribution, version,
    sha256)`` triples, one per deployed accelerated variant.  Both are
    validated non-empty at construction.

    Observational only — drives no install / rollback / KEEP decision.
    """

    backend: str
    variants: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ValueError("backend must be a non-empty string")
        object.__setattr__(self, "backend", self.backend.strip())

        variants: list[tuple[str, str, str]] = []
        for variant in self.variants:
            if (
                not isinstance(variant, tuple)
                or len(variant) != 3
                or not all(
                    isinstance(part, str) and part.strip() for part in variant
                )
            ):
                raise ValueError(
                    "variants must contain (distribution, version, sha256) "
                    "triples of non-empty strings"
                )
            variants.append(tuple(part.strip() for part in variant))
        if not variants:
            raise ValueError("variants must not be empty")
        object.__setattr__(self, "variants", tuple(sorted(variants)))


def _metadata_to_dict(metadata: AcceleratedSlotMetadata) -> dict[str, object]:
    """Render slot metadata as its JSON object."""
    return {
        "backend": metadata.backend,
        "variants": [list(variant) for variant in metadata.variants],
    }


def _metadata_from_dict(payload: object) -> AcceleratedSlotMetadata | None:
    """Reconstruct slot metadata from a JSON object (lenient).

    Returns ``None`` for any malformed payload — never raises, never
    fabricates.
    """
    if not isinstance(payload, dict):
        return None

    backend = payload.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        return None

    variants_raw = payload.get("variants")
    if not isinstance(variants_raw, list):
        return None

    try:
        return AcceleratedSlotMetadata(
            backend=backend,
            variants=tuple(tuple(variant) for variant in variants_raw),
        )
    except (TypeError, ValueError):
        return None


class AcceleratedSlotMetadataStore:
    """Persistent, slot-keyed store for accelerated slot metadata.

    Storage file: ``RuntimeLayout.state_dir / accelerated-metadata.json``
    (``{"schema_version": 1, "slots": {<slot_id>: {...}}}``).

    Thread-unsafe by design — single-owner at the service layer,
    mirroring :class:`~zealfie.runtime.installed_lock.InstalledLockStore`.
    Reads are lenient (missing file, corrupt file, unknown schema,
    unknown slot, malformed entry → ``None``; never raises).  Writes are
    atomic (temp file + fsync + ``os.replace``) and validate slot ids.

    Observational only: this store drives no install / rollback / KEEP
    decision.
    """

    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout

    @property
    def path(self) -> Path:
        """Filesystem path of the persisted metadata file."""
        return self._layout.state_dir / ACCELERATED_METADATA_FILENAME

    @property
    def layout(self) -> RuntimeLayout:
        return self._layout

    # -- read ----------------------------------------------------------------

    def load_slot(self, slot_id: str) -> AcceleratedSlotMetadata | None:
        """Return the metadata for *slot_id*, or ``None`` if unknown.

        Missing slot, missing file, corrupt file, unknown schema, or
        malformed entry → ``None``.  Never raises, never fabricates.
        """
        return self._load_all().get(slot_id)

    # -- write ---------------------------------------------------------------

    def record(
        self, slot_id: str, metadata: AcceleratedSlotMetadata
    ) -> None:
        """Record the metadata for *slot_id* (replace on rewrite).

        *slot_id* is validated with the canonical slot-id validator and
        the record is written atomically.
        """
        validate_slot_id(slot_id)
        if not isinstance(metadata, AcceleratedSlotMetadata):
            raise ValueError(
                "metadata must be an AcceleratedSlotMetadata, "
                f"got {type(metadata).__qualname__}"
            )

        all_slots = self._load_all()
        all_slots[slot_id] = metadata
        self._write_all(all_slots)

    # -- internal I/O --------------------------------------------------------

    def _load_all(self) -> dict[str, AcceleratedSlotMetadata]:
        """Load the whole file, leniently.  Never raises."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}

        if not isinstance(payload, dict):
            return {}
        if payload.get("schema_version") != ACCELERATED_METADATA_SCHEMA_VERSION:
            return {}

        slots = payload.get("slots")
        if not isinstance(slots, dict):
            return {}

        result: dict[str, AcceleratedSlotMetadata] = {}
        for slot_id, slot_payload in slots.items():
            if not isinstance(slot_id, str):
                continue
            metadata = _metadata_from_dict(slot_payload)
            if metadata is not None:
                result[slot_id] = metadata
        return result

    def _write_all(self, all_slots: dict[str, AcceleratedSlotMetadata]) -> None:
        """Atomically write the whole metadata file (sorted slot ids)."""
        rendered_slots: dict[str, dict[str, object]] = {}
        for slot_id in sorted(all_slots):
            rendered_slots[slot_id] = _metadata_to_dict(all_slots[slot_id])

        payload = {
            "schema_version": ACCELERATED_METADATA_SCHEMA_VERSION,
            "slots": rendered_slots,
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            suffix=".json",
            prefix=".accelerated-metadata-",
            dir=str(path.parent),
        )
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(tmp_name, str(path))


# ---------------------------------------------------------------------------
# Failure phase classification (reason-prefix based, deterministic)
# ---------------------------------------------------------------------------


def _phase_from_failure_reason(reason: str | None) -> AcceleratedDeploymentPhase:
    """Map an ``apply_deployment_plan`` failure reason to a phase.

    Only failures whose reason carries a stable prefix are mapped
    precisely; everything else defaults to :class:`AcceleratedDeploymentPhase.BUILD`
    (the deployment was inside the materialization window).
    """
    text = reason or ""

    # The metadata-persistence message is embedded inside the
    # pre-activation gate wrapper, so check it first.
    if "accelerated metadata persistence failed:" in text:
        return AcceleratedDeploymentPhase.PERSIST

    ordered = (
        (AcceleratedDeploymentPhase.GATE, "pre-activation gate failed:"),
        (AcceleratedDeploymentPhase.ACTIVATE, "activation failed:"),
        (AcceleratedDeploymentPhase.VALIDATE, "candidate validation failed:"),
        (AcceleratedDeploymentPhase.VALIDATE, "candidate missing expected version"),
        (AcceleratedDeploymentPhase.VALIDATE, "candidate version mismatch"),
        (AcceleratedDeploymentPhase.VALIDATE, "dependency probe failed"),
        (AcceleratedDeploymentPhase.BUILD, "dependency install failed"),
        (AcceleratedDeploymentPhase.VALIDATE, "dependency "),
        (AcceleratedDeploymentPhase.PREPARE, "deployment plan is blocked:"),
        (AcceleratedDeploymentPhase.PREPARE, "stale deployment plan:"),
        (AcceleratedDeploymentPhase.PREPARE, "shared runtime is BROKEN:"),
        (AcceleratedDeploymentPhase.PREPARE, "desired state does not match registry:"),
        (AcceleratedDeploymentPhase.PREPARE, "shared-runtime conflict detected"),
        (AcceleratedDeploymentPhase.PREPARE, "DesiredComponent "),
        (AcceleratedDeploymentPhase.PREPARE, "RuntimeLock entry key "),
        (AcceleratedDeploymentPhase.BUILD, "failed to begin transaction:"),
        (AcceleratedDeploymentPhase.BUILD, "candidate slot path already exists:"),
        (AcceleratedDeploymentPhase.BUILD, "failed to create candidate venv:"),
        (AcceleratedDeploymentPhase.BUILD, "TOCTOU:"),
        (AcceleratedDeploymentPhase.BUILD, "artifact revalidation failed"),
        (AcceleratedDeploymentPhase.BUILD, "install failed for"),
        (AcceleratedDeploymentPhase.BUILD, "component "),
        (AcceleratedDeploymentPhase.BUILD, "cancel check failed:"),
    )
    for phase, prefix in ordered:
        if text.startswith(prefix):
            return phase
    return AcceleratedDeploymentPhase.BUILD


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _candidate_python(candidate_path: Path) -> Path | None:
    """Return the Python interpreter inside a candidate slot directory."""
    if sys.platform == "win32":
        candidate = candidate_path / "Scripts" / "python.exe"
    else:
        candidate = candidate_path / "bin" / "python"
    return candidate if candidate.is_file() else None


def apply_accelerated_deployment(
    *,
    accelerated_plan: AcceleratedDeploymentPlan,
    deployment_plan: "DeploymentPlan",
    registry: "ComponentRegistry",
    runtime: "SharedRuntime | None" = None,
    acquired: tuple[AcquiredAcceleratedVariant, ...],
    declaring_distributions: Mapping[str, str],
    accelerated_gate: AcceleratedGate | None = None,
    metadata_store: AcceleratedSlotMetadataStore | None = None,
    cancel_check: Callable[[], None] | None = None,
    progress_callback=None,
) -> AcceleratedDeploymentResult:
    """Apply an accelerated deployment plan to the shared runtime.

    Flow:

    a. **PREPARE** preflight — ``accelerated_plan.status`` must be
       ``PLAN_READY``, its backend must be set, its
       ``source_active_slot_id`` must equal the deployment plan's
       (incoherent inputs are rejected), and the deployment plan must
       carry a base ``RuntimeLock`` to extend;
    b. cooperative cancellation checkpoint;
    c. **ACQUIRE** — already performed by the caller (``acquired`` is
       passed in); the phase is reported only;
    d. **RESOLVE** — the base lock is extended with the accelerated
       variants via :func:`extend_runtime_lock_with_acceleration` and the
       deployment plan is rebound via ``dataclasses.replace``;
    e. **BUILD / VALIDATE / GATE / PERSIST / ACTIVATE** — the rebound
       plan is applied by the existing M0-8B engine
       (:func:`~zealfie.runtime.deployment.apply_deployment_plan`) with a
       ``pre_activate`` hook that runs the compatibility gate
       (*accelerated_gate* or the default gate) with the candidate
       Python (platform-aware ``bin/python`` /
       ``Scripts/python.exe``) and, on gate success, records
       :class:`AcceleratedSlotMetadata` under
       ``txn.candidate_slot_id`` (the final slot id in the M0-6
       architecture — slots are created at their final path and never
       renamed) when *metadata_store* is provided; a gate failure or
       metadata write failure returns an error string that makes
       ``apply_deployment_plan`` fail BEFORE activation;
    f. the resulting ``DeploymentResult`` is mapped to an
       :class:`AcceleratedDeploymentResult` (success → ``COMPLETED``;
       failure → the phase where the deployment stopped, when
       determinable from the reason, with the old runtime preserved).

    The orchestrator NEVER mutates the active runtime in place and NEVER
    installs into the active slot — all installs go to the fresh
    candidate slot created by ``apply_deployment_plan``, and the active
    pointer is never touched before activation.  A
    :class:`CooperativeCancellationError` from *cancel_check* yields
    ``cancelled=True`` with the old runtime preserved.
    """
    phase = AcceleratedDeploymentPhase.PREPARE

    # ---- (a) PREPARE preflight -------------------------------------------
    if accelerated_plan.status is not AcceleratedPlanStatus.PLAN_READY:
        return AcceleratedDeploymentResult(
            success=False,
            cancelled=False,
            phase=phase,
            reason=(
                f"accelerated plan is not ready: "
                f"{accelerated_plan.status.value}"
            ),
        )
    if accelerated_plan.backend is None:
        return AcceleratedDeploymentResult(
            success=False,
            cancelled=False,
            phase=phase,
            reason="accelerated plan has no backend",
        )
    if (
        accelerated_plan.source_active_slot_id
        != deployment_plan.source_active_slot_id
    ):
        return AcceleratedDeploymentResult(
            success=False,
            cancelled=False,
            phase=phase,
            reason=(
                "incoherent accelerated deployment inputs: accelerated "
                f"plan built from active slot "
                f"{accelerated_plan.source_active_slot_id!r} but deployment "
                f"plan built from {deployment_plan.source_active_slot_id!r}"
            ),
        )
    if deployment_plan.dependency_lock is None:
        return AcceleratedDeploymentResult(
            success=False,
            cancelled=False,
            phase=phase,
            reason=(
                "deployment plan has no dependency lock; an accelerated "
                "deployment requires a base RuntimeLock to extend"
            ),
        )

    # ---- (b) cooperative cancellation checkpoint -------------------------
    # Acquisition is performed by the caller (phase reported only).
    phase = AcceleratedDeploymentPhase.ACQUIRE
    if cancel_check is not None:
        try:
            cancel_check()
        except CooperativeCancellationError as exc:
            return AcceleratedDeploymentResult(
                success=False,
                cancelled=True,
                phase=phase,
                reason=str(exc) or "accelerated deployment cancelled",
            )
        except Exception as exc:
            return AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=phase,
                reason=f"cancel check failed: {exc}",
            )

    # ---- (d) RESOLVE -----------------------------------------------------
    phase = AcceleratedDeploymentPhase.RESOLVE
    try:
        extended_lock = extend_runtime_lock_with_acceleration(
            deployment_plan.dependency_lock,
            accelerated_plan,
            acquired,
            declaring_distributions,
        )
    except (ValueError, TypeError) as exc:
        return AcceleratedDeploymentResult(
            success=False,
            cancelled=False,
            phase=phase,
            reason=f"accelerated lock extension failed: {exc}",
        )
    rebound = replace(deployment_plan, dependency_lock=extended_lock)

    # ---- (e) BUILD / VALIDATE / GATE / PERSIST / ACTIVATE ----------------
    gate = (
        accelerated_gate
        if accelerated_gate is not None
        else default_accelerated_gate()
    )

    variants_record: tuple[tuple[str, str, str], ...] = tuple(
        sorted(
            (variant.distribution, variant.version, variant.sha256)
            for variant in acquired
        )
    )

    def pre_activate_hook(txn: "RuntimeTransaction") -> str | None:
        python = _candidate_python(txn.candidate_path)
        if python is None:
            return (
                "candidate Python interpreter not found "
                "for accelerated gate"
            )
        try:
            gate_error = gate.check(str(python), accelerated_plan)
        except Exception as exc:
            return f"accelerated gate raised: {type(exc).__name__}: {exc}"
        if gate_error is not None:
            return gate_error
        if metadata_store is not None:
            try:
                metadata_store.record(
                    slot_id=txn.candidate_slot_id,
                    metadata=AcceleratedSlotMetadata(
                        backend=accelerated_plan.backend,
                        variants=variants_record,
                    ),
                )
            except Exception as exc:
                return (
                    f"accelerated metadata persistence failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return None

    def wrapped_cancel_check() -> None:
        # Translate the canonical accelerated cancellation signal into
        # the deployment engine's cancellation signal; any other
        # exception propagates and is converted by apply_deployment_plan
        # into a failure result (its no-throw contract).
        if cancel_check is not None:
            try:
                cancel_check()
            except CooperativeCancellationError as exc:
                raise DeploymentCancelledError(
                    str(exc) or "accelerated deployment cancelled"
                ) from exc

    phase = AcceleratedDeploymentPhase.BUILD
    try:
        result = apply_deployment_plan(
            rebound,
            registry=registry,
            runtime=runtime,
            progress_callback=progress_callback,
            cancel_check=(
                wrapped_cancel_check if cancel_check is not None else None
            ),
            pre_activate=pre_activate_hook,
        )
    except DeploymentCancelledError as exc:
        # Cancellation raised from inside the delegated deployment
        # window (BUILD...ACTIVATE): interruption, not failure.
        return AcceleratedDeploymentResult(
            success=False,
            cancelled=True,
            phase=phase,
            reason=str(exc) or "accelerated deployment cancelled",
        )

    # ---- (f) Map to the accelerated result -------------------------------
    if result.success:
        return AcceleratedDeploymentResult(
            success=True,
            cancelled=False,
            phase=AcceleratedDeploymentPhase.COMPLETED,
            active_slot_id=result.active_slot_id,
            previous_slot_id=result.previous_slot_id,
        )

    return AcceleratedDeploymentResult(
        success=False,
        cancelled=False,
        phase=_phase_from_failure_reason(result.reason),
        reason=result.reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_of_path(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file (chunked read)."""
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(65536):
            sha.update(chunk)
    return sha.hexdigest()
