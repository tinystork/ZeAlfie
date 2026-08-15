"""GUI tests for M1-2H GPU plan preview presentation and panel wiring.

Pure presentation mapping tests (no Qt) cover all four
``AcceleratedPlanStatus`` values; the QApplication-based panel tests
verify that ``Configurer le GPU`` renders the plan preview honestly and
never crashes (including when the service lacks the new method).
"""

from __future__ import annotations

import os

import pytest

from zealfie.acceleration import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    HardwareCompatibility,
    HardwareCompatibilityStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
)
from zealfie.gui.presentation import gpu_plan_preview_lines

try:
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)

needs_qt = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

SHA_A = "a" * 40
WHEEL_A = "f" * 64


# ===========================================================================
# Helpers — synthetic plans, never real hardware
# ===========================================================================


def _hardware(
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


def _make_plan(
    status: AcceleratedPlanStatus,
    **overrides,
) -> AcceleratedDeploymentPlan:
    defaults: dict = dict(
        status=status,
        hardware=_hardware(
            HardwareCompatibilityStatus.BLOCKED, "nothing to evaluate"
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


def _ready_plan() -> AcceleratedDeploymentPlan:
    return _make_plan(
        AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(
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


# ===========================================================================
# 1) Pure presentation mapping (no Qt)
# ===========================================================================


def test_preview_no_accelerated_requirements():
    plan = _make_plan(
        AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS,
        blocked=True,
        blocked_reason=(
            "no product declares accelerated requirements; the active CPU "
            "closure is preserved unchanged"
        ),
        closure_impact=(
            "No accelerated requirements declared — active shared runtime "
            "is preserved as-is.",
        ),
    )
    lines = gpu_plan_preview_lines(plan)
    assert lines == (
        "No product declares GPU acceleration requirements.",
        "The CPU deployment closure is preserved unchanged.",
    )


def test_preview_unknown_honest():
    plan = _make_plan(
        AcceleratedPlanStatus.UNKNOWN,
        hardware=_hardware(
            HardwareCompatibilityStatus.UNKNOWN,
            "host capability observation is partial",
        ),
        blocked=True,
        blocked_reason="host capability observation is partial",
    )
    lines = gpu_plan_preview_lines(plan)
    assert any("could not be determined" in line for line in lines)
    assert not any("UNKNOWN" in line for line in lines)


def test_preview_blocked_includes_reason():
    plan = _make_plan(
        AcceleratedPlanStatus.BLOCKED,
        blocked=True,
        blocked_reason="nvidia driver too old",
    )
    lines = gpu_plan_preview_lines(plan)
    assert any("blocked" in line.lower() for line in lines)
    assert "nvidia driver too old" in "\n".join(lines)


def test_preview_plan_ready_honest_detail():
    lines = gpu_plan_preview_lines(_ready_plan())
    text = "\n".join(lines)
    assert "Hardware: SUPPORTED" in text
    assert "Backend: NVIDIA_CUDA" in text
    assert "Products concerned: zebench" in text
    assert f"Keep zebench 2.0.0 (commit {SHA_A})" in text
    assert "Planned actions:" in text
    assert "Add accelerated-lib (>=1.0) [variant 1.2.0]" in text
    assert "No changes have been made yet." in text
    # Honest detail only: never claims an install happened.
    assert "installed" not in text.lower()


def test_preview_plan_ready_without_keep_products():
    plan = _make_plan(
        AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(
            HardwareCompatibilityStatus.SUPPORTED, "compatible"
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
        closure_impact=(),
    )
    lines = gpu_plan_preview_lines(plan)
    text = "\n".join(lines)
    assert "Products concerned: zebench" in text
    assert "Planned actions: none recorded" in text
    assert "No changes have been made yet." in text


# ===========================================================================
# 2) Panel widget (headless)
# ===========================================================================


@pytest.fixture(scope="session")
def qapp():
    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _rec(status=RecommendationStatus.OFFER_SETUP) -> AccelerationRecommendation:
    gpu = GpuInfo(
        vendor="NVIDIA",
        model="GeForce RTX 4090",
        kind=GpuKind.DISCRETE,
        hardware_present=True,
        driver_status=CapabilityStatus.AVAILABLE,
        driver_version="560.35.03",
        driver_reason_code=None,
        driver_reason=None,
        nvidia_smi_available=True,
        cuda_driver_present=True,
    )
    return AccelerationRecommendation(
        status=status,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="offer",
        gpus=(gpu,),
    )


class _FakeGpuPlanService:
    """Panel service with intent + plan preview (plan optional)."""

    def __init__(self, plan=None, plan_raises=None) -> None:
        self._plan = plan
        self._plan_raises = plan_raises
        self.plan_calls = 0
        self.plan_recommendations = []

    def prepare_gpu_setup_intent(self, recommendation=None):
        from zealfie.host.models import GpuSetupIntent

        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=True,
            message="GPU setup prepared, but no CUDA toolkit was installed.",
        )

    def build_accelerated_deployment_plan(self, *, recommendation=None):
        self.plan_calls += 1
        self.plan_recommendations.append(recommendation)
        if self._plan_raises is not None:
            raise self._plan_raises
        return self._plan


class _FakeNoPlanService:
    """Panel service without the M1-2H plan method (older service)."""

    def prepare_gpu_setup_intent(self, recommendation=None):
        from zealfie.host.models import GpuSetupIntent

        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=True,
            message="GPU setup prepared, but no CUDA toolkit was installed.",
        )


@needs_qt
def test_panel_click_shows_plan_preview_lines(qapp):
    from zealfie.gui.acceleration_panel import AccelerationPanel

    rec = _rec()
    service = _FakeGpuPlanService(plan=_ready_plan())
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(rec)
        panel._button.click()
        assert service.plan_calls == 1
        # The displayed recommendation is passed through — no re-probe.
        assert service.plan_recommendations == [rec]
        assert service.plan_recommendations[0] is rec
        text = panel._detail_label.text()
        assert "no CUDA toolkit was installed" in text
        assert "Backend: NVIDIA_CUDA" in text
        assert "No changes have been made yet." in text
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


@needs_qt
def test_panel_without_plan_method_is_graceful(qapp):
    from zealfie.gui.acceleration_panel import AccelerationPanel

    service = _FakeNoPlanService()
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(_rec())
        panel._button.click()
        text = panel._detail_label.text()
        # Existing intent behaviour preserved, no plan preview, no crash.
        assert "no CUDA toolkit was installed" in text
        assert "No changes have been made" not in text
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


@needs_qt
def test_panel_survives_plan_build_error(qapp):
    from zealfie.gui.acceleration_panel import AccelerationPanel

    service = _FakeGpuPlanService(plan_raises=RuntimeError("planner exploded"))
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(_rec())
        panel._button.click()
        text = panel._detail_label.text()
        assert "no CUDA toolkit was installed" in text
        assert "GPU plan preview unavailable" in text
        assert "Traceback" not in text
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()

# ===========================================================================
# 3) Single-observation preview: stored capabilities + recommendation
# ===========================================================================


def _caps() -> HostCapabilities:
    """A synthetic complete host observation (never real hardware)."""
    return HostCapabilities(
        os_name="linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(),
        partial=False,
    )


class _FakeCapabilityAwareGpuPlanService:
    """Panel service that records plan-builder kwargs and counts probes."""

    def __init__(self, plan=None) -> None:
        self._plan = plan
        self.plan_kwargs = []
        self.collect_calls = 0

    def prepare_gpu_setup_intent(self, recommendation=None):
        from zealfie.host.models import GpuSetupIntent

        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=True,
            message="GPU setup prepared, but no CUDA toolkit was installed.",
        )

    def collect_host_capabilities(self):
        self.collect_calls += 1
        return _caps()

    def build_accelerated_deployment_plan(self, **kwargs):
        self.plan_kwargs.append(kwargs)
        return self._plan


@needs_qt
def test_panel_passes_stored_capabilities_to_plan_builder(qapp):
    """When both capabilities and recommendation are stored, the click
    passes both kwargs to the plan builder — no second probe."""
    from zealfie.gui.acceleration_panel import AccelerationPanel

    rec = _rec()
    caps = _caps()
    service = _FakeCapabilityAwareGpuPlanService(plan=_ready_plan())
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(rec, capabilities=caps)
        panel._button.click()
        assert service.plan_kwargs == [
            {"capabilities": caps, "recommendation": rec}
        ]
        assert service.plan_kwargs[0]["capabilities"] is caps
        assert service.plan_kwargs[0]["recommendation"] is rec
        # The panel never invoked the service capability collector.
        assert service.collect_calls == 0
        assert "Backend: NVIDIA_CUDA" in panel._detail_label.text()
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


@needs_qt
def test_plan_builder_with_both_kwargs_does_not_reprobe_capabilities(qapp):
    """A builder called with both stored kwargs never triggers a second
    hardware observation (counting fake collector stays at zero)."""
    from zealfie.gui.acceleration_panel import AccelerationPanel

    rec = _rec()
    caps = _caps()
    service = _FakeCapabilityAwareGpuPlanService(plan=_ready_plan())
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(rec, capabilities=caps)
        panel._button.click()
        assert service.collect_calls == 0
        assert len(service.plan_kwargs) == 1
        assert service.plan_kwargs[0] == {
            "capabilities": caps,
            "recommendation": rec,
        }
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


@needs_qt
def test_panel_without_stored_capabilities_falls_back_to_recommendation_only(
    qapp,
):
    """Without a stored observation the builder is called
    recommendation-only (documented fallback)."""
    from zealfie.gui.acceleration_panel import AccelerationPanel

    rec = _rec()
    service = _FakeCapabilityAwareGpuPlanService(plan=_ready_plan())
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(rec)
        panel._button.click()
        assert service.plan_kwargs == [{"recommendation": rec}]
        assert service.collect_calls == 0
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()
