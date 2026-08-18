"""Transactional offline deployment engine (M0-8B, M1-2I hooks).

Applies a validated :class:`DeploymentPlan` to the shared runtime:
preflight → dependency materialization → full-state component
materialization → candidate validation → pre-activation M0-6
revalidation → atomic activation.

All installation is offline (``--no-index --no-deps``) and every
artifact is TOCTOU-revalidated immediately before pip handoff.

M1-2I adds two backward-compatible optional keyword hooks so the
accelerated deployment engine can gate and cancel a base product
deployment before activation: ``cancel_check`` (cooperative
cancellation checkpoints) and ``pre_activate`` (a pre-activation gate
that can fail the deployment with an error string).  Both default to
``None`` and preserve the exact M0-8B behaviour when omitted.
"""

from __future__ import annotations

import hashlib
import logging
import sys as _sys
import venv
from pathlib import Path as _Path

from zealfie.common import normalise_distribution_name
from zealfie.components.model import ComponentDefinition
from zealfie.components.registry import ComponentRegistry
from zealfie.dependencies.models import LockedDependency, RuntimeLock
from zealfie.releases.model import VerifiedArtifact
from zealfie.releases.verifier import ArtifactRejectionError, revalidate_verified_artifact

from .manager import SharedRuntime
from .mutation_lock import (
    OPERATION_RUNTIME_APPLY,
    RuntimeMutationLock,
    RuntimeMutationLockError,
)
from .model import (
    DeploymentResult,
    InstallOutcome,
    RuntimeState,
)
from .planning import (
    ORIGIN_KEEP,
    ORIGIN_UPDATE,
    DeploymentPlan,
    DeploymentStep,
    DesiredComponent,
    PlanningError,
    check_desired_state_conflicts,
)
from .transaction import RuntimeTransaction


logger = logging.getLogger(__name__)


class DeploymentCancelledError(RuntimeError):
    """Signal raised by ``cancel_check`` to interrupt a deployment.

    Cancellation is an interruption, not a failure: no
    :class:`DeploymentResult` is produced, and the active pointer is
    never touched before activation.
    """


def _invoke_cancel_check(cancel_check) -> DeploymentResult | None:
    """Run ``cancel_check`` once (M1-2I).

    Returns ``None`` to continue, or a failure ``DeploymentResult`` when
    ``cancel_check`` raises anything other than
    :class:`DeploymentCancelledError`.  A
    :class:`DeploymentCancelledError` is re-raised — cancellation is an
    interruption, not a failure.
    """
    if cancel_check is None:
        return None
    try:
        cancel_check()
    except DeploymentCancelledError:
        raise
    except Exception as exc:
        return DeploymentResult(
            success=False,
            reason=f"cancel check failed: {exc}",
        )
    return None


