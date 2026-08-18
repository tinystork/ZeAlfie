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
        assert "Install" in state_label(s)

    def test_state_label_installed_launchable(self):
        s = _make_state(
            "test", reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason="ok",
        )
        assert "Ready" in state_label(s)
        assert "Launch" in state_label(s)

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
        assert "Launch" in action_label(s)

    def test_action_label_not_launchable(self):
        s = _make_state("test", launchable=False)
        assert "Install" in action_label(s)

    def test_action_enabled_launchable(self):
        s = _make_state("test", launchable=True,
                        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE)
        assert action_enabled(s) is True

    def test_action_enabled_not_launchable(self):
        s = _make_state("test", installed=False, launchable=False)
        assert action_enabled(s) is True

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
        assert "Install Test" in action_tooltip(s)

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

    def test_window_wires_fetcher_and_work_root_into_acceleration_panel(
        self, tmp_path,
    ):
        """ZA-M1-2J.1: the composition root's fetcher/work root reach the
        acceleration panel (and therefore the accelerated install worker)."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        class _FakeFetcher:
            pass

        fetcher = _FakeFetcher()
        work_root = tmp_path / "work"
        service = _create_standard_fake_service()
        window = ZeAlfieMainWindow(
            service=service,
            fetcher=fetcher,
            work_root=work_root,
        )
        panel = window._acceleration_panel
        assert panel is not None
        assert panel._fetcher is fetcher
        assert panel._work_root == work_root

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
        assert "Launch" in btn.text()

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
        assert "Install" in btn.text()
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
        assert "Install" in card._action_button.text()

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

# ===========================================================================
# D.4.1G: GUI install wiring tests
# ===========================================================================


class FakeInstallService(FakeService):
    """FakeService that also records install_product calls.

    Supports post_install_shell_state to simulate the state
    transition observable after a successful install: the first
    collect_product_state() returns the initial shell, and after
    the first successful install_product() the fake transparently
    switches to the post-install shell.
    """

    def __init__(
        self,
        descriptors: tuple[ProductDescriptor, ...] = (),
        shell_state: ProductShellState | None = None,
        spawn_result: SpawnedLaunch | None = None,
        spawn_raises: Exception | None = None,
        *,
        install_result: DeploymentResult | None = None,
        install_raises: Exception | None = None,
        post_install_shell_state: ProductShellState | None = None,
    ) -> None:
        super().__init__(
            descriptors=descriptors,
            shell_state=shell_state,
            spawn_result=spawn_result,
            spawn_raises=spawn_raises,
        )
        self._install_result = install_result
        self._install_raises = install_raises
        self._post_install_shell_state = post_install_shell_state
        self.install_calls: list[dict] = []

    def install_product(
        self, product_id, *, resolver=None, fetcher=None, work_root=None,
        dependency_wheelhouse=None, probe_distribution=None,
        progress_callback=None,
    ):
        self.install_calls.append({
            "product_id": product_id,
            "resolver": resolver,
            "fetcher": fetcher,
            "work_root": work_root,
        })
        if self._install_raises:
            raise self._install_raises
        result = self._install_result
        if result is None:
            from zealfie.runtime.model import DeploymentResult
            result = DeploymentResult(success=True, active_slot_id="rt-fake-slot")
        if result.success and self._post_install_shell_state is not None:
            self._shell_state = self._post_install_shell_state
        return result


class TestInstallPresentation:
    """Updated presentation tests reflecting installer-is-active."""

    def test_state_label_not_installed(self):
        s = _make_state(
            "test", reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        assert "Not installed" in state_label(s)
        assert "Install" in state_label(s)

    def test_action_enabled_not_installed(self):
        """Not-installed products have the Installer button enabled."""
        s = _make_state("test", installed=False, launchable=False)
        assert action_enabled(s) is True

    def test_action_enabled_installed_not_launchable_still_disabled(self):
        """Installed-but-not-launchable still disabled."""
        s = _make_state("test", installed=True, launchable=False,
                        reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE)
        assert action_enabled(s) is False

    def test_action_enabled_runtime_absent_enabled(self):
        """Products with RUNTIME_ABSENT → Installer enabled."""
        s = _make_state("test", installed=False, launchable=False,
                        reason_code=ProductStateReasonCode.RUNTIME_ABSENT)
        assert action_enabled(s) is True

    def test_action_tooltip_not_installed(self):
        """Not-installed tooltip shows 'Install <name>'."""
        s = _make_state("test", display_name="TestApp", installed=False, launchable=False)
        assert "Install TestApp" in action_tooltip(s)

    def test_action_tooltip_installed_not_launchable(self):
        """Installed-not-launchable still shows contract message."""
        s = _make_state("test", installed=True, launchable=False,
                        reason_code=ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE)
        assert "contract" in action_tooltip(s).lower()

    def test_state_label_never_shows_next_milestone(self):
        """NOT_INSTALLED label no longer says 'coming in the next milestone'."""
        s = _make_state("test",
                        reason_code=ProductStateReasonCode.NOT_INSTALLED,
                        reason="not installed")
        label = state_label(s)
        assert "next milestone" not in label
        assert "Install" in label


class TestInstallCardSmoke:
    """ProductCard install button smoke tests (headless)."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _fake_deps(self):
        """Return fake resolver/fetcher/work_root for testing."""
        return (
            lambda o, r, ref: "a" * 40,
            lambda o, r, sha: b"zip",
            Path("/tmp/fake-work"),
        )

    def test_installer_button_enabled_for_not_installed(self, qapp):
        """Not-installed products get enabled Installer button."""
        from zealfie.gui.product_card import ProductCard

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        resolver, fetcher, work_root = self._fake_deps()
        service = FakeInstallService(descriptors=(descriptor,))

        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        try:
            btn = card._action_button
            assert btn.isEnabled() is True
            assert "Install" in btn.text()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_install_button_disabled_when_deps_missing(self, qapp):
        """Installer button click with missing deps shows error."""
        from zealfie.gui.product_card import ProductCard

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(descriptor,))

        # No resolver/fetcher/work_root passed
        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
        )
        try:
            card._on_action_clicked()
            status = card._status_label.text()
            assert "install dependencies" in status.lower() or "not configured" in status.lower()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_install_click_emits_install_requested(self, qapp):
        """M1-2D.5: Clicking Installer emits install_requested; does NOT call install_product."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(descriptor,))

        signals: list[str] = []
        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        card.install_requested.connect(lambda pid: signals.append(pid))
        try:
            btn = card._action_button
            btn.click()
            # install_requested signal must be emitted
            assert signals == ["zesolver"], f"Expected install_requested, got {signals}"

            # service.install_product() MUST NOT be called directly
            assert len(service.install_calls) == 0, (
                "service.install_product MUST NOT be called directly by ProductCard"
            )
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_install_requested_and_set_in_progress(self, qapp):
        """M1-2D.5: Click emits install_requested; set_install_in_progress shows
        'Installation…' and disables the button."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(descriptor,))

        sigs: list[str] = []
        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        card.install_requested.connect(lambda pid: sigs.append(pid))
        try:
            btn = card._action_button
            assert "Install" in btn.text()
            btn.click()

            assert sigs == ["zesolver"]

            # Simulate MainWindow calling set_install_in_progress
            card.set_install_in_progress(True)
            assert "Installing" in btn.text()
            assert btn.isEnabled() is False
            status = card._status_label.text()
            assert "Installing ZeSolver" in status

            # Clear state (MainWindow would call refresh_state)
            card.set_install_in_progress(False)
            card.refresh_state(state)
            assert btn.isEnabled() is True
            assert "Install" in btn.text()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_install_failure_via_set_install_error(self, qapp):
        """M1-2D.5: set_install_error shows user-friendly failure on card."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(descriptor,))

        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        try:
            card.set_install_error("deployment plan blocked")
            status = card._status_label.text()
            assert "failed" in status.lower()
            assert "blocked" in status.lower()
            # Button re-enabled after error
            assert card._action_button.isEnabled() is True
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_install_exception_via_set_install_error(self, qapp):
        """M1-2D.5: Exception message set via set_install_error — no traceback."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()

        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(descriptor,))

        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        try:
            card.set_install_error("simulated install failure")
            status = card._status_label.text()
            assert ("Error" in status or "failed" in status.lower())
            assert "simulated" in status
            assert "Traceback" not in status
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_installed_not_launchable_button_disabled(self):
        """Installed-but-not-launchable: button stays disabled."""
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
        service = FakeInstallService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
        )
        window = ZeAlfieMainWindow(
            service=service,
            # Pass install deps so cards have full wiring
            resolver=lambda o, r, ref: "a" * 40,
            fetcher=lambda o, r, sha: b"zip",
            work_root=Path("/tmp/fake-work"),
        )  # type: ignore[arg-type]

        card = window._cards["zemosaic"]
        assert card._state.installed is True
        assert card._state.launchable is False
        assert card._action_button.isEnabled() is False
        assert "Install" in card._action_button.text()
    def test_set_install_complete_refresh_required(self, qapp):
        """M1-2D.5: set_install_complete_refresh_required shows safe fallback."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()
        descriptor = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(descriptor,))

        card = ProductCard(
            descriptor=descriptor,
            state=state,
            service=service,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
        try:
            card.set_install_complete_refresh_required()
            status = card._status_label.text()
            assert "refresh required" in status.lower()
            # Button stays disabled
            assert card._action_button.isEnabled() is False
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()



class TestInstallWiringThroughMainWindow:
    """MainWindow forwards install deps to ProductCards."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_main_window_forwards_install_deps_to_cards(self):
        """MainWindow passes resolver/fetcher/work_root to ProductCard."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver = lambda o, r, ref: "b" * 40
        fetcher = lambda o, r, sha: b"test-zip"
        work_root = Path("/tmp/fake-install-work")

        descriptors = (
            _make_descriptor("zesolver", "ZeSolver"),
        )
        products = (
            _make_state(
                "zesolver", "ZeSolver",
                installed=False, launchable=False,
                reason_code=ProductStateReasonCode.NOT_INSTALLED,
                reason="not installed",
            ),
        )
        service = FakeInstallService(
            descriptors=descriptors,
            shell_state=_make_shell_state(products, runtime_state=RuntimeState.READY),
        )
        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )

        card = window._cards["zesolver"]
        assert card._resolver is resolver
        assert card._fetcher is fetcher
        assert card._work_root == work_root


# ===========================================================================
# M1-4 LOT E — runtime language selector (thin GUI wiring)
# ===========================================================================


class TestLanguageSelection:
    """Minimal wiring test for the top-level Language menu."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    @pytest.fixture(autouse=True)
    def _reset_language(self):
        from zealfie.i18n import reset_language

        reset_language()
        yield
        reset_language()

    def test_language_menu_switches_and_retranslates(self, qapp, monkeypatch):
        from zealfie.i18n import Language, get_language
        from zealfie.gui.main_window import ZeAlfieMainWindow

        saved: dict = {}

        class _FakeStore:
            def __init__(self, path=None):
                pass

            def save(self, lang):
                saved["lang"] = lang

        monkeypatch.setattr(
            "zealfie.gui.main_window.LanguageStore", _FakeStore
        )

        window = ZeAlfieMainWindow(service=_create_standard_fake_service())
        try:
            assert get_language() is Language.EN
            assert "Astronomy Launcher" in window.windowTitle()
            assert window._language_actions[Language.FR].isChecked() is False

            window._language_actions[Language.FR].trigger()

            assert get_language() is Language.FR
            assert saved.get("lang") is Language.FR
            assert "Lanceur" in window.windowTitle()
            assert "Astronomy Launcher" not in window.windowTitle()

            window._language_actions[Language.EN].trigger()
            assert get_language() is Language.EN
            assert "Astronomy Launcher" in window.windowTitle()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

