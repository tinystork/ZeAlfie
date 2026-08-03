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

At this stage, ZeAlfie does not yet:

* detect external components;
* configure external components;
* install external components;
* update external components;
* launch external components;
* manage astronomical catalogues;
* provide a graphical interface.

The current implementation validates only the package structure, application startup, CLI entry points, version reporting, and basic system status reporting.

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
│       └── cli.py
└── tests/
    ├── test_cli.py
    └── test_startup.py
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

Future discovery, status, version, capability, and launching logic for managed ZeSoftware applications.

### `diagnostics/`

Future operating-system, Python runtime, CPU, GPU, CUDA, memory, storage, and dependency checks.

### `resources/`

Future management and verification of shared astronomical resources such as ASTAP databases and ZeBlind indexes.

### `runtime/`

Future management of the shared Python runtime and controlled virtual environment.

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

## Evolution

This document is intentionally provisional.

Its structure, terminology, component model, runtime strategy, and module boundaries may evolve as ZeAlfie progresses from a minimal experimental launcher to a stable cross-platform application manager.

Architectural changes should be driven by validated requirements and implementation experience rather than premature generalisation.
