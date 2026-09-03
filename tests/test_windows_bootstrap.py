"""Hermetic tests for the ZeAlfie Windows standalone bootstrap (ZA-WIN-BOOT-01).

Covers, from ``packaging/windows/provision.py`` (the pure provisioning
module):

* reproducibility-record load/validation (fail closed on bad records,
  incl. the python-build-standalone filename/URL/tag consistency contract);
* SHA-256 verification pass/fail (fail closed on mismatch);
* standalone tarball layout helpers (python.exe + pythonw.exe + Lib) and
  private-runtime presence requirements;
* appenv / child-venv provenance assertions pass/fail;
* runner/system preinstalled-Python detection and rejection (path
  provenance only, case-insensitive, ntpath semantics on every host).

All tests are FAST and hermetic: no real private Python, no venv creation,
no network, no pip.  Windows paths are simulated with Windows-style strings
and :mod:`ntpath` semantics inside the module under test, which behaves
identically on Linux.  No ``integration`` / ``zealfie_slow`` markers.

The module under test lives under ``packaging/`` — a deliberately flat
directory (NOT an import package, so the top-level ``packaging`` folder can
never shadow the PyPI ``packaging`` distribution that ZeAlfie depends on).
It is therefore loaded here by file path under a unique module name.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROVISION_FILE = _REPO_ROOT / "packaging" / "windows" / "provision.py"
_RECORD_FILE = _REPO_ROOT / "packaging" / "windows" / "reproducibility.toml"


def _load_provision():
    spec = importlib.util.spec_from_file_location(
        "zealfie_windows_boot_provision", _PROVISION_FILE
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Dataclass processing looks the module up in sys.modules by name, so
    # the alias must be registered before exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provision = _load_provision()

# Windows-style witness layout used across the pure path-provenance tests.
_WITNESS = r"C:\zealfie-witness"
_PRIVATE_DIR = r"C:\zealfie-witness\python"
_APPENV_DIR = r"C:\zealfie-witness\appenv"
_APPENV_PY = r"C:\zealfie-witness\appenv\Scripts\python.exe"


# ---------------------------------------------------------------------------
# Reproducibility record
# ---------------------------------------------------------------------------


def test_record_file_exists_and_default_path_points_to_it() -> None:
    assert _RECORD_FILE.is_file()
    assert provision.default_record_path() == _RECORD_FILE


def test_record_loads_and_pins_the_documented_values() -> None:
    record = provision.load_record(_RECORD_FILE)
    assert record.zealfie_version == "0.1.0"
    assert record.zealfie_revision == "a1a777a2df065b86c9f7e5305550ac436f60b42d"
    assert record.cpython_version == "3.13.15"
    assert record.substrate == "python-build-standalone"
    assert record.upstream_repo == "astral-sh/python-build-standalone"
    assert record.release_tag == "20260901"
    assert record.target_triple == "x86_64-pc-windows-msvc"
    assert record.installer_filename == (
        "cpython-3.13.15+20260901-x86_64-pc-windows-msvc-"
        "install_only.tar.gz"
    )
    assert record.installer_url == (
        "https://github.com/astral-sh/python-build-standalone/releases/"
        "download/20260901/cpython-3.13.15%2B20260901-x86_64-pc-windows-"
        "msvc-install_only.tar.gz"
    )
    assert record.sha256 == (
        "9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7"
    )
    assert len(record.sha256) == 64
    assert record.sha256 == record.sha256.lower()
    assert record.size == 47042104
    assert record.per_user is True
    assert record.silent is True


def _write_record(tmp_path: Path, **overrides) -> Path:
    base = """
[zealfie]
version = "0.1.0"
revision = "a1a777a2df065b86c9f7e5305550ac436f60b42d"

[cpython]
version = "3.13.15"
substrate = "python-build-standalone"
upstream_repo = "astral-sh/python-build-standalone"
release_tag = "20260901"
target_triple = "x86_64-pc-windows-msvc"
installer_filename = "cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
installer_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
sha256 = "9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7"
size = 47042104

