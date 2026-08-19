"""ZA-M1-5-B LOT E — product card cleanup.

Hermetic: fake service, offscreen Qt, no probing, no network, no install.

Locks the card visual-hierarchy decisions:

* every card gives, at a glance: name, short description, state, primary
  action — with install/launch/update actions intact;
* the update-status line stays hidden until a check actually produced a
  value — never a useless "update status unknown" line before any check;
* the channel selector stays on the card, but only for multi-channel
  products (the full channel policy behaviour is covered by
  ``tests/test_gui_channel_policy.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from PySide6.QtWidgets import QApplication, QLabel
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.app import (
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    ProductUpdateResult,
    UpdateStatus,
)
from zealfie.components.model import EntryPointContract
from zealfie.runtime.model import RuntimeState

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

_EP = (EntryPointContract("gui_scripts", "zesolver"),)


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


def _desc(product_id: str, name: str | None = None) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=name or product_id.title(),
        distribution_name=name or product_id,
        launch_entry_points=_EP,
        description="A short description.",
    )


def _state(product_id: str, *, installed: bool = False, launchable: bool = False) -> ProductState:
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


class FakeService:
    def __init__(self, descriptors: tuple[ProductDescriptor, ...]) -> None:
        self._descriptors = descriptors

    def list_products(self):
        return self._descriptors

    def collect_product_state(self):
        return ProductShellState(
            runtime_state=RuntimeState.READY,
            runtime_root=Path("/fake/runtime"),
            products=tuple(_state(d.product_id) for d in self._descriptors),
        )


def _result(status: UpdateStatus) -> ProductUpdateResult:
    return ProductUpdateResult(product_id="zesolver", status=status)


# ---------------------------------------------------------------------------
# 1. Update-status line is hidden until a check produced a value
# ---------------------------------------------------------------------------


class TestUpdateStatusLineHidden:
    def test_hidden_before_any_check(self, qapp):
        from zealfie.gui.product_card import ProductCard

        desc = _desc("zesolver", "ZeSolver")
        service = FakeService((desc,))
        card = ProductCard(
            descriptor=desc,
            state=_state("zesolver", installed=True, launchable=True),
            service=service,
        )
        try:
            assert card.update_status_text == ""
            assert card._update_label.isHidden() is True
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_not_checked_and_none_are_hidden(self, qapp):
        from zealfie.gui.product_card import ProductCard

        desc = _desc("zesolver", "ZeSolver")
        service = FakeService((desc,))
        card = ProductCard(
            descriptor=desc,
            state=_state("zesolver", installed=True, launchable=True),
            service=service,
        )
        try:
            card.set_update_status(_result(UpdateStatus.NOT_CHECKED))
            assert card.update_status_text == ""
            assert card._update_label.isHidden() is True

            card.set_update_status(None)
            assert card.update_status_text == ""
            assert card._update_label.isHidden() is True
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_unknown_shown_only_after_a_real_check(self, qapp):
        """PROVENANCE_UNKNOWN (a post-check terminal) is shown honestly; it
        is never a pre-check placeholder."""
        from zealfie.gui.product_card import ProductCard

        desc = _desc("zesolver", "ZeSolver")
        service = FakeService((desc,))
        card = ProductCard(
            descriptor=desc,
            state=_state("zesolver", installed=True, launchable=True),
            service=service,
        )
        try:
            card.set_update_status(_result(UpdateStatus.PROVENANCE_UNKNOWN))
            assert "unknown" in card.update_status_text.lower()
            assert card._update_label.isHidden() is False

            # Back to not-checked → hidden again (no stale line).
            card.set_update_status(_result(UpdateStatus.NOT_CHECKED))
            assert card.update_status_text == ""
            assert card._update_label.isHidden() is True
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. Primary elements + actions intact
# ---------------------------------------------------------------------------


class TestCardHierarchy:
    def test_card_shows_name_description_state_and_action(self, qapp):
        from zealfie.gui.product_card import ProductCard

        desc = _desc("zesolver", "ZeSolver")
        service = FakeService((desc,))
        card = ProductCard(
            descriptor=desc,
            state=_state("zesolver", installed=True, launchable=True),
            service=service,
        )
        try:
            # Description label present and populated.
            label = card.findChild(QLabel, "descLabel")
            assert label is not None
            assert label.text() == "A short description."

            # State label present with the human-readable state.
            assert card._status_label is not None
            assert "Ready" in card._status_label.text()

            # Primary action shows Launch for a launchable product.
            assert card._action_button is not None
            assert "Launch" in card._action_button.text()
            assert card._action_button.isEnabled() is True
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()

    def test_install_and_update_actions_intact(self, qapp):
        from zealfie.gui.product_card import ProductCard

        desc = _desc("zesolver", "ZeSolver")
        service = FakeService((desc,))
        not_installed = _state("zesolver", installed=False, launchable=False)
        card = ProductCard(descriptor=desc, state=not_installed, service=service)
        try:
            # Install action is the primary action for a not-installed product.
            assert "Install" in card._action_button.text()
            assert card._action_button.isEnabled() is True

            # Update button is hidden until an update is actually available.
            assert card._update_button.isHidden() is True

            card.set_update_status(
                ProductUpdateResult(
                    product_id="zesolver",
                    status=UpdateStatus.UPDATE_AVAILABLE,
                    latest_commit_sha="a" * 40,
                )
            )
            assert card._update_button.isHidden() is False
            assert "Update" in card._update_button.text()
        finally:
            card.close()
            card.deleteLater()
            qapp.processEvents()
