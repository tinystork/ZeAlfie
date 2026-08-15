"""Pure accelerated deployment planning (M1-2H).

Builds the read-only :class:`AcceleratedDeploymentPlan` describing how
the shared runtime closure must change to satisfy the accelerated
requirements declared by products.  The planner is pure and
deterministic: no I/O, no network, no Qt, no mutation of its inputs —
it only documents what *would* change.

Architectural invariant — ZeAlfie NEVER selects a concrete accelerated
framework.  Requirements are merged per distribution across products,
and concrete variants are looked up in an explicit
:class:`~zealfie.acceleration.variants.AcceleratedVariantCatalog`;
a missing variant blocks the plan (fail-closed, no partial fallback,
no silent approximation).  KEEP products are documented verbatim from
provenance and are never re-resolved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from zealfie.acceleration.compatibility import (
    evaluate_acceleration_compatibility,
)
from zealfie.acceleration.models import (
    HardwareCompatibility,
    HardwareCompatibilityStatus,
    ProductAccelerationRequirements,
)
from zealfie.acceleration.variants import (
    AcceleratedVariant,
    AcceleratedVariantCatalog,
)
from zealfie.host.models import (
    AccelerationRecommendation,
    HostCapabilities,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zealfie.products.catalog import ProductCatalog
    from zealfie.runtime.model import RuntimeState, RuntimeStatus


@dataclass(frozen=True, slots=True)
class PlannedKeepProduct:
    """Exact preservation of one KEEP product — never re-resolved.

    ``product_id`` identifies the product, ``version`` is the exact
    installed version, and ``commit_sha`` / ``wheel_sha256`` are the
    provenance records of the source commit and artifact digest.  The
    planner documents these values verbatim; it never resolves,
    rebuilds, or alters them.
    """

    product_id: str
    version: str
    commit_sha: str | None = None
    wheel_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product_id, str) or not self.product_id.strip():
            raise ValueError("product_id must be a non-empty string")
        object.__setattr__(self, "product_id", self.product_id.strip())

        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        object.__setattr__(self, "version", self.version.strip())

        for field_name in ("commit_sha", "wheel_sha256"):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{field_name} must be None or a non-empty string"
                    )
                object.__setattr__(self, field_name, value.strip())


class VariantStatus(str, Enum):
    """Whether an accelerated variant was found for a planned dependency."""

    SELECTED = "SELECTED"
    """A concrete variant was selected from the variant catalog."""

    NOT_AVAILABLE = "NOT_AVAILABLE"
    """No variant matched; the plan must block (fail-closed)."""


@dataclass(frozen=True, slots=True)
class PlannedAcceleratedDependency:
    """One merged accelerated dependency the plan would add.

    ``distribution`` is canonicalized (PEP 503) and is the merge key:
    one entry per distribution across all concerned products.
    ``specifier`` is the deterministically combined specifier string
    (``", ".join`` of the sorted unique non-None declared specifiers)
    or ``None`` for "any version".  ``extras`` is the sorted
    canonicalized union of declared extras.  ``declaring_products``
    lists the sorted product ids that declared this dependency.
    ``variant`` is the selected :class:`AcceleratedVariant` or ``None``
    when no variant matched; ``variant_status`` must be ``SELECTED``
    iff ``variant`` is not ``None``.
    """

    distribution: str
    specifier: str | None
    extras: tuple[str, ...]
    declaring_products: tuple[str, ...]
    variant: AcceleratedVariant | None
    variant_status: VariantStatus

    def __post_init__(self) -> None:
        if not isinstance(self.distribution, str) or not self.distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        object.__setattr__(
            self, "distribution", canonicalize_name(self.distribution.strip())
        )

        specifier = self.specifier
        if specifier is not None:
            if not isinstance(specifier, str) or not specifier.strip():
                raise ValueError(
                    "specifier must be None or a non-empty string"
                )
            try:
                SpecifierSet(specifier)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"invalid specifier {specifier!r}: {exc}"
                ) from exc
            object.__setattr__(self, "specifier", specifier.strip())

        canon_extras: list[str] = []
        seen_extras: set[str] = set()
        for raw_extra in self.extras:
            if not isinstance(raw_extra, str) or not raw_extra.strip():
                raise ValueError("extras must not contain empty values")
            extra = canonicalize_name(raw_extra.strip())
            if extra in seen_extras:
                raise ValueError(f"duplicate extra: {extra}")
            seen_extras.add(extra)
            canon_extras.append(extra)
        object.__setattr__(self, "extras", tuple(sorted(canon_extras)))

        declaring: list[str] = []
        seen_products: set[str] = set()
        for raw_product in self.declaring_products:
            if not isinstance(raw_product, str) or not raw_product.strip():
                raise ValueError(
                    "declaring_products must not contain empty values"
                )
            product_id = raw_product.strip()
            if product_id in seen_products:
                raise ValueError(f"duplicate declaring product: {product_id}")
            seen_products.add(product_id)
            declaring.append(product_id)
        if not declaring:
            raise ValueError("declaring_products must not be empty")
        object.__setattr__(self, "declaring_products", tuple(sorted(declaring)))

        variant = self.variant
        if variant is not None and not isinstance(variant, AcceleratedVariant):
            raise ValueError(
                "variant must be None or an AcceleratedVariant, "
                f"got {type(variant).__qualname__}"
            )
        if not isinstance(self.variant_status, VariantStatus):
            raise ValueError(
                "variant_status must be a VariantStatus, "
                f"got {type(self.variant_status).__qualname__}"
            )
        expected = (
            VariantStatus.SELECTED
            if variant is not None
            else VariantStatus.NOT_AVAILABLE
        )
        if self.variant_status is not expected:
            raise ValueError(
                "variant_status must be SELECTED iff variant is not None; "
                f"got {self.variant_status.value} with variant "
                f"{'set' if variant is not None else 'None'}"
            )


class AcceleratedPlanStatus(str, Enum):
    """Overall status of an accelerated deployment plan."""

    NO_ACCELERATED_REQUIREMENTS = "NO_ACCELERATED_REQUIREMENTS"
    """No product declares accelerated requirements."""

    PLAN_READY = "PLAN_READY"
    """A complete accelerated closure can be planned."""

    BLOCKED = "BLOCKED"
    """The accelerated closure cannot be planned (fail-closed)."""

    UNKNOWN = "UNKNOWN"
    """Host evidence is insufficient to decide (fail-closed)."""


@dataclass(frozen=True, slots=True)
class AcceleratedDeploymentPlan:
    """Read-only description of the accelerated deployment to perform.

    ``hardware`` carries the full compatibility evaluation result.
    ``backend`` is the single accelerator backend the plan is for, or
    ``None`` when no accelerated closure is planned.  ``products_concerned``
    lists the sorted product ids declaring accelerated requirements.
    ``keep_products`` documents the exact KEEP products (sorted by
    product id) that must be preserved verbatim.  ``added_requirements``
    lists the merged accelerated dependencies (sorted by distribution).
    ``source_*`` fields snapshot the runtime status the plan was built
    from.  ``target_runtime`` is a descriptive label only — no mutation.
    ``blocked`` / ``blocked_reason`` carry the fail-closed verdict.
    ``closure_impact`` is the deterministic, human-readable list of what
    would change; it may be empty.
    """

    status: AcceleratedPlanStatus
    hardware: HardwareCompatibility
    backend: str | None
    products_concerned: tuple[str, ...]
    keep_products: tuple[PlannedKeepProduct, ...]
    added_requirements: tuple[PlannedAcceleratedDependency, ...]
    source_runtime_state: str
    source_active_slot_id: str | None
    source_previous_slot_id: str | None
    target_runtime: str
    blocked: bool
    blocked_reason: str | None
    closure_impact: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, AcceleratedPlanStatus):
            raise ValueError(
                "status must be an AcceleratedPlanStatus, "
                f"got {type(self.status).__qualname__}"
            )
        if not isinstance(self.hardware, HardwareCompatibility):
            raise ValueError(
                "hardware must be a HardwareCompatibility, "
                f"got {type(self.hardware).__qualname__}"
            )

        backend = self.backend
        if backend is not None:
            if not isinstance(backend, str) or not backend.strip():
                raise ValueError("backend must be None or a non-empty string")
            object.__setattr__(self, "backend", backend.strip())

        concerned: list[str] = []
        for raw_product in self.products_concerned:
            if not isinstance(raw_product, str) or not raw_product.strip():
                raise ValueError(
                    "products_concerned must not contain empty values"
                )
            concerned.append(raw_product.strip())
        object.__setattr__(self, "products_concerned", tuple(sorted(concerned)))

        keeps = tuple(self.keep_products)
        for keep in keeps:
            if not isinstance(keep, PlannedKeepProduct):
                raise ValueError(
                    "keep_products must contain PlannedKeepProduct values, "
                    f"got {type(keep).__qualname__}"
                )
        object.__setattr__(
            self,
            "keep_products",
            tuple(sorted(keeps, key=lambda keep: keep.product_id)),
        )

        added = tuple(self.added_requirements)
        for entry in added:
            if not isinstance(entry, PlannedAcceleratedDependency):
                raise ValueError(
                    "added_requirements must contain "
                    "PlannedAcceleratedDependency values, "
                    f"got {type(entry).__qualname__}"
                )
        object.__setattr__(
            self,
            "added_requirements",
            tuple(sorted(added, key=lambda entry: entry.distribution)),
        )

        if (
            not isinstance(self.source_runtime_state, str)
            or not self.source_runtime_state.strip()
        ):
            raise ValueError("source_runtime_state must be a non-empty string")
        object.__setattr__(
            self, "source_runtime_state", self.source_runtime_state.strip()
        )

        for field_name in ("source_active_slot_id", "source_previous_slot_id"):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{field_name} must be None or a non-empty string"
                    )
                object.__setattr__(self, field_name, value.strip())

        if not isinstance(self.target_runtime, str) or not self.target_runtime.strip():
            raise ValueError("target_runtime must be a non-empty string")
        object.__setattr__(self, "target_runtime", self.target_runtime.strip())

        if not isinstance(self.blocked, bool):
            raise ValueError("blocked must be a bool")

        blocked_reason = self.blocked_reason
        if blocked_reason is not None:
            if not isinstance(blocked_reason, str) or not blocked_reason.strip():
                raise ValueError(
                    "blocked_reason must be None or a non-empty string"
                )
            object.__setattr__(self, "blocked_reason", blocked_reason.strip())

        impact: list[str] = []
        for line in self.closure_impact:
            if not isinstance(line, str) or not line.strip():
                raise ValueError(
                    "closure_impact must not contain empty lines"
                )
            impact.append(line.strip())
        object.__setattr__(self, "closure_impact", tuple(impact))


def _runtime_state_string(state: RuntimeState | str) -> str:
    """Return the plain string value of a runtime state.

    Accepts a :class:`~zealfie.runtime.model.RuntimeState` member (its
    ``.value`` is used) or a plain non-empty string.
    """
    if isinstance(state, Enum):
        value = str(state.value)
    elif isinstance(state, str):
        value = state
    else:
        raise ValueError(
            "runtime status state must be a RuntimeState or string, "
            f"got {type(state).__qualname__}"
        )
    if not value.strip():
        raise ValueError("runtime status state must not be empty")
    return value.strip()


def _sorted_keep_products(
    keep_products: Mapping[str, PlannedKeepProduct],
) -> tuple[PlannedKeepProduct, ...]:
    """Return KEEP products verbatim, sorted deterministically.

    Values are never modified; the sort key starts with ``product_id``
    and uses the remaining fields only as deterministic tie-breakers.
    """
    keeps = tuple(keep_products.values())
    for keep in keeps:
        if not isinstance(keep, PlannedKeepProduct):
            raise ValueError(
                "keep_products must map product ids to PlannedKeepProduct "
                f"values, got {type(keep).__qualname__}"
            )
    return tuple(
        sorted(
            keeps,
            key=lambda keep: (
                keep.product_id,
                keep.version,
                keep.commit_sha or "",
                keep.wheel_sha256 or "",
            ),
        )
    )


def build_accelerated_deployment_plan(
    *,
    catalog: ProductCatalog,
    capabilities: HostCapabilities,
    recommendation: AccelerationRecommendation,
    runtime_status: RuntimeStatus,
    variant_catalog: AcceleratedVariantCatalog,
    keep_products: Mapping[str, PlannedKeepProduct],
    platform_tag: str,
) -> AcceleratedDeploymentPlan:
    """Build the read-only accelerated deployment plan (pure).

    Rules are applied deterministically, in this order (fail-closed):

    1. collect ``requirements_map`` from the catalog (products whose
       ``acceleration`` is not ``None``); ``products_concerned`` is its
       sorted keys and KEEP products are always documented;
    2. evaluate hardware compatibility
       (:func:`~zealfie.acceleration.compatibility.evaluate_acceleration_compatibility`);
    3. empty ``requirements_map`` → ``NO_ACCELERATED_REQUIREMENTS``,
       blocked, CPU closure preserved unchanged;
    4. hardware ``UNKNOWN`` / ``BLOCKED`` → ``UNKNOWN`` / ``BLOCKED``
       with the hardware reason, no added requirements;
    5. hardware ``SUPPORTED`` → derive the single backend from the
       sorted set of declared backends (multiple backends → blocked,
       "conflicting acceleration backends"); merge requirements into
       one entry per distribution and look each distribution up in
       *variant_catalog* with *backend* and *platform_tag*;
    6. any missing variant → ``BLOCKED`` listing the missing
       distributions (no partial fallback); otherwise ``PLAN_READY``
       with the deterministic closure impact lines.

    The planner never mutates its inputs and never performs I/O.
    ``keep_products`` values are documented verbatim — provenance is
    supplied by the caller and never re-resolved here.
    """
    if not isinstance(platform_tag, str) or not platform_tag.strip():
        raise ValueError("platform_tag must be a non-empty string")
    platform_tag = platform_tag.strip()

    requirements_map: dict[str, ProductAccelerationRequirements] = {}
    for desc in catalog.list():
        if desc.acceleration is not None:
            requirements_map[desc.product_id] = desc.acceleration
    products_concerned = tuple(sorted(requirements_map))

    kept = _sorted_keep_products(keep_products)

    hardware = evaluate_acceleration_compatibility(
        requirements_map, capabilities, recommendation
    )

    base = {
        "hardware": hardware,
        "products_concerned": products_concerned,
        "keep_products": kept,
        "source_runtime_state": _runtime_state_string(runtime_status.state),
        "source_active_slot_id": runtime_status.active_slot_id,
        "source_previous_slot_id": runtime_status.previous_slot_id,
    }

    if not requirements_map:
        return AcceleratedDeploymentPlan(
            status=AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS,
            backend=None,
            added_requirements=(),
            target_runtime="no new runtime required",
            blocked=True,
            blocked_reason=(
                "no product declares accelerated requirements; the active "
                "CPU closure is preserved unchanged"
            ),
            closure_impact=(
                "No accelerated requirements declared — active shared "
                "runtime is preserved as-is.",
            ),
            **base,
        )

    if hardware.status is not HardwareCompatibilityStatus.SUPPORTED:
        status = (
            AcceleratedPlanStatus.UNKNOWN
            if hardware.status is HardwareCompatibilityStatus.UNKNOWN
            else AcceleratedPlanStatus.BLOCKED
        )
        return AcceleratedDeploymentPlan(
            status=status,
            backend=None,
            added_requirements=(),
            target_runtime="no new runtime required",
            blocked=True,
            blocked_reason=hardware.reason,
            closure_impact=(),
            **base,
        )

    declared_backends = sorted({req.backend for req in requirements_map.values()})
    if len(declared_backends) != 1:
        return AcceleratedDeploymentPlan(
            status=AcceleratedPlanStatus.BLOCKED,
            backend=None,
            added_requirements=(),
            target_runtime="no new runtime required",
            blocked=True,
            blocked_reason="conflicting acceleration backends",
            closure_impact=(),
            **base,
        )
    backend = declared_backends[0]

    # Merge requirements across products into one entry per distribution.
    specifiers: dict[str, set[str]] = {}
    extras: dict[str, set[str]] = {}
    declarers: dict[str, set[str]] = {}
    for product_id in products_concerned:
        for req in requirements_map[product_id].requirements:
            specifiers.setdefault(req.distribution, set())
            extras.setdefault(req.distribution, set())
            declarers.setdefault(req.distribution, set())
            if req.specifier is not None:
                specifiers[req.distribution].add(req.specifier)
            extras[req.distribution].update(req.extras)
            declarers[req.distribution].add(product_id)

    planned: list[PlannedAcceleratedDependency] = []
    missing: list[str] = []
    for distribution in sorted(specifiers):
        combined_specifiers = sorted(specifiers[distribution])
        combined_specifier = (
            ", ".join(combined_specifiers) if combined_specifiers else None
        )
        variant = variant_catalog.find_variant(
            distribution, backend, platform_tag
        )
        planned.append(
            PlannedAcceleratedDependency(
                distribution=distribution,
                specifier=combined_specifier,
                extras=tuple(sorted(extras[distribution])),
                declaring_products=tuple(sorted(declarers[distribution])),
                variant=variant,
                variant_status=(
                    VariantStatus.SELECTED
                    if variant is not None
                    else VariantStatus.NOT_AVAILABLE
                ),
            )
        )
        if variant is None:
            missing.append(distribution)

    if missing:
        return AcceleratedDeploymentPlan(
            status=AcceleratedPlanStatus.BLOCKED,
            backend=backend,
            added_requirements=tuple(planned),
            target_runtime="no new runtime required",
            blocked=True,
            blocked_reason=(
                "no accelerated variant available for: "
                + ", ".join(sorted(missing))
            ),
            closure_impact=(),
            **base,
        )

    impact_lines: list[str] = []
    if kept:
        impact_lines.append(
            f"Preserve {len(kept)} installed product(s): "
            + ", ".join(keep.product_id for keep in kept)
        )
    for entry in planned:
        # All variants are SELECTED here: a missing variant returned
        # early with a BLOCKED plan.
        version_label = (
            entry.specifier if entry.specifier is not None else "any version"
        )
        assert entry.variant is not None
        impact_lines.append(
            f"Add {entry.distribution} ({version_label}) "
            f"[variant {entry.variant.version}]"
        )

    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        backend=backend,
        added_requirements=tuple(planned),
        target_runtime=f"new shared runtime slot with accelerated {backend} closure",
        blocked=False,
        blocked_reason=None,
        closure_impact=tuple(impact_lines),
        **base,
    )
