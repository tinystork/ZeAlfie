"""Inter-process mutation lock for the ZeAlfie shared runtime (ZA-M1-2L).

Invariant: FOR ONE RUNTIME ROOT, at most ONE mutating ZeAlfie operation
may own the runtime at a time.  The lease is acquired with
:meth:`RuntimeMutationLock.acquire` at the outermost layer that owns a
complete logical mutation (D1) and covers the whole mutation window,
including downloads and builds.  Read-only commands never acquire it.

Backend (D4).  The lock is an exclusive, non-blocking OS lock on a
sibling lock file derived from the *resolved* runtime root (D5)::

    <root>.parent / f".{root.name}.zealfie-mutation.lock"

(if ``root.name`` is empty, the name is
``".zealfie-" + sha256(str(root))[:16] + ".mutation-lock"``).

* **POSIX** (Linux / macOS): ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the
  lock file descriptor.
* **Windows** (``os.name == "nt"``): ``msvcrt.locking(fd, LK_NBLCK, 1)``
  — a non-blocking exclusive byte-range lock on byte range ``[0, 1)`` of
  the lock file.  The lock file is padded to at least one byte on first
  creation so the range legally exists; its contents are never
  authority.  Windows locks are process-owned by the OS: they are
  released automatically on process death, and a second handle in the
  same process conflicts natively (a different thread → BUSY without
  extra local state).
* **any other platform**: :meth:`RuntimeMutationLock.acquire` raises
  :class:`RuntimeMutationLockError` *before* touching the filesystem: no
  lock file, no directory — and, critically, no mutation is ever allowed
  without a lease (fail closed).

``fcntl`` / ``msvcrt`` are imported *inside* the backend functions,
never at module level, so this module imports cleanly on every platform.
Only the parent directory of the lock file is created; the runtime root
itself is never created by locking.

The lock file is only a rendezvous point, never the authority:
**FILE EXISTS != LOCK HELD**.  A stale lock file (left by a crash or by
manual creation) never blocks acquisition; only a live OS lock does.
The OS releases the lock automatically when the holding process dies or
the file descriptor is closed, so crash recovery needs no manual
cleanup.  The lock file is never deleted by this module.

Diagnostics: each acquisition atomically overwrites
``<lock_path>.owner.json`` (mkstemp + fsync + ``os.replace``,
best-effort ``try/except`` — a failed sidecar write never fails the
acquisition) with exactly ``{operation, pid, acquired_at, invocation_id}``.
The sidecar is diagnostics only, never authority: stale, corrupt or
missing sidecars are tolerated and never cause BUSY.

Reentrance (D3): held leases are tracked in a ``ContextVar`` stack per
thread/task.  A nested :meth:`~RuntimeMutationLock.acquire` for the same
resolved root in the same context reuses the existing lease (refcount
incremented, no new OS lock, no deadlock).  A different thread gets a
fresh context and therefore a real non-blocking OS lock: if the lease is
held it fails fast with :class:`RuntimeMutationBusyError` — a different
thread of the same process is a *new writer*, never reentrant.
Subprocesses never inherit the lock fd (PEP 446, close-on-exec by
default), so children (pip, probes) never carry the lock.

BUSY (D6): acquisition is strictly non-blocking and fail-fast, with zero
retry.  A held lock raises :class:`RuntimeMutationBusyError` whose
message states "Runtime is busy with another ZeAlfie mutation. No
changes have been applied." followed by owner diagnostics when the
sidecar is readable.  A primitive failure (permissions, OS error,
unavailable backend) raises :class:`RuntimeMutationLockError` — fail
closed: never mutate without a lock.

Advisory nature: the lock serializes *cooperating* ZeAlfie writers that
acquire it.  Writers that do not use this API (external tools, manual
edits) are outside its control.  The lock does not replace stale-state
validation (stale transaction checks, the GC state fingerprint) or
atomic persistence — it complements them.

See ``docs/mutation-lock.md`` for the full contract (ZA-M1-2L).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import tempfile
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# -- operation names (D7) ----------------------------------------------------

OPERATION_RUNTIME_CREATE = "runtime-create"
OPERATION_RUNTIME_APPLY = "runtime-apply"
OPERATION_RUNTIME_ROLLBACK = "runtime-rollback"
OPERATION_RUNTIME_DISCARD = "runtime-discard"
OPERATION_RUNTIME_GC = "runtime-gc"
OPERATION_PRODUCT_INSTALL = "product-install"
OPERATION_PRODUCT_UPDATE = "product-update"
OPERATION_GPU_INSTALL = "gpu-install"

_LOCK_FILE_SUFFIX = ".zealfie-mutation.lock"
_OWNER_SUFFIX = ".owner.json"
_OWNER_FIELDS = ("operation", "pid", "acquired_at", "invocation_id")

# Per-thread/per-task stack of held leases (D3).  A new thread or task
# starts with a virgin (empty) context: reentrance is never implicit.
_lease_stack: ContextVar[tuple["RuntimeMutationLease", ...]] = ContextVar(
    "zealfie_runtime_mutation_lease_stack", default=()
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeMutationLockError(Exception):
    """The lock primitive itself failed.

    Fail closed: a primitive failure (unsupported platform, unavailable
    backend primitive — ``fcntl`` or ``msvcrt`` —, permissions, OS error)
    must never allow a mutation to proceed without a lease.
    """


class RuntimeMutationBusyError(Exception):
    """Another ZeAlfie writer owns the runtime root's mutation lease.

    Raised by the strictly non-blocking :meth:`RuntimeMutationLock.acquire`
    (D6).  The ``operation`` / ``pid`` / ``acquired_at`` / ``invocation_id``
    attributes are diagnostics read from the owner sidecar (best-effort,
    each may be ``None``); they are never authority.
    """

    def __init__(
        self,
        *,
        lock_path: Path,
        operation: str | None = None,
        pid: int | None = None,
        acquired_at: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.operation = operation
        self.pid = pid
        self.acquired_at = acquired_at
        self.invocation_id = invocation_id
        message = (
            "Runtime is busy with another ZeAlfie mutation. "
            "No changes have been applied."
        )
        details: list[str] = []
        if operation:
            details.append(f"operation={operation}")
        if pid is not None:
            details.append(f"pid={pid}")
        if acquired_at:
            details.append(f"acquired_at={acquired_at}")
        if invocation_id:
            details.append(f"invocation_id={invocation_id}")
        if details:
            message += " (" + ", ".join(details) + ")"
        super().__init__(message)


class RuntimeMutationLeaseRequired(Exception):
    """A low-level mutating primitive was called without a lease.

    Raised by :meth:`RuntimeMutationLock.require_lease` when no lease is
    held in the current execution context (D2 contract, fail closed).
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what} requires an active runtime mutation lease "
            "(acquire one with RuntimeMutationLock.acquire) in the "
            "current execution context; none is held"
        )


