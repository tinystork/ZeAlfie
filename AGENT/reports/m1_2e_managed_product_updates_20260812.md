M1-2E MANAGED PRODUCT UPDATES: COMPLETE

## Baseline

```text
initial HEAD: ef93f5c231d72e8dfafbfbd9c64b34ec50462d7c
initial branch: feature/m1-2d-selective-install
working branch: feature/m1-2e-managed-updates
push: non
```

Baseline audit command sequence required by mission was run:

```text
git status
git rev-parse HEAD
git log -12 --oneline
git branch -vv
```

Observed: HEAD matches `ef93f5c`. The worktree was not strictly clean because a pre-existing untracked `.smoke/` directory exists. It was inspected and preserved; it will not be added to commits or deleted.

## Lot status

- E.1 Installed Product Provenance: ACCEPTED and locally committed (`1bcf205`).
- E.2 Read-only Update Detection: ACCEPTED and locally committed (`4ffcdb7`).
- E.3 Non-blocking Startup Check: ACCEPTED and locally committed (`19f807f`).
- E.4 Product Shell Update UX: ACCEPTED and locally committed (`599f10d`).
- E.5 Transactional Update: ACCEPTED and locally committed (`98ddac1`).
- E.6a GUI Update Action: ACCEPTED and locally committed (`d2ff44d`).
- E.6 Real ZeSolver A→B witness: PASS.
- E.7 Documentation: completed.
- E.8 Global Nono review: ACCEPT_WITH_NOTES, no blockers.
- Final gates: targeted, FAST, and FULL passed.

## E.1 Installed Product Provenance

Status: ACCEPTED and locally committed.

Evidence:

```text
Junior targeted: git diff --check OK; py_compile OK; tests/test_provenance.py => 23 passed; related targeted FAST => 89 passed, 1 deselected.
Junior FAST: 1116 passed, 191 deselected, 5 warnings in 57.21s.
Nono review: ACCEPT_WITH_NOTES, no blockers, no major findings; commit authorized.
```

Implementation summary:

- Added pure Python runtime provenance store at `RuntimeLayout.state_dir / product-provenance.json`.
- Provenance is keyed by active slot id and product id.
- Persisted fields: product_id, version, source owner/repo, requested ref, resolved commit SHA, wheel SHA256.
- Provenance is written after successful runtime activation and after desired selection persistence.
- Apply failure or selection persistence failure leaves old provenance authoritative.
- Old runtime / missing/corrupt provenance reads as safe unknown (`None` / `{}`), never invented SHA.

Accepted notes carried to E.2/E.7:

- Provenance write failure after activation is logged and swallowed; acceptable for E.1, but update status should distinguish missing provenance from normal non-managed/unknown state.
- `except Exception` around provenance persistence is broad; acceptable now, consider narrowing if future status needs sharper diagnostics.
- `apply_offline_deployment` does not record managed-source provenance; document as legacy/offline unknown unless later product requirements change.

Logs:

- `AGENT/logs/m1_2e_e1_review_targeted_20260812T201435.log`
- `AGENT/logs/m1_2e_e1_fast_20260812T201533.log`


## E.2 Read-only Update Detection

Status: ACCEPTED and locally committed.

Evidence:

```text
Junior targeted before Nono: git diff --check OK; py_compile OK; tests/test_updates.py => 12 passed; related provenance/update/source tests => 93 passed.
Junior FAST before Nono: 1128 passed, 191 deselected, 5 warnings.
Nono review: ACCEPT_WITH_NOTES; no blockers/majors.
Post-Nono hardening: moved RemoteSource construction inside the check boundary so invalid/corrupt source provenance returns CHECK_FAILED instead of raising; added regression test.
Junior targeted after fix: tests/test_updates.py => 13 passed; related provenance/update/source tests => 94 passed.
Junior FAST after fix: 1129 passed, 191 deselected, 5 warnings.
```

Implementation summary:

- Added pure Python/Qt-free `zealfie.app.updates`.
- Added `UpdateStatus`: NOT_CHECKED, CHECKING, UP_TO_DATE, UPDATE_AVAILABLE, CHECK_FAILED, PROVENANCE_UNKNOWN.
- Added immutable `ProductUpdateResult` with installed/latest commit fields and source/version/error context.
- Added read-only `check_product_update(product_id, provenance, resolver=...)` core; resolver injection is mandatory.
- Added `ZeAlfieService.check_product_update(...)` and `check_updates(...)` read-only service APIs.
- No runtime/provenance/selection/active pointer writes during update checks; tests compare persisted bytes.

