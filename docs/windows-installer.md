# ZeAlfie Windows installer (ZA-WIN-BOOT-02)

Status: **accepted native Windows installer** (`ZeAlfie-Setup-0.1.0-dev.exe`,
Inno Setup 6.7.3) that turns a normal user machine into the proven
ZA-WIN-BOOT-01 layout **without the user installing Python / venv / pip or
touching a terminal**, and with a **fully OFFLINE install** (bundled
SHA-256-locked wheelhouse, `pip --no-index --find-links`, no PyPI on the
user machine). The installer **wraps** the proven architecture — private
pinned CPython 3.13.15 → `appenv` → ZeAlfie — it does **not** freeze it (no
PyInstaller/Nuitka/cx_Freeze/MSIX/self-extracting ZIP).

Closure evidence: functional acceptance at HEAD
`a3c378b4876838428909ebe72aea906f62d2dc3a` on the real Windows CI run
`33746857554` (job `100621204745`): offline closure preflight, Inno 6.7.3
compile, silent install, private CPython, appenv provenance, CLI, offscreen
GUI, offline proof, silent uninstall and the artifact upload all PASS. A
non-invasive Windows side-effect witness (`side_effect_witness.py`, baseline
→ post-install → post-uninstall delta audit) tracks the machine-scope
footprint (see below).

| | |
| --- | --- |
| Inno script | `packaging/windows/installer/zealfie.iss` |
| Inno toolchain pin | `packaging/windows/installer/innosetup.toml` |
| Offline wheelhouse lock | `packaging/windows/wheelhouse.lock.toml` |
| Wheelhouse acquisition | `packaging/windows/acquire_wheelhouse.py` |
| Installer smoke | `packaging/windows/installer_smoke.py` |
| Side-effect witness | `packaging/windows/side_effect_witness.py` |
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
runtime root. The side-effect witness asserts it stays untouched across
install and uninstall.

## Install-time sequence (all subprocesses hidden)

The bootstrap is driven from the `[Code]` **`CurStepChanged(ssPostInstall)`**
event function with explicit `Exec(Filename, Params, WorkingDir, SW_HIDE,
ewWaitUntilTerminated, ResultCode)` calls whose **child exit codes are
observed** — there is NO declarative `[Run]` section (it never checks exit
codes). In order:

1. **Verify the bundled CPython installer** — `GetSHA256OfFile` on
   `{app}\assets\cpython\python-3.13.15-amd64.exe` must equal the digest
   embedded in `zealfie.iss` (which MUST equal
   `packaging/windows/reproducibility.toml` — enforced by a hermetic test).
2. **Install private CPython** — `RunCheckedCpython` executes the silent
   per-user install (`/quiet /norestart InstallAllUsers=0 PrependPath=0
   Include_launcher=0 AssociateFiles=0 Shortcuts=0 Include_pip=1
   Include_venv=1 Include_test=0 Include_doc=0 Include_tcltk=0
   TargetDir="{app}\python" /log "{app}\logs\cpython-install.log"`),
   accepting exit codes **0 and 3010** ("success, reboot advised").
3. **Require the actual interpreter** — `{app}\python\python.exe` must
   exist before anything else continues — fail closed, never a fallback to
   a system Python.
4. **Build the offline appenv** — `RunCheckedZero` runs
   `{app}\python\python.exe` with the bootstrap script and the CORRECT
   argparse order (global option before the subcommand):
   `"{app}\assets\bootstrap\provision_windows.py" --witness-root "{app}"
   make-appenv --offline-wheelhouse "{app}\assets\wheelhouse"` (WorkingDir
   `{app}\assets\bootstrap`). The venv is created from the private python,
   then `pip install --no-index --find-links {app}\assets\wheelhouse
   {app}\assets\wheelhouse\zealfie-0.1.0-py3-none-any.whl` — **PyPI is never
   contacted**. Exit code 0 required.
5. **Completeness gate** — all four launchers (`python.exe`, `pythonw.exe`,
   `zealfie.exe`, `zealfie-gui.exe`) must exist under
   `{app}\appenv\Scripts`.

The pip stdout/stderr is captured to `{app}\logs\appenv-pip-install.log`
(opening with a recorded `[zealfie-offline] argv:` banner), so the offline
path is provable after the fact.