def apply_deployment_plan(
    plan: DeploymentPlan,
    *,
    registry: ComponentRegistry,
    runtime: SharedRuntime | None = None,
    progress_callback=None,
    cancel_check=None,
    pre_activate=None,
) -> DeploymentResult:
    """Apply a complete deployment plan to the shared runtime.

    This is the single transactional entry point for offline deployment.
    It performs the full sequence:

    1. **Preflight** — validate the plan against the current runtime state
       and reject before any filesystem mutation.
    2. **Dependency lock coherence** — validate every DesiredComponent
       against the RuntimeLock (M1-1D: no component escapes coherence
       because of ``required_by`` edges).  No mutation.
    3. **Candidate creation** — begin an M0-6 transaction and create a
       fresh venv directly at the final slot path.
    4. **Dependency materialization** — install locked non-component
       dependencies from the RuntimeLock into the candidate.  This is the
       shared runtime foundation and MUST precede component installs.
    5. **Full-state component materialization** — install every desired
       component in deterministic component-id order, regardless of
       KEEP/INSTALL.  Each artifact is TOCTOU-revalidated immediately
       before pip handoff.
    6. **Candidate validation** — verify every component is installed
       correctly within the candidate.
    6b. **Version-match checks** — verify every candidate component
       version equals the desired state (M0-8B.2).
    6c. **Pre-activation gate** — optional ``pre_activate`` hook (M1-2I):
       runs after the version-match checks and before activation; a
       non-``None`` return value (an error string) fails the deployment
       with no activation.
    7. **Activation** — atomically switch the active pointer (M0-6),
       including M1-1D dependency distribution TOCTOU revalidation.

    The active pointer is never modified before step 7.  On failure at
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
    progress_callback:
        Optional ``Callable[[InstallProgress], None]`` observing the
        per-package install, validation, and activation boundaries.
        Observational only; it never affects behaviour, results, or error
        propagation.
    cancel_check:
        Optional ``Callable[[], None]`` cooperative cancellation hook
        (M1-2I).  Invoked before begin-transaction, before candidate
        venv creation, before each dependency install, before each
        component install, before validation, and before activation.
        Raising :class:`DeploymentCancelledError` interrupts the
        deployment (re-raised, no result object, active pointer
        untouched).  Any other exception becomes a
        ``DeploymentResult(success=False)`` failure.
    pre_activate:
        Optional ``Callable[[RuntimeTransaction], str | None]``
        pre-activation gate hook (M1-2I).  Runs after the version-match
        checks (step 6b) and before ``runtime.activate`` (step 7).
        A non-``None`` return value is an error string that fails the
        deployment with ``reason="pre-activation gate failed: <err>"``
        and no activation.  Any exception raised by the hook is caught
        and converted to the same failure result.

    Returns
    -------
    DeploymentResult
        *success=True* with the new active slot id when activation succeeds.
        *success=False* with a reason string on any failure.

    ZA-M1-2L (D1): the whole deployment window runs under the
    ``runtime-apply`` mutation lease, acquired at entry (early
    acquisition) and released on every exit path including exceptions and
    cancellation.  This is the outermost layer for the CLI ``runtime
    apply`` offline flow; when a service-layer lease (``product-install``
    / ``gpu-install``) is already held for the same runtime root in the
    same context, the nested acquisition reuses it (the outer operation
    name is preserved).

    A runtime that exposes no layout (``getattr(runtime, "layout",
    None)`` is ``None``) is rejected with
    :class:`RuntimeMutationLockError` before any mutation: there is no
    lease to scope the mutation to, and the engine refuses to mutate
    without one (fail closed).
    """
    if runtime is None:
        runtime = SharedRuntime()
    layout = getattr(runtime, "layout", None)
    if layout is None:
        raise RuntimeMutationLockError(
            "apply_deployment_plan requires a runtime exposing a layout "
            "for mutation-lease scoping; refusing to mutate without a lock"
        )
    with RuntimeMutationLock(layout.root).acquire(
        OPERATION_RUNTIME_APPLY
    ):
        return _apply_deployment_plan_locked(
            plan,
            registry=registry,
            runtime=runtime,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            pre_activate=pre_activate,
        )


