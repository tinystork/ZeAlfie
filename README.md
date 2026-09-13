# ZeAlfie

ZeAlfie means **Astronomy Launcher For Imaging Engines**.

It is also the **Astronomical Little Fellow Integrating Everything**.

ZeAlfie is the common launcher and runtime manager for the ZeSoftware imaging
ecosystem. It provides one place to install, update, launch, and manage the
supported applications while keeping their runtime dependencies isolated from
the ZeAlfie development environment.

## Current status

ZeAlfie **0.1.0** is the first release where the Product Shell is the main
user-facing front door of the product.

The current version provides:

- a persistent, slot-based shared runtime;
- transactional deployment with rollback support;
- a PySide6 graphical Product Shell (home page + Settings/System zone);
- managed product installation, update, and launch workflows;
- runtime and product state probing without importing application code;
- GPU capability inspection and accelerated-runtime planning;
- GPU onboarding for compatible products (never a silent install);
- a transactional self-update mechanism for packaged ZeAlfie installations;
- English and French GUI support.

The runtime architecture and update machinery are usable today and validated by
real Windows witnesses.  ZeAlfie is still under active development.

> **Accepted product deviation (ZA-M1-5):** the visible **Refresh** action was
> removed from the main interface by product decision (it was not demonstrated
> as necessary, and its removal simplifies the shell).  The F5 shortcut and the
> refresh logic remain in the codebase; this deviation is intentional and is
> not to be reverted to satisfy earlier mission wording.

## Installation

### Python requirement for pip/source installations

ZeAlfie's Python package requires **Python 3.11 or newer**.

This requirement matters whenever ZeAlfie is installed or run through Python,
including:

- installation from a wheel with `pip`;
- installation from a Git/source checkout;
- editable development installations;
- running the CLI through `python -m zealfie`.

A future standalone installer or application bundle may provide or manage its
own Python runtime. In that case, users do not need to select a Python
interpreter manually. This is different from a wheel or source installation,
where the Python interpreter used to create the environment must already meet
ZeAlfie's requirement.

> **Important:** a virtual environment does not upgrade Python.
>
> The `.venv` uses the interpreter that created it. Upgrading `pip`,
> `setuptools`, or `wheel` inside the environment does **not** change its Python
> version. If the system default points to an older Python, create the virtual
> environment explicitly with a supported interpreter.

Python 3.12 is used in some examples below because it is a convenient current
choice, but it is **not** a special ZeAlfie requirement. Any supported Python
version satisfying **Python >= 3.11** may be used.

### User installation (packaged)

The user-facing Python installation path is a **packaged (non-editable)
install** of a built wheel. Packaged wheel installations support ZeAlfie's
transactional self-update flow from the GUI or the CLI.

Before installing a wheel, verify that the active Python interpreter is
supported:

```bash
python --version
```

It must report Python 3.11 or newer.

Then install the wheel:

```bash
python -m pip install zealfie-0.1.0-py3-none-any.whl
```

After installation, start the graphical interface with:

```bash
zealfie-gui
```

Packaged installations can update themselves through the GUI self-update flow
(check → consent → restart) or through the CLI commands documented under
[Updating ZeAlfie](#updating-zealfie). Standalone Windows/Linux installers are
planned to expose the same flow without requiring users to manage Python or pip
themselves.

### Source installation (developers and testers)

Source-based installations are **development/test installations only**. They
are not the user-facing path and are never updated by ZeAlfie's self-update
mechanism.

Clone the repository and enter its root directory — the directory containing
`pyproject.toml` — before creating the virtual environment.

The key point is to select the Python interpreter **before** creating `.venv`.

#### Linux

First check the available interpreter:

```bash
python3 --version
```

If it already reports Python 3.11 or newer, it can be used directly:

```bash
cd ZeAlfie
python3 -m venv .venv
source .venv/bin/activate

python --version
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
```

If the system default is older than Python 3.11, install an additional
supported Python interpreter and create the environment explicitly with it.
For example, with Python 3.12:

```bash
cd ZeAlfie
rm -rf .venv
python3.12 -m venv .venv
source .venv/bin/activate

python --version
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
```

The `python --version` command after activation should still report the
supported interpreter used to create the environment.

> **Do not replace the operating system's Python just for ZeAlfie.**
>
> On Linux, the system Python may be used by the distribution itself. If it is
> too old for ZeAlfie, install a newer interpreter alongside it and use that
> interpreter only to create the ZeAlfie virtual environment.
>
> Package names and installation methods vary by distribution. On Ubuntu, for
> example, `python3.12` and `python3.12-venv` may be installed when they are
> available for the release in use. Older Ubuntu releases may require an
> additional trusted Python package source. That is an operating-system setup
> choice, not a ZeAlfie runtime requirement.

#### Windows PowerShell

On Windows, the Python Launcher is the clearest way to see which Python
versions are installed:

```powershell
py -0p
```

If Python 3.11 or newer is available, create the environment explicitly with
that interpreter. For example, with Python 3.12:

```powershell
cd ZeAlfie
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

python --version
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
```

Python 3.12 is only an example. If Python 3.11 is the supported interpreter
installed on the machine, this is equally valid:

```powershell
py -3.11 -m venv .venv
```

After activation, `python --version` should report Python 3.11 or newer.

If the `py` launcher is not available, a specific supported `python.exe` may be
used directly to create the environment instead.

### Why the interpreter choice matters

The following sequence is **not sufficient** if `python` itself is too old:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

`pip` may become newer, but the virtual environment still contains the same
Python version that created it.

The safe sequence is therefore:

1. identify or install a supported Python interpreter;
2. create `.venv` with that exact interpreter;
3. activate `.venv`;
4. verify `python --version`;
5. upgrade packaging tools;
6. install ZeAlfie.

This keeps ZeAlfie isolated without modifying the operating system's Python
installation.

### Editable installation

The source installation command is meant to be used exactly as written:

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

Editable/source installations are development or test installations. ZeAlfie's
self-update mechanism does not replace or update a Git source checkout.

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
- keeps technical details (runtime, slots, GPU plan) in the Settings/System
  page rather than the home page;
- offers GPU onboarding for newly installed GPU-capable products;
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

The current CLI workflow is primarily a development/test surface.  Packaged
GUI installations expose the same transactional update engine through the
normal graphical self-update flow.

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

The development environment must also use Python 3.11 or newer.

Activate the repository environment on Linux/macOS:

```bash
source .venv/bin/activate
python --version
```

Or on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

If the reported Python version is too old, deactivate/remove the environment
and recreate it with a supported interpreter as described under
[Source installation](#source-installation-developers-and-testers). Installing
newer packages into an old virtual environment does not upgrade its Python
interpreter.

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
