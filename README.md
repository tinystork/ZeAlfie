# ZeAlfie

ZeAlfie means **Astronomy Launcher For Imaging Engines**.

It is also the **Astronomical Little Fellow Integrating Everything**.

ZeAlfie is the common launcher and runtime manager for the ZeSoftware imaging
ecosystem. It provides one place to install, update, launch, and manage the
supported applications while keeping their runtime dependencies isolated from
the ZeAlfie development environment.

## Current status

ZeAlfie **0.0.8** is an experimental but functional release.

The current version provides:

- a persistent, slot-based shared runtime;
- transactional deployment with rollback support;
- a PySide6 graphical Product Shell;
- managed product installation, update, and launch workflows;
- runtime and product state probing without importing application code;
- GPU capability inspection and accelerated-runtime planning;
- a transactional self-update mechanism for packaged ZeAlfie installations;
- English and French GUI support.

ZeAlfie is still under active development. The runtime architecture and update
machinery are usable today, but the end-user installation experience is not yet
final.

## Installation

### Current source installation

For now, source-based installations are intended for testers and developers.

Clone the repository, enter its root directory — the directory containing
`pyproject.toml` — then create and activate a Python virtual environment.

#### Linux

```bash
cd ZeAlfie
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

#### Windows PowerShell

```powershell
cd ZeAlfie
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
```

The installation command is meant to be used exactly as written:

```bash
python -m pip install -e .
```

- `.` means **install the Python project located in the current directory**.
  Run the command from the ZeAlfie repository root, where `pyproject.toml`
  is located.
- `-e` means **editable install**. ZeAlfie runs directly from the checked-out
  source tree, so code changes are immediately visible to the virtual
  environment without reinstalling the package.

After installation, start the graphical interface with:

```bash
zealfie-gui
```

Editable/source installations are development or test installations.
ZeAlfie's self-update mechanism does not replace or update a Git source
checkout.

### Development dependencies

If you intend to run the test suite or work on ZeAlfie itself, install the
development dependencies as well:

```bash
python -m pip install -e ".[dev]"
```

This command is also meant to be used exactly as written:

- `.` still means the project in the current directory;
- `[dev]` asks pip to install ZeAlfie's optional **development dependency
  group** in addition to the normal runtime dependencies;
- the square brackets are literal syntax — `dev` is not a placeholder and
  should not be replaced with a path or directory name.

### Planned end-user installers

Standalone installers for **Windows** and **Linux** are planned.

The goal is to provide a normal end-user installation that does not require
Git, pip, or a development virtual environment. Those installers will also
integrate ZeAlfie's self-update mechanism into the normal graphical experience.

## Launching ZeAlfie

Start the graphical Product Shell with:

```bash
zealfie-gui
```

The command-line interface remains available as:

```bash
zealfie
```

or:

```bash
python -m zealfie
```

Check the installed ZeAlfie version with:

```bash
zealfie --version
```

## Product Shell

The Product Shell is the main graphical interface.

It:

- displays the known ZeSoftware products as individual cards;
- shows whether each product is installed, managed, launchable, or requires
  attention;
- installs supported products into the shared runtime;
- checks for and applies supported product updates;
- launches installed products through their public launch contracts;
- provides a single **Refresh** action, also available with **F5**;
- displays runtime and hardware-acceleration status;
- supports live English/French language switching;
- isolates product or probe failures instead of crashing the whole shell.

The GUI entry point is declared as `zealfie-gui` under
`[project.gui-scripts]`.

## Updating ZeAlfie

Packaged ZeAlfie installations can update themselves transactionally.

The update flow is deliberately split into three steps:

```bash
zealfie self-update check --channel stable
zealfie self-update stage --channel stable
zealfie self-update apply
```

### `check`

```bash
zealfie self-update check --channel stable
```

Read-only. Resolves the selected release channel and reports whether a newer
ZeAlfie version is available.

### `stage`

```bash
zealfie self-update stage --channel stable
```

Acquires, builds, and verifies the candidate update, then records it as pending.

The currently installed ZeAlfie version is **not** replaced during staging.

### `apply`

```bash
zealfie self-update apply
```

Applies the previously staged and verified update.

The ZeAlfie GUI should not be running while the update is applied. On platforms
that require it, activation is handed off to a separate updater process so the
running ZeAlfie process does not overwrite itself.

After a successful update:

```bash
zealfie --version
zealfie self-update check --channel stable
```

should report the new version and `UP_TO_DATE`.

### Beta channel

Testers can explicitly use the beta channel:

```bash
zealfie self-update check --channel beta
zealfie self-update stage --channel beta
```

The stable channel remains the default and recommended channel.

The current CLI workflow is primarily a development/test surface. Future
Windows and Linux installers are intended to expose the same transactional
update engine through a normal graphical update flow.

## Runtime model

ZeAlfie keeps three concepts separate by design:

- **development venv (`.venv`)** — used to develop and test ZeAlfie itself;
- **shared runtime** — persistent runtime managed by ZeAlfie for installed
  products;
- **temporary environments** — short-lived environments used for builds,
  validation, and hermetic tests.

The shared runtime is stored in a platform-appropriate user data location and
uses slot-based activation.

A deployment is prepared and validated in a candidate slot before activation.
The previous known-good slot can be retained for rollback and lifecycle
management.

## Product and runtime commands

Inspect the shared runtime:

```bash
zealfie runtime status
```

Inspect all known products:

```bash
zealfie products
```

Inspect a single product:

```bash
zealfie products zesolver
```

Install a product from its configured stable channel:

```bash
zealfie install zesolver --channel stable
```

Launch a managed component:

```bash
zealfie launch zesolver
```

Inspect host capabilities:

```bash
zealfie system capabilities
```

Preview the GPU deployment plan without changing the runtime:

```bash
zealfie system gpu-plan
```

The preview is read-only.

## Runtime lifecycle

Create the shared runtime if it does not exist:

```bash
zealfie runtime create
```

Preview safe runtime garbage collection:

```bash
zealfie runtime gc-plan
```

Apply safe runtime garbage collection:

```bash
zealfie runtime gc
```

Roll back to the previous runtime slot when available:

```bash
zealfie runtime rollback
```

Runtime mutations are serialized so concurrent writers cannot silently modify
the managed runtime at the same time.

## Offline deployment

ZeAlfie also retains an explicit offline deployment path for controlled and
hermetic workflows.

### Offline release directory convention

A release directory contains one trusted manifest per component and the wheel
artifacts referenced by those manifests:

```text
release_dir/
  <component_id>.toml
  <wheel_filename>.whl