Accepted notes carried to E.3:

- Field name `latest_commit_sha` really means resolved SHA for the requested ref. Keep stable unless a deliberate API rename is chosen before GUI hardening.
- Broad `except Exception` is deliberate at the check boundary to turn resolver/source validation failures into CHECK_FAILED.
- `check_updates(None)` checks all catalog products; products without provenance produce PROVENANCE_UNKNOWN.

Logs:

- `AGENT/logs/m1_2e_e2_review_targeted_20260812T202748.log`
- `AGENT/logs/m1_2e_e2_fast_20260812T202822.log`
- `AGENT/logs/m1_2e_e2_post_nono_fix_20260812T203049.log`
- `AGENT/logs/m1_2e_e2_fast_post_fix_20260812T203051.log`

## E.3 Non-blocking Startup Update Check State

Status: ACCEPTED and locally committed.

Evidence:

```text
Junior targeted before Nono: git diff --check OK; py_compile OK; tests/test_update_checks.py => 10 passed; related targeted FAST => 123 passed.
Junior FAST before Nono: 1139 passed, 191 deselected, 5 warnings.
Nono review: ACCEPT_WITH_NOTES; no blockers/majors.
Post-Nono hardening: shutdown of owned default executor now resets the pool reference so start-after-shutdown recreates a fresh executor instead of leaving a product stuck in CHECKING; added regression test.
Junior targeted after fix: tests/test_update_checks.py => 11 passed; related targeted FAST => 124 passed.
Junior FAST after fix: 1140 passed, 191 deselected, 5 warnings.
```

Implementation summary:

- Added pure Python/Qt-free `zealfie.app.update_checks`.
- Added `UpdateCheckCoordinator` and re-export from `zealfie.app`.
- Initial per-product state is `NOT_CHECKED`; starting checks transitions synchronously to `CHECKING`.
- `start(product_ids)` returns `Future` objects immediately via an injectable executor / lazy default `ThreadPoolExecutor`.
- Observer callbacks receive `CHECKING` and terminal results; observer exceptions are logged and swallowed.
- Per-product generation counters prevent stale older completions from overwriting newer state.
- Coordinator mutates only in-memory state and delegates actual read-only detection to an injected check function.

Accepted notes carried to E.4:

- Observer notifications are state-change hints; GUI wiring should re-read `state(product_id)` before painting if ordering matters.
- `shutdown(wait=True)` follows `ThreadPoolExecutor` semantics and may wait for a stuck resolver; GUI teardown can use `wait=False` where appropriate.
- Unexpected check exceptions are converted to `CHECK_FAILED`; log level may be reconsidered during GUI diagnostics hardening.

Logs:

- `AGENT/logs/m1_2e_e3_review_targeted_20260812T203928.log`
- `AGENT/logs/m1_2e_e3_fast_20260812T204009.log`
- `AGENT/logs/m1_2e_e3_post_nono_fix_20260812T204246.log`
- `AGENT/logs/m1_2e_e3_fast_post_fix_20260812T204302.log`

## E.4 — Product Shell Update UX

Status: ACCEPTED by Junior (Nono retry failed to return an exploitable verdict; Junior performed final review gate).
Commit: this commit.

Implemented read-only product-shell update UX:
- `ProductCard` now has a separate hidden-by-default update status label.
- `presentation.update_status_label()` maps update states to user-facing text:
  - `NOT_CHECKED` / `None`: hidden label;
  - `CHECKING`: “Checking for updates…”;
  - `UP_TO_DATE`: “Up to date”;
  - `UPDATE_AVAILABLE`: “Update available” with short latest SHA when present;
  - `CHECK_FAILED`: friendly compact error, no traceback/raw enum;
  - `PROVENANCE_UNKNOWN`: “Update status unknown”.
- `ZeAlfieMainWindow` can start read-only `UpdateCheckCoordinator` checks after initial refresh.
- Update result delivery is marshaled through `UpdateResultBridge` (`QObject` signal) before widget mutation.
- Close path shuts down update coordinator with `wait=False`.
- Production GUI wiring injects `service.check_product_update(..., resolver=resolver)` only where resolver exists.

