"""Runtime mutation lock tests (ZA-M1-2L Phase B+C).

Covers the mission §12 proof matrix (proofs 1-10), the §31 stress test
(N=6 subprocesses, exactly one owner), the §35 subprocess synchronization
discipline (pipe / READY handshakes — never sleep-based), path safety and
root identity, PEP 446 fd non-inheritance, and the fail-closed non-POSIX
platform gate.

All subprocess synchronization is explicit (stdout READY lines, stdin
commands); no test uses ``sleep`` as a synchronization mechanism.

Proof → test mapping:
  1  test_acquire_release_normal_cycle
  2  test_second_process_same_root_is_busy
  3  test_different_roots_do_not_conflict
  4  test_owner_releases_normally_allows_reacquisition
  5  test_owner_crash_releases_lock_without_cleanup
  6  test_persistent_lock_file_alone_does_not_block
  7  test_stale_owner_sidecar_does_not_block (parametrized)
  8  test_nested_same_context_reuses_lease_three_levels
  9  test_different_thread_same_process_is_busy
  10 test_exception_in_context_manager_releases_lease

Additional (mission §31 / §35 / path safety / platform):
  - test_stress_six_processes_exactly_one_owner
  - test_lock_path_identity_across_spellings
  - test_fallback_lock_name_for_nameless_root
  - test_lock_creation_does_not_create_runtime_root
  - test_probe_busy_during_and_after_hold
  - test_lock_fd_is_not_inherited_by_subprocess (PEP 446)
  - test_non_posix_platform_fails_closed_before_any_disk_write
  - test_require_lease_without_lease_raises
  - test_require_lease_with_lease_returns_token
  - test_operation_constants_exact_values
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import stat
import subprocess
import sys
import textwrap
import threading
from datetime import datetime
from pathlib import Path

import pytest

from zealfie.runtime import mutation_lock as mutation_lock_module
from zealfie.runtime.mutation_lock import (
    OPERATION_GPU_INSTALL,
    OPERATION_PRODUCT_INSTALL,
    OPERATION_PRODUCT_UPDATE,
    OPERATION_RUNTIME_APPLY,
    OPERATION_RUNTIME_CREATE,
    OPERATION_RUNTIME_DISCARD,
    OPERATION_RUNTIME_GC,
    OPERATION_RUNTIME_ROLLBACK,
    RuntimeMutationBusyError,
    RuntimeMutationLease,
    RuntimeMutationLeaseRequired,
    RuntimeMutationLock,
    RuntimeMutationLockError,
)

needs_posix = pytest.mark.skipif(
    os.name != "posix", reason="fcntl.flock backend is POSIX-only"
)

BUSY_MESSAGE_CORE = (
    "Runtime is busy with another ZeAlfie mutation. "
    "No changes have been applied."
)


# ---------------------------------------------------------------------------
# Subprocess helpers — explicit READY/stdin synchronization, no sleeps.
# ---------------------------------------------------------------------------


def _spawn(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _read_line(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    """Read one stdout line from *proc* with a hard timeout (no sleep loop)."""
    box: queue.Queue[str] = queue.Queue()

    def _target() -> None:
        line = proc.stdout.readline()
        box.put(line)

    reader = threading.Thread(target=_target, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise AssertionError(
            f"timed out after {timeout}s waiting for child output"
        )
    return box.get()


def _expect_line(proc: subprocess.Popen, prefixes: tuple[str, ...]) -> str:
    """Read one line and assert it starts with one of *prefixes*."""
    line = _read_line(proc)
    if not line.startswith(prefixes):
        proc.kill()
        proc.wait()
        raise AssertionError(
            f"expected line starting with one of {prefixes!r}, got {line!r}"
        )
    return line


def _send(proc: subprocess.Popen, text: str) -> None:
    proc.stdin.write(text + "\n")
    proc.stdin.flush()


def _reap(proc: subprocess.Popen, timeout: float = 30.0) -> int:
    return proc.wait(timeout=timeout)


@contextlib.contextmanager
def _child_holding(root: Path, operation: str):
    """Spawn a child that acquires the lock, prints READY, then waits on stdin."""
    child = _spawn(_HOLD_SCRIPT, str(root), operation)
    try:
        _expect_line(child, ("READY",))
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


_HOLD_SCRIPT = textwrap.dedent(
    """
    import sys
    from zealfie.runtime.mutation_lock import RuntimeMutationLock
    lock = RuntimeMutationLock(sys.argv[1])
    lease = lock.acquire(sys.argv[2])
    print("READY", flush=True)
    sys.stdin.readline()
    lease.release()
    print("RELEASED", flush=True)
    """
).strip()


_CRASH_SCRIPT = textwrap.dedent(
    """
    import sys
    from zealfie.runtime.mutation_lock import RuntimeMutationLock
    lock = RuntimeMutationLock(sys.argv[1])
    lock.acquire(sys.argv[2])
    print("READY", flush=True)
    sys.stdin.read()
    """
).strip()


def _acquire_in_thread(
    lock: RuntimeMutationLock, operation: str = OPERATION_RUNTIME_GC
):
    """Run acquire in a fresh thread (virgin context) and return result."""
    box: list = []

    def _target() -> None:
        try:
            box.append(lock.acquire(operation))
        except Exception as exc:  # noqa: BLE001 — captured for assertion
            box.append(exc)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "acquire thread did not finish"
    return box[0]


def _probe_in_thread(lock: RuntimeMutationLock):
    """Run probe_busy in a fresh thread (virgin context) and return result."""
    box: list = []

    def _target() -> None:
        box.append(lock.probe_busy())

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "probe thread did not finish"
    return box[0]


def _owner_sidecar(lock: RuntimeMutationLock) -> Path:
    return Path(str(lock.lock_path()) + ".owner.json")


# ---------------------------------------------------------------------------
# Proof 1 — normal acquire/release
# ---------------------------------------------------------------------------


@needs_posix
def test_acquire_release_normal_cycle(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "runtime_root"
    lock = RuntimeMutationLock(root)
    assert lock.lock_path() == (
        tmp_path / "nested" / ".runtime_root.zealfie-mutation.lock"
    )
    lease = lock.acquire(OPERATION_RUNTIME_CREATE)
    try:
        assert isinstance(lease, RuntimeMutationLease)
        assert lease.runtime_root == root.resolve()
        assert lease.operation == OPERATION_RUNTIME_CREATE
        assert lease.pid == os.getpid()
        assert len(lease.invocation_id) == 32
        assert all(c in "0123456789abcdef" for c in lease.invocation_id)
        datetime.fromisoformat(lease.acquired_at)  # must parse
        assert RuntimeMutationLock.current_lease() is lease
        # lock file exists, 0o600, and the runtime root was NOT created
        lock_path = lock.lock_path()
        assert lock_path.is_file()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert not root.exists()
        # sidecar: exactly the 4 diagnostic fields, matching this lease
        sidecar = _owner_sidecar(lock)
        assert sidecar.is_file()
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert set(payload) == {"operation", "pid", "acquired_at", "invocation_id"}
        assert payload == {
            "operation": OPERATION_RUNTIME_CREATE,
            "pid": os.getpid(),
            "acquired_at": lease.acquired_at,
            "invocation_id": lease.invocation_id,
        }
        # held → another (virgin) context sees the owner; we see ourselves as free
        assert lock.probe_busy() is None
        info = _probe_in_thread(lock)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["operation"] == OPERATION_RUNTIME_CREATE
    finally:
        lease.release()
    # released: idempotent release, no current lease, root free
    lease.release()
    assert RuntimeMutationLock.current_lease() is None
    assert lock.probe_busy() is None
    # re-acquisition works normally
    with lock.acquire(OPERATION_RUNTIME_GC):
        pass


# ---------------------------------------------------------------------------
# Proof 2 — second process, same root → BUSY
# ---------------------------------------------------------------------------


@needs_posix
def test_second_process_same_root_is_busy(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    with _child_holding(root, OPERATION_RUNTIME_APPLY) as child:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            lock.acquire(OPERATION_RUNTIME_GC)
        exc = excinfo.value
        assert BUSY_MESSAGE_CORE in str(exc)
        # diagnostics come from the child's owner sidecar
        assert exc.pid == child.pid
        assert exc.operation == OPERATION_RUNTIME_APPLY
        assert exc.acquired_at is not None
        assert exc.invocation_id is not None
        # nothing was mutated on our side
        assert not root.exists()
        _send(child, "RELEASE")
        _expect_line(child, ("RELEASED",))
        _reap(child)
    # owner released normally → we can acquire now
    with lock.acquire(OPERATION_RUNTIME_GC):
        pass


# ---------------------------------------------------------------------------
# Proof 3 — different roots, simultaneous holds OK
# ---------------------------------------------------------------------------


@needs_posix
def test_different_roots_do_not_conflict(tmp_path: Path) -> None:
    root_a = tmp_path / "a" / "runtime_root"
    root_b = tmp_path / "b" / "runtime_root"
    lock_a = RuntimeMutationLock(root_a)
    lock_b = RuntimeMutationLock(root_b)
    assert lock_a.lock_path() != lock_b.lock_path()
    with lock_a.acquire(OPERATION_RUNTIME_APPLY):
        with lock_b.acquire(OPERATION_RUNTIME_GC):
            assert lock_a.lock_path().is_file()
            assert lock_b.lock_path().is_file()
    # cross-process: a child holding root A does not block root B in-process
    with _child_holding(root_a, OPERATION_RUNTIME_APPLY) as child:
        with lock_b.acquire(OPERATION_RUNTIME_GC):
            pass
        with pytest.raises(RuntimeMutationBusyError):
            lock_a.acquire(OPERATION_RUNTIME_GC)
        _send(child, "RELEASE")
        _expect_line(child, ("RELEASED",))
        _reap(child)


# ---------------------------------------------------------------------------
# Proof 4 — owner releases normally → re-acquisition OK
# ---------------------------------------------------------------------------


@needs_posix
def test_owner_releases_normally_allows_reacquisition(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    with _child_holding(root, OPERATION_PRODUCT_INSTALL) as child:
        with pytest.raises(RuntimeMutationBusyError):
            lock.acquire(OPERATION_RUNTIME_GC)
        _send(child, "RELEASE")
        _expect_line(child, ("RELEASED",))
        assert _reap(child) == 0
        # clean exit releases the flock → immediate re-acquisition
        with lock.acquire(OPERATION_RUNTIME_GC):
            assert lock.probe_busy() is None
        # sidecar was overwritten by our own acquisition
        assert json.loads(
            _owner_sidecar(lock).read_text(encoding="utf-8")
        )["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Proof 5 — owner crash (SIGKILL) → re-acquisition without manual cleanup
# ---------------------------------------------------------------------------


@needs_posix
def test_owner_crash_releases_lock_without_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    child = _spawn(_CRASH_SCRIPT, str(root), OPERATION_RUNTIME_APPLY)
    try:
        _expect_line(child, ("READY",))
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            lock.acquire(OPERATION_RUNTIME_GC)
        assert excinfo.value.pid == child.pid
        # SIGKILL — no cleanup path can run in the child
        child.kill()
        assert _reap(child) != 0
        # stale lock file + stale owner sidecar remain on disk …
        assert lock.lock_path().exists()
        assert _owner_sidecar(lock).exists()
        # … but the OS released the flock: re-acquisition needs no cleanup
        with lock.acquire(OPERATION_RUNTIME_GC):
            # our acquisition overwrote the stale sidecar (never authority)
            payload = json.loads(
                _owner_sidecar(lock).read_text(encoding="utf-8")
            )
            assert payload["pid"] == os.getpid()
            assert payload["operation"] == OPERATION_RUNTIME_GC
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# ---------------------------------------------------------------------------
# Proof 6 — a persistent lock file alone does NOT block
# ---------------------------------------------------------------------------


@needs_posix
def test_persistent_lock_file_alone_does_not_block(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    # pre-existing lock file with arbitrary (stale) content — FILE EXISTS
    # != LOCK HELD
    lock.lock_path().parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path().write_text("stale garbage from a previous crash\n")
    assert lock.probe_busy() is None
    with lock.acquire(OPERATION_RUNTIME_GC):
        pass
    assert lock.lock_path().is_file()


# ---------------------------------------------------------------------------
# Proof 7 — stale owner sidecars (corrupt JSON, dead pid, missing fields)
# ---------------------------------------------------------------------------


@needs_posix
@pytest.mark.parametrize(
    "stale_content",
    [
        pytest.param(b"{not valid json", id="corrupt-json"),
        pytest.param(
            json.dumps(
                {
                    "operation": "runtime-gc",
                    "pid": 99999999,  # pid_max is far below this → dead pid
                    "acquired_at": "2020-01-01T00:00:00+00:00",
                    "invocation_id": "deadbeef",
                }
            ).encode(),
            id="nonexistent-pid",
        ),
        pytest.param(b'{"operation": "runtime-gc"}', id="missing-fields"),
        pytest.param(b"[]", id="non-dict-json"),
    ],
)
def test_stale_owner_sidecar_does_not_block(
    tmp_path: Path, stale_content: bytes
) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    _owner_sidecar(lock).parent.mkdir(parents=True, exist_ok=True)
    _owner_sidecar(lock).write_bytes(stale_content)
    # stale diagnostics are tolerated — never authority
    with lock.acquire(OPERATION_RUNTIME_GC):
        payload = json.loads(_owner_sidecar(lock).read_text(encoding="utf-8"))
        assert set(payload) == {"operation", "pid", "acquired_at", "invocation_id"}
        assert payload["pid"] == os.getpid()
        assert payload["operation"] == OPERATION_RUNTIME_GC


# ---------------------------------------------------------------------------
# Proof 8 — nested acquisition in the same context (service→engine→txn)
# ---------------------------------------------------------------------------


@needs_posix
def test_nested_same_context_reuses_lease_three_levels(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    # service → engine → transaction, three nested logical levels
    service_lease = lock.acquire(OPERATION_PRODUCT_INSTALL)
    try:
        engine_lease = lock.acquire(OPERATION_RUNTIME_APPLY)
        txn_lease = lock.acquire(OPERATION_RUNTIME_CREATE)
        assert engine_lease is service_lease
        assert txn_lease is service_lease
        assert RuntimeMutationLock.current_lease() is service_lease
        assert RuntimeMutationLock.require_lease("activate") is service_lease
        # our own context sees no third party …
        assert lock.probe_busy() is None
        # … but a different thread (virgin context) sees the real lock
        assert isinstance(_acquire_in_thread(lock), RuntimeMutationBusyError)
        # one level released → still held (refcount 2)
        txn_lease.release()
        assert RuntimeMutationLock.current_lease() is service_lease
        assert isinstance(_acquire_in_thread(lock), RuntimeMutationBusyError)
        engine_lease.release()
        assert RuntimeMutationLock.current_lease() is service_lease
        assert isinstance(_acquire_in_thread(lock), RuntimeMutationBusyError)
        service_lease.release()
        # fully released → a fresh context acquires for real
        result = _acquire_in_thread(lock, OPERATION_RUNTIME_GC)
        assert isinstance(result, RuntimeMutationLease)
        result.release()
    finally:
        service_lease.release()
    assert RuntimeMutationLock.current_lease() is None


# ---------------------------------------------------------------------------
# Proof 9 — a different thread of the same process is a NEW writer → BUSY
# ---------------------------------------------------------------------------


@needs_posix
def test_different_thread_same_process_is_busy(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    with lock.acquire(OPERATION_RUNTIME_APPLY):
        # NOT reentrant: a different thread has a virgin context
        result = _acquire_in_thread(lock, OPERATION_RUNTIME_GC)
        assert isinstance(result, RuntimeMutationBusyError)
        assert result.pid == os.getpid()
        assert result.operation == OPERATION_RUNTIME_APPLY
        assert BUSY_MESSAGE_CORE in str(result)
        info = _probe_in_thread(lock)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["operation"] == OPERATION_RUNTIME_APPLY
    # after release, the thread acquires normally
    result = _acquire_in_thread(lock, OPERATION_RUNTIME_GC)
    assert isinstance(result, RuntimeMutationLease)
    result.release()


# ---------------------------------------------------------------------------
# Proof 10 — an exception inside the context manager releases the lease
# ---------------------------------------------------------------------------


@needs_posix
def test_exception_in_context_manager_releases_lease(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    with pytest.raises(ValueError, match="boom"):
        with lock.acquire(OPERATION_RUNTIME_GC):
            raise ValueError("boom")
    assert RuntimeMutationLock.current_lease() is None
    assert lock.probe_busy() is None
    with lock.acquire(OPERATION_RUNTIME_GC):
        pass


# ---------------------------------------------------------------------------
# Stress (§31) — N=6 subprocesses, synchronized GO, exactly one owner
# ---------------------------------------------------------------------------

_STRESS_SCRIPT = textwrap.dedent(
    """
    import sys
    from zealfie.runtime.mutation_lock import (
        RuntimeMutationBusyError,
        RuntimeMutationLock,
    )
    lock = RuntimeMutationLock(sys.argv[1])
    op = sys.argv[2]
    for round_no in (1, 2):
        print("READY", round_no, flush=True)
        cmd = sys.stdin.readline().strip()
        if cmd == "EXIT":
            break
        try:
            lease = lock.acquire(op)
        except RuntimeMutationBusyError:
            print("BUSY", round_no, flush=True)
        else:
            print("OWNER", round_no, flush=True)
            sys.stdin.readline()  # wait for RELEASE
            lease.release()
            print("RELEASED", round_no, flush=True)
    """
).strip()


@needs_posix
def test_stress_six_processes_exactly_one_owner(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    n = 6
    children = [
        _spawn(_STRESS_SCRIPT, str(root), OPERATION_RUNTIME_GC) for _ in range(n)
    ]
    try:
        # ---- round 1: all six race after a synchronized GO ----
        for child in children:
            _expect_line(child, ("READY 1",))
        for child in children:
            _send(child, "GO")
        outcomes = [
            _expect_line(child, ("OWNER 1", "BUSY 1")) for child in children
        ]
        owners = [line for line in outcomes if line.startswith("OWNER 1")]
        busy = [line for line in outcomes if line.startswith("BUSY 1")]
        assert len(owners) == 1
        assert len(busy) == n - 1
        round1_owner = next(
            child
            for child, line in zip(children, outcomes)
            if line.startswith("OWNER 1")
        )
        _send(round1_owner, "RELEASE")
        _expect_line(round1_owner, ("RELEASED 1",))

        # ---- round 2: the round-1 owner exits; the remaining five race ----
        # (the round-1 winner is retired so the round-2 owner is guaranteed
        # to be a *different* process)
        _expect_line(round1_owner, ("READY 2",))
        _send(round1_owner, "EXIT")
        assert _reap(round1_owner) == 0
        contenders = [c for c in children if c is not round1_owner]
        for child in contenders:
            _expect_line(child, ("READY 2",))
        for child in contenders:
            _send(child, "GO")
        outcomes2 = [
            _expect_line(child, ("OWNER 2", "BUSY 2")) for child in contenders
        ]
        owners2 = [line for line in outcomes2 if line.startswith("OWNER 2")]
        busy2 = [line for line in outcomes2 if line.startswith("BUSY 2")]
        assert len(owners2) == 1
        assert len(busy2) == len(contenders) - 1
        round2_owner = next(
            child
            for child, line in zip(contenders, outcomes2)
            if line.startswith("OWNER 2")
        )
        assert round2_owner is not round1_owner
        _send(round2_owner, "RELEASE")
        _expect_line(round2_owner, ("RELEASED 2",))
        # remaining children exit on their own after round 2
        for child in contenders:
            assert _reap(child) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


# ---------------------------------------------------------------------------
# Path safety / root identity
# ---------------------------------------------------------------------------


@needs_posix
def test_lock_path_identity_across_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real_runtime_root"
    real.mkdir()
    link = tmp_path / "symlink_to_runtime_root"
    link.symlink_to(real, target_is_directory=True)

    lock_real = RuntimeMutationLock(real).lock_path()
    # symlink → same resolved root → same lock path
    assert RuntimeMutationLock(link).lock_path() == lock_real
    # trailing slash
    assert RuntimeMutationLock(Path(str(real) + os.sep)).lock_path() == lock_real
    # relative (from the same cwd) vs absolute
    monkeypatch.chdir(tmp_path)
    assert RuntimeMutationLock("real_runtime_root").lock_path() == lock_real
    # ".." detour resolves to the same root
    assert (
        RuntimeMutationLock(tmp_path / "other" / ".." / "real_runtime_root").lock_path()
        == lock_real
    )
    # distinct roots → distinct lock paths
    assert RuntimeMutationLock(tmp_path / "other_runtime_root").lock_path() != lock_real
    # resolved root is exposed, and lock_path is a sibling of the root
    lock = RuntimeMutationLock(link)
    assert lock.runtime_root == real.resolve()
    assert lock.lock_path().parent == lock.runtime_root.parent


@needs_posix
def test_fallback_lock_name_for_nameless_root() -> None:
    root = Path("/")
    lock = RuntimeMutationLock(root)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    assert lock.lock_path() == root / f".zealfie-{digest}.mutation-lock"


@needs_posix
def test_lock_creation_does_not_create_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "deeply" / "nested" / "runtime_root"
    lock = RuntimeMutationLock(root)
    with lock.acquire(OPERATION_RUNTIME_GC):
        # ABSENT stays ABSENT: nothing is created under the runtime root
        assert not root.exists()
        assert not (root / "state").exists()
        assert not (root / "slots").exists()
        assert lock.lock_path().is_file()
        # only the lock file and its sidecar appear in the parent directory
        names = {p.name for p in root.parent.iterdir()}
        assert names == {lock.lock_path().name, _owner_sidecar(lock).name}


@needs_posix
def test_probe_busy_during_and_after_hold(tmp_path: Path) -> None:
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    with _child_holding(root, OPERATION_RUNTIME_GC) as child:
        info = lock.probe_busy()
        assert info is not None
        assert info["operation"] == OPERATION_RUNTIME_GC
        assert info["pid"] == child.pid
        assert info["acquired_at"] is not None
        assert info["invocation_id"] is not None
        _send(child, "RELEASE")
        _expect_line(child, ("RELEASED",))
        _reap(child)
    # after the owner releases, the probe reports free
    assert lock.probe_busy() is None
    with lock.acquire(OPERATION_RUNTIME_GC):
        pass


@needs_posix
def test_lock_fd_is_not_inherited_by_subprocess(tmp_path: Path) -> None:
    """PEP 446: a child spawned while the lock is held must not inherit the fd."""
    script = textwrap.dedent(
        """
        import os
        import sys
        st = os.stat(sys.argv[1])
        target = (st.st_dev, st.st_ino)
        try:
            fds = sorted(int(name) for name in os.listdir("/proc/self/fd"))
        except OSError:
            print("NOPROC", flush=True)
            sys.exit(0)
        inherited = []
        for fd in fds:
            try:
                s = os.fstat(fd)
            except OSError:
                continue
            if (s.st_dev, s.st_ino) == target:
                inherited.append(fd)
        print("INHERITED" if inherited else "CLEAN", flush=True)
        """
    ).strip()
    root = tmp_path / "runtime_root"
    lock = RuntimeMutationLock(root)
    lease = lock.acquire(OPERATION_RUNTIME_APPLY)
    try:
        child = _spawn(script, str(lock.lock_path()))
        line = _read_line(child)
        _reap(child)
    finally:
        lease.release()
    if line.startswith("NOPROC"):
        pytest.skip("no /proc/self/fd on this platform")
    assert line.startswith("CLEAN"), f"child inherited the lock fd: {line!r}"


def test_non_posix_platform_fails_closed_before_any_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a non-POSIX platform by monkeypatching the platform check
    # (on a real Windows host this returns False naturally).  Patching
    # os.name itself would make pathlib switch to WindowsPath.
    monkeypatch.setattr(mutation_lock_module, "_platform_is_posix", lambda: False)
    root = tmp_path / "sub" / "runtime_root"
    lock = RuntimeMutationLock(root)
    with pytest.raises(RuntimeMutationLockError):
        lock.acquire(OPERATION_RUNTIME_GC)
    # fail closed BEFORE any disk write: no lock file, no directory, no root
    assert not lock.lock_path().exists()
    assert not lock.lock_path().parent.exists()
    assert not root.exists()
    # the read-only probe degrades gracefully instead of raising
    assert lock.probe_busy() is None


# ---------------------------------------------------------------------------
# require_lease contract (D2)
# ---------------------------------------------------------------------------


def test_require_lease_without_lease_raises() -> None:
    assert RuntimeMutationLock.current_lease() is None
    with pytest.raises(RuntimeMutationLeaseRequired):
        RuntimeMutationLock.require_lease("RuntimeTransaction.activate")


@needs_posix
def test_require_lease_with_lease_returns_token(tmp_path: Path) -> None:
    lock = RuntimeMutationLock(tmp_path / "runtime_root")
    with lock.acquire(OPERATION_RUNTIME_APPLY) as lease:
        assert RuntimeMutationLock.require_lease("activate") is lease
        nested = lock.acquire(OPERATION_RUNTIME_GC)
        assert RuntimeMutationLock.require_lease("activate") is nested
        nested.release()
        assert RuntimeMutationLock.require_lease("activate") is lease


# ---------------------------------------------------------------------------
# Operation name constants (D7)
# ---------------------------------------------------------------------------


def test_operation_constants_exact_values() -> None:
    assert OPERATION_RUNTIME_CREATE == "runtime-create"
    assert OPERATION_RUNTIME_APPLY == "runtime-apply"
    assert OPERATION_RUNTIME_ROLLBACK == "runtime-rollback"
    assert OPERATION_RUNTIME_DISCARD == "runtime-discard"
    assert OPERATION_RUNTIME_GC == "runtime-gc"
    assert OPERATION_PRODUCT_INSTALL == "product-install"
    assert OPERATION_PRODUCT_UPDATE == "product-update"
    assert OPERATION_GPU_INSTALL == "gpu-install"
