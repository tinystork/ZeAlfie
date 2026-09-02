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
from zealfie.i18n import effective_language, set_language
from zealfie.selfupdate import (
    make_self_update_apply_fn,
    make_self_update_check_fn,
    restart_gui_after_update,
)
from zealfie.selfupdate.resolver import GitHubTagsLister
from zealfie.runtime.layout import default_runtime_layout

from .main_window import ZeAlfieMainWindow
from .icon import apply_app_icon

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

    # ZA-ICON-01: ZeAlfie identity at the QApplication level.  Qt then
    # uses this icon for every top-level window/dialog that does not set
    # its own (window decorations, taskbar/alt-tab, dialogs, …).
    # Best-effort: a missing icon asset must never block GUI startup.
    # NOTE (future Windows packager): the EXE-embedded icon and the
    # AppUserModelID are packager concerns (see zealfie.gui.icon) and
    # are intentionally not implemented here.
    apply_app_icon(app)

    service = ZeAlfieService()

    # ZA-M1-4 LOT A: confirm the persisted ACTIVE runtime health after a
    # fresh startup.  Best-effort only -- the GUI must never crash if the
    # confirmation is impossible (no runtime, corrupt state, no Python, …).
    try:
        service.confirm_startup_runtime_health()
    except Exception:
        logger.warning(
            "startup runtime health confirmation failed (ignored)",
            exc_info=True,
        )

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

    # ZA-M1-4.2: wire the existing self-update engine onto the GUI shell.
    # The check + stage (background) and apply (worker thread) run off the
    # GUI thread; the restart spawns a detached supervisor that relaunches
    # zealfie-gui after the standalone activator/helper finishes.
    layout = default_runtime_layout()
    tags_lister = GitHubTagsLister()
    self_update_check_fn = make_self_update_check_fn(
        resolver=resolver,
        tags_lister=tags_lister,
        fetcher=fetcher,
        work_root=work_root,
        layout=layout,
        channel="stable",
    )
    self_update_apply_fn = make_self_update_apply_fn(layout=layout)
    self_update_restart_fn = lambda: restart_gui_after_update(
        runtime_root=layout.root
    )

    # M1-4 LOT E: apply the persisted preference (or first-run locale
    # inference) before building the UI so the shell renders in the right
    # language from the start.
    set_language(effective_language())

    window = ZeAlfieMainWindow(
        service=service,
        resolver=resolver,
        fetcher=fetcher,
        work_root=work_root,
        check_fn=check_fn,
        self_update_check_fn=self_update_check_fn,
        self_update_apply_fn=self_update_apply_fn,
        self_update_restart_fn=self_update_restart_fn,
    )
    window.show()

    sys.exit(app.exec())
