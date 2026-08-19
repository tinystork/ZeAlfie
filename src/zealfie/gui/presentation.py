"""M1-2C — product state ↔ UI presentation mapping.

Maps :class:`ProductState` enums and fields to human-readable GUI text.
Never shows raw enum names to users.  All user-visible text is localized via
:func:`zealfie.i18n.translate`; machine phase names / codes that are shown
as-is (e.g. ``Hardware: SUPPORTED``, ``REQUIRED_HOST``, ``MANAGED_RUNTIME``)
are left untouched.
"""

from __future__ import annotations

from zealfie.acceleration import (
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentResult,
    AcceleratedPlanStatus,
    HostPrerequisiteStatus,
)
from zealfie.app import (
    InstallPhase,
    ManagedStatus,
    PHASE_PERCENT,
    ProductState,
    ProductStateReasonCode,
    ProductUpdateResult,
    UpdateStatus,
)
from zealfie.i18n import translate
from zealfie.host import RecommendationStatus


# ---------------------------------------------------------------------------
# User-facing state text
# ---------------------------------------------------------------------------

_STATE_LABELS: dict[ProductStateReasonCode, str] = {
    ProductStateReasonCode.RUNTIME_ABSENT: "state.runtime_absent",
    ProductStateReasonCode.RUNTIME_BROKEN: "state.runtime_broken",
    ProductStateReasonCode.INSTALLED_LAUNCHABLE: "state.installed_launchable",
    ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE: "state.installed_not_launchable",
    ProductStateReasonCode.NOT_INSTALLED: "state.not_installed",
    ProductStateReasonCode.PROBE_FAILED: "state.probe_failed",
}


def state_label(state: ProductState) -> str:
    """Return a human-readable label for a product state.

    Never leaks raw enum values.
    Falls back to the reason string if the code is unknown.
    """
    key = _STATE_LABELS.get(state.reason_code)
    if key is not None:
        return translate(key)
    # safety fallback — never raw enum
    return state.reason or translate("state.unknown")


def action_label(state: ProductState) -> str:
    """Return the primary action button label for a product state."""
    if state.launchable:
        return "🚀 " + translate("cards.launch")
    return "📦 " + translate("cards.install")


def action_enabled(state: ProductState) -> bool:
    """Return whether the primary action button should be enabled."""
    if state.launchable:
        return True
    # Enable Installer for products that are not installed (or have a
    # broken runtime — runtime creation happens before install).
    if not state.installed:
        return True
    # Installed but not launchable: button stays disabled.
    return False


def action_tooltip(state: ProductState) -> str:
    """Return the tooltip for the primary action button."""
    if state.launchable:
        return translate("action.launch_tooltip", name=state.display_name)
    if state.installed and not state.launchable:
        return translate("action.launch_contract_missing")
    return translate("action.install_tooltip", name=state.display_name)


# ---------------------------------------------------------------------------
# Update status → UI text (M1-2E LOT E.4)
# ---------------------------------------------------------------------------


def _short_sha(sha: str | None) -> str:
    """Return a short (7-char) form of a commit SHA, or ``""``."""
    if not sha:
        return ""
    sha = str(sha).strip()
    return sha[:7] if len(sha) >= 7 else sha


def _short_error(error: str | None) -> str:
    """Return a compact, single-line, user-safe error message.

    Collapses whitespace (stripping any traceback-style line breaks) and
    truncates long messages.  Never returns raw exception/enum names.
    """
    if not error:
        return ""
    text = str(error)
    # Defensive: if a raw Python traceback leaked through, drop it and any
    # trailing frame lines — users never need to see them.
    marker = "Traceback (most recent call last)"
    index = text.find(marker)
    if index != -1:
        text = text[:index]
    text = " ".join(text.split())
    if len(text) > 80:
        text = text[:77] + "\u2026"
    return text


