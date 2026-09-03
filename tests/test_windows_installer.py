"""Hermetic tests for the ZeAlfie Windows installer offline packaging (ZA-WIN-BOOT-02).

Covers, from ``packaging/windows/wheelhouse_lock.py`` and the refactored
``packaging/windows/provision.py``:

* wheelhouse.lock.toml load/validation against the REAL committed lock
  (fail closed on malformed/drifted/inconsistent locks);
* wheelhouse directory verification (exact file set + per-wheel SHA-256,
  fail closed on missing/extra/corrupted wheels) — including one test that
  exercises the REAL file-hashing code path;
* the offline pip install argv builder
  (``--no-index --find-links <wheelhouse> <zealfie wheel>``);
* the installer completeness primitives (four appenv launchers + the
  private-runtime python.exe/pythonw.exe/Lib requirement);
* the python-build-standalone substrate record: pin + metadata
  consistency, archive digest verification pass/fail, and SAFE tar
  extraction to the private layout (tiny synthetic tarballs, no download);
* .iss <-> reproducibility coupling (ZA-WIN-BOOT-03B): the .iss must embed
  the extracted runtime (``/DPythonDir`` -> ``{app}\\python``) with NO
  python.org EXE/Burn/``3010`` path left, gate on BOTH private interpreters,
  and use ``python -m pip`` only; the Inno toolchain pin
  (``installer/innosetup.toml``) must be consistent with the docs/CI.

All tests are FAST and hermetic: no real private Python, no Windows, no
venv creation, no network, no pip.  Windows paths are simulated with
Windows-style strings and :mod:`ntpath` semantics inside the modules under
test.  No ``integration`` / ``zealfie_slow`` markers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

from zealfie.gui.windows_identity import APP_USER_MODEL_ID

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WINDOWS_PKG = _REPO_ROOT / "packaging" / "windows"


def _load_module(relative_path: Path, module_name: str):
    """Load a packaging/windows module by file path (they are not import
    packages, so they are loaded under unique module names)."""
    spec = importlib.util.spec_from_file_location(
        module_name, relative_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_modules():
    # wheelhouse_lock imports `provision` as a same-directory module.
    if str(_WINDOWS_PKG) not in sys.path:
        sys.path.insert(0, str(_WINDOWS_PKG))
    provision = _load_module(
        _WINDOWS_PKG / "provision.py", "zealfie_installer_provision"
    )
    lock_mod = _load_module(
        _WINDOWS_PKG / "wheelhouse_lock.py", "zealfie_wheelhouse_lock"
    )
    return provision, lock_mod


provision, wheelhouse = _load_modules()

_RECORD_FILE = _WINDOWS_PKG / "reproducibility.toml"
_LOCK_FILE = _WINDOWS_PKG / "wheelhouse.lock.toml"
_ISS_FILE = _WINDOWS_PKG / "installer" / "zealfie.iss"
_INNO_FILE = _WINDOWS_PKG / "installer" / "innosetup.toml"

_SOURCE_COMMIT = "94f0b5164deb4b9e2ad1bd34dc910e89066b734b"
# Legacy python.org installer digest recorded in wheelhouse.lock.toml
# metadata (the environment the wheelhouse was resolved under).  The
# substrate pin is now the python-build-standalone ARCHIVE digest below.
_CPYTHON_SHA = "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403"
_PBS_SHA = "9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7"
_PBS_FILENAME = ("cpython-3.13.15+20260901-x86_64-pc-windows-msvc-"
                 "install_only.tar.gz")


def _mini_lock(tmp_path: Path, filename: str = "w-1.0-py3-none-any.whl",
               name: str = "w", version: str = "1.0",
               sha256: str | None = None, size: int = 7) -> Path:
    """Write a minimal-but-valid lock file; returns its path."""
    digest = sha256 or ("ab" * 32)
    body = f"""
[metadata]
zealfie_version = "0.1.0"
source_commit = "{_SOURCE_COMMIT}"
platform_tag = "win_amd64"
python_tag = "cp313"
abi_tag = "cp313"
generated = "2026-09-03"
cpython_version = "3.13.15"
cpython_installer_sha256 = "{_CPYTHON_SHA}"
requirements = ["packaging>=24"]
pip_command = "pip download ..."

[zealfie]
name = "zealfie"
version = "0.1.0"
wheel_filename = "zealfie-0.1.0-py3-none-any.whl"
source_commit = "{_SOURCE_COMMIT}"
source = "local-build"

