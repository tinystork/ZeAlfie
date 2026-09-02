"""ZeAlfie Windows bootstrap — bounded offscreen GUI instantiation smoke.

Executed with the **appenv** python (``<witness-root>\\appenv\\Scripts\\python.exe``)
under ``QT_QPA_PLATFORM=offscreen``.  It constructs the real
``QApplication`` + the real product shell window (:class:`ZeAlfieMainWindow`)
with the real :class:`ZeAlfieService`, then drains pending events for a
bounded moment and exits — NO interactive user action, no event loop that
waits on the user, no network (every check/update hook is unwired).

Isolation from the ZeSoftware shared runtime: the service is built on a
``SharedRuntime`` whose layout root is a throwaway temp directory under the
witness area, so ``%LOCALAPPDATA%\\zealfie\\runtime`` (slots/state/cache) is
never read or written.  The window constructor's ``apply_window_icon`` and
the service's runtime-health confirmation are best-effort by design; a smoke
failure is a real GUI-construction failure, never a cosmetic one.

Exit code 0 only when the window is constructed, shown offscreen, and the
event loop drains cleanly.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Offscreen must be decided before QApplication is created.  A real
# (non-offscreen) platform plugin is not required anywhere in this smoke.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="zealfie-boot-gui-smoke",
        description="bounded offscreen GUI instantiation smoke (ZA-WIN-BOOT-01)",
    )
    parser.add_argument("--work-root", required=True, type=Path)
    args = parser.parse_args(argv)

    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    # Qt imports are deferred: the smoke must be importable (--help) even on
    # an interpreter without PySide6, and the appenv always provides it.
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from zealfie.app import ZeAlfieService
    from zealfie.runtime.layout import RuntimeLayout
    from zealfie.runtime.manager import SharedRuntime

    # Fully isolated throwaway runtime — never the real shared runtime.
    isolated_root = Path(tempfile.mkdtemp(prefix="zealfie-gui-smoke-runtime-"))
    runtime = SharedRuntime(layout=RuntimeLayout(root=isolated_root))
    service = ZeAlfieService(runtime=runtime)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("ZeAlfie")
    app.setOrganizationName("ZeSoftware")

    # Construct the REAL product shell window.  All check/update hooks are
    # deliberately unwired (None): the smoke proves instantiation, not
    # network behaviour, and nothing may leave the machine.
    from zealfie.gui.main_window import ZeAlfieMainWindow

    window = ZeAlfieMainWindow(
        service=service,
        work_root=work_root,
        resolver=None,
        fetcher=None,
        check_fn=None,
        self_update_check_fn=None,
        self_update_apply_fn=None,
        self_update_restart_fn=None,
    )
    window.show()

    # Bounded drain: process the pending show/refresh events, then quit.
    # Never enters an interactive loop; QTimer is used purely to bound the
    # offscreen event processing window.
    QTimer.singleShot(1500, app.quit)
    exit_code = app.exec()
    if exit_code != 0:
        print(f"GUI SMOKE FAILED: event loop exited rc={exit_code}", file=sys.stderr)
        return 1

    print(
        "GUI SMOKE PASS: ZeAlfieMainWindow constructed and shown offscreen "
        f"(title={window.windowTitle()!r}) on interpreter {sys.executable}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
