"""M1-2C — product card widget for the ZeAlfie product shell.

Each card displays a product's display name, short description,
user-facing state text, and the primary action button.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zealfie.app import ProductDescriptor, ProductState, ZeAlfieService

from .presentation import (
    action_enabled,
    action_label,
    action_tooltip,
    state_label,
)

logger = logging.getLogger(__name__)

# Cooldown in ms to re-enable the button after a spawn attempt
DEBOUNCE_MS = 500


class ProductCard(QFrame):
    """A single product card in the product shell.

    Shows display name, description, state, and a primary action button
    (Lancer / Installer).  Never calls subprocess, pip, resolver, or
    deployment layers directly — routes through ``ZeAlfieService``.
    """

    def __init__(
        self,
        descriptor: ProductDescriptor,
        state: ProductState | None,
        service: ZeAlfieService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._descriptor = descriptor
        self._state = state
        self._service = service
        self._spawning = False
        self._status_label: QLabel | None = None
        self._action_button: QPushButton | None = None
        self._last_spawn_error: str | None = None
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_debounce_done)
        self._build_ui()
        self._apply_state(state)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh_state(self, state: ProductState) -> None:
        """Update the card display for a new product state observation."""
        self._last_spawn_error = None
        self._state = state
        self._apply_state(state)

    @property
    def product_id(self) -> str:
        return self._descriptor.product_id

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # --- Header row: display name ---
        name_label = QLabel(self._descriptor.display_name)
        name_font = name_label.font()
        name_font.setBold(True)
        name_font.setPointSize(name_font.pointSize() + 2)
        name_label.setFont(name_font)
        layout.addWidget(name_label)

        # --- Description ---
        desc_text = self._descriptor.description or ""
        desc_label = QLabel(desc_text if desc_text else "")
        desc_label.setWordWrap(True)
        if desc_text:
            desc_label.setMaximumHeight(40)
        else:
            desc_label.setVisible(False)
        desc_label.setObjectName("descLabel")
        layout.addWidget(desc_label)

        # --- State label ---
        self._status_label = QLabel()
        self._status_label.setObjectName("statusLabel")
        layout.addWidget(self._status_label)

        # --- Action button row ---
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch()

        self._action_button = QPushButton()
        self._action_button.setMinimumWidth(130)
        self._action_button.clicked.connect(self._on_action_clicked)
        btn_layout.addWidget(self._action_button)

        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # State → UI
    # ------------------------------------------------------------------

    def _apply_state(self, state: ProductState | None) -> None:
        """Wire the product state observables into the widget."""
        if state is None:
            if self._status_label:
                self._status_label.setText("Loading…")
            if self._action_button:
                self._action_button.setText("…")
                self._action_button.setEnabled(False)
                self._action_button.setToolTip("")
            return

        if self._status_label:
            self._status_label.setText(state_label(state))

        if self._action_button:
            self._action_button.setText(action_label(state))
            self._action_button.setEnabled(action_enabled(state) and not self._spawning)
            self._action_button.setToolTip(action_tooltip(state))

    def _set_spawning(self, spawning: bool) -> None:
        self._spawning = spawning
        if self._action_button:
            if self._state is not None:
                self._action_button.setEnabled(
                    action_enabled(self._state) and not self._spawning
                )
            else:
                self._action_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Action handling
    # ------------------------------------------------------------------

    def _on_action_clicked(self) -> None:
        """Handle the primary action button click.

        For launchable products: calls ``service.spawn_component``.
        For non-launchable products: does nothing (button is disabled).
        """
        if self._state is None:
            return
        if not self._state.launchable:
            return  # defensive — button should already be disabled

        self._last_spawn_error = None
        pid = self._descriptor.product_id
        logger.info("Spawning product %r via service", pid)
        self._set_spawning(True)

        try:
            self._service.spawn_component(pid)
            self._status_label.setText(f"Launching {self._descriptor.display_name}…")
        except Exception as exc:
            logger.error("Spawn of %r failed: %s", pid, exc)
            msg = str(exc)
            # Keep message short and user-friendly
            if len(msg) > 120:
                msg = msg[:117] + "…"
            self._status_label.setText(f"Error: {msg}")
            self._last_spawn_error = msg
        finally:
            # Re-enable after a short debounce.  Use an owned timer rather than
            # a static singleShot so tests/window teardown cannot leave a
            # dangling callback to a destroyed card.
            self._debounce_timer.start(DEBOUNCE_MS)

    def _on_debounce_done(self) -> None:
        if self._debounce_timer.isActive():
            self._debounce_timer.stop()
        self._set_spawning(False)
        if self._last_spawn_error:
            # Error label already visible; do not overwrite it
            return
        if self._status_label and self._state is not None and self._state.launchable:
            self._status_label.setText(state_label(self._state))
