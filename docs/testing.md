# Test Running Guide

## Quick Reference

| Tier | Command | Speed |
|------|---------|-------|
| **FAST** | `python -m pytest -q -m "not zealfie_slow and not integration"` | ~10s |
| **TARGETED** (integration only) | `python -m pytest -q -m "integration"` | ~80s |
| **TARGETED** (runtime/release) | `python -m pytest -q -m "zealfie_slow" tests/test_runtime_service.py tests/test_runtime_deployment.py tests/test_runtime_hardening.py` | ~260s |
| **FULL** | `python -m pytest -q` | ~580s (all 473 tests) |

## Marker Definitions

- **Unmarked (default)** → **FAST**: pure unit tests, no wheel building, no venv creation, no pip, no subprocess.
- **`zealfie_slow`**: tests that build wheels via `python -m build`, create venvs, run pip installs, or otherwise take >5s per file on a typical machine.
- **`integration`**: full end-to-end cycles: build, install, launch, upgrade, rollback. These are the most expensive tests.

## Marker Assignment

Markers are assigned at **file level** via `pytestmark = pytest.mark.<marker>` for files where nearly all tests are `zealfie_slow` or `integration`. Files with a mix of `zealfie_slow` and fast tests use individual `@pytest.mark.zealfie_slow` decorations.

### Files marked `zealfie_slow` at module level (80 tests)

| File | Tests | Reason |
|------|-------|--------|
| `test_wheel_building.py` | 28 | Builds real wheels via `python -m build` |
| `test_runtime_service.py` | 24 | Session fixtures build witness wheels; tests use SharedRuntime |
| `test_runtime_deployment.py` | 14 | Builds wheels + creates venvs + pip installs |
| `test_runtime_manager.py` | 14 | Uses SharedRuntime (venv creation) — dominantly slow |

### Files with individual `@pytest.mark.zealfie_slow` (39 zealfie_slow / 266 fast)

| File | Total | zealfie_slow | Fast | Rationale |
|------|-------|------|------|-----------|
| `test_cli.py` | 38 | 6 | 32 | Only E2E witness-cycle tests and real venv-creating runtime tests are zealfie_slow; pure formatting/fake-service/rollback tests are fast |
| `test_runtime_hardening.py` | 25 | 16 | 9 | Only tests using `witness_wheel` + `rt.create()` are zealfie_slow; pure `slot_path` validation and canonical state checks are fast |
| `test_runtime_6b.py` | 14 | 6 | 8 | Only TOCTOU/discard tests using witness fixtures and `rt.create()` are zealfie_slow; slot validation and canonical JSON tests are fast |
| `test_runtime_6b1.py` | 11 | 3 | 8 | Only tests using witness fixtures or `rt.create()` are zealfie_slow; non-object JSON root tests and rollback-on-absent are fast |
| `test_runtime_6b2.py` | 10 | 1 | 9 | Only the transaction test using witness fixtures is zealfie_slow; pure `save_active_state` and `load_active_state` tests are fast |
| `test_runtime_status.py` | 10 | 5 | 5 | Tests calling `rt.create()` or `venv.create()` are zealfie_slow; BROKEN/ABSENT state reads without real venvs are fast |
| `test_releases.py` | 99 | 2 | 97 | Only 2 tests create real venvs |

### Files marked `integration` (9 tests)

| File | Tests | Reason |
|------|-------|--------|
| `test_runtime_transaction.py` | 6 | Full upgrade + rollback cycle |
| `test_runtime_witness_cycle.py` | 2 | Full slot lifecycle |
| `test_witness_install_launch.py` | 1 | Build, install, detect, launch |

## Shared Fixtures

Witness component wheels (`witness_component`, `witness_component_v2`, `witness_second`) are built **once per session** by shared session-scoped fixtures in `tests/conftest.py`. Tests that need to mutate/copy artifacts must copy into `tmp_path` first.

CLI test fixtures (`witness_wheel_cli`, `witness_v2_wheel_cli`, `witness2_wheel_cli`) are aliases to the same shared wheels, defined in `tests/conftest.py` to avoid duplicate session-scoped builds.

Previously, 13 separate `build_wheel()` calls were made across 7 test files for the same 3 wheels.

## Notes

- Test selection uses only standard pytest marker expressions; no external pytest plugin options are required.
- If disk space on `/tmp` is limited (<1G free), run individual heavy files separately to avoid ENOSPC errors from accumulated venv copies.
- Remaining module-level `zealfie_slow` files (`test_wheel_building.py`, `test_runtime_service.py`, `test_runtime_deployment.py`, `test_runtime_manager.py`) are dominantly slow; individual marking would not yield meaningful FAST coverage gains.
