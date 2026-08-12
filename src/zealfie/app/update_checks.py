"""Non-blocking, in-memory update-check coordinator (M1-2E LOT E.3).

E.2 gave us a pure, read-only :func:`~zealfie.app.updates.check_product_update`
core.  E.3 adds the state container and the non-blocking *mechanism* that a
startup check / GUI shell needs, without any Qt and without any real network
or filesystem mutation.

Design
------

* :class:`UpdateCheckCoordinator` owns an in-memory mapping of
  ``product_id → ProductUpdateResult``.  Every product starts at
  :attr:`UpdateStatus.NOT_CHECKED`, transitions to
  :attr:`UpdateStatus.CHECKING` when a check begins, and reaches a terminal
  status (``UP_TO_DATE`` / ``UPDATE_AVAILABLE`` / ``CHECK_FAILED`` /
  ``PROVENANCE_UNKNOWN``) when the underlying check finishes.

* The check itself is a single injected callable ``check_fn(product_id) →
  ProductUpdateResult``.  In production this is
  ``functools.partial(service.check_product_update, resolver=…)``; in tests
  it is a pure fake.  The coordinator never constructs a resolver, never
  touches GitHub, and never reads/writes runtime, provenance, selection or
  the ``active.json`` pointer.  It only *calls* the injected callable.

* :meth:`UpdateCheckCoordinator.check_one` runs a check synchronously (still
  read-only) and returns the terminal result.

* :meth:`UpdateCheckCoordinator.start` runs checks in the background via an
  injectable :class:`~concurrent.futures.Executor` (default: a lazily created
  :class:`~concurrent.futures.ThreadPoolExecutor`) and returns the pending
  :class:`~concurrent.futures.Future` objects immediately — it never blocks
  on network or resolution.

* Observers registered via :meth:`UpdateCheckCoordinator.add_observer` are
  notified, on the completing thread, with every state change (``CHECKING``
  then the terminal result).  Observer exceptions are logged and swallowed so
  observation can never break a check.

Out-of-order safety
-------------------

Each *start* of a check for a product increments a per-product generation
counter.  When a check completes it only commits its result (and notifies)
if its generation is still the latest for that product.  A stale run that
finishes after a newer run for the same product is therefore discarded and
cannot overwrite the newer result.

Thread-safety
-------------

All shared state (results, generations, observer list) is guarded by a
single reentrant lock.  Observers are invoked *outside* the lock to avoid
deadlocks and to allow observers to safely re-enter the coordinator.

This module is pure Python and Qt-free.  E.4 will wire the GUI to it; it
intentionally owns no presentation and performs no persistence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from threading import RLock

from zealfie.app.updates import ProductUpdateResult, UpdateStatus

logger = logging.getLogger(__name__)

# A single product check, already bound to its resolver/network in production.
CheckFunction = Callable[[str], ProductUpdateResult]
# A state-change observer: called with each CHECKING/terminal result.
Observer = Callable[[ProductUpdateResult], None]


def _not_checked(product_id: str) -> ProductUpdateResult:
    """Return the initial ``NOT_CHECKED`` state for *product_id*."""
    return ProductUpdateResult.not_checked(product_id)


def _checking(product_id: str) -> ProductUpdateResult:
    """Return the transient ``CHECKING`` state for *product_id*."""
    return ProductUpdateResult(product_id=product_id, status=UpdateStatus.CHECKING)


def _error_message(exc: Exception) -> str:
    """Return a stable, human-readable reason from an exception."""
    message = str(exc).strip()
    return message or type(exc).__name__


class UpdateCheckCoordinator:
    """Thread-safe, in-memory owner of per-product update-check state.

    Holds the current :class:`ProductUpdateResult` for each product that has
    been checked (or queried), exposes it via :meth:`state` / :meth:`states`,
    and runs checks either synchronously (:meth:`check_one`) or non-blocking
    (:meth:`start`).

    The coordinator is deliberately product-agnostic: it does not know the
    catalog and never resolves sources.  It only calls the injected
    *check_fn* and records the result.  This keeps it fully testable without
    Qt and without real GitHub.
    """

    def __init__(
        self,
        check_fn: CheckFunction,
        *,
        executor: Executor | None = None,
    ) -> None:
        if check_fn is None:
            raise ValueError("check_fn is required")
        self._check_fn = check_fn
        self._executor = executor
        self._owns_executor = executor is None
        self._lock = RLock()
        self._states: dict[str, ProductUpdateResult] = {}
        self._generations: dict[str, int] = {}
        self._observers: list[Observer] = []

    # ------------------------------------------------------------------
    # State read access
    # ------------------------------------------------------------------

    def state(self, product_id: str) -> ProductUpdateResult:
        """Return the current state for *product_id*.

        Products that have never been checked return
        :attr:`UpdateStatus.NOT_CHECKED` (never ``None``).
        """
        with self._lock:
            result = self._states.get(product_id)
        return result if result is not None else _not_checked(product_id)

    def status(self, product_id: str) -> UpdateStatus:
        """Return the current :class:`UpdateStatus` for *product_id*."""
        return self.state(product_id).status

    def states(self) -> Mapping[str, ProductUpdateResult]:
        """Return an immutable snapshot of all known product states."""
        with self._lock:
            return dict(self._states)

    # ------------------------------------------------------------------
    # Observers
    # ------------------------------------------------------------------

    def add_observer(self, observer: Observer) -> None:
        """Register *observer* to be notified of every state change.

        Duplicate registrations are ignored.  Observers are invoked with
        each ``CHECKING`` transition and each terminal result, on the thread
        that produced the change (the caller thread for :meth:`check_one`,
        a pool thread for :meth:`start`).
        """
        with self._lock:
            if observer not in self._observers:
                self._observers.append(observer)

    def remove_observer(self, observer: Observer) -> None:
        """Unregister *observer*; unknown observers are ignored."""
        with self._lock:
            try:
                self._observers.remove(observer)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Check entry points
    # ------------------------------------------------------------------

    def check_one(self, product_id: str) -> ProductUpdateResult:
        """Check *product_id* synchronously and record the result.

        Transitions the product to :attr:`UpdateStatus.CHECKING`, runs the
        injected *check_fn*, then records the terminal result and notifies
        observers.  Returns the terminal result.

        Read-only: it only calls *check_fn* and mutates in-memory state.
        """
        generation = self._begin(product_id)
        result = self._invoke_check(product_id)
        return self._finish(product_id, generation, result)

    def start(
        self,
        product_ids: Iterable[str],
    ) -> tuple[Future[ProductUpdateResult], ...]:
        """Start non-blocking checks for *product_ids*.

        Each product is transitioned to :attr:`UpdateStatus.CHECKING`
        synchronously (before this method returns), then the actual check is
        submitted to the executor.  Returns the pending futures immediately —
        this method never waits for the resolver/network.

        The returned futures resolve to the terminal result even when a run
        is superseded by a newer run for the same product (see the module
        docstring for out-of-order semantics); in that case the stale result
        is simply not committed to state and not broadcast to observers.
        """
        futures: list[Future[ProductUpdateResult]] = []
        for product_id in product_ids:
            generation = self._begin(product_id)
            futures.append(
                self._pool().submit(self._run_and_finish, product_id, generation)
            )
        return tuple(futures)

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the default executor, if one was created.

        A caller-injected executor is never shut down (the caller owns its
        lifecycle).  Safe to call multiple times.  A later :meth:`start`
        after shutting down the owned default executor creates a fresh pool
        instead of mutating state and then failing on a closed executor.
        """
        with self._lock:
            executor = self._executor
            owns = self._owns_executor
        if executor is not None and owns:
            executor.shutdown(wait=wait)
            with self._lock:
                if self._executor is executor:
                    self._executor = None

    def __enter__(self) -> "UpdateCheckCoordinator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pool(self) -> Executor:
        """Return the executor, creating a lazily-owned default if needed."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    thread_name_prefix="zealfie-update-check"
                )
            return self._executor

    def _begin(self, product_id: str) -> int:
        """Mark *product_id* CHECKING, bump its generation, and notify.

        Returns the new generation.  Safe under concurrency: a later
        ``_begin`` invalidates any in-flight older run for the same product.
        """
        with self._lock:
            generation = self._generations.get(product_id, 0) + 1
            self._generations[product_id] = generation
            self._states[product_id] = _checking(product_id)
        self._notify(_checking(product_id))
        return generation

    def _finish(
        self,
        product_id: str,
        generation: int,
        result: ProductUpdateResult,
    ) -> ProductUpdateResult:
        """Commit *result* for *product_id* iff its generation is current.

        Returns *result* either way.  Stale runs are discarded (not stored,
        not broadcast).
        """
        with self._lock:
            is_current = self._generations.get(product_id, 0) == generation
            if is_current:
                self._states[product_id] = result
        if is_current:
            self._notify(result)
        return result

    def _invoke_check(self, product_id: str) -> ProductUpdateResult:
        """Call the injected check function, never letting it escape.

        A misbehaving *check_fn* (unexpected exception) is turned into a
        ``CHECK_FAILED`` result so a product never gets stuck in
        ``CHECKING``.
        """
        try:
            return self._check_fn(product_id)
        except Exception as exc:  # noqa: BLE001 - check boundary: never crash
            logger.debug(
                "update check for %r raised unexpectedly; reporting CHECK_FAILED",
                product_id,
                exc_info=True,
            )
            return ProductUpdateResult(
                product_id=product_id,
                status=UpdateStatus.CHECK_FAILED,
                error=_error_message(exc),
            )

    def _run_and_finish(
        self,
        product_id: str,
        generation: int,
    ) -> ProductUpdateResult:
        """Pool-worker entry point: check, then commit if still current."""
        result = self._invoke_check(product_id)
        return self._finish(product_id, generation, result)

    def _notify(self, result: ProductUpdateResult) -> None:
        """Broadcast *result* to observers, outside the lock.

        Observer exceptions are logged and swallowed — observation is
        observational only and must never break a check.
        """
        with self._lock:
            observers = tuple(self._observers)
        for observer in observers:
            try:
                observer(result)
            except Exception:  # noqa: BLE001 - observers are observational
                logger.debug(
                    "update-check observer raised; ignoring", exc_info=True
                )
