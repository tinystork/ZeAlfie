"""Qt worker + bridge for GUI self-update (ZA-M1-4.2).

Two pieces, mirroring the existing conventions in ZeAlfie:

* :class:`SelfUpdateResultBridge` — a :class:`QObject` living on the GUI
  thread that re-emits a self-update check result (computed on a background
  daemon :class:`threading.Thread`, so interpreter exit is never blocked)
  onto the GUI thread via a queued connection.  Mirrors
  :class:`~zealfie.gui.update_bridge.UpdateResultBridge`.

* :class:`SelfUpdateApplyWorker` — a :class:`QObject` that runs the existing
  ``apply_pending_update`` call on a :class:`QThread` (never the GUI thread),
  mirroring :mod:`zealfie.gui.install_worker`.  Emits exactly one
  :attr:`apply_finished` with the :class:`~zealfie.selfupdate.SelfUpdateApplyResult`,
  followed by :attr:`finished`.

Neither object performs network/build/pip work on the GUI thread, and neither
mutates the running environment directly: staging writes a pending marker,
and apply is the standalone activator (in-process Linux subprocess pip, or a
detached Windows helper).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal

from zealfie.selfupdate import (
    ApplyStatus,
    SelfUpdateApplyResult,
)
from zealfie.selfupdate.orchestration import (
    GuiSelfUpdateResult,
    GuiSelfUpdateStatus,
)

logger = logging.getLogger(__name__)


class SelfUpdateResultBridge(QObject):
    """Re-emit a check result from a worker thread onto the GUI thread.

    Created with the main window as parent so it lives on the GUI thread;
    ``notify`` is the observer callable handed to the background check and
    may be called from any thread.
    """

    result_ready = Signal(object)  # GuiSelfUpdateResult

    def notify(self, result: GuiSelfUpdateResult) -> None:
        """Observer entry point (thread-agnostic)."""
        self.result_ready.emit(result)


class SelfUpdateApplyWorker(QObject):
    """Run the standalone apply on a worker thread.

    The caller MUST move this object to the worker thread before calling
    ``run()``.  Emits exactly one ``apply_finished`` (with the apply result)
    followed by ``finished``.  Never raises out of ``run``: an unexpected
    exception is converted to a ``FAILED`` result (fail-closed).
    """

    #: Emitted (from worker thread) with a ``SelfUpdateApplyResult``.
    apply_finished = Signal(object)

    #: Emitted (from worker thread) after ``apply_finished``, unconditionally.
    finished = Signal()

    def __init__(self, apply_fn, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._apply_fn = apply_fn

    def run(self) -> None:
        """Execute the apply call (called from worker thread)."""
        try:
            result = self._apply_fn()
        except Exception as exc:  # noqa: BLE001 - defensive: apply never escapes
            logger.exception("self-update apply raised unexpectedly")
            result = SelfUpdateApplyResult(ApplyStatus.FAILED, str(exc))
        self.apply_finished.emit(result)
        self.finished.emit()


def create_self_update_apply_thread(
    apply_fn,
    parent: QObject | None = None,
) -> tuple[QThread, SelfUpdateApplyWorker]:
    """Create a worker + thread wired for one self-update apply.

    Returns ``(thread, worker)`` after moving the worker to the thread and
    connecting ``thread.started`` → ``worker.run``.  The caller is
    responsible for connecting worker signals to GUI slots, calling
    ``thread.start()``, and connecting ``thread.finished`` to cleanup on the
    main event loop (mirroring the main window's install pattern).
    """
    thread = QThread(parent)
    worker = SelfUpdateApplyWorker(apply_fn, parent=None)
    worker.moveToThread(thread)

    worker.finished.connect(worker.deleteLater)
    worker.destroyed.connect(thread.quit)
    thread.started.connect(worker.run)
    return thread, worker