def update_status_label(result: ProductUpdateResult | None) -> str:
    """Return a user-facing update-status label for a product card.

    ``result`` is a :class:`~zealfie.app.ProductUpdateResult` (or any
    object carrying a ``status`` attribute).  Returns ``""`` for
    ``NOT_CHECKED`` (and for ``None``) so callers can hide the label
    entirely.  Never returns raw enum names or tracebacks.

    Mapping (M1-2E LOT E.4):

    * ``NOT_CHECKED``        → ``""`` (no scary/error state).
    * ``CHECKING``           → "Checking for updates…".
    * ``UP_TO_DATE``         → "Up to date" (subdued, stable).
    * ``UPDATE_AVAILABLE``   → "Update available (<short latest SHA>)".
    * ``CHECK_FAILED``       → "Update check failed: <short error>".
    * ``PROVENANCE_UNKNOWN`` → "Update status unknown".
    """
    if result is None:
        return ""
    status = getattr(result, "status", None)
    if status is UpdateStatus.NOT_CHECKED:
        return ""
    if status is UpdateStatus.CHECKING:
        return translate("update.checking")
    if status is UpdateStatus.UP_TO_DATE:
        return translate("update.up_to_date")
    if status is UpdateStatus.UPDATE_AVAILABLE:
        sha = _short_sha(getattr(result, "latest_commit_sha", None))
        if sha:
            return translate("update.available_sha", sha=sha)
        return translate("update.available")
    if status is UpdateStatus.CHECK_FAILED:
        error = _short_error(getattr(result, "error", None))
        if error:
            return translate("update.check_failed_error", error=error)
        return translate("update.check_failed")
    if status is UpdateStatus.PROVENANCE_UNKNOWN:
        return translate("update.unknown")
    return ""

# ---------------------------------------------------------------------------
# Runtime status summary
# ---------------------------------------------------------------------------


def runtime_summary(
    runtime_state_value: str,
    installed_count: int,
    managed_count: int,
    total_known: int,
) -> str:
    """Return a one-line runtime status summary for the status bar."""
    if runtime_state_value == "ABSENT":
        return translate("runtime.absent")
    if runtime_state_value == "BROKEN":
        return translate("runtime.broken")
    if installed_count == 0:
        return translate("runtime.ready_none", total=total_known)
    text = translate(
        "runtime.ready", installed=installed_count, total=total_known
    )
    if managed_count > 0:
        text += translate("runtime.managed_suffix", managed=managed_count)
    return text


# ---------------------------------------------------------------------------
# Compact GPU status badge (M1-5-A)
# ---------------------------------------------------------------------------


def primary_nvidia_gpu(recommendation):
    """Return the first NVIDIA GPU of a recommendation, or ``None``."""
    for gpu in getattr(recommendation, "gpus", ()) or ():
        if getattr(gpu, "is_nvidia", False):
            return gpu
    return None


def compact_gpu_status(
    recommendation,
    *,
    install_active: bool = False,
) -> str:
    """Return the one-line GPU acceleration status for the home badge.

    Pure presentation mapping (no Qt, no probing): mirrors the
    recommendation status into a short localized label, plus an honest
    "installing" state while the accelerated install worker is running.
    Never leaks raw enum names.
    """
    if install_active:
        return translate("gpu.badge.installing")
    if recommendation is None:
        return translate("gpu.badge.unknown")
    status = recommendation.status
    if status is RecommendationStatus.OFFER_SETUP:
        gpu = primary_nvidia_gpu(recommendation)
        if gpu is not None and gpu.model:
            return translate("gpu.badge.offer_setup_nvidia", model=gpu.model)
        return translate("gpu.badge.offer_setup")
    if status is RecommendationStatus.ALREADY_READY:
        return translate("gpu.badge.ready")
    if status is RecommendationStatus.BLOCKED:
        return translate("gpu.badge.blocked")
    if status is RecommendationStatus.NOT_APPLICABLE:
        return translate("gpu.badge.not_applicable")
    return translate("gpu.badge.unknown")


# ---------------------------------------------------------------------------
# Accelerated GPU plan → UI text (M1-2H)
# ---------------------------------------------------------------------------


