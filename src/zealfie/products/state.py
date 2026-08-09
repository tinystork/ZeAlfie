"""Product-shell read model — product state as observed from the runtime.

State determination rules:

1. **Installed** is determined exclusively from the ZeAlfie-managed
   runtime (``SharedRuntime``), never from dev venv, ``PYTHONPATH``,
   checkout importability, or global package state.

2. **Launchable** is ``True`` only when the runtime's active slot
   contains the product's distribution and the launch entry-point
   contract is satisfied in that runtime.  Launchability is never
   inferred from catalog knowledge alone.

3. **Managed** indicates whether the product appears in the current
   component (deployment) registry — i.e. whether the user has chosen to
   manage/install it.  Unmanaged products are known but not currently
   selected for deployment planning.

4. **Unknown products** are rejected with a typed
   :class:`~zealfie.products.catalog.UnknownProductError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import RuntimeState, RuntimeStatus

from .catalog import ProductCatalog, ProductDescriptor, UnknownProductError


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProductStateReasonCode(StrEnum):
    """Stable reason codes for product state."""

    # -- Runtime-level reasons -----------------------------------------------
    RUNTIME_ABSENT = "RUNTIME_ABSENT"
    """The shared runtime does not exist — nothing can be installed."""

    RUNTIME_BROKEN = "RUNTIME_BROKEN"
    """The shared runtime is BROKEN — state cannot be determined."""


    # -- Installed → True reasons --------------------------------------------
    INSTALLED_LAUNCHABLE = "INSTALLED_LAUNCHABLE"
    """Product distribution is installed and launch contract is satisfied."""

    INSTALLED_NOT_LAUNCHABLE = "INSTALLED_NOT_LAUNCHABLE"
    """Product distribution is installed but the expected launch
    entry-point contract is not satisfied in the runtime."""

    # -- Installed → False reasons -------------------------------------------
    NOT_INSTALLED = "NOT_INSTALLED"
    """Product distribution is not installed in the active runtime."""

    PROBE_FAILED = "PROBE_FAILED"
    """Could not probe the runtime for this product."""


class ManagedStatus(StrEnum):
    """Whether a known product is currently managed by ZeAlfie's
    deployment contract."""

    MANAGED = "MANAGED"
    """Product is in the component registry — selected for deployment."""

    UNMANAGED = "UNMANAGED"
    """Product is in the catalog but not in the component registry."""


# ---------------------------------------------------------------------------
# Product state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductState:
    """Immutable snapshot of one product's state as observed from the
    ZeAlfie-managed runtime.

    This is a read model for the CLI and future GUI — it carries no
    mutation, no deployment planning, and no launch execution.
    """

    product_id: str
    display_name: str
    known: bool
    installed: bool
    launchable: bool
    version: str | None
    reason_code: ProductStateReasonCode
    reason: str
    managed: ManagedStatus = ManagedStatus.UNMANAGED

    def __post_init__(self) -> None:
        for field_name in ("product_id", "display_name"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)


# ---------------------------------------------------------------------------
# Product shell state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductShellState:
    """Immutable snapshot of all product state + runtime context.

    This is the top-level read model consumed by the ``zealfie products``
    CLI command and (future) GUI product shell.
    """

    runtime_state: RuntimeState
    runtime_root: Path
    products: tuple[ProductState, ...]
    managed_count: int = field(default=0)
    installed_count: int = field(default=0)

    def __post_init__(self) -> None:
        mcount = sum(1 for p in self.products if p.managed == ManagedStatus.MANAGED)
        icount = sum(1 for p in self.products if p.installed)
        object.__setattr__(self, "managed_count", mcount)
        object.__setattr__(self, "installed_count", icount)


# ---------------------------------------------------------------------------
# State collection (pure, injectable)
# ---------------------------------------------------------------------------


def collect_product_state(
    catalog: ProductCatalog,
    runtime_status: RuntimeStatus,
    *,
    managed_component_ids: frozenset[str] = frozenset(),
    probe_fn: object = None,
) -> ProductShellState:
    """Collect product state for every known product against the current
    runtime.

    Parameters
    ----------
    catalog:
        The product catalog (all known products).
    runtime_status:
        Current ``SharedRuntime.status()`` observation.
    managed_component_ids:
        The set of component ids currently in the deployment registry
        (i.e. products the user chose to manage).
    probe_fn:
        Callable ``(runtime_python: str, distribution_name: str) -> dict``
        with the same signature as
        :func:`~zealfie.runtime.probe.probe_runtime_distribution`.
        Injected for testability; defaults to the real probe.

    Returns
    -------
    ProductShellState
        Immutable snapshot of all product state.
    """
    # Resolve probe.
    if probe_fn is None:
        from zealfie.runtime.probe import probe_runtime_distribution
        probe = probe_runtime_distribution
    else:
        probe = probe_fn

    products: list[ProductState] = []

    for descriptor in catalog.list():
        state = _determine_product_state(
            descriptor,
            runtime_status,
            managed_component_ids=managed_component_ids,
            probe=probe,
        )
        products.append(state)

    return ProductShellState(
        runtime_state=runtime_status.state,
        runtime_root=runtime_status.runtime_root,
        products=tuple(products),
    )


def get_product_state(
    catalog: ProductCatalog,
    product_id: str,
    runtime_status: RuntimeStatus,
    *,
    managed_component_ids: frozenset[str] = frozenset(),
    probe_fn: object = None,
) -> ProductState:
    """Collect state for a single product.

    Raises :class:`UnknownProductError` if *product_id* is not in the catalog.
    """
    descriptor = catalog.get(product_id)  # raises UnknownProductError on miss

    if probe_fn is None:
        from zealfie.runtime.probe import probe_runtime_distribution
        probe = probe_runtime_distribution
    else:
        probe = probe_fn

    return _determine_product_state(
        descriptor,
        runtime_status,
        managed_component_ids=managed_component_ids,
        probe=probe,
    )


# ---------------------------------------------------------------------------
# Internal state determination
# ---------------------------------------------------------------------------


def _determine_product_state(
    desc: ProductDescriptor,
    runtime_status: RuntimeStatus,
    *,
    managed_component_ids: frozenset[str],
    probe,
) -> ProductState:
    """Determine product state from runtime observation."""
    managed = (
        ManagedStatus.MANAGED
        if desc.product_id in managed_component_ids
        else ManagedStatus.UNMANAGED
    )
    display_name = desc.display_name

    # -- Runtime ABSENT ---------------------------------------------------
    if runtime_status.state == RuntimeState.ABSENT:
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.RUNTIME_ABSENT,
            reason="shared runtime is absent",
            managed=managed,
        )

    # -- Runtime BROKEN ---------------------------------------------------
    if runtime_status.state == RuntimeState.BROKEN:
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.RUNTIME_BROKEN,
            reason=runtime_status.reason or "shared runtime is BROKEN",
            managed=managed,
        )

    # -- Runtime READY — probe the active slot ----------------------------
    runtime_python = runtime_status.python_executable
    if runtime_python is None:
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.RUNTIME_BROKEN,
            reason="runtime is READY but has no Python executable",
            managed=managed,
        )

    try:
        probe_result = probe(str(runtime_python), desc.distribution_name)
    except Exception as exc:
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.PROBE_FAILED,
            reason=f"runtime probe failed: {exc}",
            managed=managed,
        )

    # Validate probe payload structure.
    if not isinstance(probe_result, dict):
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.PROBE_FAILED,
            reason="runtime probe returned non-dict payload",
            managed=managed,
        )

    installed = probe_result.get("installed")
    if installed is not True:
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason=f"distribution {desc.distribution_name!r} not installed in runtime",
            managed=managed,
        )

    # Installed — check version and launch contract.
    version = probe_result.get("version")
    version_str = str(version) if version else None

    launchable = _check_launch_contract_from_probe(
        probe_result, desc.launch_entry_points
    )

    if launchable:
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=True,
            launchable=True,
            version=version_str,
            reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason=f"{desc.distribution_name} {version_str} installed and launchable",
            managed=managed,
        )
    else:
        expected = ", ".join(
            f"{ep.group}:{ep.name}" for ep in desc.launch_entry_points
        )
        return ProductState(
            product_id=desc.product_id,
            display_name=display_name,
            known=True,
            installed=True,
            launchable=False,
            version=version_str,
            reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE,
            reason=(
                f"{desc.distribution_name} {version_str} installed but "
                f"expected launch contract ({expected}) not satisfied"
            ),
            managed=managed,
        )


def _check_launch_contract_from_probe(
    probe_result: dict,
    expected_entry_points: tuple[EntryPointContract, ...],
) -> bool:
    """Check whether any expected launch entry-point contract is present
    in the probe's entry_points."""
    if not expected_entry_points:
        return True  # No contract → launchable by definition.

    expected = {(ep.group, ep.name) for ep in expected_entry_points}
    observed_eps = probe_result.get("entry_points", [])
    if not isinstance(observed_eps, list):
        return False

    for ep in observed_eps:
        if not isinstance(ep, dict):
            continue
        g = str(ep.get("group", ""))
        n = str(ep.get("name", ""))
        if (g, n) in expected:
            return True

    return False
