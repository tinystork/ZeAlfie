"""GUI tests for M1-4 LOT B — bounded/scrollable GPU detail area.

The GPU plan preview must stay inspectable without letting a long
multi-line preview expand the whole acceleration panel (and therefore the
product shell) indefinitely: the detail label lives inside a
height-bounded, scrollable container so the product cards below always
stay reachable and the user can cancel/ignore GPU configuration.

Hermetic: fake services only, offscreen Qt, no GPU, no network, no real
install.  Reuses the exact session-scoped offscreen QApplication pattern
used by the other GUI test modules (never invents a new one).
"""

from __future__ import annotations

import os

import pytest

from zealfie.acceleration import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    HardwareCompatibility,
    HardwareCompatibilityStatus,
    PlannedKeepProduct,
)
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostReasonCode,
    RecommendationStatus,
)

try:
    from PySide6.QtWidgets import (
        QAbstractScrollArea,
        QApplication,
        QScrollArea,
        QWidget,
        QVBoxLayout,
    )
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

SHA_A = "a" * 40
WHEEL_A = "f" * 64


# ===========================================================================
# Synthetic data — never real hardware
# ===========================================================================


def _hardware(status: HardwareCompatibilityStatus, reason: str) -> HardwareCompatibility:
    return HardwareCompatibility(
        status=status,
        reason_code="COMPATIBLE"
        if status is HardwareCompatibilityStatus.SUPPORTED
        else "ACCELERATION_BLOCKED",
        reason=reason,
        products_concerned=(),
    )


def _make_plan(status: AcceleratedPlanStatus, **overrides) -> AcceleratedDeploymentPlan:
    defaults: dict = dict(
        status=status,
        hardware=_hardware(HardwareCompatibilityStatus.BLOCKED, "nothing to evaluate"),
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
            HardwareCompatibilityStatus.SUPPORTED, "host acceleration is compatible"
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
        target_runtime="new shared runtime slot with accelerated NVIDIA_CUDA closure",
        closure_impact=("Add accelerated-lib (>=1.0) [variant 1.2.0]",),
    )


def _blocked_plan() -> AcceleratedDeploymentPlan:
    return _make_plan(
        AcceleratedPlanStatus.BLOCKED,
        blocked=True,
        blocked_reason="nvidia driver too old",
    )


def _rec() -> AccelerationRecommendation:
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
        status=RecommendationStatus.OFFER_SETUP,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="offer",
        gpus=(gpu,),
    )


class _FakeGpuPlanService:
    """Panel service exposing the intent + read-only plan preview."""

    def __init__(self, plan=None) -> None:
        self._plan = plan

    def prepare_gpu_setup_intent(self, recommendation=None):
        from zealfie.host.models import GpuSetupIntent

        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=True,
            message="GPU setup prepared, but no CUDA toolkit was installed.",
        )

    def build_accelerated_deployment_plan(self, *, recommendation=None):
        return self._plan


def _long_detail_text(lines: int = 80) -> str:
    """A long, multi-line preview (well beyond the 180 px bound)."""
    return "\n".join(
        f"Line {i:03d}: host prerequisite and closure detail for the "
        "accelerated NVIDIA_CUDA runtime deployment plan"
        for i in range(lines)
    )


def _embed(panel, width: int, height: int = 500) -> QWidget:
    """Mirror the main window: panel above a widgetResizable scroll area."""
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(10)
    layout.addWidget(panel)
    cards = QWidget()
    cards_layout = QVBoxLayout(cards)
    cards_layout.setContentsMargins(0, 0, 0, 0)
    cards_layout.addStretch()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(cards)
    layout.addWidget(scroll)
    host.resize(width, height)
    return host


# ===========================================================================
# Tests
# ===========================================================================