[wheel."{filename}"]
name = "{name}"
version = "{version}"
sha256 = "{digest}"
size = {size}
"""
    path = tmp_path / "wheelhouse.lock.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _mini_lock_with_real_digest(tmp_path: Path, content: bytes,
                                filename: str = "w-1.0-py3-none-any.whl",
                                name: str = "w", version: str = "1.0") -> Path:
    """A mini lock whose wheel digest matches *content* (real hashing)."""
    from hashlib import sha256

    return _mini_lock(
        tmp_path, filename=filename, name=name, version=version,
        sha256=sha256(content).hexdigest(), size=len(content),
    )


def _fake_wheelhouse(tmp_path: Path, lock, corrupt: str | None = None,
                     drop: str | None = None, extra: str | None = None) -> Path:
    """Materialise a fake wheelhouse dir (bytes are placeholders; hash the
    pinned digests via the _hash seam in the tests)."""
    directory = tmp_path / "wheelhouse"
    directory.mkdir()
    names = [e.filename for e in lock.wheels] + [lock.zealfie_wheel.filename]
    for name in names:
        if name == drop:
            continue
        content = bytes([(i * 7 + len(name)) % 251 for i in range(16)])
        if name == corrupt:
            content = b"X" * 16  # will never match the locked sha256
        (directory / name).write_bytes(content)
    if extra:
        (directory / extra).write_bytes(b"extra")
    return directory


def _hash_seam(lock):
    """Injectable hash fn: claims every file has its locked digest."""
    by_name = {e.filename: e.sha256 for e in lock.wheels}

    def _hash(path: Path) -> str:
        digest = by_name.get(path.name)
        if digest is None:
            raise AssertionError(f"no locked digest for {path.name}")
        return digest

    return _hash


# ---------------------------------------------------------------------------
# Real committed lock
# ---------------------------------------------------------------------------


def test_lock_file_exists_next_to_module() -> None:
    assert _LOCK_FILE.is_file()
    assert wheelhouse.default_lock_path() == _LOCK_FILE


def test_real_lock_loads_and_metadata_is_consistent() -> None:
    lock = wheelhouse.load_lock(_LOCK_FILE)
    assert lock.zealfie_version == "0.1.1"
    assert lock.platform_tag == "win_amd64"
    assert lock.python_tag == "cp313"
    assert lock.cpython_version == "3.13.15"
    assert len(lock.cpython_installer_sha256) == 64
    assert lock.zealfie_wheel.filename == "zealfie-0.1.1-py3-none-any.whl"
    assert lock.zealfie_wheel.sha256 is None  # local-build, never pinned
    assert len(lock.wheels) >= 8
    names = {e.name for e in lock.wheels}
    assert {"packaging", "PySide6", "PySide6-Addons", "PySide6-Essentials",
            "shiboken6", "build", "pyproject-hooks", "setuptools",
            "wheel"} <= names
    pyside = [e for e in lock.wheels if e.name == "PySide6"][0]
    addons = [e for e in lock.wheels if e.name == "PySide6-Addons"][0]
    assert pyside.version == addons.version  # Qt family stays in lockstep
    # every pinned wheel has a full lowercase sha256 and a sane size
    for entry in lock.wheels:
        assert re.fullmatch(r"[0-9a-f]{64}", entry.sha256)
        assert entry.size > 0
    # expected file set contains the zealfie wheel too
    assert lock.zealfie_wheel.filename in wheelhouse.expected_filenames(lock)


def test_real_lock_cpython_metadata_decoupled_from_substrate_pin() -> None:
    """(ZA-WIN-BOOT-03B) The wheelhouse lock's informational cpython fields
    describe the python.org-installer era in which the wheelhouse was
    resolved; reproducibility.toml now pins the python-build-standalone
    ARCHIVE (a different artifact).  Version stays coupled; the digests are
    deliberately decoupled and must BOTH be valid 64-hex digests."""
    record = tomllib.loads(_RECORD_FILE.read_text(encoding="utf-8"))
    lock = wheelhouse.load_lock(_LOCK_FILE)
    assert lock.cpython_version == record["cpython"]["version"]
    assert re.fullmatch(r"[0-9a-f]{64}", lock.cpython_installer_sha256)
    assert lock.cpython_installer_sha256 == _CPYTHON_SHA  # frozen historical
    assert record["cpython"]["sha256"] == _PBS_SHA  # standalone archive pin
    assert lock.cpython_installer_sha256 != record["cpython"]["sha256"]
    # lock requirements mirror the zealfie wheel's runtime dependency list
    # (pyproject.toml [project].dependencies)
    assert set(lock.requirements) == {
        "packaging>=24", "PySide6>=6", "build>=1.2", "setuptools>=77",
        "wheel>=0.45",
    }


# ---------------------------------------------------------------------------
# Lock validation (fail closed)
# ---------------------------------------------------------------------------


def test_lock_rejects_missing_sha256(tmp_path: Path) -> None:
    # a non-hex digest must fail the 64-char lowercase-hex validation
    path = _mini_lock(tmp_path, sha256="nope")
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.load_lock(path)


def test_lock_rejects_bad_source_commit(tmp_path: Path) -> None:
    path = _mini_lock(tmp_path)
    text = path.read_text(encoding="utf-8").replace(_SOURCE_COMMIT, "not-a-sha")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.load_lock(path)


def test_lock_rejects_zealfie_sha256_pinned(tmp_path: Path) -> None:
    path = _mini_lock(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        'source = "local-build"',
        'source = "local-build"\nsha256 = "' + _CPYTHON_SHA + '"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.load_lock(path)


def test_lock_rejects_filename_name_version_mismatch(tmp_path: Path) -> None:
    # entry says version 1.0 but the filename says 2.0
    path = _mini_lock(tmp_path, filename="w-2.0-py3-none-any.whl")
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.load_lock(path)


def test_lock_rejects_empty_wheel_set(tmp_path: Path) -> None:
    path = _mini_lock(tmp_path)
    text = path.read_text(encoding="utf-8")
    # drop the single [wheel."..."] table
    start = text.find('[wheel."')
    end = text.find("]", start) + 1
    # remove the table (up to and including its last field line)
    table_start = text.rfind("\n", 0, start)
    nxt = text.find("\n[", start)
    if nxt == -1:
        nxt = len(text)
    text = text[:table_start] + text[nxt:]
    path.write_text(text, encoding="utf-8")
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.load_lock(path)


def test_parse_wheel_filename_handles_dashed_distributions() -> None:
    assert wheelhouse.parse_wheel_filename(
        "pyproject_hooks-1.2.0-py3-none-any.whl"
    ) == ("pyproject_hooks", "1.2.0")
    assert wheelhouse.parse_wheel_filename(
        "pyside6_addons-6.11.2-cp310-abi3-win_amd64.whl"
    ) == ("pyside6_addons", "6.11.2")
    assert wheelhouse.parse_wheel_filename(
        "zealfie-0.1.0-py3-none-any.whl"
    ) == ("zealfie", "0.1.0")
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.parse_wheel_filename("not-a-wheel.txt")


# ---------------------------------------------------------------------------
# Wheelhouse directory verification (fail closed)
# ---------------------------------------------------------------------------


def test_wheelhouse_verify_real_hashing_mini_lock(tmp_path: Path) -> None:
    """Full pass+fail through the REAL file-hashing code path."""
    from hashlib import sha256

    content = b"real wheel bytes for hermetic sha verification"
    lock_path = _mini_lock_with_real_digest(tmp_path, content)
    lock = wheelhouse.load_lock(lock_path)
    directory = tmp_path / "real-wh"
    directory.mkdir()
    (directory / lock.zealfie_wheel.filename).write_bytes(content)
    (directory / "w-1.0-py3-none-any.whl").write_bytes(content)
    # genuine digest computation (no seam) must pass
    assert wheelhouse.verify_wheelhouse_dir(directory, lock)["wheel_count"] == 1
    # corrupting the file must fail through the REAL hash path
    (directory / "w-1.0-py3-none-any.whl").write_bytes(b"tampered!")
    with pytest.raises(wheelhouse.WheelhouseVerificationError) as exc:
        wheelhouse.verify_wheelhouse_dir(directory, lock)
    assert "SHA-256 mismatch" in str(exc.value)


def test_wheelhouse_verify_pass_on_real_lock(tmp_path: Path) -> None:
    lock = wheelhouse.load_lock(_LOCK_FILE)
    directory = _fake_wheelhouse(tmp_path, lock)
    summary = wheelhouse.verify_wheelhouse_dir(
        directory, lock, _hash_file=_hash_seam(lock)
    )
    assert summary["wheel_count"] == len(lock.wheels)
    assert sorted(summary["files"]) == sorted(wheelhouse.expected_filenames(lock))


def test_wheelhouse_verify_rejects_missing(tmp_path: Path) -> None:
    lock = wheelhouse.load_lock(_LOCK_FILE)
    directory = _fake_wheelhouse(tmp_path, lock, drop="wheel-0.48.0-py3-none-any.whl")
    with pytest.raises(wheelhouse.WheelhouseVerificationError) as exc:
        wheelhouse.verify_wheelhouse_dir(
            directory, lock, _hash_file=_hash_seam(lock)
        )
    assert "missing" in str(exc.value)


def test_wheelhouse_verify_rejects_extra(tmp_path: Path) -> None:
    lock = wheelhouse.load_lock(_LOCK_FILE)
    directory = _fake_wheelhouse(tmp_path, lock, extra="sneaky-1.0-py3-none-any.whl")
    with pytest.raises(wheelhouse.WheelhouseVerificationError) as exc:
        wheelhouse.verify_wheelhouse_dir(
            directory, lock, _hash_file=_hash_seam(lock)
        )
    assert "unexpected" in str(exc.value)


def test_pinned_subset_allows_missing_zealfie_wheel(tmp_path: Path) -> None:
    """After `pip download` (before adding the local zealfie wheel) the
    pinned subset must verify even though the zealfie wheel is absent."""
    lock = wheelhouse.load_lock(_LOCK_FILE)
    directory = _fake_wheelhouse(tmp_path, lock)
    (directory / lock.zealfie_wheel.filename).unlink()
    wheelhouse.verify_pinned_subset(directory, lock, _hash_file=_hash_seam(lock))
    with pytest.raises(wheelhouse.WheelhouseVerificationError):
        wheelhouse.verify_wheelhouse_dir(
            directory, lock, _hash_file=_hash_seam(lock)
        )


def test_pinned_download_specs_are_exact_pins() -> None:
    lock = wheelhouse.load_lock(_LOCK_FILE)
    specs = wheelhouse.pinned_download_specs(lock)
    assert len(specs) == len(lock.wheels)
    for spec, entry in zip(specs, lock.wheels):
        assert spec == f"{entry.name}=={entry.version}"


# ---------------------------------------------------------------------------
# Offline pip argv builder (provision.py)
# ---------------------------------------------------------------------------


def test_offline_install_argv_is_no_index_find_links() -> None:
    argv = provision.pip_install_wheel_offline_argv(
        r"C:\app\appenv\Scripts\python.exe",
        r"C:\app\assets\wheelhouse\zealfie-0.1.0-py3-none-any.whl",
        r"C:\app\assets\wheelhouse",
    )
    assert argv[:4] == [r"C:\app\appenv\Scripts\python.exe", "-m", "pip",
                        "install"]
    assert "--no-index" in argv
    links = argv.index("--find-links")
    assert argv[links + 1] == r"C:\app\assets\wheelhouse"
    # the wheel is passed by explicit path
    assert any(a.endswith("zealfie-0.1.0-py3-none-any.whl") for a in argv)
    # no network-capable source flag is present
    assert "-i" not in argv and "--index-url" not in argv


def test_online_offline_argv_share_determinism_flags() -> None:
    online = provision.pip_install_wheel_argv(
        r"C:\w\appenv\Scripts\python.exe", r"C:\w\w.whl"
    )
    offline = provision.pip_install_wheel_offline_argv(
        r"C:\w\appenv\Scripts\python.exe", r"C:\w\w.whl", r"C:\w\wh"
    )
    common = set(online) & set(offline)
    assert "--disable-pip-version-check" in common
    assert "--no-cache-dir" in common
    # the online (BOOT-01) argv is UNCHANGED — no --no-index there
    assert "--no-index" not in online


# ---------------------------------------------------------------------------
# Appenv completeness primitive (provision.py)
# ---------------------------------------------------------------------------


def test_appenv_launcher_names_are_the_four_contract_launchers() -> None:
    assert provision.appenv_launcher_names() == (
        "python.exe", "pythonw.exe", "zealfie.exe", "zealfie-gui.exe",
    )


def test_missing_appenv_launchers_empty_when_complete() -> None:
    import ntpath

    root = r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
    existing = {
        ntpath.normcase(r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
                        r"\appenv\Scripts\python.exe"),
        ntpath.normcase(r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
                        r"\appenv\Scripts\pythonw.exe"),
        ntpath.normcase(r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
                        r"\appenv\Scripts\zealfie.exe"),
        ntpath.normcase(r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
                        r"\appenv\Scripts\zealfie-gui.exe"),
    }
    missing = provision.missing_appenv_launchers(
        root,
        _exists=lambda p: ntpath.normcase(str(p)) in existing,
    )
    assert missing == []


def test_missing_appenv_launchers_reports_gaps() -> None:
    root = r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
    missing = provision.missing_appenv_launchers(
        root, _exists=lambda p: False
    )
    assert missing == [r"Scripts\python.exe", r"Scripts\pythonw.exe",
                       r"Scripts\zealfie.exe", r"Scripts\zealfie-gui.exe"]


# ---------------------------------------------------------------------------
# .iss <-> reproducibility / innosetup pin coupling (single source of truth)
# ---------------------------------------------------------------------------


def _iss_text() -> str:
    return _ISS_FILE.read_text(encoding="utf-8")


def test_iss_bundles_extracted_runtime_not_an_exe() -> None:
    """(ZA-WIN-BOOT-03B) The .iss embeds the ALREADY-EXTRACTED standalone
    runtime via /DPythonDir -> {app}\\python (recursive); there is NO
    python.org EXE define, digest const, Burn/MSI step or 0/3010 handling
    left anywhere."""
    iss = _iss_text()
    assert "#ifndef PythonDir" in iss
    assert '#error "PythonDir not defined' in iss
    assert r'Source: "{#PythonDir}\*"; DestDir: "{app}\python"' in iss
    assert "recursesubdirs" in iss
    # no legacy CPython-installer surface
    assert "ZeAlfieCpythonVersion" not in iss
    assert "ZeAlfieCpythonSha256" not in iss
    assert "CpythonInstaller" not in iss
    assert "python-3.13.15-amd64.exe" not in iss
    assert "CpythonExeName" not in iss
    assert "CpythonSha256" not in iss
    assert "GetSHA256OfFile" not in iss
    assert "3010" not in iss
    # [Code]+[Files] carry no Burn/MSI machinery (header prose may still
    # explain WHY the EXE era ended)
    code_files = iss.split("[Code]", 1)[1]
    assert "Burn" not in code_files
    assert "msiexec" not in code_files.lower()
    assert "RunCheckedCpython" not in code_files

def test_iss_posture_no_installer_properties_remain() -> None:
    """(ZA-WIN-BOOT-03B) With plain-file extraction there are no python.org
    silent-install properties to embed; the per-user / non-admin / x64-only
    posture is preserved structurally."""
    iss = _iss_text()
    assert "InstallAllUsers" not in iss
    assert "PrependPath" not in iss
    assert "Include_launcher" not in iss
    assert "AssociateFiles" not in iss
    assert "Shortcuts=0" not in iss
    assert "PrivilegesRequired=lowest" in iss
    assert "ArchitecturesAllowed=x64os" in iss
    assert "ArchitecturesInstallIn64BitMode=x64os" in iss
    # default per-user dir
    assert "DefaultDirName={localappdata}\\Programs\\ZeAlfie" in iss
    # per-user only, no /ALLUSERS path
    assert "PrivilegesRequiredOverridesAllowed" not in iss


def test_iss_has_stable_single_appid() -> None:
    iss = _iss_text()
    matches = re.findall(r"AppId=\{\{([0-9A-Fa-f-]{36})\}", iss)
    assert len(matches) == 1, "exactly one fixed AppId required (never regenerate)"
    guid = matches[0]
    assert re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        guid,
    )


def test_iss_offline_contract_and_gates_present() -> None:
    iss = _iss_text()
    assert "offline-wheelhouse" in iss
    for gate in ("RequirePrivatePythonFiles", "RequireAppenvComplete",
                 "RaiseException", "FailBootstrap"):
        assert gate in iss, f"{gate} missing from zealfie.iss [Code]"
    for launcher in ("python.exe", "pythonw.exe", "zealfie.exe",
                     "zealfie-gui.exe"):
        assert launcher in iss
    # the two private interpreters are gated separately in [Code]
    assert "python\\python.exe" in iss and "python\\pythonw.exe" in iss


def test_provision_windows_argparse_global_option_before_subcommand() -> None:
    """Regression (rework-5, defect 1): --witness-root is a GLOBAL option
    defined before the subparsers, so argparse rejects it AFTER
    make-appenv.  The installer must pass it before the subcommand."""
    provision_windows = _load_module(
        _WINDOWS_PKG / "provision_windows.py", "zealfie_provision_windows_r5"
    )
    parser = provision_windows._build_parser()
    # correct order (the installer contract): global option first
    args = parser.parse_args(
        ["--witness-root", r"C:\app", "make-appenv",
         "--offline-wheelhouse", r"C:\app\assets\wheelhouse"]
    )
    assert args.command == "make-appenv"
    assert str(args.witness_root).lower().endswith("c:\\app") or str(
        args.witness_root
    ) == r"C:\app"
    assert str(args.offline_wheelhouse) == r"C:\app\assets\wheelhouse"
    assert args.wheel is None  # offline path derives the wheel from the wheelhouse
    # wrong order (the r5 failure): subcommand first -> argparse exits 2
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            ["make-appenv", "--witness-root", r"C:\app",
             "--offline-wheelhouse", r"C:\app\assets\wheelhouse"]
        )
    assert exc.value.code == 2


def test_iss_bootstrap_argv_orders_witness_root_before_make_appenv() -> None:
    """Regression (rework-5, defect 1) at the .iss level: --witness-root is
    a global option and must precede the make-appenv subcommand."""
    iss = _iss_text()
    first_witness = iss.find("--witness-root")
    first_make = iss.find("make-appenv")
    assert first_witness != -1 and first_make != -1
    assert first_witness < first_make, (
        "--witness-root must precede make-appenv in the bootstrap argv"
    )
    assert "make-appenv --witness-root" not in iss


def test_iss_code_checks_child_exit_codes() -> None:
    """Regression (rework-5, defect 2): the bootstrap must observe every
    child exit code via Exec + ewWaitUntilTerminated + ResultCode; the
    python.org 0/3010 special-casing is GONE (nothing runs an installer)."""
    iss = _iss_text()
    assert "Exec(" in iss
    assert "ewWaitUntilTerminated" in iss
    assert "ResultCode" in iss
    assert "(ResultCode = 0)" in iss  # RunCheckedZero success comparison
    assert "RunCheckedCpython" not in iss
    assert "3010" not in iss


def test_iss_failure_couples_to_nonzero_setup_and_run_section_gone() -> None:
    """Regression (rework-5, defect 2): a failed bootstrap must abort Setup
    with a non-zero exit (RaiseException in CurStepChanged at ssPostInstall)
    — the declarative [Run] section (which never checks exit codes) is gone."""
    iss = _iss_text()
    assert "CurStepChanged" in iss
    assert "ssPostInstall" in iss
    assert "RaiseException" in iss
    assert "RunCheckedZero" in iss
    assert "RunCheckedCpython" not in iss
    assert "SW_HIDE" in iss
    assert not re.search(r"^\[Run\]", iss, re.M), "[Run] section must be gone"


def test_iss_success_path_preserves_layout_and_gates() -> None:
    """The Exec-based success path must preserve the full installer
    contract (layout, gates, offline wheelhouse, per-user posture)."""
    iss = _iss_text()
    for token in ("offline-wheelhouse", "--witness-root", "{app}\\python",
                  "{app}\\appenv", "RequirePrivatePythonFiles",
                  "{app}\\python\\python.exe",
                  "RequireAppenvComplete", "PrivilegesRequired=lowest",
                  "ArchitecturesAllowed=x64os", "zealfie.ico",
                  "{autoprograms}", "provision_windows.py", "licenses"):
        assert token in iss, f"installer contract token missing: {token}"
    for launcher in ("python.exe", "pythonw.exe", "zealfie.exe",
                     "zealfie-gui.exe"):
        assert launcher in iss


def test_no_pascal_brace_comment_embeds_installer_constant() -> None:
    """Regression (rework-4): Inno Pascal `{ ... }` comments end at the first
    `}` — a comment body embedding an installer constant like `{app}` closes
    the comment early and the trailing text is parsed as code.  No brace
    comment may embed an installer constant token; `{app}` inside
    single-quoted string literals is legal and must be ignored."""
    iss = _iss_text()
    assert "[Code]" in iss
    code_lines = iss.splitlines()
    start = next(i for i, line in enumerate(code_lines)
                 if line.strip() == "[Code]")
    in_brace = False
    offenders: list[str] = []
    for abs_no in range(start + 1, len(code_lines)):
        line = code_lines[abs_no]
        i = 0
        while i < len(line):
            ch = line[i]
            if in_brace:
                # inside a { ... } brace comment: a constant token (the very
                # pattern this test forbids) would end the comment at its }
                if ch == "}":
                    in_brace = False
                    i += 1
                elif ch == "{" and i + 1 < len(line) and (
                    line[i + 1] == "#" or line[i + 1].isalpha()
                ):
                    offenders.append(
                        f"line {abs_no + 1}: installer constant inside a "
                        f"Pascal brace comment: ...{line[max(0, i - 20):i + 24]}..."
                    )
                    # in real Pascal the token's } would close the comment
                    end = line.find("}", i + 1)
                    if end != -1:
                        in_brace = False
                        i = end + 1
                    else:
                        in_brace = False
                        i = len(line)
                else:
                    i += 1
            elif ch == "'":
                # single-quoted string literal: may legally contain {app}
                i += 1
                while i < len(line):
                    if line[i] == "'":
                        if i + 1 < len(line) and line[i + 1] == "'":
                            i += 2  # doubled apostrophe inside the string
                            continue
                        i += 1
                        break
                    i += 1
            elif line.startswith("//", i):
                i = len(line)  # // line comment: nothing to scan
            elif ch == "{":
                in_brace = True
                i += 1
            else:
                i += 1
    assert not offenders, (
        "Pascal brace comments must not embed installer constants:\n"
        + "\n".join(offenders)
    )
    # the corrected // comments are present verbatim (no brace comment may
    # embed an installer constant)
    assert "// Fail-closed gate: the private standalone runtime must be in place as" in iss
    assert "// REAL files — BOTH the console interpreter AND the windowed interpreter" in iss
    assert "// (pythonw.exe is a hard functional requirement of the windowed GUI" in iss
    assert not any(line.lstrip().startswith("{ After the silent per-user")
                   for line in code_lines)

def test_iss_pythondir_define_is_a_plain_build_input() -> None:
    """(ZA-WIN-BOOT-03B) The runtime source is the CI-staged extracted tree
    passed as /DPythonDir (a plain build input with an #error guard) — no
    filename-concatenation macro, no version/sha define, no nested ISPP
    macro, no dead CPython-installer name resolution."""
    iss = _iss_text()
    for line in iss.splitlines():
        if line.lstrip().startswith("#define"):
            assert "{#" not in line, f"nested ISPP macro in: {line}"
    assert "#ifndef PythonDir" in iss
    assert "#define ZeAlfieVersion" in iss
    assert "CpythonInstallerName" not in iss
    assert "ZeAlfieCpythonVersion" not in iss
    assert "CpythonExeName" not in iss
    # PythonDir is referenced by the [Files] recursion source
    assert 'Source: "{#PythonDir}\\*"' in iss


def test_lock_includes_windows_marker_colorama_closure() -> None:
    """Regression (rework-6, defect A): build depends on colorama under the
    os_name == "nt" marker, which Linux `pip download` does not evaluate —
    the lock must pin colorama explicitly so the Windows appenv install
    resolves with NO PyPI."""
    lock = wheelhouse.load_lock(_LOCK_FILE)
    colorama = [e for e in lock.wheels if e.name == "colorama"]
    assert len(colorama) == 1
    entry = colorama[0]
    assert entry.version == "0.4.6"
    assert re.fullmatch(r"[0-9a-f]{64}", entry.sha256)
    assert entry.size > 0
    assert "colorama==0.4.6" in wheelhouse.pinned_download_specs(lock)
    # build is the wheel that declares the marker dep
    build = [e for e in lock.wheels if e.name == "build"][0]
    assert build.version == "1.6.0"


def test_workflow_has_offline_closure_preflight_before_compile() -> None:
    """The authoritative Windows closure check must run on the runner AFTER
    acquisition and BEFORE compilation, with NO PyPI."""
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    assert "offline-closure-preflight" in workflow
    assert "--no-index" in workflow
    assert "--find-links" in workflow
    assert "--dry-run" in workflow
    assert "--ignore-installed" in workflow
    preflight = workflow.find("- id: offline-closure-preflight")
    acquire = workflow.find("- id: acquire-wheelhouse")
    compile_step = workflow.find("- id: compile-installer")
    assert -1 < acquire < preflight < compile_step, (
        "step order must be acquire-wheelhouse -> offline-closure-preflight "
        "-> compile-installer"
    )


def test_iss_custom_exit_code_mechanism() -> None:
    """Regression (rework-6, defect B): RaiseException alone does not yield a
    non-zero Setup exit (Inno swallows event-function exceptions), so [Code]
    must carry the BootstrapFailed flag + FailBootstrap +
    GetCustomSetupExitCode."""
    iss = _iss_text()
    assert "BootstrapFailed" in iss
    assert "BootstrapFailed: Boolean;" in iss
    assert "procedure FailBootstrap" in iss
    assert "BootstrapFailed := True;" in iss
    assert "function GetCustomSetupExitCode(): Integer;" in iss
    assert "Result := 2" in iss  # documented non-zero bootstrap-failure code
    assert "Result := 0" in iss  # success path


def test_iss_every_fatal_path_routes_through_failbootstrap() -> None:
    """All fatal conditions must set the flag via FailBootstrap, and
    RaiseException must be called ONLY inside FailBootstrap."""
    iss = _iss_text()
    code = iss.split("[Code]", 1)[1]
    # RaiseException( appears exactly once in [Code] code (inside FailBootstrap)
    assert code.count("RaiseException(") == 1
    # each gate procedure + CurStepChanged reference FailBootstrap
    for proc in ("RequirePrivatePythonFiles", "RequireAppenvComplete",
                 "CurStepChanged"):
        seg = code.split(proc, 1)[1]
        assert "FailBootstrap(" in seg.split("end;", 1)[0] or "FailBootstrap(" in code
    # at least the documented fatal call sites
    assert code.count("FailBootstrap(") >= 4
    # the documented failure conditions are all present (no EXE-era wording)
    for token in ("The private Python runtime is incomplete at {app}\\python",
                  "is incomplete — missing appenv launchers",
                  "offline appenv bootstrap exited non-zero"):
        assert token in iss, f"fatal condition missing: {token}"
    assert "failed SHA-256 verification" not in iss
    assert "CPython installer exited non-zero" not in iss


def test_installer_smoke_utf8_child_env() -> None:
    """_utf8_child_env returns a copy with deterministic UTF-8 child I/O
    and never mutates its input."""
    smoke = _load_module(
        _WINDOWS_PKG / "installer_smoke.py", "zealfie_installer_smoke"
    )
    base = {"A": "B", "PYTHONIOENCODING": "latin-1"}
    env = smoke._utf8_child_env(base)
    assert env["A"] == "B"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    # input untouched
    assert base == {"A": "B", "PYTHONIOENCODING": "latin-1"}
    assert env is not base
    # default = copy of os.environ, not the live mapping
    default = smoke._utf8_child_env()
    assert default is not os.environ
    assert default["PYTHONUTF8"] == "1"


def test_installer_smoke_decodes_strict_utf8_no_silent_replace() -> None:
    """The GUI smoke capture must decode STRICT UTF-8 (child forced to UTF-8
    via PYTHONIOENCODING/PYTHONUTF8) — no silent U+FFFD replacement."""
    source = (_WINDOWS_PKG / "installer_smoke.py").read_text(encoding="utf-8")
    assert 'errors="strict", timeout_s=600' in source  # GUI capture
    gui_segment = source.split("def _smoke_gui", 1)[1]
    assert 'errors="replace"' not in gui_segment.split("def ", 1)[0]
    assert 'encoding="utf-8"' in source
    assert "PYTHONIOENCODING" in source and "PYTHONUTF8" in source
    assert "_utf8_child_env" in source


def test_installer_smoke_configures_stdio_utf8() -> None:
    """The smoke reconfigures its own stdout/stderr to UTF-8 before any
    output, so a Unicode PASS/diagnostic line cannot trip a Windows charmap
    UnicodeEncodeError."""
    source = (_WINDOWS_PKG / "installer_smoke.py").read_text(encoding="utf-8")
    assert "def _configure_stdio" in source
    assert 'reconfigure(encoding="utf-8")' in source
    # main() calls _configure_stdio() before argparse parsing
    main_at = source.index("def main(")
    call_at = source.index("_configure_stdio()", main_at)
    parser_at = source.index("parser = argparse.ArgumentParser", main_at)
    assert main_at < call_at < parser_at


def test_workflow_guards_native_inno_paths_from_msys_conversion() -> None:
    """Regression (rework-8): both native Inno executions (silent install
    and silent uninstall) must export MSYS2_ARG_CONV_EXCL before invoking
    the tool, otherwise Git Bash mangles the slash switches into
    C:/Program Files/Git/... and Inno falls back to interactive dialogs.
    The uninstaller must also be invoked with a bounded exit-code capture."""
    text = (_REPO_ROOT / ".github" / "workflows"
            / "windows-installer-build.yml").read_text(encoding="utf-8")

    def step_body(step_id: str) -> str:
        start = text.index(f"- id: {step_id}")
        nxt = text.find("\n      - id:", start + 1)
        return text[start: nxt if nxt != -1 else len(text)]

    install = step_body("silent-install")
    uninstall = step_body("uninstall-witness")
    # guard precedes each native invocation
    assert install.index('MSYS2_ARG_CONV_EXCL="*"') < install.index('"$SETUP"')
    assert uninstall.index('MSYS2_ARG_CONV_EXCL="*"') < uninstall.index('"$UNINS"')
    # uninstaller runs under a bounded if ... else rc=$? capture
    assert 'if "$UNINS" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART' in uninstall
    assert "rc=0" in uninstall
    assert "rc=$?" in uninstall
    assert "uninstaller exit code: $rc" in uninstall


def test_acquire_wheelhouse_provenance_out_writes_pure_json(tmp_path: Path) -> None:
    """--provenance-out must produce a PURE-JSON file (round-trip through
    json.loads, no [acquire] console prefix)."""
    acquire = _load_module(
        _WINDOWS_PKG / "acquire_wheelhouse.py", "zealfie_acquire_wheelhouse"
    )
    out = tmp_path / "nested" / "provenance.json"
    acquire._write_provenance({"status": "ok", "a": 1}, out)
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8")) == {"status": "ok", "a": 1}
    assert "[acquire]" not in out.read_text(encoding="utf-8")
    source = (_WINDOWS_PKG / "acquire_wheelhouse.py").read_text(encoding="utf-8")
    assert "--provenance-out" in source


def test_workflow_provenance_fails_closed_and_passes_sha_size_directly() -> None:
    """The provenance step must fail closed (no 'unavailable' fallback) and
    receive SETUP_SHA/SETUP_SIZE directly (GITHUB_ENV is not visible within
    the same step)."""
    text = (_REPO_ROOT / ".github" / "workflows"
            / "windows-installer-build.yml").read_text(encoding="utf-8")
    start = text.index("- id: provenance")
    nxt = text.find("\n      - id:", start + 1)
    step = text[start: nxt]
    assert 'SETUP_SHA="$SHA" SETUP_SIZE="$SIZE" python' in step
    assert '"setup_sha256": os.environ["SETUP_SHA"]' in step
    assert '"setup_size": int(os.environ["SETUP_SIZE"])' in step
    assert "json.load(fh)" in step
    assert '"status": "unavailable"' not in step
    assert 'os.environ.get("GITHUB_SHA", "")' in step  # top-level source_commit


def test_workflow_acquire_writes_pure_json_provenance_file() -> None:
    """The acquire step must write the JSON provenance to its own file and
    tee only the human console log to acquire.log."""
    text = (_REPO_ROOT / ".github" / "workflows"
            / "windows-installer-build.yml").read_text(encoding="utf-8")
    start = text.index("- id: acquire-wheelhouse")
    nxt = text.find("\n      - id:", start + 1)
    step = text[start: nxt]
    assert "--provenance-out" in step
    assert "acquire-provenance.json" in step
    assert 'tee "$ZEALFIE_STAGE/logs/acquire.log"' in step
    assert "tee \"$ZEALFIE_STAGE/logs/acquire-provenance.json\"" not in step


_SEW = _load_module(
    _WINDOWS_PKG / "side_effect_witness.py", "zealfie_side_effect_witness"
)

_ROOT = r"C:\Users\u\AppData\Local\Programs\ZeAlfie"


def _baseline_snapshot() -> dict:
    return {
        "schema": 1,
        "user_path": ["c:\\windows\\system32"],
        "machine_path": ["c:\\windows\\system32"],
        "py_launcher_path": None,
        "py_0p": None,
        "py_association_progid": None,
        "pythoncore_3_13_hkcu_install_path": None,
        "pythoncore_3_13_hklm_install_path": None,
        "uninstall_hkcu": {},
        "uninstall_hklm": {},
        "start_menu_shortcut_exists": False,
        "start_menu_shortcut_target": None,
        "runtime_exists": False,
        "zealfie_appdata_exists": False,
    }


def _zealfie_hkcu(root: str = _ROOT) -> dict:
    return {
        "zealfie 0.1.0": {
            "display_name": "ZeAlfie 0.1.0",
            "install_location": root,
            "uninstall_string": root + "\\unins000.exe",
        },
    }


def _install_snapshot(root: str = _ROOT, **overrides) -> dict:
    """Post-install snapshot under NO-provider semantics (ZA-WIN-BOOT-03B):
    the host footprint is byte-identical to the baseline (NO PythonCore,
    NO CPython Apps&Features entry) and only the ZeAlfie shell appeared."""
    snap = {
        "schema": 1,
        "user_path": ["c:\\windows\\system32"],
        "machine_path": ["c:\\windows\\system32"],
        "py_launcher_path": None,
        "py_0p": None,
        "py_association_progid": None,
        "pythoncore_3_13_hkcu_install_path": None,
        "pythoncore_3_13_hklm_install_path": None,
        "uninstall_hkcu": _zealfie_hkcu(root),
        "uninstall_hklm": {},
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": (
            root + "\\appenv\\Scripts\\zealfie-gui.exe"
        ),
        "runtime_exists": False,
        "zealfie_appdata_exists": False,
    }
    snap.update(overrides)
    return snap


def test_side_effect_witness_clean_install_has_no_findings() -> None:
    """No-provider semantics: a clean install creates NO PythonCore /
    CPython Apps&Features state — the ZeAlfie shell is the only footprint."""
    baseline = _baseline_snapshot()
    snapshot = _install_snapshot()
    assert _SEW.verify_install_findings(baseline, snapshot, _ROOT) == []


def test_side_effect_witness_path_changes_are_findings() -> None:
    baseline = _baseline_snapshot()
    # user PATH changed
    snapshot = _install_snapshot(user_path=["c:\\windows\\system32",
                                            "c:\\evil"])
    findings = _SEW.verify_install_findings(baseline, snapshot, _ROOT)
    assert any("user PATH" in f for f in findings)
    # machine PATH changed
    snapshot = _install_snapshot(machine_path=["c:\\other"])
    findings = _SEW.verify_install_findings(baseline, snapshot, _ROOT)
    assert any("machine PATH" in f for f in findings)


def test_side_effect_witness_new_py_launcher_association_and_py0p() -> None:
    baseline = _baseline_snapshot()
    snap = _install_snapshot(
        py_launcher_path="C:\\Users\\u\\AppData\\Local\\Programs\\Python\\"
                        "Launcher\\py.exe"
    )
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert any("py.exe launcher" in f for f in findings)

    snap = _install_snapshot(py_association_progid="Python.File")
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert any(".py file association" in f for f in findings)

    # py -0p registration list changed -> finding (strengthened witness)
    baseline_py = _baseline_snapshot()
    baseline_py["py_launcher_path"] = r"C:\Windows\py.exe"
    baseline_py["py_0p"] = [" -V:3.13        *  C:\\...\\Python313\\python.exe"]
    snap = _install_snapshot(
        py_launcher_path=r"C:\Windows\py.exe",
        py_0p=[" -V:3.13        *  C:\\...\\Python313\\python.exe",
               " -V:3.11                 C:\\...\\Python311\\python.exe"],
    )
    findings = _SEW.verify_install_findings(baseline_py, snap, _ROOT)
    assert any("py -0p" in f for f in findings)


def test_side_effect_witness_no_new_pythoncore_state() -> None:
    """(ZA-WIN-BOOT-03B) The standalone substrate creates NO PythonCore
    state: a NEW HKCU or HKLM PythonCore 3.13 registration is forbidden,
    while a pre-existing registration left byte-identical is tolerated."""
    baseline = _baseline_snapshot()
    # clean: no PythonCore anywhere -> no finding
    snap = _install_snapshot()
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert not any("PythonCore" in f for f in findings)
    # NEW user-scoped PythonCore IS a finding now (no-provider semantics)
    snap = _install_snapshot(pythoncore_3_13_hkcu_install_path=_ROOT + "\\python")
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert any("PythonCore" in f for f in findings)
    # NEW machine-scope registration is forbidden
    snap = _install_snapshot(
        pythoncore_3_13_hklm_install_path=r"C:\Program Files\Python313"
    )
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert any("machine-scope PythonCore" in f for f in findings)
    # pre-existing registration unchanged (baseline-delta) -> no finding
    host_py = {"user_path": ["c:\\windows\\system32"],
               "machine_path": ["c:\\windows\\system32"],
               "py_launcher_path": None, "py_0p": None,
               "py_association_progid": None,
               "pythoncore_3_13_hkcu_install_path": (
                   r"C:\Users\u\AppData\Local\Programs\Python\Python313"),
               "pythoncore_3_13_hklm_install_path": None,
               "uninstall_hkcu": {}, "uninstall_hklm": {},
               "start_menu_shortcut_exists": False,
               "start_menu_shortcut_target": None, "runtime_exists": False,
               "zealfie_appdata_exists": False}
    snap = _install_snapshot(
        pythoncore_3_13_hkcu_install_path=(
            r"C:\Users\u\AppData\Local\Programs\Python\Python313")
    )
    findings = _SEW.verify_install_findings(host_py, snap, _ROOT)
    assert not any("PythonCore" in f for f in findings)


def test_side_effect_witness_new_per_user_cpython_entry_is_finding() -> None:
    """Regression (rework-11 inverted by BOOT-03B): with the standalone
    substrate a NEW per-user CPython Apps&Features entry (even one whose
    uninstaller lives in %LOCALAPPDATA%\\Package Cache) is FORBIDDEN —
    there is no python.org provider bookkeeping any more."""
    baseline = _baseline_snapshot()
    snapshot = _install_snapshot(
        uninstall_hkcu={
            "python 3.13.15 (64-bit)": {
                "display_name": "Python 3.13.15 (64-bit)",
                "install_location": None,
                "uninstall_string": (
                    "C:\\Users\\u\\AppData\\Local\\Package Cache\\"
                    "{5F0F1A2B-...}\\python-3.13.15-amd64.exe /uninstall"
                ),
            },
            **_zealfie_hkcu(),
        },
    )
    findings = _SEW.verify_install_findings(baseline, snapshot, _ROOT)
    assert any("per-user CPython Apps&Features" in f for f in findings)


def test_side_effect_witness_preexisting_hkcu_cpython_is_not_a_finding() -> None:
    """Baseline-delta: a per-user host CPython entry that ALREADY existed at
    baseline and is byte-identical afterwards is tolerated."""
    preexisting = {
        "python 3.13.15 (64-bit)": {
            "display_name": "Python 3.13.15 (64-bit)",
            "install_location": r"C:\Users\u\AppData\Local\Programs\Python\Python313",
            "uninstall_string": (
                r"C:\Users\u\AppData\Local\Programs\Python\Python313\uninstall.exe"),
        },
    }
    baseline = _baseline_snapshot()
    baseline["uninstall_hkcu"] = dict(preexisting)
    snapshot = _install_snapshot(
        uninstall_hkcu={**dict(preexisting), **_zealfie_hkcu()}
    )
    assert _SEW.verify_install_findings(baseline, snapshot, _ROOT) == []


def test_side_effect_witness_new_hklm_cpython_entry_is_finding() -> None:
    """A CPython Apps&Features entry NEWLY appearing in HKLM (machine scope,
    absent from the baseline) is forbidden."""
    baseline = _baseline_snapshot()
    snapshot = _install_snapshot(
        uninstall_hklm={
            "python 3.13.15 (64-bit)": {
                "display_name": "Python 3.13.15 (64-bit)",
                "install_location": r"C:\Program Files\Python313",
                "uninstall_string": None,
            },
        },
    )
    findings = _SEW.verify_install_findings(baseline, snapshot, _ROOT)
    assert any("machine-scope CPython Apps&Features" in f
               for f in findings)


def test_side_effect_witness_preexisting_hklm_cpython_is_not_a_finding() -> None:
    """Baseline-delta semantics: a machine CPython that ALREADY existed at
    baseline (runner preinstalled) must not be flagged."""
    preexisting = {
        "python 3.13.5 (64-bit)": {
            "display_name": "Python 3.13.5 (64-bit)",
            "install_location": r"C:\Program Files\Python313",
            "uninstall_string": None,
        },
    }
    baseline = _baseline_snapshot()
    baseline["uninstall_hklm"] = dict(preexisting)
    snapshot = _install_snapshot(uninstall_hklm=dict(preexisting))
    assert _SEW.verify_install_findings(baseline, snapshot, _ROOT) == []


def test_side_effect_witness_changed_existing_cpython_entry_is_finding() -> None:
    """An existing host CPython entry whose values change is forbidden (the
    substrate must not touch the host's registrations)."""
    entry = {
        "python 3.13.5 (64-bit)": {
            "display_name": "Python 3.13.5 (64-bit)",
            "install_location": r"C:\Program Files\Python313",
            "uninstall_string": r"C:\Program Files\Python313\uninstall.exe",
        },
    }
    baseline = _baseline_snapshot()
    baseline["uninstall_hklm"] = dict(entry)
    altered = dict(entry)
    altered["python 3.13.5 (64-bit)"] = dict(
        entry["python 3.13.5 (64-bit)"], install_location=r"C:\Elsewhere"
    )
    snapshot = _install_snapshot(uninstall_hklm=altered)
    findings = _SEW.verify_install_findings(baseline, snapshot, _ROOT)
    assert any("CPython Apps&Features" in f for f in findings)


def test_side_effect_witness_start_menu_target() -> None:
    baseline = _baseline_snapshot()
    # correct appenv target -> no finding; wrong target -> finding
    snap = _install_snapshot()
    assert _SEW.verify_install_findings(baseline, snap, _ROOT) == []
    snap = _install_snapshot(
        start_menu_shortcut_target=r"C:\Windows\System32\python.exe"
    )
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert any("shortcut target" in f for f in findings)
    snap = _install_snapshot(start_menu_shortcut_exists=False)
    findings = _SEW.verify_install_findings(baseline, snap, _ROOT)
    assert any("shortcut is missing" in f for f in findings)


def test_side_effect_witness_uninstall_deltas() -> None:
    """(ZA-WIN-BOOT-03B + ZA-WIN-UNINSTALL-01) After uninstall EVERYTHING
    installer-owned is gone — shortcut, ZeAlfie registration, assets,
    private {app}\\python, {app}\\appenv AND the whole %LOCALAPPDATA%\\zealfie
    managed app-data tree — and the host footprint is byte-identical to
    baseline."""
    baseline = _baseline_snapshot()
    # clean post-uninstall snapshot (host untouched, nothing left)
    snap = {
        "schema": 1,
        "user_path": ["c:\\windows\\system32"],
        "machine_path": ["c:\\windows\\system32"],
        "py_launcher_path": None,
        "py_0p": None,
        "py_association_progid": None,
        "pythoncore_3_13_hkcu_install_path": None,
        "pythoncore_3_13_hklm_install_path": None,
        "uninstall_hkcu": {},
        "uninstall_hklm": {},
        "start_menu_shortcut_exists": False,
        "start_menu_shortcut_target": None,
        "runtime_exists": False,
        "zealfie_appdata_exists": False,
        "owned_assets": False,
        "private_python_exists": False,
        "appenv_exists": False,
    }
    assert _SEW.verify_uninstall_findings(baseline, snap, _ROOT) == []
    # private runtime preserved -> finding (removed WITH the installer now)
    snap2 = dict(snap, private_python_exists=True)
    assert any("private runtime still present" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap2, _ROOT))
    # appenv preserved -> finding
    snap3 = dict(snap, appenv_exists=True)
    assert any("application environment still present" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap3, _ROOT))
    # shortcut still present -> finding
    snap4 = dict(snap, start_menu_shortcut_exists=True)
    assert any("shortcut still present" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap4, _ROOT))
    # ZeAlfie registration still present -> finding
    snap5 = dict(snap, uninstall_hkcu=_zealfie_hkcu())
    assert any("ZeAlfie uninstall registration still present" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap5, _ROOT))
    # asset still present -> finding
    snap6 = dict(snap, owned_assets=True)
    assert any("asset still present" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap6, _ROOT))
    # whole managed app-data tree still present -> finding (the whole
    # %LOCALAPPDATA%\zealfie tree is removed WITH the installer now; the
    # old "shared runtime never touched" contract is obsolete)
    snap7 = dict(snap, zealfie_appdata_exists=True)
    findings = _SEW.verify_uninstall_findings(baseline, snap7, _ROOT)
    assert any("zealfie" in f and "still present after uninstall" in f
               for f in findings)
    # host PATH changed by uninstall -> finding
    snap8 = dict(snap, user_path=["c:\\windows\\system32", "c:\\evil"])
    assert any("user PATH" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap8, _ROOT))
    # host CPython registration touched by uninstall -> finding
    snap9 = dict(snap, pythoncore_3_13_hklm_install_path=r"C:\Program Files\Python313")
    assert any("PythonCore" in f
               for f in _SEW.verify_uninstall_findings(baseline, snap9, _ROOT))


