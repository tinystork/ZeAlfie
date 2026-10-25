# ZeAlfie 0.1.4-rc1 — Windows x64 + ARM64 installer candidate

Status: local release-candidate implementation. The GitHub Actions workflow is manual-only and does not publish a Release.

## Artifact contract

The workflow `.github/workflows/windows-installer-build.yml` builds two independent artifacts:

- `ZeAlfie-Setup-0.1.4-rc1-windows-x64.exe`
- `ZeAlfie-Setup-0.1.4-rc1-windows-arm64.exe`

Each carries its own exact CPython profile and offline wheelhouse lock:

| Target | CPython profile | Wheelhouse lock | Runner |
| --- | --- | --- | --- |
| x64 | `reproducibility.x64.toml` | `wheelhouse.x64.lock.toml` | `windows-latest` |
| ARM64 | `reproducibility.arm64.toml` | `wheelhouse.arm64.lock.toml` | `windows-11-arm` |

The profiles are never selected from an arbitrary user-provided architecture. The workflow matrix supplies one of the two committed profiles; lock parsing rejects unknown Windows targets and native wheels from the opposite architecture.

## Native claim and Inno exception

The installed payload under `{app}\python` and `{app}\appenv` is native to the selected target. `payload_arch_audit.py` recursively inspects every `.exe`, `.dll` and `.pyd` in those trees and requires `IMAGE_FILE_MACHINE_AMD64` or `IMAGE_FILE_MACHINE_ARM64` exactly.

Inno Setup 6.7.3's Setup program itself remains a 32-bit x86 binary. On Windows ARM it runs under Microsoft's emulation. It is deliberately excluded from the native-payload claim; the provenance artifact records `inno_setup_loader_native_arm64=false`.

## ARM64 dependency closure

The ARM64 profile uses the pinned CPython 3.13.15 `aarch64-pc-windows-msvc` install-only archive and PySide6 6.11.2 `win_arm64` wheels. Universal dependencies retain the x64 profile's exact versions. CUDA remains unavailable/fail-closed because the acceleration catalog has no `win_arm64` closure.

This candidate qualifies ZeAlfie itself. It does not claim that every managed ZeSoftware product is ARM64-ready.

## Workflow gates

Both lanes perform:

1. exact runner-architecture check;
2. wheel build (`zealfie-0.1.4rc1-py3-none-any.whl`);
3. architecture-specific locked wheelhouse acquisition with SHA-256 verification;
4. offline dependency preflight;
5. pinned CPython archive download, SHA-256 verification and extraction;
6. Inno compilation with strict architecture directives;
7. silent install;
8. installed provenance, CLI, mutation-free self-update command, Qt offscreen GUI and native PE audit;
9. side-effect audits;
10. complete silent uninstall;
11. architecture-labelled installer, checksum, native-audit JSON and provenance upload as Actions artifacts.

The workflow has `workflow_dispatch` only. It does not tag, sign, push, create a GitHub Release or publish an installer.

## Remaining human/release gates

- real GitHub-hosted x64 and ARM64 workflow runs;
- review of both artifacts and their provenance/checksums;
- optional physical Windows ARM64 user-machine witness;
- explicit authorization before tag `v0.1.4-rc1` and GitHub Pre-release creation;
- code signing remains unavailable, so any published RC is unsigned and may trigger SmartScreen.
