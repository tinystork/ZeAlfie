"""M1-2C — product state ↔ UI presentation mapping.

Maps :class:`ProductState` enums and fields to human-readable GUI text.
Never shows raw enum names to users.
"""

from __future__ import annotations

from zealfie.app import ManagedStatus, ProductState, ProductStateReasonCode


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