def gpu_plan_preview_lines(plan) -> tuple[str, ...]:
    """Return honest user-facing preview lines for an accelerated plan.

    Pure presentation mapping (no Qt, no I/O) covering all four
    :class:`~zealfie.acceleration.planning.AcceleratedPlanStatus`
    values:

    * ``NO_ACCELERATED_REQUIREMENTS`` — no product declares GPU
      requirements and the CPU closure is preserved unchanged;
    * ``UNKNOWN`` — honest unknown, no fabricated detail;
    * ``BLOCKED`` — the blocked reason;
    * ``PLAN_READY`` — hardware, backend, products concerned, KEEP
      products, and the planned actions exactly as the planner
      documented them (never invented), plus the explicit statement
      that nothing has been modified yet.

    Never renders raw enum names or traceback content.  Machine codes shown
    as-is (e.g. ``Hardware: SUPPORTED``, ``REQUIRED_HOST``) are preserved.
    """
    status = plan.status
    if status is AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS:
        return (
            translate("plan.no_requirements"),
            translate("plan.cpu_preserved"),
        )
    if status is AcceleratedPlanStatus.UNKNOWN:
        return (
            translate("plan.unknown"),
            translate("plan.no_change"),
        )
    if status is AcceleratedPlanStatus.BLOCKED:
        reason = plan.blocked_reason or translate("plan.no_reason")
        return (
            translate("plan.blocked"),
            translate("plan.reason", reason=reason),
        )
    # PLAN_READY
    lines = [
        translate("plan.hardware", value=plan.hardware.status.value),
        translate("plan.backend", backend=plan.backend),
    ]
    concerned = ", ".join(plan.products_concerned)
    lines.append(
        translate(
            "plan.products_concerned",
            list=(concerned or translate("plan.none")),
        )
    )
    for keep in plan.keep_products:
        commit = keep.commit_sha or translate("plan.unknown_commit")
        lines.append(
            translate(
                "plan.keep",
                product=keep.product_id,
                version=keep.version,
                commit=commit,
            )
        )
    if plan.closure_impact:
        lines.append(translate("plan.actions"))
        lines.extend(
            translate("plan.action_item", line=line)
            for line in plan.closure_impact
        )
    else:
        lines.append(translate("plan.actions_none"))
    if plan.host_prerequisites is not None:
        lines.append(translate("plan.host_prereqs"))
        for entry in plan.host_prerequisites.required_host:
            observed = (
                f" (observed {entry.observed})" if entry.observed else ""
            )
            status = (
                ""
                if entry.status is HostPrerequisiteStatus.OK
                else f" [{entry.status.display}]"
            )
            lines.append(
                f" - REQUIRED_HOST {entry.entry} "
                f"{entry.requirement}{observed}{status}"
            )
        for entry in plan.host_prerequisites.managed_runtime:
            lines.append(
                f" - MANAGED_RUNTIME {entry.entry} {entry.requirement}"
            )
    lines.append(translate("plan.no_changes_yet"))
    return tuple(lines)


# ---------------------------------------------------------------------------
# Accelerated deployment progress → UI view (M1-2I / I3)
# ---------------------------------------------------------------------------

#: i18n key per accelerated deployment phase.  GATE renders like VALIDATE and
#: PERSIST renders like ACTIVATE because they are sub-steps of the same
#: user-visible moment (the honest check before activation, and the metadata
#: write inside it).
_ACCELERATED_PHASE_KEYS: dict[str, str] = {
    AcceleratedDeploymentPhase.PREPARE.value: "phase.preparation",
    AcceleratedDeploymentPhase.ACQUIRE.value: "phase.download",
    AcceleratedDeploymentPhase.RESOLVE.value: "phase.dependency_resolution",
    AcceleratedDeploymentPhase.BUILD.value: "phase.runtime_build",
    AcceleratedDeploymentPhase.VALIDATE.value: "phase.validation",
    AcceleratedDeploymentPhase.GATE.value: "phase.validation",
    AcceleratedDeploymentPhase.PERSIST.value: "phase.activation",
    AcceleratedDeploymentPhase.ACTIVATE.value: "phase.activation",
    AcceleratedDeploymentPhase.COMPLETED.value: "phase.completed",
}

