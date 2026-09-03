# ZeAlfie Windows installer (ZA-WIN-BOOT-02)

Status: **native Windows installer implementation** (`ZeAlfie-Setup.exe`,
Inno Setup) that turns a normal user machine into the proven ZA-WIN-BOOT-01
layout **without the user installing Python / venv / pip or touching a
terminal**, and with a **fully OFFLINE install** (bundled SHA-256-locked
wheelhouse, `pip --no-index --find-links`, no PyPI on the user machine).
The installer **wraps** the proven architecture — private pinned CPython
3.13.15 → `appenv` → ZeAlfie — it does **not** freeze it (no
PyInstaller/Nuitka/cx_Freeze/MSIX/self-extracting ZIP). The real Windows
build/install evidence is the human-gated CI witness
`.github/workflows/windows-installer-build.yml`.

| | |
| --- | --- |
| Inno script | `packaging/windows/installer/zealfie.iss` |
| Inno toolchain pin | `packaging/windows/installer/innosetup.toml` |
| Offline wheelhouse lock | `packaging/windows/wheelhouse.lock.toml` |
| Wheelhouse acquisition | `packaging/windows/acquire_wheelhouse.py` |
| Installer smoke | `packaging/windows/installer_smoke.py` |
| CI witness | `.github/workflows/windows-installer-build.yml` |
| Hermetic tests | `tests/test_windows_installer.py` |
| Docs (bootstrap proof) | `docs/windows-bootstrap.md` (ZA-WIN-BOOT-01) |

## Installer layout

```
{localappdata}\Programs\ZeAlfie\      # {app} — default (per-user, no admin)
  python\                             # private pinned CPython 3.13.15
    python.exe                        #   (silent per-user python.org install)
  appenv\                             # dedicated venv (offline-built)
    Scripts\python.exe                # console interpreter
    Scripts\pythonw.exe               # windowed interpreter
    Scripts\zealfie.exe               # console entry point
    Scripts\zealfie-gui.exe           # windowed entry point (Start Menu)
  assets\
    cpython\python-3.13.15-amd64.exe  # bundled installer (SHA-256 re-verified)
    wheelhouse\*.whl                  # EXACT lock-verified offline wheel set
    bootstrap\                        # provision.py/provision_windows.py/
                                      #   installer_smoke.py/gui_smoke_offscreen.py/
                                      #   reproducibility.toml
    zealfie.ico                       # canonical icon (single source of truth)
  logs\                               # cpython-install.log / appenv-*.log
```

The existing ZeSoftware **shared runtime** (`%LOCALAPPDATA%\zealfie\runtime`,
slots/state/cache) is a **separate concern**: nothing in the installer reads,
writes, or deletes it, and the installer smoke runs its GUI on a throwaway
runtime root.

## Install-time sequence (all subprocesses hidden)

1. **Verify + install private CPython.** Before the bundled
   `python-3.13.15-amd64.exe` runs, `[Code]` computes `GetSHA256OfFile` and
   compares it with the digest embedded in `zealfie.iss` (which MUST equal
   `packaging/windows/reproducibility.toml` — enforced by a hermetic test).
   A mismatch raises an exception → Setup aborts (non-zero exit, rollback).
   Then the silent per-user install runs:
   `/quiet /norestart InstallAllUsers=0 PrependPath=0 Include_launcher=0
   AssociateFiles=0 Shortcuts=0 Include_pip=1 Include_venv=1 Include_test=0
   Include_doc=0 Include_tcltk=0 TargetDir={app}\python`. `AfterInstall`
   verifies the **actual** `{app}\python\python.exe` exists before anything
   continues — fail closed, no fallback to a system Python.
2. **Build the offline appenv.** `{app}\python\python.exe` runs the shared
   BOOT-01 primitive (`provision_windows.py make-appenv
   --offline-wheelhouse {app}\assets\wheelhouse`): venv creation from the
   private python, then
   `pip install --no-index --find-links {app}\assets\wheelhouse
   {app}\assets\wheelhouse\zealfie-0.1.0-py3-none-any.whl` — **PyPI is never
   contacted**. `AfterInstall` raises unless all four launchers
   (`python.exe`, `pythonw.exe`, `zealfie.exe`, `zealfie-gui.exe`) exist
   under `{app}\appenv\Scripts`. The pip stdout/stderr is captured to
   `{app}\logs\appenv-pip-install.log` (and mirrored into the Setup log via
   `logoutput`), so the offline path is provable after the fact.
3. **Shortcuts.** A single per-user Start Menu item targets
   `{app}\appenv\Scripts\zealfie-gui.exe` with the canonical ICO — no
   terminal, no system-Python/PATH/CWD/source-tree dependency. Desktop
   shortcut deliberately omitted (zero-pollution; optional future task).