class RuntimeMutationLease:
    """Token proving ownership of the runtime root's mutation lease.

    Obtained from :meth:`RuntimeMutationLock.acquire`.  Supports the
    context-manager protocol; ``release()`` is idempotent and safe to
    call any number of times (extra calls are no-ops).
    """

    def __init__(
        self,
        *,
        runtime_root: Path,
        operation: str,
        pid: int,
        acquired_at: str,
        invocation_id: str,
        fd: int,
        lock_path: Path,
    ) -> None:
        self.runtime_root = runtime_root
        self.operation = operation
        self.pid = pid
        self.acquired_at = acquired_at
        self.invocation_id = invocation_id
        self._fd = fd
        self._lock_path = lock_path
        self._refcount = 1
        self._held = True

    @property
    def lock_path(self) -> Path:
        """Path of the flock support file backing this lease."""
        return self._lock_path

    def release(self) -> None:
        """Release one reference; the last one unlocks and closes the fd.

        Idempotent: releasing an already-released lease is a no-op.
        """
        if not self._held:
            return
        if self._refcount > 1:
            self._refcount -= 1
            return
        self._held = False
        self._refcount = 0
        stack = _lease_stack.get()
        remaining = tuple(item for item in stack if item is not self)
        if remaining != stack:
            _lease_stack.set(remaining)
        _release_fd(self._fd)
        self._fd = None

    def __enter__(self) -> "RuntimeMutationLease":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.release()
        return False


# -- platform dispatch / backends (D4) ----------------------------------------


_os_name_override: str | None = None


