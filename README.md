# ZeAlfie

ZeAlfie means **Astronomy Launcher For Imaging Engines**.

It is also the **Astronomical Little Fellow Integrating Everything**.

## Version 0 status

ZeAlfie is experimental. Version 0.0.2 provides an installable package, a minimal command-line entry point, a version option, and local inspection of known Python component distributions.

ZeAlfie does not install, download, update, configure, or launch ZeSoftware components yet.

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

`status` reports the current ZeAlfie runtime and the known managed components.

`Installed` means the component's Python distribution is present in the active Python environment metadata.

`Launchable` means the installed distribution declares a public entry point supported by ZeAlfie. ZeSolver may be detected as installed but not launchable until it publishes a compatible public GUI launch entry point.

You can also inspect a known component directly:

```bash
zealfie status zesolver
```
