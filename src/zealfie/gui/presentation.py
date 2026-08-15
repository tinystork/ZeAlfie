"""M1-2C — product state ↔ UI presentation mapping.

Maps :class:`ProductState` enums and fields to human-readable GUI text.
Never shows raw enum names to users.
"""

from __future__ import annotations

from zealfie.acceleration import AcceleratedPlanStatus
from zealfie.app import (
    ManagedStatus,
    ProductState,
    ProductStateReasonCode,
    ProductUpdateResult,
    UpdateStatus,
)


# ---------------------------------------------------------------------------
# User-facing state text
# ---------------------------------------------------------------------------

_STATE_LABELS: dict[ProductStateReasonCode, str] = {
    ProductStateReasonCode.RUNTIME_ABSENT: "No runtime — deploy a runtime first",
    ProductStateReasonCode.RUNTIME_BROKEN: "Runtime broken — check or recreate",
    ProductStateReasonCode.INSTALLED_LAUNCHABLE: "Ready — click Lancer to start",
    ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE: "Installed but launch contract missing",
    ProductStateReasonCode.NOT_INSTALLED: "Not installed — click Installer to fetch and install",
    ProductStateReasonCode.PROBE_FAILED: "Could not check — probe failed",
}


def state_label(state: ProductState) -> str:
    """Return a human-readable label for a product state.

    Never leaks raw enum values.
    Falls back to the reason string if the code is unknown.
    """
    label = _STATE_LABELS.get(state.reason_code)
    if label is not None:
        return label
    # safety fallback — never raw enum
    return state.reason or "Unknown state"


def action_label(state: ProductState) -> str:
    """Return the primary action button label for a product state."""
    if state.launchable:
        return "🚀 Lancer"
    return "📦 Installer"


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
        return f"Launch {state.display_name}"
    if state.installed and not state.launchable:
        return "Launch contract not satisfied — product is installed but cannot be launched"
    return f"Install {state.display_name}"


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
        return "Checking for updates\u2026"
    if status is UpdateStatus.UP_TO_DATE:
        return "Up to date"
    if status is UpdateStatus.UPDATE_AVAILABLE:
        sha = _short_sha(getattr(result, "latest_commit_sha", None))
        if sha:
            return f"Update available ({sha})"
        return "Update available"
    if status is UpdateStatus.CHECK_FAILED:
        error = _short_error(getattr(result, "error", None))
        if error:
            return f"Update check failed: {error}"
        return "Update check failed"
    if status is UpdateStatus.PROVENANCE_UNKNOWN:
        return "Update status unknown"
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
        return "Runtime: absent"
    if runtime_state_value == "BROKEN":
        return "Runtime: broken"
    if installed_count == 0:
        return f"Runtime: ready — {total_known} known, none installed"
    return (
        f"Runtime: ready — {installed_count}/{total_known} installed"
        + (f", {managed_count} managed" if managed_count > 0 else "")
    )


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

    Never renders raw enum names or traceback content.
    """
    status = plan.status
    if status is AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS:
        return (
            "No product declares GPU acceleration requirements.",
            "The CPU deployment closure is preserved unchanged.",
        )
    if status is AcceleratedPlanStatus.UNKNOWN:
        return (
            "GPU acceleration status could not be determined.",
            "No accelerated change has been planned.",
        )
    if status is AcceleratedPlanStatus.BLOCKED:
        reason = plan.blocked_reason or "no reason recorded"
        return (
            "GPU acceleration planning is blocked.",
            f"Reason: {reason}",
        )
    # PLAN_READY
    lines = [
        f"Hardware: {plan.hardware.status.value}",
        f"Backend: {plan.backend}",
    ]
    concerned = ", ".join(plan.products_concerned)
    lines.append(f"Products concerned: {concerned or 'none'}")
    for keep in plan.keep_products:
        commit = keep.commit_sha or "unknown"
        lines.append(f"Keep {keep.product_id} {keep.version} (commit {commit})")
    if plan.closure_impact:
        lines.append("Planned actions:")
        lines.extend(f" - {line}" for line in plan.closure_impact)
    else:
        lines.append("Planned actions: none recorded")
    lines.append("No changes have been made yet.")
    return tuple(lines)
