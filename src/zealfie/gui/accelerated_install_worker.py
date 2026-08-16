"""M1-2I / I3 — Qt worker for the transactional accelerated runtime install.

Mirrors :mod:`zealfie.gui.install_worker` conventions: one synchronous
``service.install_accelerated_runtime(...)`` call running on a QThread;
the GUI thread is never blocked.  The worker computes NO business
progress — the user-facing ``(label, percent)`` pairs come from the pure
reducer :func:`~zealfie.gui.presentation.accelerated_install_view` fed
with the backend's ``InstallProgress`` observations and the final
result.

Cooperative cancellation: :meth:`AcceleratedInstallWorker.cancel` sets a
``threading.Event`` (thread-safe); the ``cancel_check`` handed to the
service raises :class:`~zealfie.acceleration.deployment.CooperativeCancellationError`
once the event is set, and the service returns a ``cancelled=True``
result at its next deterministic checkpoint.  The worker never kills the
thread and never interrupts the service call directly.
"""

from __future__ import annotations

import logging

import threading

from PySide6.QtCore import QObject, QThread, Signal

from zealfie.acceleration import (
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentResult,
    CooperativeCancellationError,
)
from zealfie.app import InstallPhase
from zealfie.runtime import RuntimeMutationBusyError, RuntimeMutationLockError
from zealfie.gui.presentation import accelerated_install_view

logger = logging.getLogger(__name__)

#: Shared ``InstallPhase`` → accelerated phase, used only to synthesize an
#: honest phase for the defensive result when the service raises (the
#: service contract is to return results, never raise).
_INSTALL_TO_ACCELERATED_PHASE: dict[InstallPhase, AcceleratedDeploymentPhase] = {
    InstallPhase.PREPARING: AcceleratedDeploymentPhase.PREPARE,
    InstallPhase.RESOLVING_SOURCE: AcceleratedDeploymentPhase.PREPARE,
    InstallPhase.DOWNLOADING_SOURCE: AcceleratedDeploymentPhase.ACQUIRE,
    InstallPhase.BUILDING_PRODUCT: AcceleratedDeploymentPhase.BUILD,
    InstallPhase.ACQUIRING_DEPENDENCIES: AcceleratedDeploymentPhase.ACQUIRE,
    InstallPhase.PLANNING_RUNTIME: AcceleratedDeploymentPhase.RESOLVE,
    InstallPhase.INSTALLING_RUNTIME: AcceleratedDeploymentPhase.BUILD,
    InstallPhase.VALIDATING: AcceleratedDeploymentPhase.VALIDATE,
    InstallPhase.ACTIVATING: AcceleratedDeploymentPhase.ACTIVATE,
    InstallPhase.COMPLETED: AcceleratedDeploymentPhase.COMPLETED,
}


