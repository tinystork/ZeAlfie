"""M1-2C — product card widget for the ZeAlfie product shell.

Each card displays a product's display name, short description,
user-facing state text, and the primary action button.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
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
from zealfie.sources.acquisition import ArchiveFetcher
from zealfie.sources import SourceRefResolver

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

    # Emitted when install_product succeeds; transports product_id.
    # The MainWindow connects this to trigger a full state refresh.
    install_succeeded = Signal(str)

    def __init__(
        self,
        descriptor: ProductDescriptor,
        state: ProductState | None,
        service: ZeAlfieService,
        parent: QWidget | None = None,
        *,
        resolver: SourceRefResolver | None = None,
        fetcher: ArchiveFetcher | None = None,
        work_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._descriptor = descriptor
        self._state = state
        self._service = service
        self._resolver = resolver
        self._fetcher = fetcher
        self._work_root = work_root
        self._spawning = False
        self._installing = False
        self._awaiting_install_refresh: bool = False
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
        self._awaiting_install_refresh = False
        self._set_installing(False)
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
                self._status_label.setText("Loading\u2026")
            if self._action_button:
                self._action_button.setText("\u2026")
                self._action_button.setEnabled(False)
                self._action_button.setToolTip("")
            return

        if self._status_label:
            self._status_label.setText(state_label(state))

        if self._action_button:
            self._action_button.setText(action_label(state))
            self._action_button.setEnabled(action_enabled(state) and not self._spawning and not self._installing)
            self._action_button.setToolTip(action_tooltip(state))

    def _set_spawning(self, spawning: bool) -> None:
        self._spawning = spawning
        if self._action_button:
            if self._state is not None:
                self._action_button.setEnabled(
                    action_enabled(self._state) and not self._spawning and not self._installing
                )
            else:
                self._action_button.setEnabled(False)

    def _set_installing(self, installing: bool) -> None:
        self._installing = installing
        if self._action_button:
            if self._state is not None:
                self._action_button.setEnabled(
                    action_enabled(self._state) and not self._spawning and not self._installing
                )
            else:
                self._action_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Action handling
    # ------------------------------------------------------------------

    def _on_action_clicked(self) -> None:
        """Handle the primary action button click.

        For launchable products: calls ``service.spawn_component``.
        For not-installed products: calls ``service.install_product``.
        For installed-not-launchable: no-op (button is disabled).
        """
        if self._state is None:
            return

        if self._state.launchable:
            self._handle_launch()
        elif not self._state.installed:
            self._handle_install()
        # else: installed-not-launchable — button disabled, nothing to do

    def _handle_launch(self) -> None:
        """Launch the product component."""
        pid = self._descriptor.product_id
        self._last_spawn_error = None

        logger.info("Spawning product %r via service", pid)
        self._set_spawning(True)

        try:
            self._service.spawn_component(pid)
            self._status_label.setText(f"Launching {self._descriptor.display_name}\u2026")
        except Exception as exc:
            logger.error("Spawn of %r failed: %s", pid, exc)
            msg = str(exc)
            # Keep message short and user-friendly
            if len(msg) > 120:
                msg = msg[:117] + "\u2026"
            self._status_label.setText(f"Error: {msg}")
            self._last_spawn_error = msg
        finally:
            self._debounce_timer.start(DEBOUNCE_MS)

    def _handle_install(self) -> None:
        """Install the product from its remote source."""
        pid = self._descriptor.product_id
        self._last_spawn_error = None

        if self._resolver is None or self._fetcher is None or self._work_root is None:
            self._status_label.setText("Error: install dependencies not configured")
            logger.error("Install deps not wired for product %r", pid)
            return

        logger.info("Installing product %r via service", pid)
        self._set_installing(True)
        self._action_button.setText("Installing\u2026")

        try:
            result = self._service.install_product(
                pid,
                resolver=self._resolver,
                fetcher=self._fetcher,
                work_root=self._work_root,
            )
            if result.success:
                self._status_label.setText(f"Installation complete — {self._descriptor.display_name}")
                self._awaiting_install_refresh = True
                self.install_succeeded.emit(pid)
            else:
                reason = result.reason or "unknown error"
                if len(reason) > 120:
                    reason = reason[:117] + "\u2026"
                self._status_label.setText(f"Install failed: {reason}")
                self._last_spawn_error = reason
        except Exception as exc:
            logger.error("Install of %r failed: %s", pid, exc)
            msg = str(exc)
            if len(msg) > 120:
                msg = msg[:117] + "\u2026"
            self._status_label.setText(f"Error: {msg}")
            self._last_spawn_error = msg
        finally:
            self._debounce_timer.start(DEBOUNCE_MS)

    def _on_debounce_done(self) -> None:
        if self._debounce_timer.isActive():
            self._debounce_timer.stop()
        self._set_spawning(False)
        if not self._awaiting_install_refresh:
            self._set_installing(False)
        if self._last_spawn_error:
            # Error label already visible; do not overwrite it
            return
        if self._awaiting_install_refresh:
            # Waiting for a full state refresh from MainWindow;
            # do not overwrite the status label with stale state.
            return
        if self._status_label and self._state is not None and self._state.launchable:
            self._status_label.setText(state_label(self._state))
