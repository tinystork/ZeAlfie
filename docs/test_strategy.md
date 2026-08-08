# ZeAlfie Test Strategy

> Version 0 — Proposal for local development and milestone review.
>
> This document defines test orchestration only. It must not change ZeAlfie
> product semantics, runtime behaviour, deployment validation, or security
> policy.

## Core Invariant

Do not make product code weaker to make tests faster.

In particular, runtime/deployment tests must not skip candidate creation,
offline wheel installation, artifact revalidation, candidate validation, or
activation-time TOCTOU checks when those behaviours are the subject of the
test.

Speed should come from choosing the appropriate test scope and from safer test
orchestration, not from reducing runtime guarantees.

## Test File Classes

The current suite is organized by file rather than by pytest markers.
Until markers or a dedicated runner exist, use file-level selection.

### Fast — synthetic or light checks

Mostly no real venv/pip I/O:

```text
tests/test_startup.py
tests/test_cli.py
tests/test_component_model.py
tests/test_component_registry.py
tests/test_component_manifest.py
tests/test_component_metadata.py
tests/test_launch_model.py
tests/test_launch_executor.py
tests/test_runtime_layout.py
tests/test_runtime_status.py
tests/test_runtime_probe.py
tests/test_runtime_planning.py
tests/test_review_bundle.py
```

### Medium — wheel/release checks

Builds or inspects witness wheels, generally without full runtime install
cycles:

```text
tests/test_wheel_building.py
tests/test_releases.py
```

### Slow — real runtime venvs and offline pip installs

These exercise the shared runtime and can create many temporary venvs:

```text
tests/test_runtime_6b.py
tests/test_runtime_6b1.py
tests/test_runtime_6b2.py
tests/test_runtime_hardening.py
tests/test_runtime_manager.py
tests/test_runtime_deployment.py
```

### Integration — witness cycles

End-to-end witness/runtime flows:

```text
tests/integration/test_runtime_transaction.py
tests/integration/test_runtime_witness_cycle.py
tests/integration/test_witness_install_launch.py
```

## Scratch and Checkpoint Policy

Runtime tests create temporary virtual environments and can generate large
scratch trees. Long runs should avoid relying on `/tmp` alone, because tmpfs can
fill and abort otherwise valid test runs.

Recommended scratch root for long local validations:

```bash
mkdir -p AGENT/tmp/pytest-scratch
TMPDIR="$PWD/AGENT/tmp/pytest-scratch" \
.venv/bin/python -m pytest ...
```

Recommended cleanup for generated scratch data:

```bash
rm -rf AGENT/tmp/pytest-scratch AGENT/tmp/*segmented*pytest*state*.json
mkdir -p AGENT/tmp/pytest-scratch
```

`AGENT/tmp/` is operational scratch. It is intentionally ignored by Git and is
not part of the product runtime. A review bundle is generated from tracked files
plus explicit `REVIEW/` metadata, so scratch content must not be required to
understand or reproduce the source state.

Persistent checkpoints for long test orchestration may live under `AGENT/tmp/`
so interrupted runs can resume without restarting from the first file. These
checkpoints are evidence for the current local run, not durable project state.

Avoid running multiple long suites concurrently against the same scratch root.
If concurrent runs become necessary, use per-run scratch directories under
`AGENT/tmp/`.

## FAST

Purpose: quick developer feedback after small edits.

For M0-8 planning/release/deployment changes:

```bash
.venv/bin/python -m pytest \
  tests/test_runtime_planning.py \
  tests/test_releases.py \
  tests/test_runtime_deployment.py \
  -q
```

For isolated CLI/startup edits:

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_startup.py -q
```

Use the smallest set that actually covers the edited behaviour. Do not treat a
FAST pass as a release or milestone gate.

## TARGETED

Purpose: milestone/review validation for a bounded implementation area.

For M0-8B transactional deployment work:

```bash
mkdir -p AGENT/tmp/pytest-scratch
TMPDIR="$PWD/AGENT/tmp/pytest-scratch" \
.venv/bin/python -m pytest \
  tests/test_runtime_deployment.py \
  tests/test_runtime_manager.py \
  tests/integration/test_runtime_transaction.py \
  -q
```

This covers the apply engine, candidate validation, activation/rollback paths,
and transaction integration. It is the minimum targeted gate before accepting
M0-8B runtime changes.

For M0-8A planning-only changes:

```bash
.venv/bin/python -m pytest tests/test_runtime_planning.py -q
```

For release/artifact validation changes:

```bash
.venv/bin/python -m pytest tests/test_releases.py tests/test_wheel_building.py -q
```

For docs-only changes, `git diff --check` and review-bundle generation are
usually sufficient. Run code tests only when the documentation change is coupled
to behaviour or examples that may drift.

## FULL

Purpose: final local confidence before producing a milestone review bundle.

The monolithic command remains valid when resources are healthy:

```bash
mkdir -p AGENT/tmp/pytest-scratch
TMPDIR="$PWD/AGENT/tmp/pytest-scratch" \
.venv/bin/python -m pytest -q
```

However, runtime tests are long and create many venvs. The preferred FULL mode
for review is segmented by test file with a persistent checkpoint:

1. discover `tests/**/test_*.py` deterministically;
2. run one test file per subprocess;
3. write checkpoint state after each passed file;
4. resume at the first non-passed file after SIGTERM/timeout;
5. fail closed when a file records a failure;
6. place both scratch and checkpoint under `AGENT/tmp/`.

A future developer helper may formalize this as:

```bash
python tools/run_segmented_pytest.py \
  --state AGENT/tmp/segmented_pytest_state.json \
  --scratch AGENT/tmp/pytest-scratch \
  -- tests
```

Suggested helper behaviour:

* discover test files deterministically;
* run one file per child process while setting `TMPDIR`;
* write JSON state after each pass/fail;
* resume after interruption;
* support a time budget for long sessions;
* print remaining files when stopping early;
* never import or patch ZeAlfie product modules merely to speed execution.

The helper should be test orchestration only. It must not patch, monkeypatch, or
otherwise weaken ZeAlfie product behaviour to get a faster result.

## Risks and Tradeoffs

* FAST is not a gate; it covers the edited area only.
* File-level selection is pragmatic but can drift as test files grow; pytest
  markers may become useful later.
* A shared scratch root can clash between concurrent runs; use per-run scratch
  directories if parallel validation becomes common.
* Until the segmented runner exists, FULL checkpointing is manual or
  agent-driven and therefore more error-prone than a dedicated tool.
* `/tmp` may be a small tmpfs on Linux; runtime tests with venvs can exhaust it
  even when the main filesystem has plenty of space.
* No coverage threshold is defined yet; that is a separate quality-policy
  decision, not part of M0-8B closure.

## M0-8B Closure Evidence

For the M0-8B closure pass, a segmented full validation was run successfully
after `/tmp` exhaustion was corrected:

- 24 test files passed;
- 0 failures;
- checkpoint used: `AGENT/tmp/m0_8b_segmented_pytest_state.json`.

This proves the segmented approach can resume after interruption while keeping
the same test semantics.
