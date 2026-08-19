"""M1-5-A — compact, clickable GPU acceleration status badge (home page).

Replaces the always-embedded full :class:`~zealfie.gui.acceleration_panel.AccelerationPanel`
on the product shell home page with a one-line status that opens the
Settings page when clicked.  The badge performs no probing and holds no
compatibility logic: it renders the status derived from the recommendation
observation already collected by the main window's refresh flow.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from zealfie.gui.presentation import compact_gpu_status
from zealfie.i18n import translate


class AccelerationBadge(QPushButton):
    """A flat, clickable one-line GPU acceleration status.

    Clicking it opens Settings (wired by the main window via ``clicked``).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._recommendation = None
        self._install_active = False
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(translate("gpu.badge.tooltip"))
        self._render()

    def set_status(self, recommendation, *, install_active: bool = False) -> None:
        """Render the badge from a recommendation observation (and install state)."""
        self._recommendation = recommendation
        self._install_active = bool(install_active)
        self._render()

    def retranslate(self) -> None:
        """Re-render the badge text and tooltip from the stored state."""
        self.setToolTip(translate("gpu.badge.tooltip"))
        self._render()

    def _render(self) -> None:
        self.setText(
            compact_gpu_status(
                self._recommendation,
                install_active=self._install_active,
            )
        )
