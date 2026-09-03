# Windows taskbar/launcher icon — root cause & update-safe design (ZA-ICON-03A)

Status: **architecture / investigation only.** No implementation, no version
bump, no push. This is the root-cause proof and the design recommendation;
implementation is the follow-up mission (ZA-ICON-03B), pending human
approval.

Base: `beta` @ `ebcc82b` (ZA-ICON-02 retained).

## TL;DR — why the taskbar icon is generic on Windows

The ZeAlfie GUI window is created by **`pythonw.exe`** (the private Python
interpreter), reached through the distlib-generated launcher
`{app}\appenv\Scripts\zealfie-gui.exe`. That interpreter's PE icon resource
is the **generic Python icon**. On Windows, the **taskbar button icon is
resolved from the owning process's executable icon**, not from the Qt window
icon:

* ZA-ICON-01 (`app.setWindowIcon`) sets the Qt **window** icon → title bar +
  Alt-Tab only. It does **not** set the taskbar button icon.
* ZA-ICON-02 (`SetCurrentProcessExplicitAppUserModelID`) sets the process
  **identity** (grouping / pinned-icon matching), but — in the absence of a
  Start-Menu shortcut that carries the **same AppUserModelID + a custom
  icon** — the shell falls back to the executable's generic icon.

So the fix is not "set a bigger window icon"; it is to give the shell a
**stable AppUserModelID → icon mapping** it can resolve, in a place that
survives pip self-update (which regenerates the launchers but never touches
the Inno-owned shortcut).

---

## 1. Proven root cause (evidence)

1. `pyproject.toml` declares the GUI entry point as a **gui_scripts** entry:
   `[project.gui-scripts] zealfie-gui = "zealfie.gui:main"`.
2. pip/setuptools builds that entry point with **distlib**, producing
   `{app}\appenv\Scripts\zealfie-gui.exe`. distlib's Windows launcher for a
   `gui_scripts` entry is a **generic pre-built launcher binary** (`t64.exe`
   on x64, `t32.exe` on x86 — present in the toolchain under
   `pip/_vendor/distlib/`), copied verbatim and suffixed with a shebang line
   `#!…\pythonw.exe`. The generic launcher embeds **no ZeAlfie icon** (its PE
   icon resource is the generic distlib/Python launcher icon).
3. At runtime `zealfie-gui.exe` spawns **`pythonw.exe`** (the windowed
   interpreter) and waits. The **top-level window is therefore owned by
   `pythonw.exe`**, whose PE icon resource is the generic Python icon.
4. The Inno shortcut points at the launcher and sets the shortcut's icon:
   `Name: "{autoprograms}\ZeAlfie"; Filename: "{app}\appenv\Scripts\zealfie-gui.exe"; IconFilename: "{app}\assets\zealfie.ico"; …`
   — but it does **not** set the shortcut's `System.AppUserModel.ID`. So the
   shortcut icon ≠ a registered AUMID icon, and Windows cannot map the
   running process (`AUMID = ZeSoftware.ZeAlfie`) back to that icon.

Conclusion (established from source; the PE-resource inspection itself is a
Windows-side confirmation for the implementation mission): the running
process's executable (pythonw.exe, and the distlib launcher) carries a
generic icon, and Windows uses that executable icon for the taskbar button
because nothing maps the process's AppUserModelID to the canonical icon.

---

## 2. Actual process executable used by the ZeAlfie GUI

