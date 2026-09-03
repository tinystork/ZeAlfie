; ============================================================================
; ZeAlfie — native Windows installer (ZA-WIN-BOOT-02)
;
; Produces ZeAlfie-Setup-<version>-dev.exe: a per-user, non-admin, fully
; OFFLINE installer that turns a normal Windows x64 machine into the proven
; ZA-WIN-BOOT-01 layout WITHOUT the user installing Python/venv/pip or
; touching a terminal:
;
;     {app}\python\   private pinned CPython 3.13 (silent per-user install)
;     {app}\appenv\   dedicated venv with the installed ZeAlfie
;     {app}\assets\   cpython installer + wheelhouse + bootstrap scripts + ico
;
; The installer WRAPS the proven architecture (private pinned CPython ->
; appenv -> ZeAlfie); it does NOT freeze it (no PyInstaller/Nuitka).  The
; appenv install is fully OFFLINE: pip runs with
; `--no-index --find-links {app}\assets\wheelhouse` — PyPI is never
; contacted on the user machine, and a repair/reinstall is offline too.
;
; Pin coupling: the CPython version/hash here MUST equal
; packaging/windows/reproducibility.toml and the Inno toolchain version MUST
; equal packaging/windows/installer/innosetup.toml — enforced by hermetic
; tests (tests/test_windows_installer.py) and by the CI workflow.
;
; Fail-closed posture:
;   * At ssPostInstall, before the bundled CPython installer runs, its
;     SHA-256 is verified in [Code] (GetSHA256OfFile) against the pinned
;     digest — a mismatch raises an exception that aborts Setup (non-zero
;     exit).
;   * Every bootstrap child process is launched with Exec + SW_HIDE +
;     ewWaitUntilTerminated and its EXIT CODE is observed: the python.org
;     installer must return 0/3010 and the appenv bootstrap python must
;     return 0 — any other result raises an exception (Setup aborts
;     non-zero; the declarative [Run] section is NOT used because it never
;     checks child exit codes).
;   * After the CPython install, {app}\python\python.exe MUST exist.
;   * After the offline appenv build, the four launchers
;     {app}\appenv\Scripts\{python.exe,pythonw.exe,zealfie.exe,
;     zealfie-gui.exe} MUST all exist — otherwise Setup aborts.  There is
;     never a silent fallback to a system Python.
;   * Hidden console != hidden failure: subprocesses are invisible
;     (SW_HIDE + CREATE_NO_WINDOW inside the Python bootstrap), but errors
;     surface via the Setup log, the {app}\logs files and the non-zero
;     Setup exit code.
;
; Non-goals respected: no PATH/launcher/file-association/global-shortcut
; pollution, no Authenticode signing, no public Release, no frozen single
; exe.  The user's separate %LOCALAPPDATA%\zealfie\runtime (shared runtime)
; is never read, written, or deleted by this installer.
; ============================================================================

#ifndef ZeAlfieVersion
  #define ZeAlfieVersion "0.1.0"
#endif
; CPython baseline MUST mirror packaging/windows/reproducibility.toml
; ([cpython] version/sha256) — enforced by tests/test_windows_installer.py.
#ifndef ZeAlfieCpythonVersion
  #define ZeAlfieCpythonVersion "3.13.15"
#endif
#ifndef ZeAlfieCpythonSha256
  #define ZeAlfieCpythonSha256 "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403"
#endif
#define CpythonInstallerName "python-" + ZeAlfieCpythonVersion + "-amd64.exe"
; Silent per-user CPython install properties — order mirrors
; reproducibility.toml [install] (switches are added by the [Code]
; ssPostInstall bootstrap).
#define CpythonInstallProperties "InstallAllUsers=0 PrependPath=0 Include_launcher=0 AssociateFiles=0 Shortcuts=0 Include_pip=1 Include_venv=1 Include_test=0 Include_doc=0 Include_tcltk=0"

; ---- Build inputs (absolute paths supplied by the CI workflow via /D=) ----
#ifndef CpythonInstaller
  #error "CpythonInstaller not defined - pass /DCpythonInstaller=<path to python-3.13.15-amd64.exe> (see docs/windows-installer.md)"
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
; Created before file extraction so the python.org installer and the
; Python bootstrap can write their logs here (bootstrap also mkdir -p it).
Name: "{app}\logs"

[Files]
; Bundled CPython installer (SHA-256 verified in [Code] before it runs).
Source: "{#CpythonInstaller}"; DestDir: "{app}\assets\cpython"; Flags: ignoreversion
; Bundled OFFLINE wheelhouse (exact lock-verified set + the zealfie wheel).
Source: "{#WheelhouseDir}\*.whl"; DestDir: "{app}\assets\wheelhouse"; Flags: ignoreversion
; Bootstrap primitives shared with the ZA-WIN-BOOT-01 witness.
Source: "{#BootstrapDir}\provision.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\provision_windows.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\installer_smoke.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\gui_smoke_offscreen.py"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
Source: "{#BootstrapDir}\reproducibility.toml"; DestDir: "{app}\assets\bootstrap"; Flags: ignoreversion
; Canonical icon (reused from src/zealfie/icon — never duplicated/altered).
Source: "{#IconFile}"; DestDir: "{app}\assets"; DestName: "zealfie.ico"; Flags: ignoreversion