def _apply_deployment_plan_locked(
    plan: DeploymentPlan,
    *,
    registry: ComponentRegistry,
    runtime: SharedRuntime | None = None,
    progress_callback=None,
    cancel_check=None,
    pre_activate=None,
) -> DeploymentResult:
    """Apply a complete deployment plan to the shared runtime.

    This is the single transactional entry point for offline deployment.
    It performs the full sequence:

    1. **Preflight** — validate the plan against the current runtime state
       and reject before any filesystem mutation.
    2. **Dependency lock coherence** — validate every DesiredComponent
       against the RuntimeLock (M1-1D: no component escapes coherence
       because of ``required_by`` edges).  No mutation.
    3. **Candidate creation** — begin an M0-6 transaction and create a
       fresh venv directly at the final slot path.
    4. **Dependency materialization** — install locked non-component
       dependencies from the RuntimeLock into the candidate.  This is the
       shared runtime foundation and MUST precede component installs.
    5. **Full-state component materialization** — install every desired
       component in deterministic component-id order, regardless of
       KEEP/INSTALL.  Each artifact is TOCTOU-revalidated immediately
       before pip handoff.
    6. **Candidate validation** — verify every component is installed
       correctly within the candidate.
    6b. **Version-match checks** — verify every candidate component
       version equals the desired state (M0-8B.2).
    6c. **Pre-activation gate** — optional ``pre_activate`` hook (M1-2I):
       runs after the version-match checks and before activation; a
       non-``None`` return value (an error string) fails the deployment
       with no activation.
    7. **Activation** — atomically switch the active pointer (M0-6),
       including M1-1D dependency distribution TOCTOU revalidation.

    The active pointer is never modified before step 7.  On failure at
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
    progress_callback:
        Optional ``Callable[[InstallProgress], None]`` observing the
        per-package install, validation, and activation boundaries.
        Observational only; it never affects behaviour, results, or error
        propagation.
    cancel_check:
        Optional ``Callable[[], None]`` cooperative cancellation hook
        (M1-2I).  Invoked before begin-transaction, before candidate
        venv creation, before each dependency install, before each
        component install, before validation, and before activation.
        Raising :class:`DeploymentCancelledError` interrupts the
        deployment (re-raised, no result object, active pointer
        untouched).  Any other exception becomes a
        ``DeploymentResult(success=False)`` failure.
    pre_activate:
        Optional ``Callable[[RuntimeTransaction], str | None]``
        pre-activation gate hook (M1-2I).  Runs after the version-match
        checks (step 6b) and before ``runtime.activate`` (step 7).
        A non-``None`` return value is an error string that fails the
        deployment with ``reason="pre-activation gate failed: <err>"``
        and no activation.  Any exception raised by the hook is caught
        and converted to the same failure result.

    Returns
    -------
    DeploymentResult
        *success=True* with the new active slot id when activation succeeds.
        *success=False* with a reason string on any failure.
    """
    if runtime is None:
        runtime = SharedRuntime()

    # ---- Progress observation (optional, Qt-free) ---------------------------
    # Lazy import keeps the runtime layer free of an app-layer top-level
    # dependency while still emitting the shared InstallProgress contract.
    from zealfie.app.progress import (
        PHASE_PERCENT,
        InstallPhase,
        InstallProgress,
        interpolate_percent,
    )

    def _emit(phase, percent, message):
        if progress_callback is not None:
            try:
                progress_callback(
                    InstallProgress(phase=phase, percent=percent, message=message)
                )
            except Exception:
                logger.debug(
                    "Progress callback raised during %s; ignoring (observational only)",
                    getattr(phase, "value", phase),
                    exc_info=True,
                )

    dep_names = (
        [d.name for d in _iter_locked_dependencies(plan.dependency_lock)]
        if plan.dependency_lock is not None
        else []
    )
    install_names = dep_names + [dc.component_id for dc in plan.desired_state.components]
    install_counter = {"i": 0}

    def _emit_install_message(message: str) -> None:
        total = len(install_names)
        if total > 0:
            pct = interpolate_percent(install_counter["i"], total)
        else:
            pct = PHASE_PERCENT[InstallPhase.INSTALLING_RUNTIME]
        install_counter["i"] += 1
        _emit(InstallPhase.INSTALLING_RUNTIME, pct, message)

    def _emit_install(name):
        _emit_install_message(f"Installing {name}\u2026")

    # ---- 1. Preflight: blocked plan ----------------------------------------
    if plan.blocked:
        return DeploymentResult(
            success=False,
            reason=f"deployment plan is blocked: {plan.blocked_reason or 'unknown'}",
        )

    # ---- 2. Preflight: stale plan detection --------------------------------
    status = runtime.status()

    if status.state == RuntimeState.BROKEN:
        return DeploymentResult(
            success=False,
            reason=f"shared runtime is BROKEN: {status.reason or 'unknown'}",
        )

    if plan.source_active_slot_id != status.active_slot_id:
        return DeploymentResult(
            success=False,
            reason=(
                f"stale deployment plan: plan built from active slot "
                f"{plan.source_active_slot_id!r}, but current runtime "
                f"active slot is {status.active_slot_id!r}"
            ),
        )

    # ---- 3. Preflight: validate desired state against registry --------------
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

    # ---- 4. Preflight: shared-runtime conflict check -----------------------
    try:
        check_desired_state_conflicts(plan.desired_state, registry)
    except PlanningError as exc:
        return DeploymentResult(
            success=False,
            reason=f"shared-runtime conflict detected at apply time: {exc}",
        )

    # ---- 5. Preflight: dependency lock coherence (M1-1D: all components) ---
    if plan.dependency_lock is not None:
        coh_err = _validate_dependency_lock_coherence(plan)
        if coh_err is not None:
            return DeploymentResult(success=False, reason=coh_err)

    # ---- 6. Candidate creation ----------------------------------------------
    fail = _invoke_cancel_check(cancel_check)
    if fail is not None:
        return fail

    try:
        txn = runtime.begin_transaction()
    except Exception as exc:
        return DeploymentResult(
            success=False,
            reason=f"failed to begin transaction: {exc}",
        )

    # Attach the dependency lock to the transaction for activation-time
    # TOCTOU revalidation (M1-1D hardening).
    txn.set_dependency_lock(plan.dependency_lock)

    # Create the candidate venv directly at the final slot path.
    candidate_path = txn.candidate_path
    if candidate_path.exists():
        return DeploymentResult(
            success=False,
            reason=f"candidate slot path already exists: {candidate_path}",
        )

    fail = _invoke_cancel_check(cancel_check)
    if fail is not None:
        return fail

    try:
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        venv.create(candidate_path, with_pip=True, clear=False)
    except Exception as exc:
        return DeploymentResult(
            success=False,
            reason=f"failed to create candidate venv: {exc}",
        )

    # ---- 7. Dependency materialization (from RuntimeLock) -------------------
    # Dependencies are the shared runtime foundation and MUST be installed
    # before any component — the milestone boundary guarantees component
    # installs operate on top of a fully-resolved dependency environment.
    if plan.dependency_lock is not None:
        dep_result = _install_locked_dependencies(
            plan, runtime, txn, emit_install=_emit_install,
            cancel_check=cancel_check,
        )
        if dep_result is not None:
            return dep_result

    # ---- 8. Full-state component materialization ---------------------------
    # Install every desired component in deterministic component_id order,
    # regardless of KEEP/INSTALL action.  Each artifact is TOCTOU-revalidated
    # immediately before pip handoff.
    definitions: list[ComponentDefinition] = []
    steps_by_id = {s.component_id: s for s in plan.steps}

    for desired in plan.desired_state.components:
        # Resolve definition from registry.
        try:
            definition = registry.get(desired.component_id)
        except KeyError:
            return DeploymentResult(
                success=False,
                reason=f"component {desired.component_id!r} not found in registry",
            )

        # TOCTOU revalidation of the artifact before pip handoff.
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

        fail = _invoke_cancel_check(cancel_check)
        if fail is not None:
            return fail

        # Install the component wheel into the candidate.
        _emit_install_message(
            _component_install_message(
                desired,
                definition,
                steps_by_id.get(desired.component_id),
            )
        )
        result = runtime.install_local_wheel(
            fresh_artifact.path,
            slot_id=txn.candidate_slot_id,
            component_definition=definition,
        )

        if result.outcome in (InstallOutcome.INSTALLED, InstallOutcome.ALREADY_INSTALLED):
            definitions.append(definition)
            continue

        return DeploymentResult(
            success=False,
            reason=(
                f"install failed for {desired.component_id!r}: "
                f"{result.outcome.value} — {result.detail or 'no detail'}"
            ),
        )

    # ---- 9. Candidate validation (multi-component) --------------------------
    fail = _invoke_cancel_check(cancel_check)
    if fail is not None:
        return fail

    _emit(
        InstallPhase.VALIDATING,
        PHASE_PERCENT[InstallPhase.VALIDATING],
        "Validating\u2026",
    )
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

    # ---- 9b. Verify candidate versions match desired state (M0-8B.2) --------
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

    # ---- 9c. Optional pre-activation gate hook (M1-2I) ----------------------
    # Runs after the version-match checks and before activation.  A non-None
    # error string fails the deployment with no activation; exceptions are
    # caught to preserve the no-throw contract.
    if pre_activate is not None:
        try:
            gate_error = pre_activate(txn)
        except Exception as exc:
            gate_error = f"{type(exc).__name__}: {exc}"
        if gate_error is not None:
            return DeploymentResult(
                success=False,
                reason=f"pre-activation gate failed: {gate_error}",
            )

    # ---- 10. Activation (includes pre-activation TOCTOU revalidation) -------
    fail = _invoke_cancel_check(cancel_check)
    if fail is not None:
        return fail

    _emit(
        InstallPhase.ACTIVATING,
        PHASE_PERCENT[InstallPhase.ACTIVATING],
        "Activating\u2026",
    )
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


