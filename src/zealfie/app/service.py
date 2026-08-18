"""Application-level deployment orchestration service (M0-9/M1-0A).

M0-9.1: read-only offline deployment planning (plan_offline_deployment).
M0-9.2: apply + rollback orchestration using existing runtime primitives.
M0-9.3: CLI commands delegate to this service.
M1-0A: runtime component launch (prepare_launch_plan, launch_component).
M1-1C: shared runtime dependency resolution wired into plan_offline_deployment.
M1-2B: non-blocking managed product launch (spawn_component).
M1-2A: product catalog and product-shell read model (list_products, collect_product_state, get_product_state).
M1-2D.3: user selection store and desired-component materialization
         (select_product, desired_selection,
          materialize_desired_components, desired_component_registry).
M1-2D.4.1A: registry authority — common deployment core accepts explicit
            :class:`ComponentRegistry`; launch resolves effective registry
            from :class:`SelectionStore` when the selection file exists,
            falling back to the legacy packaged registry otherwise.
M1-2D.4.1B: Remote product artifact preparation — source → exact SHA →
            staged wheel → VerifiedArtifact.  Turns a
            :class:`~zealfie.products.catalog.ProductDescriptor` with a
            ``remote_source`` into a local verified wheel artifact
            without installing it or mutating the shared runtime.
M1-2D.4.2C: Service integration — dependency acquisition before
            planning/apply in ``install_product``.  Auto-acquires
            dependency wheelhouse via ``PipWheelhouseAcquirer`` when
            the caller does not supply ``dependency_wheelhouse``.
            Acquired staging is cleaned in ``finally`` after plan/
            apply/TOCTOU/install/activation.
M1-2H: read-only accelerated GPU deployment plan preview
       (``build_accelerated_deployment_plan``) — no writes, no
       network, no runtime mutation.
M1-2I: transactional accelerated deployment
       (``install_accelerated_runtime``) — acquire -> resolve ->
       build -> validate -> gate -> persist -> activate through the
       M1-2I engine, extending the current full desired runtime
       (KEEP semantics).  No provenance or selection writes (products
       unchanged); the engine's observational metadata record is the
       only new persistent state.
M1-2J Phase D: real artifact source — the service defaults
       (``build_accelerated_deployment_plan`` variant catalog and
       ``install_accelerated_runtime`` acquirer) are wired to the
       packaged accelerated artifact manifest; explicit injection
       always wins.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
import logging
import tempfile
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from packaging.utils import canonicalize_name

from zealfie.acceleration import (
    AcceleratedArtifactAcquirer,
    AcceleratedDeploymentPlan,
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentResult,
    AcceleratedGate,
    AcceleratedPlanStatus,
    AcceleratedSlotMetadata,
    AcceleratedSlotMetadataStore,
    AcceleratedVariantCatalog,
    CooperativeCancellationError,
    HardwareCompatibilityStatus,
    PlannedKeepProduct,
    apply_accelerated_deployment,
    build_acceleration_preservation_plan,
    build_accelerated_deployment_plan,
    default_accelerated_gate,
    default_manifest_artifact_acquirer,
    default_manifest_variant_catalog,
)
from zealfie.acceleration.compatibility import (
    evaluate_acceleration_compatibility,
)
from zealfie.app.progress import InstallPhase, InstallProgress, PHASE_PERCENT
from zealfie.app.updates import (
    ProductUpdateResult,
    UpdateStatus,
    check_product_update as _check_product_update,
)
from zealfie.compatibility import CompatibilityReport, evaluate_wheels
from zealfie.components.model import ComponentDefinition
from zealfie.components.registry import ComponentRegistry, UnknownComponentError, default_registry
from zealfie.dependencies import (
    DependencyResolutionError,
    PipWheelhouseAcquirer,
    build_acquisition_request,
    resolve_runtime_dependencies,
)
from zealfie.dependencies.acquisition import (
    AcquisitionTransportError,
    DependencyAcquisitionResult,
)
from zealfie.dependencies.models import (
    ExtraNotFound,
    MetadataError,
    RuntimeLock,
)

from zealfie.launching import (
    EntryPointScriptNotFoundError,
    LaunchPlan,
    LaunchResult,
    SpawnedLaunch,
    execute_launch_plan,
    resolve_script,
    spawn_launch_plan,
)
from zealfie.products.catalog import (
    ProductCatalog,
    ProductDescriptor,
    UnknownProductError,
    default_catalog,
)
from zealfie.products.policy import (
    ProductPolicy,
    ProductPolicyStore,
    effective_ref,
)
from zealfie.products.selection import (
    CorruptSelectionError,
    DesiredProductSelection,
    bootstrap_selection_from_legacy_registry,
    SelectionStore,
    desired_component_registry as _desired_component_registry,
    materialize_desired_components as _materialize_desired_components,
    validate_selection_against_catalog as _validate_selection_against_catalog,
)
from zealfie.products.state import (
    ProductShellState,
    ProductState,
    collect_product_state,
    get_product_state,
)
from zealfie.releases.manifest import (
    ReleaseManifestError,
    parse_release_manifest_file,
)
from zealfie.releases.model import (
    ArtifactEntry,
    HostTarget,
    ReleaseManifest,
    VerifiedArtifact,
)
from zealfie.releases.resolver import ReleaseResolutionError, resolve_local_release
from zealfie.releases.verifier import ArtifactRejectionError, verify_artifact
from zealfie.runtime.artifact_cache import ArtifactCacheStore, runtime_cache_gc
from zealfie.runtime.deployment import apply_deployment_plan
from zealfie.runtime.gc import (
    GcResult,
    GcStatus,
    apply_gc_plan,
    build_gc_plan,
)
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.mutation_lock import (
    OPERATION_GPU_INSTALL,
    OPERATION_PRODUCT_INSTALL,
    OPERATION_PRODUCT_UPDATE,
    RuntimeMutationLock,
)
from zealfie.runtime.model import (
    DeploymentResult,
    RuntimeState,
    RuntimeStatus,
)
from zealfie.runtime.provenance import ProductProvenance, ProductProvenanceStore
from zealfie.runtime.installed_lock import (
    InstalledLockStore,
    InstalledRuntimeLock,
    installed_lock_from_runtime_lock,
)
from zealfie.runtime.planning import (
    ORIGIN_KEEP,
    ORIGIN_UPDATE,
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    build_deployment_plan,
)
from zealfie.runtime.probe import probe_runtime_distribution
from zealfie.runtime.startup_health import (
    StartupHealthResult,
    confirm_and_record_startup_health,
)
from zealfie.host import (
    AccelerationRecommendation,
    GpuSetupIntent,
    HostCapabilities,
    HostProber,
    HostReasonCode,
    RecommendationStatus,
    build_gpu_setup_intent,
    recommend,
)
from zealfie.sources import (
    RemoteSource,
    ResolvedSource,
    SourceRefResolver,
    resolve_source,
)
from zealfie.sources.acquisition import (
    ArchiveFetcher,
    acquire_source,
    build_wheel_from_staged,
)


logger = logging.getLogger(__name__)


class OfflineReleaseError(ValueError):
    """Raised when an offline release directory cannot be resolved.

    Wraps all lower-level failures (missing manifests, parse errors,
    artifact resolution failures, extra unknown manifests) into a
    single application-layer error type.
    """


# ---------------------------------------------------------------------------
# M1-0A: Launch errors
# ---------------------------------------------------------------------------


class LaunchPreparationError(RuntimeError):
    """Raised when a launch cannot be prepared.

    Each subclass carries an *exit_code* that the CLI can use when
    surfacing the error to the user.  All messages are human-readable
    and free of Python traceback details.
    """


class ComponentNotInstalledError(LaunchPreparationError):
    """The component's distribution is not installed in the runtime."""


class LaunchContractNotSatisfiedError(LaunchPreparationError):
    """None of the component's declared entry-point contracts were found
    in the installed distribution."""


class LaunchScriptNotFoundError(LaunchPreparationError):
    """The matched entry-point script does not exist inside the runtime."""


# ---------------------------------------------------------------------------
# M1-2D.4.1B: Product install preparation errors
# ---------------------------------------------------------------------------


class ProductInstallPreparationError(RuntimeError):
    """Raised when remote product artifact preparation fails.

    Wraps all lower-level failures (unknown product, missing remote
    source, resolution failure, acquisition failure, build failure,
    verification failure) into a single application-layer error family.
    """


class RemoteSourceUnavailableError(ProductInstallPreparationError):
    """Raised when a product has no ``remote_source`` metadata.

    Preparation from remote source requires the product descriptor to
    carry a :class:`~zealfie.sources.RemoteSource`.  This error is
    raised before any resolver, fetcher, or build call is made so that
    callers can detect the condition early and provide a clear message.
    """


class ProductChannelUnavailableError(ProductInstallPreparationError):
    """Raised when a follow policy names a channel the product does not
    declare in its catalog descriptor (M1-2F Phase 5).

    This is a **fail-closed** preflight signal raised before any resolver,
    fetcher, build, apply, or network call.  ``DEFAULT_CHANNEL_REFS`` is a
    default mapper only; it never grants a channel to a product that did not
    declare it in ``manifests/products.toml``.

    ``pin`` policies never raise this — pin resolves the exact immutable
    SHA and ignores the discovery channel entirely (the product id and
    remote source are still validated by the normal install path).
    """

    def __init__(
        self,
        *,
        product_id: str,
        channel: str,
        available: tuple[str, ...] = (),
    ) -> None:
        self.product_id = str(product_id)
        self.channel = str(channel)
        self.available = tuple(available)
        avail = ", ".join(self.available) or "none"
        super().__init__(
            f"channel {self.channel!r} is not available for product "
            f"{self.product_id!r} (available channels: {avail})"
        )



class ProductDeploymentPlanningError(RuntimeError):
    """Raised when bridging prepared product artifacts to a deployment
    plan fails validation.

    This is a planning-only error: no install, no apply, no runtime
    mutation, no selection persistence occurs.  The error is raised
    before any deployment planner call when the input is structurally
    invalid (empty, duplicates, mismatches).
    """


class ProductCompatibilityBlockedError(RuntimeError):
    """Raised when the prepared product candidate set fails the
    interoperability compatibility gate before activation.

    The gate evaluates the full prepared candidate set (all primary
    prepared product wheels) *before* :func:`apply_deployment_plan`, so
    the shared runtime and the selection store are never touched on this
    path.  A blocking report is ``INCOMPATIBLE`` or
    ``METADATA_UNAVAILABLE``.

    Carries the full :class:`~zealfie.compatibility.CompatibilityReport`
    (including stable machine-readable reason codes such as
    ``API_VERSION_MISMATCH``, ``MISSING_REQUIRED_CAPABILITY``, or
    ``PROVIDER_METADATA_UNAVAILABLE``) so callers can surface precise
    diagnostics without re-running the evaluation.
    """

    def __init__(self, report: CompatibilityReport) -> None:
        self.report = report
        super().__init__(_compatibility_blocked_message(report))


def _compatibility_blocked_message(report: CompatibilityReport) -> str:
    """Return a stable, human-readable reason for a blocked candidate set.

    Names the stable reason code for every blocking finding; it never
    depends on long prose so API/CLI consumers can rely on the codes.
    """
    blocking = [f for f in report.findings if f.blocking]
    if not blocking:
        # Defensive: verdict is blocked but no blocking finding recorded.
        return (
            "compatibility gate blocked activation of the prepared product "
            f"set (verdict {report.verdict.value!r})"
        )
    details = "; ".join(
        f"{f.code}" + (f": {f.message}" if f.message else "")
        for f in blocking
    )
    return (
        "compatibility gate blocked activation of the prepared product set: "
        f"{details}"
    )


def _surface_compatibility_diagnostics(report: CompatibilityReport) -> None:
    """Log structured, non-blocking compatibility diagnostics.

    Degraded / non-blocking findings (optional provider absent, optional
    capability missing, unreferenced metadata-unavailable provider) are
    informational only and never change control flow.  They are surfaced
    via the logger with stable reason codes so they are never silently
    swallowed.
    """
    non_blocking = [f for f in report.findings if not f.blocking]
    if not non_blocking:
        return
    codes = sorted({f.code for f in non_blocking})
    logger.info(
        "compatibility gate: prepared candidate set is compatible with "
        "non-blocking diagnostics (codes: %s)",
        ", ".join(codes),
    )


class ProductDependencyAcquisitionError(RuntimeError):
    """Raised when dependency acquisition for a product fails.

    Carries the original cause via ``__cause__`` so callers can
    inspect the underlying failure without traceback leakage.
    """


class ProductUpdateNotApplicableError(RuntimeError):
    """Raised when an update cannot be applied because the product is not
    in an :attr:`UpdateStatus.UPDATE_AVAILABLE` state.

    This is a **preflight** error: it is raised after the read-only update
    check determined that no update should be applied, and before any
    archive fetch, wheel build, deployment planning, apply, or
    selection/provenance mutation.  No runtime, provenance, or selection
    state is touched.

    The original preflight result is carried on :attr:`result` (including
    the check ``error`` for :attr:`UpdateStatus.CHECK_FAILED`), and the
    check outcome on :attr:`status`, so callers can present a clear reason
    without re-running the check.
    """

    def __init__(self, result: ProductUpdateResult) -> None:
        self.result = result
        self.status = result.status
        super().__init__(_update_not_applicable_message(result))


def _update_not_applicable_message(result: ProductUpdateResult) -> str:
    """Return a stable, human-readable reason for a non-applicable update.

    Never returns a bare enum value: every message names the product and
    the concrete situation so API/CLI consumers can surface it directly.
    """
    product_id = result.product_id
    status = result.status
    if status is UpdateStatus.UP_TO_DATE:
        return f"product {product_id!r} is already up to date"
    if status is UpdateStatus.PROVENANCE_UNKNOWN:
        return (
            f"product {product_id!r} has no active installed provenance; "
            "cannot determine an update target"
        )
    if status is UpdateStatus.CHECK_FAILED:
        reason = result.error or "unknown check failure"
        return f"update check failed for product {product_id!r}: {reason}"
    if status is UpdateStatus.CHECKING:
        return f"update check for product {product_id!r} is still in progress"
    # UpdateStatus.NOT_CHECKED (and any future status) fall through here.
    return f"product {product_id!r} has not been checked for updates"


