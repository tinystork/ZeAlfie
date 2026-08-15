# ZeAlfie Architecture Notes

> **Version 0 — Working document**
>
> ZeAlfie is currently at version 0. This document describes the initial architectural direction of the project and is expected to evolve as requirements, implementation experience, and component integrations become clearer.

## Purpose

ZeAlfie is intended to act as the common entry point for the ZeSoftware astronomy ecosystem.

The initial ecosystem includes:

* ZeAnalyser;
* ZeSolver;
* ZeMosaic;
* ZeSeestarStacker.

Its expected responsibilities include:

* launching supported applications;
* detecting installed components and their versions;
* checking for validated updates;
* assisting with technical setup;
* detecting available CPU and GPU capabilities;
* managing shared astronomical resources such as star catalogues;
* managing the shared Python runtime;
* diagnosing and repairing installation problems;
* keeping every application independent and modular.

## Guiding Principle

ZeAlfie is an orchestrator.

It must not absorb the internal source code of ZeSolver, ZeMosaic, ZeAnalyser, or ZeSeestarStacker.

Each component must remain independently:

* developed;
* tested;
* versioned;
* published;
* maintainable.

ZeAlfie should coordinate these applications through stable and documented public boundaries.

## Current Version 0 State

The first milestone exposes only a minimal command-line interface.

At this stage, ZeAlfie can load known component definitions from a packaged local TOML manifest and inspect matching distributions installed in the active Python environment.

It does not yet:

* configure external components;
* install external components;
* update external components;
* launch external components;
* manage astronomical catalogues;
* provide a graphical interface.

The current implementation validates only the package structure, application startup, CLI entry points, version reporting, basic system status reporting, local component manifest loading, and local component metadata inspection.

## Component Model

ZeAlfie separates static component definitions from detected component status.

A component definition records:

* stable component id;
* display name;
* Python distribution name;
* expected public launch entry point contracts.

Each launch entry point contract records both:

* entry point group;
* entry point name.

The group and name are compared exactly. For example, `console_scripts:zesolver` and `gui_scripts:zesolver` are different contracts.

A component status records:

* whether the distribution is installed in the active Python environment;
* the installed version when available;
* whether an expected public launch contract is declared by the installed distribution;
* the matched entry point metadata when a contract is available;
* a stable reason code and user-facing reason when the component is absent or its launch contract is unavailable.

Missing components are normal status results, not exceptions at the application layer.

`Launch contract: available` means only that compatible entry point metadata exists. It does not mean ZeAlfie has launched the process, imported the entry point, validated GUI dependencies, checked catalogues, tested GPU capability, or confirmed runtime health.

## Local Component Manifest

Version 0.0.3 ships a local TOML manifest as a package resource:

```text
zealfie/manifests/components.toml
```

The manifest currently uses:

```toml
schema_version = 1

[[components]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"

[[components.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"
```

The manifest is loaded with `importlib.resources`, so production code does not depend on the current working directory, a source checkout root, a neighbouring repository, or a Git clone.

Manifest validation rejects:

* missing or unsupported schema versions;
* missing or incorrectly typed component lists;
* empty component identifiers, display names, or distribution names;
* duplicate component identifiers;
* malformed launch tables;
* empty or incorrectly typed entry point groups and names;
* duplicate entry point contracts.

There is no remote manifest, download URL, release channel, hash, signature, installation path, or command field yet.

## Local Registry

Version 0.0.3 builds the local registry from the packaged manifest.

The registry can:

* enumerate known components;
* return a definition by id;
* inspect all known components;
* report unknown ids without a traceback.

There is no plugin system, remote manifest, or dynamic component loading yet.

## Metadata Inspection

Component installation and version detection use `importlib.metadata` against the active Python environment.

ZeAlfie does not detect components by:

* scanning neighbouring repositories;
* reading personal workspace paths;
* importing component packages;
* importing GUI modules;
* checking Git branches;
* reconstructing commands from source checkout paths.

This is intentional because the future shared runtime should be validated through installed distributions, not accidental local source trees.

Distribution absence and metadata errors are distinct:

* `DISTRIBUTION_NOT_INSTALLED` means the distribution is absent from the active environment metadata.
* `DISTRIBUTION_METADATA_ERROR` means a distribution lookup or metadata result existed but could not be inspected correctly.

The CLI and future GUI must use these reason codes instead of parsing free text.

## Component Boundary

ZeAlfie must not import internal GUI classes or implementation details from managed applications.

The target component boundary should rely on stable mechanisms such as:

* published Python entry points;
* module entry points;
* subprocess commands;
* installed-package metadata;
* versioned manifests;
* small public diagnostic interfaces;
* structured capability results.

For ZeSolver, the preferred integration direction is:

1. detect the installed distribution through package metadata;
2. read its installed version without importing GUI internals;
3. determine whether a stable public launch entry point exists;
4. launch it independently under the shared runtime;
5. report clearly when the component is installed but no supported launch contract is available.

Direct imports from ZeSolver GUI modules are not part of the target architecture.

For M0-3, `zeblindsolver`, `zeblindsolve`, and `zebuildindex` are not accepted as ZeSolver application launch contracts. They are solver/index utility scripts, not the public ZeSolver GUI entry point declared in the local manifest.

## Shared Python Environment

The target architecture uses one shared Python runtime and one shared active virtual environment for the supported ZeSoftware ecosystem.

This enables:

* reduced disk usage;
* direct compatibility testing between components;
* controlled dependency versions;
* suite-wide validation before publication;
* a consistent runtime managed by ZeAlfie.

No managed component should independently modify the shared environment.

Large or persistent resources must remain outside the virtual environment, including:

* ASTAP databases;
* ZeBlind indexes;
* user settings;
* logs;
* caches;
* downloaded packages;
* user images;
* generated results.

## Current Repository Structure

```text
ZeAlfie/
├── README.md
├── LICENSE
├── pyproject.toml
├── .gitignore
├── docs/
│   └── architecture.md
├── src/
│   └── zealfie/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── cli.py
│       ├── components/
│       │   ├── __init__.py
│       │   ├── manifest.py
│       │   ├── metadata.py
│       │   ├── model.py
│       │   └── registry.py
│       └── manifests/
│           └── components.toml
└── tests/
    ├── test_cli.py
    ├── test_component_manifest.py
    ├── test_component_metadata.py
    ├── test_component_model.py
    ├── test_component_registry.py
    ├── test_startup.py
    └── fixtures/
        └── witness_component/
```

## Provisional Target Structure

The following structure represents a possible evolution of the project. It is not yet a fixed or fully implemented design.

```text
ZeAlfie/
├── docs/
│   └── architecture.md
├── src/
│   └── zealfie/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── cli.py
│       ├── components/
│       ├── diagnostics/
│       ├── resources/
│       ├── runtime/
│       └── updates/
└── tests/
```

Directories should only be introduced when real implementation needs justify them.

Version 0 should not create empty abstractions or speculative modules merely to match this provisional structure.

## Module Responsibilities

### `__main__.py`

Provides the minimal `python -m zealfie` execution bridge.

It should remain a thin wrapper around the public application entry point.

### `cli.py`

Owns command-line parsing, CLI-specific output, and command exit codes.

It should remain a thin interface over reusable application services.

### `app.py`

Contains application-level behaviour that can be reused by both the CLI and the future graphical interface.

It must not become tied exclusively to either presentation layer.

### `components/`

Discovery, status, version, capability, and launching logic for managed ZeSoftware applications.

### `building/`

Local wheel construction and inspection.  ``build_wheel`` produces a wheel
from a source directory using ``python -m build --wheel``.  ``inspect_wheel``
opens the wheel as a ZIP archive and reports packages, metadata, version,
and entry points without loading or executing any code from the wheel.

### `environment/`

``TemporaryVenv`` creates an isolated temporary virtual environment,
installs wheels offline, and runs Python commands inside it.  The
environment is cleaned up when the context manager exits, even on errors.

### `releases/`

Trusted local release manifest parsing, host-compatible artifact
selection, and artifact verification (integrity, identity, contract).

**Release manifest** (``manifest.py``, ``model.py``):

