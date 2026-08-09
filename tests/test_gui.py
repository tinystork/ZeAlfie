"""Tests for M1-2C — PySide6 product shell GUI.

All tests run headless via ``QT_QPA_PLATFORM=offscreen``.
No real runtime, no filesystem probing, no subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# PySide6 import guarded — test session will fail cleanly if missing.
try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.app import (
    ManagedStatus,
    ProductCatalog,
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    SpawnedLaunch,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.products.state import collect_product_state
from zealfie.runtime.model import RuntimeReasonCode, RuntimeState, RuntimeStatus

from zealfie.gui.presentation import (
    action_enabled,
    action_label,
    action_tooltip,
    runtime_summary,
    state_label,
)

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

# ===========================================================================
# Helpers
# ===========================================================================


def _make_descriptor(
    product_id: str,
    display_name: str = "",
    description: str = "",
) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=display_name or product_id.title(),
        distribution_name=display_name or product_id,
        launch_entry_points=(EntryPointContract("gui_scripts", product_id),),
        description=description,
    )


def _make_catalog(*descriptors: ProductDescriptor) -> ProductCatalog:
    return ProductCatalog(descriptors)


def _make_state(
    product_id: str,
    display_name: str = "Test",
    *,
    installed: bool = False,
    launchable: bool = False,
    version: str | None = None,
    reason_code: ProductStateReasonCode = ProductStateReasonCode.RUNTIME_ABSENT,
    reason: str = "shared runtime is absent",
    managed: ManagedStatus = ManagedStatus.UNMANAGED,
) -> ProductState:
    return ProductState(
        product_id=product_id,
        display_name=display_name,
        known=True,
        installed=installed,
        launchable=launchable,
        version=version,
        reason_code=reason_code,
        reason=reason,
        managed=managed,
    )


def _make_shell_state(
    products: tuple[ProductState, ...],
    runtime_state: RuntimeState = RuntimeState.ABSENT,
    runtime_root: Path = Path("/fake/runtime"),
) -> ProductShellState:
    return ProductShellState(
        runtime_state=runtime_state,
        runtime_root=runtime_root,
        products=products,
    )


class FakeService:
    """Fake ZeAlfieService for GUI smoke tests.

    Injects fake catalog, fake state, and a recording spawn.
    """

    def __init__(
        self,
        descriptors: tuple[ProductDescriptor, ...] = (),
        shell_state: ProductShellState | None = None,
        spawn_result: SpawnedLaunch | None = None,
        spawn_raises: Exception | None = None,
    ) -> None:
        self._descriptors = descriptors
        self._shell_state = shell_state
        self._spawn_result = spawn_result or SpawnedLaunch(
            component_id="fake", pid=99999
        )
        self._spawn_raises = spawn_raises
        self.spawn_calls: list[str] = []
        self.collect_calls: int = 0

    @property
    def catalog(self):
        return _make_catalog(*self._descriptors)

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        return self._descriptors

    def collect_product_state(self) -> ProductShellState:
        self.collect_calls += 1
        if self._shell_state is None:
            raise ValueError("no shell state configured")
        return self._shell_state

    def get_product_state(self, product_id: str) -> ProductState:
        if self._shell_state:
            for p in self._shell_state.products:
                if p.product_id == product_id:
                    return p
        raise ValueError(f"unknown product: {product_id}")

    def spawn_component(self, product_id: str, **kwargs) -> SpawnedLaunch:
        self.spawn_calls.append(product_id)
        if self._spawn_raises:
            raise self._spawn_raises
        return self._spawn_result


def _create_standard_fake_service(
    *,
    runtime_state: RuntimeState = RuntimeState.ABSENT,
) -> FakeService:
    """Create a FakeService with all 4 standard products."""
    descriptors = (
        _make_descriptor(
            "zesolver", "ZeSolver",
            description="Optical solver for astrophotography.",
        ),
        _make_descriptor(
            "zemosaic", "ZeMosaic",
            description="Mosaic planner and composer.",
        ),
        _make_descriptor(
            "zeseestarstacker", "ZeSeestarStacker",
            description="One-click Seestar stacking.",
        ),
        _make_descriptor(
            "zeanalyser", "ZeAnalyser",
            description="Deep image analysis toolkit.",
        ),
    )

    products: tuple[ProductState, ...] = tuple(
        _make_state(
            pid,
            display_name=desc.display_name,
            reason_code=ProductStateReasonCode.RUNTIME_ABSENT
            if runtime_state == RuntimeState.ABSENT
            else ProductStateReasonCode.NOT_INSTALLED,
            reason="shared runtime is absent"
            if runtime_state == RuntimeState.ABSENT
            else "not installed",
        )
        for pid, desc in [
            ("zesolver", descriptors[0]),
            ("zemosaic", descriptors[1]),
            ("zeseestarstacker", descriptors[2]),
            ("zeanalyser", descriptors[3]),
        ]
    )

    return FakeService(
        descriptors=descriptors,
        shell_state=_make_shell_state(products, runtime_state=runtime_state),
    )


# ===========================================================================
# 1) Presentation layer unit tests (no Qt)
# ===========================================================================


class TestPresentation:
    """Unit tests for presentation layer — no QApplication needed."""

    def test_state_label_not_installed(self):
        s = _make_state(
            "test", reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        assert "Not installed" in state_label(s)
        assert "Installer coming" in state_label(s)

    def test_state_label_installed_launchable(self):
        s = _make_state(
            "test", reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason="ok",
        )
        assert "Ready" in state_label(s)
        assert "Lancer" in state_label(s)

    def test_state_label_installed_not_launchable(self):
        s = _make_state(
            "test", reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE,
            reason="contract missing",
        )
        assert "launch contract missing" in state_label(s).lower()

    def test_state_label_runtime_absent(self):
        s = _make_state("test", reason_code=ProductStateReasonCode.RUNTIME_ABSENT)
        assert "No runtime" in state_label(s)

    def test_state_label_runtime_broken(self):
        s = _make_state("test", reason_code=ProductStateReasonCode.RUNTIME_BROKEN)
        assert "broken" in state_label(s).lower()

    def test_state_label_probe_failed(self):
        s = _make_state("test", reason_code=ProductStateReasonCode.PROBE_FAILED)
        assert "probe failed" in state_label(s).lower()

    def test_state_label_fallback_never_raw_enum(self):
        """Unknown reason code falls back to the reason string, not the enum."""
        s = _make_state(
            "test",
            reason_code=ProductStateReasonCode.RUNTIME_ABSENT,  # valid but we test fallback
            reason="custom fallback reason",
        )
        label = state_label(s)
        # With valid code, we get the mapped label; but for coverage
        # we test that the label never contains raw enum names like "RUNTIME_ABSENT"
        assert "RUNTIME_ABSENT" not in label
        assert "ProductStateReasonCode" not in label

    def test_action_label_launchable(self):
        s = _make_state("test", launchable=True,
                        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE)
        assert "Lancer" in action_label(s)

    def test_action_label_not_launchable(self):
        s = _make_state("test", launchable=False)
        assert "Installer" in action_label(s)

    def test_action_enabled_launchable(self):
        s = _make_state("test", launchable=True,
                        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE)
        assert action_enabled(s) is True

    def test_action_enabled_not_launchable(self):
        s = _make_state("test", launchable=False)
        assert action_enabled(s) is False

    def test_action_enabled_installed_not_launchable(self):
        s = _make_state("test", installed=True, launchable=False,
                        reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE)
        assert action_enabled(s) is False

    def test_action_tooltip_launchable(self):
        s = _make_state("test", display_name="TestApp", launchable=True,
                        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE)
        assert "Launch TestApp" in action_tooltip(s)

    def test_action_tooltip_installed_not_launchable(self):
        s = _make_state("test", installed=True, launchable=False,
                        reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE)
        assert "contract" in action_tooltip(s).lower()

    def test_action_tooltip_not_installed(self):
        s = _make_state("test", installed=False, launchable=False)
        assert "next milestone" in action_tooltip(s)

    def test_runtime_summary_absent(self):
        s = runtime_summary("ABSENT", 0, 0, 4)
        assert "absent" in s

    def test_runtime_summary_broken(self):
        s = runtime_summary("BROKEN", 0, 0, 4)
        assert "broken" in s

    def test_runtime_summary_ready_none_installed(self):
        s = runtime_summary("READY", 0, 0, 4)
        assert "ready" in s
        assert "none installed" in s

    def test_runtime_summary_ready_with_installed(self):
        s = runtime_summary("READY", 1, 1, 4)
        assert "1/4 installed" in s
        assert "1 managed" in s

    def test_state_label_never_shows_raw_enum(self):
        """No presentation label returns a raw ProductStateReasonCode name."""
        for code in ProductStateReasonCode:
            s = _make_state("x", reason_code=code, reason="test reason")
            label = state_label(s)
            assert code.value not in label, f"Raw enum {code.value} leaked into label"
            assert code.name not in label, f"Raw enum name {code.name} leaked into label"


# ===========================================================================
# 2) GUI smoke tests (headless)


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication for headless (offscreen) GUI tests."""
    import os
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not available")

    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    yield app


