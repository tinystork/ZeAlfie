"""Application-level deployment orchestration service (M0-9).

M0-9.1: read-only offline deployment planning (plan_offline_deployment).
M0-9.2: apply + rollback orchestration using existing runtime primitives.
M0-9.3: CLI commands delegate to this service.
"""

from __future__ import annotations

from pathlib import Path

from zealfie.components.registry import ComponentRegistry, default_registry
from zealfie.releases.manifest import (
    ReleaseManifestError,
    parse_release_manifest_file,
)
from zealfie.releases.model import HostTarget
from zealfie.releases.resolver import ReleaseResolutionError, resolve_local_release
from zealfie.runtime.deployment import apply_deployment_plan
from zealfie.runtime.manager import SharedRuntime
from zealfie.runtime.model import DeploymentResult, RuntimeStatus
from zealfie.runtime.planning import (
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    build_deployment_plan,
)


class OfflineReleaseError(ValueError):
    """Raised when an offline release directory cannot be resolved.

    Wraps all lower-level failures (missing manifests, parse errors,
    artifact resolution failures, extra unknown manifests) into a
    single application-layer error type.
    """


class ZeAlfieService:
    """Application-level deployment orchestration for ZeAlfie.

    Provides ``plan_offline_deployment`` for preview/planning,
    ``apply_offline_deployment`` for orchestrated apply, and
    ``rollback_runtime`` for rollback.

    Dependencies (registry, runtime, host) are injectable so tests
    can supply synthetic instances.
    """

    def __init__(
        self,
        *,
        registry: ComponentRegistry | None = None,
        runtime: SharedRuntime | None = None,
        host: HostTarget | None = None,
    ) -> None:
        self._registry = registry or default_registry()
        self._runtime = runtime or SharedRuntime()
        self._host = host or HostTarget.from_current_host()

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

    # ------------------------------------------------------------------
    # resolve_offline_release_set
    # ------------------------------------------------------------------

    def resolve_offline_release_set(
        self, release_dir: Path
    ) -> DesiredRuntimeState:
        """Resolve a complete offline release set from *release_dir*.

        Reads every manifest, resolves each to a
        :class:`VerifiedArtifact` via the safe local release resolver,
        and returns a complete :class:`DesiredRuntimeState`.

        This is a read-only operation.  No filesystem mutation.
        """
        # Every known component must have a manifest.
        expected_ids = frozenset(self._registry.available_ids())

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
                    registry=self._registry,
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
    # plan_offline_deployment
    # ------------------------------------------------------------------

    def plan_offline_deployment(
        self, release_dir: Path
    ) -> DeploymentPlan:
        """Plan a full offline deployment from *release_dir*.

        Resolves the complete release set for all registry components,
        builds the desired runtime state, then builds a read-only
        deployment plan from the current runtime status.

        This is a read-only operation.  No filesystem mutation.
        """
        desired_state = self.resolve_offline_release_set(release_dir)
        runtime_status = self._runtime.status()
        return build_deployment_plan(
            desired_state,
            registry=self._registry,
            runtime_status=runtime_status,
        )

    # ------------------------------------------------------------------
    # apply_offline_deployment   (M0-9.2)
    # ------------------------------------------------------------------

    def apply_offline_deployment(
        self, release_dir: Path
    ) -> DeploymentResult:
        """Apply a complete offline deployment from *release_dir*.

        Re-resolves the release set and re-plans fresh at call time
        — a previously generated ``DeploymentPlan`` is never reused
        or persisted.  The resulting plan is applied via the existing
        :func:`apply_deployment_plan` engine.

        This mutates the shared runtime: creates a candidate slot,
        installs all components, validates, and atomically activates.
        """
        plan = self.plan_offline_deployment(release_dir)
        return apply_deployment_plan(
            plan,
            registry=self._registry,
            runtime=self._runtime,
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
