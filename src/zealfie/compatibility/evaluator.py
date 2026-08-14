"""Product-agnostic interoperability evaluator.

Evaluates the declared consumer requirements of a candidate set of primary
product wheels against the declared provider contracts in the same set.

The evaluator is pure and read-only: it accepts wheel paths (or pre-scanned
:class:`WheelInterop` records), never imports product code, and can be run at
any point before activation (e.g. before ``apply_deployment_plan``).

Linkage between a consumer and its provider uses the normalized provider
distribution name as the canonical key.  ``product_id`` values are purely
diagnostic and are never trusted as identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .model import (
    AnyOfGroup,
    CompatibilityFinding,
    CompatibilityReport,
    CompatibilityVerdict,
    ConsumerRequirement,
    InteropParseStatus,
    InteropRecord,
    ProviderDeclaration,
    WheelInterop,
)
from .parser import scan_wheel_interop


def evaluate_wheels(wheel_paths: Iterable[str | Path]) -> CompatibilityReport:
    """Scan and evaluate a candidate set of primary product wheels."""
    interops = tuple(scan_wheel_interop(p) for p in wheel_paths)
    return evaluate_interops(interops)


def evaluate_interops(interops: Iterable[WheelInterop]) -> CompatibilityReport:
    """Evaluate already-scanned wheel interop records.

    Pure function: no filesystem access, no product imports.
    """
    records = tuple(interops)
    findings: list[CompatibilityFinding] = []

    # --- Presence index: normalized distribution name -> wheel scan. ---------
    present: dict[str, WheelInterop] = {}
    for wi in records:
        if wi.distribution_name:
            # Duplicate distribution names are a separate planning conflict;
            # for evaluation, the first record wins deterministically.
            present.setdefault(wi.distribution_name, wi)

    # --- Valid provider contracts, indexed by normalized distribution name. --
    providers: dict[str, InteropRecord] = {}
    for wi in records:
        if wi.status is InteropParseStatus.VALID and wi.record is not None:
            providers.setdefault(wi.distribution_name, wi.record)

    # --- Set of provider distribution names referenced by any consumer. ------
    referenced: set[str] = set()
    for wi in records:
        if wi.status is InteropParseStatus.VALID and wi.record is not None:
            for req in wi.record.consumes:
                referenced.add(req.provider_distribution_name)

    # --- Non-blocking warning for present-but-unreadable providers that no
    # consumer references.  A provider whose metadata is unavailable is only
    # blocking when some consumer actually declares a requirement for it. -----
    for name, wi in present.items():
        if wi.status is InteropParseStatus.VALID:
            continue
        if name in referenced:
            continue
        findings.append(
            CompatibilityFinding(
                verdict=CompatibilityVerdict.METADATA_UNAVAILABLE,
                code="METADATA_UNAVAILABLE_UNREFERENCED",
                blocking=False,
                provider_distribution=name,
                message=(
                    f"provider distribution {name!r} has no usable interop "
                    f"declaration but is not referenced by any consumer; "
                    f"non-blocking ({wi.reason_code}: {wi.reason})"
                ),
            )
        )

    # --- Evaluate each declared consumer requirement. ------------------------
    for wi in records:
        if wi.status is not InteropParseStatus.VALID or wi.record is None:
            continue
        for req in wi.record.consumes:
            _evaluate_requirement(wi, req, present, providers, findings)

    return CompatibilityReport(
        verdict=_overall_verdict(findings),
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# Internal evaluation helpers
# ---------------------------------------------------------------------------


def _evaluate_requirement(
    consumer_wi: WheelInterop,
    req: ConsumerRequirement,
    present: dict[str, WheelInterop],
    providers: dict[str, InteropRecord],
    findings: list[CompatibilityFinding],
) -> None:
    consumer_dist = consumer_wi.distribution_name
    provider_name = req.provider_distribution_name

    provider_wi = present.get(provider_name)

    # --- Provider absent -----------------------------------------------------
    if provider_wi is None:
        if req.optional:
            findings.append(
                CompatibilityFinding(
                    verdict=CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED,
                    code="OPTIONAL_PROVIDER_ABSENT",
                    blocking=False,
                    consumer_distribution=consumer_dist,
                    provider_distribution=provider_name,
                    message=(
                        f"optional provider {provider_name!r} is absent; "
                        f"consumer {consumer_dist!r} degrades"
                    ),
                )
            )
        else:
            findings.append(
                CompatibilityFinding(
                    verdict=CompatibilityVerdict.INCOMPATIBLE,
                    code="MANDATORY_PROVIDER_ABSENT",
                    blocking=True,
                    consumer_distribution=consumer_dist,
                    provider_distribution=provider_name,
                    message=(
                        f"mandatory provider {provider_name!r} is absent; "
                        f"consumer {consumer_dist!r} requires it"
                    ),
                )
            )
        return

    # --- Provider present but metadata unavailable/invalid -------------------
    if provider_wi.status is not InteropParseStatus.VALID or provider_wi.record is None:
        findings.append(
            CompatibilityFinding(
                verdict=CompatibilityVerdict.METADATA_UNAVAILABLE,
                code="PROVIDER_METADATA_UNAVAILABLE",
                blocking=True,
                consumer_distribution=consumer_dist,
                provider_distribution=provider_name,
                message=(
                    f"provider {provider_name!r} is present but its interop "
                    f"declaration is unavailable/invalid "
                    f"({provider_wi.reason_code}: {provider_wi.reason}); "
                    f"cannot verify compatibility with consumer {consumer_dist!r}"
                ),
            )
        )
        return

    # --- Provider present with valid metadata --------------------------------
    record = provider_wi.record
    matched = [p for p in record.provides if p.api_module == req.api_module]

    if not matched:
        findings.append(
            CompatibilityFinding(
                verdict=CompatibilityVerdict.INCOMPATIBLE,
                code="API_MODULE_MISMATCH",
                blocking=True,
                consumer_distribution=consumer_dist,
                provider_distribution=provider_name,
                message=(
                    f"provider {provider_name!r} does not declare API module "
                    f"{req.api_module!r}"
                ),
            )
        )
        return

    # A requirement is satisfied if *any* matching provides entry satisfies it.
    # A fully satisfying entry (``_evaluate_against_provider`` returns ``None``)
    # wins outright over sibling entries for the same ``api_module`` that are
    # incompatible or merely degraded.  Only when no entry fully satisfies do
    # we fall back to the most severe finding across the non-satisfying matches.
    worst: CompatibilityFinding | None = None
    for prov in matched:
        result = _evaluate_against_provider(req, prov, consumer_dist, provider_name)
        if result is None:
            return
        worst = _worse(worst, result)

    if worst is not None:
        findings.append(worst)


def _evaluate_against_provider(
    req: ConsumerRequirement,
    prov: ProviderDeclaration,
    consumer_dist: str,
    provider_name: str,
) -> CompatibilityFinding | None:
    """Return a finding for one requirement/provides pair, or None if compatible."""
    # API module already matched; now check the version range (PEP 440).
    provider_version = _normalise_api_version(prov.api_version)
    if not _specifier_contains(req.api_version, provider_version):
        return CompatibilityFinding(
            verdict=CompatibilityVerdict.INCOMPATIBLE,
            code="API_VERSION_MISMATCH",
            blocking=True,
            consumer_distribution=consumer_dist,
            provider_distribution=provider_name,
            message=(
                f"provider {provider_name!r} API {prov.api_version!r} "
                f"(normalised {provider_version!r}) does not satisfy "
                f"consumer requirement {req.api_version!r}"
            ),
        )

    provider_caps = set(prov.capabilities)

    # --- Required capabilities (all must be present) -------------------------
    missing_required = [c for c in req.required_capabilities if c not in provider_caps]
    if missing_required:
        return CompatibilityFinding(
            verdict=CompatibilityVerdict.INCOMPATIBLE,
            code="MISSING_REQUIRED_CAPABILITY",
            blocking=True,
            consumer_distribution=consumer_dist,
            provider_distribution=provider_name,
            message=(
                f"provider {provider_name!r} lacks required capability ids: "
                f"{missing_required}"
            ),
        )

    # --- Required any-of groups (each must have at least one capability) -----
    for group in req.any_of_capabilities:
        if group.required and not _any_of_satisfied(group, provider_caps):
            return CompatibilityFinding(
                verdict=CompatibilityVerdict.INCOMPATIBLE,
                code="MISSING_ANY_OF_CAPABILITY",
                blocking=True,
                consumer_distribution=consumer_dist,
                provider_distribution=provider_name,
                message=(
                    f"provider {provider_name!r} satisfies none of the required "
                    f"any-of group {group.id!r} ({sorted(group.capabilities)})"
                ),
            )

    # --- Optional degradation (explicit, never silent) -----------------------
    degraded: list[str] = []

    missing_optional = [c for c in req.optional_capabilities if c not in provider_caps]
    if missing_optional:
        degraded.append(f"optional capabilities missing: {missing_optional}")

    for group in req.any_of_capabilities:
        if not group.required and not _any_of_satisfied(group, provider_caps):
            degraded.append(f"optional any-of group {group.id!r} unsatisfied")

    if degraded:
        return CompatibilityFinding(
            verdict=CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED,
            code="MISSING_OPTIONAL_CAPABILITY",
            blocking=False,
            consumer_distribution=consumer_dist,
            provider_distribution=provider_name,
            message=(
                f"consumer {consumer_dist!r} degrades against provider "
                f"{provider_name!r}: {'; '.join(degraded)}"
            ),
        )

    return None


def _any_of_satisfied(group: AnyOfGroup, provider_caps: set[str]) -> bool:
    return any(c in provider_caps for c in group.capabilities)


def _normalise_api_version(value: str) -> str:
    """Normalize a PEP 440 version to ``major.minor`` (the API contract unit)."""
    v = Version(value)
    return f"{v.major}.{v.minor}"


def _specifier_contains(specifier: str, version: str) -> bool:
    """Return True when *version* satisfies the PEP 440 *specifier*."""
    return SpecifierSet(specifier).contains(Version(version), prereleases=True)


def _worse(
    current: CompatibilityFinding | None,
    candidate: CompatibilityFinding | None,
) -> CompatibilityFinding | None:
    """Keep the most severe of two findings (None ranks least severe)."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    if candidate.blocking and not current.blocking:
        return candidate
    if current.blocking and not candidate.blocking:
        return current
    # Both blocking or both non-blocking: prefer INCOMPATIBLE over METADATA_UNAVAILABLE,
    # and any blocking over degraded.  Deterministic tie-break by code.
    rank = {
        CompatibilityVerdict.INCOMPATIBLE: 0,
        CompatibilityVerdict.METADATA_UNAVAILABLE: 1,
        CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED: 2,
        CompatibilityVerdict.COMPATIBLE: 3,
    }
    return current if rank[current.verdict] <= rank[candidate.verdict] else candidate


def _overall_verdict(findings: list[CompatibilityFinding]) -> CompatibilityVerdict:
    blocking = [f for f in findings if f.blocking]
    if blocking:
        if any(
            f.verdict is CompatibilityVerdict.INCOMPATIBLE for f in blocking
        ):
            return CompatibilityVerdict.INCOMPATIBLE
        return CompatibilityVerdict.METADATA_UNAVAILABLE
    if any(
        f.verdict is CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED for f in findings
    ):
        return CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED
    return CompatibilityVerdict.COMPATIBLE
