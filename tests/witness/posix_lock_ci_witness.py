"""POSIX (Linux/macOS) CI witness for the ZeAlfie runtime mutation lock.

Scope: this file proves the REAL ``fcntl.flock`` backend of
:class:`zealfie.runtime.mutation_lock.RuntimeMutationLock` on POSIX
hosts (Linux and macOS), using real separate OS processes and real
kernel-owned locks.  It maps to the mutation-lock contract docs (D4
backend, D5 lock path, D6 BUSY) in ``docs/mutation-lock.md``.

**Zero mocks.**  Nothing in this file monkeypatches ``fcntl``,
``os.name``, ``sys.platform``, ``subprocess``, or the filesystem: every
exclusion observed below is enforced by the kernel, not by test fakes.
The only synchronisation primitive is reading the owner child's
``READY`` line (which is printed only after the OS lock is held); the
bounded timeouts only bound hangs, never *prove* anything.

Usage (one process, real lock backend):

    python tests/witness/posix_lock_ci_witness.py owner     <root> <operation>
    python tests/witness/posix_lock_ci_witness.py contender <root> <operation>
    python tests/witness/posix_lock_ci_witness.py drive

Protocol for ``owner``: acquire -> print ``READY`` -> block on stdin ->
release -> print ``RELEASED`` -> exit 0.  BUSY exits 1, lock error
exits 2.

Protocol for ``contender``: try-acquire -> ``BUSY`` exit 1, lock error
exit 2, or ``ACQUIRED`` + ``RELEASED`` exit 0.

``drive`` is the CI driver: it creates two temp runtime roots and runs
three scenarios, each with its own PASS/FAIL line and one final verdict
line.  It exits 0 only if ALL scenarios pass.  Per-wait timeouts are
bounded (default 20s, override with ``ZEALFIE_WITNESS_TIMEOUT``).

Scenarios:

* E2 same-root exclusion: an owner holding root1 blocks a contender on
  root1 (BUSY); after the owner releases cleanly, the contender
  acquires.
* E3 crash release: an owner holding root1 is SIGKILLed (no
  release code runs); the OS must release the flock so a contender
  acquires; the stale lock file stays on disk (file exists != lock
  held).
* E4 different-root independence: an owner holding root1 does not
  block a contender on root2.

On non-POSIX platforms the driver prints ``UNSUPPORTED_PLATFORM`` and
exits 3 (nothing to witness: the fcntl backend does not exist there).

Self-contained: prefers an installed ``zealfie`` package and falls back
to the ``src/`` sibling of the repository checkout only if the import
fails (clean checkout without install).
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from zealfie.runtime.mutation_lock import (
        RuntimeMutationBusyError,
        RuntimeMutationLock,
        RuntimeMutationLockError,
    )
except ImportError:  # clean checkout without install: repo src/ sibling
    _REPO_SRC = Path(__file__).resolve().parents[2] / "src"
    sys.path.insert(0, str(_REPO_SRC))
    from zealfie.runtime.mutation_lock import (
        RuntimeMutationBusyError,
        RuntimeMutationLock,
        RuntimeMutationLockError,
    )

#: Logical operation name recorded in the owner sidecar by every run.
_OPERATION = "za-m1-2m-ci"

#: Bounded per-wait timeout (seconds).  Overridable via environment.
_DEFAULT_TIMEOUT = 20.0


def _timeout() -> float:
    raw = os.environ.get("ZEALFIE_WITNESS_TIMEOUT")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Role modes (owner / contender) — separate OS processes, real flock
# ---------------------------------------------------------------------------


def _run_owner(root: str, operation: str) -> int:
    """Acquire, announce READY, hold until stdin EOF, release, exit 0."""
    try:
        lease = RuntimeMutationLock(root).acquire(operation)
    except RuntimeMutationBusyError:
        print("BUSY", flush=True)
        return 1
    except RuntimeMutationLockError as exc:
        print(f"LOCKERROR {exc}", flush=True)
        return 2
    print("READY", flush=True)
    sys.stdin.read()  # hold the kernel lock until the driver closes stdin
    lease.release()
    print("RELEASED", flush=True)
    return 0


def _run_contender(root: str, operation: str) -> int:
    """Try-acquire: BUSY exit 1, lock error exit 2, else ACQUIRED+RELEASED."""
    try:
        lease = RuntimeMutationLock(root).acquire(operation)
    except RuntimeMutationBusyError:
        print("BUSY", flush=True)
        return 1
    except RuntimeMutationLockError as exc:
        print(f"LOCKERROR {exc}", flush=True)
        return 2
    print("ACQUIRED", flush=True)
    lease.release()
    print("RELEASED", flush=True)
    return 0


# ---------------------------------------------------------------------------
# CI driver helpers — event-driven (select), no sleep-based synchronisation
# ---------------------------------------------------------------------------


def _child_args(*args: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), *args]


def _spawn_owner(root: Path) -> subprocess.Popen:
    """Start an owner child holding <root>; stdout/stderr are PIPEs."""
    return subprocess.Popen(
        _child_args("owner", str(root), _OPERATION),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_line(proc: subprocess.Popen, buf: list[bytes], timeout: float) -> str:
    """Read one line from the child stdout (select-based, no sleeps).

    *buf* (one-element list) carries partial data across calls so
    nothing is lost between successive reads.  Raises ``TimeoutError``
    if no line arrives within *timeout* seconds.
    """
    if b"\n" in buf[0]:
        line, rest = buf[0].split(b"\n", 1)
        buf[0] = rest
        return line.decode("utf-8", "replace")
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out after {timeout:.1f}s waiting for output "
                f"from pid {proc.pid}"
            )
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            continue  # timeout slice elapsed; loop re-checks the deadline
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            # EOF: return whatever partial line remains, if any.
            data, buf[0] = buf[0], b""
            if b"\n" in data:
                line, rest = data.split(b"\n", 1)
                buf[0] = rest
                return line.decode("utf-8", "replace")
            return data.decode("utf-8", "replace")
        buf[0] += chunk
        if b"\n" in buf[0]:
            line, rest = buf[0].split(b"\n", 1)
            buf[0] = rest
            return line.decode("utf-8", "replace")


def _drain(proc: subprocess.Popen, buf: list[bytes], timeout: float) -> tuple[str, str]:
    """Read all remaining stdout/stderr (bounded) so nothing deadlocks.

    Returns ``(stdout_remainder, stderr_all)`` as decoded strings.  The
    child is NOT waited here; the caller must still reap it.
    """
    out = buf[0]
    buf[0] = b""
    err = b""
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            try:
                out += os.read(proc.stdout.fileno(), 65536)
            except OSError:
                pass
            try:
                err += os.read(proc.stderr.fileno(), 65536)
            except OSError:
                pass
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        readable, _, _ = select.select(
            [proc.stdout.fileno(), proc.stderr.fileno()], [], [], min(remaining, 0.5)
        )
        if not readable:
            continue
        for fd in readable:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                continue
            if not chunk:
                continue
            if fd == proc.stdout.fileno():
                out += chunk
            else:
                err += chunk
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _finish_owner(proc: subprocess.Popen, buf: list[bytes], timeout: float) -> tuple[int, str, str]:
    """Close owner stdin, collect RELEASED + everything else, reap.

    Returns ``(returncode, stdout_all, stderr_all)``.  stdout_all
    includes the READY line already consumed.
    """
    proc.stdin.close()
    try:
        released = _read_line(proc, buf, timeout)
    except TimeoutError:
        released = "<TIMEOUT>"
    tail, err = _drain(proc, buf, timeout)
    rc = proc.wait(timeout=timeout)
    return rc, f"READY\n{released}\n{tail}", err


def _run_contender_child(root: Path, timeout: float) -> subprocess.CompletedProcess:
    """Run a contender child synchronously (bounded by *timeout*)."""
    return subprocess.run(
        _child_args("contender", str(root), _OPERATION),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _scenario_e2_same_root(root1: Path, timeout: float) -> tuple[bool, str]:
    """E2: owner holds root1 -> contender BUSY -> release -> acquire."""
    owner = _spawn_owner(root1)
    buf: list[bytes] = [b""]
    try:
        ready = _read_line(owner, buf, timeout)
        if ready != "READY":
            return False, f"owner did not print READY: {ready!r}"

        held = _run_contender_child(root1, timeout)
        if held.returncode != 1 or "BUSY" not in held.stdout:
            return (
                False,
                "contender while owner holds root1: expected BUSY + exit 1, "
                f"got exit {held.returncode} stdout={held.stdout!r} "
                f"stderr={held.stderr!r}",
            )

        rc, out, err = _finish_owner(owner, buf, timeout)
        if rc != 0 or "RELEASED" not in out:
            return (
                False,
                "owner after stdin close: expected RELEASED + exit 0, "
                f"got exit {rc} stdout={out!r} stderr={err!r}",
            )

        free = _run_contender_child(root1, timeout)
        if free.returncode != 0 or "ACQUIRED" not in free.stdout or "RELEASED" not in free.stdout:
            return (
                False,
                "contender after release: expected ACQUIRED+RELEASED + exit 0, "
                f"got exit {free.returncode} stdout={free.stdout!r} "
                f"stderr={free.stderr!r}",
            )
        return True, ""
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=timeout)
        else:
            owner.wait(timeout=timeout)


def _scenario_e3_crash_release(root1: Path, timeout: float) -> tuple[bool, str]:
    """E3: SIGKILL the owner; the kernel must release the flock.

    The stale lock file stays on disk — file existence is never
    authority (only the live OS lock is), and nobody deletes it.
    """
    owner = _spawn_owner(root1)
    buf: list[bytes] = [b""]
    try:
        ready = _read_line(owner, buf, timeout)
        if ready != "READY":
            return False, f"owner did not print READY: {ready!r}"

        held = _run_contender_child(root1, timeout)
        if held.returncode != 1 or "BUSY" not in held.stdout:
            return (
                False,
                "contender while owner holds root1: expected BUSY + exit 1, "
                f"got exit {held.returncode} stdout={held.stdout!r} "
                f"stderr={held.stderr!r}",
            )

        owner.kill()  # SIGKILL — no lease-release code may run
        owner.wait(timeout=timeout)
        _drain(owner, buf, timeout)

        after_crash = _run_contender_child(root1, timeout)
        if (
            after_crash.returncode != 0
            or "ACQUIRED" not in after_crash.stdout
            or "RELEASED" not in after_crash.stdout
        ):
            return (
                False,
                "contender after SIGKILL: expected ACQUIRED+RELEASED + exit 0, "
                f"got exit {after_crash.returncode} "
                f"stdout={after_crash.stdout!r} stderr={after_crash.stderr!r}",
            )

        lock_path = RuntimeMutationLock(root1).lock_path()
        if not lock_path.exists():
            return False, f"lock file missing after crash: {lock_path}"
        return True, ""
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=timeout)
        else:
            owner.wait(timeout=timeout)


def _scenario_e4_different_roots(
    root1: Path, root2: Path, timeout: float
) -> tuple[bool, str]:
    """E4: root1 held does not block acquisition of root2."""
    owner = _spawn_owner(root1)
    buf: list[bytes] = [b""]
    try:
        ready = _read_line(owner, buf, timeout)
        if ready != "READY":
            return False, f"owner did not print READY: {ready!r}"

        other = _run_contender_child(root2, timeout)
        if (
            other.returncode != 0
            or "ACQUIRED" not in other.stdout
            or "RELEASED" not in other.stdout
        ):
            return (
                False,
                "contender on root2 while root1 held: expected "
                f"ACQUIRED+RELEASED + exit 0, got exit {other.returncode} "
                f"stdout={other.stdout!r} stderr={other.stderr!r}",
            )

        rc, out, err = _finish_owner(owner, buf, timeout)
        if rc != 0 or "RELEASED" not in out:
            return (
                False,
                "owner after stdin close: expected RELEASED + exit 0, "
                f"got exit {rc} stdout={out!r} stderr={err!r}",
            )
        return True, ""
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=timeout)
        else:
            owner.wait(timeout=timeout)


def _drive() -> int:
    """Run all scenarios; print PASS/FAIL lines + one verdict; exit 0 iff all pass."""
    timeout = _timeout()
    tmp_roots: list[str] = []
    results: list[tuple[str, bool, str]] = []
    try:
        root1 = Path(tempfile.mkdtemp(prefix="zealfie-posix-witness-")) / "runtime"
        root2 = Path(tempfile.mkdtemp(prefix="zealfie-posix-witness-")) / "runtime"
        tmp_roots.extend([str(root1.parent), str(root2.parent)])

        for label, runner, args in (
            ("E2_SAME_ROOT_EXCLUSION", _scenario_e2_same_root, (root1,)),
            ("E3_CRASH_RELEASE", _scenario_e3_crash_release, (root1,)),
            ("E4_DIFFERENT_ROOT_INDEPENDENCE", _scenario_e4_different_roots, (root1, root2)),
        ):
            try:
                ok, detail = runner(*args, timeout)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((label, ok, detail))
            if ok:
                print(f"{label} PASS", flush=True)
            else:
                print(f"{label} FAIL: {detail}", flush=True)

        all_pass = all(ok for _, ok, _ in results)
        print(
            f"VERDICT: {'PASS' if all_pass else 'FAIL'} "
            f"({sum(1 for _, ok, _ in results if ok)}/{len(results)} scenarios)",
            flush=True,
        )
        return 0 if all_pass else 1
    finally:
        for tmp in tmp_roots:
            shutil.rmtree(tmp, ignore_errors=True)


def _usage() -> int:
    print(
        "usage: posix_lock_ci_witness.py owner <root> <operation>\n"
        "       posix_lock_ci_witness.py contender <root> <operation>\n"
        "       posix_lock_ci_witness.py drive",
        file=sys.stderr,
    )
    return 64


def main() -> int:
    if os.name != "posix":
        print("UNSUPPORTED_PLATFORM", flush=True)
        return 3
    argv = sys.argv[1:]
    if argv and argv[0] == "drive":
        return _drive()
    if len(argv) == 3 and argv[0] == "owner":
        return _run_owner(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "contender":
        return _run_contender(argv[1], argv[2])
    return _usage()


if __name__ == "__main__":
    sys.exit(main())