[install]
per_user = true
silent = true
"""
    import tomllib

    data = tomllib.loads(base)
    for dotted, value in overrides.items():
        section, _, key = dotted.partition(".")
        if section not in data:
            data[section] = {}
        data[section][key] = value
    import json

    def _dump(value, indent=0):
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, list):
            return "[" + ", ".join(_dump(v) for v in value) + "]"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    lines = []
    for section, fields in data.items():
        lines.append(f"[{section}]")
        for key, value in fields.items():
            lines.append(f'{key} = {_dump(value)}')
        lines.append("")
    path = tmp_path / "reproducibility.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_record_rejects_bad_sha256(tmp_path) -> None:
    path = _write_record(tmp_path, **{"cpython.sha256": "not-a-hash"})
    with pytest.raises(provision.RecordError, match="sha256"):
        provision.load_record(path)


def test_record_rejects_short_sha256(tmp_path) -> None:
    path = _write_record(tmp_path, **{"cpython.sha256": "a" * 63})
    with pytest.raises(provision.RecordError, match="sha256"):
        provision.load_record(path)


def test_record_rejects_inconsistent_installer_url(tmp_path) -> None:
    path = _write_record(
        tmp_path,
        **{"cpython.installer_url": "https://evil.example.com/not-the-pinned-archive"},
    )
    with pytest.raises(provision.RecordError, match="inconsistent"):
        provision.load_record(path)


def test_record_rejects_installer_filename_inconsistent_with_fields(tmp_path) -> None:
    # filename must derive exactly from version + release_tag + target_triple
    path = _write_record(
        tmp_path,
        **{"cpython.installer_filename": "cpython-3.13.15+20260101-x86_64-pc-windows-msvc-install_only.tar.gz"},
    )
    with pytest.raises(provision.RecordError, match="installer_filename"):
        provision.load_record(path)


def test_record_rejects_non_pbs_filename_shape(tmp_path) -> None:
    # a python.org EXE filename must NOT be accepted by the standalone record
    path = _write_record(
        tmp_path,
        **{"cpython.installer_filename": "python-3.13.15-amd64.exe",
           "cpython.installer_url": "https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe"},
    )
    with pytest.raises(provision.RecordError, match="install_only"):
        provision.load_record(path)


def test_record_rejects_unknown_substrate(tmp_path) -> None:
    path = _write_record(tmp_path, **{"cpython.substrate": "python.org-installer"})
    with pytest.raises(provision.RecordError, match="substrate"):
        provision.load_record(path)


def test_record_rejects_missing_size(tmp_path) -> None:
    import json
    import tomllib

    def _dump(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, int):
            return str(value)
        raise AssertionError(f"unhandled type: {type(value)!r}")

    data = tomllib.loads(
        _write_record(tmp_path).read_text(encoding="utf-8")
    )
    del data["cpython"]["size"]
    lines = []
    for section, fields in data.items():
        lines.append(f"[{section}]")
        for key, value in fields.items():
            lines.append(f"{key} = {_dump(value)}")
        lines.append("")
    path = tmp_path / "reproducibility.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(provision.RecordError, match="size"):
        provision.load_record(path)


def test_record_rejects_bad_release_tag(tmp_path) -> None:
    path = _write_record(tmp_path, **{"cpython.release_tag": "2026-09-01"})
    with pytest.raises(provision.RecordError, match="release_tag"):
        provision.load_record(path)


def test_record_rejects_non_full_revision(tmp_path) -> None:
    path = _write_record(tmp_path, **{"zealfie.revision": "a1a777a"})
    with pytest.raises(provision.RecordError, match="revision"):
        provision.load_record(path)


def test_record_rejects_non_per_user(tmp_path) -> None:
    # the bootstrap contract is per-user only (no installer properties exist
    # anymore — the substrate is extracted plain files, so the posture is a
    # hard boolean on [install] rather than an EXE property list)
    path = _write_record(tmp_path, **{"install.per_user": False})
    with pytest.raises(provision.RecordError, match="per-user"):
        provision.load_record(path)


def test_record_rejects_non_boolean_install_flags(tmp_path) -> None:
    path = _write_record(tmp_path, **{"install.silent": "yes"})
    with pytest.raises(provision.RecordError, match="boolean"):
        provision.load_record(path)


def test_record_no_exe_property_surface_remains() -> None:
    # the python.org EXE semantics (properties/switches/architecture) are gone
    text = _RECORD_FILE.read_text(encoding="utf-8")
    assert "InstallAllUsers" not in text
    assert "PrependPath" not in text
    assert "Include_launcher" not in text
    assert "properties" not in text
    assert "switches" not in text
    assert "architecture =" not in text


def test_record_load_fails_closed_on_missing_file(tmp_path) -> None:
    with pytest.raises(provision.RecordError, match="not found"):
        provision.load_record(tmp_path / "nope.toml")


# ---------------------------------------------------------------------------
# SHA-256 verification
# ---------------------------------------------------------------------------


def test_sha256_file_matches_stdlib(tmp_path) -> None:
    payload = b"zealfie-windows-boot-probe\x00\xff" * 4096
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)
    assert provision.sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_verify_archive_sha256_passes_on_match(tmp_path) -> None:
    archive = tmp_path / "cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
    archive.write_bytes(b"fake-archive-bytes")
    expected = hashlib.sha256(b"fake-archive-bytes").hexdigest()
    assert provision.verify_archive_sha256(archive, expected) == expected


def test_verify_archive_sha256_fails_closed_on_mismatch(tmp_path) -> None:
    archive = tmp_path / "cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
    archive.write_bytes(b"fake-archive-bytes")
    expected = "e" * 64
    with pytest.raises(provision.HashMismatchError, match="SHA-256 mismatch"):
        provision.verify_archive_sha256(archive, expected)


def test_verify_archive_sha256_uses_record_when_supplied(tmp_path) -> None:
    archive = tmp_path / "cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
    archive.write_bytes(b"fake-archive-bytes")
    record = provision.load_record(_RECORD_FILE)
    with pytest.raises(provision.HashMismatchError):
        # The real pinned digest cannot match this tiny fake file.
        provision.verify_archive_sha256(archive, record=record)


# ---------------------------------------------------------------------------
# Private standalone runtime layout (python.exe + pythonw.exe + Lib)
# ---------------------------------------------------------------------------


def test_layout_helpers_include_pythonw_and_lib() -> None:
    def _win(path: Path) -> str:
        return str(path).replace("/", "\\")

    assert _win(provision.private_python_dir(_WITNESS)) == _PRIVATE_DIR
    assert _win(provision.private_python_exe(_WITNESS)) == (
        _PRIVATE_DIR + r"\python.exe"
    )
    assert _win(provision.private_pythonw_exe(_WITNESS)) == (
        _PRIVATE_DIR + r"\pythonw.exe"
    )
    assert _win(provision.private_python_lib(_WITNESS)) == (
        _PRIVATE_DIR + r"\Lib"
    )


def test_missing_private_python_files_empty_when_complete() -> None:
    import ntpath

    root = r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
    existing = {
        ntpath.normcase(root + r"\python\python.exe"),
        ntpath.normcase(root + r"\python\pythonw.exe"),
        ntpath.normcase(root + r"\python\Lib"),
    }
    missing = provision.missing_private_python_files(
        root,
        _exists=lambda p: ntpath.normcase(str(p)) in existing,
    )
    assert missing == []


def test_missing_private_python_files_reports_gaps() -> None:
    root = r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
    # only python.exe present -> pythonw.exe AND Lib must be reported
    import ntpath

    existing = {ntpath.normcase(root + r"\python\python.exe")}
    missing = provision.missing_private_python_files(
        root,
        _exists=lambda p: ntpath.normcase(str(p)) in existing,
    )
    assert missing == [r"python\pythonw.exe", r"python\Lib"]

    missing_none = provision.missing_private_python_files(
        root, _exists=lambda p: False
    )
    assert missing_none == [r"python\python.exe", r"python\pythonw.exe",
                            r"python\Lib"]


# ---------------------------------------------------------------------------
# venv / pip argv builders (mirror the runtime child-venv mechanism)
# ---------------------------------------------------------------------------


def test_venv_create_argv_mirrors_runtime_mechanism() -> None:
    argv = provision.venv_create_argv(_APPENV_PY, r"C:\w\child", with_pip=True)
    assert argv == [r"C:\zealfie-witness\appenv\Scripts\python.exe",
                    "-m", "venv", r"C:\w\child"]
    assert provision.venv_create_argv(_APPENV_PY, r"C:\w\child", with_pip=False) == [
        _APPENV_PY, "-m", "venv", "--without-pip", r"C:\w\child"
    ]


def test_pip_install_wheel_argv_shape() -> None:
    argv = provision.pip_install_wheel_argv(_APPENV_PY, r"C:\w\zealfie.whl")
    assert argv[:4] == [_APPENV_PY, "-m", "pip", "install"]
    assert argv[-1] == r"C:\w\zealfie.whl"


def test_layout_helpers() -> None:
    # Native Path objects on POSIX render with "/" separators; on the real
    # Windows witness they render natively.  The assembly is what matters.
    def _win(path: Path) -> str:
        return str(path).replace("/", "\\")

    assert _win(provision.private_python_dir(_WITNESS)) == _PRIVATE_DIR
    assert _win(provision.private_python_exe(_WITNESS)) == (
        _PRIVATE_DIR + r"\python.exe"
    )
    assert _win(provision.appenv_dir(_WITNESS)) == _APPENV_DIR
    assert _win(provision.appenv_python_exe(_WITNESS)) == _APPENV_PY


# ---------------------------------------------------------------------------
# Runner/system Python detection + rejection (pure, ntpath)
# ---------------------------------------------------------------------------


def test_forbidden_roots_include_canonical_runner_locations() -> None:
    roots = provision.forbidden_python_roots(
        localappdata=r"C:\Users\runneradmin\AppData\Local"
    )
    joined = "\n".join(roots)
    assert r"c:\hostedtoolcache\windows\python" in joined
    assert r"c:\program files\python*" in joined
    assert r"c:\users\runneradmin\appdata\local\programs\python" in joined


def test_runner_python_detection_hits_hostedtoolcache() -> None:
    violations = provision.detect_runner_python_violations(
        executable=r"C:\hostedtoolcache\windows\Python\3.13.15\x64\python.exe",
        prefix=r"C:\hostedtoolcache\windows\Python\3.13.15\x64",
        base_prefix=r"C:\hostedtoolcache\windows\Python\3.13.15\x64",
        base_executable=None,
        pyvenv_cfg_home=None,
        localappdata=r"C:\Users\runneradmin\AppData\Local",
    )
    assert any("sys.base_prefix" in v and "hostedtoolcache" in v.lower()
               for v in violations)


def test_runner_python_detection_hits_program_files() -> None:
    violations = provision.detect_runner_python_violations(
        executable=r"C:\Program Files\Python313\python.exe",
        prefix=r"C:\Program Files\Python313",
        base_prefix=r"C:\Program Files\Python313",
        base_executable=None,
        pyvenv_cfg_home=None,
        localappdata=None,
    )
    assert violations
    assert "python*" in violations[0].lower()
    assert any("sys.base_prefix" in v for v in violations)


def test_runner_python_detection_hits_default_per_user_programs() -> None:
    violations = provision.detect_runner_python_violations(
        executable=r"C:\Users\runneradmin\AppData\Local\Programs\Python\Python313\python.exe",
        prefix=r"C:\Users\runneradmin\AppData\Local\Programs\Python\Python313",
        base_prefix=r"C:\Users\runneradmin\AppData\Local\Programs\Python\Python313",
        base_executable=None,
        pyvenv_cfg_home=None,
        localappdata=r"C:\Users\runneradmin\AppData\Local",
    )
    assert violations and "programs\\python" in violations[0].lower()


def test_runner_python_detection_is_case_insensitive() -> None:
    violations = provision.detect_runner_python_violations(
        executable=r"C:\HOSTEDTOOLCACHE\Windows\Python\3.13.15\x64\python.exe",
        prefix=r"C:\HOSTEDTOOLCACHE\Windows\Python\3.13.15\x64",
        base_prefix=r"C:\HOSTEDTOOLCACHE\Windows\Python\3.13.15\x64",
        base_executable=None,
        pyvenv_cfg_home=None,
        localappdata=r"C:\Users\runneradmin\AppData\Local",
    )
    assert violations
    assert "hostedtoolcache" in violations[0].lower()


def test_clean_private_interpreter_has_no_violations() -> None:
    violations = provision.detect_runner_python_violations(
        executable=_APPENV_PY,
        prefix=_APPENV_DIR,
        base_prefix=_PRIVATE_DIR,
        base_executable=r"C:\zealfie-witness\python\python.exe",
        pyvenv_cfg_home=_PRIVATE_DIR,
        localappdata=r"C:\Users\runneradmin\AppData\Local",
    )
    assert violations == []


def test_assert_no_runner_python_raises_on_runner_base() -> None:
    with pytest.raises(provision.RunnerPythonError, match="base_prefix"):
        provision.assert_no_runner_python(
            executable=_APPENV_PY,
            prefix=_APPENV_DIR,
            base_prefix=r"C:\hostedtoolcache\windows\Python\3.13.15\x64",
            base_executable=None,
            pyvenv_cfg_home=_PRIVATE_DIR,
            localappdata=r"C:\Users\runneradmin\AppData\Local",
        )


def test_assert_no_runner_python_passes_for_clean_interpreter() -> None:
    provision.assert_no_runner_python(
        executable=_APPENV_PY,
        prefix=_APPENV_DIR,
        base_prefix=_PRIVATE_DIR,
        base_executable=r"C:\zealfie-witness\python\python.exe",
        pyvenv_cfg_home=_PRIVATE_DIR,
        localappdata=r"C:\Users\runneradmin\AppData\Local",
    )


# ---------------------------------------------------------------------------
# Provenance assertions (appenv + child venv)
# ---------------------------------------------------------------------------


def test_appenv_provenance_passes_for_private_base() -> None:
    provision.assert_appenv_provenance(
        sys_executable=_APPENV_PY,
        sys_prefix=_APPENV_DIR,
        sys_base_prefix=_PRIVATE_DIR,
        witness_root=_WITNESS,
    )


def test_appenv_provenance_rejects_runner_base_prefix() -> None:
    with pytest.raises(provision.ProvenanceError, match="base_prefix"):
        provision.assert_appenv_provenance(
            sys_executable=_APPENV_PY,
            sys_prefix=_APPENV_DIR,
            sys_base_prefix=r"C:\hostedtoolcache\windows\Python\3.13.15\x64",
            witness_root=_WITNESS,
        )


def test_appenv_provenance_rejects_wrong_prefix() -> None:
    with pytest.raises(provision.ProvenanceError, match="sys.prefix"):
        provision.assert_appenv_provenance(
            sys_executable=_APPENV_PY,
            sys_prefix=r"C:\somewhere-else\venv",
            sys_base_prefix=_PRIVATE_DIR,
            witness_root=_WITNESS,
        )


def test_appenv_provenance_rejects_executable_outside_scripts() -> None:
    with pytest.raises(provision.ProvenanceError, match="sys.executable"):
        provision.assert_appenv_provenance(
            sys_executable=r"C:\zealfie-witness\python\python.exe",
            sys_prefix=_APPENV_DIR,
            sys_base_prefix=_PRIVATE_DIR,
            witness_root=_WITNESS,
        )


def test_child_venv_provenance_passes_for_private_derived_child() -> None:
    provision.assert_child_venv_provenance(
        pyvenv_cfg_home=_PRIVATE_DIR,
        sys_base_prefix=_PRIVATE_DIR,
        child_scripts_python=r"C:\witness-child\child\Scripts\python.exe",
        private_python_dir_path=_PRIVATE_DIR,
    )


def test_child_venv_provenance_rejects_runner_home() -> None:
    with pytest.raises(provision.ProvenanceError, match="home"):
        provision.assert_child_venv_provenance(
            pyvenv_cfg_home=r"C:\hostedtoolcache\windows\Python\3.13.15\x64",
            sys_base_prefix=_PRIVATE_DIR,
            child_scripts_python=r"C:\witness-child\child\Scripts\python.exe",
            private_python_dir_path=_PRIVATE_DIR,
        )


def test_child_venv_provenance_rejects_runner_base_prefix() -> None:
    with pytest.raises(provision.ProvenanceError, match="base_prefix"):
        provision.assert_child_venv_provenance(
            pyvenv_cfg_home=_PRIVATE_DIR,
            sys_base_prefix=r"C:\Program Files\Python313",
            child_scripts_python=r"C:\witness-child\child\Scripts\python.exe",
            private_python_dir_path=_PRIVATE_DIR,
        )


def test_child_venv_provenance_rejects_non_scripts_interpreter() -> None:
    with pytest.raises(provision.ProvenanceError, match="Scripts"):
        provision.assert_child_venv_provenance(
            pyvenv_cfg_home=_PRIVATE_DIR,
            sys_base_prefix=_PRIVATE_DIR,
            child_scripts_python=r"C:\witness-child\child\bin\python",
            private_python_dir_path=_PRIVATE_DIR,
        )


# ---------------------------------------------------------------------------
# pyvenv.cfg parsing + shared assertion core
# ---------------------------------------------------------------------------


def test_parse_pyvenv_cfg_handles_crlf_and_flags() -> None:
    text = (
        "home = C:\\zealfie-witness\\python\r\n"
        "include-system-site-packages = false\r\n"
        "version = 3.13.15\r\n"
    )
    parsed = provision.parse_pyvenv_cfg(text)
    assert parsed["home"] == r"C:\zealfie-witness\python"
    assert parsed["version"] == "3.13.15"


def test_pyvenv_cfg_home_reads_value_with_seam(tmp_path) -> None:
    cfg = tmp_path / "pyvenv.cfg"
    cfg.write_text(f"home = {_PRIVATE_DIR}\n", encoding="utf-8")
    assert provision.pyvenv_cfg_home(tmp_path) == _PRIVATE_DIR


def test_pyvenv_cfg_home_returns_none_when_missing(tmp_path) -> None:
    assert provision.pyvenv_cfg_home(tmp_path / "absent") is None


def test_venv_provenance_core_asserts_home_mismatch() -> None:
    with pytest.raises(provision.ProvenanceError, match="home"):
        provision.assert_venv_provenance(
            sys_base_prefix=_PRIVATE_DIR,
            pyvenv_cfg_home=r"C:\hostedtoolcache\windows\Python\3.13.15\x64",
            expected_base_dir=_PRIVATE_DIR,
            expected_home_dir=_PRIVATE_DIR,
        )


def test_venv_provenance_core_asserts_base_mismatch() -> None:
    with pytest.raises(provision.ProvenanceError, match="base_prefix"):
        provision.assert_venv_provenance(
            sys_base_prefix=r"C:\elsewhere",
            pyvenv_cfg_home=_PRIVATE_DIR,
            expected_base_dir=_PRIVATE_DIR,
            expected_home_dir=_PRIVATE_DIR,
        )


# ---------------------------------------------------------------------------
# Entrypoint wiring (thin, runnable)
# ---------------------------------------------------------------------------


def test_entrypoint_help_runs_and_exits_zero() -> None:
    entrypoint = _REPO_ROOT / "packaging" / "windows" / "provision_windows.py"
    proc = subprocess.run(
        [sys.executable, str(entrypoint), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "provision-python" in proc.stdout
    assert "make-appenv" in proc.stdout
    assert "smoke-gui" in proc.stdout


def test_entrypoint_requires_a_subcommand() -> None:
    entrypoint = _REPO_ROOT / "packaging" / "windows" / "provision_windows.py"
    proc = subprocess.run(
        [sys.executable, str(entrypoint)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
