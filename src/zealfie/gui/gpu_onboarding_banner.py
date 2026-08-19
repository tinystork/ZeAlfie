"""GPU acceleration onboarding banner (ZA-M1-5-B LOT D).

A small, non-intrusive banner shown after a product that declares GPU
acceleration requirements has been successfully installed **and** the
service's acceleration recommendation is ``OFFER_SETUP`` (compatible
hardware present, nothing accelerated yet).  It offers exactly two actions:

* **Enable acceleration** — opens Settings with the GPU panel visible; the
  user reviews the plan and clicks *Install* themselves.  Consent is
  preserved and the existing install path is unchanged: this button only
  navigates, it never installs anything.
* **Later** — dismisses the banner for this session (nothing is installed,
  and the banner is not re-shown on refresh).  The action stays reachable
  via the Settings GPU badge/panel.

All user-visible text is localized via :func:`zealfie.i18n.translate`
(EN + FR); no hardcoded language strings live in the widget.  The widget
performs no network/build/pip work and never mutates anything — it only
emits :attr:`activate_requested` / :attr:`dismissed`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from zealfie.i18n import translate

__all__ = ["GpuOnboardingBanner"]


class GpuOnboardingBanner(QFrame):
    """Proposal banner: "GPU acceleration is available for {product}." + 2 actions."""

    activate_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._product_name: str | None = None
        self._build_ui()
        self.hide()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setObjectName("gpuOnboardingBanner")
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        self._message_label = QLabel()
        self._message_label.setObjectName("gpuOnboardingMessageLabel")
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._message_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()

        self._activate_button = QPushButton(translate("gpu.onboarding.activate"))
        self._activate_button.setObjectName("gpuOnboardingActivateButton")
        self._activate_button.setMinimumWidth(170)
        self._activate_button.clicked.connect(self.activate_requested.emit)
        row.addWidget(self._activate_button)

        self._later_button = QPushButton(translate("gpu.onboarding.later"))
        self._later_button.setObjectName("gpuOnboardingLaterButton")
        self._later_button.setMinimumWidth(100)
        self._later_button.clicked.connect(self.dismissed.emit)
        row.addWidget(self._later_button)

        row.addStretch()
        outer.addLayout(row)

    # ------------------------------------------------------------------
    # State → UI
    # ------------------------------------------------------------------

    def show_for_product(self, product_name: str) -> None:
        """Show the proposal for a GPU-capable product (its display name)."""
        self._product_name = product_name
        self._message_label.setText(
            translate("gpu.onboarding.message", product=product_name)
        )
        self.show()

    def dismiss(self) -> None:
        """Hide the proposal (deferred by the user; nothing installed)."""
        self._product_name = None
        self.hide()

    def retranslate(self) -> None:
        """Re-apply translated strings after a runtime language switch."""
        self._activate_button.setText(translate("gpu.onboarding.activate"))
        self._later_button.setText(translate("gpu.onboarding.later"))
        if self._product_name is not None:
            self._message_label.setText(
                translate("gpu.onboarding.message", product=self._product_name)
            )

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @property
    def message_text(self) -> str:
        return self._message_label.text()

    @property
    def product_name(self) -> str | None:
        return self._product_name
