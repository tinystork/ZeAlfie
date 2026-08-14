"""Data model for the product-agnostic interoperability evaluator.

This module is intentionally free of any product-specific knowledge:
it models *providers* and *consumers* of versioned, capability-bearing
API contracts, using a Python-distribution name as the only linkage key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Canonical schema identifier for the only supported metadata version.
SCHEMA_V1 = "zesoftware.interop.v1"


class InteropParseStatus(StrEnum):
    """Outcome of scanning a single primary wheel for interop metadata."""

    VALID = "VALID"
    """Wheel carries a well-formed, cross-checked interop declaration."""

    ABSENT = "ABSENT"
    """Wheel carries no interop declaration (metadata unavailable)."""

    INVALID = "INVALID"
    """Wheel carries an interop declaration that failed validation."""


class CompatibilityVerdict(StrEnum):
    """Overall or per-finding compatibility outcome."""

    COMPATIBLE = "COMPATIBLE"
    """All declared requirements are satisfiable with no degradation."""

    COMPATIBLE_WITH_DEGRADED = "COMPATIBLE_WITH_DEGRADED"
    """Compatible, but an optional provider or optional capability is absent."""

    INCOMPATIBLE = "INCOMPATIBLE"
    """A mandatory requirement is known to be unsatisfiable."""

    METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
    """A referenced provider is present but its declaration is unreadable."""


@dataclass(frozen=True, slots=True)
class ProviderDeclaration:
    """A single ``provides`` entry: one API contract exposed by a product."""

    api_module: str
    """Exact public API module name (primary contract identity)."""

    api_version: str
    """PEP 440 version string (e.g. ``"1.0"``)."""

    capabilities: tuple[str, ...]
    """Capability identifiers exposed by this API contract."""


@dataclass(frozen=True, slots=True)
class AnyOfGroup:
    """A named group of capabilities where at least one must be present."""

    id: str
    """Diagnostic group identifier."""

    capabilities: tuple[str, ...]

    required: bool = True
    """When true, absence of every capability is incompatible."""


@dataclass(frozen=True, slots=True)
class ConsumerRequirement:
    """A single ``consumes`` entry: one demand against a provider."""

    provider_distribution_name: str
    """Normalized distribution name of the provider (canonical linkage key)."""

    provider_product_id: str | None
    """Diagnostic product id — never the sole trust key."""

    optional: bool
    """When true, an absent provider degrades rather than blocks."""

    api_module: str
    """Exact API module the consumer requires."""

    api_version: str
    """PEP 440 specifier/range evaluated against the provider's API version."""

    required_capabilities: tuple[str, ...]
    any_of_capabilities: tuple[AnyOfGroup, ...]
    optional_capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InteropRecord:
    """A fully validated interop declaration from a single wheel."""

    distribution_name: str
    """Normalized distribution name (cross-checked against wheel metadata)."""

    product_id: str
    schema: str
    provides: tuple[ProviderDeclaration, ...]
    consumes: tuple[ConsumerRequirement, ...]


@dataclass(frozen=True, slots=True)
class WheelInterop:
    """Result of scanning one primary wheel for interoperability metadata."""

    wheel_path: Path
    distribution_name: str
    """Normalized distribution name read from wheel metadata (``""`` if unreadable)."""

    status: InteropParseStatus
    record: InteropRecord | None = None
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CompatibilityFinding:
    """A single diagnostic produced by the evaluator."""

    verdict: CompatibilityVerdict
    code: str
    """Stable machine-readable reason code."""

    blocking: bool
    """True when this finding must prevent activation."""

    consumer_distribution: str | None = None
    provider_distribution: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """Aggregate result of evaluating a candidate set of primary wheels."""

    verdict: CompatibilityVerdict
    findings: tuple[CompatibilityFinding, ...]

    @property
    def blocked(self) -> bool:
        """True when activation must be refused for this candidate set."""
        return self.verdict in (
            CompatibilityVerdict.INCOMPATIBLE,
            CompatibilityVerdict.METADATA_UNAVAILABLE,
        )