def _effective_os_name() -> str:
    """``os.name``, overridable for tests (never set in production code)."""
    return _os_name_override if _os_name_override is not None else os.name


def _backend_kind() -> str:
    """Dispatch key: "posix" (fcntl) | "windows" (msvcrt) | "unsupported"."""
    name = _effective_os_name()
    if name == "posix":
        return "posix"
    if name == "nt":
        return "windows"
    return "unsupported"


def _check_platform() -> None:
    """Gate the backend (D4) — fails closed before any disk write.

    ``fcntl`` / ``msvcrt`` are imported here (not at module level) so the
    module itself imports cleanly on every platform.
    """
    kind = _backend_kind()
    if kind == "posix":
        try:
            import fcntl  # noqa: F401
        except ImportError as exc:
            raise RuntimeMutationLockError(
                "fcntl is unavailable on this platform; refusing to mutate "
                "without a lock"
            ) from exc
    elif kind == "windows":
        try:
            import msvcrt  # noqa: F401
        except ImportError as exc:
            raise RuntimeMutationLockError(
                "msvcrt is unavailable on this platform; refusing to mutate "
                "without a lock"
            ) from exc
    else:
        raise RuntimeMutationLockError(
            "ZeAlfie runtime mutation lock has no backend for "
            f"os.name={_effective_os_name()!r}; refusing to mutate "
            "without a lock"
        )


def _open_lock_fd(lock_path: Path) -> int:
    """Open (creating if needed) the lock file with 0o600 permissions."""
    try:
        return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise RuntimeMutationLockError(
            f"cannot open mutation lock file {lock_path}: {exc}"
        ) from exc


def _ensure_lock_range_backing(fd: int) -> None:
    """Pad the lock file to >=1 byte so the Windows byte range [0, 1) exists.

    ``msvcrt.locking`` requires the locked region to exist in the file; a
    freshly created lock file is empty.  Write one zero byte at offset 0
    only when the file is empty — deterministic backing, the contents are
    never authority (FILE EXISTS != LOCK HELD), and the file is never
    truncated.  Only the Windows path calls this.
    """
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\x00")
    except OSError as exc:
        raise RuntimeMutationLockError(
            f"cannot prepare mutation lock file backing: {exc}"
        ) from exc


def _try_lock_posix(fd: int) -> bool:
    """Non-blocking exclusive flock.  Returns False when the lock is held."""
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise RuntimeMutationLockError(f"fcntl.flock failed: {exc}") from exc
    return True


def _try_lock_windows(fd: int) -> bool:
    """Non-blocking exclusive Windows byte-range lock.  False when held.

    ``msvcrt.locking(fd, LK_NBLCK, 1)`` locks byte range [0, 1).  The OS
    owns the lock: it is released on process death, and a second handle
    in the same process conflicts natively (different thread -> BUSY
    without extra local state).  Error classification is strict (B5,
    fail closed): only the documented contention signatures map to BUSY;
    every other OSError is a primitive failure.
    """
    import msvcrt

    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise RuntimeMutationLockError(
            f"cannot seek mutation lock file: {exc}"
        ) from exc
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        # POSIX-built OSErrors carry no ``winerror`` attribute at all;
        # getattr keeps the strict classification uniform (B5).
        winerror = getattr(exc, "winerror", None)
        if winerror == 33:
            # ERROR_LOCK_VIOLATION — the region is held elsewhere.
            return False
        if winerror is None and exc.errno in (
            errno.EACCES,
            errno.EDEADLK,
            errno.EAGAIN,
        ):
            # msvcrt contention errno mapping.
            return False
        raise RuntimeMutationLockError(f"msvcrt.locking failed: {exc}") from exc
    return True