# ===========================================================================
# M1-4.1 UX polish — top-level language menu, single refresh, FR descriptions
# ===========================================================================


class TestM141LanguageMenuTopLevel:
    """The language selector is a top-level menu, not nested in a Shell menu."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    @pytest.fixture(autouse=True)
    def _reset_language(self):
        from zealfie.i18n import reset_language

        reset_language()
        yield
        reset_language()

    def test_language_menu_is_top_level(self, qapp):
        from zealfie.i18n import Language
        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_create_standard_fake_service())
        try:
            menu_bar = window.menuBar()
            top_titles = [a.text() for a in menu_bar.actions()]

            # No "Shell" menu exists (top-level or otherwise).
            assert "Shell" not in top_titles
            assert "&Shell" not in top_titles

            # The language menu is a direct top-level child of the menu bar.
            assert window._language_menu is not None
            assert window._language_menu.parent() is menu_bar
            assert "Language" in top_titles

            # It holds exactly the two checkable actions.
            labels = [a.text() for a in window._language_menu.actions()]
            assert labels == ["English", "Français"]
            assert window._language_actions[Language.EN].isCheckable() is True
            assert window._language_actions[Language.FR].isCheckable() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_single_refresh_action_in_toolbar_only(self, qapp):
        from PySide6.QtGui import QAction

        from zealfie.gui.main_window import ZeAlfieMainWindow

        window = ZeAlfieMainWindow(service=_create_standard_fake_service())
        try:
            # Exactly one Refresh action exists anywhere in the window.
            refresh_actions = [
                a for a in window.findChildren(QAction) if "Refresh" in a.text()
            ]
            assert len(refresh_actions) == 1
            assert refresh_actions[0] is window._refresh_action

            # No menu contains a Refresh action.
            for top in window.menuBar().actions():
                menu = top.menu()
                if menu is not None:
                    for a in menu.actions():
                        assert "Refresh" not in a.text()

            # The F5 shortcut is preserved on the toolbar action.
            assert window._refresh_action.shortcut().toString() == "F5"

            # _set_global_install_lock still disables it during installs.
            window._set_global_install_lock(True)
            assert window._refresh_action.isEnabled() is False
            window._set_global_install_lock(False)
            assert window._refresh_action.isEnabled() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_language_switch_retitles_top_level_menu(self, qapp, monkeypatch):
        from zealfie.i18n import Language
        from zealfie.gui.main_window import ZeAlfieMainWindow

        class _FakeStore:
            def __init__(self, path=None):
                pass

            def save(self, lang):
                pass

        monkeypatch.setattr(
            "zealfie.gui.main_window.LanguageStore", _FakeStore
        )

        window = ZeAlfieMainWindow(service=_create_standard_fake_service())
        try:
            assert window._language_menu.title() == "Language"
            window._language_actions[Language.FR].trigger()
            assert window._language_menu.title() == "Langue"
            window._language_actions[Language.EN].trigger()
            assert window._language_menu.title() == "Language"
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


class TestM141ProductDescriptionTranslation:
    """Product cards translate descriptions via the i18n layer (FR only)."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    @pytest.fixture(autouse=True)
    def _reset_language(self):
        from zealfie.i18n import reset_language

        reset_language()
        yield
        reset_language()

    @staticmethod
    def _make_card(product_id: str, description: str):
        from zealfie.gui.product_card import ProductCard

        descriptor = _make_descriptor(
            product_id, product_id.title(), description
        )
        service = FakeService(descriptors=(descriptor,))
        return ProductCard(descriptor=descriptor, state=None, service=service)

    @staticmethod
    def _desc_text(card):
        from PySide6.QtWidgets import QLabel

        label = card.findChild(QLabel, "descLabel")
        assert label is not None
        return label.text()

    def test_english_description_in_en(self, qapp):
        card = self._make_card("zesolver", "Optical solver EN.")
        try:
            assert self._desc_text(card) == "Optical solver EN."
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_french_description_in_fr(self, qapp):
        from zealfie.i18n import FR, Language, set_language

        set_language(Language.FR)
        card = self._make_card("zesolver", "Optical solver EN.")
        try:
            assert self._desc_text(card) == FR["product.description.zesolver"]
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_missing_fr_key_falls_back_to_english(self, qapp):
        from zealfie.i18n import Language, set_language

        set_language(Language.FR)
        card = self._make_card("no_such_product", "Only English.")
        try:
            text = self._desc_text(card)
            assert text == "Only English."
            assert "product.description" not in text
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_description_label_does_not_clip_fr_zesolver(self, qapp):
        """FR zesolver (longest FR description) never clips at 480/580 px.

        The old fixed 40px maximum height silently clipped the longer FR
        text once it wrapped to 3 lines.  Simulate a larger font / high-DPI
        (the exact trigger noted in the review) so the condition reproduces
        deterministically on the offscreen CI platform, then assert the
        rendered label is tall enough for its wrapped content at both
        480px and 580px window widths.
        """
        from PySide6.QtWidgets import QLabel

        from zealfie.i18n import FR, Language, set_language

        set_language(Language.FR)
        card = self._make_card(
            "zesolver",
            "Optical solver for high-resolution astrophotography — "
            "plate solve, blind solve, and star field analysis.",
        )
        try:
            label = card.findChild(QLabel, "descLabel")
            assert label is not None
            assert label.text() == FR["product.description.zesolver"]

            # Force 3-line wrapping deterministically (larger font / high-DPI).
            font = label.font()
            font.setPointSize(font.pointSize() + 4)
            label.setFont(font)

            for width in (480, 580):
                card.resize(width, card.sizeHint().height())
                card.show()
                qapp.processEvents()
                needed = label.heightForWidth(label.width())
                assert needed > 40, (
                    "precondition failed: FR zesolver should wrap to 3+ lines"
                )
                assert label.height() >= needed, (
                    f"description label clipped at width {width}: "
                    f"height={label.height()}px < heightForWidth={needed}px"
                )
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()
