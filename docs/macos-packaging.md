# ZeAlfie macOS ARM64 unsigned application bundle (ZA-MAC-BOOT-01)

Status: **implemented, hermetically validated on Linux, NOT yet executed on a
real macOS runner.** This document describes the first reproducible native
**arm64** unsigned ``ZeAlfie.app`` bundle and the manual GitHub Actions
workflow that builds it. It is an **engineering RC** — unsigned, not
notarised, not a release, no DMG, no version bump, no self-update.

| | |
| --- | --- |
| Bundle layout contract | `packaging/macos/macpack.py` |
| PBS pin (substrate) | `packaging/macos/reproducibility.toml` |
| Offline wheelhouse lock | `packaging/macos/wheelhouse.lock.toml` |
| Wheelhouse acquisition | `packaging/macos/acquire_wheelhouse.py` |
| Mach-O inspection / ARM64 audit / lipo thinning | `packaging/macos/macho.py` |
| Bundle builder | `packaging/macos/build_app.py` |
| POSIX launcher | `packaging/macos/launcher/ZeAlfie` |
| Offscreen GUI smoke | `packaging/macos/gui_smoke_offscreen.py` |
| Runtime witnesses | `packaging/macos/witnesses.py` |
| CI witness (manual) | `.github/workflows/macos-packaging-arm64.yml` |
| Hermetic tests | `tests/test_macos_packaging.py` |

## Architecture (accepted; do not redesign)

```
ZeAlfie.app/
  Contents/
    Info.plist                      # exact pinned contract (see below)
    MacOS/ZeAlfie                   # POSIX launcher SCRIPT (never Mach-O)
    Resources/
      python/                       # private pinned CPython 3.13.15 (PBS
                                    #   install_only, aarch64-apple-darwin)
        bin/python3.13
        lib/…                       # full stdlib + bundled pip
      app/                          # ZeAlfie + deps, installed with the
                                    #   private python (no venv), from the
                                    #   locked wheelhouse, offline
      zealfie.icns
```

* **PBS only.** The private interpreter is the python-build-standalone
  `install_only` tarball
  `cpython-3.13.15+20260901-aarch64-apple-darwin-install_only.tar.gz`,
  obtained from the official GitHub release and **SHA-256-verified before
  extraction** (`b9054a9d…23ee`, 25 293 188 bytes). Acquisition fails closed
  on any mismatch; no other Python/version/triple is ever substituted.
* **No application venv.** The wheelhouse is installed **directly** into
  `Contents/Resources/app` with the private interpreter
  (`pip --target … --no-index --find-links <wheelhouse>`). PyPI is never
  contacted; there is no live dependency resolution.
* **No host dependency.** Nothing at runtime uses the system Python,
  Homebrew, the source checkout, a runner venv, or the current working
  directory.
* **Managed-product runtime unchanged.** Product payloads stay under
  `~/Library/Application Support/zealfie/runtime`; the app bundle is
  conceptually immutable and nothing in it redirects managed data inward.

## Info.plist (exact)

| Key | Value |
| --- | --- |
| `CFBundleIdentifier` | `com.zesoftware.zealfie` |
| `CFBundleExecutable` | `ZeAlfie` |
| `CFBundleDisplayName` | `ZeAlfie` |
| `CFBundleShortVersionString` | `0.1.1` |
| `CFBundleVersion` | `0.1.1` |
| `CFBundleIconFile` | `zealfie.icns` |
| `CFBundlePackageType` | `APPL` |
| `LSMinimumSystemVersion` | `13.0` |
| `NSHighResolutionCapable` | `true` |

`zealfie.icns` is copied from `src/zealfie/icon/zealfie.icns` — the same
single source of truth the Windows packager uses for `zealfie.ico`.

## Launcher contract

`Contents/MacOS/ZeAlfie` is a POSIX `/bin/sh` script. It resolves its own
physical bundle from `$0` (following symlinks), derives `Contents/Resources`,
and `exec`s the **absolute** bundled
`Resources/python/bin/python3.13`. It unsets inherited `PYTHONHOME`,
`PYTHONPATH`, `VIRTUAL_ENV` (and friends), sets `PYTHONNOUSERSITE=1`,
exposes `Contents/Resources/app` on `PYTHONPATH`, and quotes every path so a
bundle location containing spaces works. It never resolves `python` /
`python3` / `pip` / `brew` through `PATH`: if the bundled interpreter is
missing it exits non-zero rather than falling back.

