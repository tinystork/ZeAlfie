# ZeAlfie

ZeAlfie means **Astronomy Launcher For Imaging Engines**.

It is also the **Astronomical Little Fellow Integrating Everything**.

## Version 0 status

ZeAlfie is experimental. Version 0.0.4 provides an installable package, a minimal command-line entry point, a version option, local inspection of known Python component distributions, a packaged local manifest describing expected components, and a validated local witness cycle:

* building wheels from local sources;
* inspecting wheel archives without executing their code;
* creating temporary isolated virtual environments;
* installing wheels offline from local paths only;
* detecting installed components through real Python metadata;
* preparing structured launch plans (no shell strings);
* executing a controlled witness subprocess;
* capturing stdout, stderr, return code, and timeout;
* cleaning up temporary environments after validation.

ZeAlfie does not install, download, update, or launch real ZeSoftware components yet. The witness cycle proves the pipeline but uses only a controlled local test fixture.

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

ZeSolver may be detected as installed but with `Launch contract: unavailable` until it publishes a compatible public GUI entry point.

ZeAlfie still performs no network access, managed installation, update, configuration, or real component launch.

You can also inspect a known component directly:

```bash
zealfie status zesolver
```
