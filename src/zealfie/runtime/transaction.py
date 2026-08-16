"""Staged runtime transaction — prepare, validate, activate, rollback."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .layout import RuntimeLayout, validate_slot_id
from .mutation_lock import (
    RuntimeMutationLease,
    RuntimeMutationLeaseRequired,
    RuntimeMutationLock,
)
from .model import (
    ActiveRuntimeState,
    CandidateState,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from .state import load_active_state, save_active_state

if TYPE_CHECKING:
    from zealfie.dependencies.models import RuntimeLock


def _require_runtime_lease(what: str, root: "pathlib.Path") -> RuntimeMutationLease:
    """Prove a mutation lease is held for *root* in the current context (D2).

    The low-level mutating primitives (:meth:`RuntimeTransaction.activate`,
    :meth:`RuntimeTransaction.rollback`,
    :meth:`RuntimeTransaction.discard_slot`) must fail closed — no write may
    happen before this proof.  Raises :class:`RuntimeMutationLeaseRequired`
    when no lease is held, or when the held lease covers a *different* runtime
    root (a writer holding a lease for root A must never mutate root B).
    """
    lease = RuntimeMutationLock.require_lease(what)
    resolved_root = root.resolve()
    if lease.runtime_root != resolved_root:
        raise RuntimeMutationLeaseRequired(
            f"{what} on runtime root {resolved_root} (the held lease is for "
            f"a different runtime root: {lease.runtime_root})"
        )
    return lease


def generate_slot_id() -> str:
    """Return a unique, safe slot identifier (``rt-<uuid>``)."""
    return "rt-" + uuid.uuid4().hex[:12]


class RuntimeTransaction:
    """A prepared candidate runtime that may be validated and activated.

    The active pointer is **never** modified until :meth:`activate` is
    called and validates successfully.

    *component_definition* and *expected_version* are stored so
    :meth:`activate` can revalidate a single component immediately
    before switching the pointer (TOCTOU protection).

    For multi-component deployments, :meth:`set_component_expectations`
    stores a map of ``component_id → version`` and the full list of
    definitions, which :meth:`activate` rechecks.

    M1-1D: :meth:`set_dependency_lock` stores the entire RuntimeLock
    so that :meth:`activate` also revalidates all non-component
    dependency distributions against the lock immediately before
    switching the active pointer.
    """

    def __init__(
        self,
        layout: RuntimeLayout,
        candidate_slot_id: str,
        *,
        base_active_slot_id: str | None,
        base_previous_slot_id: str | None = None,
        component_definition: "ComponentDefinition | None" = None,
        expected_version: str | None = None,
    ) -> None:
        validate_slot_id(candidate_slot_id)
        self._layout = layout
        self._candidate_id = candidate_slot_id
        self._base_active = base_active_slot_id
        self._base_previous = base_previous_slot_id
        self._candidate_state = CandidateState.PREPARED
        self._component_definition = component_definition
        self._expected_version = expected_version
        # Multi-component support.
        self._component_definitions: "tuple[ComponentDefinition, ...]" = ()
        self._expected_versions: dict[str, str] = {}
        # M1-1D: dependency lock for activation-time TOCTOU revalidation.
        self._dependency_lock: RuntimeLock | None = None

    @property
    def candidate_slot_id(self) -> str:
        return self._candidate_id

    @property
    def candidate_path(self) -> "pathlib.Path":
        return self._layout.slot_path(self._candidate_id)

    @property
    def base_active_slot_id(self) -> str | None:
        return self._base_active

    @property
    def state(self) -> CandidateState:
        return self._candidate_state

    @property
    def component_definitions(self) -> "tuple[ComponentDefinition, ...]":
        return self._component_definitions

    @property
    def expected_versions(self) -> dict[str, str]:
        return dict(self._expected_versions)

    def _mark_valid(self) -> None:
        self._candidate_state = CandidateState.VALID

    def _mark_invalid(self) -> None:
        self._candidate_state = CandidateState.INVALID

    def set_component_expectations(
        self,
        definitions: "tuple[ComponentDefinition, ...]",
        versions: dict[str, str],
    ) -> None:
        """Record expected components and versions for TOCTOU revalidation.

        Called by :meth:`SharedRuntime.validate_candidate` when multiple
        component definitions are supplied.  :meth:`activate` re-checks
        every entry before atomically switching the active pointer.
        """
        self._component_definitions = definitions
        self._expected_versions = dict(versions)

    def set_dependency_lock(
        self,
        dependency_lock: RuntimeLock | None,
    ) -> None:
        """Record the RuntimeLock for activation-time dependency TOCTOU
        revalidation (M1-1D hardening).

        Called by :func:`apply_deployment_plan` before candidate creation.
        :meth:`activate` probes every non-component dependency distribution
        from the lock immediately before switching the active pointer.
        """
        self._dependency_lock = dependency_lock

    def activate(self) -> RuntimeStatus:
        """Activate this candidate, making it the active slot.

        Before writing:

        1. The candidate must be ``VALID``.
        2. The current active state is reloaded from disk.
        3. If the active slot has changed since the transaction began,
           activation is refused (stale transaction).
        4. All component distributions are TOCTOU-reprobed for
           installed status, version, and launch contract.
        5. (M1-1D) All non-component dependency distributions from the
           RuntimeLock are TOCTOU-reprobed for installed status and
           exact version.

        On success the previous active slot becomes ``previous_slot`` and
        the candidate becomes ``active_slot``.
        """
        from pathlib import Path

        # D2 contract (ZA-M1-2L): prove a mutation lease is held for this
        # runtime root in the current context BEFORE any write.  Fail closed.
        _require_runtime_lease(
            "RuntimeTransaction.activate", self._layout.root
        )

        if self._candidate_state != CandidateState.VALID:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                reason="candidate has not been validated",
            )

        # Reload current state.
        current = load_active_state(
            self._layout.active_pointer, layout_root=self._layout.root
        )

        # Never activate when global state is BROKEN.
        # ABSENT/READY are both acceptable.
        if current.state == RuntimeState.BROKEN:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.ACTIVATION_FAILED,
                reason=(
                    f"cannot activate: current runtime state is "
                    f"{current.state.value}"
                ),
            )

        # Stale transaction protection.
        if current.active_slot_id != self._base_active:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                active_slot_id=current.active_slot_id,
                reason_code=RuntimeReasonCode.STALE_TRANSACTION,
                reason=(
                    f"active slot changed from {self._base_active!r} "
                    f"to {current.active_slot_id!r} since transaction began"
                ),
            )

        # Verify the candidate path exists and is a directory.
        candidate_path = self._layout.slot_path(self._candidate_id)
        if not candidate_path.is_dir():
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND,
                reason=f"candidate slot {self._candidate_id!r} no longer exists",
            )

        # -- TOCTOU revalidation: re-check candidate NOW --------------------
        from .probe import probe_runtime_distribution, probe_runtime_python_version
        python = _slot_python(candidate_path)
        if python is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                reason="candidate Python not found at activation time",
            )
        pv = probe_runtime_python_version(python)
        if pv is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                reason="candidate Python unusable at activation time",
            )

        # -- Multi-component TOCTOU revalidation ---------------------------
        if self._component_definitions:
            error = _revalidate_multi_component(
                python, self._component_definitions, self._expected_versions
            )
            if error is not None:
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason=error,
                )

        # -- Single-component TOCTOU revalidation (backward-compat) --------
        elif self._component_definition is not None:
            try:
                probe = probe_runtime_distribution(
                    python, self._component_definition.distribution_name
                )
            except Exception as exc:
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason=f"candidate probe failed at activation time: {exc}",
                )
            if not probe.get("installed"):
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason="expected distribution not found in candidate at activation time",
                )
            if self._expected_version is not None:
                if probe.get("version") != self._expected_version:
                    return RuntimeStatus(
                        state=RuntimeState.BROKEN,
                        runtime_root=self._layout.root,
                        reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                        reason=f"candidate version changed at activation time",
                    )
            # Check contract.
            from zealfie.components.model import EntryPointContract
            expected = set(self._component_definition.launch_entry_points)
            if expected:
                observed_eps = probe.get("entry_points", [])
                if not isinstance(observed_eps, list):
                    observed_eps = []
                matched = False
                for ep in observed_eps:
                    contract = EntryPointContract(
                        group=str(ep.get("group", "")),
                        name=str(ep.get("name", "")),
                    )
                    if contract in expected:
                        matched = True
                        break
                if not matched:
                    return RuntimeStatus(
                        state=RuntimeState.BROKEN,
                        runtime_root=self._layout.root,
                        reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                        reason="candidate no longer satisfies launch contract at activation time",
                    )

        # -- M1-1D: Dependency TOCTOU revalidation -------------------------
        if self._dependency_lock is not None:
            dep_error = _revalidate_dependency_distributions(
                python, self._dependency_lock
            )
            if dep_error is not None:
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason=dep_error,
                )

        # -- Atomically write the new pointer. ---------------------------------
        new_previous = self._base_active if self._base_active is not None else current.previous_slot_id
        try:
            save_active_state(
                self._layout.active_pointer,
                active_slot_id=self._candidate_id,
                previous_slot_id=new_previous,
            )
        except OSError as exc:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.ACTIVATION_FAILED,
                reason=f"could not write active pointer: {exc}",
            )

        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=self._layout.root,
            active_slot_id=self._candidate_id,
            previous_slot_id=new_previous,
            reason_code=RuntimeReasonCode.RUNTIME_READY,
        )

    @staticmethod
    def rollback(layout: RuntimeLayout) -> RuntimeStatus:
        """Swap the active and previous slots (reversible rollback).

        Requires a valid ``previous_slot`` in the active pointer.
        The previous slot is validated (path must exist) before switching.
        """
        # D2 contract (ZA-M1-2L): prove a mutation lease is held for this
        # runtime root in the current context BEFORE any write.  Fail closed.
        _require_runtime_lease("RuntimeTransaction.rollback", layout.root)

        current = load_active_state(
            layout.active_pointer, layout_root=layout.root
        )

        # ABSENT runtime has nothing to roll back — return ABSENT unchanged.
        if current.state == RuntimeState.ABSENT:
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=layout.root,
                reason_code=RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND,
                reason="shared runtime is absent — nothing to roll back",
            )

        # Never rollback when global state is untrustworthy.
        if current.state == RuntimeState.BROKEN:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                reason_code=current.reason_code or RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
                reason=f"cannot rollback: global state is BROKEN ({current.reason})",
            )

        if current.previous_slot_id is None:
            return RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id=current.active_slot_id,
                reason_code=RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND,
                reason="no previous slot to roll back to",
            )

        # Validate previous slot exists and is healthy.
        prev_path = layout.slot_path(current.previous_slot_id)
        if not prev_path.is_dir():
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                active_slot_id=current.active_slot_id,
                reason_code=RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND,
                reason=f"previous slot {current.previous_slot_id!r} not found",
            )

        # Validate runtime health of the rollback target.
        from .probe import probe_runtime_python_version
        python = _slot_python(prev_path)
        if python is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                active_slot_id=current.active_slot_id,
                reason_code=RuntimeReasonCode.RUNTIME_PYTHON_NOT_FOUND,
                reason=f"rollback target slot {current.previous_slot_id!r} has no Python",
            )
        version = probe_runtime_python_version(python)
        if version is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                active_slot_id=current.active_slot_id,
                reason_code=RuntimeReasonCode.RUNTIME_PYTHON_UNUSABLE,
                reason=f"rollback target slot {current.previous_slot_id!r} Python is unusable",
            )

        try:
            save_active_state(
                layout.active_pointer,
                active_slot_id=current.previous_slot_id,
                previous_slot_id=current.active_slot_id,
            )
        except OSError as exc:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                reason_code=RuntimeReasonCode.ACTIVATION_FAILED,
                reason=f"rollback write failed: {exc}",
            )

        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=layout.root,
            active_slot_id=current.previous_slot_id,
            previous_slot_id=current.active_slot_id,
            reason_code=RuntimeReasonCode.RUNTIME_READY,
        )

    @staticmethod
    def discard_slot(layout: RuntimeLayout, slot_id: str) -> RuntimeStatus:
        """Remove a slot directory, but only if it is neither active nor previous."""
        # D2 contract (ZA-M1-2L): prove a mutation lease is held for this
        # runtime root in the current context BEFORE any write.  Fail closed.
        _require_runtime_lease("RuntimeTransaction.discard_slot", layout.root)

        validate_slot_id(slot_id)

        current = load_active_state(
            layout.active_pointer, layout_root=layout.root
        )

        # Never allow discard when global state is untrustworthy.
        if current.state == RuntimeState.BROKEN:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                reason_code=RuntimeReasonCode.SLOT_DISCARD_REFUSED,
                reason="cannot discard slot: global runtime state is BROKEN",
            )

        if slot_id in (current.active_slot_id, current.previous_slot_id):
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                reason_code=RuntimeReasonCode.SLOT_DISCARD_REFUSED,
                reason=f"cannot discard active or previous slot: {slot_id!r}",
            )

        slot_path = layout.slot_path(slot_id)
        if not slot_path.is_dir():
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout.root,
                reason_code=RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND,
                reason=f"slot {slot_id!r} not found",
            )

        import shutil

        shutil.rmtree(slot_path, ignore_errors=True)
        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=layout.root,
            active_slot_id=current.active_slot_id,
            reason_code=RuntimeReasonCode.RUNTIME_READY,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot_python(slot_dir: "pathlib.Path") -> "pathlib.Path | None":
    """Return the Python interpreter inside a slot dir, or None."""
    import sys
    if sys.platform == "win32":
        candidate = slot_dir / "Scripts" / "python.exe"
    else:
        candidate = slot_dir / "bin" / "python"
    return candidate if candidate.is_file() else None


def _revalidate_multi_component(
    python: "pathlib.Path",
    definitions: "tuple[ComponentDefinition, ...]",
    expected_versions: dict[str, str],
) -> str | None:
    """Revalidate every expected component at activation time.

    Returns ``None`` on success, or an error string on the first failure.
    """
    from .probe import probe_runtime_distribution
    from zealfie.components.model import EntryPointContract

    for cd in definitions:
        # Check expected version is recorded.
        expected_ver = expected_versions.get(cd.component_id)
        if expected_ver is None:
            return (
                f"missing expected version for component "
                f"{cd.component_id!r} at activation time"
            )

        try:
            probe = probe_runtime_distribution(python, cd.distribution_name)
        except Exception as exc:
            return (
                f"activation probe failed for {cd.component_id!r}: {exc}"
            )

        if not probe.get("installed"):
            return (
                f"distribution {cd.distribution_name!r} not found in "
                f"candidate at activation time"
            )

        probe_ver = probe.get("version")
        if probe_ver != expected_ver:
            return (
                f"version changed for {cd.component_id!r} at activation "
                f"time: expected {expected_ver!r}, got {probe_ver!r}"
            )

        # Check contract.
        expected_contracts = set(cd.launch_entry_points)
        if expected_contracts:
            observed_eps = probe.get("entry_points", [])
            if not isinstance(observed_eps, list):
                observed_eps = []
            matched = False
            for ep in observed_eps:
                contract = EntryPointContract(
                    group=str(ep.get("group", "")),
                    name=str(ep.get("name", "")),
                )
                if contract in expected_contracts:
                    matched = True
                    break
            if not matched:
                return (
                    f"candidate no longer satisfies launch contract "
                    f"for {cd.component_id!r} at activation time"
                )

    return None


def _revalidate_dependency_distributions(
    python: "pathlib.Path",
    dependency_lock: RuntimeLock,
) -> str | None:
    """M1-1D: TOCTOU revalidate all non-component dependency distributions
    from the RuntimeLock immediately before activation.

    Every locked dependency that is NOT a primary must be installed in
    the candidate at the exact version recorded in the lock.

    Returns ``None`` on success, or an error string on the first failure.
    """
    from .probe import probe_runtime_distribution

    lock = dependency_lock
    for dep_name in lock.dependency_names:
        locked_dep = lock[dep_name]

        try:
            probe = probe_runtime_distribution(python, dep_name)
        except Exception as exc:
            return (
                f"dependency TOCTOU probe failed for {dep_name!r} "
                f"at activation time: {exc}"
            )

        if not probe.get("installed"):
            return (
                f"dependency {dep_name!r} not installed in candidate "
                f"at activation time"
            )

        installed_version = probe.get("version")
        if installed_version != locked_dep.version:
            return (
                f"dependency {dep_name!r} version changed at activation "
                f"time: expected {locked_dep.version!r}, "
                f"got {installed_version!r}"
            )

    return None