## The macOS wheelhouse

The official Qt macOS wheels are tagged `macosx_13_0_universal2` — that is,
they contain **both arm64 and x86_64 Mach-O slices**. The lock therefore
pins the real macOS closure (PySide6 meta + Essentials + Addons +
shiboken6, plus `packaging`, `build`, `pyproject-hooks`, `setuptools`,
`wheel`), each with a SHA-256 and size computed from a real download.
`PySide6` alone is **not** the binary closure.

After installation, every Mach-O file **and every static archive** under
`Resources/python` and `Resources/app` is inspected **by content** (never by
suffix). Universal2 Mach-O images, and universal (`lipo`-created) static
archives whose members are Mach-O objects, are thinned to arm64:

* a Mach-O result must be thin arm64;
* a static-archive result (`!<arch>\n`) must be a structurally valid `ar`
  container whose object members are exactly arm64 and for which `ar -t`
  succeeds.

The build then audits both trees — with separate evidence for the Mach-O and
static-archive categories — and fails closed unless `ARM64_ONLY=PASS` and
`X86_64_RESIDUES=0`. The final bundle contains **no x86_64 slice** in either
category.

The real Qt wheels use BSD long names (`#1/<len>`) inside their archives and
contain such objects as
`PySide6/Qt/qml/Qt/labs/assetdownloader/libqmlassetdownloaderprivateplugin.a`
(the archive that exposed the missing archive support in the first native
run); the parser resolves BSD and GNU long names and skips Darwin
`__.SYMDEF*` symbol tables.

## Building

On an Apple-silicon Mac with a driver Python 3.13:

```sh
python -m build --wheel --outdir "$WORK/zealfie-wheel"
python packaging/macos/acquire_wheelhouse.py \
    --dest "$WORK/wheelhouse" \
    --zealfie-wheel "$WORK/zealfie-wheel/zealfie-0.1.1-py3-none-any.whl"
python packaging/macos/build_app.py \
    --work "$WORK/build" \
    --wheelhouse "$WORK/wheelhouse" \
    --zealfie-wheel "$WORK/zealfie-wheel/zealfie-0.1.1-py3-none-any.whl" \
    --out-zip "$WORK/ZeAlfie-macOS-arm64-unsigned.zip"
python packaging/macos/witnesses.py all \
    --app "$WORK/build/ZeAlfie.app" --work "$WORK/witness" \
    --wheelhouse "$WORK/wheelhouse"
```

The CI path is the manual workflow
`.github/workflows/macos-packaging-arm64.yml` (`workflow_dispatch` only, on
`macos-15`). It verifies the runner is arm64, acquires and SHA-256-verifies
the PBS archive, prepares the locked wheelhouse, builds the bundle,
normalises and audits Mach-O, runs every witness, and uploads the artifact
`ZeAlfie-macOS-arm64-unsigned.zip` (top-level `ZeAlfie.app`). There is no
release step and no `continue-on-error` anywhere.

## Witnesses

| Gate | What it proves |
| --- | --- |
| `PRIVATE_PYTHON` | `platform.machine()==arm64`, Python 3.13.15, `sys.executable`/`sys.prefix` inside the bundle |
| `IMPORT_SMOKE` / `CLI_SMOKE` | `import zealfie` + runtime modules; `python -m zealfie --help` |
| `HOST_TARGET` | `HostTarget.from_current_host()` → `macosx_*` on arm64 |
| `QT_SMOKE` | real `QApplication` + `ZeAlfieMainWindow`, offscreen, isolated runtime, bounded drain |
| `RELOCATION` | the copied `.app` runs from `/tmp` with a sanitized environment; no build-path references |
| `NO_HOST_PYTHON` | the launcher starts the absolute bundled Python with a hostile/minimal environment; PATH shims are never executed; negative control fails closed |
| `CHILD_VENV` | the bundled Python creates a child venv with pip and installs/imports a deterministic wheel offline |
| `RUNTIME_ROOT` | resolves exactly `~/Library/Application Support/zealfie/runtime` |

## Not in scope

Signing, notarisation, DMG, App Store packaging, Intel/Universal2 bundles,
macOS self-update, PyInstaller/Nuitka freezing, and any change to the
Windows/Linux runtime or packaging. `pyproject.toml` version and
dependencies are untouched.
