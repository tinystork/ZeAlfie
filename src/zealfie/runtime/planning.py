"""Deployment planning layer (M0-8A) — pure, read-only.

Accepts a complete desired runtime state and current runtime observation,
then returns a structured :class:`DeploymentPlan`.  No installation, no
slot creation, no activation, no filesystem mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Sequence

from zealfie.common import normalise_distribution_name
from zealfie.components.model import ComponentDefinition
from zealfie.components.registry import ComponentRegistry
from zealfie.releases.model import VerifiedArtifact

from .probe import probe_runtime_distribution
from .model import RuntimeState, RuntimeStatus

# ---------------------------------------------------------------------------
# Public enums
# ---------------------------------------------------------------------------


class DeploymentAction(StrEnum):
    """A single planned action for one component."""

    KEEP = "KEEP"
    """The installed distribution already satisfies the desired contract."""

    INSTALL = "INSTALL"
    """The component must be installed (absent, version mismatch, or contract repair)."""

    BLOCKED = "BLOCKED"
    """The plan cannot proceed for this component (broken runtime, probe failure)."""


class DeploymentReasonCode(StrEnum):
    """Stable reason codes carried by each :class:`DeploymentStep`."""

    # -- ABSENT runtime -------------------------------------------------------
    RUNTIME_ABSENT = "RUNTIME_ABSENT"
    """The shared runtime does not exist yet."""

    # -- BROKEN runtime -------------------------------------------------------
    RUNTIME_BROKEN = "RUNTIME_BROKEN"
    """The shared runtime is in a broken state."""

    # -- READY runtime outcomes -----------------------------------------------
    DISTRIBUTION_MISSING = "DISTRIBUTION_MISSING"
    """The desired distribution is not installed in the active runtime."""

    VERSION_MISMATCH = "VERSION_MISMATCH"
    """Installed version differs from the desired version."""

    LAUNCH_CONTRACT_MISMATCH = "LAUNCH_CONTRACT_MISMATCH"
    """Installed distribution does not declare the expected launch entry point(s)."""

    PROBE_FAILED = "PROBE_FAILED"
    """The runtime probe raised an exception or returned malformed data."""

    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    """Installed version and launch contract match the desired state."""

    # -- Validation failures --------------------------------------------------
    DESIRED_STATE_INVALID = "DESIRED_STATE_INVALID"
    """The desired runtime state failed structural or registry validation."""


# ---------------------------------------------------------------------------
# Desired runtime state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DesiredComponent:
    """A single component that must be present and correct in the runtime.

    The *artifact* field carries a :class:`VerifiedArtifact` as a
    point-in-time proof; a future application step must revalidate it.
    """

    component_id: str
    version: str
    artifact: VerifiedArtifact

    def __post_init__(self) -> None:
        if not self.component_id or not self.component_id.strip():
            raise ValueError("component_id must be non-empty")
        if not self.version or not self.version.strip():
            raise ValueError("version must be non-empty")
        if self.component_id != self.artifact.component_id:
            raise ValueError(
                f"component_id {self.component_id!r} does not match "
                f"artifact.component_id {self.artifact.component_id!r}"
            )
        if self.version != self.artifact.version:
            raise ValueError(
                f"version {self.version!r} does not match "
                f"artifact.version {self.artifact.version!r}"
            )
        if self.version != self.artifact.wheel_version:
            raise ValueError(
                f"version {self.version!r} does not match "
                f"artifact.wheel_version {self.artifact.wheel_version!r}"
            )


@dataclass(frozen=True)
class DesiredRuntimeState:
    """The complete, authoritative desired state for the trusted component registry.

    Immutable, deterministic (sorted by *component_id*), and validated:

    * Non-empty.
    * No duplicate *component_id* values.
    * Every :class:`DesiredComponent` self-validates on construction.

    The completeness guard (exact match with ``registry.available_ids()``)
    is enforced at plan-build time, not at construction time, so that a
    :class:`DesiredRuntimeState` can be built independently of a registry
    for testing purposes.
    """

    components: tuple[DesiredComponent, ...]

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("DesiredRuntimeState must contain at least one component")
        ids = [dc.component_id for dc in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("DesiredRuntimeState must not contain duplicate component ids")
        # Deterministic ordering.
        object.__setattr__(self, "components", tuple(sorted(self.components, key=lambda dc: dc.component_id)))

    def __iter__(self):
        return iter(self.components)

    def __len__(self) -> int:
        return len(self.components)

    def __contains__(self, component_id: str) -> bool:
        return any(dc.component_id == component_id for dc in self.components)


# ---------------------------------------------------------------------------
# Deployment step
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeploymentStep:
    """One atomic step in the deployment plan.

    Every step carries the :class:`VerifiedArtifact` so a future
    full-state application can materialize the entire desired runtime
    from the plan alone, not only from deltas.
    """

    component_id: str
    desired_version: str
    artifact: VerifiedArtifact
    action: DeploymentAction
    current_version: str | None = None
    reason_code: DeploymentReasonCode | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Deployment plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentPlan:
    """The complete result of plan-building.

    Immutable.  Steps are in deterministic order by *component_id*.
    """

    desired_state: DesiredRuntimeState
    runtime_state: RuntimeState
    steps: tuple[DeploymentStep, ...]
    blocked: bool = False
    blocked_reason: str | None = None
    # M0-8B foundation: bind to source runtime identity.
    source_active_slot_id: str | None = None
    source_previous_slot_id: str | None = None


# ---------------------------------------------------------------------------
# Planning error
# ---------------------------------------------------------------------------


class PlanningError(ValueError):
    """Raised when the desired state fails validation at plan-build time."""


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

# Default probe callable type.
_ProbeFn = Callable[[str, str], dict[str, Any]]


def build_deployment_plan(
    desired_state: DesiredRuntimeState,
    registry: ComponentRegistry,
    runtime_status: RuntimeStatus,
    *,
    probe_distribution: _ProbeFn | None = None,
) -> DeploymentPlan:
    """Build a pure, read-only deployment plan.

    Parameters
    ----------
    desired_state:
        Complete desired runtime state.  Every component id must match
        exactly ``registry.available_ids()`` — no missing or extra ids.
    registry:
        The trusted local component registry.
    runtime_status:
        Current global runtime observation from :meth:`SharedRuntime.status`.
    probe_distribution:
        Callable with the same signature and semantics as
        :func:`probe_runtime_distribution`.  Passed as a dependency so
        tests can supply synthetic probes.  Defaults to the real
        :func:`probe_runtime_distribution` for ``READY`` runtimes;
        unused for ``ABSENT`` / ``BROKEN``.

    Returns
    -------
    DeploymentPlan
        A complete, deterministic plan that describes every component.

    Raises
    ------
    PlanningError
        If the desired state is structurally invalid or does not exactly
        match the trusted registry.
    """
    # ------------------------------------------------------------------
    # 1) Complete desired state guard.
    # ------------------------------------------------------------------
    desired_ids = frozenset(dc.component_id for dc in desired_state.components)
    registry_ids = frozenset(registry.available_ids())

    if desired_ids != registry_ids:
        only_desired = desired_ids - registry_ids
        only_registry = registry_ids - desired_ids
        parts: list[str] = []
        if only_desired:
            parts.append(f"unknown component ids: {sorted(only_desired)}")
        if only_registry:
            parts.append(f"missing component ids: {sorted(only_registry)}")
        raise PlanningError("; ".join(parts))

    # ------------------------------------------------------------------
    # 2) Validate desired components against registry definitions.
    # ------------------------------------------------------------------
    for dc in desired_state.components:
        try:
            definition = registry.get(dc.component_id)
        except KeyError as exc:
            # Should not happen given the set-equality check above,
            # but guard nonetheless.
            raise PlanningError(
                f"component {dc.component_id!r} not found in registry"
            ) from exc

        _validate_component_definition_match(dc, definition)

    # ------------------------------------------------------------------
    # 2b) Conflict hardening — refuse incoherent shared-runtime desires.
    # ------------------------------------------------------------------
    check_desired_state_conflicts(desired_state, registry)

    # ------------------------------------------------------------------
    # 3) Route by runtime state.
    # ------------------------------------------------------------------
    if runtime_status.state == RuntimeState.BROKEN:
        return _build_blocked_plan(
            desired_state,
            runtime_status,
            reason_code=DeploymentReasonCode.RUNTIME_BROKEN,
            reason=runtime_status.reason or "shared runtime is BROKEN",
        )

    if runtime_status.state == RuntimeState.ABSENT:
        return _build_absent_plan(desired_state, runtime_status)

    # ------------------------------------------------------------------
    # 4) READY runtime — probe each desired component.
    # ------------------------------------------------------------------
    if runtime_status.python_executable is None:
        raise PlanningError(
            "RuntimeStatus.state is READY but python_executable is None; "
            "cannot probe the runtime"
        )

    if probe_distribution is None:
        probe_distribution = probe_runtime_distribution

    runtime_python = str(runtime_status.python_executable)
    steps: list[DeploymentStep] = []

    for dc in desired_state.components:
        definition = registry.get(dc.component_id)
        try:
            probe = probe_distribution(runtime_python, definition.distribution_name)
        except Exception as exc:
            return _build_blocked_plan(
                desired_state,
                runtime_status,
                reason_code=DeploymentReasonCode.PROBE_FAILED,
                reason=f"probe failed for {dc.component_id!r}: {exc}",
            )

        # Validate probe payload structure.
        if not isinstance(probe, dict):
            return _build_blocked_plan(
                desired_state,
                runtime_status,
                reason_code=DeploymentReasonCode.PROBE_FAILED,
                reason=f"probe returned non-dict payload for {dc.component_id!r}",
            )

        # Strict payload structure validation.
        validation_error = _validate_probe_payload(probe, dc.component_id)
        if validation_error is not None:
            return _build_blocked_plan(
                desired_state,
                runtime_status,
                reason_code=DeploymentReasonCode.PROBE_FAILED,
                reason=validation_error,
            )

        step = _plan_step_for_component(dc, definition, probe)
        steps.append(step)

    blocked = any(s.action == DeploymentAction.BLOCKED for s in steps)

    return DeploymentPlan(
        desired_state=desired_state,
        runtime_state=runtime_status.state,
        # M0-8B foundation: populate source slot identity.
        source_active_slot_id=runtime_status.active_slot_id,
        source_previous_slot_id=runtime_status.previous_slot_id,
        steps=tuple(steps),
        blocked=blocked,
        blocked_reason="one or more components blocked" if blocked else None,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_component_definition_match(
    dc: DesiredComponent,
    definition: ComponentDefinition,
) -> None:
    """Validate that *dc* and *definition* agree on distribution identity."""
    normalised_artifact = normalise_distribution_name(dc.artifact.distribution_name)
    normalised_definition = normalise_distribution_name(definition.distribution_name)
    if normalised_artifact != normalised_definition:
        raise PlanningError(
            f"distribution_name mismatch for {dc.component_id!r}: "
            f"artifact {dc.artifact.distribution_name!r} "
            f"(normalised: {normalised_artifact!r}) != "
            f"definition {definition.distribution_name!r} "
            f"(normalised: {normalised_definition!r})"
        )


def _build_blocked_plan(
    desired_state: DesiredRuntimeState,
    runtime_status: RuntimeStatus,
    *,
    reason_code: DeploymentReasonCode,
    reason: str,
) -> DeploymentPlan:
    steps = tuple(
        DeploymentStep(
            component_id=dc.component_id,
            desired_version=dc.version,
            artifact=dc.artifact,
            action=DeploymentAction.BLOCKED,
            reason_code=reason_code,
            reason=reason,
        )
        for dc in desired_state.components
    )
    return DeploymentPlan(
        desired_state=desired_state,
        runtime_state=runtime_status.state,
        # M0-8B foundation: populate source slot identity.
        source_active_slot_id=runtime_status.active_slot_id,
        source_previous_slot_id=runtime_status.previous_slot_id,
        steps=steps,
        blocked=True,
        blocked_reason=reason,
    )


def _build_absent_plan(
    desired_state: DesiredRuntimeState,
    runtime_status: RuntimeStatus,
) -> DeploymentPlan:
    """Plan INSTALL for every component when the runtime is ABSENT."""
    steps = tuple(
        DeploymentStep(
            component_id=dc.component_id,
            desired_version=dc.version,
            artifact=dc.artifact,
            action=DeploymentAction.INSTALL,
            reason_code=DeploymentReasonCode.RUNTIME_ABSENT,
            reason="shared runtime is absent — install planned",
        )
        for dc in desired_state.components
    )
    return DeploymentPlan(
        desired_state=desired_state,
        runtime_state=runtime_status.state,
        # M0-8B foundation: populate source slot identity.
        source_active_slot_id=runtime_status.active_slot_id,
        source_previous_slot_id=runtime_status.previous_slot_id,
        steps=steps,
        blocked=False,
    )


def _plan_step_for_component(
    dc: DesiredComponent,
    definition: ComponentDefinition,
    probe: dict[str, Any],
) -> DeploymentStep:
    """Decide KEEP / INSTALL for one component based on the probe result."""
    # --- Not installed -------------------------------------------------------
    if probe.get("installed") is False:
        return DeploymentStep(
            component_id=dc.component_id,
            desired_version=dc.version,
            artifact=dc.artifact,
            action=DeploymentAction.INSTALL,
            reason_code=DeploymentReasonCode.DISTRIBUTION_MISSING,
            reason=f"distribution {definition.distribution_name!r} not installed",
        )

    # --- Installed — check version -------------------------------------------
    installed_version = _string_or_none(probe.get("version"))

    if installed_version != dc.version:
        return DeploymentStep(
            component_id=dc.component_id,
            desired_version=dc.version,
            artifact=dc.artifact,
            action=DeploymentAction.INSTALL,
            current_version=installed_version,
            reason_code=DeploymentReasonCode.VERSION_MISMATCH,
            reason=(
                f"version mismatch: installed {installed_version!r}, "
                f"desired {dc.version!r}"
            ),
        )

    # --- Installed, version matches — check launch contract -------------------
    contract_ok = _check_launch_contract_from_probe(probe, definition)

    if not contract_ok:
        return DeploymentStep(
            component_id=dc.component_id,
            desired_version=dc.version,
            artifact=dc.artifact,
            action=DeploymentAction.INSTALL,
            current_version=installed_version,
            reason_code=DeploymentReasonCode.LAUNCH_CONTRACT_MISMATCH,
            reason="installed distribution does not satisfy the expected launch contract",
        )

    # --- Everything matches — KEEP -------------------------------------------
    return DeploymentStep(
        component_id=dc.component_id,
        desired_version=dc.version,
        artifact=dc.artifact,
        action=DeploymentAction.KEEP,
        current_version=installed_version,
        reason_code=DeploymentReasonCode.ALREADY_SATISFIED,
        reason="installed version and launch contract are correct",
    )


def _check_launch_contract_from_probe(
    probe: dict[str, Any],
    definition: ComponentDefinition,
) -> bool:
    """Return True if *probe* entry_points satisfy the *definition* launch contract.

    This is a local copy of the logic from :mod:`zealfie.runtime.manager` to
    avoid importing private helpers from a mutation module.
    """
    expected_contracts = set(definition.launch_entry_points)
    if not expected_contracts:
        # No launch contract required — satisfied by definition.
        return True

    observed_eps = probe.get("entry_points", [])
    if not isinstance(observed_eps, list):
        return False

    # Import locally to avoid coupling to components.model internals.
    from zealfie.components.model import EntryPointContract

    for ep in observed_eps:
        try:
            contract = EntryPointContract(
                group=str(ep.get("group", "")),
                name=str(ep.get("name", "")),
            )
        except ValueError:
            continue
        if contract in expected_contracts:
            return True

    return False


def _string_or_none(value: object) -> str | None:
    """Convert a value to str, or return None if it is None."""
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Probe payload validation (strict, fail-closed)
# ---------------------------------------------------------------------------


def _validate_probe_payload(probe: dict[str, Any], component_id: str) -> str | None:
    """Validate the structure of a READY probe payload.

    Returns ``None`` if the payload is well-formed, or an error message
    string suitable for ``PROBE_FAILED`` reason.

    Rules (fail-closed — any violation blocks the plan):

    * ``installed`` must be exactly ``bool``.
    * If ``installed is False``:
        * ``version`` must be present and exactly ``None``.
        * ``entry_points`` must be present, must be a ``list``, and
          must be empty.
    * If ``installed is True``:
        * ``version`` must be a non-empty ``str``.
        * ``entry_points`` must be a ``list``.
        * Every item in ``entry_points`` must be a ``dict`` whose
          ``group`` and ``name`` values are ``str``.
    """
    installed = probe.get("installed")

    # --- installed must be exactly bool ---------------------------------
    if not isinstance(installed, bool):
        return (
            f"probe payload for {component_id!r}: "
            f"installed must be bool, got {type(installed).__name__}"
        )

    # --- installed False ------------------------------------------------
    if installed is False:
        # version must exist and be exactly None.
        if "version" not in probe:
            return (
                f"probe payload for {component_id!r}: "
                f"missing 'version' key when installed=False"
            )
        version = probe["version"]
        if version is not None:
            return (
                f"probe payload for {component_id!r}: "
                f"version must be None when installed=False, "
                f"got {type(version).__name__}={version!r}"
            )
        # entry_points must exist, be a list, and be empty.
        if "entry_points" not in probe:
            return (
                f"probe payload for {component_id!r}: "
                f"missing 'entry_points' key when installed=False"
            )
        entry_points = probe["entry_points"]
        if not isinstance(entry_points, list):
            return (
                f"probe payload for {component_id!r}: "
                f"entry_points must be a list when installed=False, "
                f"got {type(entry_points).__name__}"
            )
        if len(entry_points) != 0:
            return (
                f"probe payload for {component_id!r}: "
                f"entry_points must be empty when installed=False, "
                f"got {type(entry_points).__name__} "
                f"with {len(entry_points)} item(s)"
            )
        return None
    # --- installed True -------------------------------------------------

    version = probe.get("version")
    if not isinstance(version, str) or not version:
        return (
            f"probe payload for {component_id!r}: "
            f"version must be non-empty str when installed=True, "
            f"got {type(version).__name__}={version!r}"
        )

    entry_points = probe.get("entry_points")
    if not isinstance(entry_points, list):
        return (
            f"probe payload for {component_id!r}: "
            f"entry_points must be a list when installed=True, "
            f"got {type(entry_points).__name__}"
        )

    for i, ep in enumerate(entry_points):
        if not isinstance(ep, dict):
            return (
                f"probe payload for {component_id!r}: "
                f"entry_points[{i}] must be dict, got {type(ep).__name__}"
            )
        group = ep.get("group")
        name = ep.get("name")
        if not isinstance(group, str) or not isinstance(name, str):
            return (
                f"probe payload for {component_id!r}: "
                f"entry_points[{i}] group/name must be str, "
                f"got group={type(group).__name__} name={type(name).__name__}"
            )

    return None


# ---------------------------------------------------------------------------
# Internal helpers (continued)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# M0-8B foundation: desired-state conflict hardening
# ---------------------------------------------------------------------------


def check_desired_state_conflicts(
    desired_state: DesiredRuntimeState,
    registry: ComponentRegistry,
) -> None:
    """Refuse incoherent shared-runtime desires before planning succeeds.

    Two invariants are enforced — this is intentionally NOT a dependency
    resolver, conflict solver, or version negotiator.  It is a fail-closed
    structural guard.

    1. No two registry components may normalise to the same Python
       distribution name (as installed by pip).  A shared venv cannot
       contain two distributions with the same normalised name.

    2. No two components may declare the same launch entry-point
       ``group:name`` contract.  Two components that both claim
       ``console_scripts:zesolver`` would make the runtime incoherent
       — only one can own the entry-point.

    Raises
    ------
    PlanningError
        If either invariant is violated.
    """
    # --- Duplicate normalised distribution names ---------------------------
    seen_dists: dict[str, str] = {}  # normalised_name -> component_id
    for dc in desired_state.components:
        try:
            definition = registry.get(dc.component_id)
        except KeyError:
            # Should not happen — full validation occurred earlier.
            continue
        normalised = normalise_distribution_name(definition.distribution_name)
        if normalised in seen_dists:
            raise PlanningError(
                f"duplicate normalised distribution name {normalised!r}: "
                f"components {seen_dists[normalised]!r} and {dc.component_id!r} "
                f"both normalise to {normalised!r}; a shared venv cannot contain "
                f"two distributions with the same normalised pip name"
            )
        seen_dists[normalised] = dc.component_id

    # --- Duplicate launch entry-point group:name contracts ------------------
    seen_contracts: dict[tuple[str, str], str] = {}
    for dc in desired_state.components:
        try:
            definition = registry.get(dc.component_id)
        except KeyError:
            continue
        for contract in definition.launch_entry_points:
            key = (contract.group, contract.name)
            if key in seen_contracts:
                raise PlanningError(
                    f"duplicate launch entry-point contract "
                    f"{contract.group!r}:{contract.name!r}: "
                    f"components {seen_contracts[key]!r} and "
                    f"{dc.component_id!r} both declare the same "
                    f"entry-point; a shared runtime cannot resolve the ambiguity"
                )
            seen_contracts[key] = dc.component_id