# ---------------------------------------------------------------------------
# M1-2D.4.1B: Prepared product artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedProductArtifact:
    """A product whose remote source has been resolved, built into a
    wheel, and verified — ready to hand off to the deployment pipeline.

    No installation has occurred.  The shared runtime and selection
    store are untouched.

    ``policy`` is the exact discovery policy that produced this artifact
    (``None`` when unknown, e.g. for KEEP products whose active provenance
    predates Phase 4).  It is metadata for provenance persistence only and
    never drives resolution here.
    """

    product_id: str
    component_id: str
    resolved_source: ResolvedSource
    wheel_path: Path
    verified_artifact: VerifiedArtifact
    policy: ProductPolicy | None = None
    # Service-level intent that produced this artifact (ZA-M1-3A.3 LOT E):
    # "keep" (preserved at exact installed identity), "update" (explicit
    # update target) or "install" (fresh install, the default).  Drives
    # honest progress wording only - never resolution or transaction
    # behaviour.
    origin: str = "install"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ZeAlfieService:
    """Application-level deployment orchestration for ZeAlfie.

    Provides ``plan_offline_deployment`` for preview/planning,
    ``apply_offline_deployment`` for orchestrated apply,
    ``rollback_runtime`` for rollback, and
    ``prepare_launch_plan`` / ``launch_component`` for runtime launch
    (M1-0A).

    M1-2B adds ``spawn_component`` for non-blocking launch.
    M1-2A adds ``list_products``, ``collect_product_state``, and
    ``get_product_state`` for the product-shell read model.
    M1-2D.3 adds ``select_product``, ``desired_selection``,
    ``materialize_desired_components``, and
    ``desired_component_registry`` for the user selection store and
    desired-component materialization.

    M1-2D.4.0 adds ``bootstrap_desired_selection`` for legacy-preserving
    one-shot initialisation of the selection file from the packaged
    ``ComponentRegistry``.  ``select_product`` guarantees the bootstrap
    before any additive mutation.

    M1-2D.4.1A adds explicit-registry variants to the deployment core
    (``_resolve_release_set_for_registry``,
    ``_plan_deployment_for_registry``,
    ``_apply_deployment_for_registry``,
    ``_prepare_launch_plan_for_registry``) so that future install
    orchestration can pass a candidate desired registry.  Launch now
    resolves the effective registry from the ``SelectionStore`` when
    the selection file exists, falling back to the legacy packaged
    registry otherwise.

    M1-2D.4.1B adds ``prepare_product_artifact`` for remote-source
    → exact-SHA → staged-wheel → VerifiedArtifact preparation,
    producing a :class:`PreparedProductArtifact` ready for the
    existing deployment pipeline without installing or mutating the
    shared runtime.

    The **managed** set in the product shell is now driven by the
    ``SelectionStore`` (what the user wants), not by the packaged
    ``ComponentRegistry``.  The ``ComponentRegistry`` remains the
    deployment/launch contract for pre-D4 paths.

    M1-2H adds ``build_accelerated_deployment_plan`` — the read-only
    accelerated GPU deployment plan preview wired to the CLI
    (``zealfie system gpu-plan``) and the GUI configure panel.

    M1-2I adds ``install_accelerated_runtime`` — the transactional
    accelerated deployment (acquire -> resolve -> build -> validate ->
    gate -> persist -> activate).  The default production path stays
    fail-closed: the default acquirer refuses (no real accelerated
    artifact source is configured yet), and a non-``PLAN_READY`` plan
    performs no acquisition and no runtime work.

    ZA-M1-3A.3 (LOT C+D) adds the shared verified artifact cache:
    KEEP products, dependency wheelhouse entries, and accelerated GPU
    wheels are stored content-addressed under
    ``<runtime_root>/cache/artifacts`` (outside slots/) and reused only
    after byte-level digest re-verification; a bounded best-effort cache
    GC prunes artifacts not referenced by the persisted slot state
    (ACTIVE + PREVIOUS always survive) after every successful
    transaction.  The cache is an optimization, never an authority: any
    miss or mismatch re-acquires normally.

    Dependencies (registry, runtime, catalog, host, selection_store)
    are injectable so tests can supply synthetic instances.
    """

    def __init__(
        self,
        *,
        registry: ComponentRegistry | None = None,
        runtime: SharedRuntime | None = None,
        catalog: ProductCatalog | None = None,
        host: HostTarget | None = None,
        selection_store: SelectionStore | None = None,
        acquirer: object | None = None,
        provenance_store: ProductProvenanceStore | None = None,
        installed_lock_store: InstalledLockStore | None = None,
        policy_store: ProductPolicyStore | None = None,
        capability_collector: object | None = None,
        recommender: object | None = None,
        auto_gc: bool = True,
    ) -> None:
        self._registry = registry or default_registry()
        self._runtime = runtime or SharedRuntime()
        self._catalog = catalog or default_catalog()
        self._host = host or HostTarget.from_current_host()
        self._selection_store = selection_store or SelectionStore()
        # M1-2D.4.2C: Injectable acquirer for test isolation.
        if acquirer is not None:
            self._acquirer = acquirer
        else:
            self._acquirer = PipWheelhouseAcquirer()
        # M1-2E E.1: installed-product provenance store.  Explicit injection
        # wins; otherwise derive from the runtime's layout when available
        # (real SharedRuntime).  Fake runtimes without a layout leave
        # provenance disabled rather than writing to the real user runtime.
        if provenance_store is not None:
            self._provenance_store = provenance_store
        else:
            layout = getattr(self._runtime, "layout", None)
            self._provenance_store = (
                ProductProvenanceStore(layout) if layout is not None else None
            )
        # M1-2F Phase 4 corrective: installed-runtime lock read model.
        # Explicit injection wins; otherwise derive from the runtime's layout
        # when available (real SharedRuntime).  Fake runtimes without a layout
        # leave it disabled rather than writing to the real user runtime.
        if installed_lock_store is not None:
            self._installed_lock_store = installed_lock_store
        else:
            layout = getattr(self._runtime, "layout", None)
            self._installed_lock_store = (
                InstalledLockStore(layout) if layout is not None else None
            )
        # M1-2F Phase 3 (F.2): per-product channel/follow/pin configuration.
        # Explicit injection wins; otherwise a default store reads the
        # platform-appropriate user config path lazily.
        self._policy_store = (
            policy_store if policy_store is not None else ProductPolicyStore()
        )
        # M1-2G: host acceleration discovery.  The capability collector and
        # recommender are injectable for hermetic tests; production defaults
        # to the read-only HostProber and the pure recommend() function.
        self._capability_collector = capability_collector or HostProber().collect
        self._recommender = recommender or recommend
        # ZA-M1-3A.3: bounded best-effort GC of historical slots after
        # every successful transaction.  ``False`` disables it (the
        # manual ``runtime gc`` / ``runtime gc-plan`` commands remain
        # available either way).  Default True: in steady-state
        # operation only ACTIVE + PREVIOUS slots persist.
        self._auto_gc = bool(auto_gc)
        # ZA-M1-3A.3 LOT C+D: shared verified artifact cache, derived from
        # the runtime layout (``<runtime_root>/cache/artifacts``, outside
        # slots/).  Layout-less test doubles leave it disabled (None):
        # every cache path then behaves exactly as before.
        layout_for_cache = getattr(self._runtime, "layout", None)
        self._artifact_cache = (
            ArtifactCacheStore(layout_for_cache.artifact_cache_dir)
            if layout_for_cache is not None
            else None
        )

    # ------------------------------------------------------------------
    # ZA-M1-2L: runtime mutation lease helper
    # ------------------------------------------------------------------

    def _runtime_mutation_lock(self) -> RuntimeMutationLock | None:
        """Derive the mutation lock from the runtime layout (D1).

        Production services always hold a real :class:`SharedRuntime` with a
        layout; the lock is scoped to that runtime root.  Returns ``None``
        only when the injected runtime exposes no layout (test doubles).
        The structural fail-closed guard lives in
        :func:`~zealfie.runtime.deployment.apply_deployment_plan` (and the
        other lock-owning entry points): a layout-less runtime raises
        :class:`RuntimeMutationLockError` there instead of mutating, so a
        fake without a layout can no longer reach a real mutation, and the
        low-level D2 contract (:func:`RuntimeMutationLock.require_lease`)
        still fails closed inside the engine as a second line of defence.
        """
        layout = getattr(self._runtime, "layout", None)
        if layout is None:
            return None
        return RuntimeMutationLock(layout.root)

    # ------------------------------------------------------------------
    # ZA-M1-3A.3: bounded best-effort auto-GC after successful
    # transactions
    # ------------------------------------------------------------------

    def _runtime_gc_best_effort(self) -> GcResult | None:
        """Run a bounded best-effort GC of historical slots (ZA-M1-3A.3).

        Called only after a successful transaction that activated a new
        slot.  Plans and applies the GC for the runtime root, which
        deletes only slots outside {active, previous} (the rollback
        target is never touched) together with their store entries.

        Every failure mode is contained: a BLOCKED plan (e.g. an
        unrelated repair state) skips destructively, and any exception
        — including a busy mutation lock or a Windows file-locked
        cleanup — is logged as a warning and never changes the outcome
        of the transaction that called this.  Per-slot cleanup failures
        inside the apply engine are collected on the result (partial,
        non-destructive cleanup; the next transaction retries).

        Returns the :class:`GcResult` when a GC was attempted, ``None``
        when auto-GC is disabled or the runtime exposes no layout
        (test doubles).
        """
        if not self._auto_gc:
            return None
        layout = getattr(self._runtime, "layout", None)
        if layout is None:
            return None
        result: GcResult | None = None
        try:
            plan = build_gc_plan(layout.root)
            if plan.status == GcStatus.BLOCKED:
                logger.warning(
                    "auto-GC skipped: runtime GC plan is BLOCKED (%s)",
                    "; ".join(plan.blocking_reasons),
                )
            else:
                result = apply_gc_plan(layout.root, plan)
                if result.deleted_slots:
                    logger.info(
                        "auto-GC deleted historical slots %s "
                        "(reclaimed ~%d bytes estimated)",
                        ", ".join(result.deleted_slots),
                        result.reclaimed_bytes,
                    )
                if result.errors:
                    logger.warning(
                        "auto-GC completed with errors: %s",
                        "; ".join(result.errors),
                    )
            # ZA-M1-3A.3 LOT D: after slot GC, prune unreferenced cache
            # artifacts (best-effort; the just-written state protects the
            # transaction's own artifacts).
            self._artifact_cache_gc_best_effort(layout)
            return result
        except Exception:
            logger.warning(
                "auto-GC failed; the completed transaction is unaffected",
                exc_info=True,
            )
            return None

    def _artifact_cache_gc_best_effort(self, layout) -> None:
        """Bounded best-effort GC of unreferenced cache artifacts (LOT D).

        Deletes only artifacts NOT referenced by the persisted slot state
        stores (product provenance wheel digests, installed-lock
        dependency identities, accelerated metadata variant digests) —
        so the ACTIVE + PREVIOUS protection set and the just-completed
        transaction's artifacts always survive.  Every failure mode is
        logged, never raised, and never changes the transaction outcome.
        """
        if self._artifact_cache is None:
            return
        try:
            result = runtime_cache_gc(layout.root)
        except Exception as exc:
            logger.warning("artifact cache GC failed: %s", exc)
            return
        if result.deleted:
            logger.info(
                "artifact cache GC deleted %d unreferenced artifact(s) "
                "(reclaimed ~%d bytes)",
                len(result.deleted),
                result.reclaimed_bytes,
            )
        for error in result.errors:
            logger.warning("artifact cache GC: %s", error)

    # ------------------------------------------------------------------
    # ZA-M1-4 LOT A: fresh-startup runtime health confirmation
    # ------------------------------------------------------------------

    def confirm_startup_runtime_health(self) -> StartupHealthResult | None:
        """Confirm the persisted ACTIVE runtime health (ZA-M1-4 LOT A).

        Runs the fresh-startup health checks and, when healthy, records the
        startup-health confirmation (best-effort, atomic write).  Never
        raises: any error is logged and ``None`` is returned.  Callers
        (service bootstrap / GUI) invoke this explicitly -- it is NOT
        called from ``__init__`` (to avoid surprising test doubles).
        """
        layout = getattr(self._runtime, "layout", None)
        if layout is None:
            return None
        try:
            return confirm_and_record_startup_health(layout.root)
        except Exception:
            logger.warning(
                "startup runtime health confirmation failed (best-effort)",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # ZA-M1-3A.3 LOT C.2: proven dependency identities + acquisition
    # ------------------------------------------------------------------

    def _proven_dependency_requirements(self) -> tuple[tuple[str, str], ...]:
        """Return ``(name, version)`` identities from the active installed lock.

        The active slot's installed-runtime lock proves which dependency
        distributions were installed by the previous transaction.  These
        identities let the wheelhouse acquirer reuse exact cached wheels
        instead of re-downloading them.  ``()`` when no lock is available
        (layout-less test doubles, ABSENT runtime, unknown slot) — the
        acquirer then behaves exactly as before (normal pip acquisition).
        """
        store = self._installed_lock_store
        if store is None:
            return ()
        lock = store.load_active()
        if lock is None:
            return ()
        return tuple(
            sorted(
                (dependency.name, dependency.version)
                for dependency in lock.dependencies.values()
            )
        )

    def _acquire_product_dependencies(
        self,
        prepared_artifact: PreparedProductArtifact,
        staging_dir: Path,
        *,
        proven: tuple[tuple[str, str], ...],
    ) -> bool:
        """Acquire dependencies for one prepared product wheel.

        When a shared artifact cache is available AND the active installed
        lock proves dependency identities, the acquirer may satisfy those
        identities from the cache (fail-closed, digest-verified) instead of
        the network; otherwise the call is byte-identical to the
        pre-cache behaviour.

        Returns ``True`` when the acquirer reported that the wheelhouse was
        seeded from the verified artifact cache (observational, ZA-M1-3A.3
        LOT E) - the caller may surface an honest "reusing cached
        dependencies" message.  Test doubles that return ``None`` yield
        ``False``.
        """
        desc = self._catalog.get(prepared_artifact.product_id)
        req = build_acquisition_request(
            prepared_artifact.wheel_path,
            active_extras=frozenset(desc.required_extras),
        )
        result: DependencyAcquisitionResult | None
        if self._artifact_cache is not None:
            # Pass the cache whenever it exists: with a proven closure the
            # acquirer may satisfy identities locally (0 network for hits);
            # without one it still FEEDS the verified result into the cache
            # so the next transaction can reuse it.
            result = self._acquirer.acquire(
                req,
                staging_dir=staging_dir,
                cache=self._artifact_cache,
                proven_requirements=proven,
            )
        else:
            result = self._acquirer.acquire(req, staging_dir=staging_dir)
        return bool(getattr(result, "seeded_from_cache", False))

    # ------------------------------------------------------------------
    # M1-2G: Host acceleration discovery (read-only)
    # ------------------------------------------------------------------

    def collect_host_capabilities(self) -> HostCapabilities:
        """Return a read-only observation of the host platform and GPUs.

        Delegates to the injected capability collector (default:
        :class:`~zealfie.host.HostProber`).  Never mutates the system.
        """
        return self._capability_collector()

    def get_acceleration_recommendation(
        self,
        capabilities: HostCapabilities | None = None,
    ) -> AccelerationRecommendation:
        """Interpret host capabilities into an acceleration recommendation.

        Observation -> interpretation, via the injected recommender
        (default: :func:`~zealfie.host.recommend`).  Read-only.

        When *capabilities* is ``None``, a fresh observation is collected
        (preserving the original convenience behavior for callers such as
        the GUI).  Callers that already hold an observation — such as the
        CLI, which prints both the capabilities and the recommendation —
        should pass it in so the recommendation is derived from the exact
        same observation that is displayed and host probes run only once.
        """
        if capabilities is None:
            capabilities = self.collect_host_capabilities()
        recommendation = self._recommender(capabilities)
        # ZA-M1-3A.2: the recommendation is an observation -> interpretation
        # of the HOST only; runtime readiness is a separate, slot-state fact.
        # When the host side says OFFER_SETUP but the ACTIVE slot already
        # carries a validated accelerated runtime, the honest user-facing
        # status is ALREADY_READY (never derived from backend probes alone).
        # Injected recommenders may return anything (tests, fakes): the
        # overlay only applies to real AccelerationRecommendation values.
        if (
            isinstance(recommendation, AccelerationRecommendation)
            and recommendation.status is RecommendationStatus.OFFER_SETUP
            and self.acceleration_runtime_ready()
        ):
            recommendation = AccelerationRecommendation(
                status=RecommendationStatus.ALREADY_READY,
                backend=recommendation.backend,
                reason_code=HostReasonCode.ACCELERATION_ALREADY_READY,
                reason=(
                    "an accelerated runtime is installed and validated in "
                    "the active runtime slot"
                ),
                gpus=recommendation.gpus,
            )
        return recommendation

    def acceleration_runtime_ready(self) -> bool:
        """Return whether the ACTIVE slot carries a validated accelerated runtime.

        ZA-M1-3A.2 (GPU readiness): readiness is derived ONLY from the
        active slot's persisted state — never from host probes alone
        (a visible GPU + driver is OFFER_SETUP territory, not readiness):

        1. the runtime must expose a layout whose active pointer names a
           READY slot;
        2. that slot must have a valid accelerated-metadata record
           (:class:`~zealfie.acceleration.deployment.AcceleratedSlotMetadataStore`);
        3. every recorded variant ``(distribution, version, sha256)``
           must be verified installed at the recorded version inside the
           slot's own interpreter (real distribution probe).

        Read-only; any missing, corrupt, or unverifiable state fails
        closed to ``False`` (the GUI then keeps the honest offer).
        """
        return self._validated_active_accelerated_metadata() is not None

    def _validated_active_accelerated_metadata(
        self,
    ) -> AcceleratedSlotMetadata | None:
        """Return the ACTIVE slot's validated accelerated metadata, or None.

        Exactly the readiness logic (layout/READY/load_slot + per-variant
        distribution probe), but returns the
        :class:`~zealfie.acceleration.deployment.AcceleratedSlotMetadata`
        instead of a bool.  ``None`` whenever the slot is missing, corrupt,
        or unverifiable — fail-closed, never invents a closure.
        """
        layout = getattr(self._runtime, "layout", None)
        if layout is None:
            return None
        status = self._runtime.status()
        if status.state is not RuntimeState.READY or status.active_slot_id is None:
            return None
        try:
            metadata = AcceleratedSlotMetadataStore(layout).load_slot(
                status.active_slot_id
            )
        except Exception:
            return None
        if metadata is None:
            return None
        slot_path = layout.slot_path(status.active_slot_id)
        if sys.platform == "win32":
            python = slot_path / "Scripts" / "python.exe"
        else:
            python = slot_path / "bin" / "python"
        if not python.is_file():
            return None
        for distribution, version, _digest in metadata.variants:
            try:
                probe = probe_runtime_distribution(str(python), distribution)
            except Exception:
                return None
            if not probe.get("installed"):
                return None
            if probe.get("version") != version:
                return None
        return metadata

    def prepare_gpu_setup_intent(
        self,
        recommendation: AccelerationRecommendation | None = None,
    ) -> GpuSetupIntent:
        """Return a preparatory, no-mutation GPU setup intent for the GUI.

        When *recommendation* is supplied (the GUI already rendered it), the
        intent is derived from that exact recommendation with no fresh
        hardware observation.  When omitted, a recommendation is recomputed
        for backwards compatibility with callers that hold no recommendation
        yet.

        This prepares intent only: it never installs a CUDA toolkit, driver,
        or accelerated runtime, and never mutates the system.
        """
        if recommendation is None:
            recommendation = self.get_acceleration_recommendation()
        return build_gpu_setup_intent(recommendation)

    # ------------------------------------------------------------------
    # M1-2H: Read-only accelerated deployment plan preview
    # ------------------------------------------------------------------

    def build_accelerated_deployment_plan(
        self,
        *,
        capabilities: HostCapabilities | None = None,
        recommendation: AccelerationRecommendation | None = None,
        variant_catalog: AcceleratedVariantCatalog | None = None,
    ) -> AcceleratedDeploymentPlan:
        """Build the read-only accelerated GPU deployment plan (M1-2H).

        Collects the host observation (unless *capabilities* is
        provided) and derives the acceleration recommendation (unless
        *recommendation* is provided), reads the current runtime
        status, documents KEEP products verbatim from active
        provenance and the installed runtime lock, and delegates to
        the pure
        :func:`~zealfie.acceleration.planning.build_accelerated_deployment_plan`.

        Callers that already hold both the capabilities observation
        and the recommendation derived from it — such as the GUI
        preview — should pass both, so no second hardware observation
        occurs.  Supplying only *recommendation* still triggers a
        fresh capability observation (the recommendation alone is not
        an observation), and supplying neither triggers exactly one.

        **100% read-only:** no writes, no network, no runtime
        mutation, no selection persistence, no install.  KEEP identity
        is never re-resolved and never fabricated: active provenance
        is authoritative for ``version`` / ``commit_sha`` /
        ``wheel_sha256``; products known only from the installed lock
        degrade ``commit_sha`` / ``wheel_sha256`` to ``None``; when no
        provenance or installed-lock record exists at all,
        ``keep_products`` may be empty.  Every KEEP entry carries a
        ``source`` tag (``"provenance"`` or ``"installed_lock"``)
        documenting which read-only store supplied it.

        *variant_catalog* defaults to
        :func:`~zealfie.acceleration.acquisition.default_manifest_variant_catalog`
        — the real variant catalog derived from the packaged artifact
        manifest (ZA-M1-2J Phase D).  Explicit injection always wins;
        the pure empty
        :func:`~zealfie.acceleration.variants.default_variant_catalog`
        remains available for unit tests.  The platform tag comes from
        the service host target (default:
        :meth:`~zealfie.releases.model.HostTarget.from_current_host`).

        Returns
        -------
        AcceleratedDeploymentPlan
            The read-only plan.  ``status`` is one of
            ``NO_ACCELERATED_REQUIREMENTS`` / ``PLAN_READY`` /
            ``BLOCKED`` / ``UNKNOWN``; blocked plans are honest
            previews, not errors.
        """
        if capabilities is None:
            capabilities = self.collect_host_capabilities()
        if recommendation is None:
            recommendation = self.get_acceleration_recommendation(capabilities)
        runtime_status = self._runtime.status()
        keep_products = self._keep_products_for_acceleration_plan()
        if variant_catalog is None:
            variant_catalog = default_manifest_variant_catalog()
        return build_accelerated_deployment_plan(
            catalog=self._catalog,
            capabilities=capabilities,
            recommendation=recommendation,
            runtime_status=runtime_status,
            variant_catalog=variant_catalog,
            keep_products=keep_products,
            platform_tag=self._host.platform_tag,
        )

    def _keep_products_for_acceleration_plan(
        self,
    ) -> dict[str, PlannedKeepProduct]:
        """Document KEEP products verbatim for the acceleration plan.

        Two read-only sources, merged deterministically:

        1. active provenance (authoritative): ``product_id``,
           ``version``, ``commit_sha`` and ``wheel_sha256`` are copied
           verbatim — never re-resolved, never revalidated — with
           ``source="provenance"``;
        2. installed-runtime lock (fallback): a primary installed
           distribution whose name maps to a catalog product without a
           provenance record contributes ``product_id`` + installed
           ``version`` with ``commit_sha`` / ``wheel_sha256`` degraded
           to ``None`` (planning never fabricates) and
           ``source="installed_lock"``.

        Products from provenance are kept even when absent from the
        catalog (verbatim documentation).  Returns an empty mapping
        when neither store has records — the plan then honestly
        carries no KEEP products.
        """
        keeps: dict[str, PlannedKeepProduct] = {}
        for product_id, prov in self.active_provenance().items():
            keeps[product_id] = PlannedKeepProduct(
                product_id=product_id,
                version=prov.version,
                commit_sha=prov.commit_sha,
                wheel_sha256=prov.wheel_sha256,
                source="provenance",
            )
        lock = self.active_installed_lock()
        if lock is not None and lock.dependencies:
            by_distribution = {
                canonicalize_name(desc.distribution_name): desc.product_id
                for desc in self._catalog.list()
            }
            for name, dep in lock.dependencies.items():
                if not (dep.primary or name in lock.primary_names):
                    continue
                product_id = by_distribution.get(canonicalize_name(name))
                if product_id is None or product_id in keeps:
                    continue
                keeps[product_id] = PlannedKeepProduct(
                    product_id=product_id,
                    version=dep.version,
                    commit_sha=None,
                    wheel_sha256=None,
                    source="installed_lock",
                )
        return keeps

    # ------------------------------------------------------------------
    # M1-2I: Transactional accelerated runtime installation
    # ------------------------------------------------------------------

    def install_accelerated_runtime(
        self,
        *,
        plan: AcceleratedDeploymentPlan | None = None,
        capabilities: HostCapabilities | None = None,
        recommendation: AccelerationRecommendation | None = None,
        acquirer: AcceleratedArtifactAcquirer | None = None,
        gate: AcceleratedGate | None = None,
        metadata_store: AcceleratedSlotMetadataStore | None = None,
        cancel_check: Callable[[], None] | None = None,
        progress_callback=None,
        work_root: Path | None = None,
        fetcher: ArchiveFetcher | None = None,
        full_state_provider: Callable[
            [], Sequence[PreparedProductArtifact]
        ] | None = None,
        dependency_wheelhouse: Path | None = None,
    ) -> AcceleratedDeploymentResult:
        """Execute an accelerated deployment transactionally (M1-2I).

        Full pipeline: acquire -> resolve -> build -> validate -> gate
        -> persist -> activate, extending the CURRENT full desired
        runtime (KEEP semantics) with the accelerated closure declared
        by products.  Products are re-prepared at their exact installed
        identity through the same machinery used for KEEP in
        :meth:`install_product` — never re-resolved from a mutable ref.

        Contract (fail-closed at every step):

        1. Read-only first: when *plan* is ``None`` it is built via
           :meth:`build_accelerated_deployment_plan` from the provided
           (or freshly collected) *capabilities* / *recommendation*.
        2. A non-``PLAN_READY`` plan (``NO_ACCELERATED_REQUIREMENTS`` /
           ``BLOCKED`` / ``UNKNOWN``) returns
           ``success=False, phase=PREPARE`` with an honest reason and
           performs NO acquisition and NO runtime work.  This is the
           honest default on TINYDEBIAN today: no product declares GPU
           requirements and the default variant catalog is empty.
        2b. Deploy-time hardware re-verification (TOCTOU, fail-closed):
            strictly after the plan-status gate and strictly BEFORE any
            base preparation or acquisition, the catalog's acceleration
            requirements for ``plan.products_concerned`` are
            re-evaluated against a fresh host observation
            (:func:`~zealfie.acceleration.compatibility.evaluate_acceleration_compatibility`).
            This closes the planning->deployment observation window
            (driver removed, GPU disabled, partial evidence, or a
            catalog requirement vanished after planning): a fresh
            non-``SUPPORTED`` verdict — or a recommendation backend that
            no longer matches ``plan.backend`` — fails closed at
            ``phase=PREPARE`` with an honest "late GPU compatibility
            conflict" reason and zero mutation.  Provided
            *capabilities* / *recommendation* are reused verbatim (no
            second probe); omitted values are collected/derived exactly
            once at this check.
        3. The base deployment plan is produced for the current full
           desired state: every managed product at its exact installed
           version.  Base artifacts come from ``full_state_provider()``
           when provided (synthetic/hermetic callers supply local
           verified artifacts); otherwise from active provenance
           re-acquired at the exact commit SHA via the M1-2F KEEP
           machinery (:meth:`_prepare_keep_product_artifact`, which
           requires *fetcher*).  The base plan must carry a
           ``dependency_lock`` to extend; when no lock can be produced
           the deployment fails before any candidate slot creation.
        4. ACQUIRE uses *acquirer* or the manifest-backed default
           :func:`~zealfie.acceleration.acquisition.default_manifest_artifact_acquirer`
           (real, human-gated artifact source from the packaged
           accelerated artifact manifest; downloads are sha256-verified,
           fail-closed) and honours cooperative cancellation.  The
           explicit fail-closed
           :func:`~zealfie.acceleration.deployment.default_accelerated_artifact_acquirer`
           remains available for callers that must refuse unconditionally.
        5. :func:`~zealfie.acceleration.deployment.apply_accelerated_deployment`
           runs the engine with the default gate / metadata store when
           not provided, deriving ``declaring_distributions`` from the
           product catalog (product id -> distribution name).
        6. On success the NEW active slot is fully described
           (ZA-M1-3A.2 slot state continuity): product provenance for
           the exact KEEP identities is recorded under the new
           ``active_slot_id`` (same versions / commit SHAs / wheel
           digests as the previous provenance — never re-resolved,
           never invented), and the installed-runtime lock for the new
           slot is reduced from the engine's extended lock (base
           closure verbatim + the acquired accelerated closure that
           was actually deployed).  Selection / policy / channels are
           untouched (products are unchanged).  On any failure or
           cancellation the previously active slot keeps its provenance
           and lock authority: no write under the new slot id occurs.
        7. The method NEVER pip-installs into the active slot: every
           install goes to the fresh candidate slot created by the
           engine, and the active pointer is only switched at
           activation.

        Parameters
        ----------
        plan:
            Optional pre-built accelerated plan.  ``None`` builds it via
            the read-only M1-2H path.
        capabilities / recommendation:
            Reused verbatim wherever supplied — by the read-only plan
            build when *plan* is ``None`` and by the deploy-time
            hardware re-verification (no probe when provided).  When
            omitted, each consumer that needs them collects/derives its
            own observation exactly once.
        acquirer:
            Accelerated artifact source.  Defaults to the manifest-backed
            acquirer (real source, sha256-verified, fail-closed).
        gate:
            Pre-activation compatibility gate.  Defaults to the
            stdlib-only distribution/version probe gate.
        metadata_store:
            Observational accelerated slot metadata store.  Defaults to
            a store bound to the runtime layout (``None`` when the
            runtime exposes no layout).
        cancel_check:
            Optional cooperative cancellation callable.  Raising
            :class:`CooperativeCancellationError` aborts cleanly with
            ``cancelled=True`` and the old runtime preserved.
        progress_callback:
            Optional ``Callable[[InstallProgress], None]`` observer.
            Observational only.
        work_root:
            Staging root for base artifacts and acquired wheels.
            ``None`` uses a private temporary directory that is removed
            when the method returns.
        fetcher:
            Archive fetcher for KEEP re-acquisition (exact SHA).  Only
            used when *full_state_provider* is ``None``.
        full_state_provider:
            Optional zero-arg callable returning the prepared
            full-state artifacts.  Synthetic/hermetic tests supply
            local ``VerifiedArtifact``-backed artifacts here; production
            uses the provenance + *fetcher* KEEP path.
        dependency_wheelhouse:
            Optional dependency wheelhouse for base lock resolution.
            ``None`` auto-acquires via the injected dependency acquirer
            (mirroring :meth:`install_product`).

        Returns
        -------
        AcceleratedDeploymentResult
            Every expected failure is reported as a result (never
            raised); the phase names where the deployment stopped.

        ZA-M1-2L (D1): the whole accelerated install window (plan build, hardware re-check, base preparation, artifact acquisition, the compute gate, activation and the accelerated-metadata record) runs under the ``gpu-install`` mutation lease, acquired at entry and released on every exit path including exceptions.
        """
        lock = self._runtime_mutation_lock()
        if lock is None:
            return self._install_accelerated_runtime(
                plan=plan,
                capabilities=capabilities,
                recommendation=recommendation,
                acquirer=acquirer,
                gate=gate,
                metadata_store=metadata_store,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
                work_root=work_root,
                fetcher=fetcher,
                full_state_provider=full_state_provider,
                dependency_wheelhouse=dependency_wheelhouse,
            )
        with lock.acquire(OPERATION_GPU_INSTALL):
            return self._install_accelerated_runtime(
                plan=plan,
                capabilities=capabilities,
                recommendation=recommendation,
                acquirer=acquirer,
                gate=gate,
                metadata_store=metadata_store,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
                work_root=work_root,
                fetcher=fetcher,
                full_state_provider=full_state_provider,
                dependency_wheelhouse=dependency_wheelhouse,
            )


    def _install_accelerated_runtime(
        self,
        *,
        plan: AcceleratedDeploymentPlan | None = None,
        capabilities: HostCapabilities | None = None,
        recommendation: AccelerationRecommendation | None = None,
        acquirer: AcceleratedArtifactAcquirer | None = None,
        gate: AcceleratedGate | None = None,
        metadata_store: AcceleratedSlotMetadataStore | None = None,
        cancel_check: Callable[[], None] | None = None,
        progress_callback=None,
        work_root: Path | None = None,
        fetcher: ArchiveFetcher | None = None,
        full_state_provider: Callable[
            [], Sequence[PreparedProductArtifact]
        ] | None = None,
        dependency_wheelhouse: Path | None = None,
    ) -> AcceleratedDeploymentResult:
        """Execute an accelerated deployment transactionally (M1-2I).

        Full pipeline: acquire -> resolve -> build -> validate -> gate
        -> persist -> activate, extending the CURRENT full desired
        runtime (KEEP semantics) with the accelerated closure declared
        by products.  Products are re-prepared at their exact installed
        identity through the same machinery used for KEEP in
        :meth:`install_product` — never re-resolved from a mutable ref.

        Contract (fail-closed at every step):

        1. Read-only first: when *plan* is ``None`` it is built via
           :meth:`build_accelerated_deployment_plan` from the provided
           (or freshly collected) *capabilities* / *recommendation*.
        2. A non-``PLAN_READY`` plan (``NO_ACCELERATED_REQUIREMENTS`` /
           ``BLOCKED`` / ``UNKNOWN``) returns
           ``success=False, phase=PREPARE`` with an honest reason and
           performs NO acquisition and NO runtime work.  This is the
           honest default on TINYDEBIAN today: no product declares GPU
           requirements and the default variant catalog is empty.
        2b. Deploy-time hardware re-verification (TOCTOU, fail-closed):
            strictly after the plan-status gate and strictly BEFORE any
            base preparation or acquisition, the catalog's acceleration
            requirements for ``plan.products_concerned`` are
            re-evaluated against a fresh host observation
            (:func:`~zealfie.acceleration.compatibility.evaluate_acceleration_compatibility`).
            This closes the planning->deployment observation window
            (driver removed, GPU disabled, partial evidence, or a
            catalog requirement vanished after planning): a fresh
            non-``SUPPORTED`` verdict — or a recommendation backend that
            no longer matches ``plan.backend`` — fails closed at
            ``phase=PREPARE`` with an honest "late GPU compatibility
            conflict" reason and zero mutation.  Provided
            *capabilities* / *recommendation* are reused verbatim (no
            second probe); omitted values are collected/derived exactly
            once at this check.
        3. The base deployment plan is produced for the current full
           desired state: every managed product at its exact installed
           version.  Base artifacts come from ``full_state_provider()``
           when provided (synthetic/hermetic callers supply local
           verified artifacts); otherwise from active provenance
           re-acquired at the exact commit SHA via the M1-2F KEEP
           machinery (:meth:`_prepare_keep_product_artifact`, which
           requires *fetcher*).  The base plan must carry a
           ``dependency_lock`` to extend; when no lock can be produced
           the deployment fails before any candidate slot creation.
        4. ACQUIRE uses *acquirer* or the manifest-backed default
           :func:`~zealfie.acceleration.acquisition.default_manifest_artifact_acquirer`
           (real, human-gated artifact source from the packaged
           accelerated artifact manifest; downloads are sha256-verified,
           fail-closed) and honours cooperative cancellation.  The
           explicit fail-closed
           :func:`~zealfie.acceleration.deployment.default_accelerated_artifact_acquirer`
           remains available for callers that must refuse unconditionally.
        5. :func:`~zealfie.acceleration.deployment.apply_accelerated_deployment`
           runs the engine with the default gate / metadata store when
           not provided, deriving ``declaring_distributions`` from the
           product catalog (product id -> distribution name).
        6. On success the NEW active slot is fully described
           (ZA-M1-3A.2 slot state continuity): product provenance for
           the exact KEEP identities is recorded under the new
           ``active_slot_id`` (same versions / commit SHAs / wheel
           digests as the previous provenance — never re-resolved,
           never invented), and the installed-runtime lock for the new
           slot is reduced from the engine's extended lock (base
           closure verbatim + the acquired accelerated closure that
           was actually deployed).  Selection / policy / channels are
           untouched (products are unchanged).  On any failure or
           cancellation the previously active slot keeps its provenance
           and lock authority: no write under the new slot id occurs.
        7. The method NEVER pip-installs into the active slot: every
           install goes to the fresh candidate slot created by the
           engine, and the active pointer is only switched at
           activation.

        Parameters
        ----------
        plan:
            Optional pre-built accelerated plan.  ``None`` builds it via
            the read-only M1-2H path.
        capabilities / recommendation:
            Reused verbatim wherever supplied — by the read-only plan
            build when *plan* is ``None`` and by the deploy-time
            hardware re-verification (no probe when provided).  When
            omitted, each consumer that needs them collects/derives its
            own observation exactly once.
        acquirer:
            Accelerated artifact source.  Defaults to the manifest-backed
            acquirer (real source, sha256-verified, fail-closed).
        gate:
            Pre-activation compatibility gate.  Defaults to the
            stdlib-only distribution/version probe gate.
        metadata_store:
            Observational accelerated slot metadata store.  Defaults to
            a store bound to the runtime layout (``None`` when the
            runtime exposes no layout).
        cancel_check:
            Optional cooperative cancellation callable.  Raising
            :class:`CooperativeCancellationError` aborts cleanly with
            ``cancelled=True`` and the old runtime preserved.
        progress_callback:
            Optional ``Callable[[InstallProgress], None]`` observer.
            Observational only.
        work_root:
            Staging root for base artifacts and acquired wheels.
            ``None`` uses a private temporary directory that is removed
            when the method returns.
        fetcher:
            Archive fetcher for KEEP re-acquisition (exact SHA).  Only
            used when *full_state_provider* is ``None``.
        full_state_provider:
            Optional zero-arg callable returning the prepared
            full-state artifacts.  Synthetic/hermetic tests supply
            local ``VerifiedArtifact``-backed artifacts here; production
            uses the provenance + *fetcher* KEEP path.
        dependency_wheelhouse:
            Optional dependency wheelhouse for base lock resolution.
            ``None`` auto-acquires via the injected dependency acquirer
            (mirroring :meth:`install_product`).

        Returns
        -------
        AcceleratedDeploymentResult
            Every expected failure is reported as a result (never
            raised); the phase names where the deployment stopped.
        """
        # ---- 1. Read-only: obtain the accelerated plan -------------------
        if plan is None:
            try:
                plan = self.build_accelerated_deployment_plan(
                    capabilities=capabilities,
                    recommendation=recommendation,
                )
            except Exception as exc:
                return AcceleratedDeploymentResult(
                    success=False,
                    cancelled=False,
                    phase=AcceleratedDeploymentPhase.PREPARE,
                    reason=(
                        "accelerated plan building failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

        # ---- 2. Fail-closed plan gate (no acquisition, no runtime work) --
        if plan.status is not AcceleratedPlanStatus.PLAN_READY:
            return AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.PREPARE,
                reason=(
                    f"accelerated plan is not ready ({plan.status.value}): "
                    f"{plan.blocked_reason or 'no accelerated deployment planned'}"
                ),
            )
        if plan.backend is None:
            return AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.PREPARE,
                reason="accelerated plan has no backend",
            )

        # ---- 2b. Deploy-time hardware re-verification (TOCTOU) ------------
        # The read-only plan and this transactional install are separate
        # observations: between planning and deployment the host may have
        # changed (driver removed, GPU disabled, partial evidence).  Like
        # artifact revalidation, the hardware side is re-checked here —
        # strictly after the plan-status gate (so a non-PLAN_READY plan
        # never probes) and strictly BEFORE any base preparation or
        # acquisition, so a late conflict performs zero mutation.
        # Provided *capabilities* / *recommendation* are reused verbatim
        # (no second probe); omitted values are collected/derived exactly
        # once.  The evaluator result is used directly: SUPPORTED passes,
        # anything else fails closed with the honest reason — including an
        # empty requirements map, because a PLAN_READY plan whose catalog
        # requirements vanished is itself a late conflict.
        deploy_capabilities = (
            capabilities
            if capabilities is not None
            else self.collect_host_capabilities()
        )
        deploy_recommendation = (
            recommendation
            if recommendation is not None
            else self.get_acceleration_recommendation(deploy_capabilities)
        )
        requirements_map = {}
        for pid in plan.products_concerned:
            descriptor = self._catalog.get(pid)
            if descriptor.acceleration is not None:
                requirements_map[pid] = descriptor.acceleration
        hardware = evaluate_acceleration_compatibility(
            requirements_map, deploy_capabilities, deploy_recommendation
        )
        if hardware.status is not HardwareCompatibilityStatus.SUPPORTED:
            return AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.PREPARE,
                reason=(
                    "late GPU compatibility conflict detected at "
                    f"deployment time: {hardware.reason}"
                ),
            )
        if (
            plan.backend is not None
            and deploy_recommendation.backend != plan.backend
        ):
            return AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.PREPARE,
                reason=(
                    "late GPU compatibility conflict detected at "
                    "deployment time: accelerator backend changed from "
                    f"{plan.backend!r} at planning to "
                    f"{deploy_recommendation.backend!r} at deployment"
                ),
            )

        # ---- 3. Base full-state plan (KEEP semantics) + ACQUIRE + apply --
        _emit_progress(
            progress_callback,
            InstallPhase.PREPARING,
            PHASE_PERCENT[InstallPhase.PREPARING],
            "Preparing accelerated runtime\u2026",
        )

        own_work_root = work_root is None
        if own_work_root:
            work_root = Path(
                tempfile.mkdtemp(prefix="zealfie-accel-runtime-")
            )
        auto_staging: Path | None = None
        try:
            # ---- 3a. Base artifacts (exact KEEP identity) -----------------
            try:
                prepared = self._accelerated_base_prepared_artifacts(
                    plan,
                    fetcher=fetcher,
                    full_state_provider=full_state_provider,
                    work_root=work_root,
                    progress_callback=progress_callback,
                )
                # ---- 3b. Dependency wheelhouse (mirror install_product) --
                if dependency_wheelhouse is None:
                    _emit_progress(
                        progress_callback,
                        InstallPhase.ACQUIRING_DEPENDENCIES,
                        PHASE_PERCENT[InstallPhase.ACQUIRING_DEPENDENCIES],
                        "Acquiring dependencies\u2026",
                    )
                    auto_staging = _private_acquisition_staging(work_root)
                    proven = self._proven_dependency_requirements()
                    reused = False
                    for pa in prepared:
                        reused = (
                            self._acquire_product_dependencies(
                                pa, auto_staging, proven=proven,
                            )
                            or reused
                        )
                    if reused:
                        _emit_progress(
                            progress_callback,
                            InstallPhase.ACQUIRING_DEPENDENCIES,
                            PHASE_PERCENT[InstallPhase.ACQUIRING_DEPENDENCIES],
                            "Reusing cached dependencies",
                        )
                    dependency_wheelhouse = auto_staging
                _emit_progress(
                    progress_callback,
                    InstallPhase.PLANNING_RUNTIME,
                    PHASE_PERCENT[InstallPhase.PLANNING_RUNTIME],
                    "Planning accelerated runtime\u2026",
                )
                deployment_plan = self.plan_prepared_product_deployment(
                    prepared,
                    dependency_wheelhouse=dependency_wheelhouse,
                )
            except Exception as exc:
                return AcceleratedDeploymentResult(
                    success=False,
                    cancelled=False,
                    phase=AcceleratedDeploymentPhase.PREPARE,
                    reason=(
                        "base runtime preparation failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

            # ---- 3c. The base plan MUST carry a dependency lock -----------
            if deployment_plan.dependency_lock is None:
                return AcceleratedDeploymentResult(
                    success=False,
                    cancelled=False,
                    phase=AcceleratedDeploymentPhase.PREPARE,
                    reason=(
                        "base deployment plan has no dependency lock; an "
                        "accelerated deployment requires a base RuntimeLock "
                        "to extend"
                    ),
                )

            registry = self._registry_for_prepared_products(prepared)
            declaring_distributions = {
                desc.product_id: desc.distribution_name
                for desc in self._catalog.list()
            }

            # ---- 4. ACQUIRE (fail-closed default) --------------------------
            effective_acquirer = (
                acquirer
                if acquirer is not None
                else default_manifest_artifact_acquirer(
                    cache=self._artifact_cache,
                )
            )
            if cancel_check is not None:
                try:
                    cancel_check()
                except CooperativeCancellationError as exc:
                    return AcceleratedDeploymentResult(
                        success=False,
                        cancelled=True,
                        phase=AcceleratedDeploymentPhase.ACQUIRE,
                        reason=(
                            str(exc) or "accelerated deployment cancelled"
                        ),
                    )
                except Exception as exc:
                    return AcceleratedDeploymentResult(
                        success=False,
                        cancelled=False,
                        phase=AcceleratedDeploymentPhase.ACQUIRE,
                        reason=f"cancel check failed: {exc}",
                    )
            work_root.mkdir(parents=True, exist_ok=True)
            try:
                acquired = effective_acquirer.acquire(
                    plan, work_root, cancel_check=cancel_check,
                )
            except CooperativeCancellationError as exc:
                return AcceleratedDeploymentResult(
                    success=False,
                    cancelled=True,
                    phase=AcceleratedDeploymentPhase.ACQUIRE,
                    reason=str(exc) or "accelerated deployment cancelled",
                )
            except Exception as exc:
                return AcceleratedDeploymentResult(
                    success=False,
                    cancelled=False,
                    phase=AcceleratedDeploymentPhase.ACQUIRE,
                    reason=(
                        "accelerated artifact acquisition failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

            # ---- 5. Engine: resolve -> build -> validate -> gate ->
            #        persist -> activate ------------------------------------
            if gate is None:
                gate = default_accelerated_gate()
            if metadata_store is None:
                layout = getattr(self._runtime, "layout", None)
                metadata_store = (
                    AcceleratedSlotMetadataStore(layout)
                    if layout is not None
                    else None
                )

            result = apply_accelerated_deployment(
                accelerated_plan=plan,
                deployment_plan=deployment_plan,
                registry=registry,
                runtime=self._runtime,
                acquired=acquired,
                declaring_distributions=declaring_distributions,
                accelerated_gate=gate,
                metadata_store=metadata_store,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )

            # ---- 6. Slot state continuity (ZA-M1-3A.2) -------------------
            # The engine activated a NEW slot.  That slot must be fully
            # described like any install_product slot — otherwise the
            # update checker reports PROVENANCE_UNKNOWN, a subsequent
            # install cannot rebuild the full state from provenance, and
            # the GUI still offers GPU setup (readiness is slot-state
            # based).  On success only:
            #   * provenance for the EXACT KEEP product identities
            #     (same version / commit SHA / wheel digest — the
            #     prepared artifacts reconstructed from the previous
            #     provenance, never re-resolved, never invented);
            #   * the installed-runtime lock reduced from the engine's
            #     EXTENDED lock (base closure verbatim + the acquired
            #     accelerated closure actually deployed).
            # Selection / policy / channels are untouched (products are
            # unchanged).  A failure or cancellation never reaches this
            # block: the old slot keeps its provenance and lock
            # authority, and no partial write under the new slot id can
            # become authoritative.
            if result.success:
                self._persist_provenance(prepared, result)
                self._persist_installed_lock_for_slot(
                    result.extended_dependency_lock,
                    result.active_slot_id,
                )
                _emit_progress(
                    progress_callback,
                    InstallPhase.COMPLETED,
                    PHASE_PERCENT[InstallPhase.COMPLETED],
                    "Accelerated runtime is ready",
                )
                # ZA-M1-3A.3: bounded best-effort auto-GC — historical
                # slots (outside active/previous) only, never the
                # rollback target; failures are logged, never raised.
                self._runtime_gc_best_effort()
            return result
        finally:
            if auto_staging is not None:
                _rmtree_best_effort(auto_staging)
            if own_work_root:
                _rmtree_best_effort(work_root)

    def _accelerated_base_prepared_artifacts(
        self,
        plan: AcceleratedDeploymentPlan,
        *,
        fetcher: ArchiveFetcher | None,
        full_state_provider: Callable[
            [], Sequence[PreparedProductArtifact]
        ] | None,
        work_root: Path,
        progress_callback=None,
    ) -> list[PreparedProductArtifact]:
        """Materialize the base full-state (KEEP) artifacts (M1-2I).

        Two sources:

        * ``full_state_provider()`` when provided — synthetic/hermetic
          callers supply already-prepared local artifacts backed by
          ``VerifiedArtifact`` (offline, exact identity);
        * otherwise active provenance re-acquired at the exact commit
          SHA through the M1-2F KEEP machinery
          (:meth:`_prepare_keep_product_artifact`) — never re-resolved
          from a mutable ref.  Empty active provenance, a selected
          catalog-known product without provenance, or a missing
          *fetcher* fails closed before any acquisition or runtime
          mutation.

        The prepared set is then validated against the plan's KEEP
        documentation (fail-closed, deterministic): the prepared
        product set must equal ``plan.keep_products`` (no silent drop,
        no silent addition), every KEEP version must match, every
        known KEEP commit SHA must match the prepared artifact's exact
        commit, and every product declaring accelerated requirements
        must be part of the base state.
        """
        if full_state_provider is not None:
            # Synthetic/hermetic callers supply already-prepared local
            # artifacts; they carry exact KEEP semantics (never
            # re-resolved), so mark them for honest progress wording.
            prepared = [
                replace(pa, origin=ORIGIN_KEEP)
                for pa in full_state_provider()
            ]
        else:
            active = self.active_provenance()
            if not active:
                raise ProductInstallPreparationError(
                    "no active product provenance: cannot materialize the "
                    "base full-state runtime at exact installed identity"
                )
            selected = self._selection_store.current_selection()
            missing = frozenset(
                pid
                for pid in selected.selected_product_ids
                if pid in self._catalog and pid not in active
            )
            if missing:
                raise ProductInstallPreparationError(
                    "cannot materialize base full-state runtime: selected "
                    f"product(s) {sorted(missing)!r} have no active "
                    "provenance"
                )
            if fetcher is None:
                raise ProductInstallPreparationError(
                    "no artifact fetcher configured: cannot re-acquire "
                    "KEEP products at exact commit SHA"
                )
            prepared = [
                self._prepare_keep_product_artifact(
                    pid,
                    active[pid],
                    fetcher=fetcher,
                    work_root=work_root,
                    progress_callback=progress_callback,
                )
                for pid in sorted(active)
            ]

        # ---- KEEP coherence (fail-closed, deterministic) ------------------
        keep_by_id = {
            keep.product_id: keep for keep in plan.keep_products
        }
        prepared_by_id = {pa.product_id: pa for pa in prepared}
        if set(prepared_by_id) != set(keep_by_id):
            only_prepared = sorted(set(prepared_by_id) - set(keep_by_id))
            only_kept = sorted(set(keep_by_id) - set(prepared_by_id))
            detail = ""
            if only_prepared:
                detail += f"; prepared but not kept: {only_prepared}"
            if only_kept:
                detail += f"; kept but not prepared: {only_kept}"
            raise ProductInstallPreparationError(
                "base full-state products do not match the accelerated "
                "plan KEEP documentation" + detail
            )
        for pid in sorted(keep_by_id):
            keep = keep_by_id[pid]
            pa = prepared_by_id[pid]
            if pa.verified_artifact.version != keep.version:
                raise ProductInstallPreparationError(
                    f"KEEP product {pid!r} version drift: accelerated "
                    f"plan documents {keep.version!r}, prepared artifact "
                    f"is {pa.verified_artifact.version!r}"
                )
            if (
                keep.commit_sha is not None
                and pa.resolved_source.commit_sha != keep.commit_sha
            ):
                raise ProductInstallPreparationError(
                    f"KEEP product {pid!r} commit drift: accelerated plan "
                    f"documents {keep.commit_sha!r}, prepared artifact is "
                    f"{pa.resolved_source.commit_sha!r}"
                )
        missing_concerned = sorted(
            pid
            for pid in plan.products_concerned
            if pid not in prepared_by_id
        )
        if missing_concerned:
            raise ProductInstallPreparationError(
                "product(s) declaring accelerated requirements are not "
                "part of the base full-state runtime: "
                + ", ".join(missing_concerned)
            )
        return prepared

    # ------------------------------------------------------------------
    # Release directory convention
    # ------------------------------------------------------------------
    #
    # Deterministic, minimal, local convention:
    #
    #   release_dir/
    #     <component_id>.toml      -- one release manifest per component
    #     <wheel_filename>.whl     -- wheel artifacts at top level
    #
    # Rules:
    #
    # 1. For every *component_id* in ``registry.available_ids()``, the
    #    file ``<component_id>.toml`` MUST exist at the top level.
    #
    # 2. Each manifest's declared ``component_id`` MUST match its
    #    filename stem.
    #
    # 3. Wheel artifacts referenced by manifests live at the top
    #    level of the release directory.
    #
    # 4. Any ``.toml`` file whose stem does not match a known
    #    component id → fail closed.
    #
    # 5. No recursive scan, no fallback names, no heuristic discovery.
    #
    # 6. (M1-1C) The release directory serves as the local dependency
    #    wheelhouse.  Dependency ``.whl`` files live in the same
    #    top-level directory as component manifests and wheel artifacts.

    # ------------------------------------------------------------------
    # D.4.1B: Remote product artifact preparation
    # ------------------------------------------------------------------

    def prepare_product_artifact(
        self,
        product_id: str,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        progress_callback=None,
        source_ref: str | None = None,
    ) -> PreparedProductArtifact:
        """Prepare a verified wheel artifact from a product's remote source.

        Full pipeline: catalog lookup → resolve branch ref to exact SHA →
        fetch and stage source archive → build wheel → verify through
        existing release verification chain.

        The caller controls *work_root*; staging and wheel output live
        under it.  The shared runtime, selection store, and
        ``desired-products.toml`` are **never** mutated.

        Parameters
        ----------
        product_id:
            The product to prepare.  Must exist in the service's catalog.
        resolver:
            Injectable ``(owner, repo, ref) → 40-char-hex-SHA`` callable.
            Tests inject fakes; production resolves against GitHub.
        fetcher:
            Injectable ``(owner, repo, commit_sha) → bytes`` callable.
            Tests inject fakes; production downloads the archive.
        work_root:
            Base directory for staging and wheel output.  Must exist
            and be a directory.  Callers should use a dedicated prep
            root (tests use ``tmp_path`` or ``basetemp``).
        progress_callback:
            Optional ``Callable[[InstallProgress], None]`` observing the
            resolve / download / build boundaries.  Observational only;
            it never affects behaviour or results.
        source_ref:
            Optional ref override (M1-2F Phase 3).  When provided, it
            replaces the catalog descriptor's ``remote_source.ref`` for the
            resolve step (e.g. the channel's mapped ref for a ``follow``
            policy).  Defaults to ``None`` → resolve the catalog's
            ``remote_source.ref`` exactly as before.

        Returns
        -------
        PreparedProductArtifact
            The resolved source (exact SHA provenance), built wheel path,
            and :class:`VerifiedArtifact` proof.

        Raises
        ------
        UnknownProductError
            If *product_id* is not in the product catalog.
        RemoteSourceUnavailableError
            If the product descriptor has no ``remote_source``.
        SourceResolutionError
            If the resolver cannot resolve the ref to a commit SHA.
        AcquisitionError
            If the fetcher fails or the archive is invalid.
        ArtifactRejectionError
            If the built wheel fails verification (identity, version,
            entry-point contract, or integrity check).
        """
        # 1. Lookup in catalog — UnknownProductError for unknown product.
        desc = self._catalog.get(product_id)

        # 2. Guard: remote_source must be present.
        if desc.remote_source is None:
            raise RemoteSourceUnavailableError(
                f"product {product_id!r} has no remote source — "
                f"cannot prepare from remote"
            )

        # 3. Resolve branch/tag ref to exact 40-char commit SHA.
        _emit_progress(
            progress_callback,
            InstallPhase.RESOLVING_SOURCE,
            PHASE_PERCENT[InstallPhase.RESOLVING_SOURCE],
            f"Resolving {desc.display_name}\u2026",
        )
        source = desc.remote_source
        if source_ref is not None:
            source = RemoteSource(
                owner=source.owner,
                repo=source.repo,
                ref=source_ref,
            )
        resolved = resolve_source(source, resolver=resolver)

        # 4-12. Acquire, build, verify — shared with the KEEP path.
        return self._prepare_product_artifact_from_resolved(
            desc,
            resolved,
            fetcher=fetcher,
            work_root=work_root,
            progress_callback=progress_callback,
        )

    def _prepare_product_artifact_from_resolved(
        self,
        desc: ProductDescriptor,
        resolved: ResolvedSource,
        *,
        fetcher: ArchiveFetcher,
        work_root: Path,
        progress_callback=None,
    ) -> PreparedProductArtifact:
        """Acquire, build, and verify a wheel from an already-resolved source.

        Shared by :meth:`prepare_product_artifact` (ref→SHA resolution) and
        :meth:`prepare_product_artifact_at_commit` (exact SHA from active
        provenance).  No ref resolution occurs here — the exact commit SHA
        in *resolved* is authoritative and is passed verbatim to the
        fetcher.  This is a read-only preparation step: the shared runtime
        and selection store are never mutated.
        """
        # 4. Ensure work_root exists.
        work_root.mkdir(parents=True, exist_ok=True)

        # 5. Acquire source archive and extract to staging directory.
        #    The context manager cleans up the staging dir after the
        #    wheel is built (the wheel lives in work_root independently).
        _emit_progress(
            progress_callback,
            InstallPhase.DOWNLOADING_SOURCE,
            PHASE_PERCENT[InstallPhase.DOWNLOADING_SOURCE],
            f"Downloading {desc.display_name}\u2026",
        )
        with acquire_source(
            resolved, fetcher=fetcher, stage_root=work_root,
        ) as staged:
            # 6. Build wheel from the staged source.
            _emit_progress(
                progress_callback,
                InstallPhase.BUILDING_PRODUCT,
                PHASE_PERCENT[InstallPhase.BUILDING_PRODUCT],
                f"Building {desc.display_name}\u2026",
            )
            wheel_path = build_wheel_from_staged(
                staged, output_dir=work_root,
            )

        # 7-12. Verify through the existing release verification chain,
        #     then feed the verified wheel into the shared artifact cache
        #     (ZA-M1-3A.3 LOT C: fill is best-effort — a cache failure
        #     never changes the prepared result).
        prepared = self._verify_prepared_wheel(wheel_path, desc, resolved)
        self._fill_product_cache(prepared)
        return prepared

    def _verify_prepared_wheel(
        self,
        wheel_path: Path,
        desc: ProductDescriptor,
        resolved: ResolvedSource,
    ) -> PreparedProductArtifact:
        """Build a :class:`PreparedProductArtifact` from an existing wheel.

        Shared by the fetch+build path (``_prepare_product_artifact_from_resolved``)
        and the artifact-cache KEEP reuse path (``_prepare_keep_product_artifact``).
        The wheel file is treated as read-only input: its size and SHA256
        are computed from the actual bytes, its identity metadata is
        inspected, and it is verified through the existing release
        verification chain (path safety, size, SHA256, wheel identity,
        version match, distribution name match, entry-point contract).
        Raises :class:`ArtifactRejectionError` on any failure — never
        produces a half-verified artifact.
        """
        wheel_size = wheel_path.stat().st_size
        sha256_hash = hashlib.sha256()
        with open(wheel_path, "rb") as fh:
            while chunk := fh.read(1 << 20):  # 1 MiB chunks
                sha256_hash.update(chunk)
        wheel_sha256 = sha256_hash.hexdigest()

        from zealfie.building import inspect_wheel

        info = inspect_wheel(wheel_path)

        component_def = ComponentDefinition(
            component_id=desc.product_id,
            display_name=desc.display_name,
            distribution_name=desc.distribution_name,
            launch_entry_points=desc.launch_entry_points,
            required_extras=desc.required_extras,
        )
        registry = ComponentRegistry([component_def])

        artifact_entry = ArtifactEntry(
            filename=wheel_path.name,
            size=wheel_size,
            sha256=wheel_sha256,
        )
        manifest = ReleaseManifest(
            schema_version=1,
            component_id=desc.product_id,
            version=info.version,
            artifacts=(artifact_entry,),
        )
        verified = verify_artifact(
            manifest,
            registry=registry,
            artifact_root=wheel_path.parent,
        )
        return PreparedProductArtifact(
            product_id=desc.product_id,
            component_id=desc.product_id,
            resolved_source=resolved,
            wheel_path=wheel_path,
            verified_artifact=verified,
        )

    def _fill_product_cache(self, prepared: PreparedProductArtifact) -> None:
        """Feed a verified product wheel into the artifact cache (LOT C).

        Best-effort only: :meth:`ArtifactCacheStore.put` never raises and
        a failure is logged inside the store — it can never change the
        prepared result or the transaction outcome.
        """
        cache = self._artifact_cache
        if cache is None:
            return
        verified = prepared.verified_artifact
        cache.put(
            prepared.wheel_path,
            kind="product",
            distribution=verified.distribution_name,
            version=verified.version,
        )

    def prepare_product_artifact_at_commit(
        self,
        product_id: str,
        *,
        commit_sha: str,
        source_owner: str,
        source_repo: str,
        requested_ref: str,
        fetcher: ArchiveFetcher,
        work_root: Path,
        progress_callback=None,
    ) -> PreparedProductArtifact:
        """Prepare a verified wheel from an exact, immutable commit SHA.

        This is the **KEEP materialization** path used when reconstructing
        the full desired product set (M1-2F-P1).  Active provenance is
        authoritative: the product is reacquired/rebuild from the exact
        *commit_sha* — the mutable *requested_ref* is only recorded for
        provenance, never re-resolved.  No resolver is invoked.

        The ``resolved_source`` in the returned artifact carries the exact
        *commit_sha* and preserves *requested_ref* so downstream provenance
        persistence records the historical ref without treating it as a
        mutable authority.

        Raises
        ------
        UnknownProductError
            If *product_id* is not in the product catalog.
        AcquisitionError
            If the fetcher fails or the archive is invalid.
        ArtifactRejectionError
            If the built wheel fails verification.
        """
        desc = self._catalog.get(product_id)
        resolved = ResolvedSource(
            source=RemoteSource(
                owner=source_owner,
                repo=source_repo,
                ref=requested_ref,
            ),
            commit_sha=commit_sha,
        )
        return self._prepare_product_artifact_from_resolved(
            desc,
            resolved,
            fetcher=fetcher,
            work_root=work_root,
            progress_callback=progress_callback,
        )

    def _prepare_keep_product_artifact(
        self,
        product_id: str,
        provenance: ProductProvenance,
        *,
        fetcher: ArchiveFetcher,
        work_root: Path,
        progress_callback=None,
    ) -> PreparedProductArtifact:
        """Materialize a KEEP product from active provenance (exact SHA).

        Rebuilds the wheel from the exact ``provenance.commit_sha`` without
        re-resolving ``provenance.requested_ref``.  The rebuilt wheel's
        version must match ``provenance.version`` (active provenance is
        authoritative for version); a mismatch fails honestly rather than
        silently changing the recorded version.

        ZA-M1-3A.3 LOT C (product cache): when the exact wheel recorded in
        ``provenance.wheel_sha256`` is present in the shared artifact cache,
        it is reused WITHOUT any GitHub fetch or build — but only after:

        1. the cached file's SHA256 is recomputed and matches
           ``provenance.wheel_sha256`` (content-addressed, filename-blind);
        2. the wheel passes the full release verification chain
           (:meth:`_verify_prepared_wheel`: identity, version, distribution,
           entry-point contract) with ``version == provenance.version``.

        Any failure (cache miss, bad digest, verification failure) falls
        back to the exact-SHA rebuild path below — the cache is never a
        source of authority.  The mutable ``requested_ref`` is still never
        re-resolved on either path.

        A fresh ``wheel_sha256`` from a rebuild is recorded downstream by
        the existing provenance persistence, so an artifact rebuilt from
        the same source SHA is always described honestly.

        ZA-M1-3A.3 LOT E (update-UX): the returned artifact is marked
        ``origin="keep"`` and an honest progress message is emitted —
        ``Preserving <display> <version>`` when the cache served the exact
        wheel, ``Reacquiring <display> <version> for runtime rebuild`` when
        the exact-SHA rebuild path had to run.  A KEEP product is never
        labelled "Installing" or "Updating".
        """
        display = _product_display_name(self._catalog, product_id)
        cache = self._artifact_cache
        if cache is not None:
            cached_wheel = cache.cached_path_for_digest(
                provenance.wheel_sha256
            )
            if cached_wheel is not None:
                resolved = ResolvedSource(
                    source=RemoteSource(
                        owner=provenance.source_owner,
                        repo=provenance.source_repo,
                        ref=provenance.requested_ref,
                    ),
                    commit_sha=provenance.commit_sha,
                )
                try:
                    prepared = self._verify_prepared_wheel(
                        cached_wheel, self._catalog.get(product_id), resolved
                    )
                except (ArtifactRejectionError, OSError, ValueError):
                    # Verification failure, TOCTOU disappearance, or a
                    # structurally broken cached wheel: fall back to the
                    # normal exact-SHA rebuild — never activate unverified.
                    prepared = None
                if prepared is not None and (
                    prepared.verified_artifact.version != provenance.version
                    or prepared.verified_artifact.sha256
                    != provenance.wheel_sha256
                ):
                    prepared = None
                if prepared is not None:
                    # Propagate known discovery-policy metadata forward
                    # (no re-resolution).  A pre-Phase-4 provenance record
                    # yields None → policy-unknown.
                    _emit_progress(
                        progress_callback,
                        InstallPhase.PREPARING,
                        PHASE_PERCENT[InstallPhase.PREPARING],
                        f"Preserving {display} {provenance.version}",
                    )
                    return replace(
                        prepared,
                        policy=_policy_from_provenance(provenance),
                        origin=ORIGIN_KEEP,
                    )

        prepared = self.prepare_product_artifact_at_commit(
            product_id,
            commit_sha=provenance.commit_sha,
            source_owner=provenance.source_owner,
            source_repo=provenance.source_repo,
            requested_ref=provenance.requested_ref,
            fetcher=fetcher,
            work_root=work_root,
            progress_callback=progress_callback,
        )
        if prepared.verified_artifact.version != provenance.version:
            raise ProductInstallPreparationError(
                f"KEEP product {product_id!r} rebuilt from commit "
                f"{provenance.commit_sha!r} has version "
                f"{prepared.verified_artifact.version!r}, expected "
                f"{provenance.version!r} from active provenance"
            )
        # Propagate known discovery-policy metadata forward (no re-resolution).
        # A pre-Phase-4 provenance record yields None → policy-unknown.
        _emit_progress(
            progress_callback,
            InstallPhase.BUILDING_PRODUCT,
            PHASE_PERCENT[InstallPhase.BUILDING_PRODUCT],
            f"Reacquiring {display} {provenance.version} for runtime rebuild",
        )
        return replace(
            prepared,
            policy=_policy_from_provenance(provenance),
            origin=ORIGIN_KEEP,
        )

    def _prepare_target_product_artifact(
        self,
        product_id: str,
        policy: ProductPolicy,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        progress_callback=None,
    ) -> PreparedProductArtifact:
        """Prepare the target product's artifact according to its policy.

        * ``follow`` → resolve the channel's mapped ref (via
          :func:`~zealfie.products.policy.effective_ref`) exactly like the
          existing ``prepare_product_artifact`` ref→SHA path.
        * ``pin`` → prepare from the exact ``pin_sha`` without invoking the
          resolver at all (no mutable-ref resolution, no network).  This
          reuses :meth:`prepare_product_artifact_at_commit`, so provenance
          records ``requested_ref = pin_sha`` and ``commit_sha = pin_sha``.

        Raises
        ------
        RemoteSourceUnavailableError
            If the product descriptor has no ``remote_source`` (the owner/
            repo needed to fetch/build are unavailable).
        """
        desc = self._catalog.get(product_id)
        if desc.remote_source is None:
            raise RemoteSourceUnavailableError(
                f"product {product_id!r} has no remote source — "
                f"cannot prepare from remote"
            )

        if policy.policy == "pin":
            kwargs: dict = dict(
                commit_sha=policy.pin_sha,
                source_owner=desc.remote_source.owner,
                source_repo=desc.remote_source.repo,
                requested_ref=policy.pin_sha,
                fetcher=fetcher,
                work_root=work_root,
            )
            if progress_callback is not None:
                kwargs["progress_callback"] = progress_callback
            prepared = self.prepare_product_artifact_at_commit(
                product_id,
                **kwargs,
            )
            return replace(prepared, policy=policy)

        ref = _effective_product_ref(desc, policy)
        kwargs = dict(
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        if ref != desc.remote_source.ref:
            kwargs["source_ref"] = ref
        prepared = self.prepare_product_artifact(
            product_id,
            **kwargs,
        )
        return replace(prepared, policy=policy)


    # ------------------------------------------------------------------
    # D.4.1C: Prepared artifact → deployment plan bridge
    # ------------------------------------------------------------------

    def plan_prepared_product_deployment(
        self,
        prepared_artifacts: Sequence[PreparedProductArtifact],
        *,
        probe_distribution=None,
        dependency_wheelhouse: Path | None = None,
    ) -> DeploymentPlan:
        """Build a read-only deployment plan from one or more
        :class:`PreparedProductArtifact` objects.

        This is the bridge between D.4.1B (artifact preparation) and
        the existing deployment planner.  It validates the prepared
        artifacts against the product catalog, materializes a
        :class:`ComponentRegistry` from the catalog descriptors, builds a
        :class:`DesiredRuntimeState` from the verified artifacts, and
        calls :func:`build_deployment_plan`.

        **No install, no apply, no runtime mutation, no selection
        persistence.**  This is purely a read-only planning operation.

        Returns
        -------
        DeploymentPlan
            For ABSENT runtimes, every component receives an INSTALL
            step.  For READY runtimes, the probe determines KEEP/INSTALL.

        Raises
        ------
        ProductDeploymentPlanningError
            If *prepared_artifacts* is empty, contains duplicate ids,
            or has artifact-id mismatches.
        UnknownProductError
            If any prepared artifact's *product_id* is not in the
            product catalog.
        PlanningError
            If the desired state fails registry validation at
            plan-build time.
        """
        # 1. Validate input — empty, duplicates, mismatches.
        _validate_prepared_artifacts(prepared_artifacts)

        # 2. Validate every product_id exists in catalog.
        for pa in prepared_artifacts:
            self._catalog.get(pa.product_id)  # raises UnknownProductError

        # 3. Build catalog-derived ComponentRegistry.
        registry = _registry_for_prepared_products(
            self._catalog, prepared_artifacts
        )

        # 4. Build DesiredRuntimeState from verified artifacts.
        desired_state = _desired_state_from_prepared_artifacts(
            prepared_artifacts
        )

        # 5. Resolve shared runtime dependencies (optional).
        lock = None
        if dependency_wheelhouse is not None and dependency_wheelhouse.is_dir():
            primary_wheels: list[tuple[Path, frozenset[str]]] = []
            for dc in desired_state.components:
                definition = registry.get(dc.component_id)
                primary_wheels.append(
                    (
                        dc.artifact.path,
                        frozenset(definition.required_extras),
                    )
                )
            if primary_wheels:
                try:
                    lock = resolve_runtime_dependencies(
                        primary_wheels,
                        wheelhouse=dependency_wheelhouse,
                    )
                except DependencyResolutionError as exc:
                    raise OfflineReleaseError(
                        f"shared runtime dependency resolution failed: {exc}"
                    ) from exc

        # 6. Get runtime status and build the plan.
        runtime_status = self._runtime.status()
        return build_deployment_plan(
            desired_state,
            registry=registry,
            runtime_status=runtime_status,
            probe_distribution=probe_distribution,
            dependency_lock=lock,
        )

    # ------------------------------------------------------------------
    # D.4.1C: Internal helpers
    # ------------------------------------------------------------------

    def _registry_for_prepared_products(
        self,
        prepared_artifacts: Sequence[PreparedProductArtifact],
    ) -> ComponentRegistry:
        """Build a :class:`ComponentRegistry` from product catalog
        descriptors matching the prepared artifacts."""
        return _registry_for_prepared_products(
            self._catalog, prepared_artifacts
        )

    def _desired_state_from_prepared_artifacts(
        self,
        prepared_artifacts: Sequence[PreparedProductArtifact],
    ) -> DesiredRuntimeState:
        """Build a :class:`DesiredRuntimeState` from prepared artifact
        verified artifacts."""
        return _desired_state_from_prepared_artifacts(
            prepared_artifacts
        )


    def evaluate_prepared_compatibility(
        self,
        prepared_artifacts: Sequence[PreparedProductArtifact],
    ) -> CompatibilityReport:
        """Evaluate interoperability compatibility for the full prepared
        candidate set before activation.

        Scans only the **primary prepared product wheels** (never
        transitive dependency wheels) using the product-agnostic
        evaluator.  This is a pure read-only operation: no install, no
        apply, no runtime mutation, no selection persistence.

        The evaluator sees the *complete* prepared candidate set — not
        just a single target product — so cross-product consumer/provider
        requirements are evaluated against the whole candidate runtime.
        """
        wheel_paths = [pa.wheel_path for pa in prepared_artifacts]
        return evaluate_wheels(wheel_paths)




    # ------------------------------------------------------------------

    # D.4.1D: Apply prepared product deployment

    # ------------------------------------------------------------------



    def install_prepared_product_deployment(

        self,

        prepared_artifacts: Sequence[PreparedProductArtifact],

        *,

        dependency_wheelhouse: Path | None = None,

        probe_distribution=None,

        progress_callback=None,

        accelerated_acquirer: AcceleratedArtifactAcquirer | None = None,

    ) -> DeploymentResult | AcceleratedDeploymentResult:

        """Apply a prepared product deployment to the shared runtime.



        Full pipeline: preflight selection store → plan via D.4.1C →

        apply via existing transactional :func:`apply_deployment_plan` →

        persist product selection only on success.



        **No remote source resolution, fetch, build, or network I/O.**

        This gate expects already-prepared product artifacts and

        delegates to the existing deployment engine.



        Mutation ordering guarantees

        ----------------------------

        Before apply (read-only preflight):

        1. Validate selection store is readable (corrupt → fail).

        2. Validate existing selection against product catalog.

        3. Validate prepared artifacts (non-empty, no duplicates,

           no mismatches) via D.4.1C validator.

        4. Validate every product_id in catalog (UnknownProductError).

        5. Build read-only deployment plan via

           :meth:`plan_prepared_product_deployment`.

        6. Build catalog-derived :class:`ComponentRegistry`.



        Apply (runtime mutation via engine):

        7. Call :func:`apply_deployment_plan` with plan + registry.

           The existing engine handles venv creation, dependency

           materialization, component installation, validation, and

           atomic activation.



        After success only:

        8. Persist installed product ids to

           ``desired-products.toml``, preserving any pre-existing

           selected ids.  The selection store is never touched on

           any failure path.



        Parameters

        ----------

        prepared_artifacts:

            One or more prepared product artifacts from D.4.1B.

        dependency_wheelhouse:

            Optional directory containing dependency wheels.

        probe_distribution:

            Injectable probe callable for READY runtime planning.

        progress_callback:

            Optional ``Callable[[InstallProgress], None]`` observing the

            planning / install / validation / activation / completion

            boundaries.  Observational only; it never affects behaviour,

            results, or error propagation.
        accelerated_acquirer:

            Optional injected accelerated artifact acquirer for the GPU

            continuity path (ZA-M1-3A.3a).  When the active slot carries

            a validated accelerated runtime, the candidate runtime is

            rebuilt with the SAME accelerated closure through this

            acquirer (``None`` falls back to the manifest-backed

            production acquirer with the shared artifact cache).  When

            the active slot carries no validated accelerated runtime the

            argument is ignored and the plain CPU path applies.



        Returns

        -------

        DeploymentResult or AcceleratedDeploymentResult

            *success=True* with the new active slot id on

            successful apply; an ``AcceleratedDeploymentResult`` when

            the active slot's accelerated closure was preserved.

            *success=False* with a reason

            string on any failure.



        Raises

        ------

        ProductDeploymentPlanningError

            If *prepared_artifacts* is empty, contains duplicate ids,

            or has artifact-id mismatches.

        UnknownProductError

            If any prepared artifact's *product_id* is not in the

            product catalog.

        CorruptSelectionError

            If the selection store file is present but unreadable.

        """

        # ---- 1. Preflight: validate selection store is readable ------

        try:

            existing_selection = self._selection_store.current_selection()

        except CorruptSelectionError:

            raise  # Fail before apply — no mutation to runtime or selection.



        # ---- 2. Preflight: validate existing selection vs catalog ----

        _validate_selection_against_catalog(self._catalog, existing_selection)



        # ---- 3-5. Plan via D.4.1C (validates artifacts, catalog) ----

        _emit_progress(

            progress_callback,

            InstallPhase.PLANNING_RUNTIME,

            PHASE_PERCENT[InstallPhase.PLANNING_RUNTIME],

            "Planning runtime\u2026",

        )

        plan = self.plan_prepared_product_deployment(

            prepared_artifacts,

            dependency_wheelhouse=dependency_wheelhouse,

            probe_distribution=probe_distribution,

        )



        # ---- 6. Build catalog-derived ComponentRegistry --------------

        registry = self._registry_for_prepared_products(prepared_artifacts)


        # ---- 6b. Compatibility gate (pre-activation, read-only) ------
        # Evaluate the FULL prepared candidate set against the
        # product-agnostic interoperability evaluator before any runtime
        # mutation.  Blocking reports (INCOMPATIBLE or
        # METADATA_UNAVAILABLE) fail closed here, before
        # apply_deployment_plan and before selection persistence.
        compatibility_report = self.evaluate_prepared_compatibility(
            prepared_artifacts
        )
        if compatibility_report.blocked:
            raise ProductCompatibilityBlockedError(compatibility_report)
        _surface_compatibility_diagnostics(compatibility_report)


        # ---- 6c. Accelerated closure preservation (ZA-M1-3A.3a) ------
        # When the ACTIVE slot already carries a validated accelerated
        # runtime, an ordinary product transaction must rebuild the
        # candidate with the SAME accelerated closure — never a silent
        # CPU-only downgrade.  This reuses the M1-2I engine with a
        # preservation plan derived verbatim from the active slot's
        # observational metadata.  Readiness here is a slot-state fact,
        # never re-derived from host hardware probes.
        metadata = self._validated_active_accelerated_metadata()
        if metadata is not None:
            preserve_plan = build_acceleration_preservation_plan(
                catalog=self._catalog,
                backend=metadata.backend,
                variants=metadata.variants,
                source_active_slot_id=plan.source_active_slot_id,
            )
            effective_acquirer = (
                accelerated_acquirer
                if accelerated_acquirer is not None
                else default_manifest_artifact_acquirer(
                    cache=self._artifact_cache
                )
            )
            layout = getattr(self._runtime, "layout", None)
            metadata_store = (
                AcceleratedSlotMetadataStore(layout)
                if layout is not None
                else None
            )
            declaring_distributions = {
                desc.product_id: desc.distribution_name
                for desc in self._catalog.list()
            }
            accel_work = Path(
                tempfile.mkdtemp(prefix="zealfie-accel-preserve-")
            )
            try:
                acquired = effective_acquirer.acquire(
                    preserve_plan, accel_work, cancel_check=None,
                )
                result = apply_accelerated_deployment(
                    accelerated_plan=preserve_plan,
                    deployment_plan=plan,
                    registry=registry,
                    runtime=self._runtime,
                    acquired=acquired,
                    declaring_distributions=declaring_distributions,
                    accelerated_gate=None,  # -> default_accelerated_gate()
                    metadata_store=metadata_store,
                    progress_callback=progress_callback,
                )
            finally:
                _rmtree_best_effort(accel_work)
            if result.success:
                for pa in prepared_artifacts:
                    self._selection_store.select(
                        pa.product_id, catalog=self._catalog,
                    )
                self._persist_provenance(prepared_artifacts, result)
                self._persist_installed_lock_for_slot(
                    result.extended_dependency_lock,
                    result.active_slot_id,
                )
                ready_message = _completion_message(
                    self._catalog, prepared_artifacts,
                )
                _emit_progress(
                    progress_callback,
                    InstallPhase.COMPLETED,
                    PHASE_PERCENT[InstallPhase.COMPLETED],
                    ready_message,
                )
                self._runtime_gc_best_effort()
            return result



        # ---- 7. Apply via existing transactional engine --------------

        _emit_progress(

            progress_callback,

            InstallPhase.INSTALLING_RUNTIME,

            PHASE_PERCENT[InstallPhase.INSTALLING_RUNTIME],

            "Installing runtime\u2026",

        )

        if progress_callback is not None:

            result = apply_deployment_plan(

                plan,

                registry=registry,

                runtime=self._runtime,

                progress_callback=progress_callback,

            )

        else:

            result = apply_deployment_plan(

                plan,

                registry=registry,

                runtime=self._runtime,

            )



        # ---- 8. Persist selection only after successful activation ---

        if result.success:

            for pa in prepared_artifacts:

                self._selection_store.select(

                    pa.product_id, catalog=self._catalog,

                )

            # ---- 9. Persist provenance only after activation + selection ---
            # Ordering invariant: provenance is written LAST, so any apply or
            # selection failure leaves the old active provenance authoritative.
            self._persist_provenance(prepared_artifacts, result)
            # ---- 10. Persist installed-runtime lock (observational) ----
            # Reduced installed-reality lock, written alongside/after
            # provenance.  Never drives install/rollback decisions.
            self._persist_installed_lock(plan, result)
            ready_message = _completion_message(
                self._catalog, prepared_artifacts,
            )
            _emit_progress(

                progress_callback,

                InstallPhase.COMPLETED,

                PHASE_PERCENT[InstallPhase.COMPLETED],

                ready_message,

            )

            # ---- 11. Bounded best-effort auto-GC (ZA-M1-3A.3) --------
            # Historical slots (outside active/previous) are pruned
            # together with their store entries.  Best-effort only: a
            # BLOCKED plan or any failure is logged and never changes
            # the transaction outcome.
            self._runtime_gc_best_effort()



        return result



    # ------------------------------------------------------------------
    # M1-2E E.1: provenance persistence (post-activation)
    # ------------------------------------------------------------------

    def _persist_provenance(
        self,
        prepared_artifacts: Sequence[PreparedProductArtifact],
        result: DeploymentResult | AcceleratedDeploymentResult,
    ) -> None:
        """Persist installed-product provenance after successful activation.

        Called only after ``apply_deployment_plan`` returned success and
        selection persistence succeeded.  Provenance is keyed by the new
        active slot id so it always describes the active runtime, never a
        failed candidate.

        A persistence failure here does **not** roll back the runtime: the
        runtime stays active and provenance readback returns unknown
        (``None``).  The failure is logged, never raised.
        """
        store = self._provenance_store
        if store is None:
            return
        slot_id = result.active_slot_id
        if not slot_id:
            return
        try:
            store.record(
                slot_id, _provenance_entries_for(prepared_artifacts)
            )
        except Exception:
            logger.warning(
                "failed to persist product provenance for slot %r; "
                "runtime activation is unaffected",
                slot_id,
                exc_info=True,
            )


    # ------------------------------------------------------------------
    # M1-2F Phase 4 corrective: installed-runtime lock persistence
    # ------------------------------------------------------------------

    def _persist_installed_lock(
        self,
        plan: DeploymentPlan,
        result: DeploymentResult,
    ) -> None:
        self._persist_installed_lock_for_slot(
            plan.dependency_lock,
            result.active_slot_id,
        )

    def _persist_installed_lock_for_slot(
        self,
        lock: RuntimeLock | None,
        slot_id: str | None,
    ) -> None:
        """Persist the reduced installed-runtime lock after activation.

        Called only after ``apply_deployment_plan`` returned success and
        selection persistence succeeded (alongside provenance).  The lock is
        reduced from the provided ``lock`` — transient install-input
        fields (``wheel_path`` / ``size`` / ``sha256``) are dropped — and
        keyed by the new active slot id so it always describes the active
        runtime, never a failed candidate.  For the accelerated path the
        caller passes the engine's extended lock (base closure + acquired
        accelerated closure), so the recorded lock describes the FINAL
        accelerated reality, never the pre-acceleration closure alone.

        A ``None`` lock (no resolved closure was used) records a known-empty
        lock for the slot, so "no closure used" is distinguishable from
        UNKNOWN (no record).

        This store is **observational only**: no install/update/rollback/KEEP
        decision reads it.  A persistence failure here does **not** roll back
        the runtime and is logged, never raised (identical non-destructive
        semantics to provenance persistence).
        """
        store = self._installed_lock_store
        if store is None:
            return
        if not slot_id:
            return
        try:
            store.record(slot_id, installed_lock_from_runtime_lock(lock))
        except Exception:
            logger.warning(
                "failed to persist installed-runtime lock for slot %r; "
                "runtime activation is unaffected",
                slot_id,
                exc_info=True,
            )


    # ------------------------------------------------------------------
    # D.4.1E: Public service install_product orchestration
    # ------------------------------------------------------------------


    # M1-2F-P1: full-state multi-product orchestration.  install_product
    # reconstructs the complete desired product set (all KEEP active
    # products plus the target product), prepares artifacts for every
    # product, acquires ONE shared dependency wheelhouse, and applies ONE
    # transaction so the candidate runtime contains the whole set.  It
    # reuses the existing D.4.1B/D.4.1C/D.4.1D primitives; no second
    # deployment engine is introduced.
    def install_product(
        self,
        product_id: str,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        dependency_wheelhouse: Path | None = None,
        probe_distribution=None,
        progress_callback=None,
        accelerated_acquirer: AcceleratedArtifactAcquirer | None = None,
    ) -> DeploymentResult | AcceleratedDeploymentResult:
        """Install (or update) a product within the full desired product set.

        Full-state orchestration:

        1. Reconstruct the complete desired set as ``KEEP ∪ {product_id}``
           where KEEP products come from active provenance (exact commit
           SHA, never re-resolved) and the target is resolved from its
           source ref.
        2. Prepare a verified wheel artifact for every product in the set.
        3. Acquire ONE combined dependency wheelhouse covering all products
           (auto-acquired only when the caller does not supply
           ``dependency_wheelhouse``).
        4. Plan and apply a single transaction via
           :meth:`install_prepared_product_deployment`, which resolves ONE
           combined dependency lock, builds ONE candidate runtime, validates
           every expected product, and atomically activates it.

        Delegates to:

        * :meth:`prepare_product_artifact` (D.4.1B): remote source
          resolution, fetch, build, and verification — used for the target.
        * :meth:`prepare_product_artifact_at_commit`: exact-SHA
          reacquisition for KEEP products (no ref resolution).
        * :meth:`install_prepared_product_deployment` (D.4.1D):
          planning, transactional apply, and post-success selection
          persistence.
        * :class:`PipWheelhouseAcquirer` (D.4.2B → D.4.2C):
          auto-acquires a combined transitive dependency wheelhouse when
          the caller does not supply ``dependency_wheelhouse``.

        This is service-layer orchestration only.  It must not duplicate
        source resolution, fetch, build, verify, planning, apply, or
        selection persistence logic.

        **Exception propagation:** errors from the preparation and
        prepared-install layers propagate without wrapping.  Callers
        receive the exact exception from the first failing step.
        Dependency acquisition failures, including product wheel disappearance,
        Metadata errors, ExtraNotFound, and transport failures, are
        wrapped in :class:`ProductDependencyAcquisitionError`.

        Parameters
        ----------
        product_id:
            The product to install.  Must exist in the product catalog
            with a ``remote_source``.
        resolver:
            Injectable ``(owner, repo, ref) → 40-char-hex-SHA`` callable.
        fetcher:
            Injectable ``(owner, repo, commit_sha) → bytes`` callable.
        work_root:
            Base directory for staging and wheel output.
        dependency_wheelhouse:
            Optional wheelhouse directory for dependency resolution.
        probe_distribution:
            Injectable probe callable for READY runtime planning.
        progress_callback:
            Optional ``Callable[[InstallProgress], None]`` observing the
            prepare / acquire / plan / install / validate / activate /
            complete boundaries.  Observational only; it never affects
            behaviour, results, or error propagation.
        accelerated_acquirer:
            Optional injected accelerated artifact acquirer for the GPU
            continuity path (ZA-M1-3A.3a).  When the active slot carries
            a validated accelerated runtime, the candidate rebuilds the
            SAME accelerated closure via this acquirer; ``None`` falls
            back to the manifest-backed production acquirer (with the
            shared artifact cache).  Hermetic tests inject a synthetic
            acquirer here.

        Returns
        -------
        DeploymentResult or AcceleratedDeploymentResult
            The exact result from the transactional deployment engine;
            an ``AcceleratedDeploymentResult`` when the active slot's
            accelerated closure was preserved (GPU continuity).

        Raises
        ------
        UnknownProductError
            If *product_id* is not in the product catalog.
        RemoteSourceUnavailableError
            If the product descriptor has no ``remote_source``.
        SourceResolutionError
            If the resolver cannot resolve the ref to a commit SHA.
        ArtifactRejectionError
            If the built wheel fails verification.
        ProductDependencyAcquisitionError
            If auto-acquisition fails (product wheel disappeared,
            Metadata unreadable, extra unknown, transport failure).
            The original cause is preserved via ``__cause__``.
        CorruptSelectionError
            If the selection store file is present but unreadable.

        ZA-M1-2L (D1): the whole product install window (artifact preparation, dependency wheelhouse acquisition, the deployment engine, and the post-activation selection/provenance/installed-lock persistence) runs under the ``product-install`` mutation lease, acquired at entry and released on every exit path including exceptions.
        """
        lock = self._runtime_mutation_lock()
        if lock is None:
            return self._install_product(
                product_id,
                resolver=resolver,
                fetcher=fetcher,
                work_root=work_root,
                dependency_wheelhouse=dependency_wheelhouse,
                probe_distribution=probe_distribution,
                progress_callback=progress_callback,
                accelerated_acquirer=accelerated_acquirer,
            )
        with lock.acquire(OPERATION_PRODUCT_INSTALL):
            return self._install_product(
                product_id,
                resolver=resolver,
                fetcher=fetcher,
                work_root=work_root,
                dependency_wheelhouse=dependency_wheelhouse,
                probe_distribution=probe_distribution,
                progress_callback=progress_callback,
                accelerated_acquirer=accelerated_acquirer,
            )


    def _install_product(
        self,
        product_id: str,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        dependency_wheelhouse: Path | None = None,
        probe_distribution=None,
        progress_callback=None,
        accelerated_acquirer: AcceleratedArtifactAcquirer | None = None,
    ) -> DeploymentResult | AcceleratedDeploymentResult:
        """Install (or update) a product within the full desired product set.

        Full-state orchestration:

        1. Reconstruct the complete desired set as ``KEEP ∪ {product_id}``
           where KEEP products come from active provenance (exact commit
           SHA, never re-resolved) and the target is resolved from its
           source ref.
        2. Prepare a verified wheel artifact for every product in the set.
        3. Acquire ONE combined dependency wheelhouse covering all products
           (auto-acquired only when the caller does not supply
           ``dependency_wheelhouse``).
        4. Plan and apply a single transaction via
           :meth:`install_prepared_product_deployment`, which resolves ONE
           combined dependency lock, builds ONE candidate runtime, validates
           every expected product, and atomically activates it.

        Delegates to:

        * :meth:`prepare_product_artifact` (D.4.1B): remote source
          resolution, fetch, build, and verification — used for the target.
        * :meth:`prepare_product_artifact_at_commit`: exact-SHA
          reacquisition for KEEP products (no ref resolution).
        * :meth:`install_prepared_product_deployment` (D.4.1D):
          planning, transactional apply, and post-success selection
          persistence.
        * :class:`PipWheelhouseAcquirer` (D.4.2B → D.4.2C):
          auto-acquires a combined transitive dependency wheelhouse when
          the caller does not supply ``dependency_wheelhouse``.

        This is service-layer orchestration only.  It must not duplicate
        source resolution, fetch, build, verify, planning, apply, or
        selection persistence logic.

        **Exception propagation:** errors from the preparation and
        prepared-install layers propagate without wrapping.  Callers
        receive the exact exception from the first failing step.
        Dependency acquisition failures, including product wheel disappearance,
        Metadata errors, ExtraNotFound, and transport failures, are
        wrapped in :class:`ProductDependencyAcquisitionError`.

        Parameters
        ----------
        product_id:
            The product to install.  Must exist in the product catalog
            with a ``remote_source``.
        resolver:
            Injectable ``(owner, repo, ref) → 40-char-hex-SHA`` callable.
        fetcher:
            Injectable ``(owner, repo, commit_sha) → bytes`` callable.
        work_root:
            Base directory for staging and wheel output.
        dependency_wheelhouse:
            Optional wheelhouse directory for dependency resolution.
        probe_distribution:
            Injectable probe callable for READY runtime planning.
        progress_callback:
            Optional ``Callable[[InstallProgress], None]`` observing the
            prepare / acquire / plan / install / validate / activate /
            complete boundaries.  Observational only; it never affects
            behaviour, results, or error propagation.
        accelerated_acquirer:
            Optional injected accelerated artifact acquirer for the GPU
            continuity path (ZA-M1-3A.3a); threaded unchanged to
            :meth:`install_prepared_product_deployment`.  ``None`` uses
            the manifest-backed production acquirer (with cache).

        Returns
        -------
        DeploymentResult or AcceleratedDeploymentResult
            The exact result from the transactional deployment engine;
            an ``AcceleratedDeploymentResult`` when the active slot's
            accelerated closure was preserved (GPU continuity).

        Raises
        ------
        UnknownProductError
            If *product_id* is not in the product catalog.
        RemoteSourceUnavailableError
            If the product descriptor has no ``remote_source``.
        SourceResolutionError
            If the resolver cannot resolve the ref to a commit SHA.
        ArtifactRejectionError
            If the built wheel fails verification.
        ProductDependencyAcquisitionError
            If auto-acquisition fails (product wheel disappeared,
            Metadata unreadable, extra unknown, transport failure).
            The original cause is preserved via ``__cause__``.
        CorruptSelectionError
            If the selection store file is present but unreadable.
        """
        # --- 0. Reconstruct the complete desired product set -------------
        active_provenance, desired_ids = (
            self._reconstruct_full_desired_product_ids(product_id)
        )

        # --- 0b. Update-vs-install semantics (ZA-M1-3A.3 LOT E) ----------
        # A target that already carries active provenance is an UPDATE (its
        # old version is authoritative); a target without provenance is a
        # fresh INSTALL.  This drives the origin marker and the honest
        # "Updating <old> -> <new>" message - it never changes resolution,
        # planning, or transaction behaviour.
        old_version: str | None = (
            active_provenance[product_id].version
            if product_id in active_provenance
            else None
        )

        # --- 1. Determine whether to auto-acquire dependencies ----------
        auto_acquire = dependency_wheelhouse is None
        auto_staging: Path | None = None

        _emit_progress(
            progress_callback,
            InstallPhase.PREPARING,
            PHASE_PERCENT[InstallPhase.PREPARING],
            f"Preparing {_product_display_name(self._catalog, product_id)}\u2026",
        )

        try:
            # --- 2. Prepare artifacts for ALL desired products -----------
            prepared: list[PreparedProductArtifact] = []
            update_target: PreparedProductArtifact | None = None
            for pid in desired_ids:
                if pid == product_id:
                    pa = self._prepare_target_product_artifact(
                        pid,
                        self._policy_store.policy_for(pid),
                        resolver=resolver,
                        fetcher=fetcher,
                        work_root=work_root,
                        progress_callback=progress_callback,
                    )
                    if old_version is not None:
                        pa = replace(pa, origin=ORIGIN_UPDATE)
                        update_target = pa
                else:
                    pa = self._prepare_keep_product_artifact(
                        pid,
                        active_provenance[pid],
                        fetcher=fetcher,
                        work_root=work_root,
                        progress_callback=progress_callback,
                    )
                prepared.append(pa)

            # --- 2b. Honest update message once old AND new are known ---
            # Emitted right after the target artifact is prepared (the new
            # version only exists then).  BUILDING_PRODUCT keeps the
            # progress contract monotone: PREPARING/RESOLVING_SOURCE
            # percents would regress after the target's build emissions.
            if update_target is not None and old_version is not None:
                _emit_progress(
                    progress_callback,
                    InstallPhase.BUILDING_PRODUCT,
                    PHASE_PERCENT[InstallPhase.BUILDING_PRODUCT],
                    (
                        f"Updating {_product_display_name(self._catalog, product_id)} "
                        f"{old_version} -> {update_target.verified_artifact.version}"
                    ),
                )

            # --- 3. Auto-acquire ONE combined dependency wheelhouse ------
            if auto_acquire:
                try:
                    _emit_progress(
                        progress_callback,
                        InstallPhase.ACQUIRING_DEPENDENCIES,
                        PHASE_PERCENT[InstallPhase.ACQUIRING_DEPENDENCIES],
                        "Acquiring dependencies\u2026",
                    )
                    auto_staging = _private_acquisition_staging(work_root)
                    proven = self._proven_dependency_requirements()
                    reused = False
                    for pa in prepared:
                        reused = (
                            self._acquire_product_dependencies(
                                pa, auto_staging, proven=proven,
                            )
                            or reused
                        )
                    if reused:
                        _emit_progress(
                            progress_callback,
                            InstallPhase.ACQUIRING_DEPENDENCIES,
                            PHASE_PERCENT[InstallPhase.ACQUIRING_DEPENDENCIES],
                            "Reusing cached dependencies",
                        )
                    dependency_wheelhouse = auto_staging
                except (FileNotFoundError, MetadataError, ExtraNotFound,
                        AcquisitionTransportError, OSError) as exc:
                    raise ProductDependencyAcquisitionError(
                        f"dependency acquisition failed for {product_id!r}: {exc}"
                    ) from exc

            # --- 4. Plan + apply + persist selection (D.4.1D) ---
            if progress_callback is not None:
                return self.install_prepared_product_deployment(
                    prepared,
                    dependency_wheelhouse=dependency_wheelhouse,
                    probe_distribution=probe_distribution,
                    progress_callback=progress_callback,
                    accelerated_acquirer=accelerated_acquirer,
                )
            return self.install_prepared_product_deployment(
                prepared,
                dependency_wheelhouse=dependency_wheelhouse,
                probe_distribution=probe_distribution,
                accelerated_acquirer=accelerated_acquirer,
            )

        finally:
            # --- 5. Clean auto-acquired staging ---
            if auto_staging is not None:
                _rmtree_best_effort(auto_staging)

    def _reconstruct_full_desired_product_ids(
        self,
        target_product_id: str,
    ) -> tuple[dict[str, ProductProvenance], list[str]]:
        """Reconstruct the complete desired product set for a transaction.

        The set is ``KEEP ∪ {target}`` where KEEP is every product with
        active provenance except the target itself (the target is being
        installed or updated, so it is re-materialized from its resolved
        source rather than kept from provenance).

        Active provenance is the sole authority for KEEP identity, version,
        and exact commit SHA; the selection store is not consulted for
        already-active products.  Deterministic ordering: KEEP ids sorted
        by product id, target last.

        **Fail-closed guard (M1-2F-P1-C1):** if the selection store
        references any catalog-known product other than the target that has
        no active provenance, full-state reconstruction would silently drop
        it.  This is raised as :class:`ProductInstallPreparationError`
        *before* any fetch, build, apply, selection, or provenance mutation.
        Unknown selected ids are not raised here — they are a separate
        pre-existing validation concern handled by
        :meth:`install_prepared_product_deployment` (``UnknownProductError``).

        Returns
        -------
        (active_provenance, desired_ids)
            The active provenance mapping and the ordered desired product
            id list (KEEP sorted, then target).

        Raises
        ------
        ProductInstallPreparationError
            If a selected, catalog-known non-target product lacks active
            provenance (including a READY runtime whose active provenance
            is empty after rollback).
        """
        active = self.active_provenance()
        active_ids = frozenset(active)

        # Guard against silently dropping a selected product that we cannot
        # KEEP: only catalog-known selected ids can be part of a full-state
        # runtime, so only those are checked for active-provenance coverage.
        selected = self._selection_store.current_selection()
        missing = frozenset(
            pid for pid in selected.selected_product_ids
            if pid != target_product_id
            and pid in self._catalog
            and pid not in active_ids
        )
        if missing:
            raise ProductInstallPreparationError(
                "cannot reconstruct full desired product set: selected "
                f"product(s) {sorted(missing)!r} have no active provenance. "
                "Exact active provenance (identity, version, commit SHA) is "
                "required to preserve a full-state runtime; refusing to "
                f"install target {target_product_id!r} rather than silently "
                "drop selected product(s)."
            )

        keep_ids = sorted(
            pid for pid in active if pid != target_product_id
        )
        return active, keep_ids + [target_product_id]

    # D.4.1A: Internal helpers with explicit registry
    # ------------------------------------------------------------------

    def _resolve_release_set_for_registry(
        self, registry: ComponentRegistry, release_dir: Path
    ) -> DesiredRuntimeState:
        """Resolve a complete offline release set using *registry*.

        Reads every manifest, resolves each to a
        :class:`VerifiedArtifact` via the safe local release resolver,
        and returns a complete :class:`DesiredRuntimeState`.

        This is a read-only operation.  No filesystem mutation.
        """
        # Every known component must have a manifest.
        expected_ids = frozenset(registry.available_ids())

        if not expected_ids:
            raise OfflineReleaseError(
                "component registry is empty — nothing to plan"
            )

        rd = release_dir
        if not rd.is_dir():
            raise OfflineReleaseError(
                f"release_dir does not exist or is not a directory: {rd}"
            )

        desired_components: list[DesiredComponent] = []

        for component_id in sorted(expected_ids):
            manifest_path = rd / f"{component_id}.toml"
            if not manifest_path.is_file():
                raise OfflineReleaseError(
                    f"missing release manifest for component {component_id!r}: "
                    f"expected {manifest_path}"
                )

            try:
                manifest = parse_release_manifest_file(manifest_path)
            except ReleaseManifestError as exc:
                raise OfflineReleaseError(
                    f"invalid release manifest at {manifest_path}: {exc}"
                ) from exc

            if manifest.component_id != component_id:
                raise OfflineReleaseError(
                    f"manifest component_id mismatch: {manifest_path} declares "
                    f"{manifest.component_id!r}, expected {component_id!r}"
                )

            try:
                verified = resolve_local_release(
                    manifest,
                    registry=registry,
                    artifact_root=rd,
                    host=self._host,
                )
            except ReleaseResolutionError as exc:
                raise OfflineReleaseError(
                    f"cannot resolve release for {component_id!r}: {exc}"
                ) from exc

            desired_components.append(
                DesiredComponent(
                    component_id=verified.component_id,
                    version=verified.version,
                    artifact=verified,
                )
            )

        # ------------------------------------------------------------------
        # Fail closed: reject extra .toml files with unknown stems.
        # ------------------------------------------------------------------
        for entry in sorted(rd.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix != ".toml":
                continue
            stem = entry.stem
            if stem not in expected_ids:
                raise OfflineReleaseError(
                    f"unknown release manifest: {entry.name} — "
                    f"component {stem!r} is not in the registry"
                )

        return DesiredRuntimeState(components=tuple(desired_components))

    # ------------------------------------------------------------------
    # resolve_offline_release_set  (compatibility wrapper)
    # ------------------------------------------------------------------

    def resolve_offline_release_set(
        self, release_dir: Path
    ) -> DesiredRuntimeState:
        """Resolve a complete offline release set from *release_dir*
        using the legacy packaged :class:`ComponentRegistry`.

        This is a read-only operation.  No filesystem mutation.
        """
        return self._resolve_release_set_for_registry(
            self._registry, release_dir
        )

    # ------------------------------------------------------------------
    # D.4.1A: plan_offline_deployment_for_registry
    # ------------------------------------------------------------------

    def _plan_deployment_for_registry(
        self, registry: ComponentRegistry, release_dir: Path
    ) -> DeploymentPlan:
        """Plan a full offline deployment from *release_dir* using
        *registry* as the component authority.

        Resolves the complete release set for all registry components,
        builds the desired runtime state, then resolves shared runtime
        dependencies from the local wheelhouse (the release directory)
        and builds a read-only deployment plan from the current runtime
        status.

        This is a read-only operation.  No filesystem mutation.
        """
        desired_state = self._resolve_release_set_for_registry(
            registry, release_dir
        )
        runtime_status = self._runtime.status()

        # M1-1C: Resolve shared runtime dependencies from the local
        # wheelhouse.  The wheelhouse is the release directory itself
        # (manifests + wheels coexist at the top level).
        # Primary wheels come from already-verified component artifacts.
        # Active extras come from the explicit *registry*'s
        # ``required_extras`` (already canonicalised by the model).
        try:
            primary_wheels: list[tuple[Path, frozenset[str]]] = []
            for dc in desired_state.components:
                definition = registry.get(dc.component_id)
                primary_wheels.append(
                    (
                        dc.artifact.path,
                        frozenset(definition.required_extras),
                    )
                )
            if primary_wheels:
                lock = resolve_runtime_dependencies(
                    primary_wheels, wheelhouse=release_dir,
                )
            else:
                lock = None
        except DependencyResolutionError as exc:
            raise OfflineReleaseError(
                f"shared runtime dependency resolution failed: {exc}"
            ) from exc

        return build_deployment_plan(
            desired_state,
            registry=registry,
            runtime_status=runtime_status,
            dependency_lock=lock,
        )

    # ------------------------------------------------------------------
    # plan_offline_deployment  (compatibility wrapper)
    # ------------------------------------------------------------------

    def plan_offline_deployment(
        self, release_dir: Path
    ) -> DeploymentPlan:
        """Plan a full offline deployment from *release_dir* using the
        legacy packaged :class:`ComponentRegistry`.

        This is a read-only operation.  No filesystem mutation.
        """
        return self._plan_deployment_for_registry(
            self._registry, release_dir
        )

    # ------------------------------------------------------------------
    # D.4.1A: _apply_deployment_for_registry
    # ------------------------------------------------------------------

    def _apply_deployment_for_registry(
        self, registry: ComponentRegistry, release_dir: Path
    ) -> DeploymentResult:
        """Apply a complete offline deployment from *release_dir* using
        *registry* as the component authority.

        Re-resolves the release set and re-plans fresh at call time
        — a previously generated ``DeploymentPlan`` is never reused
        or persisted.  The resulting plan is applied via the existing
        :func:`apply_deployment_plan` engine.

        This mutates the shared runtime: creates a candidate slot,
        installs all components, validates, and atomically activates.
        """
        plan = self._plan_deployment_for_registry(registry, release_dir)
        return apply_deployment_plan(
            plan,
            registry=registry,
            runtime=self._runtime,
        )

    # ------------------------------------------------------------------
    # apply_offline_deployment   (compatibility wrapper)
    # ------------------------------------------------------------------

    def apply_offline_deployment(
        self, release_dir: Path
    ) -> DeploymentResult:
        """Apply a complete offline deployment from *release_dir* using
        the legacy packaged :class:`ComponentRegistry`.

        This mutates the shared runtime: creates a candidate slot,
        installs all components, validates, and atomically activates.
        """
        return self._apply_deployment_for_registry(
            self._registry, release_dir
        )

    # ------------------------------------------------------------------
    # rollback_runtime   (M0-9.2)
    # ------------------------------------------------------------------

    def rollback_runtime(self) -> RuntimeStatus:
        """Rollback the shared runtime to the previous active slot.

        Delegates to the injected :class:`SharedRuntime` rollback
        mechanism.  The previous slot becomes active and the current
        active slot becomes the new previous slot.

        This mutates the shared runtime active pointer.
        """
        return self._runtime.rollback()

    # ------------------------------------------------------------------
    # D.4.1A: Launch registry resolution
    # ------------------------------------------------------------------

    def _resolve_launch_registry(self) -> ComponentRegistry:
        """Resolve the effective registry for launch preparation.

        **Rule (read-only, no bootstrap):**

        * If the selection file does **not** exist: return the legacy
          packaged :class:`ComponentRegistry` (``self._registry``) —
          backward compatibility, including pre-D4 paths and offline
          workflows.
        * If the selection file **does** exist (including an explicit
          empty selection): materialize a :class:`ComponentRegistry`
          from the :class:`SelectionStore` via the product catalog.
          This ensures a newly installed product selected in
          ``desired-products.toml`` is launchable even when absent
          from the packaged ``components.toml``.

        An explicit empty selection is authoritative: a present-empty
        file produces an empty desired registry, making legacy-only
        components unknown / not launchable.

        **No bootstrap side effect.**  This method does NOT call
        :meth:`bootstrap_desired_selection`.  It only reads the
        selection file if it already exists.
        """
        if self._selection_store.path.exists():
            return _desired_component_registry(
                self._catalog,
                self._selection_store.current_selection(),
            )
        return self._registry

    # ------------------------------------------------------------------
    # D.4.1A: _prepare_launch_plan_for_registry
    # ------------------------------------------------------------------

    def _prepare_launch_plan_for_registry(
        self, registry: ComponentRegistry, component_id: str
    ) -> LaunchPlan:
        """Prepare a :class:`LaunchPlan` for *component_id* from the
        active shared runtime, resolving component metadata from
        *registry*.

        The component MUST be declared in *registry* and its
        distribution MUST be installed in the active runtime slot with a
        satisfied launch contract.  The resolved entry-point script is
        located inside the runtime's scripts directory — never the dev
        venv.

        This is a read-only operation; no process is started.

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in *registry*.
        LaunchPreparationError
            If the runtime is ABSENT or BROKEN.
        ComponentNotInstalledError
            If the component's distribution is not installed in the
            runtime.
        LaunchContractNotSatisfiedError
            If the installed distribution does not declare any of the
            expected entry-point contracts.
        LaunchScriptNotFoundError
            If the matched entry-point script cannot be found inside the
            runtime scripts directory.
        """
        # --- 1. Resolve component from registry ----------------------------
        try:
            definition = registry.get(component_id)
        except UnknownComponentError:
            raise  # let CLI surface it directly

        # --- 2. Require READY runtime with an active slot ------------------
        rt_status = self._runtime.status()
        if rt_status.state == RuntimeState.ABSENT:
            raise LaunchPreparationError(
                "shared runtime is absent — create or deploy a runtime first "
                "(zealfie runtime create)"
            )
        if rt_status.state == RuntimeState.BROKEN:
            raise LaunchPreparationError(
                f"shared runtime is broken: {rt_status.reason or 'unknown reason'}"
            )
        if rt_status.active_path is None or rt_status.python_executable is None:
            raise LaunchPreparationError(
                "shared runtime is READY but has no active slot path"
            )

        active_path = rt_status.active_path
        runtime_python = rt_status.python_executable

        # --- 3. Probe the distribution inside the runtime ------------------
        try:
            probe = probe_runtime_distribution(
                runtime_python, definition.distribution_name
            )
        except Exception as exc:
            raise LaunchPreparationError(
                f"could not probe runtime for {definition.distribution_name!r}: {exc}"
            ) from exc

        # --- 3b. Validate probe payload structure (M1-0A C1) ---------------
        _validate_probe_payload(probe, definition.distribution_name)

        # --- 3c. Check installed flag --------------------------------------
        if not probe.get("installed"):
            raise ComponentNotInstalledError(
                f"component {component_id!r} ({definition.distribution_name}) "
                "is not installed in the shared runtime"
            )

        # --- 4. Select a declared entry point (prefer registry order) ------
        entry_point_name = _select_entry_point_name(definition, probe)
        if entry_point_name is None:
            expected = ", ".join(
                f"{c.group}:{c.name}" for c in definition.launch_entry_points
            )
            raise LaunchContractNotSatisfiedError(
                f"component {component_id!r} is installed but none of the "
                f"expected launch entry points ({expected}) were found"
            )

        # --- 5. Resolve the entry-point script in the runtime scripts dir --
        scripts_dir = _runtime_scripts_dir(active_path)
        try:
            script_path = resolve_script(scripts_dir, entry_point_name)
        except EntryPointScriptNotFoundError as exc:
            raise LaunchScriptNotFoundError(
                f"entry-point script {entry_point_name!r} not found in "
                f"the active runtime scripts directory ({scripts_dir})"
            ) from exc

        # --- 6. Build the launch plan --------------------------------------
        return LaunchPlan(
            component_id=component_id,
            executable=script_path,
        )

    # ------------------------------------------------------------------
    # M1-0A: prepare_launch_plan  (D.4.1A: resolves launch registry)
    # ------------------------------------------------------------------

    def prepare_launch_plan(self, component_id: str) -> LaunchPlan:
        """Prepare a :class:`LaunchPlan` for *component_id* from the
        active shared runtime.

        **D.4.1A:** resolves the effective registry via
        :meth:`_resolve_launch_registry` — if the selection file
        exists (including explicit empty), the desired registry is
        used; otherwise the legacy packaged registry is used.  No
        bootstrap mutation occurs.

        This is a read-only operation; no process is started.

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in the effective registry.
        LaunchPreparationError
            If the runtime is ABSENT or BROKEN.
        ComponentNotInstalledError
            If the component's distribution is not installed in the
            runtime.
        LaunchContractNotSatisfiedError
            If the installed distribution does not declare any of the
            expected entry-point contracts.
        LaunchScriptNotFoundError
            If the matched entry-point script cannot be found inside the
            runtime scripts directory.
        """
        registry = self._resolve_launch_registry()
        return self._prepare_launch_plan_for_registry(registry, component_id)

    # ------------------------------------------------------------------
    # M1-0A: launch_component
    # ------------------------------------------------------------------

    def launch_component(
        self,
        component_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> LaunchResult:
        """Prepare and execute a launch of *component_id* from the active
        shared runtime.

        Calls :meth:`prepare_launch_plan` then
        :func:`~zealfie.launching.execute_launch_plan`.

        Parameters
        ----------
        component_id:
            The trusted component to launch.
        timeout_seconds:
            Seconds to wait for the subprocess.  ``None`` (default) means
            no timeout, which is appropriate for long-running GUI apps.
            Pass an explicit value to impose a deadline.

        Returns
        -------
        LaunchResult
            The captured stdout, stderr, return code, and timeout flag.

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in the trusted registry.
        LaunchPreparationError
            (or subclass) If preparation fails — runtime absent/broken,
            component not installed, contract not satisfied, or script
            not found.
        """
        plan = self.prepare_launch_plan(component_id)
        return execute_launch_plan(plan, timeout_seconds=timeout_seconds)

    # ------------------------------------------------------------------
    # M1-2B: spawn_component (non-blocking launch)
    # ------------------------------------------------------------------

    def spawn_component(
        self,
        component_id: str,
        *,
        env_overrides: dict[str, str] | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> "SpawnedLaunch":
        """Prepare and spawn *component_id* as a non-blocking subprocess.

        Calls :meth:`prepare_launch_plan` then
        :func:`~zealfie.launching.spawn_launch_plan`.  Returns
        immediately with a :class:`SpawnedLaunch` handle; does **not**
        wait for the child process to complete.

        The child process inherits the parent environment.  Callers may
        pass *env_overrides* to add extra variables scoped to the child.

        **ZeSolver embedded-host rule:**
        When *component_id* is ``"zesolver"``, the child environment
        automatically receives ``ZESOLVER_EMBEDDED_HOST=1``.  This
        override is scoped to the child ``Popen`` call.  ZeAlfie's own
        process never becomes an embedded host.  The override is applied
        *before* any caller-supplied *env_overrides*, so a caller can
        override it intentionally.

        Parameters
        ----------
        component_id:
            The trusted component to spawn.
        env_overrides:
            Extra environment variables for the child process.
        stdin:
            Child stdin fd.  ``None`` inherits the parent stdin.
        stdout:
            Child stdout fd.  ``None`` inherits the parent stdout.
        stderr:
            Child stderr fd.  ``None`` inherits the parent stderr.

        Returns
        -------
        SpawnedLaunch

        Raises
        ------
        UnknownComponentError
            If *component_id* is not in the trusted registry.
        LaunchPreparationError
            (or subclass) If preparation fails.
        """
        plan = self.prepare_launch_plan(component_id)

        # Build env overrides, starting with the ZeSolver rule.
        merged: dict[str, str] = {}
        if component_id == "zesolver":
            merged["ZESOLVER_EMBEDDED_HOST"] = "1"
        if env_overrides:
            merged.update(env_overrides)

        return spawn_launch_plan(
            plan,
            env_overrides=merged if merged else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    # ------------------------------------------------------------------
    # M1-2A: Product Shell — read model
    # ------------------------------------------------------------------

    @property
    def catalog(self) -> ProductCatalog:
        """The product catalog (all known products).

        Distinct from the user's selection (``SelectionStore``), which
        records what the user chose to manage; the packaged
        ``ComponentRegistry`` remains the pre-D4 deployment/launch
        contract.
        """
        return self._catalog

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        """Return all known product descriptors from the catalog.

        This is the "what ZeAlfie knows" listing — distinct from the
        user's selection (``SelectionStore``), which records what the
        user chose to manage.
        """
        return self._catalog.list()

    def collect_product_state(
        self,
        *,
        probe_fn: object = None,
    ) -> ProductShellState:
        """Collect product state for every known product against the
        current managed runtime.

        Validates the current selection against the product catalog
        before collecting state.  Raises
        :class:`~zealfie.products.catalog.UnknownProductError` if any
        selected product id is unknown.

        The *managed* set is derived from the user's selection store
        (the products the user has explicitly chosen to manage), not
        from the packaged component registry.

        This is a read-only operation.  No mutation, no installation,
        no launch.
        """
        self._validate_selection()
        managed_ids = frozenset(self._selection_store.selected_product_ids)
        return collect_product_state(
            self._catalog,
            self._runtime.status(),
            managed_component_ids=managed_ids,
            probe_fn=probe_fn,
        )

    def get_product_state(
        self,
        product_id: str,
        *,
        probe_fn: object = None,
    ) -> ProductState:
        """Collect state for a single product against the current runtime.

        Validates the current selection against the product catalog before
        collecting state.

        The *managed* status is derived from the user's selection store.

        Raises :class:`~zealfie.products.catalog.UnknownProductError` if
        *product_id* is not in the product catalog or if any selected
        product id is unknown.

        This is a read-only operation.
        """
        self._validate_selection()
        managed_ids = frozenset(self._selection_store.selected_product_ids)
        return get_product_state(
            self._catalog,
            product_id,
            self._runtime.status(),
            managed_component_ids=managed_ids,
            probe_fn=probe_fn,
        )

    @property
    def managed_product_ids(self) -> frozenset[str]:
        """Return the set of product ids currently selected by the user
        as managed (from the selection store).

        Validates every selected id against the product catalog.  Raises
        :class:`~zealfie.products.catalog.UnknownProductError` if any
        selected id is unknown.

        This is the user's explicit choice.  Pre-D4, deployment and
        launch still use the ``ComponentRegistry``.
        """
        self._validate_selection()
        return frozenset(self._selection_store.selected_product_ids)

    # ------------------------------------------------------------------
    # M1-2D.3: Selection store and materialization
    # ------------------------------------------------------------------

    def _validate_selection(self) -> None:
        """Validate that every currently-selected product id exists in
        the product catalog.

        Raises :class:`~zealfie.products.catalog.UnknownProductError` if
        any selected product id is unknown.  Called by
        :meth:`managed_product_ids`, :meth:`collect_product_state`, and
        :meth:`get_product_state` to ensure unknown persisted ids are
        surfaced as errors rather than disappearing silently.
        """
        _validate_selection_against_catalog(
            self._catalog,
            self._selection_store.current_selection(),
        )

    @property
    def selection_store(self) -> SelectionStore:
        """The user selection store."""
        return self._selection_store

    @property
    def provenance_store(self) -> ProductProvenanceStore | None:
        """The installed-product provenance store (may be ``None`` when
        disabled, e.g. for synthetic runtimes without a layout)."""
        return self._provenance_store

    @property
    def installed_lock_store(self) -> InstalledLockStore | None:
        """The installed-runtime lock store (may be ``None`` when disabled,
        e.g. for synthetic runtimes without a layout)."""
        return self._installed_lock_store

    # ------------------------------------------------------------------
    # M1-2E E.1: Installed-product provenance readback
    # ------------------------------------------------------------------

    def active_provenance(self) -> dict[str, ProductProvenance]:
        """Return provenance for the currently active runtime slot.

        A runtime with no recorded provenance (old runtime, or no provenance
        store) yields an empty mapping.  Never fabricates a commit SHA.
        """
        store = self._provenance_store
        if store is None:
            return {}
        return store.load_active()

    def product_provenance(self, product_id: str) -> ProductProvenance | None:
        """Return provenance for *product_id* in the active runtime, or
        ``None`` when unknown (never invented)."""
        store = self._provenance_store
        if store is None:
            return None
        return store.product_provenance(product_id)

    # ------------------------------------------------------------------
    # M1-2F Phase 4 corrective: Installed-runtime lock readback
    # ------------------------------------------------------------------

    def active_installed_lock(self) -> InstalledRuntimeLock | None:
        """Return the installed-runtime lock for the active slot, or ``None``.

        Observational only: this readback is never used to drive an
        install/update/rollback/KEEP decision.  A runtime with no recorded
        lock (or no store) yields ``None`` (UNKNOWN), never a fabricated lock.
        """
        store = self._installed_lock_store
        if store is None:
            return None
        return store.load_active()

    # ------------------------------------------------------------------
    # M1-2F Phase 5: Per-product channel/policy read/write API
    # ------------------------------------------------------------------

    def available_product_channels(
        self,
        product_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """Return the product's declared ``(channel, ref)`` pairs.

        This is the per-product channel authority: a channel is only ever
        available when the catalog descriptor declares it.  Unknown products
        raise :class:`~zealfie.products.catalog.UnknownProductError`.
        """
        desc = self._catalog.get(product_id)  # raises UnknownProductError
        return desc.channel_refs

    def product_policy(self, product_id: str) -> ProductPolicy:
        """Return the configured policy for *product_id*.

        Validates the product is catalog-known first (fail-closed);
        unconfigured products yield the factory default (``stable`` /
        ``follow``).
        """
        self._catalog.get(product_id)  # raises UnknownProductError
        return self._policy_store.policy_for(product_id)

    def set_product_policy(self, policy: ProductPolicy) -> ProductPolicy:
        """Validate and persist *policy* for its product id.

        Fail-closed: the product must be catalog-known, and a ``follow``
        policy must name a channel the product declares.  ``pin`` policies
        skip channel validation (pin resolves the exact SHA) but still
        require a known product id.

        Raises
        ------
        UnknownProductError
            If the product is not in the catalog.
        ProductChannelUnavailableError
            If a follow policy names an undeclared channel.
        """
        desc = self._catalog.get(policy.product_id)  # UnknownProductError
        if desc.remote_source is None:
            raise RemoteSourceUnavailableError(
                f"product {policy.product_id!r} has no remote source — "
                f"cannot configure install/update policy"
            )
        if policy.policy == "follow" and policy.channel not in desc.channel_ref_map:
            raise ProductChannelUnavailableError(
                product_id=policy.product_id,
                channel=policy.channel,
                available=desc.available_channels,
            )
        return self._policy_store.set_policy(policy)

    def set_product_channel(
        self,
        product_id: str,
        channel: str,
    ) -> ProductPolicy:
        """Set the product's discovery channel (``policy=follow``).

        Convenience wrapper around :meth:`set_product_policy` for the common
        follow-channel case.  Persists the policy so later install/update
        checks (CLI and GUI) observe the same configuration.
        """
        return self.set_product_policy(
            ProductPolicy(product_id=product_id, channel=channel, policy="follow")
        )

    # ------------------------------------------------------------------
    # M1-2E E.2: Read-only update detection
    # ------------------------------------------------------------------

    def check_product_update(
        self,
        product_id: str,
        *,
        resolver: SourceRefResolver,
    ) -> ProductUpdateResult:
        """Check *product_id* for an available update, read-only.

        Reads the product's active provenance (E.1) and resolves its
        requested source ref via the injected *resolver*, comparing the
        resolved commit SHA with the installed commit SHA.

        This is **pure read-only**: it never writes runtime state, the
        provenance file, the ``active.json`` pointer, or the selection
        store, and it never installs, launches, or applies anything.

        Outcome mapping (see :func:`zealfie.app.updates.check_product_update`):

        * no active provenance → :attr:`UpdateStatus.PROVENANCE_UNKNOWN`
        * resolver failure      → :attr:`UpdateStatus.CHECK_FAILED`
        * same commit           → :attr:`UpdateStatus.UP_TO_DATE`
        * different commit      → :attr:`UpdateStatus.UPDATE_AVAILABLE`

        Never raises for missing provenance or resolver failure.
        """
        try:
            desc = self._catalog.get(product_id)
        except UnknownProductError:
            desc = None
        return _check_product_update(
            product_id,
            self.product_provenance(product_id),
            resolver=resolver,
            policy=self._policy_store.policy_for(product_id),
            channel_refs=desc.channel_ref_map if desc is not None else None,
        )

    def check_updates(
        self,
        product_ids: Sequence[str] | None = None,
        *,
        resolver: SourceRefResolver,
    ) -> tuple[ProductUpdateResult, ...]:
        """Check zero or more products for available updates, read-only.

        When *product_ids* is ``None``, every product in the catalog is
        checked (products without active provenance yield
        :attr:`UpdateStatus.PROVENANCE_UNKNOWN`).  Explicit ids are
        checked as given, preserving order; unknown ids yield
        :attr:`UpdateStatus.PROVENANCE_UNKNOWN` rather than raising.

        Pure read-only — identical guarantees to
        :meth:`check_product_update`.
        """
        if product_ids is None:
            product_ids = self._catalog.available_ids()
        return tuple(
            self.check_product_update(product_id, resolver=resolver)
            for product_id in product_ids
        )

    # ------------------------------------------------------------------
    # M1-2E E.5: Transactional product update (service-layer convenience)
    # ------------------------------------------------------------------

    def update_product(
        self,
        product_id: str,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        dependency_wheelhouse: Path | None = None,
        probe_distribution=None,
        progress_callback=None,
    ) -> DeploymentResult | AcceleratedDeploymentResult:
        """Update an already-managed installed product, transactionally.

        This is a **narrow service-layer convenience/preflight** around the
        existing read-only :meth:`check_product_update` and the existing
        transactional :meth:`install_product` pipeline.  It is not a second
        deployment engine and performs no source resolution, fetch, build,
        planning, apply, or selection/provenance persistence of its own.

        Behaviour
        ---------

        1. **Preflight (read-only):** read the product's active provenance
           via :meth:`check_product_update` using the injected *resolver*.
           This never mutates runtime, provenance, ``active.json``, or the
           selection store.
        2. If the status is :attr:`UpdateStatus.UPDATE_AVAILABLE`, delegate
           to :meth:`install_product` with the exact same injected
           ``resolver``/``fetcher``/``work_root``/``dependency_wheelhouse``/
           ``probe_distribution``/``progress_callback`` arguments.  The
           existing transactional install path performs prepare → dependency
           acquire → plan → apply → selection → provenance exactly as for a
           fresh install.
        3. For any other status (``UP_TO_DATE``, ``PROVENANCE_UNKNOWN``,
           ``CHECK_FAILED``, ``NOT_CHECKED``, or ``CHECKING``), raise
           :class:`ProductUpdateNotApplicableError` **before** any fetch,
           build, apply, or selection/provenance mutation.  Nothing is
           installed or mutated on these paths.

        Parameters
        ----------
        product_id:
            The already-managed product to update.  Unknown ids yield
            :attr:`UpdateStatus.PROVENANCE_UNKNOWN` (no active provenance)
            and therefore :class:`ProductUpdateNotApplicableError` — no
            provenance is ever invented.
        resolver / fetcher / work_root / dependency_wheelhouse /
        probe_distribution / progress_callback:
            Identical semantics and injection points to
            :meth:`install_product`; forwarded unchanged on the actual
            update attempt.

        Returns
        -------
        DeploymentResult or AcceleratedDeploymentResult
            The exact result from the transactional deployment engine,
            returned verbatim from :meth:`install_product`.

        Raises
        ------
        ProductUpdateNotApplicableError
            If the preflight status is anything other than
            :attr:`UpdateStatus.UPDATE_AVAILABLE`.  Carries the preflight
            :class:`ProductUpdateResult` and a human-readable reason.
            Raised before any mutation.

        Any exception raised by :meth:`install_product` during the actual
        update attempt propagates unchanged — deployment/build/fetch
        failures are not wrapped here.

        ZA-M1-2L (D1): the update preflight and the delegated install run under the ``product-update`` mutation lease, acquired at entry and released on every exit path including exceptions; the nested ``product-install`` acquisition reuses this lease (same root, same context).
        """
        lock = self._runtime_mutation_lock()
        if lock is None:
            return self._update_product(
                product_id,
                resolver=resolver,
                fetcher=fetcher,
                work_root=work_root,
                dependency_wheelhouse=dependency_wheelhouse,
                probe_distribution=probe_distribution,
                progress_callback=progress_callback,
            )
        with lock.acquire(OPERATION_PRODUCT_UPDATE):
            return self._update_product(
                product_id,
                resolver=resolver,
                fetcher=fetcher,
                work_root=work_root,
                dependency_wheelhouse=dependency_wheelhouse,
                probe_distribution=probe_distribution,
                progress_callback=progress_callback,
            )


    def _update_product(
        self,
        product_id: str,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        dependency_wheelhouse: Path | None = None,
        probe_distribution=None,
        progress_callback=None,
    ) -> DeploymentResult | AcceleratedDeploymentResult:
        """Update an already-managed installed product, transactionally.

        This is a **narrow service-layer convenience/preflight** around the
        existing read-only :meth:`check_product_update` and the existing
        transactional :meth:`install_product` pipeline.  It is not a second
        deployment engine and performs no source resolution, fetch, build,
        planning, apply, or selection/provenance persistence of its own.

        Behaviour
        ---------

        1. **Preflight (read-only):** read the product's active provenance
           via :meth:`check_product_update` using the injected *resolver*.
           This never mutates runtime, provenance, ``active.json``, or the
           selection store.
        2. If the status is :attr:`UpdateStatus.UPDATE_AVAILABLE`, delegate
           to :meth:`install_product` with the exact same injected
           ``resolver``/``fetcher``/``work_root``/``dependency_wheelhouse``/
           ``probe_distribution``/``progress_callback`` arguments.  The
           existing transactional install path performs prepare → dependency
           acquire → plan → apply → selection → provenance exactly as for a
           fresh install.
        3. For any other status (``UP_TO_DATE``, ``PROVENANCE_UNKNOWN``,
           ``CHECK_FAILED``, ``NOT_CHECKED``, or ``CHECKING``), raise
           :class:`ProductUpdateNotApplicableError` **before** any fetch,
           build, apply, or selection/provenance mutation.  Nothing is
           installed or mutated on these paths.

        Parameters
        ----------
        product_id:
            The already-managed product to update.  Unknown ids yield
            :attr:`UpdateStatus.PROVENANCE_UNKNOWN` (no active provenance)
            and therefore :class:`ProductUpdateNotApplicableError` — no
            provenance is ever invented.
        resolver / fetcher / work_root / dependency_wheelhouse /
        probe_distribution / progress_callback:
            Identical semantics and injection points to
            :meth:`install_product`; forwarded unchanged on the actual
            update attempt.

        Returns
        -------
        DeploymentResult or AcceleratedDeploymentResult
            The exact result from the transactional deployment engine,
            returned verbatim from :meth:`install_product`.

        Raises
        ------
        ProductUpdateNotApplicableError
            If the preflight status is anything other than
            :attr:`UpdateStatus.UPDATE_AVAILABLE`.  Carries the preflight
            :class:`ProductUpdateResult` and a human-readable reason.
            Raised before any mutation.

        Any exception raised by :meth:`install_product` during the actual
        update attempt propagates unchanged — deployment/build/fetch
        failures are not wrapped here.
        """
        preflight = self.check_product_update(product_id, resolver=resolver)
        if preflight.status is not UpdateStatus.UPDATE_AVAILABLE:
            raise ProductUpdateNotApplicableError(preflight)

        # Actual update: reuse the existing transactional install pipeline.
        # No direct apply_deployment_plan call — install_product owns the
        # prepare → acquire → plan → apply → selection → provenance sequence.
        return self.install_product(
            product_id,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
            dependency_wheelhouse=dependency_wheelhouse,
            probe_distribution=probe_distribution,
            progress_callback=progress_callback,
        )

    def bootstrap_desired_selection(self) -> DesiredProductSelection:
        """Ensure the selection file is initialised from the legacy
        :class:`~zealfie.components.registry.ComponentRegistry`
        (packaged ``components.toml``) before any desired-state mutation.

        This is a one-shot/idempotent guard: if the selection file
        already exists (including an explicit empty selection), it is
        authoritative and no bootstrap occurs.  Otherwise the legacy
        registry's ids are validated against the product catalog and
        persisted as the initial desired selection.

        **D.4.0**: this prevents `absent + zemosaic -> {zemosaic}` by
        ensuring the legacy managed set (e.g. ``zesolver``) is preserved
        before any additive ``select_product`` call.

        Returns
        -------
        DesiredProductSelection
            The authoritative selection — freshly bootstrapped or
            loaded from the existing file.

        Raises
        ------
        UnknownProductError
            If any legacy component id is not in the product catalog.
            The selection file is never written on this error.
        """
        return bootstrap_selection_from_legacy_registry(
            self._selection_store,
            self._catalog,
            self._registry,
        )

    def select_product(self, product_id: str) -> DesiredProductSelection:
        """Select a product for management and persist the choice.

        **D.3 contract:** validates *product_id* against the catalog
        first.  If unknown, raises
        :class:`~zealfie.products.catalog.UnknownProductError` without
        writing or bootstrapping the selection file.

        Guarantees a one-shot bootstrap from the legacy
        :class:`~zealfie.components.registry.ComponentRegistry` before
        the first additive mutation so that legacy managed products are
        never orphaned (D.4.0).

        * Idempotent — selecting an already-selected product is a no-op.
        * Raises :class:`~zealfie.products.catalog.UnknownProductError`
          for unknown product ids without mutating the persisted file
          or triggering a bootstrap.

        This does NOT install the product, build wheels, or mutate the
        runtime.  It only records the user's intent.
        """
        # D.3: Validate before any file I/O — unknown product must not
        # trigger a bootstrap or write any selection file.
        self._catalog.get(product_id)  # raises UnknownProductError
        self.bootstrap_desired_selection()
        return self._selection_store.select(product_id, catalog=self._catalog)

    def desired_selection(self) -> DesiredProductSelection:
        """Return the current user selection.

        This is the raw "what the user wants to manage" store view,
        persisted across sessions independently of the runtime or
        catalog.  Catalog-interpreting paths such as
        :meth:`managed_product_ids`, :meth:`collect_product_state`, and
        :meth:`materialize_desired_components` validate selected ids
        before use.
        """
        return self._selection_store.current_selection()

    def materialize_desired_components(
        self,
    ) -> tuple[ComponentDefinition, ...]:
        """Materialize the user's selection into component definitions.

        Converts every selected product descriptor to a
        :class:`~zealfie.components.model.ComponentDefinition`.
        Unknown selected ids raise
        :class:`~zealfie.products.catalog.UnknownProductError`.

        This is a pure read — no mutation, no network, no install.
        """
        return _materialize_desired_components(
            self._catalog,
            self._selection_store.current_selection(),
        )

    def desired_component_registry(self) -> ComponentRegistry:
        """Return a :class:`ComponentRegistry` built from the user's
        selection against the product catalog.

        Convenience wrapper around :meth:`materialize_desired_components`.

        This does NOT replace the deployment/launch registry.
        """
        return _desired_component_registry(
            self._catalog,
            self._selection_store.current_selection(),
        )


# ---------------------------------------------------------------------------
# M1-0A helpers
# ---------------------------------------------------------------------------

def _validate_probe_payload(
    probe: object,
    distribution_name: str,
) -> None:
    """Validate the structure of a probe payload.

    Raises :class:`LaunchPreparationError` for any malformed field so
    that callers never receive an ``AttributeError`` or ``TypeError``
    from downstream consumers such as :func:`_select_entry_point_name`.

    Rules (aligned with :func:`zealfie.runtime.planning._validate_probe_payload`):

    * ``installed`` must be exactly ``bool``.
    * If ``installed is False``:
        * ``version`` must be present and exactly ``None``.
        * ``entry_points`` must be present, must be a ``list``, and
          must be empty.
    * If ``installed is True``:
        * ``version`` must be a non-empty ``str``.
        * ``entry_points`` must be a ``list``.
        * Every item in ``entry_points`` must be a ``dict`` whose
          ``group`` and ``name`` values are non-empty ``str``.
    """
    if not isinstance(probe, dict):
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r} returned an "
            f"unexpected type {type(probe).__name__!r} (expected dict)"
        )

    installed = probe.get("installed")
    if not isinstance(installed, bool):
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r} returned "
            f"non-bool 'installed' field: {installed!r}"
        )

    if installed is False:
        # version must exist and be exactly None.
        if "version" not in probe:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                "missing 'version' key when installed=False"
            )
        version = probe["version"]
        if version is not None:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"version must be None when installed=False, "
                f"got {type(version).__name__}={version!r}"
            )
        # entry_points must exist, be a list, and be empty.
        if "entry_points" not in probe:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                "missing 'entry_points' key when installed=False"
            )
        entry_points = probe["entry_points"]
        if not isinstance(entry_points, list):
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points must be a list when installed=False, "
                f"got {type(entry_points).__name__}"
            )
        if len(entry_points) != 0:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points must be empty when installed=False, "
                f"got {type(entry_points).__name__} "
                f"with {len(entry_points)} item(s)"
            )
        return

    # installed is True
    version = probe.get("version")
    if not isinstance(version, str) or not version:
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r}: "
            f"version must be non-empty str when installed=True, "
            f"got {type(version).__name__}={version!r}"
        )

    entry_points = probe.get("entry_points")
    if not isinstance(entry_points, list):
        raise LaunchPreparationError(
            f"runtime probe for {distribution_name!r} returned "
            f"non-list 'entry_points' field: {entry_points!r}"
        )

    for i, ep in enumerate(entry_points):
        if not isinstance(ep, dict):
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points[{i}] is not a dict: {ep!r}"
            )
        group = ep.get("group")
        if not isinstance(group, str) or not group:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points[{i}] has missing or non-string "
                f"'group': {group!r}"
            )
        name = ep.get("name")
        if not isinstance(name, str) or not name:
            raise LaunchPreparationError(
                f"runtime probe for {distribution_name!r}: "
                f"entry_points[{i}] has missing or non-string "
                f"'name': {name!r}"
            )