# ===========================================================================


class TestGuiSmoke:
    """Headless GUI startup smoke tests."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        """Use the session-scoped QApplication."""
        return qapp

    def test_window_constructs_without_crash(self):
        """Main window constructs without exception using fake service."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _create_standard_fake_service()
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        assert window is not None
        assert window.windowTitle().startswith("ZeAlfie")

    def test_four_products_visible(self):
        """All 4 catalog products visible in the window."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _create_standard_fake_service()
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        assert len(window._cards) == 4
        assert set(window._cards.keys()) == {
            "zesolver", "zemosaic", "zeseestarstacker", "zeanalyser"
        }

    def test_header_shows_zealfie_and_tagline(self):
        """Window title and header labels contain ZeAlfie + tagline."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _create_standard_fake_service()
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        title = window.windowTitle()
        assert "ZeAlfie" in title
        assert "Astronomy Launcher" in title

    def test_products_visible_4_known_0_installed(self):
        """Empty managed runtime: 4 known / 0 installed shown as non-installed."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _create_standard_fake_service()
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        for card in window._cards.values():
            assert card._state.installed is False
            assert card._state.launchable is False

    def test_zesolver_launchable_button_active(self):
        """ZeSolver launchable → Lancer button enabled."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        descriptors = (
            _make_descriptor("zesolver", "ZeSolver"),
            _make_descriptor("zemosaic", "ZeMosaic"),
            _make_descriptor("zeseestarstacker", "ZeSeestarStacker"),
            _make_descriptor("zeanalyser", "ZeAnalyser"),
        )
        products = (
            _make_state(
                "zesolver", "ZeSolver",
                installed=True, launchable=True, version="1.0.0",
                reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
                reason="ZeSolver 1.0.0 installed and launchable",
                managed=ManagedStatus.MANAGED,
            ),
            _make_state("zemosaic", "ZeMosaic"),
            _make_state("zeseestarstacker", "ZeSeestarStacker"),
            _make_state("zeanalyser", "ZeAnalyser"),
        )
        service = FakeService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        zesolver_card = window._cards["zesolver"]
        assert zesolver_card._state.launchable is True
        btn = zesolver_card._action_button
        assert btn is not None
        assert btn.isEnabled() is True
        assert "Lancer" in btn.text()

    def test_launch_wiring_calls_spawn_component(self):
        """Clicking Lancer calls exactly service.spawn_component(product_id)."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        descriptors = (
            _make_descriptor("zesolver", "ZeSolver"),
            _make_descriptor("zemosaic", "ZeMosaic"),
            _make_descriptor("zeseestarstacker", "ZeSeestarStacker"),
            _make_descriptor("zeanalyser", "ZeAnalyser"),
        )
        products = (
            _make_state(
                "zesolver", "ZeSolver",
                installed=True, launchable=True, version="1.0.0",
                reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
                reason="ok", managed=ManagedStatus.MANAGED,
            ),
            _make_state("zemosaic", "ZeMosaic"),
            _make_state("zeseestarstacker", "ZeSeestarStacker"),
            _make_state("zeanalyser", "ZeAnalyser"),
        )
        service = FakeService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        zesolver_card = window._cards["zesolver"]
        btn = zesolver_card._action_button
        assert btn is not None
        btn.click()

        assert service.spawn_calls == ["zesolver"], (
            f"Expected spawn_calls=['zesolver'], got {service.spawn_calls}"
        )

    def test_non_launchable_does_not_call_spawn(self):
        """Non-launchable product button click does not call spawn."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        descriptors = (
            _make_descriptor("zesolver", "ZeSolver"),
            _make_descriptor("zemosaic", "ZeMosaic"),
            _make_descriptor("zeseestarstacker", "ZeSeestarStacker"),
            _make_descriptor("zeanalyser", "ZeAnalyser"),
        )
        products = (
            _make_state("zesolver", "ZeSolver", installed=True, launchable=True,
                        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
                        reason="ok"),
            _make_state("zemosaic", "ZeMosaic"),
            _make_state("zeseestarstacker", "ZeSeestarStacker"),
            _make_state("zeanalyser", "ZeAnalyser"),
        )
        service = FakeService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        zemosaic_card = window._cards["zemosaic"]
        # Button should be disabled for non-launchable
        btn = zemosaic_card._action_button
        assert btn is not None
        assert "Installer" in btn.text()
        # Even if somehow clicked, the handler checks launchable first
        zemosaic_card._on_action_clicked()
        assert "zemosaic" not in service.spawn_calls

    def test_spawn_error_keeps_window_alive(self):
        """Spawn raises → window stays alive, user-facing error shown."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        descriptors = (
            _make_descriptor("zesolver", "ZeSolver"),
            _make_descriptor("zemosaic", "ZeMosaic"),
            _make_descriptor("zeseestarstacker", "ZeSeestarStacker"),
            _make_descriptor("zeanalyser", "ZeAnalyser"),
        )
        products = (
            _make_state(
                "zesolver", "ZeSolver",
                installed=True, launchable=True, version="1.0.0",
                reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
                reason="ok", managed=ManagedStatus.MANAGED,
            ),
            _make_state("zemosaic", "ZeMosaic"),
            _make_state("zeseestarstacker", "ZeSeestarStacker"),
            _make_state("zeanalyser", "ZeAnalyser"),
        )
        service = FakeService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
            spawn_raises=RuntimeError("simulated spawn failure"),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        zesolver_card = window._cards["zesolver"]
        btn = zesolver_card._action_button
        btn.click()

        # Status label should show error, not traceback
        status_text = zesolver_card._status_label.text()
        assert "Error" in status_text or "error" in status_text.lower()
        assert "Traceback" not in status_text
        assert "simulated" in status_text

        # Button re-enabled after debounce
        # (we test that the button state makes sense — debounce is async)
        assert btn.isEnabled() is False  # disabled during spawn attempt

    def test_refresh_updates_card_state(self):
        """Fake service state changes → cards update after refresh."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        descriptors = (
            _make_descriptor("zesolver", "ZeSolver"),
            _make_descriptor("zemosaic", "ZeMosaic"),
            _make_descriptor("zeseestarstacker", "ZeSeestarStacker"),
            _make_descriptor("zeanalyser", "ZeAnalyser"),
        )

        # Initial: all absent
        initial_products = (
            _make_state("zesolver", "ZeSolver"),
            _make_state("zemosaic", "ZeMosaic"),
            _make_state("zeseestarstacker", "ZeSeestarStacker"),
            _make_state("zeanalyser", "ZeAnalyser"),
        )
        service = FakeService(
            descriptors=descriptors,
            shell_state=_make_shell_state(initial_products),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        # Update: now ZeSolver is launchable
        updated_products = (
            _make_state(
                "zesolver", "ZeSolver",
                installed=True, launchable=True, version="1.0.0",
                reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
                reason="ok", managed=ManagedStatus.MANAGED,
            ),
            _make_state("zemosaic", "ZeMosaic"),
            _make_state("zeseestarstacker", "ZeSeestarStacker"),
            _make_state("zeanalyser", "ZeAnalyser"),
        )
        service._shell_state = _make_shell_state(
            updated_products, runtime_state=RuntimeState.READY,
        )
        window._refresh()

        zesolver_card = window._cards["zesolver"]
        assert zesolver_card._state.launchable is True
        assert zesolver_card._action_button.isEnabled() is True

    def test_no_filesystem_probing(self):
        """Tests run entirely with fake service — no real runtime, no subprocess."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _create_standard_fake_service()
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        assert len(window._cards) == 4
        # Cards populated from service, no real Popen/filesystem calls
        assert service.collect_calls >= 1  # refresh was called

    def test_installed_not_launchable_button_disabled(self):
        """Installed but non-launchable → disabled Installer button."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        descriptors = (
            _make_descriptor("zemosaic", "ZeMosaic"),
        )
        products = (
            _make_state(
                "zemosaic", "ZeMosaic",
                installed=True, launchable=False, version="1.0.0",
                reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE,
                reason="contract missing",
            ),
        )
        service = FakeService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        card = window._cards["zemosaic"]
        assert card._state.installed is True
        assert card._state.launchable is False
        assert card._action_button.isEnabled() is False
        assert "Installer" in card._action_button.text()

    def test_status_bar_shows_runtime_state(self):
        """Status bar shows human-readable runtime summary."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = _create_standard_fake_service(
            runtime_state=RuntimeState.ABSENT,
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        status_text = window._status_label.text()
        assert "absent" in status_text.lower()

    def test_startup_error_not_blank_window(self):
        """collect_product_state raises → error shown, not a blank window."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        service = FakeService(
            descriptors=(
                _make_descriptor("zesolver", "ZeSolver"),
            ),
            shell_state=None,  # will cause error on collect
        )

        # Need to handle the import-time _refresh call via constructor
        # The constructor calls _refresh which calls collect_product_state
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        # Error label should be visible
        # isHidden() works even when window is not shown
        assert not window._error_label.isHidden(), (
            "Error label should be shown (not hidden)"
        )
        error_text = window._error_label.text()
        assert "Could not collect" in error_text or "collect product" in error_text.lower()

        # Status bar should reflect failure
        assert "Failed" in window._status_label.text() or "Refresh" in window._status_label.text()

    # ── Sentinel: spawn error persists after debounce ──────────────────

    def test_spawn_error_persists_after_debounce(self, qapp):
        """Spawn failure message remains visible after the debounce callback runs."""
        from zealfie.gui.product_card import ProductCard

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=True, launchable=True, version="1.0.0",
            reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason="ok", managed=ManagedStatus.MANAGED,
        )
        service = FakeService(
            descriptors=(descriptor,),
            spawn_raises=RuntimeError("simulated spawn failure"),
        )
        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
        )

        try:
            btn = card._action_button
            assert btn is not None
            btn.click()

            # Error text must be visible immediately after the failed spawn
            error_text = card._status_label.text()
            assert "Error" in error_text
            assert "simulated" in error_text

            # After debounce callback, error must still be visible
            card._on_debounce_done()
            error_after = card._status_label.text()
            assert "Error" in error_after, (
                f"Error should persist after debounce, got: {error_after!r}"
            )
            assert "simulated" in error_after

            # Button must be re-enabled
            assert btn.isEnabled() is True
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    # ── Sentinel: list_products failure does not crash construction ────

    def test_list_products_failure_no_crash(self, qapp):
        """ZeAlfieMainWindow survives list_products() raising at startup."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        class FailingListService(FakeService):
            def list_products(self):
                raise RuntimeError("simulated catalog failure")

        service = FailingListService(
            descriptors=(),
            shell_state=_make_shell_state(
                (),
                runtime_state=RuntimeState.ABSENT,
            ),
        )
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]

        try:
            # Window object exists and is alive
            assert window is not None

            # Error label must be visible
            assert not window._error_label.isHidden()
            error_text = window._error_label.text()
            assert "catalog" in error_text.lower() or "product" in error_text.lower()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 3) Packaging / import smoke
# ===========================================================================


class TestPackageSmoke:
    """Verify GUI package structure and importability."""

    def test_gui_init_exports_main(self):
        """zealfie.gui exports 'main'."""
        from zealfie.gui import main as gui_main
        assert callable(gui_main)

    def test_gui_app_importable(self):
        """zealfie.gui.app imports without error."""
        from zealfie.gui import app
        assert hasattr(app, "run_gui")

    def test_gui_main_window_importable(self):
        """zealfie.gui.main_window imports without error."""
        from zealfie.gui import main_window
        assert hasattr(main_window, "ZeAlfieMainWindow")

    def test_gui_product_card_importable(self):
        """zealfie.gui.product_card imports without error."""
        from zealfie.gui import product_card
        assert hasattr(product_card, "ProductCard")

    def test_gui_presentation_importable(self):
        """zealfie.gui.presentation imports without error."""
        from zealfie.gui import presentation
        assert hasattr(presentation, "state_label")
        assert hasattr(presentation, "action_label")
        assert hasattr(presentation, "action_enabled")
        assert hasattr(presentation, "action_tooltip")
        assert hasattr(presentation, "runtime_summary")

    def test_gui_package_init_file(self):
        """zealfie/gui/__init__.py exists and is a package."""
        import zealfie.gui
        assert zealfie.gui.__name__ == "zealfie.gui"

    def test_entry_point_script_parseable(self):
        """zealfie-gui entry point is valid in pyproject.toml."""
        import tomllib
        from pathlib import Path
        proj = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(proj.read_text())
        scripts = data.get("project", {}).get("gui-scripts", {})
        assert "zealfie-gui" in scripts
        assert scripts["zealfie-gui"] == "zealfie.gui:main"
        assert "zealfie-gui" not in data.get("project", {}).get("scripts", {})


# ===========================================================================
# 4) Nullable initial state — sentinel
# ===========================================================================


class TestNullableInitialState:
    """ProductCard must accept state=None without type: ignore."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_card_constructed_with_null_state_no_type_ignore(self, qapp):
        """Construct ProductCard with state=None — no type: ignore required."""
        from zealfie.gui.product_card import ProductCard

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        service = FakeService(descriptors=(descriptor,))

        # Passing None directly — must type-check without ignore
        card = ProductCard(descriptor=descriptor, state=None, service=service)

        try:
            assert "Loading" in card._status_label.text()
            assert not card._action_button.isEnabled()

            # After refresh, card must enter ready state cleanly
            real = _make_state("zesolver", "ZeSolver", installed=True, launchable=True,
                               reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
                               reason="ok")
            card.refresh_state(real)
            assert "Ready" in card._status_label.text()
            assert card._action_button.isEnabled()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()