class AcceleratedInstallWorker(QObject):
    """QObject that runs one accelerated runtime deployment in a worker thread.

    The caller MUST move this object to the worker thread before calling
    ``run()``.  Do NOT call ``run()`` from the GUI thread.

    Emits:

    * ``progress(str, object)`` — one user-facing pair per changed view:
      a phase label plus an honest ``percent`` (int) or ``None``;
      consecutive identical pairs are collapsed.  The final pair reflects
      the result (success → ``("Completed", 100)``; failure/cancellation →
      the label of the phase where the deployment stopped, percent
      ``None`` — never fake progress);
    * ``finished(object)`` — exactly once, with the
      :class:`~zealfie.acceleration.deployment.AcceleratedDeploymentResult`.
    """

    #: Emitted (from worker thread) for each changed reduced view.
    progress = Signal(str, object)  # phase_label, percent (int | None)

    #: Emitted (from worker thread) exactly once with the result.
    finished = Signal(object)  # AcceleratedDeploymentResult

    def __init__(
        self,
        service,
        *,
        plan=None,
        recommendation=None,
        capabilities=None,
        fetcher=None,
        work_root=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._plan = plan
        self._recommendation = recommendation
        self._capabilities = capabilities
        self._fetcher = fetcher
        self._work_root = work_root
        self._cancel_event = threading.Event()
        self._events: list[object] = []
        self._last_pair: tuple[str, int | None] | None = None
        self._last_phase: AcceleratedDeploymentPhase = (
            AcceleratedDeploymentPhase.PREPARE
        )

    # ------------------------------------------------------------------
    # Cooperative cancellation
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request cooperative cancellation (thread-safe, non-blocking).

        Does not interrupt the running service call; the service observes
        the request through ``cancel_check`` and returns a
        ``cancelled=True`` result at its next deterministic checkpoint.
        """
        self._cancel_event.set()

    def _cancel_check(self) -> None:
        if self._cancel_event.is_set():
            raise CooperativeCancellationError(
                "accelerated deployment cancelled by user"
            )

    # ------------------------------------------------------------------
    # Run (worker thread only)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the synchronous service call (called from worker thread).

        Must be connected to the thread's ``started`` signal.
        """
        install = getattr(self._service, "install_accelerated_runtime", None)
        if not callable(install):
            result = AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.PREPARE,
                reason=(
                    "accelerated runtime installation is not available "
                    "in this version"
                ),
            )
            self._emit_final_view(result)
            self.finished.emit(result)
            return
        try:
            result = install(
                plan=self._plan,
                recommendation=self._recommendation,
                capabilities=self._capabilities,
                cancel_check=self._cancel_check,
                progress_callback=self._on_progress,
                fetcher=self._fetcher,
                work_root=self._work_root,
            )
        except RuntimeMutationBusyError:
            # ZA-M1-2L (D6): another ZeAlfie writer owns the runtime mutation
            # lease — clean user message, no crash, worker finishes normally
            # (no second silent mutation is attempted).
            logger.warning(
                "AcceleratedInstallWorker: runtime mutation BUSY"
            )
            result = AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=self._last_phase,
                reason="another ZeAlfie runtime operation is in progress",
            )
        except RuntimeMutationLockError as exc:
            logger.error(
                "AcceleratedInstallWorker: runtime mutation lock "
                "unavailable: %s", exc,
            )
            result = AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=self._last_phase,
                reason=(
                    "the ZeAlfie runtime mutation lock is unavailable: "
                    f"{exc}"
                )[:200],
            )
        except Exception as exc:
            # Defensive: the service contract returns results, never raises.
            message = str(exc)
            if len(message) > 200:
                message = message[:197] + "\u2026"
            logger.error("AcceleratedInstallWorker: exception: %s", exc)
            result = AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=self._last_phase,
                reason=f"accelerated installation failed: {message}",
            )
        self._emit_final_view(result)
        self.finished.emit(result)

    def _on_progress(self, progress) -> None:
        """Relay one backend observation as a reduced (label, percent) pair."""
        phase = getattr(progress, "phase", None)
        if isinstance(phase, InstallPhase):
            self._last_phase = _INSTALL_TO_ACCELERATED_PHASE.get(
                phase, AcceleratedDeploymentPhase.PREPARE
            )
        self._events.append(progress)
        self._emit_view()

    # ------------------------------------------------------------------
    # View emission
    # ------------------------------------------------------------------

    def _emit_view(self) -> None:
        """Emit the current reduced view when it changed."""
        label, percent, _done = accelerated_install_view(self._events)
        pair: tuple[str, int | None] = (label, percent)
        if pair == self._last_pair:
            return
        self._last_pair = pair
        self.progress.emit(label, percent)

    def _emit_final_view(self, result) -> None:
        """Fold the terminal result into the view and emit it when changed."""
        self._events.append(result)
        self._emit_view()


# ---------------------------------------------------------------------------
# Thread / worker lifecycle helper
# ---------------------------------------------------------------------------


def create_accelerated_install_thread(
    service,
    *,
    plan=None,
    recommendation=None,
    capabilities=None,
    fetcher=None,
    work_root=None,
    parent: QObject | None = None,
) -> tuple[QThread, AcceleratedInstallWorker]:
    """Create a worker, a thread, and wire them for one accelerated install.

    Returns ``(thread, worker)`` after moving the worker to the thread and
    connecting ``thread.started`` → ``worker.run``.

    The worker MUST NOT have a parent so ``moveToThread`` succeeds.  The
    thread MAY have a parent for memory management.  The caller is
    responsible for connecting worker signals to GUI slots, calling
    ``thread.start()``, and connecting ``thread.finished`` to cleanup
    (wait, ``thread.deleteLater()``, and releasing references) on the
    main event loop — mirroring the main window's install pattern.
    """
    thread = QThread(parent)
    # Worker must be parentless so it can be moved to the worker thread.
    worker = AcceleratedInstallWorker(
        service,
        plan=plan,
        recommendation=recommendation,
        capabilities=capabilities,
        fetcher=fetcher,
        work_root=work_root,
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