Raised exceptions in `[Code]` become *"Fatal exception during installation
process"* → Setup **exit code 4** with rollback — so silent CI runs fail
loudly (`/VERYSILENT /SUPPRESSMSGBOXES`, non-zero exit) while interactive
users get an error dialog. Hidden console ≠ hidden failure; stderr is never
suppressed.

## Inno Setup toolchain pin (mirrors the CPython reproducibility approach)

`packaging/windows/installer/innosetup.toml` pins **Inno Setup 6.7.3** (the
newest stable 6.x at implementation time, 2026-05-26; the 7.x line exists
but this project deliberately stays on mature 6.x — see below) with its
official download URL
(`https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe`)
and the REAL SHA-256 (`9c73c3ba…`, computed on a Linux host on 2026-09-03).
CI provisions it explicitly: download → SHA-256 verify (fail closed) →
silent per-user install to a tool dir → `ISCC.exe /?` must report `6.7.3`
**before** compilation → compile. An absent or different compiler fails the
job.

*Why not 7.x?* The 6.x line is the mature, battle-tested series; nothing in
this installer needs 7.x features, and pinning a 6.x point release keeps
the compiler surface stable for years. If a future mission needs 7.x, bump
`innosetup.toml` (version/URL/SHA) — the .iss uses only long-stable
directives and flags (`ArchitecturesAllowed=x64os`,
`PrivilegesRequired=lowest`, `runhidden`, `logoutput`, `RaiseException`,
`GetSHA256OfFile`).

## Offline wheelhouse contract

`packaging/windows/wheelhouse.lock.toml` pins the EXACT Windows x64 /
CPython 3.13 wheel set bundled into `{app}\assets\wheelhouse`:

* the zealfie wheel metadata (version 0.1.0, source commit, local-build);
* resolution metadata (platform `win_amd64`, python `cp313`, generation
  date, the exact `pip download` command and requirement list);
* one table per wheel: name, exact version, filename, **SHA-256 and size
  computed from the real download**.

The list was produced by a REAL resolution (`pip download --platform
win_amd64 --python-version 3.13 --implementation cp --abi cp313
--only-binary=:all: packaging>=24 PySide6>=6 build>=1.2 setuptools>=77
wheel>=0.45` — the same five top-level requirements zealfie declares), never
guessed. Resolved on 2026-09-03 (pip 25.1.1): packaging 26.3, PySide6
6.11.2 + PySide6-Essentials/Addons + shiboken6 6.11.2 (cp310-abi3 wheels —
compatible via the stable ABI), build 1.6.0 + pyproject_hooks 1.2.0,
setuptools 84.0.0, wheel 0.48.0 (≈ 248 MB pinned).

`acquire_wheelhouse.py` (CI-side, needs network) re-downloads each pin with
`pip download --no-deps <name>==<version>` and **fails closed** if the
staging set is not exactly the locked set or any SHA-256 drifts, then adds
the freshly-built zealfie wheel. The wheelhouse is generated in CI, **never
committed**. Because pip runs with `--no-index --find-links`, a repair or
reinstall is offline too — the bundled wheelhouse is the only package
source the appenv ever sees.

To regenerate the lock after a dependency bump: run the `pip download`
command above, record filename/size/SHA-256 per wheel, update the tables
and the metadata (including `source_commit` and `generated`), and commit.

## Installer smoke (runs on the installed machine / CI witness)

`installer_smoke.py` executes **with the installed appenv interpreter**
against `--install-root {app}` and asserts, fail closed:

1. `{app}\python\python.exe` exists and reports the pinned 3.13.15;
2. the appenv is complete (`python.exe`, `pythonw.exe`, `zealfie.exe`,
   `zealfie-gui.exe`);
3. live provenance of the running interpreter: `sys.executable` →
   `{app}\appenv\Scripts\python.exe`, `sys.prefix` → `{app}\appenv`,
   `sys.base_prefix` → `{app}\python`, `pyvenv.cfg home` → `{app}\python`,
   and no anchor under a forbidden runner/system Python root;
4. installed ZeAlfie imports from the appenv's own site-packages (never the
   checkout);
5. `zealfie.exe --version` / `--help` exit 0;
6. bounded offscreen GUI smoke through the installed appenv;
7. the install used the bundled wheelhouse: `{app}\logs\appenv-pip-install.log`
   opens with a recorded `[zealfie-offline] argv:` banner carrying
   `--no-index --find-links`, contains pip's `Looking in links` output, and
   contains no http(s) URL evidence (newer pip no longer prints
   `Ignoring indexes`; the recorded argv banner + absence-of-URL is the
   authoritative offline proof), and the
   wheelhouse still holds the zealfie wheel (offline repair/reinstall
   capability).