A local TOML file describes one or more wheel artifacts for a component
release.  Each artifact declares a *filename*, *size*, *sha256*, and
optional host compatibility tags (*python_tag*, *abi_tag*, *platform_tag*).
The parser is strict: unknown keys, duplicate filenames, and malformed
fields are rejected.  The manifest does not define component identity
or launch contracts — those remain in ``components.toml``.

**Host compatibility** (``model.py``, ``selector.py``):

``HostTarget`` is an immutable value object capturing the target host's
Python version, ABI, and platform using wheel-tag convention (e.g.
``py312``, ``cp312``, ``linux_x86_64``).  ``HostTarget.from_current_host()``
detects the running interpreter via ``sysconfig`` and ``sys``; all other
compatibility logic operates on synthetic targets, making policy testable
without the real OS.

``select_artifact(manifest, host)`` is the low-level deterministic selector.
It returns the index of the single compatible artifact or raises
``ArtifactSelectionError``:

* At the low-level selector only, an artifact whose manifest tags are all
  ``None`` matches any host.  This keeps the M0-7A/M0-7B primitive compatible
  for focused tests and internal use.
* A tagged artifact uses strict matching: ``python_tag`` must be an exact
  match or the generic ``py<major>`` form (``py3`` matches ``py312``),
  ``abi_tag`` ``"none"`` matches any host ABI, and ``platform_tag`` ``"any"``
  matches any platform.
* A partially-tagged artifact is incompatible (fail-closed).
* Zero compatible artifacts → error.
* Multiple indistinguishable compatible artifacts → error (ambiguity;
  TOML order is not a tiebreaker).

**Safe local release resolution** (``resolver.py``):

``resolve_local_release(manifest, registry, artifact_root, host)`` is the
preferred M0-7C API for trusted local releases.  It resolves the manifest's
component id through the trusted ``ComponentRegistry`` and validates that
every artifact entry with declared compatibility tags matches the simple
wheel filename suffix ``-{python_tag}-{abi_tag}-{platform_tag}.whl`` before
selection.  It then calls ``select_artifact()`` internally and passes that
exact selected index to ``verify_artifact()``.  Callers never pass an
``artifact_index`` to this high-level API, which removes the easy misuse
``select artifact A`` followed by ``verify artifact B``.

The filename tag check is intentionally narrow and dependency-free: filenames
must end in ``.whl`` and the final three ``-``-separated stem segments are
treated as the wheel's Python, ABI, and platform tags.  In the safe M0-7C
resolver path, fully untagged manifest entries are not treated as universally
compatible; the resolver derives their effective compatibility from those
filename tags before selection.  This preserves historical ``py3-none-any``
manifests while rejecting untagged platform-specific filenames such as
``...-py3-none-win_amd64.whl`` on Linux.  When any tag is declared, mismatches
such as manifest ``platform_tag="linux_x86_64"`` with filename
``...-py3-none-any.whl`` are rejected fail-closed.

**Verification** (``verifier.py``):

``verify_artifact(manifest, artifact_index=selected_index)`` runs the full
M0-7A chain against the selected artifact: path confinement (no symlinks, no
escapes), size, SHA-256, wheel structural inspection, distribution
name/version match, and entry-point contract check.  Omitting
``artifact_index`` is allowed only for single-artifact manifests;
multi-artifact manifests require an explicit selected index.  The normal
M0-7C path is ``resolve_local_release()`` rather than direct verifier calls.
The result is a ``VerifiedArtifact`` with TOCTOU semantics (valid at a point
in time, not a permanent trust cache).

**CWD-shadow hardening** (``building/__init__.py``):

``build_wheel()`` sets ``cwd`` to the output directory so that a local
``build/`` folder in the repository checkout cannot mask the PyPA
``build`` package via Python's CWD-first ``sys.path`` behaviour.



### `launching/`

Controlled subprocess execution.  ``LaunchPlan`` is an immutable structured
command (never a shell string).  ``LaunchResult`` captures return code,
stdout, stderr, and timeout status.  ``execute_launch_plan`` runs a plan
with ``shell=False``, captures output as UTF-8 text, and kills the process
on timeout.  ``resolve_script`` locates a named entry-point script inside
a venv scripts directory, accounting for platform suffixes (``.exe`` on
Windows).

### `diagnostics/`

Future operating-system, Python runtime, CPU, GPU, CUDA, memory, storage, and dependency checks.

### `resources/`

Future management and verification of shared astronomical resources such as ASTAP databases and ZeBlind indexes.

### `runtime/`

Management of the persistent shared Python runtime.

M0-6 replaced the earlier single-directory ``current``/``staging`` idea
with an immutable slot architecture:

```text
<runtime-root>/
├── slots/
│   └── <slot-id>/
│       └── ... virtual environment ...
└── state/
    └── active.json
```

``RuntimeLayout`` defines centralised paths for ``root``, ``slots`` and
``state/active.json``.  Slot ids are strictly validated so they cannot be
absolute paths, parent-directory traversals, or path fragments.  The
production root is derived from platform-appropriate directories (XDG on
Linux, Application Support on macOS, LocalAppData on Windows) and can be
overridden via ``ZEALFIE_RUNTIME_ROOT`` or an explicit *root* parameter.

Slots are created at their final path and are not renamed during activation.
Activation changes only the atomic ``state/active.json`` pointer.  This
keeps the active runtime boundary explicit and avoids in-place mutation of
the currently active virtual environment.

``RuntimeState`` captures the coarse lifecycle: ``ABSENT``, ``READY``,
``BROKEN``.  ``RuntimeStatus`` is an immutable snapshot with the active
slot id/path, previous slot id when known, resolved Python path, version,
and a stable ``RuntimeReasonCode``.

``SharedRuntime`` is the top-level manager:

* ``status()`` inspects the active pointer and validates the active slot;
* ``create()`` creates the first runtime through the same transaction path
  and refuses implicit repair of a ``BROKEN`` runtime;
* ``begin_transaction()`` records the current active pointer as the base
  state for stale-transaction checks;
* ``install_local_wheel()`` inspects a local wheel, installs it offline into
  a chosen slot, and post-validates through an external metadata probe;
* ``validate_candidate()`` marks a candidate valid only after checking the
  slot Python and, when component definitions are supplied, each expected
  distribution and launch contract;
* ``activate()`` revalidates immediately before switching the pointer and
  refuses stale transactions if the active slot changed;
* ``rollback()`` switches the active pointer back to the recorded previous
  slot when that slot is still usable;
* ``discard()`` is for prepared, non-active candidates and must never remove
  the active slot.

Install outcomes are structured: ``INSTALLED``, ``ALREADY_INSTALLED``,
``VERSION_MISMATCH``, ``CONTRACT_MISMATCH``, ``FAILED``.

``probe_runtime_distribution()`` runs a small standard-library-only script
inside the runtime's Python and returns structured JSON.  No application
code is imported during probing.

The persistent runtime is distinct from both the development ``.venv`` and
the test-only ``TemporaryVenv``.  Temporary test environments and scratch
virtual environments are operational artefacts, not product runtime state.

### `updates/`

Future retrieval, validation, staging, activation, and rollback of published component versions.

## CLI and GUI Relationship

The command-line interface and the future graphical interface must use the same underlying application logic.

The intended model is:

```text
CLI ───────┐
           ├── Application services ── Component adapters
PySide6 ───┘
```

PySide6 is the intended GUI framework for ZeAlfie in order to remain consistent with the wider ecosystem.

It must not become a required dependency until graphical interface code actually exists.

The future GUI must not reimplement component detection, diagnostics, launching, or update logic independently from the CLI.

## First Development Milestones

### M0-1 — Foundations

The first completed prototype must:

1. install as a Python package;
2. start successfully;
3. expose `python -m zealfie`;
4. expose the `zealfie` console command;
5. report its version;
6. display basic platform and Python status;
7. remain independent from all managed applications.

### M0-2 — ZeSolver Detection Contract

The next prototype should:

1. define a minimal component information model;
2. detect whether the ZeSolver distribution is installed;
3. report its installed version;
4. determine whether a supported launch contract is available;
5. explain why launching is unavailable when no stable public entry point exists;
6. avoid importing or launching ZeSolver GUI internals.

