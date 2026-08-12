"""M1-2D.5 — minimal Qt worker to run synchronous install off the GUI thread.

Single-purpose: wraps a single ``service.install_product(…)`` call in a
QThread so the Qt event loop stays responsive during potentially long
installation operations.

Deliberately minimal — no queues, no generic job framework, no
cancellation, no async, no thread pool.  Structured backend progress is
forwarded verbatim through the ``progress`` signal; the worker never
computes business progress itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from zealfie.app import ZeAlfieService
from zealfie.sources.acquisition import ArchiveFetcher
from zealfie.sources import SourceRefResolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Worker — lives on the worker thread
# ---------------------------------------------------------------------------


class InstallWorker(QObject):
    """QObject that runs a single ``install_product`` call in a worker thread.

    The caller MUST move this object to the worker thread before calling
    ``run()``.  Do NOT call ``run()`` from the GUI thread.

    Emits exactly one of ``install_succeeded`` or ``install_failed``,
    followed by ``finished``.
    """

    #: Emitted (from worker thread) when install_product succeeds.
    install_succeeded = Signal(str)  # product_id

    #: Emitted (from worker thread) when install_product fails or raises.
    #: The str payload is a user-friendly message (no tracebacks).
    install_failed = Signal(str, str)  # product_id, error_message

    #: Emitted (from worker thread) after success/failure, unconditionally.
    finished = Signal()

    #: Emitted (from worker thread) for each backend progress observation.
    #: The payload is a :class:`~zealfie.app.progress.InstallProgress`
    #: (frozen dataclass with ``phase``, ``percent``, ``message``).  The
    #: worker does not compute progress; it only relays the backend's.
    progress = Signal(object)

    def __init__(
        self,
        product_id: str,
        service: ZeAlfieService,
        *,
        resolver: SourceRefResolver,
        fetcher: ArchiveFetcher,
        work_root: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._product_id = product_id
        self._service = service
        self._resolver = resolver
        self._fetcher = fetcher
        self._work_root = work_root

    def run(self) -> None:
        """Execute the install call (called from worker thread).

        Must be connected to the thread's ``started`` signal.
        """
        pid = self._product_id
        logger.info("Worker: starting install for %r", pid)
        try:
            result = self._service.install_product(
                pid,
                resolver=self._resolver,
                fetcher=self._fetcher,
                work_root=self._work_root,
                progress_callback=self._on_progress,
            )
            if result.success:
                logger.info("Worker: install succeeded for %r", pid)
                self.install_succeeded.emit(pid)
            else:
                reason = result.reason or "unknown error"
                logger.warning("Worker: install failed for %r: %s", pid, reason)
                self.install_failed.emit(pid, reason)
        except Exception as exc:
            msg = str(exc)
            if len(msg) > 200:
                msg = msg[:197] + "\u2026"
            logger.error("Worker: install exception for %r: %s", pid, exc)
            self.install_failed.emit(pid, msg)
        finally:
            self.finished.emit()

    def _on_progress(self, progress) -> None:
        """Relay a backend progress observation to the GUI thread."""
        self.progress.emit(progress)


# ---------------------------------------------------------------------------
# Thread / worker lifecycle helper
# ---------------------------------------------------------------------------


def create_install_thread(
    pid: str,
    service: ZeAlfieService,
    *,
    resolver: SourceRefResolver,
    fetcher: ArchiveFetcher,
    work_root: Path,
    parent: QObject | None = None,
) -> tuple[QThread, InstallWorker]:
    """Create a worker, a thread, and wire them for one install.

    Returns (thread, worker) after moving the worker to the thread and
    connecting ``thread.started`` → ``worker.run``.

    The worker MUST NOT have a parent so ``moveToThread`` succeeds.
    The thread MAY have a parent for memory management.

    The caller is responsible for:

    1. Connecting worker signals to GUI slots.
    2. Calling ``thread.start()``.
    3. Connecting ``thread.finished`` to release install lock
       and schedule ``thread.deleteLater()`` (e.g. on the main
       event loop).
    """
    thread = QThread(parent)
    # Worker must be parentless so it can be moved to the worker thread.
    worker = InstallWorker(
        pid, service,
        resolver=resolver, fetcher=fetcher, work_root=work_root,
        parent=None,
    )
    worker.moveToThread(thread)

    # Robust lifecycle: schedule worker deletion while its thread event
    # loop is still alive, then quit the thread after worker is destroyed.
    worker.finished.connect(worker.deleteLater)
    worker.destroyed.connect(thread.quit)

    # started → run (do the work)
    thread.started.connect(worker.run)
    return thread, worker
