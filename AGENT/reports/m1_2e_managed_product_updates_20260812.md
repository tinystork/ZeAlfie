M1-2E MANAGED PRODUCT UPDATES: IN PROGRESS

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

- E.1 Installed Product Provenance: delegated to Coco, pending review.
- E.2 Read-only Update Detection: pending.
- E.3 Non-blocking Startup Check: pending.
- E.4 Product Shell Update UX: pending.
- E.5 Transactional Update: pending.
- E.6 Real ZeSolver A→B witness: pending.
- E.7 Documentation: pending.
- E.8 Global Nono review: pending.

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