**Failure semantics (as shipped).** Every fatal path routes through
`FailBootstrap(Msg)`, which sets the `BootstrapFailed` flag, logs
`[zealfie] BOOTSTRAP FAILED: …`, then raises. Inno **swallows exceptions
raised from event functions** (`CurStepChanged raised an exception` in the
Setup log), so `RaiseException` alone does NOT guarantee a non-zero Setup
exit — the authoritative failure signal is
`GetCustomSetupExitCode()`, which returns **2** when `BootstrapFailed` is
set and **0** otherwise. Silent CI runs therefore fail loudly (non-zero
exit) while every diagnostic stays in the Setup log and `{app}\logs`.
Hidden console ≠ hidden failure; stderr is never suppressed.

## Inno Setup toolchain pin (mirrors the CPython reproducibility approach)

`packaging/windows/installer/innosetup.toml` pins **Inno Setup 6.7.3** (the
newest stable 6.x at implementation time, 2026-05-26; the 7.x line exists
but this project deliberately stays on mature 6.x — see below) with its
official download URL
(`https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe`)
and the REAL SHA-256 (`9c73c3ba…`, computed on a Linux host on 2026-09-03).

**Provenance model (as shipped).** Neither the ISCC banner nor its PE
version resource is a version oracle: `ISCC.exe /?` prints only "Inno Setup
6 Command-Line Compiler" (no patch level) and the PE
FileVersion/ProductVersion reports `0.0.0.0` upstream. The exact-toolchain
proof is therefore cryptographic: CI downloads the pinned official
installer, **verifies its SHA-256 before execution** (fail closed), silently
installs it to a clean per-user staging directory, and gates on the
presence of `ISCC.exe`. The real compile additionally reports
`Compiler engine version: Inno Setup 6.7.3` (observed in the r3-era CI run).
An absent or different payload fails the job.

*Why not 7.x?* The 6.x line is the mature, battle-tested series; nothing in
this installer needs 7.x features, and pinning a 6.x point release keeps
the compiler surface stable for years. If a future mission needs 7.x, bump
`innosetup.toml` (version/URL/SHA). The .iss uses only long-stable
constructs (`Exec`, `SW_HIDE`, `ewWaitUntilTerminated`, `GetSHA256OfFile`,
`RaiseException`, `GetCustomSetupExitCode`, `ArchitecturesAllowed=x64os`,
`PrivilegesRequired=lowest`, `{autoprograms}` icons).

## Offline wheelhouse contract

`packaging/windows/wheelhouse.lock.toml` pins the EXACT Windows x64 /
CPython 3.13 wheel set bundled into `{app}\assets\wheelhouse`:

* the zealfie wheel metadata (version 0.1.0, source commit, local-build);
* resolution metadata (platform `win_amd64`, python `cp313`, generation
  date, the exact `pip download` command and requirement list);
* one table per wheel: name, exact version, filename, **SHA-256 and size
  computed from the real download**.

The base list was produced by a REAL resolution (`pip download --platform
win_amd64 --python-version 3.13 --implementation cp --abi cp313
--only-binary=:all: packaging>=24 PySide6>=6 build>=1.2 setuptools>=77
wheel>=0.45` — the same five top-level requirements zealfie declares), never
guessed. Resolved on 2026-09-03 (pip 25.1.1). **Current 10-wheel lock:**
packaging 26.3; PySide6 6.11.2 + PySide6-Essentials/Addons + shiboken6
6.11.2 (cp310-abi3 wheels — compatible via the stable ABI); build 1.6.0 +
pyproject_hooks 1.2.0; setuptools 84.0.0; wheel 0.48.0; **colorama 0.4.6**
(≈ 248 MB pinned). colorama is `build`'s `os_name == "nt"` dependency and
is pinned EXPLICITLY because Linux `pip download` selects Windows wheels
but does NOT evaluate that environment marker (audited: it is the only
platform-marker runtime dep in the closure; setuptools' marker deps are
test/check/type extras only).

**Windows-marker closure preflight (CI).** Because Linux resolution cannot
prove the Windows closure, `windows-installer-build.yml` runs an
`offline-closure-preflight` step on the Windows runner AFTER acquisition and
BEFORE compilation:
`pip install --no-index --find-links <staged wheelhouse> --dry-run
--ignore-installed <zealfie wheel>` — this evaluates `os_name == "nt"` and
fails non-zero (no PyPI) if any dependency is absent from the wheelhouse.
It is the authoritative Windows closure check.

