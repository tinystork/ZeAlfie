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
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "windows-release-publish.yml"
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
        "appenv/Lib/site-packages/PySide6/Qt6Gui.dll",
        "appenv/Lib/site-packages/PySide6/Qt6Widgets.dll",
        "appenv/Lib/site-packages/PySide6/pyside6.abi3.dll",
        "appenv/Lib/site-packages/shiboken6/Shiboken.pyd",
    ):
        _write_pe(root / relative, machine)


@pytest.mark.parametrize("machine,name", ((0x8664, "AMD64"), (0xAA64, "ARM64")))
def test_payload_pe_audit_accepts_exact_native_tree(tmp_path: Path, machine: int, name: str) -> None:
    _payload(tmp_path, machine)
    result = pe.audit_payload(tmp_path, name)
    assert result["status"] == "PASS"
    assert result["images_checked"] == 10
    assert result["runtime_images_checked"] == 10
    assert result["machine_counts"] == {name: 10}
    assert result["allowed_non_native"] == []
    assert result["allowed_count"] == 0
    assert result["arm64ec_count"] == 0
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


def test_arm64ec_is_accepted_for_arm64_and_rejected_for_amd64(tmp_path: Path) -> None:
    """IMAGE_FILE_MACHINE_ARM64EC is ARM64-compatible only."""
    rel = "appenv/Lib/site-packages/PySide6/vccorlib140.dll"
    _payload(tmp_path / "arm", 0xAA64)
    _write_pe(tmp_path / "arm" / rel, 0x01C4)
    result = pe.audit_payload(tmp_path / "arm", "ARM64")
    assert result["status"] == "PASS"
    assert result["arm64ec_count"] == 1
    assert result["machine_counts"] == {"ARM64": 10, "ARM64EC": 1}
    assert result["arm64ec_is_native_compatible"] is True

    _payload(tmp_path / "x64", 0x8664)
    _write_pe(tmp_path / "x64" / rel, 0x01C4)
    with pytest.raises(pe.PeAuditError, match="non-AMD64"):
        pe.audit_payload(tmp_path / "x64", "AMD64")


def test_allowlisted_distlib_stub_is_accepted_and_reported(tmp_path: Path) -> None:
    _payload(tmp_path, 0xAA64)
    stub = "python/Lib/site-packages/pip/_vendor/distlib/t32.exe"
    _write_pe(tmp_path / stub, 0x014C)
    result = pe.audit_payload(tmp_path, "ARM64")
    assert result["status"] == "PASS"
    assert result["allowed_count"] == 1
    assert result["allowed_non_native"] == [
        {
            "path": stub,
            "machine": "I386",
            "machine_value": "0x014C",
            "reason": pe._DISTLIB_REASON,
        }
    ]
    assert result["machine_counts"] == {"ARM64": 10, "I386": 1}


def test_cross_arch_runtime_image_fails_even_with_allowlist(tmp_path: Path) -> None:
    _payload(tmp_path, 0xAA64)
    # An otherwise-allowlistable stub is present, but a RUNTIME image is wrong.
    _write_pe(
        tmp_path / "appenv/Lib/site-packages/pip/_vendor/distlib/t64.exe", 0x8664
    )
    _write_pe(tmp_path / "python/python313.dll", 0x8664)
    with pytest.raises(pe.PeAuditError, match="runtime-critical"):
        pe.audit_payload(tmp_path, "ARM64")


def test_unknown_cross_arch_image_outside_runtime_and_allowlist_fails(
    tmp_path: Path,
) -> None:
    _payload(tmp_path, 0xAA64)
    _write_pe(
        tmp_path / "appenv/Lib/site-packages/vendor_unknown/helper.dll", 0x8664
    )
    with pytest.raises(pe.PeAuditError, match="non-ARM64"):
        pe.audit_payload(tmp_path, "ARM64")


def test_x64_lane_accepts_arm64_launcher_stubs_via_allowlist(tmp_path: Path) -> None:
    _payload(tmp_path, 0x8664)
    distlib = "appenv/Lib/site-packages/pip/_vendor/distlib/t64-arm.exe"
    setuptools = "appenv/Lib/site-packages/setuptools/cli-arm64.exe"
    _write_pe(tmp_path / distlib, 0xAA64)
    _write_pe(tmp_path / setuptools, 0xAA64)
    result = pe.audit_payload(tmp_path, "AMD64")
    assert result["status"] == "PASS"
    assert result["allowed_count"] == 2
    allowed_paths = {item["path"] for item in result["allowed_non_native"]}
    assert allowed_paths == {distlib, setuptools}
    assert all(item["machine"] == "ARM64" for item in result["allowed_non_native"])
    assert all(item["reason"] for item in result["allowed_non_native"])


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


def test_release_publish_workflow_is_manual_and_fail_closed() -> None:
    """Publication-only workflow: dispatch-only, checksum first, no force."""
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    # No automatic trigger and no build trigger anywhere in the `on:` block.
    on_block = text.split("on:", 1)[1].split("permissions:", 1)[0]
    for forbidden in ("push:", "pull_request:", "schedule:", "tags:"):
        assert forbidden not in on_block
    assert "permissions:" in text
    permissions_block = text.split("permissions:", 1)[1].split("concurrency:", 1)[0]
    assert "contents: write" in permissions_block

    # Required inputs + both architecture artifact names.
    for token in ("run_id", "tag", "prerelease"):
        assert token in on_block
    assert "zealfie-setup-0.1.4-rc1-windows-x64" in text
    assert "zealfie-setup-0.1.4-rc1-windows-arm64" in text
    assert "ZeAlfie-Setup-0.1.4-rc1-windows-x64.exe" in text
    assert "ZeAlfie-Setup-0.1.4-rc1-windows-arm64.exe" in text

    # Checksums are verified BEFORE the release is created.
    assert "sha256" in text.lower()
    assert text.index("Verify assets, checksums") < text.index(
        "gh release create"
    )
    # F1: the referenced run must be validated as a successful dual-arch build
    # BEFORE any artifact download.
    gate = "Verify the referenced build run succeeded"
    download = "Download both architecture artifacts"
    assert gate in text and download in text
    assert text.index(gate) < text.index(download)
    for token in (
        "workflowName",
        "conclusion",
        "completed",
        "success",
        "headBranch",
        "Windows dual-architecture installer RC",
        "release/0.1.4-rc1-windows-dual-arch",
    ):
        assert token in text, token
    # F2: only an explicit HTTP 404 may mean "tag absent".
    assert "gh api --include" in text
    assert "could not determine tag state" in text
    assert "404" in text
    assert 'if gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${TAG}" >/dev/null 2>&1; then' not in text
    # F3: provenance/audit are bound to the lane and the resolved build SHA.
    assert "BUILD_SHA" in text
    assert text.index(gate) < text.index("Verify assets, checksums, lane/SHA provenance")
    for token in (
        "source_commit",
        "target_profile",
        "native_payload_machine",
        "expected_machine",
        '"AMD64"',
        '"ARM64"',
    ):
        assert token in text, token
    assert "refusing to move it" in text
    assert "git/ref/tags/" in text
    assert "provenance.json" in text and "native-payload-audit.json" in text
    assert "--notes-file" in text and "--target" in text
    assert "--prerelease" in text

    # Never signs, never pushes commits, never force-moves a tag.
    lowered = text.lower()
    for forbidden in ("signtool", "osslsigncode", "git push", "--force",
                      "--clobber", "cosign"):
        assert forbidden not in lowered
    assert "not code-signed" in lowered
    assert "pending external end-to-end human validation" in lowered
    assert "ci-validated" in lowered