def _component_install_message(
    desired: DesiredComponent,
    definition: ComponentDefinition | None,
    step: DeploymentStep | None,
) -> str:
    """Return the honest per-component install-loop message.

    The wording is driven by the component's service-level *origin*
    (ZA-M1-3A.3 LOT E): a preserved product is never labelled
    "Installing" or "Updating".

    * ``keep``    -> ``Preserving <display> <version>``
    * ``update``  -> ``Updating <display> <old> -> <new>`` when the
      planned step observed the previously installed version, otherwise
      ``Updating <display> <version>``
    * ``install`` -> ``Installing <display> <version>``

    ``display`` is the catalog display name when available, falling back
    to the component id.  The message never affects behaviour - it is
    progress observation only.
    """
    display = (
        definition.display_name
        if definition is not None and definition.display_name
        else desired.component_id
    )
    version = desired.version
    if desired.origin == ORIGIN_KEEP:
        return f"Preserving {display} {version}"
    if desired.origin == ORIGIN_UPDATE:
        current = step.current_version if step is not None else None
        if current and current != version:
            return f"Updating {display} {current} -> {version}"
        return f"Updating {display} {version}"
    return f"Installing {display} {version}"


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


# ---------------------------------------------------------------------------
# M1-1B / M1-1D dependency materialization helpers
# ---------------------------------------------------------------------------


