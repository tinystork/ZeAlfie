# ZeAlfie

ZeAlfie means **Astronomy Launcher For Imaging Engines**.

It is also the **Astronomical Little Fellow Integrating Everything**.

## Version 0 status

ZeAlfie is experimental. Version 0.0.6 introduces a slot-based runtime
architecture with staged transactions and rollback.

Key capabilities:

* building wheels from local sources;
* inspecting wheel archives without executing code;
* **persistent shared runtime** with platform-appropriate user location;
* runtime states: ``ABSENT``, ``READY``, ``BROKEN``;
* idempotent runtime creation (create twice = no-op);
* offline local wheel installation with pre- and post-validation;
* external Python metadata probe (no application code imported);
* temporary isolated environments for hermetic testing;
* structured launch plans and controlled subprocess execution;
* **offline deployment planning** (M0-9.1) — resolve a deterministic
  release directory into a read-only deployment plan.
* **offline deployment apply + rollback** (M0-9.2) — orchestrate
  full-state apply and reversible rollback via the application service.
* **offline deployment CLI** (M0-9.3) — plan, apply, and rollback from
  the terminal using the same application service.

Concepts kept distinct by design:

* **dev venv** (``.venv``) — for development only;
* **shared runtime** — persistent, managed by ZeAlfie;
* **temporary venv** — test-only, cleaned up after use.

## Product Shell (M1-2C)

ZeAlfie 0.0.6 ships a **PySide6 graphical product shell** for browsing
and launching managed products.

```bash
zealfie-gui
```

The product shell:
* displays all known products as individual product cards (with managed/installed/launchable state);
* shows human-readable state labels derived from runtime probe results;
* provides a **Lancer** button for each launchable product;
* includes a **Refresh** toolbar button (or F5) to re-probe runtime state;
* shows a visible error banner on startup if state collection fails.
* **depends on PySide6** — declared as a runtime dependency in `pyproject.toml`.

The GUI exposes a single entry point `zealfie-gui` under
`[project.gui-scripts]`, keeping the existing CLI under
`[project.scripts]`.

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

`status` reports the current ZeAlfie runtime and the known components from the local packaged manifest.

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

## Offline release directory convention (M0-9.1)

A deterministic, minimal, local convention for offline release
directories.  Used by the deployment planning service to resolve a
complete desired runtime state without network access.

Layout:

```
release_dir/
  <component_id>.toml      -- one release manifest per component
  <wheel_filename>.whl     -- wheel artifacts at top level
```

Rules:

1. For every *component_id* in the trusted component registry, the
   file ``<component_id>.toml`` MUST exist at the top level.
2. Each manifest's declared ``component_id`` MUST match its
   filename stem.
3. Wheel artifacts referenced by manifests live at the top level
   of the release directory.
4. Any ``.toml`` file whose stem does not match a known component
   id is rejected (fail-closed).
5. No recursive scan, no fallback names, no heuristic discovery.

### Deployment planning service

```python
from zealfie.app import ZeAlfieService

service = ZeAlfieService()
plan = service.plan_offline_deployment(release_dir)
```

``plan_offline_deployment`` is **read-only** — it does not mutate the
filesystem or shared runtime.  It resolves all manifests, verifies
every artifact, builds a ``DesiredRuntimeState``, probes the current
runtime, and returns a ``DeploymentPlan`` describing INSTALL/KEEP/BLOCKED
for each component.

The ``runtime plan`` command remains a preview: ``runtime apply`` always
resolves and plans fresh instead of consuming a previous plan output.

### Runtime plan, apply, and rollback commands (M0-9.3)

```bash
zealfie runtime plan --release-dir PATH
```

Read-only preview.  Resolves the offline release directory, builds a
``DeploymentPlan`` from the current runtime state, and prints planned
actions/reasons/versions for each component.  Returns 0 for a
successfully built plan, 1 when the plan is blocked, or 4 on
``OfflineReleaseError`` (stderr, no traceback).  Does **not** mutate
the shared runtime.

```bash
zealfie runtime apply --release-dir PATH
```

Applies the offline deployment.  Re-plans fresh at call time — a
plan from a previous ``runtime plan`` is never consumed or persisted.
Prints success/failure with active/previous slot ids.  Returns 0 on
success, 3 when the ``DeploymentResult`` reports failure, or 4 on
``OfflineReleaseError`` (stderr, no traceback).

```bash
zealfie runtime rollback
```

Rolls back the shared runtime to the previous active slot.  Prints the
resulting runtime status using the existing runtime status formatting.
Returns 0 when the resulting state is ``READY``, or 3 otherwise.

### Application service injection for tests

The CLI constructs services via a private ``_make_service()`` factory
that returns ``ZeAlfieService(registry=default_registry(),
runtime=SharedRuntime(default_runtime_layout()))``.  Tests can
monkeypatch ``zealfie.cli._make_service`` to inject a controlled
registry and temp runtime — no production runtime is touched.

### Offline deployment orchestration service

``ZeAlfieService`` (``zealfie.app.service``) is the application-level
orchestrator for offline deployment:

* ``resolve_offline_release_set(release_dir)`` — read-only, resolves
  the complete desired runtime state from a release directory.
* ``plan_offline_deployment(release_dir)`` — read-only, builds a
  ``DeploymentPlan`` from the current runtime status.
* ``apply_offline_deployment(release_dir)`` — re-plans fresh, then
  applies via ``apply_deployment_plan`` (transactional, mutates the
  shared runtime).
* ``rollback_runtime()`` — delegates to ``SharedRuntime.rollback()``.

Errors during release resolution are surfaced as ``OfflineReleaseError``,
which wraps all lower-level failures (missing manifests, parse errors,
artifact verification, extra unknown manifests).