`acquire_wheelhouse.py` (CI-side, needs network) re-downloads each pin with
`pip download --no-deps <name>==<version>` and **fails closed** if the
staging set is not exactly the locked set or any SHA-256 drifts, then adds
the freshly-built zealfie wheel. The wheelhouse is generated in CI, **never
committed**. Because pip runs with `--no-index --find-links`, a repair or
reinstall is offline too — the bundled wheelhouse is the only package
source the appenv ever sees.

**Provenance (machine-readable).** `acquire_wheelhouse.py --provenance-out
<file>` writes the acquisition summary (`status`, `zealfie_version`,
`source_commit`, `wheel_count`, `total_locked_bytes`,
`zealfie_wheel{filename,sha256,size}`, `wheelhouse`) as PURE JSON to the
file (the human console log stays on stdout / `acquire.log`). The final CI
`provenance` step builds `provenance.json` (artifact name/version, CI HEAD,
Inno + CPython pins, install contract, **setup sha256 + size**, and the
parsed wheelhouse JSON) and FAILS CLOSED if the wheelhouse JSON is missing
or malformed — there is no silent placeholder.

To regenerate the lock after a dependency bump: run the `pip download`
command above, add any Windows-marker transitives explicitly (like
colorama), record filename/size/SHA-256 per wheel, update the tables and
the metadata (including `source_commit` and `generated`), and commit.

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
6. bounded offscreen GUI smoke through the installed appenv — the child
   runs under a deterministic UTF-8 environment (`PYTHONUTF8=1` +
   `PYTHONIOENCODING=utf-8`) and its capture is decoded STRICTLY as UTF-8
   (no silent U+FFFD replacement), while the smoke's own stdout/stderr are
   reconfigured to UTF-8 so a Windows charmap console can never raise
   `UnicodeEncodeError`;
7. the install used the bundled wheelhouse: `{app}\logs\appenv-pip-install.log`
   opens with a recorded `[zealfie-offline] argv:` banner carrying
   `--no-index --find-links`, contains pip's `Looking in links` output, and
   contains no http(s) URL evidence (newer pip no longer prints
   `Ignoring indexes`; the recorded argv banner + absence-of-URL is the
   authoritative offline proof), and the
   wheelhouse still holds the zealfie wheel (offline repair/reinstall
   capability).

## Side-effect witness (baseline → install → uninstall delta audit)

`packaging/windows/side_effect_witness.py` (stdlib; `winreg` on Windows,
injectable seams for hermetic Linux tests) captures and diffs the
machine-scope footprint of the installer:

* `baseline --out <json>` — user PATH (`HKCU\Environment\Path`), machine
  PATH (`HKLM\SYSTEM\CurrentControlSet\Control\Session
  Manager\Environment\Path`), `py.exe` launcher presence
  (`shutil.which("py.exe")`), the user `.py` file association
  (`HKCU\Software\Classes\.py` + default ProgID), the user- and
  machine-scope `PythonCore\3.13` registration
  (`HKCU`/`HKLM\Software\Python\PythonCore\3.13\InstallPath`), the relevant
  per-user and per-machine Uninstall entries (Python / ZeAlfie), the ZeAlfie
  Start Menu shortcut state, and whether
  `%LOCALAPPDATA%\zealfie\runtime` exists;
* `verify-install --baseline <json> --install-root <{app}> --out <json>` —
  asserts (fail closed, non-zero on any finding) NO forbidden pollution
  (user/machine PATH unchanged; no NEW `py.exe` launcher; no NEW/changed
  `.py` association), the expected user-scoped provider state (any
  `PythonCore\3.13` registration is USER-scoped and resolves to
  `{app}\python` — the authoritative provider→runtime binding; a
  per-user CPython Apps&Features entry EXISTS in HKCU — its Burn/
  bootstrap uninstaller may live in `%LOCALAPPDATA%\Package Cache`,
  so only presence is asserted; no NEW machine-scope CPython
  registration), and the expected ZeAlfie shell (Start Menu shortcut
  targets `{app}\appenv\Scripts\zealfie-gui.exe`; ZeAlfie uninstall
  registration is per-user);
* `verify-uninstall --baseline <json> --install-root <{app}> --out <json>` —
  asserts the Start Menu shortcut and the ZeAlfie installer-owned
  registration/assets are removed, the documented private-CPython provider
  state is intentionally preserved, and
  `%LOCALAPPDATA%\zealfie\runtime` is untouched.