Read-only invariant:
- No update/apply/install/launch action was added.
- No runtime/provenance/selection/active pointer mutation introduced by update checks.
- Existing install/launch labels and behavior preserved by tests.

Validation:
- `git diff --check`: pass.
- `py_compile` changed/new GUI/test files: pass.
- `tests/test_gui_update_status.py -q`: `19 passed`.
- Related targeted GUI/update FAST subset: `101 passed`.
- FAST: `1159 passed, 191 deselected, 5 warnings`.

Logs:
- `AGENT/logs/m1_2e_e4_review_targeted_20260812T2112.log`
- `AGENT/logs/m1_2e_e4_fast_20260812T2112.log`

Notes:
- First Nono E.4 audit stopped mid-control after targeted tests; retry did not return an exploitable visible verdict before orchestration recovery.


## E.5 — Transactional Update API

Status: ACCEPTED by Junior after Nono `ACCEPT_WITH_NOTES` review and locally committed.
Commit: this commit.

Implemented service-layer update API:
- Added `ProductUpdateNotApplicableError`, exported from `zealfie.app`.
- Added `ZeAlfieService.update_product(product_id, *, resolver, fetcher, work_root, dependency_wheelhouse=None, probe_distribution=None, progress_callback=None) -> DeploymentResult`.
- The method performs a read-only preflight via `check_product_update(product_id, resolver=resolver)`.
- Only `UPDATE_AVAILABLE` delegates to existing transactional `install_product(...)`.
- All injected install arguments are forwarded unchanged: `resolver`, `fetcher`, `work_root`, `dependency_wheelhouse`, `probe_distribution`, `progress_callback`.
- Non-applicable statuses (`UP_TO_DATE`, `PROVENANCE_UNKNOWN`, `CHECK_FAILED`, `NOT_CHECKED`, `CHECKING`) raise `ProductUpdateNotApplicableError` before archive fetch/build/deployment/apply/selection/provenance mutation.
- `install_product` exceptions during the actual update attempt propagate unchanged.

Transactional invariant:
- No second deployment engine was introduced.
- No direct `apply_deployment_plan` call was added to `update_product`.
- Provenance persistence remains owned by the existing install/deployment path.
- No GUI update button, no automatic update action, no CLI surprise.

Validation:
- `git diff --check`: pass.
- `py_compile` changed/new E.5 files: pass.
- `tests/test_product_update_apply.py -q`: `12 passed`.
- Related targeted FAST subset: `81 passed`.
- FAST: `1171 passed, 191 deselected, 5 warnings`.
- Post-Nono optional hardening: added assertions that the exception carries the exact preflight result object and that monkeypatched `NOT_CHECKED`/`CHECKING` paths do not call the resolver; related subset remained `81 passed`.

Nono review:
- Verdict: `ACCEPT_WITH_NOTES`.
- No blockers.
- Notes accepted as non-blocking: `__all__` order cosmetic; TOCTOU between preflight and install is expected because `install_product` re-resolves like fresh install; required `fetcher`/`work_root` on non-applicable path is acceptable for the narrow API.

Logs:
- `AGENT/logs/m1_2e_e5_review_targeted_20260812T2123.log`
- `AGENT/logs/m1_2e_e5_fast_20260812T2124.log`


## E.6a — GUI Update Action (pre-witness wiring)

Status: accepted by Junior after independent review + Nono audit; committed locally as E.6a pre-witness gate.

Minimal Product Shell wiring so the E.6 real A→B witness can traverse the GUI
path instead of a direct service script:

- `ProductCard` gains a secondary, hidden-by-default `Mettre à jour` button
  shown only when `UpdateStatus.UPDATE_AVAILABLE`.  It emits
  `update_requested(product_id)` and never calls `service.update_product`
  directly.  `Lancer` remains the separate primary action.
- `InstallWorker`/`create_install_thread` gain a small `operation` parameter
  (`"install"` default, `"update"` calls `service.update_product`).  Same
  thread, same `progress` relay, no second framework.
- `ZeAlfieMainWindow` coordinates update requests through the same global
  install/update lock and worker plumbing.  Success refreshes authoritative
  state and re-runs the read-only update check (or marks the card
  `UP_TO_DATE` directly when no check function is wired).  Failure shows the
  error, releases the lock, keeps `Lancer` usable and the update retryable.

