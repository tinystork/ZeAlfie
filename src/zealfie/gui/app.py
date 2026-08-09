"""M1-2C — GUI composition root.

Orchestrates ``QApplication``, ``ZeAlfieService``, and
``ZeAlfieMainWindow``.  This is the ``zealfie-gui`` startup path.
"""

from __future__ import annotations

import logging
import sys

from zealfie.app import ZeAlfieService

from .main_window import ZeAlfieMainWindow

logger = logging.getLogger(__name__)


def run_gui() -> None:
    """Composition root for the ZeAlfie product shell GUI.

    - Creates ``QApplication`` before any other Qt object.
    - Instantiates ``ZeAlfieService`` with default dependencies.
    - Creates and shows ``ZeAlfieMainWindow(service=service)``.
    - Enters the Qt event loop; returns when the window closes.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("ZeAlfie")
    app.setOrganizationName("ZeSoftware")

    service = ZeAlfieService()

    window = ZeAlfieMainWindow(service=service)
    window.show()

    sys.exit(app.exec())