def _select_entry_point_name(
    definition: ComponentDefinition,
    probe: dict[str, object],
) -> str | None:
    """Return the script *name* of the first registry entry-point contract
    that is present in the probe's entry points, or ``None``."""
    observed_eps = probe.get("entry_points", [])
    if not isinstance(observed_eps, list):
        return None

    # Build a set of observed (group, name) → name for fast lookup.
    observed: dict[tuple[str, str], str] = {}
    for ep in observed_eps:
        if not isinstance(ep, dict):
            continue  # defense-in-depth: skip non-dict entries
        g = str(ep.get("group", ""))
        n = str(ep.get("name", ""))
        if g and n:
            observed[(g, n)] = n

    # Walk registry contracts in definition order.
    for contract in definition.launch_entry_points:
        key = (contract.group, contract.name)
        if key in observed:
            return observed[key]

    return None


def _runtime_scripts_dir(venv_dir: Path) -> Path:
    """Return the scripts directory inside a runtime venv slot."""
    if sys.platform == "win32":
        return venv_dir / "Scripts"
    else:
        return venv_dir / "bin"


# ---------------------------------------------------------------------------
# D.4.1C: Module-level helpers for prepared artifact → deployment plan bridge
# ---------------------------------------------------------------------------


def _validate_prepared_artifacts(
    prepared_artifacts: Sequence[PreparedProductArtifact],
) -> None:
    """Validate the prepared artifact sequence before planning.

    Fails closed with :class:`ProductDeploymentPlanningError` for:

    * Empty sequence.
    * Duplicate *product_id* values.
    * Duplicate *component_id* values.
    * Mismatch between ``product_id``, ``component_id``, and
      ``verified_artifact.component_id`` on any single artifact.
    """
    if not prepared_artifacts:
        raise ProductDeploymentPlanningError(
            "at least one prepared product artifact is required for planning"
        )

    seen_product_ids: set[str] = set()
    seen_component_ids: set[str] = set()

    for pa in prepared_artifacts:
        # Mismatch checks.
        if pa.product_id != pa.component_id:
            raise ProductDeploymentPlanningError(
                f"product_id {pa.product_id!r} != component_id "
                f"{pa.component_id!r} — must match"
            )
        if pa.product_id != pa.verified_artifact.component_id:
            raise ProductDeploymentPlanningError(
                f"product_id {pa.product_id!r} != "
                f"verified_artifact.component_id "
                f"{pa.verified_artifact.component_id!r} — must match"
            )
        if pa.component_id != pa.verified_artifact.component_id:
            raise ProductDeploymentPlanningError(
                f"component_id {pa.component_id!r} != "
                f"verified_artifact.component_id "
                f"{pa.verified_artifact.component_id!r} — must match"
            )

        # Duplicate checks.
        if pa.product_id in seen_product_ids:
            raise ProductDeploymentPlanningError(
                f"duplicate product_id: {pa.product_id!r}"
            )
        if pa.component_id in seen_component_ids:
            raise ProductDeploymentPlanningError(
                f"duplicate component_id: {pa.component_id!r}"
            )

        seen_product_ids.add(pa.product_id)
        seen_component_ids.add(pa.component_id)


