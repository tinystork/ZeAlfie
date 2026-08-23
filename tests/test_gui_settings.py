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

from zealfie.app import (
    ManagedStatus,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
)
from zealfie.gui.acceleration_panel import AccelerationPanel
from zealfie.gui.presentation import compact_gpu_status
from zealfie.runtime.model import RuntimeState

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
            assert "Gestionnaire" in window.windowTitle()
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



# ===========================================================================
# 6) Runtime details (M1-5-C): managed products, active slot, honest absent
# ===========================================================================


def _product_state(
    pid: str,
    name: str,
    *,
    installed: bool = False,
    managed=ManagedStatus.UNMANAGED,
) -> ProductState:
    return ProductState(
        product_id=pid,
        display_name=name,
        known=True,
        installed=installed,
        launchable=installed,
        version="1.0.0" if installed else None,
        reason_code=(
            ProductStateReasonCode.INSTALLED_LAUNCHABLE
            if installed
            else ProductStateReasonCode.NOT_INSTALLED
        ),
        reason="installed and launchable" if installed else "not installed",
        managed=managed,
    )


class TestRuntimeDetails:
    def _shell(
        self,
        *,
        state="READY",
        root=Path("/srv/zealfie-runtime"),
        products=(),
        slot=None,
    ):
        return ProductShellState(
            runtime_state=RuntimeState(state),
            runtime_root=root,
            products=tuple(products),
            active_slot_id=slot,
        )

    def _render(self, qapp, shell):
        from zealfie.gui.settings_page import SettingsPage

        page = SettingsPage(service=_FakeShellService())
        page.set_shell_state(shell)
        return page

    def test_root_is_authoritative(self, qapp):
        page = self._render(qapp, self._shell(root=Path("/srv/zealfie-runtime")))
        try:
            assert "/srv/zealfie-runtime" in page._runtime_label.text()
        finally:
            page.close()
            page.deleteLater()
            qapp.processEvents()

    def test_managed_products_shown_by_display_name(self, qapp):
        products = (
            _product_state(
                "zemosaic", "ZeMosaic", installed=True,
                managed=ManagedStatus.MANAGED,
            ),
            _product_state(
                "zesolver", "ZeSolver", managed=ManagedStatus.UNMANAGED,
            ),
        )
        page = self._render(qapp, self._shell(products=products))
        try:
            text = page._runtime_label.text()
            assert "ZeMosaic" in text
            assert "ZeSolver" not in text
        finally:
            page.close()
            page.deleteLater()
            qapp.processEvents()

    def test_active_slot_shown_as_advanced_detail(self, qapp):
        page = self._render(qapp, self._shell(slot="rt-db83abc"))
        try:
            text = page._runtime_label.text()
            assert "rt-db83abc" in text
            assert "Active slot" in text
        finally:
            page.close()
            page.deleteLater()
            qapp.processEvents()

    def test_runtime_absent_stays_honest_no_products_or_slot(self, qapp):
        products = (
            _product_state(
                "zemosaic", "ZeMosaic", managed=ManagedStatus.MANAGED,
            ),
        )
        page = self._render(
            qapp, self._shell(state="ABSENT", products=products, slot=None)
        )
        try:
            text = page._runtime_label.text()
            assert "absent" in text.lower()
            assert "ZeMosaic" not in text
            assert "Active slot" not in text
            assert "Slot actif" not in text
        finally:
            page.close()
            page.deleteLater()
            qapp.processEvents()

    def test_retranslate_runtime_details_en_fr(self, qapp):
        from zealfie.i18n import Language, set_language

        products = (
            _product_state(
                "zemosaic", "ZeMosaic", installed=True,
                managed=ManagedStatus.MANAGED,
            ),
        )
        page = self._render(
            qapp, self._shell(products=products, slot="rt-db83abc")
        )
        try:
            en = page._runtime_label.text()
            assert "Managed products" in en
            assert "Active slot" in en
            set_language(Language.FR)
            page.retranslate()
            fr = page._runtime_label.text()
            assert "Produits gérés" in fr
            assert "Slot actif" in fr
        finally:
            page.close()
            page.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 7) Close guard during accelerated install (M1-5-C)
# ===========================================================================


class TestGpuCloseGuard:
    def test_close_rejected_during_accelerated_install(self, qapp):
        from PySide6.QtGui import QCloseEvent
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            window._acceleration_panel._install_active = True
            ev = QCloseEvent()
            window.closeEvent(ev)
            assert ev.isAccepted() is False
            assert "please wait" in window._status_label.text().lower()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    @pytest.mark.parametrize(
        "result",
        [
            dict(success=True, cancelled=False, phase="COMPLETED"),
            dict(success=False, cancelled=False, phase="ACQUIRE"),
            dict(success=False, cancelled=True, phase="ACQUIRE"),
        ],
        ids=["success", "failure", "cancelled"],
    )
    def test_close_allowed_after_accelerated_install_terminates(
        self, qapp, result
    ):
        from PySide6.QtGui import QCloseEvent
        from zealfie.acceleration import (
            AcceleratedDeploymentPhase,
            AcceleratedDeploymentResult,
        )
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_FakeShellService())
        try:
            panel = window._acceleration_panel
            panel._install_active = True
            panel._on_install_finished(
                AcceleratedDeploymentResult(
                    success=result["success"],
                    cancelled=result["cancelled"],
                    phase=AcceleratedDeploymentPhase(result["phase"]),
                    active_slot_id="slot-new" if result["success"] else None,
                    reason=None if result["success"] else "some reason",
                )
            )
            assert panel.install_active is False
            ev = QCloseEvent()
            window.closeEvent(ev)
            assert ev.isAccepted() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
