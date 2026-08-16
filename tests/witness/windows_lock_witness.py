"""HUMAN_GATE: run on real Windows only — never executed by the test suite.

Witnesses the REAL msvcrt byte-range lock primitive (no fakes).  It
exists so a human can validate the Windows backend on a real Windows
host; the synthetic tests on Linux never claim that validation.

Usage (two processes on one Windows host, same <root>):

    python tests/witness/windows_lock_witness.py <root> owner <operation>
    python tests/witness/windows_lock_witness.py <root> contender <operation>

Protocol:
  1. Start the owner; wait for its READY line (it now holds the lease,
     blocked on stdin).
  2. Run the contender: it must print BUSY (the OS lock is held).
  3. Close the owner's stdin (or kill the process) — the OS releases
     the byte-range lock on process death.
  4. Run the contender again: it must print ACQUIRED then RELEASED
     immediately.

Any other output (LOCKERROR, a traceback, or a BUSY in step 4) means
the witness FAILED.
"""
from __future__ import annotations

import sys

from zealfie.runtime.mutation_lock import (
    RuntimeMutationBusyError,
    RuntimeMutationLock,
    RuntimeMutationLockError,
)


def main() -> int:
    root = sys.argv[1]
    role = sys.argv[2]
    operation = sys.argv[3]
    lock = RuntimeMutationLock(root)
    if role == "owner":
        lease = lock.acquire(operation)
        print("READY", flush=True)
        sys.stdin.read()  # hold the lease until the witness driver closes stdin
        lease.release()
        print("RELEASED", flush=True)
        return 0
    try:
        lease = lock.acquire(operation)
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


if __name__ == "__main__":
    sys.exit(main())