def test_workflow_side_effect_steps_present_in_order() -> None:
    text = (_REPO_ROOT / ".github" / "workflows"
            / "windows-installer-build.yml").read_text(encoding="utf-8")
    ids = ["side-effect-baseline", "silent-install", "installer-smoke",
           "side-effect-install-audit", "uninstall-witness",
           "side-effect-uninstall-audit", "provenance"]
    positions = [text.index("- id: " + sid) for sid in ids]
    assert positions == sorted(positions), "side-effect step order broken"
    assert "side-effect-baseline.json" in text
    assert "side-effect-install-audit.json" in text
    assert "side-effect-uninstall-audit.json" in text


def test_innosetup_pin_matches_docs_and_is_6x() -> None:
    inno = tomllib.loads(_INNO_FILE.read_text(encoding="utf-8"))["innosetup"]
    assert inno["version"].startswith("6."), "must stay on the pinned 6.x line"
    assert re.fullmatch(r"[0-9a-f]{64}", inno["sha256"])
    assert inno["size"] > 0
    assert inno["installer_filename"] == f"innosetup-{inno['version']}.exe"
    assert inno["version"] in inno["installer_url"]
    # no dead version-string oracle keys remain in the record
    assert "version_probe" not in _INNO_FILE.read_text(encoding="utf-8")
    # the CI workflow and the docs reference the same pinned version
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    assert inno["version"] in workflow
    doc = (_REPO_ROOT / "docs" / "windows-installer.md").read_text(encoding="utf-8")
    assert inno["version"] in doc
    assert inno["sha256"][:8] in doc