## CPython registry / machine-scope caveat audit (installer-owned layer)

The official python.org **per-user** installer is a platform provider that
manages its own bookkeeping. What it leaves OUTSIDE `{app}` (based on known
python.org installer behaviour — **flagged as to-confirm-on-real-run** by
the BOOT-02 CI witness):

* an **Apps & Features (Add/Remove Programs) entry** for "Python 3.13.15
  (64-bit)" under `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall`
  plus per-user registry data under `HKCU\Software\Python\PythonCore\3.13`
  (the per-user install is registered to the current user only);
* its own uninstall metadata pointing into `{app}\python` (the python.org
  uninstaller is reachable from Apps & Features);
* **no** PATH changes (`PrependPath=0`), **no** `py` launcher
  (`Include_launcher=0`), **no** file associations (`AssociateFiles=0`),
  **no** shortcuts (`Shortcuts=0`), **no** Start Menu/registry entries of
  its own beyond the above. These are hard requirements and the witness
  checks the four installer properties are exactly those pinned in
  `reproducibility.toml`.

The CPython-specific Apps & Features/uninstall entry is a **documented
platform-provider limitation**, not a defect: ZeAlfie deliberately does NOT
hack-delete the python.org registry data (that would corrupt the platform
uninstall state). The Inno uninstaller removes everything it owns (assets,
bootstrap scripts, logs, Start Menu item, `{app}` uninstall entry) and
leaves the nested `{app}\python` + `{app}\appenv` trees and their registry
state in place, consistent and removable later through Apps & Features /
manual deletion. The user's `%LOCALAPPDATA%\zealfie\runtime` is never
touched by either installer or uninstaller.

## Relationship to self-update (installer owns the platform/bootstrap layer)

**Distinction (documented; self-update is NOT redesigned):**

* The **installer** owns the *platform/bootstrap layer*: the private pinned
  CPython install (`{app}\python`), the offline-built dependency baseline
  (`{app}\appenv` site-packages: PySide6 etc. from the bundled wheelhouse),
  the shell/shortcuts, and the uninstall entry. Re-running Setup repairs or
  reinstalls this layer offline.
* **ZeAlfie CODE updates still go through the existing self-update**
  (`src/zealfie/selfupdate/`, unchanged) into the **same appenv**: the
  verified wheel is installed into `sys.prefix` (the appenv) via
  `pip install --no-deps --no-index`, the private CPython stays untouched,
  the detached Windows helper is venv-relative, and restart is
  interpreter-relative — exactly the analysis already accepted for
  ZA-WIN-BOOT-01 (see `docs/windows-bootstrap.md` → G8 verdict). The
  installer and self-update therefore never fight over the same layer.
* Self-update *state* (pending markers, staged wheels) lives under the
  shared-runtime layout root, decoupled from the install target — harmless
  today; a future per-install state root is a known consideration, not a
  BOOT-02 change.

## Non-goals respected

No Authenticode signing / SmartScreen / public Release / tag / automatic
publish; no macOS/ARM64 (x64-only: `ArchitecturesAllowed=x64os`); no
bundling of ZeSolver/ZeMosaic/ZSSS/ZeAnalyser/CUDA; no PyInstaller/Nuitka
freeze; no self-update redesign; no shared-runtime replacement; no PATH /
launcher / file-association / global-shortcut pollution; no registry
hacking. The canonical icon `src/zealfie/icon/zealfie.ico` is reused
verbatim for Setup identity, the shipped `{app}\assets\zealfie.ico`
(uninstall-display + Start Menu) — never duplicated or altered. (Inno does
not support replacing the stock icon of the raw `unins000.exe` binary;
Windows shows the app icon through `UninstallDisplayIcon` instead.)

## Known remaining work (out of scope here)

* Real-Windows run of `windows-installer-build.yml` (workflow_dispatch or
  feature-branch push) — the human-gated witness that compiles and
  installs `ZeAlfie-Setup-0.1.0-dev.exe` on `windows-latest` and uploads
  the artifact; confirm the CPython registry caveats above on a real run.
* Authenticode signing + SmartScreen reputation (requires a cert; separate
  mission).
* Desktop shortcut task, upgrade/downgrade UX, multi-version side-by-side,
  per-install self-update state root.
* Optional `SetupArchitecture=x64` (64-bit Setup.exe) — deferred to keep the
  script on the most conservative compiler surface.
