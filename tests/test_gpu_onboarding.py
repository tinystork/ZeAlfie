"""ZA-M1-5-B LOT D — GPU onboarding + LOT F wording (onboarding / Refresh).

Hermetic: fake services only, offscreen Qt, no probing, no network, no install.

Covers:

* LOT D trigger — after a successful install of a GPU-capable product, with
  the service recommendation at OFFER_SETUP, a non-intrusive onboarding
  banner is offered; NOT_APPLICABLE / ALREADY_READY / BLOCKED never offer it,
  and a non-GPU-capable product (ZeSolver) never offers it either.
* "Enable acceleration" opens Settings (never a silent GPU install).
* "Later" installs nothing, stays dismissed across refreshes, and the action
  stays reachable via Settings.
* LOT F wording — Refresh is localized (EN "Refresh" / FR "Rafraîchir"),
  the onboarding banner is localized, and plan.no_requirements uses the new
  installed-product wording.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.acceleration.models import (
    AcceleratedRequirement,
    ProductAccelerationRequirements,
)
from zealfie.app import (
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
)
from zealfie.components.model import EntryPointContract
from zealfie.host.models import (
    AccelerationRecommendation,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.runtime.model import DeploymentResult, RuntimeState

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")


@pytest.fixture(autouse=True)
def _reset_language():
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


_EP = (EntryPointContract("gui_scripts", "zesolver"),)


def _gpu_acceleration(product_id: str) -> ProductAccelerationRequirements:
    return ProductAccelerationRequirements(
        product_id=product_id,
        backend="NVIDIA_CUDA",
        optional=True,
        requirements=(AcceleratedRequirement(distribution="cupy-cuda12x"),),
    )


def _desc(
    product_id: str,
    *,
    gpu_capable: bool = False,
    name: str | None = None,
) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=name or product_id.title(),
        distribution_name=product_id,
        launch_entry_points=_EP,
        acceleration=_gpu_acceleration(product_id) if gpu_capable else None,
    )


def _state(
    product_id: str,
    *,
    installed: bool = False,
    launchable: bool = False,
) -> ProductState:
    return ProductState(
        product_id=product_id,
        display_name=product_id.title(),
        known=True,
        installed=installed,
        launchable=launchable,
        version="1.0.0" if installed else None,
        reason_code=(
            ProductStateReasonCode.INSTALLED_LAUNCHABLE
            if launchable
            else ProductStateReasonCode.NOT_INSTALLED
        ),
        reason="ok" if launchable else "not installed",
    )


def _shell(products: tuple[ProductState, ...]) -> ProductShellState:
    return ProductShellState(
        runtime_state=RuntimeState.READY,
        runtime_root=Path("/fake/runtime"),
        products=products,
    )


def _rec(status: RecommendationStatus) -> AccelerationRecommendation:
    return AccelerationRecommendation(
        status=status,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="",
        gpus=(),
    )


class FakeOnboardingService:
    """Fake service: catalog + state + recommendation + install recording."""

    def __init__(
        self,
        descriptors: tuple[ProductDescriptor, ...],
        *,
        recommendation: AccelerationRecommendation | None = None,
        shell_state: ProductShellState | None = None,
    ) -> None:
        self._descriptors = descriptors
        self._recommendation = recommendation
        self._shell_state = shell_state
        self.install_calls: list[str] = []
        self.accelerated_install_calls: list[dict] = []
        self.collect_calls = 0

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        return self._descriptors

    def collect_product_state(self) -> ProductShellState:
        self.collect_calls += 1
        if self._shell_state is None:
            raise ValueError("no shell state configured")
        return self._shell_state

    def collect_host_capabilities(self):
        return None

    def get_acceleration_recommendation(self, capabilities=None):
        return self._recommendation

    def install_product(self, product_id, **kwargs):
        self.install_calls.append(product_id)
        return DeploymentResult(success=True, active_slot_id="rt-fake")

    def install_accelerated_runtime(self, **kwargs):
        self.accelerated_install_calls.append(kwargs)
        return DeploymentResult(success=True, active_slot_id="rt-fake")


def _make_window(qapp, service, *, resolver=None, fetcher=None, work_root=None):
    from zealfie.gui.main_window import ZeAlfieMainWindow

    if resolver is None:
        resolver = lambda o, r, ref: "a" * 40
    if fetcher is None:
        fetcher = lambda o, r, sha: b"zip"
    if work_root is None:
        work_root = Path("/tmp/fake-work")
    return ZeAlfieMainWindow(
        service=service,
        resolver=resolver,
        fetcher=fetcher,
        work_root=work_root,
    )


# ===========================================================================
# LOT D — trigger
# ===========================================================================


class TestGpuOnboardingTrigger:
    def test_gpu_capable_product_offer_setup_shows_banner(self, qapp):
        """ZeMosaic (GPU-capable) installed + OFFER_SETUP → banner offered."""
        desc = _desc("zemosaic", gpu_capable=True, name="ZeMosaic")
        service = FakeOnboardingService(
            (desc,),
            recommendation=_rec(RecommendationStatus.OFFER_SETUP),
            shell_state=_shell((_state("zemosaic", installed=True, launchable=True),)),
        )
        window = _make_window(qapp, service)
        try:
            assert window._gpu_onboarding_banner.isHidden()

            # Simulate the post-install success callback.
            window._on_worker_success("zemosaic")

            assert not window._gpu_onboarding_banner.isHidden()
            assert "ZeMosaic" in window._gpu_onboarding_banner.message_text
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_non_gpu_product_never_shows_banner(self, qapp):
        """ZeSolver (non-accelerated) installed → no banner even at OFFER_SETUP."""
        desc = _desc("zesolver", gpu_capable=False)
        service = FakeOnboardingService(
            (desc,),
            recommendation=_rec(RecommendationStatus.OFFER_SETUP),
            shell_state=_shell((_state("zesolver", installed=True, launchable=True),)),
        )
        window = _make_window(qapp, service)
        try:
            window._on_worker_success("zesolver")
            assert window._gpu_onboarding_banner.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    @pytest.mark.parametrize(
        "status",
        [
            RecommendationStatus.NOT_APPLICABLE,
            RecommendationStatus.ALREADY_READY,
            RecommendationStatus.BLOCKED,
        ],
    )
    def test_no_banner_for_non_offer_statuses(self, qapp, status):
        """NOT_APPLICABLE / ALREADY_READY / BLOCKED never offer the banner."""
        desc = _desc("zemosaic", gpu_capable=True, name="ZeMosaic")
        service = FakeOnboardingService(
            (desc,),
            recommendation=_rec(status),
            shell_state=_shell((_state("zemosaic", installed=True, launchable=True),)),
        )
        window = _make_window(qapp, service)
        try:
            window._on_worker_success("zemosaic")
            assert window._gpu_onboarding_banner.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_refresh_never_offers_banner(self, qapp):
        """The banner is only offered on install completion, never on refresh."""
        desc = _desc("zemosaic", gpu_capable=True, name="ZeMosaic")
        service = FakeOnboardingService(
            (desc,),
            recommendation=_rec(RecommendationStatus.OFFER_SETUP),
            shell_state=_shell((_state("zemosaic", installed=True, launchable=True),)),
        )
        window = _make_window(qapp, service)
        try:
            window._refresh()
            assert window._gpu_onboarding_banner.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ===========================================================================
# LOT D — "Enable acceleration" / "Later" (no silent install)
# ===========================================================================


class TestGpuOnboardingActions:
    def _offered_window(self, qapp):
        desc = _desc("zemosaic", gpu_capable=True, name="ZeMosaic")
        service = FakeOnboardingService(
            (desc,),
            recommendation=_rec(RecommendationStatus.OFFER_SETUP),
            shell_state=_shell((_state("zemosaic", installed=True, launchable=True),)),
        )
        window = _make_window(qapp, service)
        window._on_worker_success("zemosaic")
        return window, service

    def test_activate_opens_settings_no_silent_install(self, qapp):
        """'Enable acceleration' opens Settings with the GPU panel; no install."""
        window, service = self._offered_window(qapp)
        try:
            assert not window._gpu_onboarding_banner.isHidden()

            window._gpu_onboarding_banner.activate_requested.emit()
            qapp.processEvents()

            assert window._stack.currentWidget() is window._settings_page
            # No accelerated runtime install was ever triggered.
            assert service.accelerated_install_calls == []
            assert service.install_calls == []
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_later_installs_nothing_and_stays_dismissed(self, qapp):
        """'Later' installs nothing, hides, and never re-shows on refresh."""
        window, service = self._offered_window(qapp)
        try:
            assert not window._gpu_onboarding_banner.isHidden()

            window._gpu_onboarding_banner.dismissed.emit()
            qapp.processEvents()

            assert window._gpu_onboarding_banner.isHidden()
            assert service.accelerated_install_calls == []
            assert service.install_calls == []

            # Refresh does NOT re-arm the offer.
            window._refresh()
            assert window._gpu_onboarding_banner.isHidden()

            # The action stays reachable via Settings (badge → Settings → panel).
            assert window._acceleration_badge is not None
            window._open_settings()
            assert window._stack.currentWidget() is window._settings_page
            panel = window._acceleration_panel
            assert panel is not None
            # OFFER_SETUP → Configure is offered inside Settings.
            assert panel._button.isHidden() is False
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_new_install_rearms_after_dismissal(self, qapp):
        """A NEW install request re-arms the offer for the same product."""
        window, service = self._offered_window(qapp)
        try:
            window._gpu_onboarding_banner.dismissed.emit()
            assert window._gpu_onboarding_banner.isHidden()

            # A fresh install request clears the dismissal.
            window._on_install_requested("zemosaic")
            window._on_worker_success("zemosaic")
            assert not window._gpu_onboarding_banner.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ===========================================================================
# LOT F — wording (onboarding + Refresh)
# ===========================================================================


class TestOnboardingWording:
    def test_refresh_localized_en_fr(self):
        from zealfie.i18n import Language, set_language, translate

        set_language(Language.EN)
        assert translate("menu.refresh") == "Refresh"
        set_language(Language.FR)
        assert translate("menu.refresh") == "Rafraîchir"

    def test_plan_no_requirements_wording(self):
        from zealfie.i18n import EN, FR

        assert "installed" in EN["plan.no_requirements"].lower()
        assert "installed" in FR["plan.no_requirements"].lower() or "installé" in FR["plan.no_requirements"]
        # No longer claims "no product declares" (reformulated from the
        # installed/applicable product viewpoint).
        assert "declares" not in EN["plan.no_requirements"].lower()

    def test_onboarding_banner_localized(self):
        from zealfie.i18n import Language, set_language, translate

        set_language(Language.EN)
        assert "ZeMosaic" in translate("gpu.onboarding.message", product="ZeMosaic")
        assert translate("gpu.onboarding.activate") == "Enable acceleration"
        assert translate("gpu.onboarding.later") == "Later"

        set_language(Language.FR)
        assert "ZeMosaic" in translate("gpu.onboarding.message", product="ZeMosaic")
        assert translate("gpu.onboarding.activate") == "Activer l'accélération"
        assert translate("gpu.onboarding.later") == "Plus tard"

    def test_refresh_action_retranslates_live(self, qapp, monkeypatch):
        from zealfie.i18n import Language, set_language
        from zealfie.gui.main_window import ZeAlfieMainWindow

        class _FakeStore:
            def __init__(self, path=None):
                pass

            def save(self, lang):
                pass

        monkeypatch.setattr("zealfie.gui.main_window.LanguageStore", _FakeStore)

        desc = _desc("zemosaic", gpu_capable=True, name="ZeMosaic")
        service = FakeOnboardingService(
            (desc,),
            recommendation=_rec(RecommendationStatus.OFFER_SETUP),
            shell_state=_shell((_state("zemosaic", installed=True, launchable=True),)),
        )
        window = ZeAlfieMainWindow(service=service)
        try:
            assert "Refresh" in window._refresh_action.text()
            assert window._refresh_action.shortcut().toString() == "F5"

            set_language(Language.FR)
            window._retranslate()

            assert "Rafraîchir" in window._refresh_action.text()
            assert window._refresh_action.shortcut().toString() == "F5"
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