@pytest.fixture(scope="session")
def qapp():
    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class TestGpuDetailBoundedLayout:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    # 1) long GPU preview bounded in layout --------------------------------

    def test_detail_container_is_bounded_scroll_area(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_ready_plan()))
        try:
            scroll = panel._detail_scroll
            assert scroll is not None
            assert isinstance(scroll, QScrollArea)
            assert isinstance(scroll, QAbstractScrollArea)
            assert scroll.objectName() == "gpuDetailScrollArea"
            # Bounded: a positive, finite maximum height.
            assert scroll.maximumHeight() > 0
            assert scroll.maximumHeight() == 180
            # Scrollable content is resized to the viewport width.
            assert scroll.widgetResizable() is True
            # Hidden until a detail is shown.
            assert scroll.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_long_preview_does_not_exceed_max_height(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_ready_plan()))
        host = _embed(panel, width=520)
        try:
            host.show()
            qapp.processEvents()
            panel._show_detail(_long_detail_text())
            qapp.processEvents()
            panel.layout().activate()
            host.layout().activate()
            qapp.processEvents()

            scroll = panel._detail_scroll
            assert scroll.isHidden() is False
            # The rendered container never exceeds its bound.
            assert scroll.height() <= scroll.maximumHeight() + 2, (
                f"detail scroll area {scroll.height()}px exceeds its "
                f"maximum {scroll.maximumHeight()}px"
            )
            # The full text is still inspectable via scrolling (the label
            # keeps its full wrapped height, never truncated).
            label = panel._detail_label
            assert label.height() > scroll.viewport().height(), (
                "long preview should actually scroll (label taller than viewport)"
            )
        finally:
            host.close()
            host.deleteLater()
            qapp.processEvents()

    # 2) products remain accessible ---------------------------------------

    def test_panel_size_hint_stays_bounded_with_long_preview(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_ready_plan()))
        try:
            panel.set_recommendation(_rec())
            panel.show()
            qapp.processEvents()
            panel.layout().activate()
            compact = panel.sizeHint().height()

            panel._show_detail(_long_detail_text())
            qapp.processEvents()
            panel.layout().activate()

            grown = panel.sizeHint().height()
            # The detail area is bounded: the panel grows by at most the
            # scroll container's height (+ spacing) — never unbounded.
            assert grown < compact + 260, (
                f"panel size hint grew unboundedly: compact={compact} grown={grown}"
            )
            assert panel._detail_scroll.maximumHeight() > 0
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    # 3) install button visible when PLAN_READY ---------------------------

    def test_install_button_visible_when_plan_ready(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_ready_plan()))
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            assert panel._install_button.isHidden() is False
            assert "Installer" in panel._install_button.text()
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    # 4) no install button for fail-closed plan ---------------------------

    def test_no_install_button_for_fail_closed_plan(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_blocked_plan()))
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            assert panel._install_button.isHidden() is True
            # The honest blocked reason is still inspectable (not dropped).
            assert "driver too old" in panel._detail_label.text()
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    # 5) cancel / no-install leaves shell usable --------------------------

    def test_ignore_after_configure_leaves_panel_usable(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_ready_plan()))
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            # Preview shown, Installer offered — but the user can ignore it.
            assert panel._install_button.isHidden() is False
            assert panel._detail_scroll.isHidden() is False

            # The configure button and summary remain available (no stuck
            # expanded state), and the detail area stays bounded.
            assert panel._button.isHidden() is False
            assert panel._button.isEnabled() is True
            assert panel._summary_label.text() != ""
            assert panel._detail_scroll.height() <= panel._detail_scroll.maximumHeight() + 2

            # A fresh observation resets the previewed plan (no stuck offer).
            panel.set_recommendation(_rec())
            assert panel._install_button.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    # 6) resize behaviour --------------------------------------------------

    def test_resize_keeps_detail_bounded_at_two_widths(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakeGpuPlanService(plan=_ready_plan()))
        try:
            for width in (400, 700):
                host = _embed(panel, width=width)
                host.show()
                qapp.processEvents()
                panel.set_recommendation(_rec())
                panel._show_detail(_long_detail_text())
                qapp.processEvents()
                panel.layout().activate()
                host.layout().activate()
                qapp.processEvents()

                scroll = panel._detail_scroll
                assert isinstance(scroll, QAbstractScrollArea)
                assert scroll.height() <= scroll.maximumHeight() + 2, (
                    f"width {width}: detail area {scroll.height()}px exceeds "
                    f"bound {scroll.maximumHeight()}px"
                )
                # Mouse-wheel scrolling is structurally available.
                assert scroll.verticalScrollBar() is not None
                host.close()
                host.deleteLater()
                qapp.processEvents()
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()