The CI workflow runs `side-effect-baseline` (before the silent install),
`side-effect-install-audit` (after the installer smoke) and
`side-effect-uninstall-audit` (after the uninstall witness); audit JSONs are
written into `setup-output/` so they ride the artifact upload. The
comparison logic is pure and unit-tested with injected fake snapshots on
Linux. Baseline-delta only — it never asserts "Python must not exist"
(runners may already have Python).

## CPython registry / machine-scope caveat audit (installer-owned layer)

The official python.org **per-user** installer is a platform provider that
manages its own bookkeeping. What it leaves OUTSIDE `{app}` (now audited on
REAL runs by the side-effect witness above, replacing the earlier
"to-confirm-on-real-run" flag):

* per-user registry data under
  `HKCU\Software\Python\PythonCore\3.13\InstallPath` pointing to the
  private `{app}\python` (the authoritative provider→runtime binding,
  registered to the current user only);
* an **Apps & Features (Add/Remove Programs) entry** for "Python 3.13.15
  (64-bit)" under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall` — its
  Burn/bootstrap uninstall executable lives in the user's
  `%LOCALAPPDATA%\Package Cache` (NOT under `{app}\python`); this is
  provider bookkeeping, NOT PATH/launcher/association pollution.  This
  behaviour was established by the real Windows side-effect audit run
  `33748966706` (the audit itself only failed because the original
  predicate required the uninstall metadata to point under `{app}\python`);
* **no** PATH changes (`PrependPath=0`), **no** `py` launcher
  (`Include_launcher=0`), **no** file associations (`AssociateFiles=0`),
  **no** shortcuts (`Shortcuts=0`), **no** Start Menu/registry entries of
  its own beyond the above. These are hard requirements; the witness
  enforces them as baseline deltas (PATH/launcher/associations unchanged,
  no machine-scope registration), and the hermetic .iss pin test enforces
  the four installer properties equal those pinned in
  `reproducibility.toml`.

The CPython-specific Apps & Features/uninstall entry is a **documented
platform-provider limitation**, not a defect: ZeAlfie deliberately does NOT
hack-delete the python.org registry data (that would corrupt the platform
uninstall state).

## Uninstall semantics (observed on the real CI witness)

The CI runs the Inno uninstaller silently —
`unins000.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` (guarded from Git
Bash/MSYS argument mangling by `MSYS2_ARG_CONV_EXCL="*"`, with a bounded
exit-code capture). Observed behaviour: installer-owned assets are removed
(`{app}\assets` incl. `zealfie.ico`, bootstrap scripts, logs), the ZeAlfie
uninstall registration and the Start Menu shortcut are removed, while the
nested `{app}\python` + `{app}\appenv` trees and their python.org registry
state are **intentionally preserved** (documented platform-provider state,
consistent and removable later through Apps & Features / manual deletion).
`%LOCALAPPDATA%\zealfie\runtime` is never touched by installer or
uninstaller.

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

## Historical note (r1–r9 corrections, kept for context)

Earlier revisions of this document described mechanisms that the real
Windows runs disproved and that are now superseded: the declarative
`[Run]`/`AfterInstall` bootstrap flow (replaced by the
`CurStepChanged(ssPostInstall)` + `Exec` + `ResultCode` path in r5),
`RaiseException`-alone failure semantics (Inno swallows event-function
exceptions — replaced by the `BootstrapFailed` flag +
`GetCustomSetupExitCode()` mechanism in r6), the `ISCC /?` / PE-version
toolchain oracles (neither reports the patch level — replaced by the
pinned-payload SHA-256 provenance model in r1–r2), and the 9-wheel lock
(colorama added in r6). This document describes the shipped behaviour.

## Known remaining work (out of scope here)

* Authenticode signing + SmartScreen reputation (requires a cert; separate
  mission).
* Desktop shortcut task, upgrade/downgrade UX, multi-version side-by-side,
  per-install self-update state root.
* Optional `SetupArchitecture=x64` (64-bit Setup.exe) — deferred to keep the
  script on the most conservative compiler surface.
* **Next mission: ZA-WIN-BOOT-03 — manual human Windows installer witness**
  (interactive install/uninstall on a real user machine, guided by the
  side-effect witness + CPython-registry audit findings above).
