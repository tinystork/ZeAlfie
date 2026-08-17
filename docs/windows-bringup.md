# Windows Bring-Up (ZA-M1-2L.W)

Status: implementation complete on `feature/m1-2l-runtime-mutation-lock`;
**real-Windows validation is a HUMAN GATE** — nothing in this document
claims Windows is validated or production-ready.

## Platform support matrix (runtime mutation lock)

| Platform | Primitive | Status |
|---|---|---|
| Linux / macOS (POSIX) | `fcntl.flock(LOCK_EX \| LOCK_NB)` | implemented, witness-proven |
| Windows (`os.name == "nt"`) | `msvcrt.locking(LK_NBLCK)` byte-range lock on `[0,1)` | implemented; **real witness = HUMAN GATE (W1)** |
| any other platform | no backend | fail closed (`RuntimeMutationLockError`) |

Windows semantics (implementation-level, to be confirmed by witness W1):

* exclusive byte-range lock, byte `[0,1)`, deterministic range;
* non-blocking, fail-fast: contention (`ERROR_LOCK_VIOLATION`, winerror 33)
  → `RuntimeMutationBusyError`; other primitive failures → fail closed
  `RuntimeMutationLockError`;
* the lock file is padded to ≥1 byte at first creation (range backing);
  contents never authority — FILE EXISTS != LOCK HELD;
* OS-owned lifetime: released on process death or explicit `LK_UNLCK`;
  no manual cleanup of stale files ever needed;
* same-process different-handle conflicts natively → different thread
  = BUSY without extra local state; nested same-context acquire = reuse
  (ContextVar stack, unchanged contract);
* diagnostic `<lock>.owner.json` sidecar: best-effort, never authority;
* root identity: resolved + `os.path.normcase` (case-insensitive FS).

## Host GPU detection (Windows)

On Windows, `nvidia-smi` is the only evidence channel used. POSIX-only
probes (`/proc/driver/nvidia/version`, `/dev/nvidiactl`, sysfs PCI,
`lspci`) are not run and never produce negative evidence there.

* `nvidia-smi` success → GPU(s) + driver detected (model + version).
* `nvidia-smi` malformed / errored / absent → **UNKNOWN** (never
  "driver absent", never "no hardware"); recommendation stays UNKNOWN,
  fail-closed for any GPU path that needs evidence.

## Windows accelerated artifact closure (NVIDIA_CUDA)

Implemented and recorded in `manifests/accelerated_artifacts.toml`:
`win_amd64` rows for the same 10 distributions and the same exact
versions as the proven Linux closure (no version changes, no dependency
changes; cp313 for cupy-cuda12x, py3 for the nvidia-*-cu12 packages).

* All URLs/sizes/SHA256 come from the PyPI JSON API and were
  byte-verified by downloading every wheel on 2026-08-17
  (`sha256sum -c`, all OK, total 1,204,421,859 bytes).
* `cuda-pathfinder-1.6.0-py3-none-any.whl` is the same immutable bytes
  on both platforms.
* The Linux closure is untouched: same entries, same hashes.
* The compute gate (CuPy import, device count, arange/sum, NVRTC
  RawKernel, synchronize, result check) is unchanged for Windows — no
  weakening, no `import cupy`-only shortcut.
* Real Windows GPU compute = **HUMAN GATE (W3)**.

## Human gates — copy/paste-ready witness instructions

### W1 — Windows mutation lock

Two consoles on the same real Windows machine, same runtime root:

    # From a ZeAlfie repo checkout on the real Windows machine, Console A (owner)
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root owner runtime-apply

    # after READY is printed, Console B (contender)
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root contender runtime-apply

Expected: B prints `BUSY` immediately (no wait, no mutation). Close A's
stdin (Ctrl+D / Ctrl+Z+Enter) or kill the process. Run B again:

    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root contender runtime-apply

Expected: `ACQUIRED` then `RELEASED` immediately — no manual deletion of
any lock file. Capture: `BUSY`, the stale lock file presence, and the
re-acquisition. Any `LOCKERROR`, traceback, or a BUSY in the last step =
witness FAILED.

### W2 — fresh Windows CPU chain

Fresh Windows → install ZeAlfie → start ZeAlfie GUI → create runtime →
install ZeSolver → launch ZeSolver in situ → functional ZeSolver smoke →
install ZeMosaic → verify ZeSolver remains healthy → launch ZeMosaic →
functional ZeMosaic smoke. CPU-only pass first to isolate packaging /
runtime failures from GPU issues.

### W3 — Windows GPU (only after W2 green)

Collect GPU capability → NVIDIA GPU + driver detected; `gpu-plan` →
NVIDIA_CUDA candidate; install accelerated runtime → acquire exact
win_amd64 artifacts → verify sizes/SHA256 against the manifest → install
candidate → real CuPy compute gate → NVRTC RawKernel → activate only
after PASS → launch/use ZeMosaic → confirm GPU path operational.

Capture: Windows version, Python version, GPU model, NVIDIA driver
version, platform tag (`win_amd64`), artifact identities, candidate
runtime id, previous runtime id, compute-gate result, ZeMosaic result.

## Transaction semantics

The accelerated install keeps the M1-2L transaction ownership:
`gpu-plan` → hardware re-check → base preparation → artifact acquisition
→ dependency install → compute gate → activation → accelerated metadata
write, all under one `OPERATION_GPU_INSTALL` mutation lease (identical
on Windows once W1 passes). No in-place upgrade; previous known-good
runtime preserved; rollback path unchanged.

## Implementation vs witness status

| Item | Status |
|---|---|
| Windows mutation lock backend | IMPLEMENTED (tests: synthetic decision logic) |
| Real Windows mutation witness | HUMAN GATE (W1) |
| Windows-aware host GPU probing | IMPLEMENTED (tests: injected fakes) |
| Real Windows GPU detection | HUMAN GATE (W3) |
| win_amd64 artifact closure | IMPLEMENTED (PyPI + byte-verified hashes) |
| Real Windows GPU compute gate | HUMAN GATE (W3) |
| Fresh Windows CPU chain | HUMAN GATE (W2) |
