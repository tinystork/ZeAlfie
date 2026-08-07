# ZeAlfie

ZeAlfie means **Astronomy Launcher For Imaging Engines**.

It is also the **Astronomical Little Fellow Integrating Everything**.

## Version 0 status

ZeAlfie is experimental. Version 0.0.5 provides an installable package, a minimal CLI, local component inspection, a packaged manifest, and a validated local witness cycle.

Key capabilities:

* building wheels from local sources;
* inspecting wheel archives without executing code;
* **persistent shared runtime** with platform-appropriate user location;
* runtime states: ``ABSENT``, ``READY``, ``BROKEN``;
* idempotent runtime creation (create twice = no-op);
* offline local wheel installation with pre- and post-validation;
* external Python metadata probe (no application code imported);
* temporary isolated environments for hermetic testing;
* structured launch plans and controlled subprocess execution.

Concepts kept distinct by design:

* **dev venv** (``.venv``) — for development only;
* **shared runtime** — persistent, managed by ZeAlfie;
* **temporary venv** — test-only, cleaned up after use.

ZeAlfie does not install, download, update, or launch real ZeSoftware components yet. The witness cycle proves the entire pipeline but uses only a controlled local test fixture.

## Development install

Use the repository virtual environment:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Commands

```bash
python -m zealfie
zealfie
```

```bash
zealfie --version
```

```bash
zealfie status
```

`status` reports the current ZeAlfie runtime and the known managed components from the local packaged manifest.

`Installed` means the component's Python distribution is present in the active Python environment metadata. A component can be known by the manifest but not installed in the current environment.

`Launch contract` means the installed distribution declares a public entry point whose group and name exactly match the contract ZeAlfie knows how to handle. It does not mean the application has been launched or that its runtime dependencies, GUI, catalogues, GPU, or resources have been validated.

### Shared runtime commands

```bash
zealfie runtime status
```

Reports the state (``ABSENT``, ``READY``, ``BROKEN``), location, Python path, and reason code of the persistent shared runtime.

```bash
zealfie runtime create
```

Creates the shared runtime at its platform-appropriate location if absent. Idempotent: running it again on a ``READY`` runtime does nothing.

For M0-5 the runtime hosts only the controlled witness fixture. No real ZeSoftware components are installed.

You can also inspect a known component directly:

```bash
zealfie status zesolver
```
