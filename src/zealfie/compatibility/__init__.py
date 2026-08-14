"""Product-agnostic interoperability metadata parsing and evaluation.

ZeAlfie owns only *parsing* and *evaluation* of machine-readable
interoperability declarations shipped inside product wheels.  Products own
their behavior; ZeAlfie never imports product code and never hardcodes
product-specific business knowledge.

Typical usage (before activation, e.g. before ``apply_deployment_plan``)::

    from zealfie.compatibility import evaluate_wheels

    report = evaluate_wheels([artifact.path for artifact in prepared_wheels])
    if report.blocked:
        raise SomeDeploymentError(str(report))
"""

from __future__ import annotations

from .evaluator import evaluate_interops, evaluate_wheels
from .model import (
    SCHEMA_V1,
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
from .parser import (
    InteropParseError,
    scan_wheel_interop,
    scan_wheels_interop,
)

__all__ = [
    "AnyOfGroup",
    "CompatibilityFinding",
    "CompatibilityReport",
    "CompatibilityVerdict",
    "ConsumerRequirement",
    "InteropParseError",
    "InteropParseStatus",
    "InteropRecord",
    "ProviderDeclaration",
    "SCHEMA_V1",
    "WheelInterop",
    "evaluate_interops",
    "evaluate_wheels",
    "scan_wheel_interop",
    "scan_wheels_interop",
]
