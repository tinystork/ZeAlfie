"""Hermetic contracts for the Windows x64/ARM64 installer RC."""

from __future__ import annotations

import importlib.util
import re
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "windows"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer-build.yml"
ISS = PKG / "installer" / "zealfie.iss"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pe = _load(PKG / "payload_arch_audit.py", "zealfie_payload_arch_audit_test")


def _write_pe(path: Path, machine: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = bytearray(0x88)
    body[0:2] = b"MZ"
    struct.pack_into("<I", body, 0x3C, 0x80)
    body[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", body, 0x84, machine)
    path.write_bytes(body)


def _payload(root: Path, machine: int) -> None:
    for relative in (
        "python/python.exe",
        "python/pythonw.exe",
        "python/python313.dll",
        "appenv/Scripts/python.exe",
        "appenv/Scripts/pythonw.exe",
        "appenv/Lib/site-packages/PySide6/Qt6Core.dll",
        "appenv/Lib/site-packages/shiboken6/Shiboken.pyd",
    ):
        _write_pe(root / relative, machine)


@pytest.mark.parametrize("machine,name", ((0x8664, "AMD64"), (0xAA64, "ARM64")))
def test_payload_pe_audit_accepts_exact_native_tree(tmp_path: Path, machine: int, name: str) -> None:
    _payload(tmp_path, machine)
    result = pe.audit_payload(tmp_path, name)
    assert result["status"] == "PASS"
    assert result["images_checked"] == 7
    assert result["machine_counts"] == {name: 7}
    assert result["inno_setup_loader_in_scope"] is False


def test_payload_pe_audit_rejects_one_cross_architecture_residue(tmp_path: Path) -> None:
    _payload(tmp_path, 0xAA64)
    _write_pe(tmp_path / "appenv/Lib/site-packages/bad.dll", 0x8664)
    with pytest.raises(pe.PeAuditError, match="non-ARM64"):
        pe.audit_payload(tmp_path, "ARM64")


def test_payload_pe_audit_rejects_invalid_pe(tmp_path: Path) -> None:
    _payload(tmp_path, 0x8664)
    bad = tmp_path / "python" / "bad.dll"
    bad.write_bytes(b"not pe")
    with pytest.raises(pe.PeAuditError, match="missing MZ"):
        pe.audit_payload(tmp_path, "AMD64")


def test_dual_arch_workflow_is_manual_and_has_two_native_lanes() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "push:" not in text.split("permissions:", 1)[0]
    for token in (
        "runner: windows-latest",
        "runner: windows-11-arm",
        "platform_tag: win_amd64",
        "platform_tag: win_arm64",
        "inno_arch: x64os",
        "inno_arch: arm64",
        "payload_arch_audit.py",
        "native-payload-audit.json",
        "ZeAlfie-Setup-0.1.4-rc1-windows-${{ matrix.artifact_arch }}.exe",
    ):
        assert token in text
    assert "gh release" not in text
    assert "git tag" not in text


def test_inno_script_is_strictly_parameterized_and_honest_about_loader() -> None:
    text = ISS.read_text(encoding="utf-8")
    assert "ArchitecturesAllowed={#InstallerArchitecture}" in text
    assert "ArchitecturesInstallIn64BitMode={#InstallerArchitecture}" in text
    assert 'InstallerArchitecture != "x64os"' in text
    assert 'InstallerArchitecture != "arm64"' in text
    assert "Inno Setup itself remains x86" in text
    assert "windows-{#ArtifactArchitecture}" in text
    assert "VersionInfoVersion={#ZeAlfieFileVersion}" in text
    assert 'DestName: "reproducibility.toml"' in text


def test_inno_product_version_is_numeric_and_text_keeps_rc() -> None:
    """Inno VS_FIXEDFILEINFO requires a NUMERIC product version."""
    text = ISS.read_text(encoding="utf-8")
    assert "VersionInfoProductVersion={#ZeAlfieFileVersion}" in text
    assert "VersionInfoProductTextVersion={#ZeAlfieDisplayVersion}" in text
    assert "VersionInfoProductVersion={#ZeAlfieDisplayVersion}" not in text
    file_version = re.search(
        r'#define ZeAlfieFileVersion "([^"]+)"', text
    ).group(1)
    display = re.search(
        r'#define ZeAlfieDisplayVersion "([^"]+)"', text
    ).group(1)
    # Up to four dot-separated numbers: 0.1.4.0 is valid, 0.1.4-rc1 is NOT.
    assert re.fullmatch(r"\d+(?:\.\d+){1,3}", file_version), file_version
    assert file_version == "0.1.4.0"
    assert display == "0.1.4-rc1"


def test_workflow_header_does_not_claim_a_push_trigger() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    header = text.split("on:", 1)[0]
    assert "workflow_dispatch ONLY" in header
    assert "workflow_dispatch AND push" not in header
    assert "push:" not in text.split("permissions:", 1)[0]


def test_arm64_reproducibility_size_comment_matches_pin() -> None:
    text = (PKG / "reproducibility.arm64.toml").read_text(encoding="utf-8")
    assert "size = 43801216" in text
    assert "43 801 216 bytes" in text
    assert "47 042 104 bytes" not in text


def test_provenance_payload_commit_is_sourced_not_hardcoded() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'payload_commit = str(wheel["source_commit"])' in text
    assert '"payload_commit": payload_commit' in text
    prov = text.split("- id: provenance", 1)[1].split(
        "- id: upload-artifacts", 1
    )[0]
    assert "e466698" not in prov