def _try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock via the active backend.  False = held.

    :func:`_check_platform` gates unsupported platforms before any fd
    exists, so the kind here is "posix" or "windows".
    """
    if _backend_kind() == "windows":
        return _try_lock_windows(fd)
    return _try_lock_posix(fd)


def _close_fd_best_effort(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _release_fd(fd: int) -> None:
    """Release via the active backend, then close — best-effort (crash-safe).

    POSIX: ``flock(LOCK_UN)`` then close.  Windows: ``lseek(0)`` +
    ``msvcrt.locking(fd, LK_UNLCK, 1)`` then close.  Unsupported: close
    only.  The fd is always closed, even when the unlock fails.
    """
    kind = _backend_kind()
    if kind == "windows":
        try:
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                logger.debug(
                    "mutation lock LK_UNLCK failed (fd closed anyway): %s", exc
                )
        finally:
            _close_fd_best_effort(fd)
    elif kind == "posix":
        try:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                logger.debug(
                    "mutation lock LOCK_UN failed (fd closed anyway): %s", exc
                )
        finally:
            _close_fd_best_effort(fd)
    else:
        _close_fd_best_effort(fd)


# -- owner sidecar (diagnostics only, never authority) ------------------------


def _owner_sidecar_path(lock_path: Path) -> Path:
    return Path(str(lock_path) + _OWNER_SUFFIX)


def _write_owner_sidecar(
    lock_path: Path,
    *,
    operation: str,
    pid: int,
    acquired_at: str,
    invocation_id: str,
) -> None:
    """Atomically overwrite the owner sidecar.  Best-effort: never raises.

    Content is exactly ``{operation, pid, acquired_at, invocation_id}`` —
    diagnostics only.  A stale sidecar is never read as authority.
    """
    sidecar = _owner_sidecar_path(lock_path)
    payload = {
        "operation": operation,
        "pid": pid,
        "acquired_at": acquired_at,
        "invocation_id": invocation_id,
    }
    tmp_path: str | None = None
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(sidecar.parent), prefix=sidecar.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, sidecar)
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except OSError as exc:
        logger.debug("mutation lock owner sidecar write failed (best-effort): %s", exc)


def _read_owner_info(lock_path: Path) -> dict[str, Any]:
    """Best-effort read of the owner sidecar.  Never raises.

    Unreadable, corrupt, or malformed sidecars yield ``None`` fields.
    """
    info: dict[str, Any] = {
        "operation": None,
        "pid": None,
        "acquired_at": None,
        "invocation_id": None,
    }
    try:
        text = _owner_sidecar_path(lock_path).read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, ValueError):
        return info
    if not isinstance(payload, dict):
        return info
    for key in _OWNER_FIELDS:
        value = payload.get(key)
        if isinstance(value, (str, int)):
            info[key] = value
    return info


# -- public lock object -------------------------------------------------------


class RuntimeMutationLock:
    """Per-runtime-root inter-process mutation lock (fcntl.flock on POSIX,
    msvcrt.locking on Windows).

    See the module docstring for the full contract (D3-D7):
    fail-fast BUSY, reentrance by ContextVar stack, crash-safe primitive,
    advisory scope, FILE EXISTS != LOCK HELD.
    """

    def __init__(self, runtime_root: Path | str) -> None:
        # Resolve before deriving the lock path (D5): equivalent spellings
        # of the same root (symlinks, trailing slash, relative vs absolute)
        # map to the same resolved root and therefore the same lock file.
        self.runtime_root = Path(runtime_root).expanduser().resolve(strict=False)
        if _effective_os_name() == "nt":
            # Windows filesystems are case-insensitive (D5): the same root
            # spelled differently must map to the same lock file.
            self.runtime_root = Path(os.path.normcase(str(self.runtime_root)))

    def lock_path(self) -> Path:
        """Derived sibling lock-file path for this runtime root (D5).

        Pure computation — never touches the filesystem.
        """
        root = self.runtime_root
        parent = root.parent
        if root.name:
            return parent / f".{root.name}{_LOCK_FILE_SUFFIX}"
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        return parent / f".zealfie-{digest}.mutation-lock"

    def acquire(self, operation: str) -> RuntimeMutationLease:
        """Acquire the mutation lease for this runtime root (non-blocking).

        *operation* names the logical mutation (see the ``OPERATION_*``
        constants).  Raises :class:`RuntimeMutationBusyError` when another
        writer holds the lease (D6, fail-fast) and
        :class:`RuntimeMutationLockError` when the primitive itself fails
        (fail closed).  Nested acquisition for the same resolved root in
        the same thread/task reuses the existing lease (D3, refcount).
        """
        if not isinstance(operation, str) or not operation:
            raise RuntimeMutationLockError("operation must be a non-empty string")
        lock_path = self.lock_path()
        stack = _lease_stack.get()
        for lease in stack:
            if lease._lock_path == lock_path:
                # Reentrant reuse (D3): same resolved root, same context.
                lease._refcount += 1
                _write_owner_sidecar(
                    lock_path,
                    operation=lease.operation,
                    pid=lease.pid,
                    acquired_at=lease.acquired_at,
                    invocation_id=lease.invocation_id,
                )
                return lease
        _check_platform()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeMutationLockError(
                f"cannot create mutation lock directory {lock_path.parent}: {exc}"
            ) from exc
        fd = _open_lock_fd(lock_path)
        try:
            if _backend_kind() == "windows":
                _ensure_lock_range_backing(fd)
            acquired = _try_lock(fd)
        except BaseException:
            _close_fd_best_effort(fd)
            raise
        if not acquired:
            _close_fd_best_effort(fd)
            info = _read_owner_info(lock_path)
            raise RuntimeMutationBusyError(
                lock_path=lock_path,
                operation=info["operation"],
                pid=info["pid"],
                acquired_at=info["acquired_at"],
                invocation_id=info["invocation_id"],
            )
        lease = RuntimeMutationLease(
            runtime_root=self.runtime_root,
            operation=operation,
            pid=os.getpid(),
            acquired_at=_utc_now_iso(),
            invocation_id=uuid.uuid4().hex,
            fd=fd,
            lock_path=lock_path,
        )
        _write_owner_sidecar(
            lock_path,
            operation=lease.operation,
            pid=lease.pid,
            acquired_at=lease.acquired_at,
            invocation_id=lease.invocation_id,
        )
        _lease_stack.set(stack + (lease,))
        return lease

    def probe_busy(self) -> dict[str, Any] | None:
        """Best-effort: owner info dict when another writer holds the lease.

        Try-acquire then immediately release (D9).  Returns ``None`` when
        the root is free, when the *current context itself* holds the
        lease (the caller is the owner, not a third party), or when the
        probe cannot be performed (unsupported platform, IO error) — the
        probe is strictly read-only and never raises.

        The try-acquire is a real exclusive OS lock held for a micro-window:
        a mutation starting exactly while the probe holds it may observe
        one spurious BUSY without owner diagnostics.  That is a safe
        failure — no invariant is ever violated — and the user simply
        retries.
        """
        lock_path = self.lock_path()
        for lease in _lease_stack.get():
            if lease._lock_path == lock_path:
                return None
        fd: int | None = None
        try:
            _check_platform()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = _open_lock_fd(lock_path)
            if _backend_kind() == "windows":
                _ensure_lock_range_backing(fd)
            acquired = _try_lock(fd)
        except (RuntimeMutationLockError, OSError):
            if fd is not None:
                _close_fd_best_effort(fd)
            return None
        if acquired:
            _release_fd(fd)
            return None
        _close_fd_best_effort(fd)
        return _read_owner_info(lock_path)

    @staticmethod
    def current_lease() -> "RuntimeMutationLease | None":
        """The innermost lease held by the current thread/task, or ``None``."""
        stack = _lease_stack.get()
        return stack[-1] if stack else None

    @staticmethod
    def require_lease(what: str) -> RuntimeMutationLease:
        """Require a held lease in the current context (D2 contract).

        Returns the innermost lease token.  Raises
        :class:`RuntimeMutationLeaseRequired` when no lease is held — the
        low-level mutating primitives must fail closed without one.
        """
        lease = RuntimeMutationLock.current_lease()
        if lease is None or not lease._held:
            raise RuntimeMutationLeaseRequired(what)
        return lease


__all__ = [
    "OPERATION_GPU_INSTALL",
    "OPERATION_PRODUCT_INSTALL",
    "OPERATION_PRODUCT_UPDATE",
    "OPERATION_RUNTIME_APPLY",
    "OPERATION_RUNTIME_CREATE",
    "OPERATION_RUNTIME_DISCARD",
    "OPERATION_RUNTIME_GC",
    "OPERATION_RUNTIME_ROLLBACK",
    "RuntimeMutationBusyError",
    "RuntimeMutationLease",
    "RuntimeMutationLeaseRequired",
    "RuntimeMutationLock",
    "RuntimeMutationLockError",
]