### Later Milestone — ZeSolver Launch

A later milestone should:

1. launch ZeSolver through a stable public entry point;
2. use an independent process;
3. preserve the shared runtime boundary;
4. report startup failures clearly;
5. avoid embedding ZeSolver code inside ZeAlfie.

### M0-7B — Host Compatibility + Deterministic Artifact Selection

The release manifest now supports multiple wheel artifacts per release, each
with optional host compatibility tags.  A deterministic selector matches the
current host to exactly one compatible artifact:

1. ``HostTarget`` captures Python tag, ABI tag, and platform tag from the
   running interpreter (detection layer isolated in ``from_current_host()``).
2. ``select_artifact()`` returns a single compatible index or rejects clearly.
3. ``verify_artifact()`` accepts an *artifact_index* to verify the correct
   entry after selection.
4. All matching is fail-closed: absent/ambiguous/unknown metadata → rejection.
5. Offline-only: no network, no remote manifests, wheel-only.

### M0-7C — Safe Local Release Resolution

The release layer now exposes ``resolve_local_release()`` as the normal local
release handoff.  The operation is a single fail-closed API call:

```text
ReleaseManifest + HostTarget + trusted ComponentRegistry + artifact_root
    -> VerifiedArtifact
```

It resolves component identity, checks declared manifest tags for every
artifact against the simple wheel filename tag suffix, selects exactly one
host-compatible artifact, and verifies that same selected artifact.  The
lower-level
``select_artifact()`` and ``verify_artifact()`` primitives remain available
for focused tests and internal uses, but callers of the normal release path do
not provide an artifact index.


## Explicit Non-Goals for Version 0

Version 0 should not yet:

* merge the repositories of supported applications;
* execute arbitrary code directly from development branches;
* update components from unvalidated commits;
* manage every catalogue format;
* solve every platform-specific case;
* provide a permanent graphical design;
* support arbitrary third-party plugins;
* guarantee backward compatibility;
* implement the full installation and update lifecycle at once;
* create speculative abstractions without a concrete use case.

## Cross-Platform Direction

ZeAlfie is intended to support:

* Linux;
* Windows;
* macOS.

Platform-specific implementation is acceptable, but platform assumptions should be isolated and documented.

A feature should not be considered fully validated merely because it works on one development machine.

M0-4 adds platform-aware venv path handling (``bin`` vs ``Scripts``, ``.exe``
suffixes) but physical validation on Windows and macOS has not been performed.

## Evolution

This document is intentionally provisional.

Its structure, terminology, component model, runtime strategy, and module boundaries may evolve as ZeAlfie progresses from a minimal experimental launcher to a stable cross-platform application manager.

Architectural changes should be driven by validated requirements and implementation experience rather than premature generalisation.

## M0-8A — Desired Runtime State & Deployment Planning

The M0-8A planning layer adds a pure, read-only mechanism to express a
complete desired runtime state and produce a structured deployment plan.
It does **not** install, create candidate slots, activate, rollback,
discard, or mutate the filesystem.

### Desired Runtime State

``DesiredComponent`` links a component id, version, and a
``VerifiedArtifact``.  ``DesiredRuntimeState`` is an immutable, sorted
tuple of ``DesiredComponent``.  It validates non-empty, no duplicate
component ids, and deterministic ordering by ``component_id``.

### Completeness Guard

At plan-build time, the desired component ids must **exactly match**
``registry.available_ids()``.  Missing or extra ids are rejected via
``PlanningError``.  This is the M0-8A guard against a future
ZeSolver-only update accidentally dropping other trusted runtime
components — you cannot express a partial desired state.

### Deployment Plan

``build_deployment_plan(desired_state, registry, runtime_status, *, probe_distribution)``
returns a ``DeploymentPlan`` with deterministic ``DeploymentStep``
entries in ``component_id`` order.

Each step carries:

* **component_id** — stable identity.
* **desired_version** — version requested by the desired state.
* **artifact** — the ``VerifiedArtifact``, **always present** even for
  ``KEEP`` steps, so a future full-state application can materialize the
  entire desired runtime from the plan alone.
* **action** — ``DeploymentAction.KEEP``, ``.INSTALL``, or ``.BLOCKED``.
* **reason_code** / **reason** — structured ``DeploymentReasonCode``
  and human-readable reason.

### Runtime State Routing

* **ABSENT** → every component is ``INSTALL`` with
  ``RUNTIME_ABSENT``.  No probes are executed.
* **BROKEN** → every component is ``BLOCKED`` with ``RUNTIME_BROKEN``.
  No probes are executed.  The plan describes the failure but does not
  attempt mutation.
* **READY** → each component is probed via ``probe_distribution()``:
  - not installed → ``INSTALL`` (``DISTRIBUTION_MISSING``).
  - installed, version matches, launch contract satisfied → ``KEEP``
    (``ALREADY_SATISFIED``).
  - version mismatch → ``INSTALL`` (``VERSION_MISMATCH``).
  - launch contract missing/mismatched → ``INSTALL`` (repair by future
    application; ``LAUNCH_CONTRACT_MISMATCH``).
  - probe exception or malformed payload → whole plan blocked
    (``PROBE_FAILED``).

### VerifiedArtifact TOCTOU Semantics

``VerifiedArtifact`` describes verification performed at a point in
time.  M0-8A carries the artifact reference in every step but does
**not** re-verify it.  A future application step (M0-8B) must revalidate
before installation to close the TOCTOU window.  The plan does not
create a persistent trust cache.

### No Mutation

M0-8A uses ``stdlib`` only.  It does not call:

* ``SharedRuntime.install_local_wheel``
* ``RuntimeTransaction`` / ``activate`` / ``rollback`` / ``discard``
* ``venv.create`` / ``pip install``
* any filesystem mutation

### Relationship to M0-8B

M0-8A produces the plan.  M0-8B applies that plan, revalidating each
``VerifiedArtifact`` immediately before installation into candidate slots.
The separation keeps planning deterministic, testable, and fail-closed
independent of the transaction engine.

## M0-8B — Transactional Offline Deployment

M0-8B adds the runtime-side application of a complete ``DeploymentPlan``.
It intentionally remains a reusable runtime service; this closure does not
add a CLI or GUI surface for applying plans.

``apply_deployment_plan(plan, registry, runtime)`` is the single
transactional entry point.  Its job is to transform a pure desired-state
plan into a validated candidate slot and then atomically activate that slot.
It does not fetch remote data, resolve dependencies, or mutate the active
runtime in place.

### Apply Preflight

Before candidate creation or any pip operation, apply fails closed when:

* the plan is blocked;
* the current runtime is ``BROKEN``;
* the active slot differs from the slot recorded when the plan was built
  (stale plan);
* the desired component ids no longer exactly match the current registry;
* the current registry/desired-state combination has shared-runtime
  conflicts, including duplicate normalized distribution names or duplicate
  launch entry-point ``group:name`` contracts.

The apply-time conflict recheck is deliberate redundancy: planning may have
used a coherent registry, while the registry definitions may have changed
before application even if component ids stayed identical.

### Candidate Creation and Full-State Materialization

A deployment creates a fresh candidate virtual environment directly under
``slots/<candidate-slot-id>``.  The candidate path is explicitly checked for
collision before creation, and M0-8B candidate creation does not use
``clear=True`` because an immutable slot must not be silently cleared.

The candidate is materialized from the complete desired state, not from a
partial delta.  Every desired component is installed in deterministic
``component_id`` order, including components whose plan action was ``KEEP``.
This preserves the product invariant that a new active slot represents the
whole trusted runtime state, not only the components changed by an update.

Immediately before pip handoff, each ``VerifiedArtifact`` is revalidated
against the trusted registry.  The original verification remains a
point-in-time fact; it is not treated as a permanent trust cache.
Installation is offline and local-wheel based through the runtime install
path (``--no-index`` / ``--no-deps``), so M0-8B does not perform network
access or dependency resolution.

### Candidate Validation and Activation

After installation, M0-8B validates the candidate as a multi-component
runtime.  Each expected distribution and launch contract must be present.
The versions observed in the candidate are then compared explicitly against
``DesiredRuntimeState`` before activation.  A candidate that contains the
wrong version is rejected even if installation reported success.

