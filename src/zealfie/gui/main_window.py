"""M1-2D.5 — ZeAlfie main window with non-blocking install.

The top-level product shell window hosting product cards, a status bar,
and install coordination via a QThread worker.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from zealfie.app import (
    ProductShellState,
    ProductUpdateResult,
    UpdateCheckCoordinator,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.app.update_checks import CheckFunction
from zealfie.sources.acquisition import ArchiveFetcher
from zealfie.sources import SourceRefResolver
from zealfie.i18n import Language, LanguageStore, get_language, set_language, translate

from .presentation import action_enabled, runtime_summary
from .product_card import ProductCard
from .install_worker import create_install_thread
from .update_bridge import UpdateResultBridge
from .acceleration_panel import AccelerationPanel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known UX limitation — shown during active install (text via i18n)
# ---------------------------------------------------------------------------

class ZeAlfieMainWindow(QMainWindow):
    """ZeAlfie product shell main window.

    Composition root responsibility: owns the QMainWindow, populates
    product cards, handles refresh, and displays global status.

    **M1-2D.5:** Coordinates product installs via a QThread worker so the
    GUI stays responsive during synchronous ``install_product`` calls.

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
        check_fn: CheckFunction | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._resolver = resolver
        self._fetcher = fetcher
        self._work_root = work_root
        self._check_fn = check_fn
        self._cards: dict[str, ProductCard] = {}
        self._status_label: QLabel | None = None
        self._error_label: QLabel | None = None
        self._subtitle_label: QLabel | None = None
        self._cards_container: QWidget | None = None
        self._acceleration_panel: AccelerationPanel | None = None
        self._language_actions: dict = {}

        # M1-2D.5: global install coordination
        self._install_active: bool = False
        self._active_install_pid: str | None = None
        self._install_thread: object | None = None  # QThread
        self._install_worker: object | None = None  # InstallWorker (QObject)
        # M1-2E E.6a: "install" or "update" — the operation the current
        # worker thread is running (None when idle).  Both share one lock.
        self._active_operation: str | None = None
        self._install_progress_bar: QProgressBar | None = None
        self._known_limitation_label: QLabel | None = None
        self._refresh_action: QAction | None = None

        # M1-2E LOT E.4: read-only update-check coordination.
        self._update_coordinator: UpdateCheckCoordinator | None = None
        self._update_bridge = UpdateResultBridge(self)
        self._update_bridge.update_result_ready.connect(self._on_update_result)

        self._build_ui()
        self._refresh()
        # Start update checks after the initial refresh; non-blocking and
        # a no-op when no check function / resolver is available.
        self.start_update_checks()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle(translate("app.title"))
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

        self._subtitle_label = QLabel(translate("app.subtitle"))
        self._subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self._subtitle_label)

        central_layout.addLayout(header_layout)

        # --- Hardware acceleration panel (M1-2G) ---
        # Composition-root transports are threaded down so the
        # accelerated install worker re-acquires the KEEP base runtime
        # at the exact provenance SHA (ZA-M1-2J.1).  ``None`` defaults
        # preserve the fail-closed service behaviour for callers
        # without transports (tests, headless).
        self._acceleration_panel = AccelerationPanel(
            self._service,
            self,
            fetcher=self._fetcher,
            work_root=self._work_root,
        )
        self._acceleration_panel.setObjectName("accelerationPanel")
        central_layout.addWidget(self._acceleration_panel)

        # Error label (hidden by default, shown on startup failure)
        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: #c0392b; font-weight: bold;")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        central_layout.addWidget(self._error_label)

        # --- Known UX limitation label (hidden unless install active) ---
        self._known_limitation_label = QLabel(translate("app.known_limitation"))
        self._known_limitation_label.setStyleSheet(
            "color: #e67e22; font-style: italic;"
        )
        self._known_limitation_label.setWordWrap(True)
        self._known_limitation_label.setVisible(False)
        self._known_limitation_label.setObjectName("knownLimitationLabel")
        central_layout.addWidget(self._known_limitation_label)

        # --- Indeterminate progress bar (hidden unless install active) ---
        self._install_progress_bar = QProgressBar()
        self._install_progress_bar.setMinimum(0)
        self._install_progress_bar.setMaximum(0)  # indeterminate = honest UX
        self._install_progress_bar.setVisible(False)
        self._install_progress_bar.setObjectName("installProgressBar")
        self._install_progress_bar.setTextVisible(False)
        central_layout.addWidget(self._install_progress_bar)

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
        self._status_label = QLabel(translate("status.starting"))
        self._status_label.setObjectName("statusLabel")
        status_bar.addWidget(self._status_label)
        self.setStatusBar(status_bar)

        # --- Refresh action (menu + toolbar) ---
        self._refresh_action = QAction("&Refresh", self)
        self._refresh_action.setShortcut("F5")
        self._refresh_action.triggered.connect(self._refresh)
        menu = self.menuBar().addMenu("&Shell")
        menu.addAction(self._refresh_action)
        self._build_language_menu(menu)

        toolbar = self.addToolBar('Shell')
        toolbar.addAction(self._refresh_action)

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
                translate("error.catalog_load", exc=exc),
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
            # M1-2D.5: connect install_requested (instead of install_succeeded)
            card.install_requested.connect(self._on_install_requested)
            # M1-2E E.6a: connect update_requested (same worker path)
            card.update_requested.connect(self._on_update_requested)
            # M1-2F Phase 5: connect channel_changed (persist + re-check)
            card.channel_changed.connect(self._on_channel_changed)
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

    def _refresh(self) -> None:
        """Collect fresh product state and refresh the acceleration panel.

        M1-2D.5: Refuses to run while an install is active.
        """
        if self._install_active:
            logger.debug("Refresh blocked — install in progress")
            if self._status_label:
                self._status_label.setText(
                    translate("status.refresh_deferred")
                )
            return

        self._refresh_products()
        self._refresh_acceleration()

    def _refresh_products(self) -> None:
        """Collect product state and update cards + status bar (no probing)."""
        logger.debug("Refreshing product state")
        try:
            shell: ProductShellState = self._service.collect_product_state()
        except Exception as exc:
            logger.error("Refresh failed: %s", exc)
            self._show_startup_error(translate("error.collect_state", exc=exc))
            if self._status_label:
                self._status_label.setText(translate("status.refresh_failed"))
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

    def _refresh_acceleration(self) -> None:
        """Update the acceleration panel from the service observation.

        Exactly one observation cycle: when the service exposes
        ``collect_host_capabilities``, the capabilities are collected
        once, the recommendation is derived from that exact
        observation, and both are stored in the panel — so the
        configure click's plan preview never triggers a second
        hardware observation.

        Tolerates services without the acceleration API and any probe
        failure — the panel falls back to an honest unknown/error state and
        the main window never crashes.
        """
        if self._acceleration_panel is None:
            return
        getter = getattr(self._service, "get_acceleration_recommendation", None)
        if not callable(getter):
            self._acceleration_panel.set_unknown()
            return
        collector = getattr(self._service, "collect_host_capabilities", None)
        try:
            if callable(collector):
                capabilities = collector()
                recommendation = getter(capabilities)
            else:
                capabilities = None
                recommendation = getter()
        except Exception as exc:
            logger.error("acceleration recommendation failed: %s", exc)
            self._acceleration_panel.set_error(str(exc))
            return
        self._acceleration_panel.set_recommendation(
            recommendation, capabilities=capabilities
        )

    # ------------------------------------------------------------------
    # Runtime language selection
    # ------------------------------------------------------------------

    def _build_language_menu(self, menu) -> None:
        """Add a Language submenu with English / Français actions."""
        lang_menu = menu.addMenu("&Language")
        for lang, label in ((Language.EN, "English"), (Language.FR, "Français")):
            action = QAction(label, self, checkable=True)
            action.setChecked(get_language() is lang)
            action.triggered.connect(
                lambda checked=False, l=lang: self._on_language_selected(l)
            )
            lang_menu.addAction(action)
            self._language_actions[lang] = action

    def _on_language_selected(self, lang: Language) -> None:
        """Switch the UI language, persist the preference, and re-render."""
        set_language(lang)
        try:
            LanguageStore().save(lang)
        except Exception as exc:
            logger.warning("failed to persist language preference: %s", exc)
        self._retranslate()

    def _retranslate(self) -> None:
        """Re-apply every translated string and rebuild the cards.

        Re-applies static labels, re-renders the acceleration panel from its
        stored observation (no re-probe), rebuilds the product cards, and
        refreshes their state.  Deliberately does not re-run update checks or
        re-probe host capabilities.
        """
        self.setWindowTitle(translate("app.title"))
        if self._subtitle_label is not None:
            self._subtitle_label.setText(translate("app.subtitle"))
        if self._known_limitation_label is not None:
            self._known_limitation_label.setText(translate("app.known_limitation"))
        for lang, action in self._language_actions.items():
            action.setChecked(get_language() is lang)
        if self._acceleration_panel is not None:
            self._acceleration_panel.retranslate()
        self._clear_cards()
        self._populate_cards()
        self._refresh_products()

    def _clear_cards(self) -> None:
        """Remove every product card from the cards layout."""
        layout = self._cards_layout()
        if layout is None:
            self._cards = {}
            return
        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards = {}

    # ------------------------------------------------------------------
    # M1-2E LOT E.4: Read-only update checks (informational only)
    # ------------------------------------------------------------------

    def _build_check_fn(self) -> CheckFunction | None:
        """Resolve the update-check callable, if any is available.

        Prefers the explicitly injected *check_fn*.  Otherwise, when a
        resolver is wired and the service exposes ``check_product_update``,
        builds a read-only check function from ``service + resolver``.
        Returns ``None`` when no update checking is possible (no network,
        no resolver) — cards then stay ``NOT_CHECKED`` (no label).
        """
        if self._check_fn is not None:
            return self._check_fn
        if self._resolver is not None and hasattr(
            self._service, "check_product_update"
        ):
            resolver = self._resolver
            service = self._service
            return lambda product_id: service.check_product_update(
                product_id, resolver=resolver
            )
        return None

    def start_update_checks(self) -> None:
        """Start read-only, non-blocking update checks for the visible cards.

        Idempotent: if a coordinator is already running, this is a no-op.
        Never blocks on network/resolution; results are delivered to cards
        asynchronously via the GUI-thread bridge.
        """
        if self._update_coordinator is not None:
            return
        check_fn = self._build_check_fn()
        if check_fn is None:
            return
        coordinator = UpdateCheckCoordinator(check_fn)
        coordinator.add_observer(self._update_bridge.notify)
        self._update_coordinator = coordinator
        product_ids = tuple(self._cards.keys())
        if product_ids:
            coordinator.start(product_ids)

    def _on_update_result(self, result: ProductUpdateResult) -> None:
        """GUI-thread slot: route a coordinator result to the matching card."""
        card = self._cards.get(result.product_id)
        if card is not None:
            card.set_update_status(result)

    def _shutdown_update_checks(self) -> None:
        """Stop the update-check coordinator without blocking the GUI thread."""
        coordinator = self._update_coordinator
        if coordinator is not None:
            coordinator.shutdown(wait=False)
            self._update_coordinator = None

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

    # ------------------------------------------------------------------
    # M1-2D.5: Install coordination via worker thread
    # ------------------------------------------------------------------

    def _on_install_requested(self, product_id: str) -> None:
        """Handle an install request from a product card.

        Guards:
        - Only one install at a time — second request is silently ignored.
        - Install dependencies must be wired (checked by the card before emitting).
        """
        if self._install_active:
            logger.debug(
                "Install request for %r ignored — install already active for %r",
                product_id, self._active_install_pid,
            )
            return

        # --- Acquire global install lock ---
        card = self._cards.get(product_id)
        if card is None:
            logger.error("Install requested for unknown product %r", product_id)
            return

        self._install_active = True
        self._active_install_pid = product_id
        self._active_operation = "install"
        self._set_global_install_lock(True)

        # --- Update the requesting card to "in progress" ---
        card.set_install_in_progress(True)

        # --- Create and start the worker thread ---
        self._install_thread, self._install_worker = create_install_thread(
            product_id,
            self._service,
            resolver=self._resolver,  # type: ignore[arg-type]
            fetcher=self._fetcher,    # type: ignore[arg-type]
            work_root=self._work_root,  # type: ignore[arg-type]
            parent=self,
        )

        worker = self._install_worker
        worker.install_succeeded.connect(self._on_worker_success)
        worker.install_failed.connect(self._on_worker_failure)
        worker.progress.connect(card.set_install_progress)
        # Worker lifecycle is handled in create_install_thread:
        #   worker.finished → worker.deleteLater (while thread loop alive)
        #   worker.destroyed → thread.quit

        thread = self._install_thread
        thread.finished.connect(lambda: self._cleanup_thread(thread))

        self._install_thread.start()
        logger.info("Install worker started for %r", product_id)

        # Scramble the status bar
        if self._status_label:
            self._status_label.setText(
                translate("status.installing", name=card._descriptor.display_name)
            )

    def _on_update_requested(self, product_id: str) -> None:
        """Handle an update request from a product card (M1-2E E.6a).

        Uses the exact same global lock and worker-thread plumbing as
        install; the only difference is the worker operation mode
        (``service.update_product``) and the user-facing wording.
        """
        if self._install_active:
            logger.debug(
                "Update request for %r ignored — transaction active for %r",
                product_id, self._active_install_pid,
            )
            return

        card = self._cards.get(product_id)
        if card is None:
            logger.error("Update requested for unknown product %r", product_id)
            return

        if self._resolver is None or self._fetcher is None or self._work_root is None:
            card.set_update_error(translate("error.update_deps_missing"))
            logger.error("Update deps not wired for product %r", product_id)
            return

        self._install_active = True
        self._active_install_pid = product_id
        self._active_operation = "update"
        self._set_global_install_lock(True)

        card.set_update_in_progress(True)

        self._install_thread, self._install_worker = create_install_thread(
            product_id,
            self._service,
            resolver=self._resolver,  # type: ignore[arg-type]
            fetcher=self._fetcher,    # type: ignore[arg-type]
            work_root=self._work_root,  # type: ignore[arg-type]
            operation="update",
            parent=self,
        )

        worker = self._install_worker
        worker.install_succeeded.connect(self._on_worker_success)
        worker.install_failed.connect(self._on_worker_failure)
        worker.progress.connect(card.set_install_progress)

        thread = self._install_thread
        thread.finished.connect(lambda: self._cleanup_thread(thread))

        self._install_thread.start()
        logger.info("Update worker started for %r", product_id)

        if self._status_label:
            self._status_label.setText(
                translate("status.updating", name=card._descriptor.display_name)
            )

    def _on_channel_changed(self, product_id: str, channel: str) -> None:
        """Persist a channel change and re-run the read-only update check.

        The card only offers declared channels, so this normally succeeds.
        On failure the card is re-synced to the persisted truth (no policy
        mutation is attempted directly by the card itself).
        """
        set_channel = getattr(self._service, "set_product_channel", None)
        if callable(set_channel):
            try:
                set_channel(product_id, channel)
            except Exception as exc:
                logger.error(
                    "set_product_channel failed for %r: %s", product_id, exc
                )

        card = self._cards.get(product_id)
        if card is not None:
            card.refresh_policy()

        coordinator = self._update_coordinator
        if coordinator is not None:
            coordinator.start((product_id,))

    def _set_global_install_lock(self, locked: bool) -> None:
        """Enable/disable install buttons, progress bar, and refresh action."""
        # Progress bar visibility
        if self._install_progress_bar:
            self._install_progress_bar.setVisible(locked)

        # Known limitation label visibility
        if self._known_limitation_label:
            self._known_limitation_label.setVisible(locked)

        # Refresh action: disable during install
        if self._refresh_action:
            self._refresh_action.setEnabled(not locked)

        # Cards: disable all install buttons
        for c in self._cards.values():
            if locked:
                c._action_button.setEnabled(False)
                if c._update_button is not None:
                    c._update_button.setEnabled(False)
            else:
                # Re-enable based on current state (unless awaiting refresh)
                if c._awaiting_install_refresh:
                    c._action_button.setEnabled(False)
                elif c._state is not None:
                    c._action_button.setEnabled(
                        action_enabled(c._state) and not c._spawning and not c._installing
                    )
                else:
                    c._action_button.setEnabled(False)
                c._update_update_button_enabled()

    def _on_worker_success(self, product_id: str) -> None:
        """Operation succeeded — collect authoritative state via refresh.

        For updates, additionally re-runs the read-only update check so the
        card's update display becomes authoritative (``Up to date``) without
        any direct service script (M1-2E E.6a).
        """
        operation = self._active_operation
        logger.info("%s succeeded for %r; refreshing", operation, product_id)
        card = self._cards.get(product_id)
        if card:
            if operation == "update":
                card._status_label.setText(translate("status.update_complete_refreshing"))
            else:
                card._status_label.setText(translate("status.install_complete_refreshing"))

        # Refresh to get authoritative state
        try:
            shell: ProductShellState = self._service.collect_product_state()
        except Exception as exc:
            logger.error("Post-install refresh failed for %r: %s", product_id, exc)
            # Refresh failed after successful install — safe fallback
            for c in self._cards.values():
                c.set_install_complete_refresh_required()
            if self._status_label:
                self._status_label.setText(translate("status.refresh_failed_after_install"))
            return

        # Apply new state to all cards
        if self._error_label and self._cards:
            self._error_label.setVisible(False)

        for pstate in shell.products:
            c = self._cards.get(pstate.product_id)
            if c:
                c.refresh_state(pstate)

        self._update_status_bar(shell)

        if operation == "update":
            self._recheck_update(product_id)

    def _on_worker_failure(self, product_id: str, message: str) -> None:
        """Operation failed — show error, allow retry/launch when possible."""
        operation = self._active_operation
        logger.warning("%s failed for %r: %s", operation, product_id, message)
        card = self._cards.get(product_id)
        if card:
            if operation == "update":
                card.set_update_error(message)
            else:
                card.set_install_error(message)

        if self._status_label:
            self._status_label.setText(
                translate("status.update_failed")
                if operation == "update"
                else translate("status.install_failed")
            )

    def _recheck_update(self, product_id: str) -> None:
        """Re-run the read-only update check after a successful update.

        Prefers the existing non-blocking coordinator (results delivered to
        the card via the GUI-thread bridge).  When no coordinator/check_fn is
        wired, ``update_product`` has already applied the latest commit, so we
        mark the card ``UP_TO_DATE`` directly rather than leave a stale
        ``Update available`` label behind.
        """
        coordinator = self._update_coordinator
        if coordinator is not None:
            coordinator.start((product_id,))
            return
        card = self._cards.get(product_id)
        if card is not None:
            card.set_update_status(
                ProductUpdateResult(product_id=product_id, status=UpdateStatus.UP_TO_DATE)
            )

    def _cleanup_thread(self, thread) -> None:
        """Release install lock and schedule thread deletion.

        Called when thread.finished fires, after the worker has been
        deleted and the thread event loop has quit.

        Does NOT call worker.deleteLater() — that is scheduled by
        create_install_thread while the thread event loop is still alive.
        Does NOT call QThread.terminate().
        """
        if thread is not self._install_thread:
            # Stale callback from an old thread — ignore
            return

        # Wait for thread event loop to fully exit (safety, should be stopped)
        if thread.isRunning():
            thread.wait(5000)  # 5s timeout

        # Release install lock
        self._install_active = False
        self._active_install_pid = None
        self._active_operation = None
        self._set_global_install_lock(False)

        # Schedule thread for deletion on the main event loop
        thread.deleteLater()
        self._install_worker = None
        self._install_thread = None

    # ------------------------------------------------------------------
    # Close event — reject during active install
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Reject close when an install is in progress.

        Does NOT call QThread.terminate().
        """
        if self._install_active:
            if self._status_label:
                self._status_label.setText(
                    translate("status.install_in_progress_wait")
                )
            event.ignore()
            return
        self._shutdown_update_checks()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Test helpers (used by test suite)
    # ------------------------------------------------------------------

    @property
    def install_active(self) -> bool:
        """Expose install-active state for test assertions."""
        return self._install_active

    @property
    def active_install_pid(self) -> str | None:
        """The product id currently being installed (if any)."""
        return self._active_install_pid
