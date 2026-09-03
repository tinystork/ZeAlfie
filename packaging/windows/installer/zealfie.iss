; ============================================================================
; ZeAlfie — native Windows installer (ZA-WIN-BOOT-02; substrate ZA-WIN-BOOT-03B)
;
; Produces ZeAlfie-Setup-<version>-dev.exe: a per-user, non-admin, fully
; OFFLINE installer that turns a normal Windows x64 machine into the proven
; ZA-WIN-BOOT-01 layout WITHOUT the user installing Python/venv/pip or
; touching a terminal:
;
;     {app}\python\   private pinned CPython 3.13.15 — extracted
;                     python-build-standalone runtime (python.exe +
;                     pythonw.exe + Lib + bundled pip), plain files
;     {app}\appenv\   dedicated venv with the installed ZeAlfie
;     {app}\assets\   wheelhouse + bootstrap scripts + licenses + ico
;
; The substrate (ZA-WIN-BOOT-03B) is a python-build-standalone (astral-sh)
; install_only tarball, NOT the python.org executable installer: the
; python.org Burn/MSI bundle was a shared per-user provider whose
; MajorUpgrade semantics attached the private CPython to the host's
; same-minor python.org install.  The standalone runtime is plain
; relocatable files — no installer, no Burn/MSI, no PythonCore /
; Apps&Features / PATH / launcher / association state.  It is downloaded,
; SHA-256-verified and extracted at CI BUILD time (driver python, stdlib
; tarfile); THIS Setup embeds the already-extracted files — the end-user
; machine never needs tar, PowerShell archive extraction, 7-Zip, Python or
; network access to install.
;
; The installer WRAPS the proven architecture (private pinned CPython ->
; appenv -> ZeAlfie); it does NOT freeze it (no PyInstaller/Nuitka).  The
; appenv install is fully OFFLINE: pip runs with
; `--no-index --find-links {app}\assets\wheelhouse` — PyPI is never
; contacted on the user machine, and a repair/reinstall is offline too.
;
; Pin coupling: the substrate pin lives in
; packaging/windows/reproducibility.toml (bundled verbatim under
; {app}\assets\bootstrap) and is verified by the CI acquire step BEFORE the
; files are embedded; the Inno toolchain version MUST equal
; packaging/windows/installer/innosetup.toml — enforced by hermetic tests
; (tests/test_windows_installer.py) and by the CI workflow.
;
; Fail-closed posture:
;   * Every fatal condition sets the [Code] BootstrapFailed flag via
;     FailBootstrap and GetCustomSetupExitCode returns 2, so Setup exits
;     NON-ZERO even though Inno swallows exceptions raised from event
;     functions (RaiseException alone does not guarantee a non-zero exit).
;   * At ssPostInstall, BEFORE any appenv work, the private standalone
;     runtime must actually be in place as real files: BOTH
;     {app}\python\python.exe AND {app}\python\pythonw.exe MUST exist
;     (pythonw.exe is a hard functional requirement of the windowed GUI
;     launcher) — fail closed, never a PATH/system-Python fallback.  (The
;     tarball's own SHA-256 was already verified at CI build time; there is
;     no installer to re-verify at install time.)
;   * Every bootstrap child process is launched with Exec + SW_HIDE +
;     ewWaitUntilTerminated and its EXIT CODE is observed: the offline
;     appenv bootstrap python must return 0 — any other result fails the
;     bootstrap (the declarative [Run] section is NOT used because it never
;     checks child exit codes).
;   * After the offline appenv build, the four launchers
;     {app}\appenv\Scripts\{python.exe,pythonw.exe,zealfie.exe,
;     zealfie-gui.exe} MUST all exist — otherwise the bootstrap fails.
;   * Hidden console != hidden failure: subprocesses are invisible
;     (SW_HIDE + CREATE_NO_WINDOW inside the Python bootstrap), but errors
;     surface via the Setup log, the {app}\logs files and the non-zero
;     Setup exit code.
;
; Non-goals respected: no PATH/launcher/file-association/global-shortcut
; pollution, no Authenticode signing, no public Release, no frozen single
; exe.  The user's separate %LOCALAPPDATA%\zealfie\runtime (shared runtime)
; is never read, written, or deleted by this installer.  Uninstall removes
; the WHOLE {app} tree INCLUDING the private {app}\python runtime: {app}\python
; and {app}\assets are [Files]-registered (auto-removed), and the two
; bootstrap-created trees that are NOT [Files]-registered — {app}\appenv and
; {app}\logs — are deleted explicitly via [UninstallDelete] below.  Nothing
; on the host references any of it (no external registration), so nothing is
; preserved.
; ============================================================================

#ifndef ZeAlfieVersion
  #define ZeAlfieVersion "0.1.0"
#endif

; ---- Build inputs (absolute paths supplied by the CI workflow via /D=) ----
; PythonDir = the ALREADY-EXTRACTED python-build-standalone tree (the
; staging directory whose top level is python.exe/pythonw.exe/Lib), as
; produced by the CI acquire-standalone-python step.  Its SHA-256-verified
; archive provenance is recorded in reproducibility.toml (bundled below).
#ifndef PythonDir
  #error "PythonDir not defined - pass /DPythonDir=<staged standalone python dir> (see docs/windows-installer.md)"