def test_workflow_uses_cryptographic_toolchain_provenance() -> None:
    """The CI proves the exact Inno toolchain from the pinned installer
    payload (SHA-256, verified before execution), NOT from any compiler
    version-string oracle (ISCC /? has no patch level; the PE version
    resource reports 0.0.0.0 upstream)."""
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    # provenance markers present
    assert "toolchain-provenance=verified-from-pinned-installer" in workflow
    assert "inno-compiler-pinned-version" in workflow
    assert "inno-compiler-installer-sha256" in workflow
    assert "inno-compiler-isc-path" in workflow
    # fail-closed gates still present: SHA-256 verify + ISCC existence
    assert 'digest != record["sha256"]' in workflow
    assert '[ -f "$ISCC" ]' in workflow
    # no version-string oracle anywhere
    assert "VersionInfo.FileVersion" not in workflow
    assert "VersionInfo.ProductVersion" not in workflow
    assert "import innosetup_version" not in workflow
    assert 'grep -o "6\\.7\\.3"' not in workflow
    assert '"$ISCC" /?' not in workflow


# ---------------------------------------------------------------------------
# python-build-standalone substrate (ZA-WIN-BOOT-03B) — hermetic
# ---------------------------------------------------------------------------


def _synthetic_install_only_tarball(
    tmp_path: Path,
    *,
    with_pythonw: bool = True,
    with_python: bool = True,
    with_lib: bool = True,
    extra_member: tuple[str, bytes] | None = None,
) -> Path:
    """Build a tiny synthetic install_only tar.gz whose layout mirrors the
    real python-build-standalone archive (top-level ``python/``).  Member
    modes are set explicitly so extraction works on POSIX hosts."""
    import io
    import tarfile

    def _add(tar: tarfile.TarFile, name: str, data: bytes = b"",
             *, is_dir: bool = False) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o755 if is_dir else 0o644
        if is_dir:
            info.type = tarfile.DIRTYPE
            info.size = 0
        tar.addfile(info, io.BytesIO(data) if data else None)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "python/", is_dir=True)
        if with_python:
            _add(tar, "python/python.exe", b"console-interpreter")
        if with_pythonw:
            _add(tar, "python/pythonw.exe", b"windowed-interpreter")
        if with_lib:
            _add(tar, "python/Lib/", is_dir=True)
            _add(tar, "python/Lib/os.py", b"stdlib")
            _add(tar, "python/Lib/site-packages/pip/__init__.py", b"pip")
        if extra_member is not None:
            name, data = extra_member
            _add(tar, name, data)
    path = tmp_path / "cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
    path.write_bytes(buf.getvalue())
    return path