#: i18n key per shared ``InstallPhase`` observation.  Covers all ten phases:
#: the accelerated path emits PREPARING / ACQUIRING_DEPENDENCIES /
#: PLANNING_RUNTIME / INSTALLING_RUNTIME / VALIDATING / ACTIVATING /
#: COMPLETED; the remaining three (product-install phases) are mapped
#: defensively so a mixed event stream still renders honestly.
_INSTALL_PHASE_KEYS: dict[InstallPhase, str] = {
    InstallPhase.PREPARING: "phase.preparation",
    InstallPhase.RESOLVING_SOURCE: "phase.download",
    InstallPhase.DOWNLOADING_SOURCE: "phase.download",
    InstallPhase.BUILDING_PRODUCT: "phase.runtime_build",
    InstallPhase.ACQUIRING_DEPENDENCIES: "phase.download",
    InstallPhase.PLANNING_RUNTIME: "phase.dependency_resolution",
    InstallPhase.INSTALLING_RUNTIME: "phase.runtime_build",
    InstallPhase.VALIDATING: "phase.validation",
    InstallPhase.ACTIVATING: "phase.activation",
    InstallPhase.COMPLETED: "phase.completed",
}


def accelerated_phase_label(phase) -> str:
    """Return a deterministic user-facing label for a phase.

    Accepts an
    :class:`~zealfie.acceleration.deployment.AcceleratedDeploymentPhase`
    (or anything carrying a ``.value`` string, or a raw string).  Never
    leaks raw enum values; unknown phases fall back to "In progress".
    """
    value = getattr(phase, "value", phase)
    key = _ACCELERATED_PHASE_KEYS.get(str(value))
    if key is None:
        return translate("phase.in_progress")
    return translate(key)


def accelerated_install_view(events) -> tuple[str, int | None, bool]:
    """Reduce a sequence of deployment observations to the current view.

    Pure, Qt-free, deterministic.  *events* is any iterable of observed
    events, in order:

    * an ``InstallProgress`` (an object whose ``phase`` is an
      :class:`~zealfie.app.progress.InstallPhase`) — updates the label
      and the percent;
    * an
      :class:`~zealfie.acceleration.deployment.AcceleratedDeploymentPhase`
      — updates the label only;
    * a deployment result (an object carrying a boolean ``success``
      attribute) — the terminal verdict;
    * anything else is ignored.

    Contract:

    * ``label`` — the deterministic user-facing label of the LAST
      observed phase;
    * ``percent`` — ONLY real values: the canonical fixed
      :data:`~zealfie.app.progress.PHASE_PERCENT` value for the last
      observed ``InstallProgress`` phase (the event's own ``percent``
      field is deliberately ignored — fixed table values only, never
      invented), or ``100`` after a success result reached COMPLETED;
      ``None`` when no progress was observed or after a
      failed/cancelled result (no fake progress);
    * ``done`` — ``True`` ONLY for COMPLETED (a COMPLETED
      ``InstallProgress``, a COMPLETED raw phase, or a success result);
      a failed/cancelled result forces ``done=False``.

    Invariants: ``percent=100`` is never returned unless ``done``;
    empty input yields ``("Preparation", None, False)``.
    """
    label, percent, done = translate("phase.preparation"), None, False
    for event in events:
        if isinstance(event, AcceleratedDeploymentResult) or (
            hasattr(event, "success") and hasattr(event, "phase")
        ):
            # Terminal verdict (real or duck-typed result).
            if bool(getattr(event, "success", False)):
                done = True
                label = accelerated_phase_label(event.phase)
                if getattr(event, "phase", None) is (
                    AcceleratedDeploymentPhase.COMPLETED
                ):
                    percent = 100
            else:
                # Failure / cancellation: never fabricate progress.
                done = False
                percent = None
                label = accelerated_phase_label(event.phase)
            continue

        phase = getattr(event, "phase", None)
        if isinstance(phase, InstallPhase):
            key = _INSTALL_PHASE_KEYS.get(phase)
            label = translate(key) if key is not None else translate("phase.in_progress")
            percent = PHASE_PERCENT.get(phase)
            done = phase is InstallPhase.COMPLETED
            continue

        if isinstance(event, AcceleratedDeploymentPhase):
            label = accelerated_phase_label(event)
            if event is AcceleratedDeploymentPhase.COMPLETED:
                done = True
            continue

        # Unknown event type: ignored.
    return label, percent, done
