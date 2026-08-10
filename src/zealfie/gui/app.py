"""M1-2C — GUI composition root.

Orchestrates ``QApplication``, ``ZeAlfieService``, and
``ZeAlfieMainWindow``.  This is the ``zealfie-gui`` startup path.
"""

from __future__ import annotations

import logging
import sys

from zealfie.app import ZeAlfieService
from zealfie.sources.github import GitHubArchiveFetcher, GitHubSourceRefResolver
from zealfie.app.install_defaults import default_install_work_root

from .main_window import ZeAlfieMainWindow

logger = logging.getLogger(__name__)


def run_gui() -> None:
    """Composition root for the ZeAlfie product shell GUI.

    - Creates ``QApplication`` before any other Qt object.
    - Instantiates ``ZeAlfieService`` with default dependencies.
    - Creates default GitHub transports for remote product install.
    - Creates and shows ``ZeAlfieMainWindow(service=service)``.
    - Enters the Qt event loop; returns when the window closes.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("ZeAlfie")
    app.setOrganizationName("ZeSoftware")

    service = ZeAlfieService()
    resolver = GitHubSourceRefResolver()
    fetcher = GitHubArchiveFetcher()
    work_root = default_install_work_root()
    work_root.mkdir(parents=True, exist_ok=True)

    window = ZeAlfieMainWindow(
        service=service,
        resolver=resolver,
        fetcher=fetcher,
        work_root=work_root,
    )
    window.show()

    sys.exit(app.exec())
