"""Tests for M1-2E E.6a — GUI update action wiring (pre-witness).

Verifies the minimal Product Shell update action:

* ``ProductCard`` shows an actionable "Mettre à jour" control only when
  ``UpdateStatus.UPDATE_AVAILABLE``, keeps "Lancer" as the separate primary
  action for installed+launchable products, and emits ``update_requested``
  (never calling ``service.update_product`` directly).
* ``ZeAlfieMainWindow`` coordinates the update through the exact same
  worker-thread plumbing as install, calling ``service.update_product``
  (not ``install_product``) with injected resolver/fetcher/work_root and
  progress callback.
* One global lock covers install **and** update; the update/launch buttons
  cannot start another transaction while one is active.
* Structured backend progress is relayed verbatim (no new semantics).
* Success: authoritative refresh + update display becomes "Up to date".
* Failure: error shown, lock released, launch still possible, update retryable.

All tests run headless via ``QT_QPA_PLATFORM=offscreen``.  Fakes only — no
real network, no build, no venv, no ``.smoke/`` mutation.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

try:
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.app import (
    InstallPhase,
    InstallProgress,
    ManagedStatus,
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    ProductUpdateResult,
    UpdateStatus,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import DeploymentResult, RuntimeState

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _desc(product_id: str, name: str | None = None) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=name or product_id.title(),
        distribution_name=name or product_id,
        launch_entry_points=(EntryPointContract("console_scripts", product_id),),
    )


def _state(
    product_id: str,
    *,
    installed: bool = True,
    launchable: bool = True,
    version: str | None = "1.0.0",
    managed: ManagedStatus = ManagedStatus.MANAGED,
) -> ProductState:
    return ProductState(
        product_id=product_id,
        display_name=product_id.title(),
        known=True,
        installed=installed,
        launchable=launchable,
        version=version if installed else None,
        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE
        if launchable
        else ProductStateReasonCode.NOT_INSTALLED,
        reason="ok",
        managed=managed,
    )


def _shell(products: tuple[ProductState, ...]) -> ProductShellState:
    return ProductShellState(
        runtime_state=RuntimeState.READY,
        runtime_root=Path("/fake/runtime"),
        products=products,
    )


def _result(product_id: str, status: UpdateStatus, **kwargs) -> ProductUpdateResult:
    return ProductUpdateResult(product_id=product_id, status=status, **kwargs)


def _fake_deps():
    return (
        lambda o, r, ref: "a" * 40,
        lambda o, r, sha: b"zip",
        Path("/tmp/fake-update-work"),
    )


class FakeUpdateService:
    """Fake service recording install/update calls, with optional blocking.

    Supports post-update shell-state switching (so a successful update can
    change the authoritative state) and failure injection.
    """

    def __init__(
        self,
        descriptors: tuple[ProductDescriptor, ...] = (),
        shell_state: ProductShellState | None = None,
        *,
        update_result: DeploymentResult | None = None,
        update_raises: Exception | None = None,
        post_update_shell_state: ProductShellState | None = None,
        block_seconds: float = 0.0,
    ) -> None:
        self._descriptors = descriptors
        self._shell_state = shell_state
        self._update_result = update_result
        self._update_raises = update_raises
        self._post_update_shell_state = post_update_shell_state
        self._block_seconds = block_seconds
        self.install_calls: list[str] = []
        self.update_calls: list[dict] = []
        self.spawn_calls: list[str] = []
        self.collect_calls: int = 0

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        return self._descriptors

    def collect_product_state(self) -> ProductShellState:
        self.collect_calls += 1
        if self._shell_state is None:
            raise ValueError("no shell state configured")
        return self._shell_state

    def spawn_component(self, product_id: str, **kwargs):
        self.spawn_calls.append(product_id)

    def install_product(
        self, product_id, *, resolver=None, fetcher=None, work_root=None,
        dependency_wheelhouse=None, probe_distribution=None,
        progress_callback=None,
    ):
        self.install_calls.append(product_id)
        if self._block_seconds > 0:
            time.sleep(self._block_seconds)
        return DeploymentResult(success=True, active_slot_id="rt-fake-slot")

    def update_product(
        self, product_id, *, resolver=None, fetcher=None, work_root=None,
        dependency_wheelhouse=None, probe_distribution=None,
        progress_callback=None,
    ):
        self.update_calls.append({
            "product_id": product_id,
            "resolver": resolver,
            "fetcher": fetcher,
            "work_root": work_root,
            "progress_callback": progress_callback,
        })
        if self._block_seconds > 0:
            time.sleep(self._block_seconds)
        if self._update_raises:
            raise self._update_raises
        result = self._update_result
        if result is None:
            result = DeploymentResult(success=True, active_slot_id="rt-fake-slot")
        if result.success and self._post_update_shell_state is not None:
            self._shell_state = self._post_update_shell_state
        return result


@pytest.fixture(scope="session")
def qapp():
    import os

    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wait_for(qapp, predicate, timeout_ms: float = 3000.0) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


# ---------------------------------------------------------------------------
# 1. InstallWorker operation mode (unit)
# ---------------------------------------------------------------------------


class _RecordingService:
    def __init__(self, emissions: tuple[InstallProgress, ...] = ()):
        self._emissions = emissions
        self.install_calls: list[str] = []
        self.update_calls: list[str] = []
        self.received_update_callback = None

    def install_product(self, product_id, **kwargs):
        self.install_calls.append(product_id)
        cb = kwargs.get("progress_callback")
        for p in self._emissions:
            if cb:
                cb(p)
        return DeploymentResult(success=True, active_slot_id="rt-1")

    def update_product(self, product_id, **kwargs):
        self.update_calls.append(product_id)
        cb = kwargs.get("progress_callback")
        self.received_update_callback = cb
        for p in self._emissions:
            if cb:
                cb(p)
        return DeploymentResult(success=True, active_slot_id="rt-1")


class TestInstallWorkerOperation:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_update_mode_calls_update_product_not_install(self, qapp):
        from zealfie.gui.install_worker import InstallWorker

        resolver, fetcher, work_root = _fake_deps()
        emissions = (
            InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
            InstallProgress(InstallPhase.COMPLETED, 100, "Complete."),
        )
        service = _RecordingService(emissions)

        worker = InstallWorker(
            "zesolver", service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
            operation="update",
        )
        got: list[InstallProgress] = []
        succeeded: list[str] = []
        worker.progress.connect(got.append)
        worker.install_succeeded.connect(succeeded.append)

        worker.run()

        assert service.update_calls == ["zesolver"]
        assert service.install_calls == []
        # Progress callback was wired through update_product and relayed verbatim.
        assert service.received_update_callback is not None
        assert got == list(emissions)
        assert succeeded == ["zesolver"]

    def test_default_mode_still_calls_install(self, qapp):
        from zealfie.gui.install_worker import InstallWorker

        resolver, fetcher, work_root = _fake_deps()
        service = _RecordingService()

        worker = InstallWorker(
            "zesolver", service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        worker.run()

        assert service.install_calls == ["zesolver"]
        assert service.update_calls == []

    def test_invalid_operation_raises(self, qapp):
        from zealfie.gui.install_worker import InstallWorker

        resolver, fetcher, work_root = _fake_deps()
        with pytest.raises(ValueError):
            InstallWorker(
                "zesolver", _RecordingService(),  # type: ignore[arg-type]
                resolver=resolver, fetcher=fetcher, work_root=work_root,
                operation="bogus",
            )


# ---------------------------------------------------------------------------
# 2. ProductCard update action UI + signal (no service.update_product)
# ---------------------------------------------------------------------------


class TestProductCardUpdateAction:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_update_button_visible_only_when_update_available(self, qapp):
        from zealfie.gui.product_card import ProductCard

        service = FakeUpdateService((_desc("zesolver", "ZeSolver"),))
        card = ProductCard(
            descriptor=_desc("zesolver", "ZeSolver"),
            state=_state("zesolver"),
            service=service,  # type: ignore[arg-type]
        )
        try:
            assert card._update_button.isHidden()  # NOT_CHECKED → hidden

            card.set_update_status(
                _result("zesolver", UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA)
            )
            assert not card._update_button.isHidden()
            assert card._update_button.isEnabled()
            assert "Mettre à jour" in card._update_button.text()

            # Primary Lancer remains visible and enabled (separate action).
            assert "Lancer" in card._action_button.text()
            assert card._action_button.isEnabled()

            card.set_update_status(_result("zesolver", UpdateStatus.UP_TO_DATE))
            assert card._update_button.isHidden()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_click_emits_update_requested_not_update_product(self, qapp):
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = _fake_deps()
        service = FakeUpdateService((_desc("zesolver", "ZeSolver"),))
        card = ProductCard(
            descriptor=_desc("zesolver", "ZeSolver"),
            state=_state("zesolver"),
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        signals: list[str] = []
        card.update_requested.connect(lambda pid: signals.append(pid))
        try:
            card.set_update_status(
                _result("zesolver", UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA)
            )
            card._update_button.click()

            assert signals == ["zesolver"]
            assert service.update_calls == [], (
                "ProductCard MUST NOT call service.update_product directly"
            )
            assert service.install_calls == []
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 3. MainWindow update coordination via worker thread
# ---------------------------------------------------------------------------


class TestMainWindowUpdateAction:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _window(self, service, check_fn=None):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = _fake_deps()
        return ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
            check_fn=check_fn,
        )

    def _make_update_available(self, window, pid="zesolver"):
        """Drive the card into UPDATE_AVAILABLE without a coordinator."""
        card = window._cards[pid]
        card.set_update_status(
            _result(pid, UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA)
        )
        return card

    def test_click_starts_worker_calls_update_product(self, qapp):
        """Clicking Mettre à jour → worker → service.update_product (not install)."""
        pid = "zesolver"
        pre_state = _state(pid, installed=True, launchable=True)
        service = FakeUpdateService(
            descriptors=(_desc(pid, "ZeSolver"),),
            shell_state=_shell((pre_state,)),
        )
        window = self._window(service)
        try:
            card = self._make_update_available(window, pid)

            card._update_button.click()

            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok, "update did not complete within timeout"

            assert len(service.update_calls) == 1
            assert service.install_calls == [], (
                "update path must not call install_product"
            )
            call = service.update_calls[0]
            assert call["product_id"] == pid
            assert call["resolver"] is window._resolver
            assert call["fetcher"] is window._fetcher
            assert call["work_root"] == window._work_root
            assert call["progress_callback"] is not None
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_success_refreshes_and_becomes_up_to_date(self, qapp):
        """Success: authoritative refresh + update display becomes Up to date."""
        pid = "zesolver"
        pre_state = _state(pid, installed=True, launchable=True, version="1.0.0")
        post_state = _state(pid, installed=True, launchable=True, version="2.0.0")
        service = FakeUpdateService(
            descriptors=(_desc(pid, "ZeSolver"),),
            shell_state=_shell((pre_state,)),
            post_update_shell_state=_shell((post_state,)),
        )
        window = self._window(service)
        try:
            initial_collects = service.collect_calls
            card = self._make_update_available(window, pid)

            card._update_button.click()
            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok

            # Authoritative refresh happened once more (post-success).
            assert service.collect_calls == initial_collects + 1

            # Card remains installed + launchable (still Lancer, enabled).
            assert card._state.installed is True
            assert card._state.launchable is True
            assert "Lancer" in card._action_button.text()
            assert card._action_button.isEnabled()

            # Update display is now Up to date (no direct service script).
            assert card.update_status_text == "Up to date"
            assert card._update_button.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_success_rechecks_via_coordinator(self, qapp):
        """With a check_fn, success re-runs the coordinator check → Up to date."""
        pid = "zesolver"
        pre_state = _state(pid, installed=True, launchable=True, version="1.0.0")
        post_state = _state(pid, installed=True, launchable=True, version="2.0.0")
        service = FakeUpdateService(
            descriptors=(_desc(pid, "ZeSolver"),),
            shell_state=_shell((pre_state,)),
            post_update_shell_state=_shell((post_state,)),
        )

        check_calls: list[str] = []

        def check_fn(product_id: str) -> ProductUpdateResult:
            check_calls.append(product_id)
            if len(check_calls) == 1:
                return _result(
                    product_id, UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA
                )
            return _result(product_id, UpdateStatus.UP_TO_DATE)

        window = self._window(service, check_fn=check_fn)
        try:
            card = window._cards[pid]
            # Coordinator delivers UPDATE_AVAILABLE asynchronously.
            ok = _wait_for(
                qapp,
                lambda: "Update available" in card.update_status_text,
            )
            assert ok, f"card never became update-available; text={card.update_status_text!r}"
            assert not card._update_button.isHidden()

            card._update_button.click()
            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok

            # Recheck delivered Up to date (via bridge on the GUI thread).
            ok = _wait_for(qapp, lambda: card.update_status_text == "Up to date")
            assert ok, f"card update text = {card.update_status_text!r}"
            assert card._update_button.isHidden()
            assert len(service.update_calls) == 1
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_failure_releases_lock_and_keeps_retry(self, qapp):
        """Update failure: error shown, lock released, update retryable."""
        pid = "zesolver"
        pre_state = _state(pid, installed=True, launchable=True)
        service = FakeUpdateService(
            descriptors=(_desc(pid, "ZeSolver"),),
            shell_state=_shell((pre_state,)),
            update_result=DeploymentResult(success=False, reason="build failed"),
        )
        window = self._window(service)
        try:
            card = self._make_update_available(window, pid)

            card._update_button.click()
            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok

            status = card._status_label.text()
            assert "failed" in status.lower()
            assert "build failed" in status.lower()
            assert "Traceback" not in status

            # Lock released; Lancer still possible (installed + launchable).
            assert card._action_button.isEnabled()
            assert "Lancer" in card._action_button.text()

            # Update remains available → retry possible.
            assert not card._update_button.isHidden()
            assert card._update_button.isEnabled()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_failure_exception_releases_lock_and_retry(self, qapp):
        pid = "zesolver"
        pre_state = _state(pid, installed=True, launchable=True)
        service = FakeUpdateService(
            descriptors=(_desc(pid, "ZeSolver"),),
            shell_state=_shell((pre_state,)),
            update_raises=RuntimeError("simulated update boom"),
        )
        window = self._window(service)
        try:
            card = self._make_update_available(window, pid)
            card._update_button.click()
            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok

            status = card._status_label.text()
            assert "Traceback" not in status
            assert "simulated" in status
            assert card._update_button.isEnabled()
            assert card._action_button.isEnabled()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_global_lock_blocks_concurrent_install_during_update(self, qapp):
        """While an update runs, another card's install is ignored and buttons disabled."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        resolver, fetcher, work_root = _fake_deps()
        upd = _desc("zesolver", "ZeSolver")
        inst = _desc("zemosaic", "ZeMosaic")
        products = (
            _state("zesolver", installed=True, launchable=True),
            _state("zemosaic", installed=False, launchable=False),
        )
        service = FakeUpdateService(
            descriptors=(upd, inst),
            shell_state=_shell(products),
            block_seconds=0.3,
        )
        window = ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            upd_card = self._make_update_available(window, "zesolver")
            inst_card = window._cards["zemosaic"]

            upd_card._update_button.click()
            assert window.install_active is True

            # Both cards' buttons disabled during transaction.
            assert upd_card._update_button.isEnabled() is False
            assert upd_card._action_button.isEnabled() is False
            assert inst_card._action_button.isEnabled() is False

            # Attempting a concurrent install is ignored.
            inst_card._action_button.click()

            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok

            assert len(service.update_calls) == 1
            assert len(service.install_calls) == 0, (
                "concurrent install must be blocked during update"
            )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_launch_blocked_during_update(self, qapp):
        """Launch button is disabled while an update is active."""
        pid = "zesolver"
        pre_state = _state(pid, installed=True, launchable=True)
        service = FakeUpdateService(
            descriptors=(_desc(pid, "ZeSolver"),),
            shell_state=_shell((pre_state,)),
            block_seconds=0.3,
        )
        window = self._window(service)
        try:
            card = self._make_update_available(window, pid)
            card._update_button.click()
            assert window.install_active is True
            assert card._action_button.isEnabled() is False, (
                "Lancer must be disabled during update"
            )
            ok = _wait_for(qapp, lambda: not window.install_active)
            assert ok
            assert card._action_button.isEnabled() is True
            assert service.spawn_calls == []
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
