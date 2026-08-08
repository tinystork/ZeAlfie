"""Transactional offline deployment engine (M0-8B).

Applies a validated :class:`DeploymentPlan` to the shared runtime:
preflight → candidate creation → full-state materialization → candidate
validation → pre-activation M0-6 revalidation → atomic activation.

All installation is offline (``--no-index --no-deps``) and every
artifact is TOCTOU-revalidated immediately before pip handoff.
"""

from __future__ import annotations

import venv

from zealfie.components.model import ComponentDefinition
from zealfie.components.registry import ComponentRegistry
from zealfie.releases.model import VerifiedArtifact
from zealfie.releases.verifier import ArtifactRejectionError, revalidate_verified_artifact

from .manager import SharedRuntime
from .model import (
    DeploymentResult,
    InstallOutcome,
    RuntimeState,
)
from .planning import (
    DeploymentPlan,
    DesiredComponent,
    PlanningError,
    check_desired_state_conflicts,
)


def apply_deployment_plan(
    plan: DeploymentPlan,
    *,
    registry: ComponentRegistry,
    runtime: SharedRuntime | None = None,
) -> DeploymentResult:
    """Apply a complete deployment plan to the shared runtime.

    This is the single transactional entry point for offline deployment.
    It performs the full sequence:

    1. **Preflight** — validate the plan against the current runtime state
       and reject before any filesystem mutation.
    2. **Candidate creation** — begin an M0-6 transaction and create a
       fresh venv directly at the final slot path.
    3. **Full-state materialization** — install every desired component
       in deterministic component-id order, regardless of KEEP/INSTALL.
       Each artifact is TOCTOU-revalidated immediately before pip.
    4. **Candidate validation** — verify every component is installed
       correctly within the candidate.
    5. **Activation** — atomically switch the active pointer (M0-6).

    The active pointer is never modified before step 5.  On failure at
    any stage, the partially-created candidate is left for diagnostics
    and the active slot is unchanged.

    Parameters
    ----------
    plan:
        A complete, valid deployment plan from :func:`build_deployment_plan`.
    registry:
        The trusted local component registry.  Must contain definitions
        matching every component in the plan's desired state.
    runtime:
        The shared runtime target.  Created with default layout if ``None``.

    Returns
    -------
    DeploymentResult
        *success=True* with the new active slot id when activation succeeds.
        *success=False* with a reason string on any failure.
    """
    if runtime is None:
        runtime = SharedRuntime()

    # ---- 1. Preflight: blocked plan -----------------------------------------
    if plan.blocked:
        return DeploymentResult(
            success=False,
            reason=f"deployment plan is blocked: {plan.blocked_reason or 'unknown'}",
        )

    # ---- 2. Preflight: stale plan detection ---------------------------------
    status = runtime.status()

    # BROKEN runtime → fail closed, no repair.
    if status.state == RuntimeState.BROKEN:
        return DeploymentResult(
            success=False,
            reason=f"shared runtime is BROKEN: {status.reason or 'unknown'}",
        )

    # Stale plan: source active slot differs from current active slot.
    if plan.source_active_slot_id != status.active_slot_id:
        return DeploymentResult(
            success=False,
            reason=(
                f"stale deployment plan: plan built from active slot "
                f"{plan.source_active_slot_id!r}, but current runtime "
                f"active slot is {status.active_slot_id!r}"
            ),
        )

    # ---- 3. Preflight: validate desired state against registry ---------------
    desired_ids = frozenset(
        dc.component_id for dc in plan.desired_state.components
    )
    registry_ids = frozenset(registry.available_ids())

    if desired_ids != registry_ids:
        return DeploymentResult(
            success=False,
            reason=(
                f"desired state does not match registry: "
                f"registry has {sorted(registry_ids)}, "
                f"desired has {sorted(desired_ids)}"
            ),
        )

    # ---- 3.5  Preflight: shared-runtime conflict check -----------------
    try:
        check_desired_state_conflicts(plan.desired_state, registry)
    except PlanningError as exc:
        return DeploymentResult(
            success=False,
            reason=f"shared-runtime conflict detected at apply time: {exc}",
        )

    # ---- 4. Candidate creation ----------------------------------------------
    try:
        txn = runtime.begin_transaction()
    except Exception as exc:
        return DeploymentResult(
            success=False,
            reason=f"failed to begin transaction: {exc}",
        )

    # Create the candidate venv directly at the final slot path.
    candidate_path = txn.candidate_path
    if candidate_path.exists():
        return DeploymentResult(
            success=False,
            reason=f"candidate slot path already exists: {candidate_path}",
        )

    try:
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        venv.create(candidate_path, with_pip=True, clear=False)
    except Exception as exc:
        return DeploymentResult(
            success=False,
            reason=f"failed to create candidate venv: {exc}",
        )

    # ---- 5. Full-state materialization --------------------------------------
    # Install every desired component in deterministic component_id order,
    # regardless of KEEP/INSTALL action.  Each artifact is TOCTOU-revalidated
    # immediately before pip handoff.
    definitions: list[ComponentDefinition] = []

    for desired in plan.desired_state.components:
        # Resolve definition from registry.
        try:
            definition = registry.get(desired.component_id)
        except KeyError:
            return DeploymentResult(
                success=False,
                reason=f"component {desired.component_id!r} not found in registry",
            )

        # TOCTOU revalidation of the artifact before pip.
        try:
            fresh_artifact = _revalidate_artifact(
                desired, registry
            )
        except ArtifactRejectionError as exc:
            return DeploymentResult(
                success=False,
                reason=(
                    f"artifact revalidation failed for "
                    f"{desired.component_id!r}: {exc}"
                ),
            )

        # Install the wheel into the candidate.
        result = runtime.install_local_wheel(
            fresh_artifact.path,
            slot_id=txn.candidate_slot_id,
            component_definition=definition,
        )

        if result.outcome in (InstallOutcome.INSTALLED, InstallOutcome.ALREADY_INSTALLED):
            definitions.append(definition)
            continue

        # FAILED, VERSION_MISMATCH, CONTRACT_MISMATCH → stop.
        return DeploymentResult(
            success=False,
            reason=(
                f"install failed for {desired.component_id!r}: "
                f"{result.outcome.value} — {result.detail or 'no detail'}"
            ),
        )

    # ---- 6. Candidate validation (multi-component) --------------------------
    val_status = runtime.validate_candidate(
        txn,
        component_definitions=definitions,
    )

    if val_status.state != RuntimeState.READY:
        return DeploymentResult(
            success=False,
            reason=(
                f"candidate validation failed: "
                f"{val_status.reason or 'unknown'}"
            ),
        )

    # ---- 6b. Verify candidate versions match desired state (M0-8B.2) ----
    for desired in plan.desired_state.components:
        observed = txn.expected_versions.get(desired.component_id)
        if observed is None:
            return DeploymentResult(
                success=False,
                reason=(
                    f"candidate missing expected version for "
                    f"{desired.component_id!r}"
                ),
            )
        if observed != desired.version:
            return DeploymentResult(
                success=False,
                reason=(
                    f"candidate version mismatch for {desired.component_id!r}: "
                    f"expected {desired.version!r}, got {observed!r}"
                ),
            )

    # ---- 7. Activation (includes pre-activation TOCTOU revalidation) --------
    act_status = runtime.activate(txn)

    if act_status.state != RuntimeState.READY:
        return DeploymentResult(
            success=False,
            reason=(
                f"activation failed: "
                f"{act_status.reason or 'unknown'}"
            ),
        )

    # ---- Success ------------------------------------------------------------
    return DeploymentResult(
        success=True,
        active_slot_id=act_status.active_slot_id,
        previous_slot_id=act_status.previous_slot_id,
    )


def _revalidate_artifact(
    desired: DesiredComponent,
    registry: ComponentRegistry,
) -> VerifiedArtifact:
    """Revalidate a desired component's artifact before pip handoff.

    Uses the M0-8B.1 ``revalidate_verified_artifact`` primitive with
    the trusted registry.  Returns a fresh ``VerifiedArtifact`` whose
    path is guaranteed current.

    Raises :class:`ArtifactRejectionError` if any revalidation check
    fails.
    """
    return revalidate_verified_artifact(
        desired.artifact,
        registry=registry,
    )
