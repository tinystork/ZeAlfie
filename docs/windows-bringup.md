# Windows Bring-Up (ZA-M1-2L.W)

Status: **closed** — merged to `main` (see final status table).
W1 (mutation lock primitive), W2 (fresh CPU chain) and W3 (GPU compute)
are all **real-witness PASS** on a genuine Windows machine
(2026-08-17).  macOS core runtime / POSIX lock execution is a **real
GitHub-hosted Darwin witness PASS** (ARM64 + Intel, 2026-08-17,
workflow run 32035282141) — see `docs/mutation-lock.md`.  macOS GUI,
packaging, notarization, Metal/GPU and end-to-end product usage remain
**HUMAN GATES**.

## Real Windows witness evidence (2026-08-17)

Machine: Windows, Python 3.13, NVIDIA GeForce RTX 3070 Laptop GPU,
driver 576.80.  Fresh workflow: clone feature branch → dedicated `.venv`
→ `pip install -e .` → ZeAlfie GUI → install ZeSolver → launch ZeSolver
→ install ZeMosaic → launch ZeMosaic → configure GPU runtime → run
ZeMosaic GPU workload → run ZeSolver GPU workload.

### W2 — fresh Windows CPU chain: PASS (real witness)

| Step | Result |
|---|---|
| Fresh Windows ZeAlfie install | PASS |
| ZeAlfie GUI | PASS |
| shared runtime creation | PASS |
| ZeSolver install | PASS |
| ZeSolver launch | PASS |
| ZeMosaic install | PASS |
| ZeMosaic launch | PASS |
| ZeSolver + ZeMosaic coexistence | PASS |

### W1 — Windows mutation lock: PASS (real witness, 2026-08-17)

Two-process witness on one Windows host, same runtime root:

| Property | Result |
|---|---|
| same root: owner READY, contender BUSY | PASS — real inter-process exclusion |
| crash release: owner force-killed, contender ACQUIRED / RELEASED without lock-file deletion | PASS — OS owns the lock lifetime, no manual cleanup |
| different root: root1 held, root2 ACQUIRED / RELEASED concurrently | PASS — lock scoping per runtime root |
| case normalization: `C:\temp\zealfie-root` held, `C:\TEMP\ZEALFIE-ROOT` → BUSY | PASS — Windows case-insensitive root identity |

Any other output (`LOCKERROR`, a traceback, or a BUSY in the last step
of any variant) would have meant witness FAILED.

### W3 — Windows GPU: PASS (real witness)

| Step | Result |
|---|---|
| GPU | NVIDIA GeForce RTX 3070 Laptop GPU |
| driver | 576.80 |
| Windows NVIDIA discovery | PASS (after `nounits` hotfix, commit 24026d9) |
| win_amd64 immutable closure acquisition | PASS |
| CuPy compute | PASS |
| NVRTC RawKernel | PASS |
| accelerated runtime activation | PASS (after UTF-8 probe hotfix, 24026d9) |
| ZeMosaic real GPU workload | PASS |
| ZeSolver requested / selected / used | auto / cuda / cuda (device 0) |
| ZeSolver batch | cuda_images=66, cpu_images=0, fallbacks=0, gpu_errors=0, gpu_oom=0 |
| VRAM peak | ~1.14 GB |
| terminal | completed |

The witness proved **real compute**, not merely detection.

### Defects found and closed by the real witness

* **W-BUG-01** — invalid nvidia-smi format option (`nouuid` →
  `nounits`).  Fixed in 24026d9; regression test
  `test_windows_smi_invocation_uses_nounits_format_argv`
  (`tests/test_host_capabilities.py`) pins the exact argv.
* **W-BUG-02** — compute probe source encoded with the Windows locale
  (CP1252) because the parent subprocess used text mode without an
  explicit encoding, while the child interpreter expects UTF-8.  Fixed
  in 24026d9 (`encoding="utf-8"`); regression tests
  `test_backend_compute_probe_unicode_transport_positive_control` and
  `test_backend_compute_probe_unicode_survives_windows_locale_parent`
  (`tests/test_accelerated_deployment_engine.py`) reproduce the bug
  class behaviourally.