def _registry_for_prepared_products(
    catalog: ProductCatalog,
    prepared_artifacts: Sequence[PreparedProductArtifact],
) -> ComponentRegistry:
    """Build a :class:`ComponentRegistry` from product catalog
    descriptors corresponding to the prepared artifacts.

    The registry is always derived from the catalog — never from the
    legacy packaged registry and never from wheel metadata alone.
    Each prepared artifact's *product_id* must already be validated
    against the catalog before calling this function.
    """
    definitions: list[ComponentDefinition] = []
    for pa in prepared_artifacts:
        desc = catalog.get(pa.product_id)
        definitions.append(
            ComponentDefinition(
                component_id=desc.product_id,
                display_name=desc.display_name,
                distribution_name=desc.distribution_name,
                launch_entry_points=desc.launch_entry_points,
                required_extras=desc.required_extras,
            )
        )
    return ComponentRegistry(definitions)


def _desired_state_from_prepared_artifacts(
    prepared_artifacts: Sequence[PreparedProductArtifact],
) -> DesiredRuntimeState:
    """Build a :class:`DesiredRuntimeState` from the verified artifacts
    carried by the prepared product artifacts.

    Each :class:`DesiredComponent` is built from the
    :class:`VerifiedArtifact` inside the :class:`PreparedProductArtifact`.
    The artifact proof (path, size, SHA256, identity) is preserved — no
    re-verification occurs at this stage.  The prepared artifact's
    service-level *origin* (keep/update/install) is carried onto the
    desired component for honest progress wording (ZA-M1-3A.3 LOT E).
    """
    components = tuple(
        DesiredComponent(
            component_id=pa.verified_artifact.component_id,
            version=pa.verified_artifact.version,
            artifact=pa.verified_artifact,
            origin=pa.origin,
        )
        for pa in prepared_artifacts
    )
    return DesiredRuntimeState(components=components)


