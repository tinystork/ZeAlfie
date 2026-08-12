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

    # M1-2E LOT E.4: read-only update checks (informational only).  The
    # window only starts checks because a resolver is available here; it
    # remains strictly read-only and non-blocking on the GUI thread.
    check_fn = lambda product_id: service.check_product_update(
        product_id, resolver=resolver
    )

    window = ZeAlfieMainWindow(
        service=service,
        resolver=resolver,
        fetcher=fetcher,
        work_root=work_root,
        check_fn=check_fn,
    )
    window.show()

    sys.exit(app.exec())
