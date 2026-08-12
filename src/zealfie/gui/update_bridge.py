"""M1-2E LOT E.4 — Qt bridge to marshal update-check results onto the GUI thread.

:class:`~zealfie.app.update_checks.UpdateCheckCoordinator` invokes its
observers on the completing thread (a ``ThreadPoolExecutor`` worker for
``start()``).  Qt widgets must never be mutated from a worker thread, so
this small :class:`QObject` acts as a signal bridge: the coordinator's
observer calls :meth:`UpdateResultBridge.notify`, which emits
:attr:`update_result_ready`.  Because the bridge lives on the GUI thread,
Qt delivers that signal to GUI-thread slots via a queued connection.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class UpdateResultBridge(QObject):
    """Re-emit coordinator results onto the GUI thread.

    The object is created with the main window as parent, so it lives on
    the GUI thread.  ``notify`` is the observer callable handed to the
    coordinator; it may be called from any thread.
    """

    #: Emitted with a :class:`~zealfie.app.ProductUpdateResult`.  Slots
    #: connected on the GUI thread are invoked via a queued connection.
    update_result_ready = Signal(object)

    def notify(self, result) -> None:
        """Observer entry point (thread-agnostic)."""
        self.update_result_ready.emit(result)
