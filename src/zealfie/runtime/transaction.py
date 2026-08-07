"""Staged runtime transaction — prepare, validate, activate, rollback."""

from __future__ import annotations

import uuid

from .layout import RuntimeLayout, validate_slot_id
from .model import (
    ActiveRuntimeState,
    CandidateState,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from .state import load_active_state, save_active_state


def generate_slot_id() -> str:
    """Return a unique, safe slot identifier (``rt-<uuid>``)."""
    return "rt-" + uuid.uuid4().hex[:12]


class RuntimeTransaction:
    """A prepared candidate runtime that may be validated and activated.

    The active pointer is **never** modified until :meth:`activate` is
    called and validates successfully.

    *component_definition* and *expected_version* are stored so
    :meth:`activate` can revalidate the candidate immediately before
    switching the pointer (TOCTOU protection).
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

    def _mark_valid(self) -> None:
        self._candidate_state = CandidateState.VALID

    def _mark_invalid(self) -> None:
        self._candidate_state = CandidateState.INVALID

    def activate(self) -> RuntimeStatus:
        """Activate this candidate, making it the active slot.

        Before writing:

        1. The candidate must be ``VALID``.
        2. The current active state is reloaded from disk.
        3. If the active slot has changed since the transaction began,
           activation is refused (stale transaction).

        On success the previous active slot becomes ``previous_slot`` and
        the candidate becomes ``active_slot``.
        """
        from pathlib import Path

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
        if self._component_definition is not None:
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
        current = load_active_state(
            layout.active_pointer, layout_root=layout.root
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