def _effective_product_ref(
    desc: ProductDescriptor,
    policy: ProductPolicy,
) -> str:
    """Return the effective requested ref for *policy* against *desc*.

    M1-2F Phase 5: the product descriptor is the authority for which
    channels exist.  ``DEFAULT_CHANNEL_REFS`` is only a default mapper, so a
    follow policy naming a channel the product did not declare fails closed
    here (before any resolver/network) with
    :class:`ProductChannelUnavailableError`.

    * ``pin``    → the pinned immutable SHA (``policy.pin_sha``).  The
      channel is ignored and never validated; the product id/remote source
      are still validated by the caller.
    * ``follow`` → the declared channel ref (or the policy's explicit
      ``source_ref`` override when present), via
      :func:`~zealfie.products.policy.effective_ref` using the descriptor's
      product-specific channel mapping.
    """
    if policy.policy == "pin":
        return policy.pin_sha  # validated non-None 40-hex by ProductPolicy
    channel_ref_map = desc.channel_ref_map
    if policy.channel not in channel_ref_map:
        raise ProductChannelUnavailableError(
            product_id=desc.product_id,
            channel=policy.channel,
            available=desc.available_channels,
        )
    return effective_ref(policy, channel_refs=channel_ref_map)


def _provenance_entries_for(
    prepared_artifacts: Sequence[PreparedProductArtifact],
) -> tuple[ProductProvenance, ...]:
    """Build provenance entries from prepared product artifacts.

    Uses the prepared artifacts as the source of truth: ``resolved_source``
    (owner/repo/ref, exact commit SHA) and ``verified_artifact`` (version,
    wheel SHA-256).  ``version`` is the verified artifact's ``version``
    (equal to ``wheel_version`` for prepared artifacts).

    Discovery-policy metadata (M1-2F Phase 4) is persisted only when the
    artifact carries an exact :class:`ProductPolicy`; otherwise the entry
    records ``None`` policy metadata (policy-unknown) without fabricating
    a channel/policy/pin.
    """
    entries: list[ProductProvenance] = []
    for pa in prepared_artifacts:
        resolved = pa.resolved_source
        verified = pa.verified_artifact
        channel, policy, pin_sha = _provenance_policy_fields(pa.policy)
        entries.append(
            ProductProvenance(
                product_id=pa.product_id,
                version=verified.version,
                source_owner=resolved.source.owner,
                source_repo=resolved.source.repo,
                requested_ref=resolved.source.ref,
                commit_sha=resolved.commit_sha,
                wheel_sha256=verified.sha256,
                channel=channel,
                policy=policy,
                pin_sha=pin_sha,
            )
        )
    return tuple(entries)


