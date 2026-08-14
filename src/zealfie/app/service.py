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
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import logging
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path

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
    default_catalog,
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
from zealfie.releases.verifier import verify_artifact
from zealfie.runtime.deployment import apply_deployment_plan
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.model import (
    DeploymentResult,
    RuntimeState,
    RuntimeStatus,
)
from zealfie.runtime.provenance import ProductProvenance, ProductProvenanceStore
from zealfie.runtime.planning import (
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    build_deployment_plan,
)
from zealfie.runtime.probe import probe_runtime_distribution
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
    """

    product_id: str
    component_id: str
    resolved_source: ResolvedSource
    wheel_path: Path
    verified_artifact: VerifiedArtifact


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
        resolved = resolve_source(desc.remote_source, resolver=resolver)

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

        # 7. Compute SHA256 and size of the built wheel.
        wheel_size = wheel_path.stat().st_size
        sha256_hash = hashlib.sha256()
        with open(wheel_path, "rb") as fh:
            while chunk := fh.read(1 << 20):  # 1 MiB chunks
                sha256_hash.update(chunk)
        wheel_sha256 = sha256_hash.hexdigest()

        # 8. Inspect wheel for identity metadata (version, distribution_name).
        from zealfie.building import inspect_wheel

        info = inspect_wheel(wheel_path)

        # 9. Materialize a single-product ComponentRegistry from the
        #    product descriptor — mirrors materialize_desired_components.
        component_def = ComponentDefinition(
            component_id=desc.product_id,
            display_name=desc.display_name,
            distribution_name=desc.distribution_name,
            launch_entry_points=desc.launch_entry_points,
            required_extras=desc.required_extras,
        )
        registry = ComponentRegistry([component_def])

        # 10. Synthesize a single-artifact ReleaseManifest from the
        #     built wheel's observed identity and integrity.
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

        # 11. Verify through the existing release verification chain.
        #     This checks: path safety, size, SHA256, wheel identity,
        #     version match, distribution name match, and entry-point
        #     contract.  Raises ArtifactRejectionError on any failure.
        verified = verify_artifact(
            manifest,
            registry=registry,
            artifact_root=wheel_path.parent,
        )

        # 12. Return the prepared artifact — no runtime mutation has
        #     occurred, no install, no selection persistence.
        return PreparedProductArtifact(
            product_id=desc.product_id,
            component_id=desc.product_id,
            resolved_source=resolved,
            wheel_path=wheel_path,
            verified_artifact=verified,
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

        A fresh ``wheel_sha256`` from the rebuild is recorded downstream by
        the existing provenance persistence, so an artifact rebuilt from
        the same source SHA is always described honestly.
        """
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
        return prepared


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

    ) -> DeploymentResult:

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



        Returns

        -------

        DeploymentResult

            *success=True* with the new active slot id on

            successful apply.  *success=False* with a reason

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
            ready_message = _completion_message(
                self._catalog, prepared_artifacts,
            )
            _emit_progress(

                progress_callback,

                InstallPhase.COMPLETED,

                PHASE_PERCENT[InstallPhase.COMPLETED],

                ready_message,

            )



        return result



    # ------------------------------------------------------------------
    # M1-2E E.1: provenance persistence (post-activation)
    # ------------------------------------------------------------------

    def _persist_provenance(
        self,
        prepared_artifacts: Sequence[PreparedProductArtifact],
        result: DeploymentResult,
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
    ) -> DeploymentResult:
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

        Returns
        -------
        DeploymentResult
            The exact result from the transactional deployment engine.

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
            for pid in desired_ids:
                if pid == product_id:
                    if progress_callback is not None:
                        pa = self.prepare_product_artifact(
                            pid,
                            resolver=resolver,
                            fetcher=fetcher,
                            work_root=work_root,
                            progress_callback=progress_callback,
                        )
                    else:
                        pa = self.prepare_product_artifact(
                            pid,
                            resolver=resolver,
                            fetcher=fetcher,
                            work_root=work_root,
                        )
                else:
                    pa = self._prepare_keep_product_artifact(
                        pid,
                        active_provenance[pid],
                        fetcher=fetcher,
                        work_root=work_root,
                        progress_callback=progress_callback,
                    )
                prepared.append(pa)

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
                    for pa in prepared:
                        desc = self._catalog.get(pa.product_id)
                        req = build_acquisition_request(
                            pa.wheel_path,
                            active_extras=frozenset(desc.required_extras),
                        )
                        self._acquirer.acquire(
                            req, staging_dir=auto_staging,
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
                )
            return self.install_prepared_product_deployment(
                prepared,
                dependency_wheelhouse=dependency_wheelhouse,
                probe_distribution=probe_distribution,
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
        return _check_product_update(
            product_id,
            self.product_provenance(product_id),
            resolver=resolver,
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
    ) -> DeploymentResult:
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
        DeploymentResult
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
    re-verification occurs at this stage.
    """
    components = tuple(
        DesiredComponent(
            component_id=pa.verified_artifact.component_id,
            version=pa.verified_artifact.version,
            artifact=pa.verified_artifact,
        )
        for pa in prepared_artifacts
    )
    return DesiredRuntimeState(components=components)


def _provenance_entries_for(
    prepared_artifacts: Sequence[PreparedProductArtifact],
) -> tuple[ProductProvenance, ...]:
    """Build provenance entries from prepared product artifacts.

    Uses the prepared artifacts as the source of truth: ``resolved_source``
    (owner/repo/ref, exact commit SHA) and ``verified_artifact`` (version,
    wheel SHA-256).  ``version`` is the verified artifact's ``version``
    (equal to ``wheel_version`` for prepared artifacts).
    """
    entries: list[ProductProvenance] = []
    for pa in prepared_artifacts:
        resolved = pa.resolved_source
        verified = pa.verified_artifact
        entries.append(
            ProductProvenance(
                product_id=pa.product_id,
                version=verified.version,
                source_owner=resolved.source.owner,
                source_repo=resolved.source.repo,
                requested_ref=resolved.source.ref,
                commit_sha=resolved.commit_sha,
                wheel_sha256=verified.sha256,
            )
        )
    return tuple(entries)


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