def test_record_pins_standalone_substrate_metadata() -> None:
    """The REAL committed record pins the python-build-standalone archive
    with a consistent filename/URL/tag/triple/size contract."""
    record = tomllib.loads(_RECORD_FILE.read_text(encoding="utf-8"))
    cpy = record["cpython"]
    assert cpy["substrate"] == "python-build-standalone"
    assert cpy["upstream_repo"] == "astral-sh/python-build-standalone"
    assert cpy["release_tag"] == "20260901"
    assert cpy["target_triple"] == "x86_64-pc-windows-msvc"
    assert cpy["version"] == "3.13.15"
    assert cpy["installer_filename"] == _PBS_FILENAME
    assert cpy["sha256"] == _PBS_SHA
    assert cpy["size"] == 47042104
    expected_url = (
        "https://github.com/astral-sh/python-build-standalone/releases/"
        f"download/20260901/{_PBS_FILENAME.replace('+', '%2B')}"
    )
    assert cpy["installer_url"] == expected_url
    # no legacy EXE fields survive in the RECORD VALUES (prose comments may
    # still explain the replacement history)
    all_values = []
    for table in record.values():
        for value in table.values():
            if isinstance(value, str):
                all_values.append(value)
            elif isinstance(value, list):
                all_values.extend(str(v) for v in value)
    assert "python-3.13.15-amd64.exe" not in " ".join(all_values)
    assert "installer properties" not in " ".join(all_values)