def _provenance_policy_fields(
    policy: ProductPolicy | None,
) -> tuple[str | None, str | None, str | None]:
    """Map a discovery policy to provenance metadata fields.

    * ``follow`` → ``(channel, "follow", None)`` — the discovery channel is
      recorded; ``requested_ref`` (elsewhere) holds the effective ref.
    * ``pin``    → ``(None, "pin", pin_sha)`` — no discovery channel; the
      pinned SHA is recorded as the immutable target.
    * ``None``   → ``(None, None, None)`` — policy-unknown (never invented).
    """
    if policy is None:
        return None, None, None
    if policy.policy == "pin":
        return None, "pin", policy.pin_sha
    return policy.channel, "follow", None


def _policy_from_provenance(provenance: ProductProvenance) -> ProductPolicy | None:
    """Reconstruct discovery-policy metadata from an active provenance record.

    Used by the KEEP path to propagate known policy metadata forward without
    any re-resolution.  Returns ``None`` (policy-unknown) when the provenance
    carries no policy metadata (pre-Phase-4 v1 entry) or its metadata is not
    self-consistent enough to form a valid :class:`ProductPolicy`.  Never
    invents a follow/stable default for a legacy entry.
    """
    if provenance.policy == "pin":
        if provenance.pin_sha:
            try:
                return ProductPolicy(
                    product_id=provenance.product_id,
                    policy="pin",
                    pin_sha=provenance.pin_sha,
                )
            except ValueError:
                return None
        return None
    if provenance.policy == "follow":
        if provenance.channel:
            try:
                return ProductPolicy(
                    product_id=provenance.product_id,
                    policy="follow",
                    channel=provenance.channel,
                )
            except ValueError:
                return None
        return None
    return None


