"""Tests for M1-2E LOT E.4 — read-only product-shell update status UX.

Covers:

* presentation mapping for every :class:`UpdateStatus` (no raw enum / traceback);
* ``ProductCard.set_update_status`` — separate from install/launch state,
  hidden for ``NOT_CHECKED``, unchanged action labels/buttons;
* ``ZeAlfieMainWindow`` wiring a coordinator + injected fake check function,
  updating the matching card without blocking, marshalling results onto the
  GUI thread;
* safe lifecycle: shutdown on close without hanging (``wait=False``);
* read-only smoke: update checks never install/launch/mutate anything.

All GUI tests run headless via ``QT_QPA_PLATFORM=offscreen``.  No real
network, no wheel building, no subprocess.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

try:
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    ProductUpdateResult,
    UpdateStatus,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import RuntimeState

from zealfie.gui.presentation import update_status_label

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex

_EP = (EntryPointContract("console_scripts", "zewitness"),)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(product_id: str, status: UpdateStatus, **kwargs) -> ProductUpdateResult:
    return ProductUpdateResult(product_id=product_id, status=status, **kwargs)


def _desc(product_id: str, name: str | None = None) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=name or product_id.title(),
        distribution_name=name or product_id,
        launch_entry_points=_EP,
    )


def _state(
    product_id: str,
    *,
    installed: bool = True,
    launchable: bool = True,
) -> ProductState:
    return ProductState(
        product_id=product_id,
        display_name=product_id.title(),
        known=True,
        installed=installed,
        launchable=launchable,
        version="1.0.0" if installed else None,
        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE
        if launchable
        else ProductStateReasonCode.NOT_INSTALLED,
        reason="ok",
    )


def _shell(products: tuple[ProductState, ...]) -> ProductShellState:
    return ProductShellState(
        runtime_state=RuntimeState.READY,
        runtime_root=Path("/fake/runtime"),
        products=products,
    )


class FakeService:
    """Minimal fake service: catalog + state, recording any install/launch."""

    def __init__(self, descriptors: tuple[ProductDescriptor, ...]) -> None:
        self._descriptors = descriptors
        self.spawn_calls: list[str] = []
        self.install_calls: list[str] = []
        self.collect_calls = 0

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        return self._descriptors

    def collect_product_state(self) -> ProductShellState:
        self.collect_calls += 1
        return _shell(tuple(_state(d.product_id) for d in self._descriptors))

    def spawn_component(self, product_id: str, **kwargs):
        self.spawn_calls.append(product_id)

    def install_product(self, product_id: str, **kwargs):
        self.install_calls.append(product_id)


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
# 1. Presentation mapping (no Qt)
# ---------------------------------------------------------------------------


class TestUpdateStatusLabel:
    def test_not_checked_is_empty(self):
        assert update_status_label(_result("p", UpdateStatus.NOT_CHECKED)) == ""

    def test_none_is_empty(self):
        assert update_status_label(None) == ""

    def test_checking_indicator(self):
        label = update_status_label(_result("p", UpdateStatus.CHECKING))
        assert "checking" in label.lower()

    def test_up_to_date_stable(self):
        assert update_status_label(_result("p", UpdateStatus.UP_TO_DATE)) == "Up to date"

    def test_update_available_with_short_sha(self):
        label = update_status_label(
            _result("p", UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA)
        )
        assert "Update available" in label
        assert OTHER_SHA[:7] in label
        assert OTHER_SHA not in label  # full SHA is not shown

    def test_update_available_without_sha(self):
        assert (
            update_status_label(_result("p", UpdateStatus.UPDATE_AVAILABLE))
            == "Update available"
        )

    def test_check_failed_with_error(self):
        label = update_status_label(
            _result("p", UpdateStatus.CHECK_FAILED, error="network down")
        )
        assert "Update check failed" in label
        assert "network down" in label

    def test_check_failed_without_error(self):
        assert (
            update_status_label(_result("p", UpdateStatus.CHECK_FAILED))
            == "Update check failed"
        )

    def test_provenance_unknown(self):
        label = update_status_label(
            _result("p", UpdateStatus.PROVENANCE_UNKNOWN)
        )
        assert "unknown" in label.lower()

    def test_check_failed_error_is_compact_no_traceback(self):
        messy = "line one\n  Traceback (most recent call last):\n boom " * 5
        label = update_status_label(
            _result("p", UpdateStatus.CHECK_FAILED, error=messy)
        )
        assert "Traceback" not in label
        assert "\n" not in label
        assert len(label) <= 120

    def test_never_raw_enum_names(self):
        for status in UpdateStatus:
            result = _result("p", status, error="boom")
            label = update_status_label(result)
            assert status.name not in label, f"raw enum {status.name} leaked"
            assert "UpdateStatus" not in label


# ---------------------------------------------------------------------------
# 2. ProductCard update-status display (headless)
# ---------------------------------------------------------------------------


class TestProductCardUpdateStatus:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _card(self, qapp, service):
        from zealfie.gui.product_card import ProductCard

        desc = _desc("zesolver", "ZeSolver")
        card = ProductCard(
            descriptor=desc,
            state=_state("zesolver"),
            service=service,
        )
        return card

    def test_displays_each_status_mapping(self, qapp):
        from zealfie.gui.product_card import ProductCard

        service = FakeService((_desc("zesolver", "ZeSolver"),))
        card = self._card(qapp, service)
        try:
            cases = {
                UpdateStatus.UPDATE_AVAILABLE: "Update available",
                UpdateStatus.CHECKING: "Checking",
                UpdateStatus.CHECK_FAILED: "Update check failed",
                UpdateStatus.PROVENANCE_UNKNOWN: "Update status unknown",
                UpdateStatus.UP_TO_DATE: "Up to date",
            }
            for status, expected in cases.items():
                card.set_update_status(_result("zesolver", status))
                assert expected in card.update_status_text
                assert not card._update_label.isHidden()

            # NOT_CHECKED hides the label
            card.set_update_status(_result("zesolver", UpdateStatus.NOT_CHECKED))
            assert card.update_status_text == ""
            assert card._update_label.isHidden()

            # None also hides the label
            card.set_update_status(None)
            assert card.update_status_text == ""
            assert card._update_label.isHidden()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_action_labels_unchanged_by_update_status(self, qapp):
        """Update status changes never alter install/launch labels/buttons."""
        from zealfie.gui.product_card import ProductCard

        service = FakeService((_desc("zesolver", "ZeSolver"),))
        desc = _desc("zesolver", "ZeSolver")
        card = ProductCard(
            descriptor=desc,
            state=_state("zesolver", installed=True, launchable=True),
            service=service,
        )
        try:
            btn = card._action_button
            status_label = card._status_label
            before_btn = btn.text()
            before_enabled = btn.isEnabled()
            before_status = status_label.text()

            card.set_update_status(
                _result("zesolver", UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA)
            )

            assert btn.text() == before_btn
            assert btn.isEnabled() == before_enabled
            assert status_label.text() == before_status
            assert "Launch" in btn.text()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 3. MainWindow wiring, threading, lifecycle, read-only
# ---------------------------------------------------------------------------


class TestMainWindowUpdateChecks:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _window(self, service, check_fn=None, resolver=None):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        return ZeAlfieMainWindow(
            service=service,  # type: ignore[arg-type]
            resolver=resolver,
            check_fn=check_fn,
        )

    def test_wires_coordinator_and_updates_matching_card(self, qapp):
        service = FakeService((_desc("zesolver", "ZeSolver"),))
        calls: list[str] = []

        def check_fn(product_id: str) -> ProductUpdateResult:
            calls.append(product_id)
            return _result(
                product_id, UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA
            )

        window = self._window(service, check_fn=check_fn)
        try:
            assert window._update_coordinator is not None
            card = window._cards["zesolver"]

            ok = _wait_for(
                qapp,
                lambda: "Update available" in card.update_status_text,
            )
            assert ok, f"card never updated; text={card.update_status_text!r}"
            assert OTHER_SHA[:7] in card.update_status_text
            assert calls == ["zesolver"]
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_start_is_non_blocking(self, qapp):
        service = FakeService((_desc("zesolver", "ZeSolver"),))
        entered = threading.Event()
        release = threading.Event()

        def check_fn(product_id: str) -> ProductUpdateResult:
            entered.set()
            release.wait(timeout=5)
            return _result(product_id, UpdateStatus.UP_TO_DATE)

        window = self._window(service, check_fn=check_fn)
        try:
            # check started in the background and construction returned.
            assert entered.wait(timeout=5)
            assert not release.is_set()
        finally:
            release.set()
            _wait_for(qapp, lambda: window._cards["zesolver"].update_status_text == "Up to date")
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_no_update_check_without_check_fn_or_resolver(self, qapp):
        service = FakeService((_desc("zesolver", "ZeSolver"),))
        window = self._window(service)  # no check_fn, no resolver
        try:
            assert window._update_coordinator is None
            assert window._cards["zesolver"].update_status_text == ""
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_result_marshaled_to_gui_thread(self, qapp, monkeypatch):
        """Observer runs off-thread; widget mutation happens on the GUI thread."""
        from zealfie.gui.product_card import ProductCard

        service = FakeService((_desc("zesolver", "ZeSolver"),))
        window = self._window(service)  # no coordinator; test bridge directly
        main_thread = threading.current_thread()
        captured: list[tuple[threading.Thread, ProductUpdateResult]] = []

        def fake_set_update_status(self, result):
            captured.append((threading.current_thread(), result))

        monkeypatch.setattr(ProductCard, "set_update_status", fake_set_update_status)

        try:
            result = _result(
                "zesolver", UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA
            )
            worker = threading.Thread(
                target=window._update_bridge.notify, args=(result,)
            )
            worker.start()
            worker.join()
            qapp.processEvents()

            assert captured, "set_update_status must be invoked"
            call_thread, call_result = captured[0]
            assert call_result is result
            assert call_thread is main_thread, (
                "widget update must run on the GUI thread, not the worker thread"
            )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_shutdown_is_non_blocking_with_inflight_check(self, qapp):
        service = FakeService((_desc("zesolver", "ZeSolver"),))
        entered = threading.Event()
        release = threading.Event()

        def check_fn(product_id: str) -> ProductUpdateResult:
            entered.set()
            release.wait(timeout=5)
            return _result(product_id, UpdateStatus.UP_TO_DATE)

        window = self._window(service, check_fn=check_fn)
        try:
            assert entered.wait(timeout=5)
            start = time.monotonic()
            window._shutdown_update_checks()
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, "shutdown must not block on the in-flight check"
            assert window._update_coordinator is None

            # The in-flight check still completes and reports to the card.
            release.set()
            ok = _wait_for(
                qapp, lambda: window._cards["zesolver"].update_status_text == "Up to date"
            )
            assert ok, "in-flight check should still complete and report"
        finally:
            release.set()
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_read_only_smoke_no_install_or_launch(self, qapp):
        """Update checks never install/launch; card is updated without mutation."""
        service = FakeService((_desc("zesolver", "ZeSolver"),))
        check_calls: list[str] = []

        def check_fn(product_id: str) -> ProductUpdateResult:
            check_calls.append(product_id)
            return _result(
                product_id, UpdateStatus.UPDATE_AVAILABLE, latest_commit_sha=OTHER_SHA
            )

        window = self._window(service, check_fn=check_fn)
        try:
            ok = _wait_for(
                qapp,
                lambda: "Update available" in window._cards["zesolver"].update_status_text,
            )
            assert ok
            assert check_calls == ["zesolver"]
            # Read-only invariant: no install / launch / spawn was triggered.
            assert service.install_calls == []
            assert service.spawn_calls == []
            # The action button is unchanged (still "Lancer" for launchable).
            assert "Launch" in window._cards["zesolver"]._action_button.text()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
