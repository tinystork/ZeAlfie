# Test / witness scratch ownership and cleanup

> Mission `ZA-TMPFS-CLEANUP-GATE`. This document describes which temporary
> directories the **development and test tooling** owns, how they are cleaned
> up, and which artefacts are deliberately retained. It is about tooling
> hygiene only; it changes no production behaviour.

## Why this exists

The host `/tmp` is a tmpfs of about **3.8 GiB**. A full test run with the
default pytest `basetemp` under `/tmp` fills it (ENOSPC). Separately, a few
test/packaging helpers allocated `mkdtemp` scratch that was never removed on
success and was not placed under the caller's work directory. Both classes of
leak are covered here.

## Ownership rules

1. **The caller owns any supplied work directory.** Helpers receiving a
   `--work-root` / `work` directory must preserve it and any pre-existing
   files inside it.
2. **Scratch belongs under the supplied work directory**, or otherwise in
   the test driver's owned temporary scope. Never place it under the user's
   real runtime (`%LOCALAPPDATA%\zealfie\runtime`,
   `~/Library/Application Support/zealfie/runtime`) or its production artifact
   cache. A dedicated development scratch root on disk is separate from those
   production stores.
3. **Scratch is automatically removed on success.**
4. **On failure**, tooling may either clean up too, or deliberately retain the
   scratch **only** when it prints the exact retained path so a human can
   inspect it. Silent retention is not allowed.
5. **Allocations are registered immediately after creation** so an earlier
   allocation is still cleaned up if a *later* allocation fails.

## What is scratch vs what is deliberately kept

| Kind | Example | Owned by | On success |
|---|---|---|---|
| Isolated GUI runtime | `packaging/{windows,macos}/gui_smoke_offscreen.py` throwaway `SharedRuntime` root | the smoke, under `--work-root` | removed |
| Witness scratch | `packaging/macos/witnesses.py` Qt smoke root, child venv + driver, relocation copy, PATH shims, broken negative-control app | the witness, under `--work` | removed |
| Lock witness scratch | `tests/witness/posix_lock_ci_witness.py` two throwaway runtime roots | the witness | removed |
| pytest tmp | `tmp_path` / `tmp_path_factory` artifacts | pytest | removed (see policy below) |
| Build deliverables | `build_app.py --work` `ZeAlfie.app`, downloaded substrate, wheelhouse caches | the build caller | **kept** — explicit build output, not scratch |
| pytest failure artifacts | the last failed run's tmp dir | pytest (policy `failed`) | retained for diagnosis |
| Named diagnostic logs / review bundles | `AGENT/logs/...`, `AGENT/review/...` | humans | **kept** |

The macOS build outputs (`ZeAlfie.app`, downloaded private-Python substrate,
acquired wheelhouse) are **not** auto-delete targets: they are explicit,
caller-requested build deliverables. Do not confuse them with the disposable
witness copies, which are throwaway scratch copies of an input bundle.

## pytest retention (unchanged)

```ini
tmp_path_retention_count = 1
tmp_path_retention_policy = failed
```

Successful per-test directories are cleaned up automatically; only the last
**failed** run's artefacts are kept. For heavy runs, use a disk-backed
`basetemp` (see `docs/testing.md` and `AGENT/run_pytest_disk_tmp.sh`) — never
the default `/tmp` basetemp for a FULL run.

## Mission evidence (CASE 1) and limits

`ZA-TMPFS-CLEANUP-GATE` established **CASE 1**: the **unchanged** real updater,
run on the real ~3.8 GiB tmpfs, completed a real ZeAnalyser 3.3.2 → 3.4.0
shared-runtime update (GitHub fetch/build + real pip acquisition/install +
slot validation/activation), preserved ZeMosaic 4.7.0, ZSSS 8.4.0,
ZeSolver 1.2.0 and a dependency count of 97 including CUDA closures,
and passed activation/rollback probes. The production correction path is
therefore closed; this mission only fixes the dev/test scratch hygiene above.

Honest limits of that witness (do not over-read it):

- The tmpfs was **not** empty at start (about 802 MB already used by unrelated
  state that was deliberately not purged); the witness passed with *less*
  free space than a clean 3.8 GiB tmpfs, so it is a strong-but-not-exact
  capacity statement.
- Peak usage was **sampled** at 100 ms, not an exact byte-level high-water
  mark.
- The global pytest matrix was **not** a green full suite: a targeted 20-file
  contracts matrix passed 678/679 (1 skip); the integration file has one
  unrelated stale-version assertion (`0.0.6` vs actual `0.1.1`) that is left
  untouched by design.
- The `tests/test_macos_packaging.py::test_windows_packaging_untouched` guard now
  protects only the **Windows production packaging surface** (option 1, decided by
  the maintainer). Dev/test-only smoke/witness files are excluded; the guard is
  fail-closed on any unknown or unlisted path. See "Windows production surface"
  below.
- Exact provenance of historical `zealfie-*` scratch directory names that were
  already gone at mission start remains an **unresolved evidence gap**; this
  tooling cleanup is not claimed to be the creator of those names.

## Windows production surface (authoritative)

`packaging/windows` is split into a guarded production surface and an explicit
dev/test-only set. The guard
(`test_windows_packaging_untouched`) fails on any change to a production file
**or** any unlisted/unknown file (fail-closed), and allows only the exact
dev/test-only paths below. This list is the single source of truth — do not
reinterpret the guard's intent.

**Production surface (guarded — changes MUST fail the guard):**

- `installer/zealfie.iss` — Inno Setup script compiled into the shipped Setup.exe
- `installer/innosetup.toml` — Inno Setup toolchain pin (SHA-256 verified at build)
- `provision.py` — pure provisioning logic; staged and executed during end-user install
- `provision_windows.py` — bootstrap entrypoint; staged and executed during end-user install
- `reproducibility.toml` — pinned CPython substrate (verified fail-closed at build)
- `wheelhouse.lock.toml` — exact offline dependency lock bundled into the installer
- `wheelhouse_lock.py` — owns the exact dependency-lock contract
- `acquire_wheelhouse.py` — deterministic wheelhouse acquisition feeding the installer
- `licenses/CPython-PSF-LICENSE.txt` — shipped legal artifact
- `licenses/README.md` — shipped legal artifact

**Dev/test-only (excluded from the guard):**

- `gui_smoke_offscreen.py` — offscreen GUI smoke; staged as bootstrap asset but executed only by CI `smoke-gui`
- `installer_smoke.py` — installed-layout install/provenance smoke; staged but executed only by CI post-install verification
- `side_effect_witness.py` — CI-only side-effect delta audit witness
- `witness_runtime.py` — CI-only runtime child capability witness

The end-user install bootstrap (`ssPostInstall`) runs only
`provision_windows.py make-appenv` plus file-presence gates — never the smoke or
witness scripts — so the four dev/test files do not determine build correctness
or install-time runtime behaviour.

## Focused checks for this cleanup

```bash
.venv/bin/python -m pytest tests/test_tmpfs_cleanup_gate.py -q
```

The file runs the two GUI smoke scripts **offscreen on Linux** (real
`PySide6`, isolated `HOME`/`TMPDIR`), asserting PASS, no leftover owned
scratch under the caller work directory, and preservation of a pre-existing
caller file — plus the exceptional (failure) lifecycle and the
allocation-registration ordering. The macOS witness functions are exercised
with their native operations controlled; that is **not** a native macOS
witness run and proves nothing about a real `.app` bundle on macOS.
