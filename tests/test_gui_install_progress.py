"""Tests for M1-2D.6 — GUI bridge for structured install progress.

Verifies:
- InstallWorker relays backend progress observations through its Qt
  ``progress`` signal (it does not compute business progress).
- ProductCard displays determinate 0..100 progress: value = percent,
  text = message.

Headless via ``QT_QPA_PLATFORM=offscreen``.  No network, no runtime,
no subprocess.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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
    ProductState,
    ProductStateReasonCode,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import DeploymentResult

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")


@pytest.fixture(scope="session")
def qapp():
    import os

    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_descriptor(product_id: str, display_name: str = "") -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=display_name or product_id.title(),
        distribution_name=display_name or product_id,
        launch_entry_points=(EntryPointContract("gui_scripts", product_id),),
        description="",
    )


def _make_state(product_id: str) -> ProductState:
    return ProductState(
        product_id=product_id,
        display_name=product_id.title(),
        known=True,
        installed=False,
        launchable=False,
        version=None,
        reason_code=ProductStateReasonCode.NOT_INSTALLED,
        reason="not installed",
        managed=ManagedStatus.UNMANAGED,
    )


def _fake_deps():
    return (
        lambda o, r, ref: "a" * 40,
        lambda o, r, sha: b"zip",
        Path("/tmp/fake-work"),
    )


# ===========================================================================
# 1) Worker bridge: backend progress becomes a Qt signal
# ===========================================================================


class _FakeProgressService:
    """Fake service that records the callback and emits progress through it."""

    def __init__(self, emissions: tuple[InstallProgress, ...]):
        self._emissions = emissions
        self.received_callback = None
        self.install_calls: list[str] = []

    def install_product(
        self, product_id, *, resolver=None, fetcher=None, work_root=None,
        dependency_wheelhouse=None, probe_distribution=None,
        progress_callback=None,
    ):
        self.install_calls.append(product_id)
        self.received_callback = progress_callback
        for p in self._emissions:
            progress_callback(p)
        return DeploymentResult(success=True, active_slot_id="rt-1")


class TestInstallWorkerProgressBridge:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_worker_relays_backend_progress_to_signal(self, qapp):
        from zealfie.gui.install_worker import InstallWorker

        resolver, fetcher, work_root = _fake_deps()
        emissions = (
            InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
            InstallProgress(InstallPhase.INSTALLING_RUNTIME, 60, "Installing\u2026"),
            InstallProgress(InstallPhase.COMPLETED, 100, "Installation complete."),
        )
        service = _FakeProgressService(emissions)

        worker = InstallWorker(
            "zesolver", service,  # type: ignore[arg-type]
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )

        got: list[InstallProgress] = []
        succeeded: list[str] = []
        worker.progress.connect(got.append)
        worker.install_succeeded.connect(succeeded.append)

        worker.run()

        # Backend callback was wired through.
        assert service.received_callback is not None
        # All backend observations became Qt signal emissions.
        assert got == list(emissions)
        # Worker does not compute progress; it relays verbatim.
        assert [g.percent for g in got] == [0, 60, 100]
        assert succeeded == ["zesolver"]


# ===========================================================================
# 2) ProductCard progress slot: value/text updates
# ===========================================================================


class TestProductCardProgressSlot:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_progress_slot_updates_value_and_text(self, qapp):
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = _fake_deps()
        desc = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state("zesolver")
        service = MagicMock()

        card = ProductCard(
            descriptor=desc, state=state, service=service,
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card.set_install_in_progress(True)
            bar = card._progress_bar
            assert bar is not None
            assert bar.value() == 0

            for percent, message in (
                (0, "Preparing\u2026"),
                (35, "Acquiring\u2026"),
                (70, "Installing\u2026"),
                (100, "Installation complete."),
            ):
                card.set_install_progress(
                    InstallProgress(InstallPhase.INSTALLING_RUNTIME, percent, message)
                )
                assert bar.value() == percent, f"value should be {percent}"
                assert bar.text() == message, f"text should be {message!r}, got {bar.text()!r}"
                assert card._status_label.text() == message, (
                    f"status label should mirror {message!r}, "
                    f"got {card._status_label.text()!r}"
                )
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_progress_slot_clamps_percent(self, qapp):
        from zealfie.gui.product_card import ProductCard

        resolver, fetcher, work_root = _fake_deps()
        desc = _make_descriptor("zesolver", "ZeSolver")
        state = _make_state("zesolver")
        card = ProductCard(
            descriptor=desc, state=state, service=MagicMock(),
            resolver=resolver, fetcher=fetcher, work_root=work_root,
        )
        try:
            card.set_install_in_progress(True)
            card.set_install_progress(
                InstallProgress(InstallPhase.INSTALLING_RUNTIME, 150, "x")
            )
            assert card._progress_bar.value() == 100
            card.set_install_progress(
                InstallProgress(InstallPhase.INSTALLING_RUNTIME, -5, "x")
            )
            assert card._progress_bar.value() == 0
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()
