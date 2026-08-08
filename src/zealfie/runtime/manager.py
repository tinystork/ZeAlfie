"""Shared runtime manager — slot-based staging, activation, and rollback.

M0-6: runtimes live in immutable ``slots/<id>`` directories.
Activation swaps only the atomic ``state/active.json`` pointer.
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

from zealfie.building import inspect_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract

from .layout import RuntimeLayout, default_runtime_layout
from .model import (
    CandidateState,
    InstallOutcome,
    InstallResult,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from .probe import probe_runtime_distribution, probe_runtime_python_version
from .state import load_active_state
from .transaction import (
    RuntimeTransaction,
    generate_slot_id,
)

_INSTALL_TIMEOUT_SECONDS: float = 120


class SharedRuntimeError(Exception):
    """Raised when a runtime operation cannot proceed."""


class SharedRuntime:
    """Manage the persistent ZeAlfie shared runtime (slot architecture).

    Usage::

        rt = SharedRuntime()
        status = rt.status()
        if status.state == RuntimeState.ABSENT:
            rt.create()
        # Begin a transaction for a new candidate
        txn = rt.begin_transaction()
        rt.install_local_wheel("/path/to/w.whl", slot_id=txn.candidate_slot_id, ...)
        rt.validate_candidate(txn)
        rt.activate(txn)
        rt.rollback()
    """

    def __init__(self, layout: RuntimeLayout | None = None) -> None:
        self._layout = layout or default_runtime_layout()

    @property
    def layout(self) -> RuntimeLayout:
        return self._layout

    # -- status (global, pointer-based) ---------------------------------------

    def status(self) -> RuntimeStatus:
        """Inspect the global runtime state via the active pointer.

        Returns ``ABSENT`` when no pointer file exists, ``READY`` when the
        active slot is found and healthy, ``BROKEN`` otherwise.
        """
        st = load_active_state(
            self._layout.active_pointer, layout_root=self._layout.root
        )

        if st.state == RuntimeState.ABSENT:
            return st

        if st.state == RuntimeState.BROKEN:
            return st

        # Pointer is valid — validate the active slot.
        active_id = st.active_slot_id
        if active_id is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND,
                reason="no active slot in state file",
            )

        slot_path = self._layout.slot_path(active_id)
        if not slot_path.is_dir():
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                active_slot_id=active_id,
                reason_code=RuntimeReasonCode.ACTIVE_SLOT_NOT_FOUND,
                reason=f"active slot {active_id!r} not found at {slot_path}",
            )

        python = _runtime_python(slot_path)
        if not python or not python.is_file():
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                active_slot_id=active_id,
                active_path=slot_path,
                reason_code=RuntimeReasonCode.RUNTIME_PYTHON_NOT_FOUND,
                reason="active slot Python interpreter not found",
            )

        version = probe_runtime_python_version(python)
        if version is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                active_slot_id=active_id,
                active_path=slot_path,
                python_executable=python,
                reason_code=RuntimeReasonCode.RUNTIME_PYTHON_UNUSABLE,
                reason="active slot Python is not usable",
            )

        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=self._layout.root,
            active_slot_id=active_id,
            active_path=slot_path,
            previous_slot_id=st.previous_slot_id,
            python_executable=python,
            python_version=version,
            reason_code=RuntimeReasonCode.RUNTIME_READY,
        )

    # -- create initial runtime -----------------------------------------------

    def create(self) -> RuntimeStatus:
        """Create the very first runtime via a slot transaction.

        If a pointer already exists and is READY, nothing is done.
        """
        st = self.status()
        if st.state == RuntimeState.READY:
            return st
        if st.state == RuntimeState.BROKEN:
            raise SharedRuntimeError(
                f"shared runtime is BROKEN ({st.reason}). "
                f"It must be repaired or removed manually before re-creation."
            )

        txn = self.begin_transaction()
        slot_id = txn.candidate_slot_id

        # Create the slot venv.
        slot_path = self._layout.slot_path(slot_id)
        slot_path.parent.mkdir(parents=True, exist_ok=True)
        venv.create(slot_path, with_pip=True, clear=True)

        # Validate base runtime health (no component expected yet).
        st = self.validate_candidate(txn)
        if st.state != RuntimeState.READY:
            return st

        result = txn.activate()
        if result.state != RuntimeState.READY:
            return result
        return self.status()

    # -- transaction ----------------------------------------------------------

    def begin_transaction(self) -> RuntimeTransaction:
        """Start a new candidate transaction.

        Reads the current active pointer to record the base state for
        stale-transaction detection.
        """
        current = load_active_state(
            self._layout.active_pointer, layout_root=self._layout.root
        )
        slot_id = generate_slot_id()
        return RuntimeTransaction(
            self._layout,
            candidate_slot_id=slot_id,
            base_active_slot_id=current.active_slot_id,
            base_previous_slot_id=current.previous_slot_id,
        )

    # -- install into a specific slot -----------------------------------------

    def install_local_wheel(
        self,
        wheel_path: str | Path,
        *,
        slot_id: str | None = None,
        component_definition: ComponentDefinition | None = None,
    ) -> InstallResult:
        """Install a local wheel into a slot.

        If *slot_id* is ``None``, the active slot is targeted.
        Pre-install contract validation is performed when
        *component_definition* is supplied.
        """
        wp = Path(wheel_path)
        if not wp.is_file():
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="?",
                detail=f"wheel file not found: {wp}",
            )

        info = _inspect_or_fail(wp)
        if info is None:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="?",
                detail="wheel inspection failed",
            )

        dist_name, wheel_version, contract_error = _validate_wheel_contract(
            info, component_definition
        )
        if contract_error is not None:
            return contract_error

        # Resolve target slot.
        resolved_id = slot_id
        if resolved_id is None:
            st = self.status()
            if st.active_slot_id is None:
                return InstallResult(
                    outcome=InstallOutcome.FAILED,
                    distribution_name=dist_name,
                    detail="no active slot and no explicit slot_id provided",
                )
            resolved_id = st.active_slot_id

        slot_path = self._layout.slot_path(resolved_id)
        if not slot_path.is_dir():
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"slot {resolved_id!r} does not exist",
            )

        python = _runtime_python(slot_path)
        if python is None:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"no Python in slot {resolved_id!r}",
            )

        # Probe existing installation.
        try:
            probe = probe_runtime_distribution(python, dist_name)
        except Exception as exc:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"runtime probe failed: {exc}",
            )

        if probe.get("installed"):
            installed_version = probe.get("version")
            if installed_version != wheel_version:
                return InstallResult(
                    outcome=InstallOutcome.VERSION_MISMATCH,
                    distribution_name=dist_name,
                    version=installed_version,
                    detail=f"installed {installed_version}, requested wheel {wheel_version}",
                )
            # Same version — verify contract.
            if component_definition is not None:
                if not _check_contract_from_probe(probe, component_definition):
                    return InstallResult(
                        outcome=InstallOutcome.CONTRACT_MISMATCH,
                        distribution_name=dist_name,
                        version=installed_version,
                        detail="installed distribution does not satisfy contract",
                    )
            return InstallResult(
                outcome=InstallOutcome.ALREADY_INSTALLED,
                distribution_name=dist_name,
                version=installed_version,
            )

        # Install.
        result = subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wp)],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"pip install failed: {result.stderr.strip()}",
            )

        # Post-validation.
        try:
            probe = probe_runtime_distribution(python, dist_name)
        except Exception as exc:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"post-install probe failed: {exc}",
            )
        if not probe.get("installed"):
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail="distribution not found after install",
            )
        installed_version = probe.get("version")
        if installed_version != wheel_version:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"version mismatch after install: expected {wheel_version}, got {installed_version}",
            )

        if component_definition is not None:
            if not _check_contract_from_probe(probe, component_definition):
                return InstallResult(
                    outcome=InstallOutcome.CONTRACT_MISMATCH,
                    distribution_name=dist_name,
                    version=installed_version,
                    detail="post-install contract mismatch",
                )

        return InstallResult(
            outcome=InstallOutcome.INSTALLED,
            distribution_name=dist_name,
            version=installed_version,
        )

    # -- validate candidate ---------------------------------------------------

    def validate_candidate(
        self,
        txn: RuntimeTransaction,
        *,
        component_definition: ComponentDefinition | None = None,
    ) -> RuntimeStatus:
        """Validate a prepared candidate slot.

        Checks: slot exists, Python works, distribution/version/contract
        (if definition supplied) are correct.
        """
        slot_path = txn.candidate_path
        if not slot_path.is_dir():
            txn._mark_invalid()
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                reason=f"candidate slot {txn.candidate_slot_id!r} does not exist",
            )

        python = _runtime_python(slot_path)
        if python is None:
            txn._mark_invalid()
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                reason="candidate has no Python interpreter",
            )

        version = probe_runtime_python_version(python)
        if version is None:
            txn._mark_invalid()
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                reason="candidate Python is not usable",
            )

        if component_definition is not None:
            try:
                probe = probe_runtime_distribution(
                    python, component_definition.distribution_name
                )
            except Exception as exc:
                txn._mark_invalid()
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason=f"candidate probe failed: {exc}",
                )
            if not probe.get("installed"):
                txn._mark_invalid()
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason=f"expected distribution {component_definition.distribution_name!r} not found in candidate",
                )
            if not _check_contract_from_probe(probe, component_definition):
                txn._mark_invalid()
                return RuntimeStatus(
                    state=RuntimeState.BROKEN,
                    runtime_root=self._layout.root,
                    reason_code=RuntimeReasonCode.CANDIDATE_VALIDATION_FAILED,
                    reason="candidate does not satisfy the expected launch contract",
                )
            # Store expectations for TOCTOU revalidation at activation time.
            txn._component_definition = component_definition
            txn._expected_version = probe.get("version")

        txn._mark_valid()
        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=self._layout.root,
            active_slot_id=txn.candidate_slot_id,
            python_executable=python,
            python_version=version,
            reason_code=RuntimeReasonCode.RUNTIME_READY,
        )

    # -- activate, rollback, discard ------------------------------------------

    def activate(self, txn: RuntimeTransaction) -> RuntimeStatus:
        result = txn.activate()
        if result.state != RuntimeState.READY:
            return result
        # Re-probe to fill python details.
        return self.status()

    def rollback(self) -> RuntimeStatus:
        return RuntimeTransaction.rollback(self._layout)

    def discard_slot(self, slot_id: str) -> RuntimeStatus:
        return RuntimeTransaction.discard_slot(self._layout, slot_id)

    def python(self) -> Path | None:
        st = self.status()
        if st.active_path is None:
            return None
        return _runtime_python(st.active_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_distribution_name(name: str) -> str:
    from zealfie.common import normalise_distribution_name
    return normalise_distribution_name(name)


def _inspect_or_fail(wp: Path) -> Any | None:
    try:
        return inspect_wheel(wp)
    except Exception:
        return None


def _validate_wheel_contract(
    info: Any,
    component_definition: ComponentDefinition | None,
) -> tuple[str, str | None, InstallResult | None]:
    """Extract dist name and version from wheel info (from canonical METADATA)."""
    dist_name = info.distribution_name
    wheel_version = info.version

    # Pre-install contract validation.
    if component_definition is not None:
        expected = _normalise_distribution_name(component_definition.distribution_name)
        if dist_name != expected:
            return dist_name, wheel_version, InstallResult(
                outcome=InstallOutcome.CONTRACT_MISMATCH,
                distribution_name=dist_name,
                detail=f"wheel distribution {dist_name!r} != expected {expected!r}",
            )
        expected_contracts = set(component_definition.launch_entry_points)
        if expected_contracts:
            observed = {
                EntryPointContract(ep.group, ep.name)
                for ep in info.entry_points
            }
            if not (expected_contracts & observed):
                return dist_name, wheel_version, InstallResult(
                    outcome=InstallOutcome.CONTRACT_MISMATCH,
                    distribution_name=dist_name,
                    detail="wheel does not declare any expected launch contract",
                )

    return dist_name, wheel_version, None


def _check_contract_from_probe(
    probe: dict[str, Any], definition: ComponentDefinition
) -> bool:
    expected = set(definition.launch_entry_points)
    if not expected:
        return True
    observed_eps = probe.get("entry_points", [])
    if not isinstance(observed_eps, list):
        return False
    for ep in observed_eps:
        contract = EntryPointContract(
            group=str(ep.get("group", "")),
            name=str(ep.get("name", "")),
        )
        if contract in expected:
            return True
    return False


def _runtime_python(venv_dir: Path) -> Path | None:
    if sys.platform == "win32":
        candidate = venv_dir / "Scripts" / "python.exe"
    else:
        candidate = venv_dir / "bin" / "python"
    return candidate if candidate.is_file() else None
