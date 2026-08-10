"""M1-2C — ZeAlfie main window.

The top-level product shell window hosting product cards and a status bar.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from zealfie.app import ProductShellState, ZeAlfieService
from zealfie.sources.acquisition import ArchiveFetcher
from zealfie.sources import SourceRefResolver

from .presentation import runtime_summary
from .product_card import ProductCard

logger = logging.getLogger(__name__)


class ZeAlfieMainWindow(QMainWindow):
    """ZeAlfie product shell main window.

    Composition root responsibility: owns the QMainWindow, populates
    product cards, handles refresh, and displays global status.

    Does NOT call subprocess, pip, resolver, deployment, or registry
    internals.  All product interaction routes through ``service``.
    """

    def __init__(
        self,
        service: ZeAlfieService,
        parent: QWidget | None = None,
        *,
        resolver: SourceRefResolver | None = None,
        fetcher: ArchiveFetcher | None = None,
        work_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._resolver = resolver
        self._fetcher = fetcher
        self._work_root = work_root
        self._cards: dict[str, ProductCard] = {}
        self._status_label: QLabel | None = None
        self._error_label: QLabel | None = None
        self._cards_container: QWidget | None = None
        self._build_ui()
        self._refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle("ZeAlfie \u2014 Astronomy Launcher For Imaging Engines")
        self.resize(580, 500)

        # --- Central scroll area ---
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(16, 12, 16, 12)
        central_layout.setSpacing(10)

        # Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)

        title = QLabel("\U0001f6f8 ZeAlfie")
        title_font = title.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 8)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(title)

        subtitle = QLabel("Astronomy Launcher For Imaging Engines  \U0001f47d")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(subtitle)

        central_layout.addLayout(header_layout)

        # Error label (hidden by default, shown on startup failure)
        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: #c0392b; font-weight: bold;")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        central_layout.addWidget(self._error_label)

        # Product cards container
        self._cards_container = QWidget()
        self._cards_container.setObjectName("cardsContainer")
        cards_layout = QVBoxLayout(self._cards_container)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(8)

        # Add a stretch at bottom so cards don't fill the viewport
        cards_layout.addStretch()

        # Wrap in scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cards_container)
        scroll.setObjectName("productScrollArea")
        central_layout.addWidget(scroll)

        self.setCentralWidget(central)

        # --- Status bar ---
        status_bar = QStatusBar()
        self._status_label = QLabel("Starting\u2026")
        self._status_label.setObjectName("statusLabel")
        status_bar.addWidget(self._status_label)
        self.setStatusBar(status_bar)

        # --- Refresh action (menu + toolbar) ---
        refresh_action = QAction("&Refresh", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self._refresh)
        menu = self.menuBar().addMenu("&Shell")
        menu.addAction(refresh_action)

        toolbar = self.addToolBar('Shell')
        toolbar.addAction(refresh_action)

        # --- Populate cards from catalog ---
        self._populate_cards()

    # ------------------------------------------------------------------
    # Card population
    # ------------------------------------------------------------------

    def _populate_cards(self) -> None:
        """Create one ProductCard per catalog entry, added to the layout."""
        try:
            descriptors = self._service.list_products()
        except Exception as exc:
            logger.error("list_products failed during startup: %s", exc)
            self._show_startup_error(
                f"Could not load product catalog: {exc}",
            )
            return

        cards_layout = self._cards_layout()
        if cards_layout is None:
            return

        for desc in descriptors:
            card = ProductCard(
                descriptor=desc,
                state=None,
                service=self._service,
                parent=self,
                resolver=self._resolver,
                fetcher=self._fetcher,
                work_root=self._work_root,
            )
            self._cards[desc.product_id] = card
            # Connect install_succeeded signal to trigger a full state refresh
            card.install_succeeded.connect(self._on_install_succeeded)
            # Insert before the stretch at the end
            cards_layout.insertWidget(cards_layout.count() - 1, card)

    def _cards_layout(self) -> QVBoxLayout | None:
        """Return the cards container's layout."""
        if self._cards_container is None:
            return None
        layout = self._cards_container.layout()
        if isinstance(layout, QVBoxLayout):
            return layout
        return None

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _on_install_succeeded(self, product_id: str) -> None:
        """Install finished; re-collect authoritative state."""
        self._refresh()

    def _refresh(self) -> None:
        """Collect fresh product state from the service and update cards.

        Calls ``service.collect_product_state()`` — never probes the
        filesystem or calls subprocess from Qt.
        """
        logger.debug("Refreshing product state")
        try:
            shell: ProductShellState = self._service.collect_product_state()
        except Exception as exc:
            logger.error("Refresh failed: %s", exc)
            self._show_startup_error(f"Could not collect product state: {exc}")
            if self._status_label:
                self._status_label.setText("Refresh failed")
            return

        # Clear any previous error
        if self._error_label and self._cards:
            self._error_label.setVisible(False)

        # Update each card
        for pstate in shell.products:
            card = self._cards.get(pstate.product_id)
            if card:
                card.refresh_state(pstate)

        # Update status bar
        self._update_status_bar(shell)

    def _update_status_bar(self, shell: ProductShellState) -> None:
        if self._status_label:
            self._status_label.setText(
                runtime_summary(
                    runtime_state_value=shell.runtime_state.value,
                    installed_count=shell.installed_count,
                    managed_count=shell.managed_count,
                    total_known=len(shell.products),
                )
            )

    def _show_startup_error(self, message: str) -> None:
        """Show a user-facing error in the window, never a blank window."""
        if self._error_label:
            self._error_label.setText(message)
            self._error_label.setVisible(True)