Validation (fakes only, no network/build/venv, `.smoke/` untouched):
- `git diff --check`: pass.
- `py_compile` changed/new files: pass.
- `tests/test_gui_update_action.py -q`: `12 passed`.
- Existing GUI subset (`test_gui_update_status.py`, `test_gui_install_async.py`,
  `test_gui_install_progress.py`, `test_gui.py`): `96 passed`.
- Related FAST subset (GUI/update/install-worker, `-m "not zealfie_slow and not integration"`):
  `144 passed`.
- Full FAST gate (`pytest -m "not zealfie_slow and not integration" -q`):
  `1183 passed, 191 deselected, 5 warnings`.
- Nono audit: `ACCEPT_WITH_NOTES`, no blocker; notes limited to UX polish around refresh-failure wording / async recheck window.

Logs:
- `AGENT/logs/m1_2e_e6a_gui_update_targeted_20260812T2148.log`
- `AGENT/logs/m1_2e_e6a_gui_update_fast_20260812T2148.log`


## E.6 — Real GUI A→B Update Witness

Status: PASS after E.6a GUI wiring and witness-script hardening.

Accepted PASS log:

- `AGENT/logs/m1_2e_e6_real_gui_update_witness_retry4_20260813T020441.log`

PASS criteria satisfied:

- Log contains `résumé final PASS`.
- Log contains `EXIT_CODE=0`.
- Real A→B commits resolved:
  - A: `2a8806b2ffc265ca582ba105de88f5457578d078`.
  - B: `ea23f39be41c20ee627e4633a99654cbf892bcd7`.
- GUI pre-update state showed `Update available (ea23f39)` and enabled `Mettre à jour`.
- Qt click emitted `card_update_requested` and started the existing worker with `operation="update"`.
- Worker called `service.update_product(...)` exactly once, off the GUI thread.
- Backend progress covered the real deployment phases: preparing, resolving_source, downloading_source, building_product, acquiring_dependencies, planning_runtime, installing_runtime, validating, activating, completed.
- Runtime transaction activated new slot `rt-001e9a6db407`.
- Post-update provenance recorded B with requested ref `main` and wheel SHA `fc4467b8fa2fd920039242f3ab69949809e70a5fef6b736c1fe289b512befe49`.
- GUI refreshed to `Up to date` while the primary action was `Lancer`.
- Clicking `Lancer` spawned ZeSolver from the updated runtime slot with `ZESOLVER_EMBEDDED_HOST=1`.
- Teardown stopped the launched process, released worker/thread state, and left no new `/tmp/zealfie-acq-*` wheelhouses.

Diagnostic attempts retained:

- `AGENT/logs/m1_2e_e6_real_gui_update_witness_20260813T014142.log`: wrong interpreter (`PySide6` absent from system Python).
- `AGENT/logs/m1_2e_e6_real_gui_update_witness_retry_20260813T014206.log`: automatically selected parent commit lacked expected `gui_scripts:zesolver` contract.
- `AGENT/logs/m1_2e_e6_real_gui_update_witness_retry2_20260813T015941.log`: witness instrumentation used stale ProductCard private attribute.
- `AGENT/logs/m1_2e_e6_real_gui_update_witness_retry3_20260813T020207.log`: witness assertion expected a retained private update result, while the card exposes user-facing update text/button state.

## E.7 — Documentation

Status: completed.

Updated tracked docs:

- `docs/architecture.md`: added M1-2E managed product update architecture: provenance, read-only update detection, transactional update action, and Product Shell update UX.
- `docs/testing.md`: added the real E.6 GUI A→B witness contract and accepted PASS log.
- This report: recorded E.6 PASS evidence and diagnostic attempts.


## E.8 — Global Audit and Final Pre-FULL Gates

Status: ACCEPT_WITH_NOTES; no blockers.

Global audit summary:

- Branch/head verified: `feature/m1-2e-managed-updates` at `d2ff44d6d22cce628b4531e192055e295d2ab409` before the E.7/E.8 documentation commit.
- `.smoke/` remained untracked and untouched.
- E.6 PASS log verified: `AGENT/logs/m1_2e_e6_real_gui_update_witness_retry4_20260813T020441.log` contains both `résumé final PASS` and `EXIT_CODE=0`.
- ProductCard update decoupling verified: it emits `update_requested` and does not call `service.update_product` directly.
- GUI path verified: `Update available` → `Mettre à jour` click → `ZeAlfieMainWindow` → existing QThread worker with `operation="update"` → `service.update_product(...)` → existing transaction path → refresh → `Up to date` → `Lancer`.
- Backend invariant verified: `update_product` remains synchronous and Qt-free, performs read-only preflight, delegates only `UPDATE_AVAILABLE` to `install_product`, and does not introduce a second deployment engine or direct `apply_deployment_plan` call.
- Documentation/report checked as matching the evidence; no overclaiming blocker found.

Non-blocking notes:

- A task brief mentioned `tests/test_product_provenance.py`; the real test file is `tests/test_provenance.py`.
- Earlier E.4 review history remains documented as a Junior-final review because the Nono retry did not return an exploitable verdict at that time.

Junior gates after E.7 docs:

- `git diff --check`: pass.
- `py_compile` changed code + witness script: pass.
- Corrected targeted suite (`test_provenance.py`, `test_updates.py`, `test_update_checks.py`, `test_gui_update_status.py`, `test_product_update_apply.py`, `test_gui_update_action.py`): `90 passed`.
- Related GUI/update/install subset: `131 passed`.
- FAST clean (`-m "not zealfie_slow and not integration"`): `1183 passed, 191 deselected, 5 warnings`.

Logs:

- `AGENT/logs/m1_2e_e8_targeted_corrected_20260813T021102.log`
- `AGENT/logs/m1_2e_e8_fast_clean_20260813T021122.log`

Final FULL gate:

- `AGENT/logs/m1_2e_e8_full_20260813T021254.log`: `1374 passed, 5 warnings in 569.85s (0:09:29)` and `END=2026-08-13T02:22:25+02:00`.
- Note: the runtime-side tool call was interrupted after pytest completed, so this log does not contain the shell wrapper `EXIT_CODE=0` line. The pytest summary and `END` line are the retained source of truth for the completed FULL run.

Additional clean GUI/update subset log:

- `AGENT/logs/m1_2e_e8_gui_related_clean_20260813T022634.log`: `131 passed`, `EXIT_CODE=0`.

## Final Bundle Status

Status: ready for local commit and review bundle generation.

No push performed. `.smoke/` remains untracked and preserved.

## M1-2E Hardening — Wheel Build Output Isolation

Status: ACCEPTED and locally committed after targeted validation, FAST gate, and independent Nono review.

Context:
- A post-closure hardening delta was added to `build_wheel(output_dir=...)`.
- Goal: prevent stale wheels already present in a persistent `output_dir` from being counted as outputs of the current build.

Implementation summary:
- `build_wheel` now builds inside a unique private `zealfie-build-*` child directory under the requested persistent output directory.
- Current-build outputs are discovered only inside that private child.
- Exactly one wheel must be produced before publication.
- The validated wheel is published back to the requested output directory via `os.replace`.
- Existing artifacts in the persistent output directory are preserved on subprocess failure, zero-wheel result, multi-wheel result, and before successful same-name replacement.
- The no-`output_dir` temporary behavior remains unchanged: the wheel is returned in its private temp directory and the caller owns lifecycle.

Validation:
- `AGENT/logs/m1_2e_hardening_wheel_building_targeted_20260813T104951Z.log`: `33 passed`, `EXIT_CODE=0`.
- `AGENT/logs/m1_2e_hardening_related_fast_20260813T105011Z.log`: `14 passed, 69 deselected`, `EXIT_CODE=0`.
- Nono independent review: `PASS`, no blockers, confidence high.
- `AGENT/logs/m1_2e_hardening_fast_final_retry_20260813T105401Z.log`: `1183 passed, 196 deselected, 5 warnings`, `EXIT_CODE=0`.

FULL note:
- `AGENT/logs/m1_2e_hardening_full_final_20260813T105507Z.log` was retained.
- The FULL post-hardening run was interrupted before pytest summary / `EXIT_CODE=0`; it is therefore not used as acceptance evidence.
- A previous M1-2E FULL gate had already passed before this narrow hardening delta; for this delta, targeted + related FAST + full FAST + Nono review were judged sufficient.

No push performed. Pre-existing `.smoke/` remained untracked and preserved.
