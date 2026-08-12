"""M1-2D.5 — product card widget for the ZeAlfie product shell.

Each card displays a product's display name, short description,
user-facing state text, and the primary action button.

Install is no longer triggered directly — the card emits
``install_requested`` and the MainWindow coordinates a worker thread.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zealfie.app import (
    ProductDescriptor,
    ProductState,
    ProductUpdateResult,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.sources.acquisition import ArchiveFetcher
from zealfie.sources import SourceRefResolver

from .presentation import (
    action_enabled,
    action_label,
    action_tooltip,
    state_label,
    update_status_label,
)

logger = logging.getLogger(__name__)

# Cooldown in ms to re-enable the button after a spawn attempt
DEBOUNCE_MS = 500


class ProductCard(QFrame):
    """A single product card in the product shell.

    Shows display name, description, state, and a primary action button
    (Lancer / Installer).  Never calls subprocess, pip, resolver, or
    deployment layers directly — routes through ``ZeAlfieService``.

    For installs, emits ``install_requested`` so the MainWindow can
    coordinate a worker thread (M1-2D.5).  Does NOT block the UI.
    """

    # Emitted when the user clicks Installer (M1-2D.5).
    # MainWindow coordinates the actual install in a worker thread.
    install_requested = Signal(str)  # product_id

    # Emitted when the user clicks "Mettre à jour" (M1-2E E.6a).
    # MainWindow coordinates the actual update in the same worker thread.
    # The card never calls service.update_product directly.
    update_requested = Signal(str)  # product_id

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
        self._update_label: QLabel | None = None
        self._update_button: QPushButton | None = None
        self._update_available: bool = False
        self._progress_bar: QProgressBar | None = None
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
        if self._progress_bar is not None:
            self._progress_bar.setVisible(False)
        self._apply_state(state)

    def set_update_status(self, result: ProductUpdateResult | None) -> None:
        """Display a product's update-check status (read-only, informational).

        This is deliberately **separate** from install/launch state: the
        state label and action button are untouched.  ``NOT_CHECKED`` (or
        ``None``) produces an empty label, which is hidden.  Must only be
        called on the GUI thread (MainWindow routes it via the bridge).
        """
        if self._update_label is None:
            return
        text = update_status_label(result)
        self._update_label.setText(text)
        self._update_label.setVisible(bool(text))

        # Show an actionable update button only when an update is actually
        # available.  The button never triggers the service directly — it
        # emits ``update_requested`` and MainWindow coordinates the worker.
        status = getattr(result, "status", None)
        self._update_available = status is UpdateStatus.UPDATE_AVAILABLE
        if self._update_button is not None:
            self._update_button.setVisible(self._update_available)
            self._update_update_button_enabled()

    @property
    def update_status_text(self) -> str:
        """The current user-facing update-status text (``""`` when hidden)."""
        if self._update_label is None:
            return ""
        return self._update_label.text()

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

        # --- Update status label (separate from install/launch state) ---
        self._update_label = QLabel()
        self._update_label.setObjectName("updateStatusLabel")
        self._update_label.setVisible(False)
        layout.addWidget(self._update_label)

        # --- Determinate install progress bar (hidden unless installing) ---
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("installProgressBar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("")
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        # --- Action button row ---
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch()

        self._update_button = QPushButton("Mettre à jour")
        self._update_button.setObjectName("updateButton")
        self._update_button.setMinimumWidth(130)
        self._update_button.setVisible(False)
        self._update_button.clicked.connect(self._on_update_clicked)
        btn_layout.addWidget(self._update_button)

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
        self._update_update_button_enabled()

    def _set_installing(self, installing: bool) -> None:
        self._installing = installing
        if not installing and self._update_button is not None:
            self._update_button.setText("Mettre à jour")
        if self._action_button:
            if self._state is not None:
                self._action_button.setEnabled(
                    action_enabled(self._state) and not self._spawning and not self._installing
                )
            else:
                self._action_button.setEnabled(False)
        self._update_update_button_enabled()

    def _update_update_button_enabled(self) -> None:
        """Enable the update button only when idle and an update is available."""
        if self._update_button is None:
            return
        self._update_button.setEnabled(
            self._update_available and not self._spawning and not self._installing
        )

    # ------------------------------------------------------------------
    # Action handling
    # ------------------------------------------------------------------

    def _on_action_clicked(self) -> None:
        """Handle the primary action button click.

        For launchable products: calls ``service.spawn_component``.
        For not-installed products: emits ``install_requested`` signal.
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
        """Emit install_requested — MainWindow coordinates the worker thread.

        Does NOT call service.install_product() directly (M1-2D.5).
        """
        pid = self._descriptor.product_id
        self._last_spawn_error = None

        if self._resolver is None or self._fetcher is None or self._work_root is None:
            self._status_label.setText("Error: install dependencies not configured")
            logger.error("Install deps not wired for product %r", pid)
            return

        logger.info("Requesting install for product %r", pid)
        self.install_requested.emit(pid)

    def _on_update_clicked(self) -> None:
        """Emit update_requested — MainWindow coordinates the worker thread.

        Does NOT call service.update_product() directly (M1-2E E.6a).
        """
        pid = self._descriptor.product_id
        if not self._update_available or self._spawning or self._installing:
            return
        self._last_spawn_error = None

        if self._resolver is None or self._fetcher is None or self._work_root is None:
            self._status_label.setText("Error: update dependencies not configured")
            logger.error("Update deps not wired for product %r", pid)
            return

        logger.info("Requesting update for product %r", pid)
        self.update_requested.emit(pid)

    # ------------------------------------------------------------------
    # Install state control (called by MainWindow, M1-2D.5)
    # ------------------------------------------------------------------

    def set_install_in_progress(self, in_progress: bool) -> None:
        """Update card UI to reflect install in-progress / idle state."""
        self._set_installing(in_progress)
        if in_progress:
            self._action_button.setText("Installation\u2026")
            self._status_label.setText(
                f"Installation de {self._descriptor.display_name} en cours\u2026"
            )
            if self._progress_bar is not None:
                self._progress_bar.setValue(0)
                self._progress_bar.setFormat("")
                self._progress_bar.setVisible(True)

    def set_update_in_progress(self, in_progress: bool) -> None:
        """Update card UI to reflect update in-progress / idle state."""
        self._set_installing(in_progress)
        if in_progress:
            if self._update_button is not None:
                self._update_button.setText("Mise à jour\u2026")
            self._status_label.setText(
                f"Mise à jour de {self._descriptor.display_name} en cours\u2026"
            )
            if self._progress_bar is not None:
                self._progress_bar.setValue(0)
                self._progress_bar.setFormat("")
                self._progress_bar.setVisible(True)

    def set_install_progress(self, progress) -> None:
        """Update determinate install progress from a backend observation.

        ``progress`` is a :class:`~zealfie.app.progress.InstallProgress`
        carrying ``percent`` (0..100) and ``message``.  The card does not
        compute progress; it only displays the backend's value/text.  The
        user-facing status label mirrors the progress message so the phase
        is visible alongside the determinate bar.
        """
        if self._progress_bar is None:
            return
        percent = int(getattr(progress, "percent", 0) or 0)
        message = str(getattr(progress, "message", "") or "")
        if message:
            self._status_label.setText(message)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(max(0, min(100, percent)))
        self._progress_bar.setFormat(message)

    def set_install_error(self, message: str) -> None:
        """Show a user-friendly install error on the card (no traceback)."""
        if len(message) > 120:
            message = message[:117] + "\u2026"
        self._status_label.setText(f"Install failed: {message}")
        self._last_spawn_error = message
        if self._progress_bar is not None:
            self._progress_bar.setVisible(False)
        self._set_installing(False)

    def set_update_error(self, message: str) -> None:
        """Show a user-friendly update error on the card (no traceback)."""
        if len(message) > 120:
            message = message[:117] + "\u2026"
        self._status_label.setText(f"Update failed: {message}")
        self._last_spawn_error = message
        if self._progress_bar is not None:
            self._progress_bar.setVisible(False)
        self._set_installing(False)

    def set_install_complete_refresh_required(self) -> None:
        """Show that install succeeded but refresh failed — safe fallback.

        Button stays disabled; retry/install blocked until a subsequent
        refresh succeeds.
        """
        self._status_label.setText(
            f"Installation complete — refresh required"
        )
        self._awaiting_install_refresh = True
        # Keep installing flag True so button stays disabled
        # (we do NOT call _set_installing(False) here)
        if self._action_button:
            self._action_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

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