Activation performs one more TOCTOU revalidation immediately before writing
``state/active.json``.  If a candidate is corrupted between validation and
activation, activation fails and the active pointer is left unchanged.  On
success, only the pointer changes: the candidate slot becomes active and the
previous active slot is recorded as ``previous_slot_id``.

### Failed-Candidate Policy

On failure, M0-8B preserves the active slot and does not promote the
candidate.  Failed candidate slots may be left on disk for diagnostics.
They must not be treated as trusted runtime state, and no caller should infer
that a failed candidate is usable merely because a venv directory exists.

Automatic garbage collection of failed candidates is deferred.  Manual
cleanup or a future retention policy may remove non-active failed candidates,
but M0-8B does not auto-delete them during failure handling because deletion
would discard useful diagnostics and would introduce another destructive path
inside the transaction engine.

### Deferred Risks and Non-Goals

The following risks are acknowledged and intentionally deferred beyond this
closure pass:

* no remote update channel or network retrieval path;
* no shared dependency resolver or cross-component dependency solver;
* no automatic garbage collection/retention policy for failed candidates;
* no CLI or GUI apply surface in M0-8B;
* physical cross-platform validation remains incomplete beyond the current
  development host;
* concurrency hardening is limited to stale active-pointer checks, so future
  file locking or process coordination may be needed;
* rollback uses the current previous-slot pointer and is not yet a complete
  multi-generation retention or disaster-recovery policy.

## M0-9 — Application Service / Offline Orchestration

M0-9 assembles the existing component, release, planning, transaction, and
rollback primitives behind a small application service and terminal surface.
It does not add a GUI, network channel, dependency solver, or new generic
deployment engine.

### Offline Release Directory

M0-9 intentionally supports one local, deterministic convention:

```text
release_dir/
  <component_id>.toml
  <wheel_filename>.whl
```

For every component id in the trusted registry, a top-level
``<component_id>.toml`` release manifest must exist.  The manifest's
``component_id`` must match its filename stem, referenced wheels live at the
same top level, and unknown top-level ``.toml`` files are rejected.  There is
no recursive scan, fallback filename, channel discovery, or heuristic bundle
resolution.

### Application Service

``ZeAlfieService`` is the M0-9 application-level orchestrator:

* ``resolve_offline_release_set(release_dir)`` resolves a complete
  ``DesiredRuntimeState`` from the deterministic release directory.
* ``plan_offline_deployment(release_dir)`` is read-only and returns a
  ``DeploymentPlan`` from the current runtime status.
* ``apply_offline_deployment(release_dir)`` resolves and plans fresh at call
  time, then delegates to ``apply_deployment_plan``.  It never consumes or
  persists a previous preview plan.
* ``rollback_runtime()`` delegates to ``SharedRuntime.rollback()``.

The service reuses the existing M0-7/M0-8 models and engines rather than
reimplementing release verification, planning, transactional apply, or
rollback semantics.

### CLI Surface

M0-9.3 exposes the service through the existing ``runtime`` command group:

```bash
zealfie runtime plan --release-dir PATH
zealfie runtime apply --release-dir PATH
zealfie runtime rollback
```

``runtime plan`` is a read-only preview.  ``runtime apply`` performs fresh
resolution and planning through ``ZeAlfieService.apply_offline_deployment``.
``runtime rollback`` uses the existing pointer-level rollback mechanism.

## M1-2A — Product Catalog & Product Shell

### Product Catalog

The product catalog is an immutable registry of **known** ZeSoftware products.
It answers "what products does ZeAlfie know about?".

The catalog is loaded from the packaged resource
``zealfie/manifests/products.toml`` and contains exactly four products:

```text
zesolver, zemosaic, zeseestarstacker, zeanalyser
```

Each product descriptor records:

* stable ``product_id``;
* ``display_name``;
* ``distribution_name`` (the PyPI distribution);
* ``launch_entry_points`` (public entry-point contracts);
* ``required_extras`` (canonicalised extra names for dependency resolution).

The catalog is **deliberately separate** from the user's desired-product
selection and from the component registries.  These are distinct concepts:

| Concept | Source | Owned By | Purpose |
|---------|--------|----------|---------|
| Product Catalog | ``products.toml`` | ``ProductCatalog`` | What ZeAlfie knows about |
| Desired Product Selection | ``desired-products.toml`` | ``SelectionStore`` / ``DesiredProductSelection`` | What the user chose to manage |
| Component Registry (materialized) | catalog + selection | ``ComponentRegistry`` | Technical representation of the desired component set (derived, not primary) |
| Component Registry (packaged, pre-D4) | ``components.toml`` | ``ComponentRegistry`` (default) | Deployment/launch contract until D.4 |
| Managed Runtime State | Shared runtime slots | ``SharedRuntime`` | What is actually installed |
| Product State | Read model derived at call time | ``ProductShellState`` | Observed snapshot |

Adding a product to the catalog **never** forces it into deployment planning,
release resolution, or the user's selection.  The catalog describes
knowledge; the selection store describes intent; the component registry
materialized from catalog + selection is the derived technical representation
of the desired component set.

### Desired Product Set

The **desired product selection** — the products the user has chosen to
manage — is persisted by the ``SelectionStore`` (``desired-products.toml``),
independently of the catalog and the runtime.  The M1-2A catalog may contain
4 known products while the selection remains ZeSolver-only.  The
``ComponentRegistry`` materialized from catalog + selection
(``desired_component_registry``) is the technical representation of that
desired component set.

Pre-D4, the deployment pipeline (``plan_offline_deployment``, release
resolution, and launch preparation) still resolves against the packaged
registry (``components.toml``); D.4 is expected to switch installation
orchestration to the materialized registry.

D.4 must also perform a one-shot legacy-preserving bootstrap before the
first mutation of the desired selection: when ``desired-products.toml`` is
absent, initialise it from the packaged legacy registry after validating ids
against the product catalog, then apply the user's new delta additively.  A
present selection file, including an explicit empty selection, is
authoritative and must not be re-bootstrapped.

### Managed Runtime State

Product installed-ness is determined **exclusively** from the ZeAlfie-managed
shared runtime via the probe script (``probe_runtime_distribution``).  The
probe runs inside the runtime's Python interpreter and inspects
``importlib.metadata`` in that environment.

The following are explicitly **not** sources of installed-ness:

* the dev virtual environment;
* ``PYTHONPATH``;
* source checkout importability;
* global (system) package state;
* ``import`` in the current process.

This means that a product's distribution may be importable in the development
environment but still report ``installed=False`` through the product shell.
Installed-ness is a property of the managed runtime, not of the calling
process.

### Product State Determination Rules

1. **Runtime ABSENT** → every known product is ``installed=False``,
   ``launchable=False``, with ``RUNTIME_ABSENT`` reason code.  No probes
   are executed.

2. **Runtime BROKEN** → every product is ``installed=False``,
   ``launchable=False``, with ``RUNTIME_BROKEN`` reason code.  No probes.

3. **Runtime READY** → each product is probed via the runtime's Python:
   * Distribution installed + launch contract satisfied →
     ``INSTALLED_LAUNCHABLE``.
   * Distribution installed + contract absent →
     ``INSTALLED_NOT_LAUNCHABLE``.
   * Distribution not installed → ``NOT_INSTALLED``.
   * Probe exception or malformed payload → ``PROBE_FAILED``.

4. **Managed vs Unmanaged** is an orthogonal axis.  A product's ``managed``
   status reflects whether its id appears in the user's persisted selection
   (the ``SelectionStore``).  An
   unmanaged product that happens to be present in the runtime will still
   report ``installed=True`` — the probe does not skip unmanaged products.
   The ``managed`` field documents intent; ``installed`` documents fact.

5. **Launchability** is ``True`` only when the installed distribution's
   declared entry points contain at least one of the catalog's expected
   entry-point contracts.  A product with a satisfied contract is
   launchable; a product without one is not.  Launchability is never
   inferred from catalog knowledge alone.

6. **Unknown products** (ids not in the catalog) raise a typed
   ``UnknownProductError`` distinct from ``UnknownComponentError``.

