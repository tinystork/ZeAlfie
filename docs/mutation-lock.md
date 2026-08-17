# Runtime Mutation Lock (ZA-M1-2L)

The runtime mutation lock serializes mutating ZeAlfie operations against
a shared runtime root.

## Invariant

> **FOR ONE RUNTIME ROOT: at most ONE mutating ZeAlfie operation may
> own the runtime at a time.**

The invariant holds among *cooperating* ZeAlfie writers — every ZeAlfie
code path that mutates a runtime root must acquire the lease first
(`src/zealfie/runtime/mutation_lock.py`).

## Scope

The lock is scoped **per runtime root**, not machine-wide.  Each resolved
runtime root derives its own lock file; two different runtime roots never
contend.  The lock covers everything inside `runtime_root/`
(`slots/<id>/`, `state/active.json`, `state/product-provenance.json`,
`state/installed-lock.json`, `state/accelerated-metadata.json`).

Configuration stores that live *outside* the runtime root
(`desired-products.toml` under `XDG_DATA_HOME`, `product-policy.toml`
under `XDG_CONFIG_HOME`) are **out of scope** (D10): standalone policy /
selection writes are not runtime mutations.  The selection write that
follows a product install is inside the `product-install` lease window.

## Lock file location (D5)

The lock is an exclusive OS-level lock (`fcntl.flock` on POSIX,
`msvcrt.locking` on Windows) on a **sibling** of the *resolved* runtime
root:

```
<root>.parent / f".{root.name}.zealfie-mutation.lock"
```

If `root.name` is empty (e.g. the filesystem root), the name falls back
to `".zealfie-" + sha256(str(root))[:16] + ".mutation-lock"`.

The root is resolved (`Path.resolve(strict=False)`) *before* deriving the
lock path, so equivalent spellings of the same root — symlinks, trailing
slashes, relative vs absolute paths, `..` detours — map to the same lock
file.  On Windows the resolved root is additionally canonicalized with
`os.path.normcase` (case/separator), because Windows filesystems are
case-insensitive: the same root spelled differently must map to the same
lock file.  Only the lock file's parent directory is ever created; the runtime
root itself is **never** created by locking (an `ABSENT` runtime stays
`ABSENT`, and nothing is written under `slots/` or `state/`).

Each acquisition atomically overwrites a diagnostic sidecar
`<lock_path>.owner.json` (mkstemp + fsync + `os.replace`, best-effort —
a failed write never fails the acquisition) containing exactly
`{operation, pid, acquired_at, invocation_id}`.  The sidecar is
**diagnostics only, never authority**: stale, corrupt or missing
sidecars are tolerated.

## Owner lifetime

The lease is owned from `acquire()` until `release()` (idempotent) or
until the owning process dies.  There is no timeout and no renewal:
leases are held for the whole mutation window, including long downloads
and builds (D1, early acquisition).

## Crash behavior

`flock` locks are released by the operating system when the holding
process dies or the file descriptor is closed — including `SIGKILL`.
Windows `msvcrt.locking` byte-range locks are likewise owned and
released by the OS on process death; no manual unlock is possible or
needed.  **No manual cleanup is ever required**: a crash leaves a stale
lock file and a stale owner sidecar on disk, and both are ignored by
subsequent acquisitions.

**FILE EXISTS != LOCK HELD.**  The lock file is a rendezvous point, not
the authority; only a live OS lock blocks (on either platform).  This module never deletes the
lock file (deleting it while held would allow a second holder on a new
inode).

## Advisory / cooperative nature

The lock serializes **cooperating ZeAlfie writers** — processes that use
`RuntimeMutationLock.acquire` before mutating.  Writers outside ZeAlfie's
control (manual edits, external tools) are not covered and remain the
caller's responsibility (mission §29).  The lock also cannot protect
against an external process deleting the lock file itself while held.

**The mutation lock serializes cooperating ZeAlfie writers. It does not
replace stale-state validation or atomic persistence.**

## Read-only behavior

Read-only commands never acquire the lock and are never blocked:
`runtime status`, `products`, `runtime plan`, `gpu-plan` keep working
while a mutation is in progress.  `runtime gc-plan` may call
`probe_busy()` (try-acquire + immediate release, strictly read-only,
never raises) to add a warning line when a writer is active (D9).

`probe_busy()` takes a real exclusive OS lock for a micro-window (it
must try-acquire to observe contention).  A mutation starting exactly
while the probe holds it may receive one spurious BUSY without owner
diagnostics.  That is a safe failure — no invariant is ever violated —
and the user simply retries.

## BUSY semantics (D6)

Acquisition is strictly **non-blocking and fail-fast**, with zero retry.
When another writer holds the lease:

* `acquire()` raises `RuntimeMutationBusyError` with the message
  *"Runtime is busy with another ZeAlfie mutation. No changes have been
  applied."* followed by owner diagnostics (`operation`, `pid`,
  `acquired_at`, `invocation_id`) when the sidecar is readable;
* no mutation is performed and nothing is written to the runtime.

When the primitive itself fails (unsupported platform, missing
`fcntl`/`msvcrt`, permissions, OS error), `acquire()` raises
`RuntimeMutationLockError` — **fail closed**: a mutation must never
proceed without a lease.

## Nested transaction rule (D3)

Leases are tracked in a `ContextVar` stack per thread/task:

* a **nested acquire for the same resolved root in the same context**
  (e.g. service → engine → transaction) reuses the existing lease token
  (reference count incremented, no second OS lock, no deadlock);
* a **different thread is a new writer**: its context is virgin, so it
  performs a real non-blocking OS lock and fails with BUSY while the
  lease is held — a different thread of the same process is never
  considered reentrant;
* **subprocesses never inherit the lock**: the fd is close-on-exec
  (PEP 446), so pip, probes and other children do not carry the lease
  and cannot keep it alive after the parent releases it.

`RuntimeMutationLock.require_lease(what)` (D2) lets the low-level
mutating primitives (`RuntimeTransaction.activate`,
`RuntimeTransaction.rollback`, `discard_slot`) prove a lease is held in
the current context — raising `RuntimeMutationLeaseRequired` otherwise
(fail closed).

## Platform implementation (D4)

* **Linux / macOS (POSIX)**: `fcntl.flock(LOCK_EX | LOCK_NB)` on the
  lock file descriptor.  `fcntl` is imported lazily inside the backend
  functions, so the module imports on every platform.
* **Windows**: `msvcrt.locking(LK_NBLCK)` — a non-blocking exclusive
  byte-range lock on byte range `[0, 1)` of the lock file.  The lock
  file is padded to at least one byte on first creation so the range
  legally exists (the contents are never authority).  Windows locks are
  owned by the OS and released automatically on process death; a second
  handle in the same process conflicts natively, so a different thread
  gets BUSY without extra local state.  `msvcrt` is imported lazily
  inside the backend functions.
* **Other platforms**: no backend — `acquire()` raises
  `RuntimeMutationLockError` *before* creating any file or directory,
  and no mutation is allowed without a lease (fail closed).

## Real platform witness status (mutation lock)

The lock primitive is witness-proven on real hosts for every supported
backend:

| Platform | Primitive | Real witness | Evidence |
|---|---|---|---|
| Linux (POSIX) | `fcntl.flock(LOCK_EX \| LOCK_NB)` | **PASS** | `tests/witness/posix_lock_ci_witness.py` on real Linux hosts: E2 same-root exclusion, E3 SIGKILL crash release, E4 different-root independence |
| macOS (POSIX) | same primitive | **PASS — real GitHub-hosted Darwin witness (2026-08-17)** | `posix_lock_ci_witness.py` on real GitHub-hosted runners `macos-15` (arm64) and `macos-15-intel` (x86_64), workflow run 32035282141 (HEAD 8e2432e): E2/E3/E4 all PASS |
| Windows | `msvcrt.locking(LK_NBLCK)` byte-range lock | **PASS — real witness W1 (2026-08-17)** | `tests/witness/windows_lock_witness.py` run by a human on real Windows: same-root BUSY, force-kill crash release without lock-file deletion, different-root concurrency, case normalization |

What this does **not** claim: the macOS witness qualifies the **core
runtime and POSIX mutation lock** on real Darwin hosts only.  GUI
behaviour, `.app` packaging, codesigning / notarization, Metal/GPU
acceleration, and end-to-end product usage on macOS remain separate
**HUMAN / FUTURE GATES** and are not covered by this evidence.

## Relationship with the other safety layers

The mutation lock is one layer among three, each addressing a different
race:

* **Mutation lock (M1-2L)** — serializes cooperating writers for the
  whole mutation window (inter-process).
* **Stale-state validation (M1-2K / M0-8B)** — the stale transaction
  check (`STALE_TRANSACTION`) and the GC state fingerprint detect that
  the world changed between a snapshot and the write moment, including
  interleavings a lease cannot cover (e.g. two leases across different
  roots touching a shared config store).
* **Atomic persistence** — every state write is `mkstemp` + `fsync` +
  `os.replace`, so readers never observe a torn file at any instant.

The lock does not replace the other two, and neither do they replace the
lock: a writer that holds the lease still runs the stale checks, and the
atomic writes protect readers that never take the lock.

## Operation names (D7)

| Constant | Value |
|---|---|
| `OPERATION_RUNTIME_CREATE` | `runtime-create` |
| `OPERATION_RUNTIME_APPLY` | `runtime-apply` |
| `OPERATION_RUNTIME_ROLLBACK` | `runtime-rollback` |
| `OPERATION_RUNTIME_DISCARD` | `runtime-discard` |
| `OPERATION_RUNTIME_GC` | `runtime-gc` |
| `OPERATION_PRODUCT_INSTALL` | `product-install` |
| `OPERATION_PRODUCT_UPDATE` | `product-update` |
| `OPERATION_GPU_INSTALL` | `gpu-install` |

## API summary

```python
lock = RuntimeMutationLock(runtime_root)
lock.lock_path()                        # derived lock file (pure)
lease = lock.acquire("runtime-apply")   # non-blocking; Busy/LockError
lock.probe_busy()                       # owner info dict | None
RuntimeMutationLock.current_lease()     # innermost lease | None
RuntimeMutationLock.require_lease(what) # innermost lease; LeaseRequired

lease.release()                         # idempotent; refcount-aware
with lock.acquire("runtime-gc"):        # context manager
    ...
```

Exceptions: `RuntimeMutationBusyError`, `RuntimeMutationLockError`,
`RuntimeMutationLeaseRequired`.
