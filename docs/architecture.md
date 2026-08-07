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

``RuntimeLayout`` defines centralised paths: ``root``, ``current``, and
``staging`` (reserved).  The production location is derived from
platform-appropriate directories (XDG on Linux, Application Support on
macOS, LocalAppData on Windows) and can be overridden via
``ZEALFIE_RUNTIME_ROOT`` or an explicit *root* parameter.

``RuntimeState`` captures the coarse lifecycle: ``ABSENT``, ``READY``,
``BROKEN``.  ``RuntimeStatus`` is an immutable snapshot with the resolved
Python path, version, and a stable ``RuntimeReasonCode``.

``SharedRuntime`` is the top-level manager: ``status()`` inspects the
runtime, ``create()`` builds a venv idempotently (refusing to destroy a
broken runtime), and ``install_local_wheel()`` inspects the wheel,
installs offline, and post-validates via an external probe.

Install outcomes are structured: ``INSTALLED``, ``ALREADY_INSTALLED``,
``VERSION_MISMATCH``, ``FAILED``.

``probe_runtime_distribution()`` runs a small standard-library-only
script inside the runtime's Python and returns structured JSON.
No application code is imported during probing.

The persistent runtime is distinct from both the development ``.venv``
and the test-only ``TemporaryVenv``.

A future ``current/staging`` switch mechanism is architecturally
planned but not yet implemented.

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