### Product Shell API

The product shell is exposed through ``ZeAlfieService``:

* ``catalog`` — property returning the ``ProductCatalog``.
* ``list_products()`` — all known ``ProductDescriptor`` entries.
* ``collect_product_state()`` — full ``ProductShellState`` snapshot for
  every known product against the current runtime.
* ``get_product_state(product_id)`` — single ``ProductState``, raising
  ``UnknownProductError`` on unknown ids.
* ``managed_product_ids`` — property returning the set of ids in the user's
  selection store (the desired product set).

All product-shell methods are read-only.  No mutation, no installation,
no launch.

The application layer (``zealfie.app``) re-exports all product-shell types
(``ProductCatalog``, ``ProductDescriptor``, ``ProductShellState``,
``ProductState``, ``ProductStateReasonCode``, ``ManagedStatus``,
``UnknownProductError``) so that CLI and future GUI consumers import from
the application layer.

### CLI Surface

```bash
zealfie products             # show all product state
zealfie products zesolver    # inspect one product
```

The ``products`` command delegates to ``ZeAlfieService.collect_product_state``
and ``ZeAlfieService.get_product_state``.  It reads no internals directly.

## M1-2B — Non-Blocking Managed Product Launch

M1-2B adds a non-blocking launch primitive for managed products/components.
It is designed as the foundation for future GUI-triggered launches: spawn a
process and return a handle immediately rather than blocking until the child
exits.

### Design Decision: Non-Blocking, Not Detached

The minimum contract is *cross-platform non-blocking spawn* with explicitly
documented ownership and lifetime semantics.

* On UNIX, a child spawned via ``subprocess.Popen`` with default flags
  normally survives its parent when the parent exits normally.
* On Windows, true cross-platform survival after parent exit is **not**
  guaranteed without extra ``DETACHED_PROCESS`` creation flags.
* M1-2B does **not** implement an OS-specific process-manager palace,
  polling daemon, job registry, or cross-platform detachment mechanism.

The guarantee is: ``spawn_component`` returns immediately with a
``SpawnedLaunch`` handle.  The child runs independently; ZeAlfie does not
monitor or manage it after spawn.  If guaranteed survival after ZeAlfie
shutdown is required, a future milestone (e.g. M1-2C) may add platform-
specific detachment or a lightweight process supervisor.

### Synchronous vs Non-Blocking Launch

| Mechanism | Function | Blocking? | Use Case |
|-----------|----------|-----------|----------|
| ``execute_launch_plan`` | Blocks until child exits | Yes | Tests, diagnostics, CLI ``launch`` |
| ``spawn_launch_plan`` | Returns ``SpawnedLaunch`` immediately | No | GUI-triggered launch |
| ``launch_component`` | Service wrapper for ``execute_launch_plan`` | Yes | Synchronous component launch |
| ``spawn_component`` | Service wrapper for ``spawn_launch_plan`` | No | Non-blocking component launch |

Both exist side-by-side.  ``execute_launch_plan`` / ``launch_component`` are
preserved for synchronous use cases (tests, diagnostics, CLI command).

### Public API

**Low-level executor** (``zealfie.launching.executor``):

* ``SpawnedLaunch`` — frozen dataclass with ``component_id``, ``pid``,
  optional ``executable`` and ``command`` tuple.
* ``spawn_launch_plan(plan, *, env_overrides=None, stdin=None,
  stdout=None, stderr=None)`` — calls ``subprocess.Popen`` with
  ``shell=False`` and a structured list command.  Returns immediately
  with a ``SpawnedLaunch``.  Wraps ``OSError`` as ``LaunchError``.

**Service layer** (``ZeAlfieService``):

* ``spawn_component(component_id, *, env_overrides=None, stdin=None,
  stdout=None, stderr=None)`` — prepares a launch plan then spawns it.
  Returns a ``SpawnedLaunch`` handle immediately.

**Application layer** (``zealfie.app``):

* Re-exports ``SpawnedLaunch`` so GUI consumers import from the
  application layer.

### Stdout / Stderr Policy

* Default: ``stdin=None``, ``stdout=None``, ``stderr=None``
  (inherit parent fds).
* ``stdout=PIPE`` and ``stderr=PIPE`` are **not** default and are
  documented as dangerous without dedicated reader threads.
* Callers may pass ``stdin=subprocess.DEVNULL`` to suppress child stdin,
  or ``stdout=subprocess.DEVNULL`` / ``stderr=subprocess.DEVNULL`` for
  silent processes.

### Environment Policy

* ``os.environ`` is **never** mutated globally.
* When ``env_overrides`` is provided, ``os.environ.copy()`` is taken
  inside the spawn function and overrides are merged into the copy;
  this becomes the child's ``env`` argument to ``Popen``.
* When ``env_overrides`` is ``None`` or empty, the child inherits the
  parent environment directly (``env=None`` passed to ``Popen``).

### ZeSolver Embedded-Host Rule

When ``spawn_component`` is called with ``component_id="zesolver"``, the
child environment automatically receives ``ZESOLVER_EMBEDDED_HOST=1``.
This override:

1. Is scoped to the child ``Popen`` call — ZeAlfie's own process never
   becomes an embedded host.
2. Is applied *before* caller-supplied ``env_overrides``, so a caller can
   override it intentionally (e.g. ``ZESOLVER_EMBEDDED_HOST=0``).
3. Applies only when ``component_id`` equals ``"zesolver"`` (exact match).
   Other components receive no implicit environment overrides.

### No Global Environment Mutation

At no point is ``os.environ`` modified — not for the embedded-host rule,
not for caller overrides, not for any default.  The child environment is
always constructed from a copy or inherited via the ``Popen`` ``env``
parameter.

### Module Responsibility

* ``zealfie.launching.executor`` — ``SpawnedLaunch`` + ``spawn_launch_plan``.
* ``zealfie.launching.__init__`` — re-exports both.
* ``zealfie.app.service.ZeAlfieService`` — ``spawn_component`` with
  ZeSolver policy.
* ``zealfie.app.__init__`` — re-exports ``SpawnedLaunch``.

### Explicit Non-Goals for M1-2B

* No GUI integration or PySide6 dependency.
* No process lifecycle management, polling daemon, or job registry.
* No cross-platform OS-level daemonisation.
* No CLI surface for non-blocking launch (the ``launch`` command remains
  synchronous; a future ``launch --detach`` flag may be added in M1-2C if
  needed).
* No changes to the ZeSolver repository or its resolver/deployment code.
* No GPU provisioning, installation, or selective deployment changes.
* No replacement of ``execute_launch_plan`` / ``launch_component`` — both
  remain for synchronous use cases.

## M1-2C — PySide6 Product Shell

M1-2C adds a graphical product shell (launcher window) built on PySide6.

### Runtime Dependency Decision

**PySide6** (``>=6``) is declared as a direct runtime dependency in
``[project.dependencies]`` of ``pyproject.toml``.

Rationale:
* No optional/extras gate — the product shell is a core deliverable.
* PySide6 is packaged on PyPI and Conda, installable without a system
  Qt SDK.
* PyQt6 was not selected: the GPL licensing model is incompatible with a
  plausible future MIT/Apache-2.0 license for ZeAlfie.

### Entry Point

The product shell is exposed through ``[project.gui-scripts]`` (the
setuptools GUI scripts group), **not** ``[project.scripts]``:

```toml
[project.gui-scripts]
zealfie-gui = "zealfie.gui:main"
```

This separates the GUI launcher from the CLI:

* ``zealfie`` → ``[project.scripts]`` → CLI (``zealfie.cli:main``);
* ``zealfie-gui`` → ``[project.gui-scripts]`` → product shell
  (``zealfie.gui:main``).

On Windows, ``gui-scripts`` additionally create ``pythonw.exe``
wrappers (no console window).  On Unix the behaviour is identical to
``scripts``.

### Composition Root

The composition root (``zealfie.gui.app.run_gui``) follows a strict
initialisation order:

1. Create the singleton ``QApplication`` (must exist before any Qt
   object is constructed).
2. Instantiate ``ZeAlfieService`` with default dependencies (runtime
   layout, registry, etc.).