def _validate_dependency_lock_coherence(plan: DeploymentPlan) -> str | None:
    """Validate that EVERY DesiredComponent has a corresponding
    RuntimeLock entry agreeing on all stable identity and artifact
    fields (M1-1D hardened — no component escapes coherence).

    Fields checked per component (fail-closed — any mismatch blocks
    deployment):

    * Normalised distribution name (component → lock entry lookup)
    * Version (wheel_version)
    * Wheel path (resolved)
    * Size
    * SHA256

    Every DesiredComponent MUST have an exact matching entry in the
    RuntimeLock.  The reverse direction (lock entries that are NOT
    desired components) is expected — those are non-component
    dependencies and no coherence check is needed for them.

    This is a pure plan-inspection check that runs before any filesystem
    mutation (before ``begin_transaction``).
    """
    lock = plan.dependency_lock
    if lock is None:
        return None

    # Build normalised component distribution name -> DesiredComponent map.
    component_dists: dict[str, DesiredComponent] = {}
    for dc in plan.desired_state.components:
        norm = normalise_distribution_name(dc.artifact.distribution_name)
        if norm in component_dists:
            return (
                f"duplicate normalised distribution name {norm!r} "
                f"in desired components"
            )
        component_dists[norm] = dc

    # M1-1D: Validate EVERY DesiredComponent — not just RuntimeLock
    # primary_names.  A component that also happens to be a dependency
    # of another component must still pass coherence.
    for dc in plan.desired_state.components:
        norm = normalise_distribution_name(dc.artifact.distribution_name)

        if norm not in lock.locked:
            return (
                f"DesiredComponent {dc.component_id!r} "
                f"(distribution {norm!r}) does not have an entry in "
                f"the RuntimeLock"
            )

        locked_dep = lock[norm]

        if locked_dep.name != norm:
            return (
                f"RuntimeLock entry key {norm!r} does not match "
                f"locked dependency name {locked_dep.name!r}"
            )

        # --- Version (wheel_version) ---
        if locked_dep.version != dc.artifact.wheel_version:
            return (
                f"DesiredComponent {dc.component_id!r} "
                f"(distribution {norm!r}) version mismatch: "
                f"lock has {locked_dep.version!r}, "
                f"desired component has {dc.artifact.wheel_version!r}"
            )

        # --- Wheel path (resolve for equivalent-path detection) ---
        if locked_dep.wheel_path.resolve() != dc.artifact.path.resolve():
            return (
                f"DesiredComponent {dc.component_id!r} "
                f"(distribution {norm!r}) wheel_path mismatch: "
                f"lock has {locked_dep.wheel_path}, "
                f"desired component has {dc.artifact.path}"
            )

        # --- Size ---
        if locked_dep.size != dc.artifact.size:
            return (
                f"DesiredComponent {dc.component_id!r} "
                f"(distribution {norm!r}) size mismatch: "
                f"lock has {locked_dep.size}, "
                f"desired component has {dc.artifact.size}"
            )

        # --- SHA256 ---
        if locked_dep.sha256 != dc.artifact.sha256:
            return (
                f"DesiredComponent {dc.component_id!r} "
                f"(distribution {norm!r}) sha256 mismatch: "
                f"lock has {locked_dep.sha256[:16]}..., "
                f"desired component has {dc.artifact.sha256[:16]}..."
            )

    return None


