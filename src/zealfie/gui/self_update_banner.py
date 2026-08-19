"""Self-update proposal banner for the ZeAlfie product shell (ZA-M1-4.2).

A small, non-intrusive banner shown only once a *verified* self-update
candidate is ready.  It offers exactly two actions:

* **Update and restart** — the user accepts; the main window triggers the
  existing apply, then closes and restarts on the new version;
* **Later** — the user defers; the banner hides, the valid pending marker is
  preserved, and the proposal is not re-shown this session.

All user-visible text is localized via :func:`zealfie.i18n.translate`
(EN + FR); no hardcoded language strings live in the widget.  The widget
performs no network/build/pip work and never mutates anything — it only
emits :attr:`update_accepted` / :attr:`dismissed`.
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

__all__ = ["SelfUpdateBanner"]


class SelfUpdateBanner(QFrame):
    """Proposal banner: "ZeAlfie X.Y.Z is ready to be installed." + 2 actions."""

    update_accepted = Signal()
    dismissed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._busy = False
        # Current visible state, so a runtime language switch can re-render
        # the exact message (EN <-> FR) without any hardcoded strings.
        self._mode: str | None = None  # "ready" | "busy" | "error" | None
        self._version: str | None = None
        self._build_ui()
        self.hide()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setObjectName("selfUpdateBanner")
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        self._message_label = QLabel()
        self._message_label.setObjectName("selfUpdateMessageLabel")
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._message_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()

        self._update_button = QPushButton(translate("selfupdate.update_restart"))
        self._update_button.setObjectName("selfUpdateAcceptButton")
        self._update_button.setMinimumWidth(170)
        self._update_button.clicked.connect(self.update_accepted.emit)
        row.addWidget(self._update_button)

        self._later_button = QPushButton(translate("selfupdate.later"))
        self._later_button.setObjectName("selfUpdateLaterButton")
        self._later_button.setMinimumWidth(100)
        self._later_button.clicked.connect(self.dismissed.emit)
        row.addWidget(self._later_button)

        row.addStretch()
        outer.addLayout(row)

    # ------------------------------------------------------------------
    # State → UI
    # ------------------------------------------------------------------

    def show_ready(self, version: str | None) -> None:
        """Show the proposal for a ready candidate (target *version*)."""
        self._busy = False
        self._mode = "ready"
        self._version = version
        self._message_label.setText(
            translate("selfupdate.ready", version=(version or "?"))
        )
        self._apply_mode_stylesheet()
        self._set_buttons_enabled(True)
        self.show()

    def set_busy(self, busy: bool) -> None:
        """Disable/re-enable the actions while an apply is in progress."""
        self._busy = busy
        self._set_buttons_enabled(not busy)
        if busy:
            self._mode = "busy"
            self._message_label.setText(translate("selfupdate.applying"))
            self._apply_mode_stylesheet()

    def show_error(self) -> None:
        """Show an honest, localized apply-failure message; keep the actions.

        The pending marker is preserved by the engine, so the user may retry
        or dismiss; the shell stays fully usable.
        """
        self._busy = False
        self._mode = "error"
        self._message_label.setText(translate("selfupdate.apply_failed"))
        self._apply_mode_stylesheet()
        self._set_buttons_enabled(True)
        self.show()

    def dismiss(self) -> None:
        """Hide the proposal (deferred by the user; pending marker kept)."""
        self._busy = False
        self._mode = None
        self._apply_mode_stylesheet()
        self.hide()

    def retranslate(self) -> None:
        """Re-apply translated strings after a runtime language switch.

        Re-renders the two action buttons and, when the banner is showing a
        stateful message (ready / busy / error), re-renders that message with
        the stored state.  No hardcoded strings — all via the i18n catalogue.
        """
        self._update_button.setText(translate("selfupdate.update_restart"))
        self._later_button.setText(translate("selfupdate.later"))
        if self._mode == "ready":
            self._message_label.setText(
                translate("selfupdate.ready", version=(self._version or "?"))
            )
        elif self._mode == "busy":
            self._message_label.setText(translate("selfupdate.applying"))
        elif self._mode == "error":
            self._message_label.setText(translate("selfupdate.apply_failed"))

    def _apply_mode_stylesheet(self) -> None:
        """Apply the mode-appropriate stylesheet to the message label.

        Centralized so every mode transition recomputes the stylesheet:
        ``error`` uses a red message, every other mode uses the theme
        default (no override).  This guarantees a retry after an error
        can never leave a stale red style behind while the banner is
        busy or ready.
        """
        if self._mode == "error":
            self._message_label.setStyleSheet("color: #c0392b;")
        else:
            self._message_label.setStyleSheet("")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._update_button.setEnabled(enabled)
        self._later_button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def message_text(self) -> str:
        return self._message_label.text()
