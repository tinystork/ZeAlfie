"""ZeAlfie macOS bundle — bounded offscreen GUI instantiation smoke.

Executed with the **bundled** private interpreter
(``ZeAlfie.app/Contents/Resources/python/bin/python3.13``) under
``QT_QPA_PLATFORM=offscreen``.  It constructs the real ``QApplication`` and
the real product shell window (:class:`ZeAlfieMainWindow`) with the real
:class:`ZeAlfieService`, drains pending events for a bounded moment and
exits — no interactive action, no waiting event loop, no network (every
check/update hook is deliberately unwired).

Isolation from the managed-product runtime: the service is built on a
``SharedRuntime`` whose layout root is a throwaway temp directory under the
witness area, so ``~/Library/Application Support/zealfie/runtime``
(slots/state/cache) is never read or written, and nothing is written into
the bundle.

Exit code 0 only when the window is constructed, shown offscreen and the
event loop drains cleanly.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

# Offscreen must be decided before QApplication is created.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@contextlib.contextmanager
def _isolated_runtime_root(work_root: Path):
    """Yield a throwaway ``SharedRuntime`` root scoped to the caller's work.

    The allocation lives under the caller-supplied ``work_root`` (never the
    real ``~/Library/Application Support/zealfie/runtime`` and never inside
    the bundle) and is removed automatically on both success and failure, so
    the smoke never leaks owned scratch.  The caller's ``work_root`` itself
    and any pre-existing files in it are preserved.
    """
    with tempfile.TemporaryDirectory(
        prefix="zealfie-macos-gui-smoke-runtime-", dir=str(work_root)
    ) as allocated:
        yield Path(allocated)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="zealfie-macos-gui-smoke",
        description="bounded offscreen GUI instantiation smoke (ZA-MAC-BOOT-01)",
    )
    parser.add_argument("--work-root", required=True, type=Path)
    args = parser.parse_args(argv)

    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from zealfie.app import ZeAlfieService
    from zealfie.runtime.layout import RuntimeLayout
    from zealfie.runtime.manager import SharedRuntime

    # Fully isolated throwaway runtime under the caller's work_root.  The
    # temporary root encloses the service and window work and is removed
    # automatically (success or failure).
    with _isolated_runtime_root(work_root) as isolated_root:
        runtime = SharedRuntime(layout=RuntimeLayout(root=isolated_root))
        service = ZeAlfieService(runtime=runtime)

        app = QApplication(sys.argv[:1])
        app.setApplicationName("ZeAlfie")
        app.setOrganizationName("ZeSoftware")

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

        QTimer.singleShot(1500, app.quit)
        exit_code = app.exec()
        if exit_code != 0:
            print(
                f"QT_SMOKE FAILED: event loop exited rc={exit_code}",
                file=sys.stderr,
            )
            return 1
        title = window.windowTitle()

    print(
        "QT_SMOKE=PASS "
        f"title={title!r} interpreter={sys.executable}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
