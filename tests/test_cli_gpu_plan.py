"""CLI tests for ``zealfie system gpu-plan`` (M1-2H).

Read-only preview command: the fake service is injected via
``_make_service`` so no real host probing occurs.  A blocked plan is a
preview, not an error — exit code stays 0.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

import zealfie.cli as cli
from zealfie.acceleration import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    HardwareCompatibility,
    HardwareCompatibilityStatus,
    HostPrerequisiteEntry,
    HostPrerequisites,
    HostPrerequisitesStatus,
    HostPrerequisiteStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
)
from zealfie.app import ZeAlfieService
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.products.catalog import default_catalog
from zealfie.runtime.model import RuntimeState, RuntimeStatus
from zealfie.runtime.provenance import ProductProvenance

SHA_A = "a" * 40
WHEEL_A = "f" * 64


def _caps() -> HostCapabilities:
    return HostCapabilities(
        os_name="Linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(),
        partial=False,
    )


def _recommender(caps) -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=RecommendationStatus.NOT_APPLICABLE,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_NOT_APPLICABLE,
        reason="no accelerator",
    )


class _AbsentRt:
    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            state=RuntimeState.ABSENT,
            runtime_root=Path("/fake"),
        )


class _ActiveProvenanceStore:
    """Fake provenance store marking ``zemosaic`` ACTIVE (hermetic)."""

    def load_active(self):
        return {
            "zemosaic": ProductProvenance(
                product_id="zemosaic",
                version="1.0.0",
                source_owner="zealfie",
                source_repo="ZeMosaic",
                requested_ref="main",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            )
        }


def _make_plan(status: AcceleratedPlanStatus, **overrides) -> AcceleratedDeploymentPlan:
    defaults: dict = dict(
        status=status,
        hardware=_hardware_compatibility(
            HardwareCompatibilityStatus.BLOCKED,
            "nothing to evaluate",
        ),
        backend=None,
        products_concerned=(),
        keep_products=(),
        added_requirements=(),
        source_runtime_state="READY",
        source_active_slot_id=None,
        source_previous_slot_id=None,
        target_runtime="no new runtime required",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )
    defaults.update(overrides)
    return AcceleratedDeploymentPlan(**defaults)


def _hardware_compatibility(
    status: HardwareCompatibilityStatus,
    reason: str,
) -> HardwareCompatibility:
    reason_codes = {
        HardwareCompatibilityStatus.SUPPORTED: "COMPATIBLE",
        HardwareCompatibilityStatus.BLOCKED: "ACCELERATION_BLOCKED",
        HardwareCompatibilityStatus.UNKNOWN: "HOST_CAPABILITIES_PARTIAL",
    }
    return HardwareCompatibility(
        status=status,
        reason_code=reason_codes[status],
        reason=reason,
        products_concerned=(),
    )


def _ready_plan() -> AcceleratedDeploymentPlan:
    return _make_plan(
        AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware_compatibility(
            HardwareCompatibilityStatus.SUPPORTED,
            "host acceleration is compatible with all declared product "
            "requirements",
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
        keep_products=(
            PlannedKeepProduct(
                product_id="zebench",
                version="2.0.0",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            ),
        ),
        added_requirements=(
            PlannedAcceleratedDependency(
                distribution="accelerated-lib",
                specifier=">=1.0",
                extras=(),
                declaring_products=("zebench",),
                variant=AcceleratedVariant(
                    distribution="accelerated-lib",
                    version="1.2.0",
                    backend="NVIDIA_CUDA",
                    platform="linux_x86_64",
                ),
                variant_status=VariantStatus.SELECTED,
            ),
        ),
        target_runtime="new shared runtime slot with accelerated "
        "NVIDIA_CUDA closure",
        closure_impact=("Add accelerated-lib (>=1.0) [variant 1.2.0]",),
    )


def _blocked_plan() -> AcceleratedDeploymentPlan:
    return _make_plan(
        AcceleratedPlanStatus.BLOCKED,
        hardware=_hardware_compatibility(
            HardwareCompatibilityStatus.BLOCKED,
            "nvidia driver too old",
        ),
        backend="NVIDIA_CUDA",
        blocked=True,
        blocked_reason="nvidia driver too old",
    )


class _FakePlanService:
    """Fake service returning a canned plan (never probes hardware)."""

    def __init__(self, plan=None, raises=None) -> None:
        self._plan = plan
        self._raises = raises
        self.calls = 0

    def build_accelerated_deployment_plan(self, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._plan


# ===========================================================================
# Default catalog: honest blocked output (zemosaic contract, no GPU), nothing
# written
# ===========================================================================


def test_gpu_plan_default_catalog_honest_blocked(monkeypatch, tmp_path):
    """With the real default catalog (zemosaic declares acceleration)
    and a NOT_APPLICABLE synthetic recommender, the command prints the
    honest BLOCKED preview naming zemosaic and creates no files."""
    service = ZeAlfieService(
        catalog=default_catalog(),
        runtime=_AbsentRt(),
        capability_collector=_caps,
        recommender=_recommender,
        provenance_store=_ActiveProvenanceStore(),
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    before = sorted(p.name for p in tmp_path.iterdir())
    stdout = StringIO()
    code = cli.run(["system", "gpu-plan"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Accelerated GPU deployment plan:" in output
    assert "Status: BLOCKED" in output
    assert "Products concerned: zemosaic" in output
    assert "no supported accelerator hardware detected" in output
    assert "No changes have been applied" in output
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


# ===========================================================================
# PLAN_READY output
# ===========================================================================


def test_gpu_plan_ready_output_backend_and_variant(monkeypatch):
    service = _FakePlanService(plan=_ready_plan())
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-plan"], stdout=stdout)
    assert code == 0
    assert service.calls == 1
    output = stdout.getvalue()
    assert "Status: PLAN_READY" in output
    assert "Backend: NVIDIA_CUDA" in output
    assert "Products concerned: zebench" in output
    assert "KEEP zebench version 2.0.0 (commit " + SHA_A + ")" in output
    assert (
        "Accelerated dependency: accelerated-lib (>=1.0) "
        "[variant version 1.2.0]" in output
    )
    assert "Add accelerated-lib (>=1.0) [variant 1.2.0]" in output
    assert "No changes have been applied" in output


# ===========================================================================
# Blocked plan is a preview, not an error
# ===========================================================================


def test_gpu_plan_blocked_exits_zero_with_reason(monkeypatch):
    service = _FakePlanService(plan=_blocked_plan())
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-plan"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Status: BLOCKED" in output
    assert "Blocked: nvidia driver too old" in output
    assert "Hardware status: BLOCKED" in output


# ===========================================================================
# Phase F: host prerequisites + closure visible in the preview
# ===========================================================================


def _prereq_ready_plan() -> AcceleratedDeploymentPlan:
    """A PLAN_READY plan carrying the Phase F host prerequisites
    classification (driver OK observed, CC NOT_OBSERVED, two managed
    closure distributions + cost note)."""
    return _make_plan(
        AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware_compatibility(
            HardwareCompatibilityStatus.SUPPORTED,
            "host acceleration is compatible with all declared product "
            "requirements",
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zemosaic",),
        added_requirements=(
            PlannedAcceleratedDependency(
                distribution="cupy-cuda12x",
                specifier=">=14.1.1,<15",
                extras=(),
                declaring_products=("zemosaic",),
                variant=AcceleratedVariant(
                    distribution="cupy-cuda12x",
                    version="14.1.1",
                    backend="NVIDIA_CUDA",
                    platform="linux_x86_64",
                ),
                variant_status=VariantStatus.SELECTED,
            ),
        ),
        target_runtime="new shared runtime slot with accelerated "
        "NVIDIA_CUDA closure",
        closure_impact=("Add cupy-cuda12x (>=14.1.1,<15) [variant 14.1.1]",),
        host_prerequisites=HostPrerequisites(
            status=HostPrerequisitesStatus.OK,
            required_host=(
                HostPrerequisiteEntry(
                    entry="nvidia-driver",
                    requirement=(
                        ">= 550.54.14 (minimum officiel CUDA 12.4)"
                    ),
                    status=HostPrerequisiteStatus.OK,
                    observed="550.163.01",
                ),
                HostPrerequisiteEntry(
                    entry="nvidia-gpu-cc",
                    requirement=(
                        "NVIDIA GPU Compute Capability >= 6.0 (Pascal+)"
                    ),
                    status=HostPrerequisiteStatus.NOT_OBSERVED,
                ),
            ),
            managed_runtime=(
                HostPrerequisiteEntry(
                    entry="cupy-cuda12x",
                    requirement="==14.1.1",
                    status=HostPrerequisiteStatus.MANAGED,
                ),
                HostPrerequisiteEntry(
                    entry="total",
                    requirement="~1.16 Go download / ~1.7 Go installed",
                    status=HostPrerequisiteStatus.MANAGED,
                ),
            ),
        ),
    )


def test_gpu_plan_ready_shows_host_prerequisites(monkeypatch):
    """(d) The PLAN_READY preview shows the host prerequisites
    classification: REQUIRED_HOST entries with observed driver and the
    honest NOT_OBSERVED marker, plus MANAGED_RUNTIME closure entries."""
    service = _FakePlanService(plan=_prereq_ready_plan())
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-plan"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Host prerequisites:" in output
    assert (
        "- REQUIRED_HOST nvidia-driver >= 550.54.14 (minimum officiel "
        "CUDA 12.4) (observed 550.163.01)" in output
    )
    assert (
        "- REQUIRED_HOST nvidia-gpu-cc NVIDIA GPU Compute Capability "
        ">= 6.0 (Pascal+) [not observed]" in output
    )
    assert "- MANAGED_RUNTIME cupy-cuda12x ==14.1.1" in output
    assert (
        "- MANAGED_RUNTIME total ~1.16 Go download / ~1.7 Go installed"
        in output
    )


# ===========================================================================
# Unexpected exception -> non-zero
# ===========================================================================


def test_gpu_plan_unexpected_exception_non_zero(monkeypatch, capsys):
    service = _FakePlanService(raises=RuntimeError("probe exploded"))
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-plan"], stdout=stdout)
    assert code != 0
    assert "gpu plan failed" in capsys.readouterr().err
    assert stdout.getvalue() == ""


# ===========================================================================
# Parser
# ===========================================================================


def test_gpu_plan_in_parser():
    p = cli.build_parser()
    args = p.parse_args(["system", "gpu-plan"])
    assert args.command == "system"
    assert args.system_command == "gpu-plan"