#endif
#ifndef WheelhouseDir
  #error "WheelhouseDir not defined - pass /DWheelhouseDir=<acquired wheelhouse dir> (see docs/windows-installer.md)"
#endif
#ifndef BootstrapDir
  #error "BootstrapDir not defined - pass /DBootstrapDir=<packaging/windows dir> (see docs/windows-installer.md)"
#endif
#ifndef IconFile
  #error "IconFile not defined - pass /DIconFile=<canonical src/zealfie/icon/zealfie.ico> (see docs/windows-installer.md)"
#endif
#ifndef OutputDir
  #error "OutputDir not defined - pass /DOutputDir=<artifact dir> (see docs/windows-installer.md)"
#endif

[Setup]
; Stable AppId — a fixed GUID, NEVER regenerated (changing it would orphan
; the previous install's uninstall entry and Start Menu identity).
AppId={{27D66916-4144-491B-AEC9-4573C8E81D07}
AppName=ZeAlfie
AppVersion={#ZeAlfieVersion}
AppVerName=ZeAlfie {#ZeAlfieVersion}
AppPublisher=ZeSoftware
VersionInfoVersion={#ZeAlfieVersion}.0
VersionInfoCompany=ZeSoftware
VersionInfoDescription=ZeAlfie {#ZeAlfieVersion} (offline per-user install)
VersionInfoProductName=ZeAlfie
VersionInfoProductVersion={#ZeAlfieVersion}
DefaultDirName={localappdata}\Programs\ZeAlfie
DisableProgramGroupPage=yes
; Per-user, non-admin, always: no elevation, no /ALLUSERS override.
PrivilegesRequired=lowest
; Windows x64 ONLY (strict; x64os excludes ARM64 emulation hosts).
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
OutputBaseFilename=ZeAlfie-Setup-{#ZeAlfieVersion}-dev
OutputDir={#OutputDir}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\assets\zealfie.ico
UninstallDisplayName=ZeAlfie {#ZeAlfieVersion}
; The payload is dominated by already-compressed wheels; per-file deflate
; keeps compile time and memory sane without meaningful size loss.
Compression=zip
SolidCompression=no
SetupLogging=yes
RestartIfNeededByRun=no
CloseApplications=no
ShowLanguageDialog=no
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; Created before file extraction so the Python bootstrap can write its
; logs here (bootstrap also mkdir -p it).
Name: "{app}\logs"

[Files]
; Extracted python-build-standalone runtime (verified + extracted at CI
; BUILD time) — bundled as plain files, installed as {app}\python.
Source: "{#PythonDir}\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; Bundled OFFLINE wheelhouse (exact lock-verified set + the zealfie wheel).
Source: "{#WheelhouseDir}\*.whl"; DestDir: "{app}\assets\wheelhouse"; Flags: ignoreversion
; Bootstrap primitives shared with the ZA-WIN-BOOT-01 witness.
Source: "{#BootstrapDir}\provision.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\provision_windows.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\installer_smoke.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\gui_smoke_offscreen.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\reproducibility.toml"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
; Redistributed-runtime license material (PSF CPython license, verbatim
; from the verified archive, + provenance README).
Source: "{#BootstrapDir}\licenses\*"; DestDir: "{app}\assets\licenses"; Flags: ignoreversion
; Canonical icon (reused from src/zealfie/icon — never duplicated/altered).
Source: "{#IconFile}"; DestDir: "{app}\assets"; DestName: "zealfie.ico"; Flags: ignoreversion

[Icons]
; Normal launch = the installed windowed launcher; no terminal, no system
; Python/PATH/CWD/source-tree dependency.
Name: "{autoprograms}\ZeAlfie"; Filename: "{app}\appenv\Scripts\zealfie-gui.exe"; IconFilename: "{app}\assets\zealfie.ico"; WorkingDir: "{app}"; Comment: "Launch ZeAlfie"

[UninstallDelete]
; The appenv and the bootstrap logs are created at ssPostInstall by the
; bootstrap (NOT registered via [Files]), so the uninstaller must be told
; to delete them explicitly.  {app}\python and {app}\assets ARE registered
; via [Files] and auto-removed; these two entries cover the runtime-created
; trees, so the whole {app} tree (all of it installer-owned) is removed
; with the installer.  unins000.exe/.dat are Inno's own files and are
; handled by the uninstaller itself.
Type: filesandordirs; Name: "{app}\appenv"
Type: filesandordirs; Name: "{app}\logs"

[Code]
// Set True by every fatal bootstrap condition.  Inno swallows exceptions
// raised from event functions ('CurStepChanged raised an exception' in the
// Setup log) and would otherwise still exit 0, so the flag drives
// GetCustomSetupExitCode below to return a NON-ZERO Setup exit code.
var
  BootstrapFailed: Boolean;

// Every fatal path funnels through here: record the failure flag, log it,
// then raise.  RaiseException must never be called anywhere else in [Code].
procedure FailBootstrap(const Msg: String);
begin
  BootstrapFailed := True;
  Log('[zealfie] BOOTSTRAP FAILED: ' + Msg);
  RaiseException(Msg);
end;

// Fail-closed gate: the private standalone runtime must be in place as
// REAL files — BOTH the console interpreter AND the windowed interpreter
// (pythonw.exe is a hard functional requirement of the windowed GUI
// launcher).  Never assumed, never a fallback to a system Python.  The
// tarball's SHA-256 was verified at CI BUILD time before these files were
// embedded, so presence is the install-time integrity gate.
procedure RequirePrivatePythonFiles;
var
  Missing: String;
begin
  Missing := '';
  if not FileExists(ExpandConstant('{app}\python\python.exe')) then
    Missing := Missing + ' python.exe';
  if not FileExists(ExpandConstant('{app}\python\pythonw.exe')) then
    Missing := Missing + ' pythonw.exe';
  if Missing <> '' then
    FailBootstrap('The private Python runtime is incomplete at {app}\python — missing:' + Missing + '. Installation aborted (fail closed — no fallback to a system Python).');
  Log('[zealfie] private standalone runtime present: {app}\python\python.exe + pythonw.exe');
end;

// Completeness gate: the offline appenv must contain all four launchers
// before Setup reports success.
procedure RequireAppenvComplete;
var
  Missing: String;
begin
  Missing := '';
  if not FileExists(ExpandConstant('{app}\appenv\Scripts\python.exe')) then
    Missing := Missing + ' python.exe';
  if not FileExists(ExpandConstant('{app}\appenv\Scripts\pythonw.exe')) then
    Missing := Missing + ' pythonw.exe';
  if not FileExists(ExpandConstant('{app}\appenv\Scripts\zealfie.exe')) then
    Missing := Missing + ' zealfie.exe';
  if not FileExists(ExpandConstant('{app}\appenv\Scripts\zealfie-gui.exe')) then
    Missing := Missing + ' zealfie-gui.exe';
  if Missing <> '' then
    FailBootstrap('The offline application environment is incomplete — missing appenv launchers:' + Missing + '. Installation aborted (fail closed).');
  Log('[zealfie] appenv complete: python.exe, pythonw.exe, zealfie.exe, zealfie-gui.exe all present');
end;

// Run a bootstrap child process hidden (SW_HIDE), wait for it, and return
// True ONLY when it ran AND its exit code was 0.  The declarative [Run]
// section never observes child exit codes, so the bootstrap is driven here
// where every failure can set BootstrapFailed -> non-zero Setup exit.
function RunCheckedZero(const Filename, Params, WorkingDir: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  Log('[zealfie] running hidden: ' + Filename + ' ' + Params);
  if not Exec(Filename, Params, WorkingDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    Log('[zealfie] failed to launch: ' + Filename);
    Exit;
  end;
  Log('[zealfie] exit code ' + IntToStr(ResultCode) + ' from: ' + Filename);
  Result := (ResultCode = 0);
end;

// The installer's bootstrap is executed here (after the files are in
// place) so that every child exit code is observed and any failure sets
// BootstrapFailed -> Setup exits non-zero via GetCustomSetupExitCode —
// never a silent "success" exit code.
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir: String;
begin
  if CurStep = ssPostInstall then begin
    AppDir := ExpandConstant('{app}');

    // 1) The private standalone runtime must be present as real files:
    //    python.exe AND pythonw.exe (fail closed, never a fallback).
    RequirePrivatePythonFiles;

    // 2) Build the appenv from THAT private python, installing ZeAlfie from
    //    the bundled wheelhouse ONLY (--no-index --find-links): offline.
    //    NOTE: --witness-root is a global option and MUST precede the
    //    make-appenv subcommand (argparse rejects it afterwards).
    if not RunCheckedZero(
         AppDir + '\python\python.exe',
         '"' + AppDir + '\assets\bootstrap\provision_windows.py"' +
         ' --witness-root "' + AppDir + '"' +
         ' make-appenv --offline-wheelhouse "' + AppDir + '\assets\wheelhouse"',
         AppDir + '\assets\bootstrap') then
      FailBootstrap('The offline appenv bootstrap exited non-zero (see ' + AppDir + '\logs\appenv-pip-install.log). Installation aborted (fail closed).');

    // 3) Completeness gate: all four appenv launchers must exist.
    RequireAppenvComplete;

    Log('[zealfie] offline appenv complete — ZeAlfie installed');
  end;
end;

// Non-zero Setup exit code when any bootstrap step failed.  Inno swallows
// exceptions raised from event functions, so RaiseException alone does NOT
// guarantee a non-zero Setup exit; the BootstrapFailed flag is the
// authoritative failure signal.
// 2 = ZeAlfie bootstrap failed (private runtime / offline appenv).
function GetCustomSetupExitCode(): Integer;
begin
  if BootstrapFailed then
    Result := 2
  else
    Result := 0;
end;