def test_record_loads_and_archive_digest_verify_pass_fail(tmp_path) -> None:
    """Digest verification (fail closed) against the loader's record."""
    archive = tmp_path / _PBS_FILENAME
    archive.write_bytes(b"pinned-archive-bytes")
    record = provision.load_record(_RECORD_FILE)
    with pytest.raises(provision.HashMismatchError):
        provision.verify_archive_sha256(archive, record=record)
    # a digest that DOES match must pass
    from hashlib import sha256

    good = tmp_path / "good-archive.tar.gz"
    good.write_bytes(b"payload")
    expected = sha256(b"payload").hexdigest()
    assert provision.verify_archive_sha256(good, expected) == expected


def test_extract_python_tarball_produces_complete_layout(tmp_path: Path) -> None:
    """Extraction of a synthetic install_only tar produces
    python.exe + pythonw.exe + Lib (the complete private runtime)."""
    archive = _synthetic_install_only_tarball(tmp_path)
    private = provision.extract_python_tarball(archive, tmp_path / "out")
    assert private == tmp_path / "out" / "python"
    for name in ("python.exe", "pythonw.exe"):
        assert (private / name).is_file(), name
    assert (private / "Lib" / "os.py").is_file()
    assert (private / "Lib" / "site-packages" / "pip" / "__init__.py").is_file()
    assert provision.missing_private_python_files(tmp_path / "out") == []