3. Construct ``ZeAlfieMainWindow(service=service)``.
4. Show the window and enter the Qt event loop via ``app.exec()``.

No command-line argument parsing is performed by the GUI — it always
opens the product shell window.

### Module Layout

```
src/zealfie/gui/
  __init__.py        # exports main()
  app.py             # composition root: QApplication + run_gui()
  install_worker.py  # single-install QThread worker wrapper
  main_window.py     # QMainWindow: cards, refresh, status bar, toolbar
  product_card.py    # QFrame: single-product display + action button
  presentation.py    # pure functions: state ↔ label mapping
```

Responsibilities are separated:
* ``presentation`` is pure Python — no Qt imports, no side effects.
  Testable without a QApplication.
* ``product_card`` owns a single product's widget subtree and its
  action button click handler.  Routes spawn calls through
  ``ZeAlfieService.spawn_component`` and emits install requests to the
  main window.
* ``main_window`` owns the window-level composition: scroll area,
  product cards, menu bar, toolbar, status bar, and single active
  install coordination.
* ``install_worker`` is deliberately narrow: one ``QObject`` moved to
  one ``QThread`` for one synchronous ``service.install_product(...)``
  call.  It is not a job framework, queue, thread pool, cancellation
  system, or progress reporter.

### Service Boundary

GUI widgets **never** call subprocess, pip, resolver, or deployment
functions directly.

All product interaction routes through ``ZeAlfieService``:
* ``list_products()`` — catalog descriptors;
* ``collect_product_state()`` — runtime probe results;
* ``install_product(product_id, resolver, fetcher, work_root)`` —
  synchronous backend install, called from the GUI worker thread;
* ``spawn_component(product_id)`` — non-blocking launch.

### Product Cards and State Mapping

Each ``ProductCard`` widget maps a ``ProductState`` observation to
human-readable UI:

| ``reason_code`` | State Label | Action Button |
|--------------------|--------------|---------------|
| ``RUNTIME_ABSENT`` | "No runtime — deploy a runtime first" | Installer (enabled) |
| ``RUNTIME_BROKEN`` | "Runtime broken — check or recreate" | Installer (enabled) |
| ``NOT_INSTALLED`` | "Not installed — click Installer to fetch and install" | Installer (enabled) |
| ``INSTALLED_LAUNCHABLE`` | "Ready — click Lancer to start" | Lancer (enabled) |
| ``INSTALLED_NOT_LAUNCHABLE`` | "Installed but launch contract missing" | Installer (disabled) |
| ``PROBE_FAILED`` | "Could not check — probe failed" | Installer (enabled) |

Never shows raw enum values to users.  All user-facing strings come from
the ``presentation`` module.

### Refresh

The ``_refresh()`` method on ``ZeAlfieMainWindow`` calls
``service.collect_product_state()`` and updates every card.  It is
triggered by:

* window construction (initial probe);
* **Refresh** toolbar button;
* **F5** keyboard shortcut;
* **Shell → Refresh** menu item.

Refresh does **not** call subprocess or filesystem probing from Qt —
all heavy work stays inside ``ZeAlfieService``.

Refresh is disabled/deferred while a product install is active.  After
worker success, the main window performs an authoritative
``collect_product_state()`` refresh before re-enabling the card.

### Install Path (M1-2D.5)

Clicking the **Installer** button on a not-installed product card:

1. emits ``ProductCard.install_requested(product_id)``;
2. ``ZeAlfieMainWindow`` accepts the request only if no install is
   already active;
3. the main window disables install actions globally, disables refresh,
   shows an indeterminate progress bar, and displays that cancellation
   is not yet available;
4. the main window creates one ``QThread`` + ``InstallWorker`` and
   starts the worker;
5. the worker calls the synchronous
   ``service.install_product(product_id, resolver, fetcher, work_root)``
   off the GUI thread;
6. the backend remains synchronous and owns the existing GitHub source
   resolution, archive fetch/build, dependency acquisition, runtime
   lock/planning, transactional apply, activation, and selection
   persistence;
7. dependency acquisition staging is rooted under the supplied
   ``work_root`` filesystem, not under ``/tmp``;
8. on success, the main window refreshes authoritative state and the
   card becomes **Lancer** when ``installed=True`` and
   ``launchable=True``.

The progress UI is intentionally indeterminate.  There is no
cancellation, no percentage reporting, no asyncio bridge, no generic job
manager, and no multi-product desired-state scheduler in this milestone.

### Launch Path

Clicking the **Lancer** button on a launchable product card:

1. calls ``service.spawn_component(product_id)`` —
   non-blocking, returns immediately with a ``SpawnedLaunch``;
2. the button is disabled for a 500 ms debounce period;
3. the status label shows "Launching DisplayName...";
4. on failure, the status label shows the error message (truncated to
   120 characters);
5. the button is re-enabled after the debounce.

No QThread, no asyncio, no event-loop indirection.  The child process is
spawned and ZeAlfie does not currently track its lifecycle.

### Error Handling

* Startup failure (e.g. ``collect_product_state`` raises): a
  visible error banner with red text replaces the product card area.
  The window is **never** blank on startup.
* Spawn failure: per-product status label shows the error; the button is
  re-enabled after debounce.
* Refresh failure: status bar shows "Refresh failed"; previous
  card data is preserved.

### Explicit Non-Goals for M1-2C

* No installation/download/update/deployment/activation
  (``Installer`` buttons are visible but disabled).
* No QThread, asyncio, or event-loop indirection for spawn.
* No process lifecycle management, polling daemon, or job registry.
* No cross-platform installer packaging (MSI, AppImage, DMG).
* No GPU capability display, catalogue management, or settings dialog.
* No changes to the ZeSolver repository.
* No custom widget toolkit — PySide6 stock widgets only.
* No dark/light mode toggle, theme engine, or CSS skinning.

### M1-2D.5 Delta

M1-2D.5 retires the earlier M1-2C placeholder limitation that showed
Installer but did not perform installs.  The Product Shell can now run a
single real product install from the **Installer** button without
blocking the Qt event loop, using the minimal QThread worker described
above.  The backend service and runtime transaction engine remain
synchronous and unchanged in shape.

## M1-2E — Managed Product Updates

M1-2E adds managed update semantics for products installed in the ZeAlfie
shared runtime.  The design deliberately extends the existing install and
transaction machinery instead of adding a second update engine.

### Installed Provenance

After a successful install or update, ZeAlfie records product provenance for
the newly active runtime slot.  Provenance includes:

* product id and installed version;
* source owner/repository and the mutable requested ref (for example
  ``tinystork/ZeSolver@main``);
* the exact resolved commit SHA that was built;
* the verified wheel SHA-256.

The provenance is written only after runtime activation and selection
persistence.  If provenance is missing or corrupt, update checks fail closed
as ``PROVENANCE_UNKNOWN`` rather than inventing a source or SHA.

### Read-only Update Detection

``ZeAlfieService.check_product_update(product_id, resolver=...)`` compares the
active installed provenance commit to the current commit resolved from the
recorded requested ref.  It is read-only: it does not fetch archives, build
wheels, mutate runtime slots, change selection, or launch products.

The stable update statuses are:

* ``NOT_CHECKED`` / ``CHECKING`` for UI lifecycle state;
* ``UP_TO_DATE`` when installed and resolved commits match;
* ``UPDATE_AVAILABLE`` when they differ;
* ``CHECK_FAILED`` when resolution fails;
* ``PROVENANCE_UNKNOWN`` when active provenance is unavailable.

The Product Shell uses an ``UpdateCheckCoordinator`` to run these checks in the
background and marshal results back to the GUI thread.

### Transactional Update Action

``ZeAlfieService.update_product(...)`` is a service-layer convenience around
existing install mechanics:

1. run the read-only preflight update check;
2. only if the result is ``UPDATE_AVAILABLE``, delegate to
   ``install_product(...)`` with the same resolver, fetcher, work root,
   dependency wheelhouse, probe, and progress callback;
3. for all other statuses, raise ``ProductUpdateNotApplicableError`` before
   any fetch/build/apply/provenance mutation.

