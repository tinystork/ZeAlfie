"""Tests for M1-2F Phase 5 — GUI channel/policy wiring (follow-only).

Covers the minimal ProductCard channel selector:

* hidden when the product has no remote source or a single channel;
* shown with a selector when multiple channels are declared;
* policy label reflects the service's current policy;
* selecting a channel emits ``channel_changed`` (MainWindow persists).

Pin UI is intentionally omitted (CLI-only for Phase 5); the GUI remains
follow-channel only.  All tests run headless via ``QT_QPA_PLATFORM=offscreen``.
"""

from __future__ import annotations

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
    ProductPolicy,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import RuntimeState
from zealfie.sources import RemoteSource

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

_EP = (EntryPointContract("console_scripts", "zewitness"),)


def _desc(
    product_id: str,
    *,
    remote: bool = True,
    channel_refs: tuple[tuple[str, str], ...] = (),
) -> ProductDescriptor:
    remote_source = (
        RemoteSource(owner="tinystork", repo=f"Ze{product_id.capitalize()}", ref="main")
        if remote
        else None
    )
    return ProductDescriptor(
        product_id=product_id,
        display_name=product_id.capitalize(),
        distribution_name=product_id,
        launch_entry_points=_EP,
        remote_source=remote_source,
        channel_refs=channel_refs,
    )


def _state(product_id: str) -> ProductState:
    return ProductState(
        product_id=product_id,
        display_name=product_id.capitalize(),
        known=True,
        installed=False,
        launchable=False,
        version=None,
        reason_code=ProductStateReasonCode.NOT_INSTALLED,
        reason="not installed",
    )


class FakePolicyService:
    """Fake service: catalog + policy read/write + minimal state collection."""

    def __init__(
        self,
        descriptors: tuple[ProductDescriptor, ...],
        *,
        policy: ProductPolicy | None = None,
    ) -> None:
        self._descriptors = descriptors
        self._policy = policy or ProductPolicy(
            product_id=descriptors[0].product_id,
            channel="stable",
            policy="follow",
        )
        self.channel_calls: list[tuple[str, str]] = []
        self.install_calls: list[str] = []

    def list_products(self) -> tuple[ProductDescriptor, ...]:
        return self._descriptors

    def collect_product_state(self) -> ProductShellState:
        return ProductShellState(
            runtime_state=RuntimeState.READY,
            runtime_root=Path("/fake/runtime"),
            products=tuple(_state(d.product_id) for d in self._descriptors),
        )

    def spawn_component(self, product_id: str, **kwargs):
        pass

    def install_product(self, product_id: str, **kwargs):
        self.install_calls.append(product_id)

    def product_policy(self, product_id: str) -> ProductPolicy:
        return self._policy

    def set_product_channel(self, product_id: str, channel: str) -> ProductPolicy:
        self.channel_calls.append((product_id, channel))
        return ProductPolicy(product_id=product_id, channel=channel, policy="follow")


@pytest.fixture(scope="session")
def qapp():
    import os

    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 1. ProductCard channel selector
# ---------------------------------------------------------------------------


class TestProductCardChannelSelector:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _card(self, qapp, descriptor, service):
        from zealfie.gui.product_card import ProductCard

        return ProductCard(
            descriptor=descriptor,
            state=_state(descriptor.product_id),
            service=service,
        )

    def test_hidden_without_remote_source(self, qapp):
        from zealfie.gui.product_card import ProductCard

        desc = _desc("offline", remote=False)
        service = FakePolicyService((desc,))
        card = self._card(qapp, desc, service)
        try:
            assert card._channel_combo is None
            assert card._policy_label is None
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_hidden_with_single_channel(self, qapp):
        desc = _desc("alpha", channel_refs=(("stable", "main"),))
        service = FakePolicyService((desc,))
        card = self._card(qapp, desc, service)
        try:
            assert card._channel_combo is None
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_shown_with_multiple_channels(self, qapp):
        desc = _desc(
            "alpha",
            channel_refs=(("stable", "main"), ("beta", "beta")),
        )
        service = FakePolicyService((desc,))
        card = self._card(qapp, desc, service)
        try:
            assert card._channel_combo is not None
            assert card._channel_combo.count() == 2
            assert card._policy_label is not None
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_policy_label_reflects_service_policy(self, qapp):
        desc = _desc(
            "alpha",
            channel_refs=(("stable", "main"), ("beta", "beta")),
        )
        service = FakePolicyService(
            (desc,),
            policy=ProductPolicy(product_id="alpha", channel="beta", policy="follow"),
        )
        card = self._card(qapp, desc, service)
        try:
            assert "Channel: beta" in card._policy_label.text()
            # selector is synced to the persisted channel
            assert card._channel_combo.currentData() == "beta"
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_channel_change_emits_signal(self, qapp):
        desc = _desc(
            "alpha",
            channel_refs=(("stable", "main"), ("beta", "beta")),
        )
        service = FakePolicyService((desc,))
        card = self._card(qapp, desc, service)
        emitted: list[tuple[str, str]] = []
        card.channel_changed.connect(lambda pid, ch: emitted.append((pid, ch)))
        try:
            card._channel_combo.setCurrentIndex(1)  # -> beta
            assert emitted == [("alpha", "beta")]
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. MainWindow channel-change wiring
# ---------------------------------------------------------------------------


class TestMainWindowChannelWiring:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_channel_change_persists_via_service(self, qapp):
        from zealfie.gui.main_window import ZeAlfieMainWindow

        desc = _desc(
            "alpha",
            channel_refs=(("stable", "main"), ("beta", "beta")),
        )
        service = FakePolicyService((desc,))
        window = ZeAlfieMainWindow(service=service)  # type: ignore[arg-type]
        try:
            card = window._cards["alpha"]
            assert card._channel_combo is not None
            card.channel_changed.emit("alpha", "beta")
            qapp.processEvents()
            assert service.channel_calls == [("alpha", "beta")]
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()
