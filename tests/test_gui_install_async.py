"""Tests for M1-2D.5 — non-blocking async install with honest UX feedback.

Verifies the full architecture:
- ProductCard emits install_requested, never calls install_product directly
- MainWindow coordinates installs via QThread worker
- One install at a time; UI lockout; responsive event loop
- Success/failure/refresh-failure paths
- Close policy during active install

All tests run headless via ``QT_QPA_PLATFORM=offscreen``.
No real network, no true runtime, no subprocess.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# PySide6 import guarded — test session will fail cleanly if missing.
try:
    from PySide6.QtCore import QTimer, QThread, Signal, QObject
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.app import (
    ManagedStatus,
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    SpawnedLaunch,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import (
    DeploymentResult,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

# ===========================================================================
# Helpers — duplicates from test_gui.py for module independence
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
    runtime_state: RuntimeState = RuntimeState.READY,
    runtime_root: Path = Path("/fake/runtime"),
) -> ProductShellState:
    return ProductShellState(
        runtime_state=runtime_state,
        runtime_root=runtime_root,
        products=products,
    )


# ===========================================================================
# Fake services with thread-safe blocking support
# ===========================================================================


class FakeInstallService:
    """Fake ZeAlfieService that records install_product calls.

    Supports blocking (simulated long install) via ``block_seconds``,
    and post-install state switching via ``post_install_shell_state``.
    """

    def __init__(
        self,
        descriptors: tuple[ProductDescriptor, ...] = (),
        shell_state: ProductShellState | None = None,
        *,
        install_result: DeploymentResult | None = None,
        install_raises: Exception | None = None,
        post_install_shell_state: ProductShellState | None = None,
        block_seconds: float = 0.0,
        collect_raises: Exception | None = None,
        collect_raises_after: int = 0,
    ) -> None:
        self._descriptors = descriptors
        self._shell_state = shell_state
        self._install_result = install_result
        self._install_raises = install_raises
        self._post_install_shell_state = post_install_shell_state
        self._block_seconds = block_seconds
        self._collect_raises = collect_raises
        self._collect_raises_after = collect_raises_after
        self.install_calls: list[dict] = []
        self.spawn_calls: list[str] = []
        self.collect_calls: int = 0

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        return self._descriptors

    def collect_product_state(self) -> ProductShellState:
        self.collect_calls += 1
        if self._collect_raises and self.collect_calls > self._collect_raises_after:
            raise self._collect_raises
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
        return SpawnedLaunch(component_id="fake", pid=99999)

    def install_product(
        self, product_id, *, resolver=None, fetcher=None, work_root=None,
        dependency_wheelhouse=None, probe_distribution=None,
    ):
        self.install_calls.append({
            "product_id": product_id,
            "resolver": resolver,
            "fetcher": fetcher,
            "work_root": work_root,
        })
        if self._block_seconds > 0:
            time.sleep(self._block_seconds)
        if self._install_raises:
            raise self._install_raises
        result = self._install_result
        if result is None:
            result = DeploymentResult(success=True, active_slot_id="rt-fake-slot")
        if result.success and self._post_install_shell_state is not None:
            self._shell_state = self._post_install_shell_state
        return result


# ===========================================================================
# Shared QApplication fixture
# ===========================================================================


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
# Helper – process events until condition or timeout
# ===========================================================================


def _wait_for(
    qapp,
    condition,
    timeout_ms: int = 5000,
    interval_ms: int = 50,
) -> bool:
    """Spin the Qt event loop until ``condition()`` is True or timeout."""
    elapsed = 0
    while not condition() and elapsed < timeout_ms:
        qapp.processEvents()
        QThread.msleep(interval_ms)
        elapsed += interval_ms
    return condition()


# ===========================================================================
# 1) ProductCard signal emission (no MainWindow)
# ===========================================================================


class TestProductCardInstallRequested:
    """ProductCard emits install_requested and does NOT call install_product."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _fake_deps(self):
        return (
            lambda o, r, ref: "a" * 40,
            lambda o, r, sha: b"zip",
            Path("/tmp/fake-work"),
        )

    def test_click_emits_install_requested_not_install_product(self, qapp):
        """Clicking Installer emits install_requested, does NOT call
        service.install_product() directly."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()
        desc = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(desc,))

        signals: list[str] = []
        card = ProductCard(
            descriptor=desc, state=state, service=service,
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        card.install_requested.connect(lambda pid: signals.append(pid))
        try:
            btn = card._action_button
            btn.click()

            assert signals == ["zesolver"], f"Expected signal, got {signals}"
            assert len(service.install_calls) == 0, (
                "ProductCard MUST NOT call install_product directly"
            )
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_set_install_in_progress_updates_ui(self, qapp):
        """set_install_in_progress(True) shows 'Installation…' text."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()
        desc = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(desc,))

        card = ProductCard(
            descriptor=desc, state=state, service=service,
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card.set_install_in_progress(True)
            btn = card._action_button
            assert "Installation" in btn.text()
            assert btn.isEnabled() is False
            status = card._status_label.text()
            assert "Installation de ZeSolver en cours" in status

            card.set_install_in_progress(False)
            card.refresh_state(state)
            assert btn.isEnabled() is True
            assert "Installer" in btn.text()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_set_install_error_no_traceback(self, qapp):
        """set_install_error shows friendly message, no traceback."""
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = self._fake_deps()
        desc = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state(
            "zesolver", "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        service = FakeInstallService(descriptors=(desc,))

        card = ProductCard(
            descriptor=desc, state=state, service=service,
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card.set_install_error("simulated install failure")
            status = card._status_label.text()
            assert "Traceback" not in status
            assert "simulated" in status
            assert card._action_button.isEnabled() is True  # retry allowed
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 2) MainWindow install coordination (worker thread)
# ===========================================================================


class TestMainWindowAsyncInstall:
    """MainWindow coordinates non-blocking install via QThread worker."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _fake_deps(self):
        return (
            lambda o, r, ref: "a" * 40,
            lambda o, r, sha: b"zip",
            Path("/tmp/fake-install-work"),
        )

    # ── 1. Worker runs off UI thread, event loop responsive ──────────

    def test_worker_runs_off_ui_thread_event_loop_responsive(self, qapp):
        """While fake install blocks in worker thread, the Qt event loop
        stays responsive (a timer fires)."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        # Block for 0.3s in the fake install — worker thread, not UI
        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.3,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            timer_fired = []

            def _on_timer():
                timer_fired.append(True)

            t = QTimer(window)
            t.setSingleShot(True)
            t.timeout.connect(_on_timer)
            t.start(100)  # 100ms timer

            # Click install — this starts worker with 300ms block
            card = window._cards[pid]
            card._action_button.click()

            # Wait for install to complete
            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok, "Install did not complete within timeout"

            # Timer MUST have fired — proves event loop was responsive
            assert len(timer_fired) == 1, (
                "Timer should have fired while worker was blocking on other thread"
            )

            # install_product was called exactly once
            assert len(service.install_calls) == 1
            assert service.install_calls[0]["product_id"] == pid
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 2. Single-install guard: second click ignored ────────────────

    def test_double_click_only_one_install(self, qapp):
        """A second install click while one is active does NOT start a
        second service call."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()

        descs = (
            _make_descriptor("zesolver", "ZeSolver"),
            _make_descriptor("zemosaic", "ZeMosaic"),
        )
        products = (
            _make_state(
                "zesolver", "ZeSolver",
                installed=False, launchable=False,
                reason_code=ProductStateReasonCode.NOT_INSTALLED,
                reason="not installed",
            ),
            _make_state(
                "zemosaic", "ZeMosaic",
                installed=False, launchable=False,
                reason_code=ProductStateReasonCode.NOT_INSTALLED,
                reason="not installed",
            ),
        )
        service = FakeInstallService(
            descriptors=descs,
            shell_state=_make_shell_state(products),
            block_seconds=0.5,  # slow enough to test double-click
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            # Click first card
            card1 = window._cards["zesolver"]
            card1._action_button.click()

            # Install must be active now
            assert window.install_active is True

            # Click second card immediately
            card2 = window._cards["zemosaic"]
            card2._action_button.click()

            # Wait for completion
            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok, "Install did not complete within timeout"

            # Only ONE install_product call
            assert len(service.install_calls) == 1, (
                f"Expected 1 install call, got {len(service.install_calls)}"
            )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 3. Refresh disabled during install ────────────────────────────

    def test_refresh_disabled_during_install(self, qapp):
        """Refresh/F5 is disabled while install is active; _refresh() is
        not called mid-install."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.3,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            collect_before = service.collect_calls

            card = window._cards[pid]
            card._action_button.click()

            # Refresh action must be disabled
            assert window._refresh_action.isEnabled() is False, (
                "Refresh action should be disabled during install"
            )

            # Try calling _refresh() directly — must be blocked
            window._refresh()
            assert service.collect_calls == collect_before, (
                "_refresh() should not call collect_product_state during install"
            )

            # Wait for completion
            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            # Refresh now re-enabled
            assert window._refresh_action.isEnabled() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 4. Progress bar visible during install ────────────────────────

    def test_progress_bar_visible_during_install(self, qapp):
        """Indeterminate QProgressBar is visible while install is active."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.2,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            # Progress bar hidden initially
            assert window._install_progress_bar.isHidden() is True, (
                "Progress bar should be hidden before install"
            )

            card = window._cards[pid]
            card._action_button.click()

            # Progress bar must be visible during install
            assert window._install_progress_bar.isHidden() is False, (
                "Progress bar should be visible during install"
            )
            assert window._install_progress_bar.maximum() == 0, (
                "Progress bar should be indeterminate (max=0)"
            )

            # Known limitation visible
            assert window._known_limitation_label.isHidden() is False

            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            # Hidden after completion
            qapp.processEvents()
            assert window._install_progress_bar.isHidden() is True, (
                "Progress bar should be hidden after install"
            )
            assert window._known_limitation_label.isHidden() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 5. Success path: refresh called exactly once ──────────────────

    def test_success_path_calls_refresh_once(self, qapp):
        """Successful install calls collect_product_state exactly once
        (in _on_worker_success), and the post-install state is applied."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        post_state = _make_state(
            pid, "ZeSolver",
            installed=True, launchable=True, version="1.0.0",
            reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason="ok", managed=ManagedStatus.MANAGED,
        )
        post_shell = _make_shell_state((post_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            post_install_shell_state=post_shell,
            block_seconds=0.1,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            # Initial collect from constructor refresh
            initial_collects = service.collect_calls
            assert initial_collects >= 1

            card = window._cards[pid]
            card._action_button.click()

            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            # collect_product_state called exactly once more (the post-success refresh)
            assert service.collect_calls == initial_collects + 1, (
                f"Expected {initial_collects + 1} collect calls, got {service.collect_calls}"
            )

            # Card must show post-install state
            assert card._state.installed is True
            assert card._state.launchable is True
            assert "Lancer" in card._action_button.text()
            assert card._action_button.isEnabled() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 6. Failure path: safe error, retry possible ──────────────────

    def test_failure_path_safe_error_no_traceback(self, qapp):
        """Failed install shows safe error on card, no traceback,
        and retry is possible."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            install_result=DeploymentResult(
                success=False, reason="deployment plan blocked",
            ),
            block_seconds=0.1,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card = window._cards[pid]
            card._action_button.click()

            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            status = card._status_label.text()
            assert "failed" in status.lower()
            assert "blocked" in status.lower()
            assert "Traceback" not in status

            # Retry is possible — button enabled
            assert card._action_button.isEnabled() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_failure_exception_path_retry_possible(self, qapp):
        """Exception during install: safe error, retry possible, no traceback."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            install_raises=RuntimeError("simulated install failure"),
            block_seconds=0.1,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card = window._cards[pid]
            card._action_button.click()

            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            status = card._status_label.text()
            assert "Traceback" not in status
            assert "simulated" in status
            assert card._action_button.isEnabled() is True  # retry
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 7. Refresh failure after successful install ──────────────────

    def test_refresh_failure_after_success_shows_safe_fallback(self, qapp):
        """If refresh fails after a successful install, cards show
        'Installation complete — refresh required' and install stays disabled."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        # Install succeeds but subsequent collect raises
        # collect_raises_after=1: first call (_refresh in __init__) works,
        # second call (post-install _refresh) raises.
        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.1,
            collect_raises=RuntimeError("simulated refresh failure"),
            collect_raises_after=1,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card = window._cards[pid]
            card._action_button.click()

            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            status = card._status_label.text()
            assert "refresh required" in status.lower(), (
                f"Expected 'refresh required', got {status!r}"
            )
            # Status bar shows error
            assert "Refresh failed" in window._status_label.text()

            # Button stays disabled — no resurrected Installer state
            assert card._action_button.isEnabled() is False, (
                "Button should stay disabled after refresh failure"
            )
            # Button text stays in installation state — honest UX:
            # we don't fabricate a fake "Installer" state.
            assert "Installation" in card._action_button.text(), (
                "Button text should reflect installation progress, not fake state"
            )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 8. Close rejected during active install ──────────────────────

    def test_close_rejected_during_active_install(self, qapp):
        """closeEvent is rejected while install is active; thread is NOT
        terminated. After worker finishes, close can proceed."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.5,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card = window._cards[pid]
            card._action_button.click()

            assert window.install_active is True

            # Try to close — must be rejected
            from PySide6.QtGui import QCloseEvent
            close_event = QCloseEvent()
            window.closeEvent(close_event)
            assert close_event.isAccepted() is False, (
                "Close event should be rejected during active install"
            )

            # Status bar shows the message
            assert "please wait" in window._status_label.text().lower()

            # Wait for install to finish
            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            # Now close should succeed
            close_event2 = QCloseEvent()
            window.closeEvent(close_event2)
            assert close_event2.isAccepted() is True, (
                "Close event should be accepted after install completes"
            )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 9. No QThread destroyed while running ─────────────────────────

    def test_no_thread_destroyed_while_running(self, qapp):
        """After install completes, thread is cleaned up with quit → wait
        → deleteLater. No 'destroyed while running' warning."""
        from zealfie.gui.main_window import ZeAlfieMainWindow
        import warnings

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.1,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            # Collect any warnings during test
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")

                card = window._cards[pid]
                card._action_button.click()

                ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
                assert ok

                # Give the lambda cleanup a moment to fire
                qapp.processEvents()
                # Thread should be cleaned up by now
                assert window.install_active is False
                assert window._install_thread is None, (
                    f"Thread not cleaned up: {window._install_thread}"
                )
                assert window._install_worker is None, (
                    f"Worker not cleaned up: {window._install_worker}"
                )

                thread_warnings = [
                    x for x in w
                    if "destroyed" in str(x.message).lower() and "thread" in str(x.message).lower()
                ]
                assert len(thread_warnings) == 0, (
                    f"Thread destroyed while running warning detected: {thread_warnings}"
                )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 10. Second install after first completes ─────────────────────

    def test_second_install_after_first_completes(self, qapp):
        """After a successful install completes and cleanup is done, a
        second install can proceed."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        # post-install state resets to not-installed (simulating a re-install scenario)
        post_state = _make_state(
            pid, "ZeSolver",
            installed=True, launchable=True, version="1.0.0",
            reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason="ok", managed=ManagedStatus.MANAGED,
        )
        post_shell = _make_shell_state((post_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            post_install_shell_state=post_shell,
            block_seconds=0.1,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card = window._cards[pid]
            # First install
            card._action_button.click()
            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok
            assert len(service.install_calls) == 1

            # Reset state for second install
            second_pre_state = _make_state(
                pid, "ZeSolver",
                installed=False, launchable=False,
                reason_code=ProductStateReasonCode.NOT_INSTALLED,
                reason="not installed",
            )
            second_pre_shell = _make_shell_state((second_pre_state,))
            service._shell_state = second_pre_shell

            # Second install — card needs to be in not-installed state
            card.refresh_state(second_pre_state)
            assert "Installer" in card._action_button.text()

            card._action_button.click()
            ok2 = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok2

            # Two install calls total
            assert len(service.install_calls) == 2
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    # ── 11. Known limitation text visible ────────────────────────────

    def test_known_limitation_text_visible_during_install(self, qapp):
        """'KNOWN UX LIMITATION' text is visible during active install."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = self._fake_deps()
        pid = "zesolver"

        pre_state = _make_state(
            pid, "ZeSolver",
            installed=False, launchable=False,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
        )
        pre_shell = _make_shell_state((pre_state,))

        service = FakeInstallService(
            descriptors=(_make_descriptor(pid, "ZeSolver"),),
            shell_state=pre_shell,
            block_seconds=0.2,
        )

        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card = window._cards[pid]
            card._action_button.click()

            # Known limitation label must be visible
            assert window._known_limitation_label.isHidden() is False
            assert "cannot yet be cancelled" in window._known_limitation_label.text().lower()

            ok = _wait_for(qapp, lambda: not window.install_active, timeout_ms=3000)
            assert ok

            # Hidden after completion
            qapp.processEvents()
            assert window._known_limitation_label.isHidden() is True
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