[Icons]
; Normal launch = the installed windowed launcher; no terminal, no system
; Python/PATH/CWD/source-tree dependency.
Name: "{autoprograms}\ZeAlfie"; Filename: "{app}\appenv\Scripts\zealfie-gui.exe"; IconFilename: "{app}\assets\zealfie.ico"; WorkingDir: "{app}"; Comment: "Launch ZeAlfie"

[Code]
const
  CpythonSha256 = '{#ZeAlfieCpythonSha256}';
  CpythonExeName = '{#CpythonInstallerName}';
  CpythonInstallProps = '{#CpythonInstallProperties}';

{ Fail-closed integrity gate: the bundled CPython installer must match the
  pinned SHA-256 from packaging/windows/reproducibility.toml.  A mismatch
  raises an exception -> Setup aborts with a non-zero exit. }
procedure VerifyBundledCpythonIntegrity;
var
  FileName: String;
  Digest: String;
begin
  FileName := ExpandConstant('{app}\assets\cpython\' + CpythonExeName);
  Log('[zealfie] verifying bundled CPython installer SHA-256: ' + FileName);
  if not FileExists(FileName) then
    RaiseException('Bundled CPython installer is missing: ' + FileName);
  Digest := LowerCase(GetSHA256OfFile(FileName));
  Log('[zealfie] bundled CPython installer SHA-256: ' + Digest);
  if Digest <> CpythonSha256 then
    RaiseException('Bundled CPython installer failed SHA-256 verification: expected ' + CpythonSha256 + ', computed ' + Digest + '. Installation aborted (fail closed).');
  Log('[zealfie] bundled CPython installer SHA-256 verified OK');
end;

// After the silent per-user CPython install the ACTUAL interpreter must
// exist at {app}\python\python.exe — never assumed, never a fallback to a
// system Python.
procedure RequirePrivatePythonInstalled;
begin
  if not FileExists(ExpandConstant('{app}\python\python.exe')) then
    RaiseException('The private CPython runtime was not installed at {app}\python\python.exe. Installation aborted (fail closed — no fallback to a system Python).');
  Log('[zealfie] private CPython present: {app}\python\python.exe');
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
    RaiseException('The offline application environment is incomplete — missing appenv launchers:' + Missing + '. Installation aborted (fail closed).');
  Log('[zealfie] appenv complete: python.exe, pythonw.exe, zealfie.exe, zealfie-gui.exe all present');
end;

// Run a bootstrap child process hidden (SW_HIDE), wait for it, and return
// True ONLY when it ran AND its exit code was 0.  The declarative [Run]
// section never observes child exit codes, so the bootstrap is driven here
// where every failure can abort Setup (non-zero exit).
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

// Like RunCheckedZero but accepts the python.org installer's documented
// success codes 0 and 3010 ("success, reboot advised").
function RunCheckedCpython(const Filename, Params: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  Log('[zealfie] running hidden CPython installer: ' + Filename + ' ' + Params);
  if not Exec(Filename, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    Log('[zealfie] failed to launch the CPython installer: ' + Filename);
    Exit;
  end;
  Log('[zealfie] CPython installer exit code: ' + IntToStr(ResultCode));
  Result := (ResultCode = 0) or (ResultCode = 3010);
end;

// The installer's bootstrap is executed here (after the files are in
// place) so that every child exit code is observed and any failure aborts
// Setup with a non-zero exit — never a silent "success" screen.
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir: String;
begin
  if CurStep = ssPostInstall then begin
    AppDir := ExpandConstant('{app}');

    // 1) SHA-256 gate on the bundled CPython installer (fail closed).
    VerifyBundledCpythonIntegrity;

    // 2) Silent per-user install into {app}\python (0/3010 accepted).
    if not RunCheckedCpython(
         AppDir + '\assets\cpython\' + CpythonExeName,
         '/quiet /norestart ' + CpythonInstallProps +
         ' TargetDir="' + AppDir + '\python"' +
         ' /log "' + AppDir + '\logs\cpython-install.log"') then
      RaiseException('The private CPython installer exited non-zero (see the Setup log and ' + AppDir + '\logs\cpython-install.log). Installation aborted (fail closed).');

    // 3) The ACTUAL installed interpreter must exist (never assumed).
    RequirePrivatePythonInstalled;

    // 4) Build the appenv from THAT private python, installing ZeAlfie from
    //    the bundled wheelhouse ONLY (--no-index --find-links): offline.
    //    NOTE: --witness-root is a global option and MUST precede the
    //    make-appenv subcommand (argparse rejects it afterwards).
    if not RunCheckedZero(
         AppDir + '\python\python.exe',
         '"' + AppDir + '\assets\bootstrap\provision_windows.py"' +
         ' --witness-root "' + AppDir + '"' +
         ' make-appenv --offline-wheelhouse "' + AppDir + '\assets\wheelhouse"',
         AppDir + '\assets\bootstrap') then
      RaiseException('The offline appenv bootstrap exited non-zero (see ' + AppDir + '\logs\appenv-pip-install.log). Installation aborted (fail closed).');

    // 5) Completeness gate: all four appenv launchers must exist.
    RequireAppenvComplete;

    Log('[zealfie] offline appenv complete — ZeAlfie installed');
  end;
end;
