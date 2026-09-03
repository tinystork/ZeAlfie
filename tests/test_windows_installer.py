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
* the installer completeness primitive (four appenv launchers);
* .iss <-> reproducibility pins coupling: the CPython version + SHA-256 and
  the install properties embedded in ``packaging/windows/installer/zealfie.iss``
  MUST equal ``reproducibility.toml``, and the Inno toolchain pin
  (``installer/innosetup.toml``) must be consistent with the docs/CI.

All tests are FAST and hermetic: no real private Python, no Windows, no
venv creation, no network, no pip.  Windows paths are simulated with
Windows-style strings and :mod:`ntpath` semantics inside the modules under
test.  No ``integration`` / ``zealfie_slow`` markers.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

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
_CPYTHON_SHA = "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403"


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
    assert lock.zealfie_version == "0.1.0"
    assert lock.platform_tag == "win_amd64"
    assert lock.python_tag == "cp313"
    assert lock.cpython_version == "3.13.15"
    assert len(lock.cpython_installer_sha256) == 64
    assert lock.zealfie_wheel.filename == "zealfie-0.1.0-py3-none-any.whl"
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


def test_real_lock_cpython_pin_matches_reproducibility_record() -> None:
    record = tomllib.loads(_RECORD_FILE.read_text(encoding="utf-8"))
    lock = wheelhouse.load_lock(_LOCK_FILE)
    assert lock.cpython_version == record["cpython"]["version"]
    assert lock.cpython_installer_sha256 == record["cpython"]["sha256"]
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


def test_iss_embeds_the_record_cpython_version_and_sha256() -> None:
    record = tomllib.loads(_RECORD_FILE.read_text(encoding="utf-8"))
    iss = _iss_text()
    version = record["cpython"]["version"]
    sha = record["cpython"]["sha256"]
    # the ISPP defines embed the record values verbatim ...
    assert f'ZeAlfieCpythonVersion "{version}"' in iss
    assert f'ZeAlfieCpythonSha256 "{sha}"' in iss
    # ... and the [Code] const used by the install-time verification is
    # defined FROM that define (ISPP-expanded at compile time).
    assert "CpythonSha256 = '{#ZeAlfieCpythonSha256}';" in iss
    assert "CpythonExeName = '{#CpythonInstallerName}';" in iss


def test_iss_embeds_the_full_pinned_install_properties() -> None:
    record = tomllib.loads(_RECORD_FILE.read_text(encoding="utf-8"))
    properties = record["install"]["properties"]
    iss = _iss_text()
    assert "InstallAllUsers=0" in iss
    assert "InstallAllUsers=1" not in iss
    assert "PrependPath=0" in iss and "PrependPath=1" not in iss
    for prop in properties:
        assert prop in iss, f"install property {prop} missing from zealfie.iss"
    # per-user / non-admin / x64-only posture
    assert "PrivilegesRequired=lowest" in iss
    assert "ArchitecturesAllowed=x64os" in iss
    assert "ArchitecturesInstallIn64BitMode=x64os" in iss
    # default per-user dir
    assert "DefaultDirName={localappdata}\\Programs\\ZeAlfie" in iss


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
    for gate in ("VerifyBundledCpythonIntegrity", "RequirePrivatePythonInstalled",
                 "RequireAppenvComplete", "GetSHA256OfFile", "RaiseException"):
        assert gate in iss, f"{gate} missing from zealfie.iss [Code]"
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
    # the corrected // comment is present verbatim
    assert "// After the silent per-user CPython install the ACTUAL interpreter must" in iss
    assert "// exist at {app}\\python\\python.exe" in iss
    assert not any(
        line.lstrip().startswith("{ After the silent per-user") for line in code_lines
    )


def test_iss_has_no_nested_ispp_macro_and_resolves_installer_name() -> None:
    """Regression (rework-3): ISPP does NOT re-expand {#...} inside another
    #define value, so the CPython installer filename must use the string
    concatenation idiom and no #define line may embed a literal {#."""
    iss = _iss_text()
    # 1) guard against the nested-macro form returning: scan every #define
    for line in iss.splitlines():
        if line.lstrip().startswith("#define"):
            assert "{#" not in line, f"nested ISPP macro in: {line}"
    # 2) the corrected concatenation form is present verbatim
    assert ('#define CpythonInstallerName "python-" + ZeAlfieCpythonVersion'
            ' + "-amd64.exe"') in iss
    # 3) the + pieces resolve exactly to the pinned installer filename
    record = tomllib.loads(_RECORD_FILE.read_text(encoding="utf-8"))
    version = record["cpython"]["version"]
    assert f'ZeAlfieCpythonVersion "{version}"' in iss
    assert "".join(("python-", version, "-amd64.exe")) == (
        f"python-{version}-amd64.exe"
    )
    # the define feeds both the [Run] filename and the [Code] const
    assert "CpythonExeName = '{#CpythonInstallerName}';" in iss


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
