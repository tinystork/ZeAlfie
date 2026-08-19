"""GUI tests for M1-2G hardware acceleration panel and main-window wiring.

Hermetic: no real host probing, no subprocess.  Runs headless.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.host.models import (
    AccelerationRecommendation,
    GpuInfo,
    GpuKind,
    CapabilityStatus,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)

from zealfie.gui.acceleration_panel import (
    AccelerationPanel,
    configure_button_visible,
    panel_detail,
    panel_summary,
)

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")


# ===========================================================================
# Helpers
# ===========================================================================


def _rec(status, reason="", gpus=()) -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=status,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason=reason,
        gpus=gpus,
    )


def _nvidia_gpu() -> GpuInfo:
    return GpuInfo(
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


# ===========================================================================
# 1) Pure presentation helpers (no Qt)
# ===========================================================================


def test_configure_button_visible_offer_setup_only():
    assert configure_button_visible(_rec(RecommendationStatus.OFFER_SETUP)) is True
    assert configure_button_visible(_rec(RecommendationStatus.BLOCKED)) is False
    assert configure_button_visible(_rec(RecommendationStatus.NOT_APPLICABLE)) is False
    assert configure_button_visible(_rec(RecommendationStatus.UNKNOWN)) is False
    assert configure_button_visible(_rec(RecommendationStatus.ALREADY_READY)) is False
    assert configure_button_visible(None) is False


def test_panel_summary_per_state():
    assert "to configure" in panel_summary(_rec(RecommendationStatus.OFFER_SETUP))
    assert "driver unavailable" in panel_summary(_rec(RecommendationStatus.BLOCKED)).lower()
    assert "CPU mode" in panel_summary(_rec(RecommendationStatus.NOT_APPLICABLE))
    assert "unknown" in panel_summary(_rec(RecommendationStatus.UNKNOWN)).lower()


def test_panel_detail_blocked_includes_reason():
    assert "no driver" in panel_detail(
        _rec(RecommendationStatus.BLOCKED, reason="no driver")
    )


def test_panel_summary_already_ready_is_slot_state_verdict():
    """ZA-M1-3A.2: ALREADY_READY is a SLOT-STATE verdict (active slot
    carries validated accelerated-metadata + closure), never the mere
    presence of a GPU.  The summary explicitly distinguishes the
    validated runtime from the OFFER_SETUP offer and never says
    "to configure"; the configure button stays hidden."""
    rec = _rec(RecommendationStatus.ALREADY_READY)
    summary = panel_summary(rec)
    assert "active and validated" in summary
    assert "to configure" not in summary
    assert configure_button_visible(rec) is False


def test_gpu_wording_never_claims_version_cannot_install():
    """The exposed GPU configure wording (EN + FR) is honest: it never
    claims this version cannot install an accelerated runtime, and never
    claims an install already happened.  Rendered through i18n — no
    hardcoded widget string."""
    from zealfie.gui.acceleration_panel import panel_configure_message
    from zealfie.i18n import Language, reset_language, set_language

    rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
    try:
        en = panel_configure_message(rec)
        assert "to configure" in en
        assert "not performed by this version" not in en
        assert "did not install" not in en
        assert "not available in this version" not in en

        set_language(Language.FR)
        fr = panel_configure_message(rec)
        assert fr != en
        assert "à configurer" in fr
        assert "not performed by this version" not in fr
        assert "did not install" not in fr
    finally:
        reset_language()


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


class _FakePanelService:
    def __init__(self, intent=None, intent_raises=None):
        self._intent = intent
        self._intent_raises = intent_raises
        self.prepare_calls = 0
        self.prepared_recommendations = []

    def prepare_gpu_setup_intent(self, recommendation=None):
        self.prepare_calls += 1
        self.prepared_recommendations.append(recommendation)
        if self._intent_raises is not None:
            raise self._intent_raises
        return self._intent


def test_panel_not_offering_config_when_blocked(qapp):
    panel = AccelerationPanel(service=_FakePanelService())
    try:
        panel.set_recommendation(_rec(RecommendationStatus.BLOCKED, reason="driver missing"))
        assert panel._button.isHidden() is True
        assert "driver unavailable" in panel._summary_label.text().lower()
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_panel_offering_config_only_when_allowed(qapp):
    panel = AccelerationPanel(service=_FakePanelService())
    try:
        panel.set_recommendation(_rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),)))
        assert panel._button.isHidden() is False
        assert "Configure GPU" in panel._button.text()
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_panel_configure_click_shows_honest_no_install_message(qapp):
    from zealfie.host.models import GpuSetupIntent

    rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
    intent = GpuSetupIntent(
        recommendation=rec,
        actionable=True,
        message="NVIDIA GPU detected with a usable driver. GPU acceleration can be configured for compatible installed products.",
    )
    service = _FakePanelService(intent=intent)
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(rec)
        panel._button.click()
        assert service.prepare_calls == 1
        assert service.prepared_recommendations == [rec]
        text = panel._detail_label.text()
        assert "to configure" in text
        assert "no CUDA toolkit" not in text
        assert "did not install" not in text
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_panel_configure_click_uses_displayed_recommendation_without_reprobe(qapp):
    """Clicking ``Configurer le GPU`` passes the displayed recommendation and
    never triggers a second hardware observation."""
    from zealfie.host.models import GpuSetupIntent

    rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
    intent = GpuSetupIntent(
        recommendation=rec,
        actionable=True,
        message="NVIDIA GPU detected with a usable driver. GPU acceleration can be configured for compatible installed products.",
    )
    service = _FakePanelService(intent=intent)
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(rec)
        assert panel._recommendation is rec
        assert panel._button.isHidden() is False

        panel._button.click()

        assert service.prepare_calls == 1
        # The exact displayed recommendation was passed through — no re-probe.
        assert service.prepared_recommendations == [rec]
        assert service.prepared_recommendations[0] is rec
        assert intent.recommendation is rec
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_panel_configure_click_survives_probe_error(qapp):
    service = _FakePanelService(intent_raises=RuntimeError("probe exploded"))
    panel = AccelerationPanel(service=service)
    try:
        panel.set_recommendation(_rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),)))
        panel._button.click()
        assert "check failed" in panel._detail_label.text().lower()
        assert "Traceback" not in panel._detail_label.text()
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


# ===========================================================================
# 3) Main-window integration (headless)
# ===========================================================================


class _FakeWindowService:
    """Minimal service for main-window construction tests."""

    def __init__(self, recommendation=None, recommendation_raises=None):
        self._recommendation = recommendation
        self._recommendation_raises = recommendation_raises

    def list_products(self):
        return ()

    def collect_product_state(self):
        from zealfie.app import ProductShellState
        from zealfie.runtime.model import RuntimeState

        return ProductShellState(
            runtime_state=RuntimeState.READY,
            runtime_root=Path("/fake/runtime"),
            products=(),
        )

    def get_acceleration_recommendation(self):
        if self._recommendation_raises is not None:
            raise self._recommendation_raises
        return self._recommendation


def test_main_window_shows_unknown_when_probe_raises(qapp):
    from zealfie.gui.main_window import ZeAlfieMainWindow

    service = _FakeWindowService(
        recommendation_raises=RuntimeError("nvidia-smi exploded")
    )
    window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
    try:
        assert window is not None
        assert window._acceleration_panel is not None
        # Panel must show an honest unknown state, not crash.
        assert "unknown" in window._acceleration_panel._summary_label.text().lower()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_shows_offer_setup_when_ready(qapp):
    from zealfie.gui.main_window import ZeAlfieMainWindow

    rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
    service = _FakeWindowService(recommendation=rec)
    window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
    try:
        assert window._acceleration_panel._button.isHidden() is False
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_does_not_offer_config_when_blocked(qapp):
    from zealfie.gui.main_window import ZeAlfieMainWindow

    rec = _rec(RecommendationStatus.BLOCKED, reason="driver unavailable")
    service = _FakeWindowService(recommendation=rec)
    window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
    try:
        assert window._acceleration_panel._button.isHidden() is True
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()

class _FakeWindowCapabilitiesService:
    """Minimal service exposing capabilities + recommendation APIs.

    Mirrors the ZeAlfieService shape: the recommendation getter accepts
    the collected capabilities so the window can derive it from the
    exact same observation (one observation cycle).
    """

    def __init__(self, capabilities, recommendation) -> None:
        self._caps = capabilities
        self._rec = recommendation
        self.collect_calls = 0
        self.recommendation_calls = []

    def list_products(self):
        return ()

    def collect_product_state(self):
        from zealfie.app import ProductShellState
        from zealfie.runtime.model import RuntimeState

        return ProductShellState(
            runtime_state=RuntimeState.READY,
            runtime_root=Path("/fake/runtime"),
            products=(),
        )

    def collect_host_capabilities(self):
        self.collect_calls += 1
        return self._caps

    def get_acceleration_recommendation(self, capabilities=None):
        self.recommendation_calls.append(capabilities)
        return self._rec


def _fake_capabilities() -> HostCapabilities:
    return HostCapabilities(
        os_name="linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=(),
        partial=False,
    )


def test_main_window_one_observation_cycle_forwards_capabilities(qapp):
    """The main window collects capabilities once, derives the
    recommendation from that exact observation, and stores both in the
    panel — the configure click then never re-probes."""
    from zealfie.gui.main_window import ZeAlfieMainWindow

    caps = _fake_capabilities()
    rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
    service = _FakeWindowCapabilitiesService(capabilities=caps, recommendation=rec)
    window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
    try:
        assert service.collect_calls == 1
        assert service.recommendation_calls == [caps]
        assert window._acceleration_panel._recommendation is rec
        assert window._acceleration_panel._capabilities is caps
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