# ---------------------------------------------------------------------------
# Best-effort rmtree (no third-party dependency for staging cleanup)
# ---------------------------------------------------------------------------


def _rmtree_best_effort(directory: Path) -> None:
    """Best-effort recursive directory removal; silently ignores all errors."""
    import shutil as _shutil

    try:
        if directory.is_dir():
            _shutil.rmtree(directory)
    except Exception:
        pass


def _private_acquisition_staging(work_root: Path) -> Path:
    """Create a private unique dependency staging directory under *work_root*.

    Returns a resolved :class:`Path` to the newly created directory.
    Callers own cleanup (see :func:`_rmtree_best_effort`).
    """
    work_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="zealfie-acq-", dir=work_root)).resolve()


def _emit_progress(
    callback,
    phase: InstallPhase,
    percent: int,
    message: str,
) -> None:
    """Invoke the optional progress callback with a single observation.

    No-op when *callback* is ``None``.  Progress is observational only:
    it must never influence control flow, exceptions, or results.  A
    callback that raises is logged and swallowed so observation cannot
    alter install behaviour.
    """
    if callback is not None:
        try:
            callback(InstallProgress(phase=phase, percent=percent, message=message))
        except Exception:
            logger.debug(
                "Progress callback raised during %s; ignoring (observational only)",
                phase.value,
                exc_info=True,
            )


def _product_display_name(catalog, product_id: str) -> str:
    """Return a product's display name, falling back to its id.

    Never raises: progress messages must not introduce new failure modes.
    """
    try:
        return catalog.get(product_id).display_name
    except Exception:
        return str(product_id)


def _completion_message(catalog, prepared_artifacts) -> str:
    """Return the user-facing completion message for a successful install."""
    if len(prepared_artifacts) == 1:
        name = _product_display_name(catalog, prepared_artifacts[0].product_id)
        return f"{name} is ready"
    return "Installation complete."