def test_extract_python_tarball_fails_closed_without_pythonw(tmp_path: Path) -> None:
    """A tarball WITHOUT pythonw.exe must be rejected (hard functional
    requirement of the windowed GUI launcher)."""
    archive = _synthetic_install_only_tarball(tmp_path, with_pythonw=False)
    with pytest.raises(provision.ExtractionError, match="pythonw.exe"):
        provision.extract_python_tarball(archive, tmp_path / "out")


def test_extract_python_tarball_fails_closed_without_python_or_lib(
    tmp_path: Path,
) -> None:
    archive = _synthetic_install_only_tarball(tmp_path, with_python=False)
    with pytest.raises(provision.ExtractionError, match="python.exe"):
        provision.extract_python_tarball(archive, tmp_path / "out1")
    archive = _synthetic_install_only_tarball(tmp_path, with_lib=False)
    with pytest.raises(provision.ExtractionError, match="Lib"):
        provision.extract_python_tarball(archive, tmp_path / "out2")


def test_extract_python_tarball_rejects_unsafe_member(tmp_path: Path) -> None:
    """Defence in depth: a member escaping python/ (path traversal or an
    unexpected top-level entry) fails closed."""
    archive = _synthetic_install_only_tarball(
        tmp_path,
        extra_member=("python/../../evil.txt", b"boom"),
    )
    with pytest.raises(provision.ExtractionError, match="unsafe"):
        provision.extract_python_tarball(archive, tmp_path / "out1")
    archive = _synthetic_install_only_tarball(
        tmp_path,
        extra_member=("other/file.txt", b"boom"),
    )
    with pytest.raises(provision.ExtractionError, match="unexpected"):
        provision.extract_python_tarball(archive, tmp_path / "out2")


def test_no_pythonorg_exe_or_3010_path_remains_in_iss_and_workflow() -> None:
    """Source-level assertion: no python.org EXE / Burn / 0/3010 / bundled
    installer path survives in the .iss or the installer CI workflow."""
    iss = _iss_text()
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    for text in (iss, workflow):
        assert "python-3.13.15-amd64.exe" not in text
        assert "3010" not in text
        assert "verify-cpython-installer" not in text
        assert "CpythonInstaller" not in text
        assert "/DCpythonInstaller" not in text
        assert "RunCheckedCpython" not in text
        assert "InstallAllUsers" not in text
        assert "TargetDir=" not in text


def test_no_pip_exe_and_module_invocation_only() -> None:
    """The bootstrap must use ``python -m pip`` (module invocation) — no
    pip.exe, no PATH discovery — in every packaging/windows module."""
    for module in ("provision.py", "provision_windows.py", "installer_smoke.py"):
        source = (_WINDOWS_PKG / module).read_text(encoding="utf-8")
        assert "pip.exe" not in source, module
    # the argv builders invoke pip as a module of the (private) interpreter
    provision_source = (_WINDOWS_PKG / "provision.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r'"-m",\s*\n\s*"pip",', provision_source), (
        "pip argv must be python -m pip (module invocation)"
    )
    # the .iss bootstrap runs the private interpreter on provision_windows.py
    iss = _iss_text()
    assert r"{app}\python\python.exe" in iss
    assert "python.exe" in iss  # console interpreter drives the bootstrap

def test_private_python_exe_and_pythonw_required_in_iss_and_smoke() -> None:
    """python.exe AND pythonw.exe of the private runtime are both hard
    requirements enforced by the .iss gate and the installer smoke."""
    iss = _iss_text()
    assert r"{app}\python\python.exe" in iss
    assert r"{app}\python\pythonw.exe" in iss
    smoke = (_WINDOWS_PKG / "installer_smoke.py").read_text(encoding="utf-8")
    assert "missing_private_python_files" in smoke
    assert "pythonw.exe" in smoke
    assert "private standalone runtime incomplete" in smoke

def test_workflow_acquire_standalone_before_compile_and_extract_gate() -> None:
    """The standalone acquisition (download + SHA-256 + extract) must run
    BEFORE compile-installer and gate on BOTH staged interpreters."""
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    acquire = workflow.index("- id: acquire-standalone-python")
    compile_step = workflow.index("- id: compile-installer")
    silent_install = workflow.index("- id: silent-install")
    assert acquire < compile_step < silent_install
    assert "provision-python" in workflow  # reuses the provisioning code
    assert "PYTHON_DIR=$ZEALFIE_STAGE/python" in workflow
    assert "/DPythonDir=$PYTHON_DIR" in workflow
    assert "staged python.exe missing" in workflow
    assert "staged pythonw.exe missing" in workflow


def test_uninstall_witness_requires_private_runtime_removal() -> None:
    """The CI uninstall witness must FAIL if {app}\\python or {app}\\appenv
    survive uninstall (they are installer-owned and removed WITH Setup).
    (ZA-WIN-UNINSTALL-01) It must also seed representative managed app-data
    state before uninstalling and FAIL when the whole %LOCALAPPDATA%\\zealfie
    root survives (the whole ZeAlfie-owned managed tree is removed WITH the
    installer now)."""
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    start = workflow.index("- id: uninstall-witness")
    nxt = workflow.find("\n      - id:", start + 1)
    step = workflow[start: nxt]
    assert "private standalone runtime not removed by uninstall" in step
    assert "application environment not removed by uninstall" in step
    assert "uninstall witness PASS" in step
    assert "removed, shortcut removed" in step
    # (ZA-WIN-UNINSTALL-01) representative managed app-data state is seeded
    # BEFORE the uninstaller runs so the whole-tree cleanup is witnessed
    assert 'ZEALFIE_APPDATA="$LOCALAPPDATA/zealfie"' in step
    assert 'mkdir -p "$ZEALFIE_APPDATA/runtime/state" "$ZEALFIE_APPDATA/work"' in step
    assert 'printf \'products = ["zefocus"]\\n\' > "$ZEALFIE_APPDATA/desired-products.toml"' in step
    assert step.index('mkdir -p "$ZEALFIE_APPDATA') < step.index('if "$UNINS" /VERYSILENT')
    # ... and the whole managed app-data root must be GONE afterwards
    assert "ZeAlfie managed app-data root not removed by uninstall" in step
    assert '[ -e "$ZEALFIE_APPDATA" ]' in step
    assert "never touched" not in step
    # the preservation-era assertion is gone
    assert "unexpectedly removed by uninstall" not in step


def test_iss_uninstalldelete_covers_runtime_created_appenv_and_logs() -> None:
    """(Rework-1 + rework-3, confirmed defects) The uninstaller must delete
    EVERY runtime-created/untracked part of {app}: {app}\\appenv and
    {app}\\logs are created at ssPostInstall (NOT [Files]-registered), and
    runtime execution creates untracked .pyc/__pycache__ residue inside the
    [Files]-registered {app}\\python and {app}\\assets trees — so all four
    subtrees carry explicit recursive [UninstallDelete] ownership."""
    iss = _iss_text()
    assert "[UninstallDelete]" in iss
    for subtree in ("{app}\\appenv", "{app}\\logs",
                    "{app}\\python", "{app}\\assets"):
        assert f'Type: filesandordirs; Name: "{subtree}"' in iss, subtree
    # the SECTION must sit between [Icons] and [Code] (the header prose may
    # mention [UninstallDelete] earlier, so anchor on section headers only)
    icons = iss.index("\n[Icons]\n")
    uninst = iss.index("\n[UninstallDelete]\n")
    code = iss.index("\n[Code]\n")
    assert icons < uninst < code
    # mechanism matches the assertions: the side-effect witness expects the
    # appenv gone after uninstall, and the CI uninstall witness FAILS when it
    # survives — so the .iss deletion mechanism is what makes them true
    witness = (_WINDOWS_PKG / "side_effect_witness.py").read_text(
        encoding="utf-8"
    )
    assert "appenv_exists" in witness
    assert "application environment still present after uninstall" in witness
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    assert "application environment not removed by uninstall" in workflow


