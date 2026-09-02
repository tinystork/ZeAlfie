# ZeAlfie Windows standalone bootstrap (ZA-WIN-BOOT-01)

Status: bootstrap/packaging **proof** — not the final public installer, not
a frozen PyInstaller/Nuitka exe. It proves the existing ZeAlfie
architecture can run standalone on Windows on a **private, pinned CPython
3.13** runtime with no dependency on the GitHub runner's (or any system)
preinstalled Python after provisioning.

| | |
| --- | --- |
| Reproducibility record | `packaging/windows/reproducibility.toml` |
| Pure provisioning logic | `packaging/windows/provision.py` |
| Windows entrypoint | `packaging/windows/provision_windows.py` |
| Runtime witness (runs in appenv) | `packaging/windows/witness_runtime.py` |
| Offscreen GUI smoke (runs in appenv) | `packaging/windows/gui_smoke_offscreen.py` |
| Hermetic tests | `tests/test_windows_bootstrap.py` |
| CI witness | `.github/workflows/windows-bootstrap-witness.yml` |

## Target layout

```
<witness-root>/            # per-run/per-user witness root
  python/                  # private pinned CPython 3.13 (silent per-user install)
  appenv/                  # dedicated venv containing the installed ZeAlfie
    Scripts/python.exe     # the application interpreter
    Scripts/zealfie.exe    # console entry point
    Scripts/zealfie-gui.exe  # windowed (pythonw) entry point
  downloads/               # installer (SHA-256 verified before install)
  logs/                    # installer + pip logs (uploaded on failure)
  child-witness/           # isolated runtime-child witness venv
```

The existing ZeSoftware shared runtime
(`%LOCALAPPDATA%\zealfie\runtime` with slots/state/cache, ZA-M0-6) is a
**separate concern**. Nothing in the bootstrap reads, writes, or imports
its code; the runtime witness creates its child venv in an isolated CI
root; the GUI smoke constructs its service on a throwaway runtime root.

## Private CPython (pinned, verified, silent)

* **Version:** 3.13.15 (latest stable 3.13.x patch at implementation time,
  2026-08-05), **x86_64**, full python.org Windows x64 installer
  (`python-3.13.15-amd64.exe`) — **not** the embeddable zip (ZeAlfie needs
  real pip + venv).
* The record pins the installer URL and its **real SHA-256**, computed on a
  Linux host from the official `python.org` download on 2026-09-02.
  Provisioning **fails closed** on any hash mismatch — an unverified
  installer is never executed.
* Silent per-user install, no admin:
  `/quiet /norestart InstallAllUsers=0 TargetDir=<root>\python
  PrependPath=0 Include_launcher=0 AssociateFiles=0 Shortcuts=0
  Include_pip=1 Include_venv=1 Include_test=0 Include_doc=0
  Include_tcltk=0`.
* The **actual** installed interpreter path
  (`<root>\python\python.exe`) is verified after install by probing it —
  never assumed.

## Appenv

`<root>\python\python.exe -m venv <root>\appenv` then the built ZeAlfie
wheel (with its PySide6 and other dependencies) is installed into the
appenv with that same interpreter. The venv mechanism is the stdlib `venv`
module used by the shared runtime
(`src/zealfie/runtime/deployment.py` →
`venv.create(candidate_path, with_pip=True, clear=False)`), invoked at the
subprocess level.

## Provenance & isolation assertions (path-based, fail closed)

The witness runs **inside the appenv interpreter** and records
`sys.executable`, `sys.prefix`, `sys.base_prefix`, `sys._base_executable`
and the appenv's `pyvenv.cfg home`, then asserts:

1. `sys.base_prefix` is exactly the private install dir
   (`<root>\python`) — the appenv is a venv of the private CPython;
2. `sys.prefix` is the appenv and `sys.executable` is the appenv's own
   `Scripts\python.exe`;
3. no recorded anchor resolves to a forbidden runner/system Python root
   (`C:\hostedtoolcache\windows\Python\*`,
   `C:\Program Files\Python*`,
   `%LOCALAPPDATA%\Programs\Python\*`) — **path provenance**, never
   version-string comparison. `C:\Program Files\Python*` is matched as a
   first-component wildcard family, case-insensitively.

The job **fails** if any of these do not hold.

## Runtime-child capability witness

From the appenv python, the witness creates a child venv into an isolated
CI test root with the **same mechanism class** the shared runtime uses
(`venv.create(path, with_pip=True)`), then asserts:

* `Scripts\python.exe` exists;
* `python -m pip --version` works inside the child;
* the child's `sys.base_prefix`, `sys._base_executable` and `pyvenv.cfg
  home` all derive from the **private CPython** (and from nothing else).

This proves the runtime slot-creation capability — installing future
ZeSoftware products into shared-runtime-style slots from a standalone
appenv — works on the private runtime. A real user runtime is never
touched.

## CLI / GUI smokes

* **CLI:** `appenv\Scripts\zealfie.exe --version` and `--help` run from the
  **installed** appenv (never the checkout) and must exit 0.
* **GUI:** `QT_QPA_PLATFORM=offscreen` bounded instantiation — the real
  `QApplication` + `ZeAlfieMainWindow` are constructed with the real
  `ZeAlfieService` (on a throwaway runtime root), shown offscreen, events
  drained for a bounded moment, then exit. No interactive user action, no
  network (all check hooks unwired), and no windowed console dependency.

## Relationship to the ZeSoftware shared runtime

`src/zealfie/runtime/layout.py` (slots/state/cache,
`default_runtime_root()` → `%LOCALAPPDATA%\zealfie\runtime` on Windows) is
preserved **unchanged**. The bootstrap never merges the shared runtime into
the appenv and never executes against a real user runtime. Standalone mode
installs the application shell (ZeAlfie itself) into the appenv; the shared
runtime remains the home of managed ZeSoftware product components (slots +
atomic active pointer + provenance) as today. The runtime child witness
validates that product-slot capability *would* function on the private
python.

## No-console / windowed-launch route preserved

Nothing introduced makes a terminal window architecturally mandatory:

* all technical helper subprocesses use
  `CREATE_NO_WINDOW` (the same value
  `zealfie.common.subprocess_platform.technical_subprocess_platform_kwargs()`
  returns; the entrypoint inlines the constant so it stays stdlib-only);
* the `zealfie-gui` (pythonw) windowed route is unchanged — the appenv
  contains the normal `zealfie-gui.exe` windowed launcher and the GUI smoke
  runs through the appenv interpreter;
* stdlib `venv`/`ensurepip`'s own internal subprocess is a pre-existing,
  documented limitation (`subprocess_platform.py`) and is unaffected.

## G8 — self-update compatibility verdict (analysis, no code change required)

**VERDICT: COMPATIBLE — no real gap found; no compatibility fix needed.**

The private-python/appenv layout maps cleanly onto the existing self-update
(`src/zealfie/selfupdate/`) for these structural reasons:

1. **Install target is `sys.prefix`, not a fixed path.** The standalone
   activator (`activator.py`) installs the verified wheel into the venv it
   runs in (via `pip install --no-deps --no-index` using the interpreter
   from `resolve_install_interpreter`). In standalone mode that venv **is**
   the appenv — self-update replaces the ZeAlfie wheel in the appenv while
   the private pinned CPython stays untouched.
2. **`resolve_install_interpreter` is structural.** It operates purely on
   `sys.prefix/Scripts`: the windowed `pythonw.exe` is resolved to its
   same-venv console sibling `python.exe` by proving the executable's
   parent equals `Path(sys.prefix)/"Scripts"`. Under appenv the windowed
   GUI interpreter is `appenv\Scripts\pythonw.exe` with
   `sys.prefix = appenv` — the same proof holds, so pip/distlib never
   regenerates windowed shebangs and `zealfie.exe`/`zealfie-gui.exe` keep
   their console/windowed split (regression-covered by
   `tests/test_self_update_interpreter.py`).
3. **The detached Windows helper is venv-relative.** `spawn_windows_helper`
   launches `<same-venv>\python.exe -m zealfie.selfupdate.windows_helper`
   — under appenv that is `appenv\Scripts\python.exe`, importing the
   installed `zealfie` from the appenv's site-packages. No source checkout
   or PATH lookup is involved.
4. **Restart is interpreter-relative.** `restart.py` spawns the fresh GUI
   with the same venv interpreter (`-c "from zealfie.gui import main;
   main()"`) — under appenv this runs the newest installed code from the
   appenv.

**One documented consideration (not a gap for this proof):** the
self-update *state* (pending marker, staged wheel, `--runtime-root`) lives
under the shared runtime layout root (`%LOCALAPPDATA%\zealfie\runtime\state`)
because the GUI wires `make_self_update_check_fn/apply_fn` with
`default_runtime_layout()`. In standalone mode the install target (appenv)
is therefore decoupled from the marker area (shared runtime root). This is
harmless today — markers are versioned and fail-closed — but a future
per-install standalone packager may want a dedicated state root per
install to fully isolate multiple ZeAlfie installs from each other. No code
change was required for ZA-WIN-BOOT-01.

## Known remaining work (explicitly out of scope here)

* Final public installer (Inno Setup / Setup.exe), Start Menu/shortcut
  polish, uninstaller, Authenticode signing.
* Frozen single-file distribution (PyInstaller/Nuitka).
* Product/CUDA bundling; macOS installer; ZeSolver/ZeMosaic/ZSSS installs.
* Real-Windows interactive run of the witness (the `.github/workflows/`
  witness is the bounded CI proof; run it via **workflow_dispatch** on
  `windows-latest`).