```

Rules:

1. every required component manifest must exist at the top level;
2. each manifest's `component_id` must match its filename stem;
3. referenced wheel artifacts live at the top level;
4. unknown manifests are rejected;
5. no recursive scan, fallback names, or heuristic discovery is used.

### Preview an offline deployment

```bash
zealfie runtime plan --release-dir PATH
```

This command is read-only. It resolves manifests and artifacts, validates the
candidate state, and reports the planned actions.

### Apply an offline deployment

```bash
zealfie runtime apply --release-dir PATH
```

`runtime apply` resolves and plans again at execution time rather than trusting
a previously printed plan.

### Roll back

```bash
zealfie runtime rollback
```

Rollback switches back to the previous valid runtime slot when one is
available.

## Architecture notes

`ZeAlfieService` is the application-level orchestration boundary for runtime,
product, launch, deployment, and update operations.

Important design principles include:

- products remain independently usable outside ZeAlfie;
- ZeAlfie interacts with products through public metadata and launch contracts;
- mutable remote refs are resolved to immutable identities before activation;
- candidate artifacts are verified before they become active;
- activation is transactional;
- failures in optional integrations are isolated;
- ZeAlfie does not fabricate missing provenance for legacy runtime state.

## Development

Activate the repository environment:

```bash
source .venv/bin/activate
```

Run the focused test suites appropriate to the change being made. For example:

```bash
pytest -q tests/test_i18n.py
pytest -q tests/test_gui.py
```

Build a local wheel with:

```bash
python -m pip wheel --no-deps . -w dist
```

The development virtual environment is not the shared product runtime and
should not be treated as an end-user ZeAlfie installation.