The update path therefore reuses the same prepare → dependency acquisition →
plan → offline runtime install → validation → activation → selection →
provenance ordering as a fresh install.  There is no direct
``apply_deployment_plan`` call and no separate update transaction engine.

### Product Shell Update UX

When the background check reports ``UPDATE_AVAILABLE``, a product card shows a
secondary **Mettre à jour** button.  Clicking it:

1. emits ``ProductCard.update_requested(product_id)``;
2. ``ZeAlfieMainWindow`` acquires the same global install/update lock used by
   installs;
3. the existing one-shot Qt worker is created with ``operation="update"``;
4. the worker calls synchronous ``service.update_product(...)`` off the GUI
   thread and relays backend progress verbatim;
5. success refreshes authoritative product state and re-runs the read-only
   update check so the card returns to ``Up to date``;
6. launch remains the primary **Lancer** action and still goes through
   ``service.spawn_component``.

``ProductCard`` never calls ``service.update_product`` directly.  The backend
service remains Qt-free and synchronous.

## M1-2G — Host Acceleration Discovery & GPU Setup UX

M1-2G adds read-only host/GPU capability discovery and an honest GPU setup
surface.  It observes the host, derives an acceleration recommendation, and
exposes it through the service, CLI, and GUI.  It performs **no** real GPU,
CUDA toolkit, driver, or runtime installation and **no** system mutation.

### Architectural invariant

```text
OBSERVATION HostCapabilities
    -> INTERPRETATION AccelerationRecommendation
    -> SERVICE (ZeAlfieService)
    -> GUI / CLI
```

* Qt widgets never call subprocess, ``nvidia-smi``, ``/sys``, platform
  probing, or CUDA compatibility decision logic.
* System probes never know about Qt.
* Tests never depend on the real GPU of the host (probes are injectable).

### New package: ``zealfie/host/``

* ``models.py`` — ``HostCapabilities`` (OS/arch + zero/one/many ``GpuInfo``),
  tri-state ``CapabilityStatus`` (AVAILABLE/UNAVAILABLE/UNKNOWN), stable
  ``HostReasonCode`` values, ``AccelerationRecommendation`` (OFFER_SETUP /
  ALREADY_READY / NOT_APPLICABLE / BLOCKED / UNKNOWN), and the no-mutation
  ``GpuSetupIntent``.
* ``probes.py`` — ``HostProber``, a read-only, bounded collector using
  ``platform``/``sysconfig``, Linux sysfs PCI, ``/proc/driver/nvidia/version``,
  ``/dev/nvidiactl``, ``nvidia-smi --query-gpu``, and optional ``lspci``.
  Every failure becomes an UNAVAILABLE/UNKNOWN state, never an exception.
  Presence of NVIDIA hardware ≠ CUDA usability; presence of ``nvidia-smi`` ≠
  Python runtime compatibility.
* ``recommendation.py`` — pure ``recommend(HostCapabilities)`` and
  ``build_gpu_setup_intent(recommendation)``.  NVIDIA_CUDA is the only
  backend; no concrete PyTorch/CuPy/TensorFlow/Numba package is ever chosen.

### Service API

``ZeAlfieService`` adds ``collect_host_capabilities()``,
``get_acceleration_recommendation()``, and ``prepare_gpu_setup_intent()``
(which accepts an optional ``recommendation`` so the GUI can pass the exact
recommendation it already rendered, avoiding a second hardware probe).
The capability collector and recommender are injectable for hermetic tests.

### CLI

```bash
zealfie system capabilities
```

Read-only diagnostic: OS/arch, GPU(s), driver status/version, acceleration
recommendation, and reason.  Never installs or mutates anything.

### GUI

The main window shows an ``AccelerationPanel`` rendered from the service
recommendation only: compatible → "Configurer le GPU" button (routes through
``prepare_gpu_setup_intent`` and displays an honest intent, never claiming
installation success); driver blocked → details, no configure button; no
supported GPU → CPU-mode message; unknown → honest unknown message, no offer.
A probe/service failure degrades to the unknown state and never crashes the
window.

## M1-2H — Shared GPU Runtime Planning (read-only)

M1-2H answers: *if the user accepts GPU configuration, what must ZeAlfie
build and install in the shared runtime?*  It is **planning-only**: no
runtime mutation, no download, no installation, no network.  It transforms
the M1-2G observation chain into a pure, deterministic
``AcceleratedDeploymentPlan``.

### Architectural invariant

```text
HostCapabilities (M1-2G)
    -> AccelerationRecommendation (M1-2G)
    -> ProductAccelerationRequirements (catalog, declared)
    -> HardwareCompatibility (pure evaluation)
    -> AcceleratedDeploymentPlan (pure, read-only)
    -> GUI preview / CLI ``system gpu-plan``
```

**ZeAlfie never selects a concrete accelerated framework.**  No PyTorch /
CuPy / TensorFlow / Numba decision exists in production code.  Products
declare their accelerated needs as distribution-level requirements against a
known backend; ZeAlfie only evaluates declared backends and cross-product
consistency, fail-closed.

### Product acceleration contract

``products.toml`` gains an optional, strictly parsed
``[products.acceleration]`` table per product (catalog schema stays at 1 —
absence simply means "no accelerated requirements"):

* ``backend`` — must be a known backend (only ``NVIDIA_CUDA``);
* ``optional`` — bool, default true (carried for future planning);
* ``requirements[]`` — ``distribution`` (canonicalized), optional PEP 440
  ``specifier``, canonicalized ``extras``;
* ``incompatibilities[]`` — ``distribution`` + ``reason``.

Unknown keys, malformed specifiers, duplicates, and self-conflicts are
rejected at parse time (fail-closed).  The current packaged catalog declares
no acceleration for any product, so real deployments produce the honest
``NO_ACCELERATED_REQUIREMENTS`` preview and the CPU closure is preserved
unchanged.

### Compatibility evaluation

``evaluate_acceleration_compatibility(requirements_map, capabilities,
recommendation)`` is pure and deterministic.  Verdicts:

* ``SUPPORTED`` — host satisfies all declared requirements, no conflicts;
* ``BLOCKED`` — no requirements, unsupported backend, no accelerator
  hardware, driver blocked, or cross-product conflicts (exact-pin
  disagreements, pin vs excluding range, obviously disjoint simple
  ranges, requirement vs declared incompatibility);
* ``UNKNOWN`` — partial host evidence or unknown recommendation.

Fail-closed: silence never means "supported"; an unknown state never
produces an applicable plan.

### Accelerated variant catalog

``AcceleratedVariantCatalog`` is an immutable, injectable registry of
declared accelerated variants (distribution + version + backend + optional
platform + optional sha256).  Lookups are fail-closed: zero matches →
``None``, more than one match → ``AmbiguousVariantError``.  The default
catalog is **empty**: real plans stay blocked until M1-2I supplies artifact
acquisition.  A missing variant blocks the whole plan — no partial fallback,
no approximate stack.  A found variant must also satisfy the merged
specifier (evaluated with prereleases allowed): a variant that does not
satisfy it is treated as unavailable and blocks the plan with a
deterministic detail.

### Accelerated DeploymentPlan

``build_accelerated_deployment_plan(...)`` (``zealfie.acceleration.planning``)
is pure — all inputs are passed in, nothing is read from or written to the
filesystem.  The plan documents:

* ``status``: ``NO_ACCELERATED_REQUIREMENTS`` / ``PLAN_READY`` / ``BLOCKED``
  / ``UNKNOWN``;
* the full hardware compatibility verdict;
* the single acceleration backend;
* sorted ``products_concerned``;
* ``keep_products`` copied **verbatim** from provenance (product id, exact
  version, commit SHA, wheel SHA-256) — never re-resolved; products known
  only from the installed-runtime lock degrade commit/wheel SHAs to
  ``None`` (never fabricated); every entry carries a ``source`` tag —
  ``"provenance"`` or ``"installed_lock"`` — documenting which read-only
  store supplied it;
* merged ``added_requirements`` — one entry per distribution (combined
  specifier, union of extras, sorted declaring products, selected variant);
* source runtime state/slot snapshot and a descriptive ``target_runtime``;
* ``blocked`` / ``blocked_reason`` and deterministic ``closure_impact``
  lines.