def _install_locked_dependencies(
    plan: DeploymentPlan,
    runtime: SharedRuntime,
    txn: "RuntimeTransaction",
    emit_install=None,
    cancel_check=None,
) -> DeploymentResult | None:
    """Install locked non-component dependencies into the candidate slot.

    Steps per dependency:
    1. TOCTOU revalidation (size + sha256 against LockedDependency).
    2. ``pip install --no-index --no-deps`` into the candidate.
    3. Exact version validation in the candidate.

    Returns ``None`` on success, or a ``DeploymentResult`` on failure.
    The active pointer is never modified here.
    """
    lock = plan.dependency_lock
    if lock is None:
        return None

    # ---- Phase A: TOCTOU + install each non-component dependency ------------
    for dep in _iter_locked_dependencies(lock):
        dep_name = dep.name

        fail = _invoke_cancel_check(cancel_check)
        if fail is not None:
            return fail

        if emit_install is not None:
            emit_install(dep_name)

        # TOCTOU revalidation before pip.
        toctou_err = _revalidate_dependency_wheel(dep)
        if toctou_err is not None:
            return DeploymentResult(success=False, reason=toctou_err)

        # Install via the existing offline install path.
        result = runtime.install_local_wheel(
            dep.wheel_path,
            slot_id=txn.candidate_slot_id,
            component_definition=None,  # No component contract for deps
        )

        if result.outcome not in (
            InstallOutcome.INSTALLED,
            InstallOutcome.ALREADY_INSTALLED,
        ):
            return DeploymentResult(
                success=False,
                reason=(
                    f"dependency install failed for {dep_name!r}: "
                    f"{result.outcome.value} -- {result.detail or 'no detail'}"
                ),
            )

    # ---- Phase B: exact dependency version validation -----------------------
    validation_err = _validate_exact_dependency_versions(txn, lock)
    if validation_err is not None:
        return DeploymentResult(success=False, reason=validation_err)

    return None


def _revalidate_dependency_wheel(dep: LockedDependency) -> str | None:
    """TOCTOU revalidate a single dependency wheel before pip handoff.

    Returns an error string on mismatch, or None if the wheel is intact.
    """
    wheel_path = dep.wheel_path

    if not wheel_path.is_file():
        return (
            f"TOCTOU: dependency wheel not found for {dep.name!r}: "
            f"{wheel_path}"
        )

    actual_size = wheel_path.stat().st_size
    if actual_size != dep.size:
        return (
            f"TOCTOU size mismatch for dependency {dep.name!r}: "
            f"expected {dep.size}, got {actual_size}"
        )

    actual_sha256 = _sha256_of_path(wheel_path)
    if actual_sha256 != dep.sha256:
        return (
            f"TOCTOU sha256 mismatch for dependency {dep.name!r}: "
            f"expected {dep.sha256[:16]}..., got {actual_sha256[:16]}..."
        )

    return None


def _validate_exact_dependency_versions(
    txn: "RuntimeTransaction",
    lock: RuntimeLock,
) -> str | None:
    """Probe the candidate for every locked non-component dependency.

    Every locked dependency distribution must be installed in the
    candidate at the exact version recorded in the lock.

    Returns an error string on the first mismatch, or None if all
    dependencies satisfy the lock.
    """
    from .probe import probe_runtime_distribution

    candidate_python = _candidate_python(txn.candidate_path)
    if candidate_python is None:
        return "candidate Python not found during dependency validation"

    for dep in _iter_locked_dependencies(lock):
        dep_name = dep.name

        try:
            probe = probe_runtime_distribution(candidate_python, dep_name)
        except Exception as exc:
            return (
                f"dependency probe failed for {dep_name!r} "
                f"during exact version validation: {exc}"
            )

        if not probe.get("installed"):
            return (
                f"dependency {dep_name!r} not installed in candidate "
                f"after dependency materialization"
            )

        installed_version = probe.get("version")
        if installed_version != dep.version:
            return (
                f"dependency version mismatch for {dep_name!r}: "
                f"expected {dep.version!r}, got {installed_version!r}"
            )

    return None


def _iter_locked_dependencies(lock: RuntimeLock) -> list[LockedDependency]:
    """Yield non-primary dependencies in RuntimeLock insertion order.

    M1-1D: uses explicit ``lock.dependency_names`` (NOT ``required_by``)
    to decide which entries are non-component dependencies.
    """
    return [dep for name, dep in lock.locked.items()
            if name in lock.dependency_names]


def _sha256_of_path(path: _Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def _candidate_python(candidate_path: _Path) -> _Path | None:
    """Return the Python interpreter inside a candidate slot directory."""
    if _sys.platform == "win32":
        py = candidate_path / "Scripts" / "python.exe"
    else:
        py = candidate_path / "bin" / "python"
    return py if py.is_file() else None