* **W-UX-01** — foreground console flashes from technical helper
  subprocesses.  Fixed by `zealfie.common.subprocess_platform`
  (`technical_subprocess_platform_kwargs()` → `CREATE_NO_WINDOW` on
  Windows) applied to technical helper sites only; product application
  launches are untouched (regression tests in
  `tests/test_technical_subprocess_platform.py`).  Known limitation:
  stdlib venv/ensurepip spawns its own internal subprocess without
  creationflags.
* **W-TEXT-01** — user-visible CUDA prerequisite text claimed
  "Linux x86_64" during Windows flows.  Fixed in
  `src/zealfie/acceleration/compatibility.py` (platform-neutral
  wording; single curated driver floor unchanged).


## Platform support matrix (runtime mutation lock)

| Platform | Primitive | Status |
|---|---|---|
| Linux (POSIX) | `fcntl.flock(LOCK_EX \| LOCK_NB)` | implemented, **witness-proven on Linux** |
| macOS (POSIX) | `fcntl.flock(LOCK_EX \| LOCK_NB)` (same primitive) | implemented; **real witness PASS — GitHub-hosted Darwin (ARM64 + Intel, 2026-08-17)** — core runtime / POSIX lock only |
| Windows (`os.name == "nt"`) | `msvcrt.locking(LK_NBLCK)` byte-range lock on `[0,1)` | implemented; **real witness PASS (W1, 2026-08-17)** |
| any other platform | no backend | fail closed (`RuntimeMutationLockError`) |

Windows semantics (implementation-level, **confirmed by real witness W1**):

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

> **Update (2026-08-17, real witness):** W3 executed on a genuine Windows
> machine (RTX 3070 Laptop, driver 576.80) and **PASSED** — see the
> "Real Windows witness evidence" section at the top of this document.
> The GPU witness proved real compute, not merely detection.

## Witness instructions (historical — W1/W2/W3 executed and PASSED, 2026-08-17)

The instructions below are kept verbatim as the reproducible protocol
used for the real Windows witnesses.  W1/W2/W3 are now **closed**; real
macOS execution is witness-proven for the core runtime and POSIX lock
(GitHub-hosted Darwin, 2026-08-17).  Remaining macOS gates are
product-level (GUI, packaging, notarization, Metal/GPU).

### W1 — Windows mutation lock (executed → PASS)

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

Crash variant (mandatory, proves OS-owned lock lifetime):

    # Console A: owner acquires and prints READY
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root owner runtime-apply

    # Console B: contender must print BUSY immediately
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root contender runtime-apply

    # Force-kill A (Ctrl+C does NOT apply here: the witness holds on
    # stdin — use Task Manager, or `taskkill /F /PID <pid-of-A>` from a
    # third console).  Do NOT delete the lock file.

    # Console B again: must print ACQUIRED then RELEASED immediately
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root contender runtime-apply

Expected after the force-kill: B acquires immediately with **no manual
deletion** of `C:\temp\.zealfie-root.zealfie-mutation.lock` (the stale
file may remain on disk — FILE EXISTS != LOCK HELD).  Any LOCKERROR,
traceback, or BUSY in the last step = witness FAILED.

Different-root variant (inexpensive, proves lock scoping):

    # Console A holds the lease on C:\temp\zealfie-root
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root owner runtime-apply

    # Console B uses a DIFFERENT root: must print ACQUIRED + RELEASED
    # immediately (different runtime roots never contend)
    python tests/witness/windows_lock_witness.py C:\temp\zealfie-root-2 contender runtime-apply

Expected: concurrent acquisition succeeds for a different root while the
first root is still held.

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
on Windows — witness-proven by W1). No in-place upgrade; previous known-good
runtime preserved; rollback path unchanged.

## Implementation vs witness status