The plan is deterministic: equal inputs produce equal plans.

### Service, CLI, GUI

* ``ZeAlfieService.build_accelerated_deployment_plan(...)`` — read-only
  preview; capabilities/recommendation/variant catalog are injectable for
  hermetic tests; defaults to the fail-closed empty variant catalog.
* ``zealfie system gpu-plan`` — read-only CLI preview; a blocked plan is a
  preview (exit 0), unexpected exceptions exit non-zero.
* ``AccelerationPanel`` — the "Configurer le GPU" click now appends the
  honest plan preview (pure ``gpu_plan_preview_lines`` presentation) to the
  existing detail area.  The main window stores the capabilities
  observation and the recommendation derived from it (one observation
  cycle) and the panel passes both to the plan preview, so the click never
  triggers a second hardware observation.  No install action exists; a
  planning failure degrades to an honest notice, never a crash.  The
  preview never claims an installation happened.

### Followed by M1-2I

Artifact acquisition, candidate runtime build, compatibility gate, atomic
activation, rollback, cancellation, and the service/CLI wiring are
implemented in M1-2I (next section).  The GUI install path arrives in I3.
The first real accelerated deployment on physical hardware remains behind
the human gate: a real artifact source and explicit authorization are
required.

## M1-2I — Transactional Accelerated Deployment

M1-2I turns the read-only M1-2H plan into a real, transactional deployment:
acquire concrete accelerated artifacts, extend the base ``RuntimeLock``,
build a fresh candidate runtime, run the compatibility gate, record
observational metadata, and atomically activate — without ever mutating the
active runtime.

### Engine: ``zealfie/acceleration/deployment.py``

* ``AcceleratedArtifactAcquirer`` — injectable protocol
  ``acquire(plan, work_root, cancel_check=...) -> tuple[AcquiredAcceleratedVariant, ...]``.
  ``AcquiredAcceleratedVariant`` re-verifies size and SHA-256 against the
  on-disk wheel at construction.  The production default
  (``default_accelerated_artifact_acquirer()``) ALWAYS raises
  ``AcceleratedAcquisitionUnavailable`` — fail-closed until a real,
  human-gated artifact source is configured.
* ``extend_runtime_lock_with_acceleration`` — pure lock extension: every
  base entry preserved verbatim (same objects, same order) plus one
  NON-PRIMARY entry per acquired variant, appended deterministically;
  rejects specifier violations, duplicates, unknown declaring products,
  and collisions with the base lock.
* ``apply_accelerated_deployment`` — PREPARE preflight (``PLAN_READY``,
  backend, source-slot coherence, base dependency lock present) →
  cooperative-cancellation checkpoint → RESOLVE (rebind the
  ``DeploymentPlan`` via ``dataclasses.replace`` with the extended lock) →
  delegate to the M0-8B ``apply_deployment_plan`` using the two optional
  hooks added in M1-2I: ``cancel_check`` (``DeploymentCancelledError``)
  and ``pre_activate`` (error-string gate, run strictly after the
  version-match checks and strictly before activation).  All installs go
  to the fresh candidate slot; the active pointer is never touched before
  activation; every failure preserves the old runtime;
  ``CooperativeCancellationError`` → ``cancelled=True``.
* ``AcceleratedGate`` — the default gate probes each planned accelerated
  distribution at its planned version inside the candidate venv with a
  stdlib-only script.  Backend importability is NOT tested (it cannot be
  tested without real hardware) — the human gate covers the first real
  deployment; the gate never fabricates a success.
* ``AcceleratedSlotMetadataStore`` — observational, slot-keyed, atomic
  record (``state_dir/accelerated-metadata.json``: backend + variant
  ``(distribution, version, sha256)`` triples).  Written inside
  ``pre_activate`` under the candidate slot id (slots are created at
  their final path and never renamed).  Drives no install / rollback /
  KEEP decision.

### Service wiring

``ZeAlfieService.install_accelerated_runtime(...)``:

1. builds the read-only plan when not supplied (M1-2H preview path); a
   non-``PLAN_READY`` plan returns ``success=False, phase=PREPARE`` with an
   honest reason — no acquisition, no runtime work (the honest TINYDEBIAN
   default today: no product declares GPU requirements and the variant
   catalog is empty);
2. materializes the base full-state plan with KEEP semantics — every
   managed product re-prepared at its exact installed version/commit SHA
   through the M1-2F KEEP machinery (``prepare_product_artifact_at_commit``,
   never a mutable ref); synthetic/hermetic callers inject a
   ``full_state_provider`` of local verified artifacts (offline);
3. requires the base plan to carry a ``dependency_lock`` to extend —
   otherwise it fails with a clear reason before any candidate slot
   creation;
4. acquires accelerated artifacts (fail-closed default acquirer,
   cooperative cancellation honoured);
5. delegates to ``apply_accelerated_deployment`` with
   ``declaring_distributions`` derived from the product catalog, the
   default gate, and a metadata store bound to the runtime layout;
6. on success writes NO product provenance, selection, or installed-lock
   records — products are unchanged; the engine's accelerated metadata
   record is the only new persistent state.  The method never installs
   into the active slot.

### CLI

``zealfie system gpu-install`` is a fail-closed stub: it prints the honest
human-gate message (no accelerated artifact source configured; a real GPU
deployment requires explicit authorization and a configured artifact
source) and exits non-zero without any acquisition, planning, or mutation.

### GUI worker, honest progress, cooperative cancellation (I3)

The panel install path is wired in I3, entirely behind the preview:

* ``zealfie/gui/presentation.py`` — pure, Qt-free view logic.
  ``accelerated_phase_label(phase)`` maps every
  ``AcceleratedDeploymentPhase`` to a deterministic English label
  (PREPARE → Preparation, ACQUIRE → Download, RESOLVE → Dependency
  resolution, BUILD → Runtime build, VALIDATE/GATE → Validation,
  PERSIST/ACTIVATE → Activation, COMPLETED → Completed).
  ``accelerated_install_view(events)`` reduces the observed event stream
  (backend ``InstallProgress`` events, raw phases, terminal results) to
  ``(label, percent | None, done)``: the percent comes ONLY from the
  canonical ``PHASE_PERCENT`` table per ``InstallPhase`` (never invented,
  never read from the event), ``done`` is ``True`` only for COMPLETED,
  ``percent=100`` is never returned unless ``done``, and a
  failed/cancelled result clears the percent (no fake progress).
* ``zealfie/gui/accelerated_install_worker.py`` — ``AcceleratedInstallWorker``
  (QObject moved to a QThread, mirroring ``install_worker``) runs the one
  synchronous ``install_accelerated_runtime(...)`` call off the GUI thread,
  relays reduced ``progress(str label, int|None percent)`` pairs, and
  emits ``finished`` with the ``AcceleratedDeploymentResult`` exactly once.
  ``cancel()`` sets a ``threading.Event``; the injected ``cancel_check``
  raises ``CooperativeCancellationError`` once set, so the service returns
  a ``cancelled=True`` result at its next checkpoint — cooperative, never
  a thread kill.
* ``zealfie/gui/acceleration_panel.py`` — state machine
  preview → [Installer] → progression → result.  The configure click
  builds the plan ONCE (stored recommendation + capabilities, no second
  observation); the Installer button appears ONLY for a freshly built
  ``PLAN_READY`` plan — the honest default on any fail-closed plan is
  no button at all.  Clicking Installer disables the actions, starts the
  worker, shows the phase label + canonical percent, and shows Cancel
  only while the worker runs AND activation has not been reached
  (cancelling after the atomic activation point is a hidden no-op).
  Terminal states are honest: success → "Accelerated runtime ready"
  (100% only at the real COMPLETED), failure → the result's reason,
  cancellation → an explicit cancelled message with the previous runtime
  preserved.  All service methods are accessed via ``getattr`` (graceful
  degrade, as elsewhere in the panel).

All of it is tested hermetically with fake services (offscreen Qt, no
GPU, no network, no real install).  The first REAL accelerated deployment
on physical hardware remains behind the human gate: a real artifact
source and explicit authorization are required; no production default
ever fabricates one.