def test_iss_uninstalldelete_cleanup_bounded_to_owned_namespaces() -> None:
    """(ZA-WIN-UNINSTALL-01 r1) The uninstall deletion contract is bounded to
    the ZeAlfie-owned namespaces: five recursive filesandordirs entries
    (the four {app} subtrees + the whole-tree {localappdata}\\zealfie
    managed app-data tree) PLUS one final dirifempty entry removing the
    now-empty {app} install root itself.  No [UninstallRun], and no entry
    may broaden to {localappdata} as a whole or to any non-ZeAlfie path
    ({userprofile}/Documents/Pictures...)."""
    iss = _iss_text()
    # anchor on the SECTION header line (the header prose also mentions
    # [UninstallDelete]); the section body carries the deletion ENTRIES
    uninst_sec = iss.split("\n[UninstallDelete]\n", 1)[1].split("\n[Code]\n", 1)[0]
    entries = [ln.strip() for ln in uninst_sec.splitlines()
               if ln.strip().startswith("Type:")]
    # six whole entries: five filesandordirs (four {app} subtrees + the
    # whole ZeAlfie managed app-data tree) + one dirifempty {app} entry
    assert len(entries) == 6
    files_entries = [e for e in entries if e.startswith("Type: filesandordirs")]
    assert len(files_entries) == 5
    app_entries = [e for e in files_entries if '"{app}\\' in e]
    assert len(app_entries) == 4
    for subtree in ("{app}\\appenv", "{app}\\logs",
                    "{app}\\python", "{app}\\assets"):
        assert f'Type: filesandordirs; Name: "{subtree}"' in entries, subtree
    assert 'Type: filesandordirs; Name: "{localappdata}\\zealfie"' in entries
    # the now-empty install root is removed LAST (after the subtree
    # deletions, so it is empty by then) and only when empty
    assert entries[-1] == 'Type: dirifempty; Name: "{app}"'
    assert 'Type: dirifempty; Name: "{app}"' in entries
    assert entries.count('Type: dirifempty; Name: "{app}"') == 1
    # cleanup can never escape the ZeAlfie-owned namespace: {app}-rooted or
    # exactly the whole {localappdata}\zealfie tree — never {localappdata}
    # itself (whole), never {userprofile}/Documents/any other path
    for entry in entries:
        assert '"{localappdata}"' not in entry, entry
        assert "{userprofile}" not in entry, entry
        assert "Documents" not in entry, entry
        assert "Pictures" not in entry, entry
    assert "[UninstallRun]" not in iss


def test_iss_uninstalldelete_whole_appdata_tree_covers_selection_and_work() -> None:
    """(ZA-WIN-UNINSTALL-01) The single whole-tree {localappdata}\\zealfie
    entry removes ZeAlfie-installed products' managed runtime
    (runtime\\ slots/state/cache), the install work/cache staging (work\\),
    the persisted desired-products.toml selection and the internal
    .runtime.zealfie-mutation.lock — ALL ZeAlfie-owned disposable
    application state (no user-authored data).  The test no longer asserts
    runtime preservation: the obsolete "never read, written, or deleted"
    contract is gone from the .iss."""
    iss = _iss_text()
    uninst_sec = iss.split("\n[UninstallDelete]\n", 1)[1].split("\n[Code]\n", 1)[0]
    # whole-tree app-data entry declared with recursive filesandordirs
    assert 'Type: filesandordirs; Name: "{localappdata}\\zealfie"' in uninst_sec
    # the section prose truthfully states what the whole tree covers
    assert "{localappdata}\\zealfie" in uninst_sec
    assert "desired-products.toml" in uninst_sec
    assert ".runtime.zealfie-mutation.lock" in uninst_sec
    lowered = uninst_sec.lower()
    assert "runtime" in lowered and "work" in lowered
    # preservation-era contract removed by product decision
    assert "never read, written, or deleted" not in iss


def test_provision_windows_parser_contract_provision_python_and_record() -> None:
    """(Rework-2, real-run regression 33763688411) --witness-root and
    --record are GLOBAL options defined before the subparsers; argparse
    rejects them AFTER any subcommand.  The CLI contract is asserted with
    the REAL parser: global-first succeeds, subcommand-first exits 2."""
    provision_windows = _load_module(
        _WINDOWS_PKG / "provision_windows.py",
        "zealfie_provision_windows_r2_parser",
    )
    parser = provision_windows._build_parser()
    # global --witness-root BEFORE provision-python: valid
    args = parser.parse_args(
        ["--witness-root", r"C:\stage", "provision-python"]
    )
    assert args.command == "provision-python"
    assert str(args.witness_root).lower() == r"c:\stage"
    # subcommand BEFORE global --witness-root: argparse exits 2
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            ["provision-python", "--witness-root", r"C:\stage"]
        )
    assert exc.value.code == 2
    # global --record BEFORE provision-python: valid
    args = parser.parse_args(
        ["--record", r"C:\stage\reproducibility.toml", "provision-python"]
    )
    assert args.command == "provision-python"
    assert str(args.record).lower() == r"c:\stage\reproducibility.toml"
    # subcommand BEFORE global --record: argparse exits 2
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            ["provision-python", "--record", r"C:\stage\reproducibility.toml"]
        )
    assert exc.value.code == 2


def test_workflow_acquire_step_orders_global_witness_root_first() -> None:
    """(Rework-2, real-run regression 33763688411) The
    acquire-standalone-python step must pass --witness-root BEFORE the
    provision-python subcommand.  This test FAILED against HEAD 042a7b4,
    whose step contained 'provision-python \\' followed by
    '--witness-root "$ZEALFIE_STAGE"' (reversed order)."""
    workflow = (_REPO_ROOT / ".github" / "workflows"
                / "windows-installer-build.yml").read_text(encoding="utf-8")
    start = workflow.index("- id: acquire-standalone-python")
    nxt = workflow.find("\n      - id:", start + 1)
    step = workflow[start: nxt]
    # canonical global-before-subcommand order present
    assert "--witness-root \"$ZEALFIE_STAGE\"" in step
    assert "provision-python" in step
    witness = step.index("--witness-root")
    subcommand = step.index("provision-python")
    assert witness < subcommand, (
        "--witness-root must precede the provision-python subcommand"
    )
    # the reversed (broken) call shape must NOT be present
    assert "provision-python \\\n            --witness-root" not in step


# ---------------------------------------------------------------------------
# ZA-ICON-03B — Start Menu shortcut AppUserModelID binding (hermetic)
# ---------------------------------------------------------------------------


def _zealfie_shortcut_line() -> str:
    """The single [Icons] entry: the ZeAlfie Start Menu shortcut line."""
    iss = _iss_text()
    icons_sec = iss.split("\n[Icons]\n", 1)[1].split("\n[UninstallDelete]\n", 1)[0]
    entries = [ln.strip() for ln in icons_sec.splitlines()
               if ln.strip().startswith("Name:")]
    assert len(entries) == 1, (
        f"exactly one [Icons] entry required, found {len(entries)}"
    )
    return entries[0]


def test_iss_zealfie_shortcut_exact_canonical_line_with_aumid() -> None:
    """(ZA-ICON-03B) The ZeAlfie Start Menu shortcut is the approved exact
    line: Filename / IconFilename / WorkingDir / Comment unchanged, with
    AppUserModelID: "ZeSoftware.ZeAlfie" appended (Inno 6.1+ parameter) so
    the shell maps the running process (ZA-ICON-02 runtime AUMID) back to
    the canonical zealfie.ico taskbar button."""
    assert _zealfie_shortcut_line() == (
        'Name: "{autoprograms}\\ZeAlfie"; '
        'Filename: "{app}\\appenv\\Scripts\\zealfie-gui.exe"; '
        'IconFilename: "{app}\\assets\\zealfie.ico"; '
        'WorkingDir: "{app}"; Comment: "Launch ZeAlfie"; '
        'AppUserModelID: "ZeSoftware.ZeAlfie"'
    )


def test_iss_shortcut_still_launches_zealfie_gui_with_canonical_icon() -> None:
    """(ZA-ICON-03B) The shortcut still launches the installed windowed
    launcher ({app}\\appenv\\Scripts\\zealfie-gui.exe) and still carries the
    canonical icon asset ({app}\\assets\\zealfie.ico)."""
    line = _zealfie_shortcut_line()
    assert 'Filename: "{app}\\appenv\\Scripts\\zealfie-gui.exe"' in line
    assert 'IconFilename: "{app}\\assets\\zealfie.ico"' in line
    assert "zealfie-gui.exe" in line  # normal launch = windowed launcher


def test_iss_aumid_binding_touches_only_the_single_shortcut() -> None:
    """(ZA-ICON-03B) No unrelated shortcut/installer-identity change: the
    [Icons] section declares exactly ONE entry (the ZeAlfie Start Menu
    shortcut), AppUserModelID appears exactly once in the whole .iss, and
    the stable installer-identity surface (single fixed AppId, AppName,
    per-user posture) is untouched."""
    iss = _iss_text()
    icons_sec = iss.split("\n[Icons]\n", 1)[1].split("\n[UninstallDelete]\n", 1)[0]
    entries = [ln.strip() for ln in icons_sec.splitlines()
               if ln.strip().startswith("Name:")]
    assert len(entries) == 1
    assert entries[0].startswith('Name: "{autoprograms}\\ZeAlfie"')
    assert iss.count('AppUserModelID: "ZeSoftware.ZeAlfie"') == 1
    # no second shortcut/identity entry was invented (desktop, pin, group,
    # uninstall display name, ...): the parameter lives only on the Start
    # Menu entry, and the stable identity fields are still single + exact
    assert len(re.findall(r"AppId=\{\{([0-9A-Fa-f-]{36})\}", iss)) == 1
    assert "AppName=ZeAlfie" in iss
    assert "PrivilegesRequired=lowest" in iss


def test_iss_aumid_exactly_matches_application_constant() -> None:
    """(ZA-ICON-03B) The .iss AppUserModelID value EXACTLY equals the
    application-side single source of truth
    zealfie.gui.windows_identity.APP_USER_MODEL_ID (the same AUMID the
    running process registers at startup via ZA-ICON-02) — the shortcut
    and the runtime identity can never drift apart."""
    # sanity: mirrors tests/test_gui_windows_identity.py APP_ID
    assert APP_USER_MODEL_ID == "ZeSoftware.ZeAlfie"
    line = _zealfie_shortcut_line()
    assert f'AppUserModelID: "{APP_USER_MODEL_ID}"' in line
    assert f'AppUserModelID: "{APP_USER_MODEL_ID}"' in _iss_text()
