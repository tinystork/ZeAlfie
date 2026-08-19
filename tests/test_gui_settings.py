"""M1-5-A — product shell structure: top bar, Settings page, compact GPU badge.

Hermetic: fake services only, offscreen Qt, no probing, no network, no install.
Covers Lots A + B + C of ZA-M1-5-A:

* Lot A — single-row top bar (Settings menu w/ Language submenu + Refresh).
* Lot B — home shows a compact GPU badge, not the full AccelerationPanel.
* Lot C — Settings page hosts Language / Hardware / Runtime / GPU panel.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QComboBox
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

from zealfie.gui.acceleration_panel import AccelerationPanel
from zealfie.gui.presentation import compact_gpu_status

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")


@pytest.fixture(autouse=True)
def _reset_language():
    """Every test starts and ends in the default EN language."""
    from zealfie.i18n import reset_language

    reset_language()
    yield
    reset_language()


@pytest.fixture(scope="session")
def qapp():
    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ===========================================================================
# Synthetic data
# ===========================================================================


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


def _capabilities(gpus=()) -> HostCapabilities:
    return HostCapabilities(
        os_name="linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=tuple(gpus),
        partial=False,
    )


def _rec(status, gpus=()) -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=status,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="",
        gpus=tuple(gpus),
    )


class _FakeShellService:
    """Minimal service for main-window construction tests."""

    def __init__(
        self,
        recommendation=None,
        capabilities=None,
        runtime_state="READY",
    ) -> None:
        self._recommendation = recommendation
        self._capabilities = capabilities
        self._runtime_state = runtime_state
        self.collect_calls = 0

    def list_products(self):
        return ()

    def collect_product_state(self):
        from zealfie.app import ProductShellState
        from zealfie.runtime.model import RuntimeState

        self.collect_calls += 1
        return ProductShellState(
            runtime_state=RuntimeState(self._runtime_state),
            runtime_root=Path("/fake/runtime"),
            products=(),
        )

    def collect_host_capabilities(self):
        return self._capabilities

    def get_acceleration_recommendation(self, capabilities=None):
        return self._recommendation


# ===========================================================================
# 1) Pure compact status mapping
# ===========================================================================


class TestCompactGpuStatus:
    def test_unknown_when_none(self):
        assert "unknown" in compact_gpu_status(None).lower()

    def test_offer_setup_includes_model(self):
        rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
        text = compact_gpu_status(rec)
        assert "GeForce RTX 4090" in text
        assert "to configure" in text

    def test_ready(self):
        assert "ready" in compact_gpu_status(
            _rec(RecommendationStatus.ALREADY_READY)
        ).lower()

    def test_blocked(self):
        assert "blocked" in compact_gpu_status(
            _rec(RecommendationStatus.BLOCKED)
        ).lower()

    def test_not_applicable_cpu_mode(self):
        assert "CPU" in compact_gpu_status(
            _rec(RecommendationStatus.NOT_APPLICABLE)
        )

    def test_installing_overrides_status(self):
        rec = _rec(RecommendationStatus.ALREADY_READY)
        assert "installing" in compact_gpu_status(rec, install_active=True).lower()


# ===========================================================================
# 2) Top bar structure (Lot A)
# ===========================================================================


class TestTopBar:
    def test_single_top_level_settings_menu(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            menu_bar = window.menuBar()
            top_menus = [a for a in menu_bar.actions() if a.menu() is not None]
            assert len(top_menus) == 1
            assert top_menus[0].text() == "Settings"
            assert window._settings_menu is not None
            assert window._settings_menu.parent() is menu_bar
            assert window._language_menu is not None
            assert window._language_menu.parent() is window._settings_menu
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_single_refresh_action_f5_preserved(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            refresh_actions = [
                a for a in window.findChildren(QAction) if "Refresh" in a.text()
            ]
            assert len(refresh_actions) == 1
            assert refresh_actions[0] is window._refresh_action
            assert window._refresh_action.shortcut().toString() == "F5"

            # Refresh is never duplicated inside a menu.
            for top in window.menuBar().actions():
                menu = top.menu()
                if menu is not None:
                    for a in menu.actions():
                        assert "Refresh" not in a.text()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_no_duplicate_language_entry(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            # Exactly two language actions, reached only via the Settings menu.
            assert set(window._language_actions.keys()) == {"en", "fr"}
            top_titles = [a.text() for a in window.menuBar().actions()]
            assert "Language" not in top_titles
            assert "Langue" not in top_titles
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 3) Home badge + Settings navigation (Lot B + C)
# ===========================================================================


class TestBadgeAndNavigation:
    def test_home_has_badge_not_full_panel(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
        service = _FakeShellService(
            recommendation=rec, capabilities=_capabilities((_nvidia_gpu(),))
        )
        window = ZeAlfieMainWindow(service=service)
        try:
            assert window._acceleration_badge is not None
            # The full AccelerationPanel lives on the Settings page, not the
            # home page.
            home = window._home_page
            panels_on_home = home.findChildren(AccelerationPanel)
            assert len(panels_on_home) == 0
            assert window._acceleration_panel is not None
            assert (
                window._acceleration_panel
                in window._settings_page.findChildren(AccelerationPanel)
            )
            # Badge reflects the recommendation.
            assert "GeForce RTX 4090" in window._acceleration_badge.text()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_badge_click_opens_settings(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            assert window._stack.currentWidget() is window._home_page
            window._acceleration_badge.click()
            qapp.processEvents()
            assert window._stack.currentWidget() is window._settings_page
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_back_returns_home(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            window._open_settings()
            assert window._stack.currentWidget() is window._settings_page
            window._settings_page.back_requested.emit()
            qapp.processEvents()
            assert window._stack.currentWidget() is window._home_page
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_badge_mirrors_panel_status_change(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            assert "unknown" in window._acceleration_badge.text().lower()
            window._acceleration_panel.set_recommendation(
                _rec(RecommendationStatus.ALREADY_READY)
            )
            assert "ready" in window._acceleration_badge.text().lower()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 4) Settings page contents (Lot C)
# ===========================================================================


class TestSettingsContents:
    def test_language_combo_switches_live(self, qapp, monkeypatch):
        from zealfie.i18n import Language, get_language
        from zealfie.gui.main_window import ZeAlfieMainWindow

        saved: dict = {}

        class _FakeStore:
            def __init__(self, path=None):
                pass

            def save(self, lang):
                saved["lang"] = lang

        monkeypatch.setattr("zealfie.gui.main_window.LanguageStore", _FakeStore)

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            assert get_language() is Language.EN
            combo = window._settings_page._language_combo
            assert isinstance(combo, QComboBox)
            combo.setCurrentIndex(1)  # Français
            assert get_language() is Language.FR
            assert saved.get("lang") is Language.FR
            assert "Lanceur" in window.windowTitle()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_hardware_section_renders_capabilities(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        caps = _capabilities((_nvidia_gpu(),))
        service = _FakeShellService(
            recommendation=_rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),)),
            capabilities=caps,
        )
        window = ZeAlfieMainWindow(service=service)
        try:
            text = window._settings_page._hardware_label.text()
            assert "GeForce RTX 4090" in text
            assert "560.35.03" in text
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_runtime_section_renders_state(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _FakeShellService(runtime_state="ABSENT")
        window = ZeAlfieMainWindow(service=service)
        try:
            text = window._settings_page._runtime_label.text()
            assert "absent" in text
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_gpu_panel_hosted_in_settings(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
        service = _FakeShellService(
            recommendation=rec, capabilities=_capabilities((_nvidia_gpu(),))
        )
        window = ZeAlfieMainWindow(service=service)
        try:
            panel = window._acceleration_panel
            assert panel is not None
            # Configure button offered (OFFER_SETUP) but only in Settings.
            assert panel._button.isHidden() is False
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 5) Runtime absent: shell usable, GPU detected, no illegitimate install
# ===========================================================================


class TestRuntimeAbsent:
    def test_shell_usable_and_gpu_detected(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        rec = _rec(RecommendationStatus.OFFER_SETUP, gpus=(_nvidia_gpu(),))
        service = _FakeShellService(
            recommendation=rec,
            capabilities=_capabilities((_nvidia_gpu(),)),
            runtime_state="ABSENT",
        )
        window = ZeAlfieMainWindow(service=service)
        try:
            assert window is not None
            assert "GeForce RTX 4090" in window._acceleration_badge.text()
            # No Installer offered on the home page: the full panel (and its
            # Installer button) is only reachable via Settings.
            assert len(window._home_page.findChildren(AccelerationPanel)) == 0
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