`pythonw.exe` (the appenv's windowed interpreter), launched by the distlib
launcher `zealfie-gui.exe`. Both live under `{app}\appenv\Scripts\`. The
window (and therefore the taskbar button) is owned by `pythonw.exe`.

---

## 3. PE icon-resource findings

* `zealfie-gui.exe` = distlib `t64.exe` image → **generic launcher icon** (no
  ZeAlfie icon resource). (Exact bytes must be confirmed on Windows; the
  mechanism is proven by the distlib launcher-generation contract.)
* `pythonw.exe` = CPython windowed interpreter → **generic Python icon**.
* `zealfie.ico` is shipped at `{app}\assets\zealfie.ico` and referenced by the
  Inno shortcut, but that icon only decorates the **Start Menu .lnk**; it is
  not the running process's taskbar icon.

---

## 4. Why AppUserModelID alone was insufficient

`SetCurrentProcessExplicitAppUserModelID("ZeSoftware.ZeAlfie")` (ZA-ICON-02)
does the right thing at the identity layer, but the shell's taskbar icon
resolution for a **non-pinned** running window is:

1. a Start-Menu shortcut with the **same AppUserModelID** and its icon, if
   one exists → use that icon;
2. otherwise → the **executable's** icon (pythonw.exe's generic icon).

Since no shortcut carries `System.AppUserModel.ID = "ZeSoftware.ZeAlfie"`,
the shell takes branch (2). ZA-ICON-02 is **correct and should be retained**
(it is the necessary identity), but it is not sufficient without the icon
mapping.

---

## 5. What pip self-update does to `zealfie-gui.exe`

Self-update (`pip install --no-deps --no-index <new-wheel>` into the appenv,
see `src/zealfie/selfupdate/`) **regenerates** the console/gui entry-point
launchers. This is the exact CORR-3 area already documented in
`docs/windows-bringup.md`: `zealfie-gui.exe → pythonw.exe` is regenerated
with the correct (console vs windowed) shebang preserved via
`resolve_install_interpreter`.

Consequence: **any fix that mutates the generated `zealfie-gui.exe` (or
`pythonw.exe`) PE icon is lost on the next self-update**, because pip rewrites
that file. This rules out solution class A (post-install PE patching) as the
primary fix.

---

## 6. Evaluated alternatives

**A. Patch the generated `zealfie-gui.exe` PE icon after pip install.**
Rejected as primary: the launcher is regenerated by every self-update, and
— more importantly — the window is owned by `pythonw.exe`, so even a patched
launcher icon would not change the running taskbar button.

**B. A stable ZeAlfie-owned launcher `.exe` (custom PE icon) that runs the
private interpreter.** A thin branded launcher can own the correct icon, but
it still spawns `pythonw.exe` (which owns the window), so it does **not** by
itself fix the running taskbar icon. It is only useful if combined with the
AUMID→shortcut mapping (C). Not needed for the minimal fix.

**C. Start-Menu shortcut carrying BOTH the AppUserModelID and the canonical
icon (shell metadata).** **Recommended.** It is:
* **update-safe** — the shortcut is Inno-owned; pip self-update regenerates
  the appenv launchers but never touches the Start-Menu shortcut;
* no admin rights, no compiler/toolchain, no new third-party executable;
* compatible with the private appenv, offline bootstrap, and the
  python.exe/pythonw.exe isolation;
* uses the existing canonical `zealfie.ico`.

Mechanism: set the shortcut's `System.AppUserModel.ID` to
`ZeSoftware.ZeAlfie` (matching ZA-ICON-02) and keep `IconFilename` pointing
at `{app}\assets\zealfie.ico`. Then the shell maps the running process's
AUMID to the shortcut's icon for the taskbar button.

> Note: this is the standard Windows recipe for "Python/Qt app shows a
> generic taskbar icon". If a real-machine witness later shows it is still
> insufficient (e.g. because the shell resolves the icon from `pythonw.exe`
> before the AUMID mapping in some edge case), the fallback is the branded
> launcher (B) **combined with** C — a separate, still-bounded follow-up.
> That fallback does not require freezing the app.

---

## 7. Recommended architecture

1. **Keep** ZA-ICON-01 (`apply_app_icon`) and ZA-ICON-02
   (`apply_windows_app_identity`, AUMID `ZeSoftware.ZeAlfie`) unchanged.
2. **Add** an AppUserModelID to the Start-Menu shortcut at install time so it
   matches `ZeSoftware.ZeAlfie`, while it already points at
   `zealfie-gui.exe` with `IconFilename={app}\assets\zealfie.ico`.
3. (Optional, only if the witness requires it) a branded ZeAlfie launcher.

Implementation detail for ZA-ICON-03B: Inno's `[Icons]` has no native AUMID
parameter, so the AppUserModelID is set on the `.lnk` via a small `[Code]`
IShellLink/IPropertyStore call (or an equivalent bounded helper) — still
installer-owned, still update-safe.

---

## 8. Proposed lifecycle

* **Fresh install:** Inno creates the appenv launchers + the Start-Menu
  shortcut **with** `System.AppUserModel.ID = ZeSoftware.ZeAlfie` and the
  canonical icon.
* **Launch:** shortcut → `zealfie-gui.exe` → `pythonw.exe` → `run_gui()` sets
  the same AUMID before `QApplication` → window icon applied. The shell maps
  the running AUMID to the shortcut icon → correct taskbar icon.
* **Self-update (0.1.0 → 0.1.1 …):** pip regenerates `zealfie-gui.exe`/
  `zealfie.exe` inside the appenv; the Inno shortcut (AUMID + icon) is
  **untouched**, so the taskbar identity/icon survive.
* **Restart:** the detached self-update restart launches `pythonw.exe -c
  "from zealfie.gui import main; main()"` (or the regenerated launcher); the
  process re-applies the same AUMID; the shortcut mapping still resolves the
  icon.

---

## 9. Can 0.1.1 ship entirely through ZeAlfie self-update?

Yes — for the **application code** (the launchers + `zealfie` wheel). The
taskbar-icon fix is an **installer/launcher-shell concern**, so the
AppUserModelID-on-shortcut change lands via the **installer** (Inno), not via
self-update. Since the shortcut is not part of the pip-installed payload, a
user who installed via the fixed installer keeps the correct icon across all
subsequent `pip`-driven self-updates. (Users who installed *before* the fix
would need one re-run of the installer to pick up the shortcut AUMID; the
code-level AUMID from ZA-ICON-02 already ships in the wheel.)

---

## 10. POC results

None performed (no Windows runner in this environment). The POC is the
follow-up real-machine witness (ZA-ICON-03B): set the shortcut AUMID and
confirm the taskbar icon on a real Windows machine after a fresh install and
after a self-update.

---

## 11. Risks / open questions

* The exact shell resolution order (executable icon vs AUMID→shortcut icon)
  is established from documented Windows behavior but must be confirmed by a
  real-machine witness; the branded-launcher fallback (B + C) is the bounded
  escalation if C alone is insufficient.
* Setting the shortcut AUMID requires a small IShellLink/IPropertyStore
  `[Code]` step (no admin, no compiler) — to be confirmed in ZA-ICON-03B.
* `pythonw.exe` is shared across the whole ZeAlfie-managed ecosystem; we must
  NOT give it the ZeAlfie icon (that would misbrand every child product).
  The shortcut-AUMID approach keeps the icon scoped to the ZeAlfie shell.

---

## Human gate

**STOP here.** No implementation, no version bump, no tag/release/push. Await
explicit human approval of the recommended design (shortcut AppUserModelID +
canonical icon, retaining ZA-ICON-01/02) before ZA-ICON-03B.