| Item | Status |
|---|---|
| Windows mutation lock backend | IMPLEMENTED (tests: synthetic decision logic) |
| Real Windows mutation witness | **PASS (real witness, 2026-08-17)** — W1 closed |
| Windows-aware host GPU probing | IMPLEMENTED (tests: injected fakes) |
| Real Windows GPU detection | **PASS (real witness, 2026-08-17)** |
| win_amd64 artifact closure | IMPLEMENTED (PyPI + byte-verified hashes) |
| Real Windows GPU compute gate | **PASS (real witness, 2026-08-17)** |
| Fresh Windows CPU chain | **PASS (real witness, 2026-08-17)** |
| Windows technical-subprocess console UX | FIXED (CREATE_NO_WINDOW helper; real-window verification = follow-up) |
| Windows nvidia-smi format + probe encoding | FIXED (24026d9) + regression tests |
| macOS readiness | **PASS — real GitHub-hosted macOS runners** (`macos-15` arm64 + `macos-15-intel` x86_64, 2026-08-17, run 32035282141): install / import / CLI, host target, runtime path, acceleration fail-closed, POSIX lock witness E2/E3/E4, targeted pytest. GUI / packaging / notarization / Metal-GPU / end-to-end = **HUMAN GATE** |
| Diagnostic log discoverability | FOLLOW-UP (no persistent log file today — loggers write to stderr; a logging subsystem is out of scope for this mission) |

## M1-4 / M1-4.1 Windows HUMAN_GATE — real witness (2026-08-19, PASS)

Real Windows validation of the M1-4 (hardening/UX/lifecycle) and M1-4.1
(Windows self-update activator) work:

| Area | Result |
|---|---|
| GC transient N-1 (rollback retention) | **PASS** — previous slot released only after fresh-startup health confirmation; ~3.25 GB reclaimed |
| GPU preserved after GC | **PASS** — accelerated closure + cached GPU artifacts survived slot cleanup |
| i18n FR/EN persistence | **PASS** — language switch + persisted preference across restart |
| VPN / GitHub / PyPI downloads | **PASS** — proxy-aware path, reason-code diagnostics, no credential leak |
| Windows self-update 0.0.6 → 0.0.7b1 | **PASS** — stage + apply handoff; installed version verified == target |
| corrupted-wheel fail-closed | **PASS** — byte-altered staged wheel refused (SHA-256), marker preserved, install untouched |
| recovery 0.0.7b1 → 0.0.7b2 | **PASS** — second staged/apply cycle succeeded |

The `msvcrt` mutation-lock backend and the Windows `ctypes` helper wait remain
hermetically covered (injected seams) in `tests/test_mutation_lock.py` and
`tests/test_selfupdate.py`.

## M1-4.2 Windows HUMAN_GATE — GUI self-update real witness (2026-08-19, PASS)

Full GUI-driven self-update from a packaged **0.0.6** install (CORR-3
validation, launcher shebang preservation):

| Step | Result |
|---|---|
| Packaged 0.0.6 installed | **PASS** |
| GUI automatic stable check (non-blocking) | **PASS** |
| Automatic staging of v0.0.7 (background) | **PASS** |
| User consent ("Update and restart") | **PASS** |
| Windows helper handoff | **PASS** |
| Automatic GUI restart | **PASS** |
| Installed version 0.0.7 (verified) | **PASS** |
| Pending marker cleared | **PASS** |
| Stable channel UP_TO_DATE | **PASS** |
| Launchers preserved: `zealfie.exe → python.exe`, `zealfie-gui.exe → pythonw.exe` | **PASS** |
| No `pythonww.exe` regression | **PASS** |

Root cause fixed (CORR-3): the GUI-initiated update used `sys.executable`
(`pythonw.exe`) for the helper and pip, so distlib regenerated launchers with
windowed shebangs (`zealfie.exe → pythonw.exe`, `zealfie-gui.exe →
pythonww.exe`) — `zealfie.exe --version` printed nothing (exit 0). The
install interpreter is now resolved to the same-venv console sibling
(`...\Scripts\python.exe`) via `zealfie/selfupdate/interpreter.py`, fail-closed
when the sibling cannot be proven. Hermetic coverage:
`tests/test_self_update_interpreter.py` (17 tests).
