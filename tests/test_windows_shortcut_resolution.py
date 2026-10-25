"""Hermetic contracts for the deterministic Windows .lnk target resolver.

The ARM64 Actions lane proved the shortcut FILE exists while the external
PowerShell/WScript.Shell COM probe returned no target. These tests pin the
deterministic MS-SHLLINK parser as the PRIMARY resolver, the COM probe as a
secondary fallback, and fail-closed behaviour with diagnostics.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "windows"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sew = _load(PKG / "side_effect_witness.py", "zealfie_side_effect_witness_lnk_test")

_CLSID = bytes.fromhex("0114020000000000c000000000000046")
_GUI = r"C:\Users\u\AppData\Local\Programs\ZeAlfie\appenv\Scripts\zealfie-gui.exe"


def build_lnk(
    base: str,
    suffix: str = "",
    *,
    unicode: bool = False,
    with_id_list: bool = False,
    no_link_info: bool = False,
    idlist_items: list[bytes] | None = None,
) -> bytes:
    """Build a minimal but valid MS-SHLLINK byte string."""
    header = bytearray(0x4C)
    struct.pack_into("<I", header, 0, 0x4C)
    header[4:20] = _CLSID
    flags = 0
    if with_id_list:
        flags |= 0x1
    if not no_link_info:
        flags |= 0x2
    if unicode:
        flags |= 0x80
    struct.pack_into("<I", header, 0x14, flags)

    if no_link_info:
        return bytes(header)

    header_size = 0x24 if unicode else 0x1C
    encoding = "utf-16-le" if unicode else "latin-1"
    terminator = b"\x00\x00" if unicode else b"\x00"
    base_bytes = base.encode(encoding) + terminator
    suffix_bytes = suffix.encode(encoding) + terminator if suffix else b""
    local_off = header_size
    suffix_off = header_size + len(base_bytes) if suffix else 0

    info = bytearray(header_size)
    struct.pack_into("<I", info, 4, header_size)
    struct.pack_into("<I", info, 8, 0x1)  # VolumeIDAndLocalBasePath
    struct.pack_into("<I", info, 12, 0)  # VolumeIDOffset unused
    struct.pack_into("<I", info, 16, local_off)
    struct.pack_into("<I", info, 20, 0)  # CommonNetworkRelativeLinkOffset
    struct.pack_into("<I", info, 24, suffix_off)
    if header_size >= 0x24:
        struct.pack_into("<I", info, 28, local_off if unicode else 0)
        struct.pack_into("<I", info, 32, suffix_off if unicode else 0)
    body = bytearray(info) + base_bytes + suffix_bytes
    struct.pack_into("<I", body, 0, len(body))

    out = bytearray(header)
    if with_id_list:
        items = b"".join(idlist_items or []) + b"\x00\x00"
        out += struct.pack("<H", len(items)) + items
    out += body
    return bytes(out)


def _raw_item(data: bytes) -> bytes:
    """One ItemID with arbitrary payload (used for unknown/root items)."""
    return struct.pack("<H", len(data) + 2) + data


def _fs_item(indicator: int, name: str) -> bytes:
    """A file-system ItemID (0x30-0x3F) with the fixed 12-byte header."""
    data = bytearray([indicator, 0x00])
    data += struct.pack("<I", 1234)      # FileSize
    data += struct.pack("<I", 0)         # DOS timestamp
    data += struct.pack("<H", 0x20)      # FileAttributeFlags
    if indicator & 0x04:
        data += name.encode("utf-16-le") + b"\x00\x00"
    else:
        data += name.encode("latin-1") + b"\x00"
    return _raw_item(bytes(data))


def build_idlist_only(items: list[bytes]) -> bytes:
    """A valid .lnk whose ONLY target carrier is LinkTargetIDList."""
    header = bytearray(0x4C)
    struct.pack_into("<I", header, 0, 0x4C)
    header[4:20] = _CLSID
    struct.pack_into("<I", header, 0x14, 0x1)  # HasLinkTargetIDList only
    idlist = b"".join(items) + b"\x00\x00"
    return bytes(header) + struct.pack("<H", len(idlist)) + idlist


def _launcher_idlist(exe: str = "zealfie-gui.exe") -> list[bytes]:
    return [
        _raw_item(b"\x1f\x00"),
        _raw_item(b"\x2f\x00C:\\\x00"),
        _fs_item(0x35, "appenv"),
        _fs_item(0x35, "Scripts"),
        _fs_item(0x36, exe),
    ]


def _write_lnk(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_ansi_local_base_path_resolves() -> None:
    data = build_lnk(_GUI)
    assert sew.parse_shllink_target(data) == _GUI


def test_unicode_local_base_path_and_common_suffix_resolve() -> None:
    base = r"C:\Users\u\AppData\Local\Programs\ZeAlfie\appenv\Scripts"
    suffix = r"\zealfie-gui.exe"
    data = build_lnk(base, suffix, unicode=True, with_id_list=True)
    assert sew.parse_shllink_target(data) == _GUI


def test_link_without_local_target_returns_none() -> None:
    assert sew.parse_shllink_target(build_lnk(_GUI, no_link_info=True)) is None


@pytest.mark.parametrize(
    "data",
    (
        b"",
        b"MZ",
        b"\x00" * 0x4B,
        # Valid header but a LinkInfo size that runs past the buffer.
        build_lnk(_GUI)[:0x4C] + struct.pack("<I", 4096),
    ),
)
def test_malformed_lnk_raises_shell_link_error(data: bytes) -> None:
    with pytest.raises(sew.ShellLinkError):
        sew.parse_shllink_target(data)


# ---------------------------------------------------------------------------
# LinkTargetIDList positive evidence
# ---------------------------------------------------------------------------

def test_idlist_only_launcher_segments_resolve_via_shllink_idlist(
    tmp_path: Path,
) -> None:
    data = build_idlist_only(_launcher_idlist())
    assert sew.parse_shllink(data) == (
        None,
        ["appenv", "Scripts", "zealfie-gui.exe"],
    )
    path = _write_lnk(tmp_path / "ZeAlfie.lnk", data)
    target, diag = sew._resolve_shortcut_target(
        path, expected_target=_GUI, _com_probe=_diag_fail
    )
    assert target == _GUI
    assert diag["method_used"] == "shllink_idlist"
    assert diag["idlist_matched_expected"] is True
    assert diag["idlist_segments"] == ["appenv", "Scripts", "zealfie-gui.exe"]
    assert diag["com_rc"] is None  # COM never consulted

    snapshot = {
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": target,
        "shortcut_target_probe": diag,
        "uninstall_hkcu": {"zealfie 0.1.0": {"display_name": "ZeAlfie 0.1.0"}},
        "uninstall_hklm": {},
    }
    install_root = r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
    assert sew.verify_install_findings({}, snapshot, install_root) == []


def test_idlist_only_different_exe_fails_closed(tmp_path: Path) -> None:
    data = build_idlist_only(_launcher_idlist("cmd.exe"))
    path = _write_lnk(tmp_path / "ZeAlfie.lnk", data)
    target, diag = sew._resolve_shortcut_target(
        path, expected_target=_GUI, _com_probe=_diag_fail
    )
    assert target is None
    assert diag["method_used"] == "none"
    assert diag["idlist_segments"][-1] == "cmd.exe"
    snapshot = {
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": None,
        "shortcut_target_probe": diag,
        "uninstall_hkcu": {"zealfie 0.1.0": {"display_name": "ZeAlfie 0.1.0"}},
        "uninstall_hklm": {},
    }
    findings = sew.verify_install_findings({}, snapshot, _GUI.rsplit("\\", 3)[0])
    assert any("could not be resolved" in f and "cmd.exe" in f for f in findings)


def test_idlist_unknown_items_only_fails_closed(tmp_path: Path) -> None:
    data = build_idlist_only([_raw_item(b"\x1f\x00"), _raw_item(b"\x20\x00")])
    path = _write_lnk(tmp_path / "ZeAlfie.lnk", data)
    assert sew.parse_shllink(data) == (None, [])
    target, diag = sew._resolve_shortcut_target(
        path, expected_target=_GUI, _com_probe=_diag_fail
    )
    assert target is None
    assert diag["idlist_segments"] == []
    assert diag["method_used"] == "none"


def test_truncated_or_overrunning_itemid_is_explicit(tmp_path: Path) -> None:
    overrun = build_idlist_only([struct.pack("<H", 100) + b"x"])
    with pytest.raises(sew.ShellLinkError, match="overruns"):
        sew.parse_shllink(overrun)
    path = _write_lnk(tmp_path / "overrun.lnk", overrun)
    target, diag = sew._resolve_shortcut_target(
        path, expected_target=_GUI, _com_probe=_diag_fail
    )
    assert target is None
    assert "overruns" in diag["shllink_error"]

    truncated_name = build_idlist_only([
        _raw_item(b"\x36\x00" + b"\x00" * 10)  # < 12-byte fixed header
    ])
    with pytest.raises(sew.ShellLinkError, match="truncated file entry"):
        sew.parse_shllink(truncated_name)


def test_linkinfo_target_stays_authoritative_over_idlist(tmp_path: Path) -> None:
    data = build_lnk(
        _GUI, with_id_list=True, idlist_items=[_fs_item(0x36, "cmd.exe")]
    )
    path = _write_lnk(tmp_path / "ZeAlfie.lnk", data)
    target, diag = sew._resolve_shortcut_target(
        path, expected_target=_GUI, _com_probe=_diag_fail
    )
    assert target == _GUI
    assert diag["method_used"] == "shllink_parser"
    assert diag["idlist_segments"] == ["cmd.exe"]


# ---------------------------------------------------------------------------
# Resolution order + diagnostics
# ---------------------------------------------------------------------------

def _diag_fail(_path: str):
    return None, {
        "com_rc": 1,
        "com_stderr_tail": "WScript.Shell unavailable",
        "powershell_found": False,
    }


def test_com_probe_failure_but_parse_success_keeps_target(tmp_path: Path) -> None:
    """ARM64 regression: a silent COM failure must NOT hide a valid shortcut."""
    path = _write_lnk(tmp_path / "ZeAlfie.lnk", build_lnk(_GUI))
    target, diag = sew._resolve_shortcut_target(path, _com_probe=_diag_fail)
    assert target == _GUI
    assert diag["method_used"] == "shllink_parser"
    assert diag["shllink_error"] is None
    assert diag["com_rc"] is None  # COM never consulted
    assert json.dumps(diag)


def test_both_methods_fail_closed_with_diagnostics(tmp_path: Path) -> None:
    path = _write_lnk(
        tmp_path / "ZeAlfie.lnk", build_lnk(_GUI, no_link_info=True)
    )
    target, diag = sew._resolve_shortcut_target(path, _com_probe=_diag_fail)
    assert target is None
    assert diag["method_used"] == "none"
    assert diag["shllink_error"] == "no local target path in .lnk"
    assert diag["com_rc"] == 1
    assert "WScript" in diag["com_stderr_tail"]

    snapshot = {
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": None,
        "shortcut_target_probe": diag,
        "uninstall_hkcu": {
            "zealfie 0.1.0": {"display_name": "ZeAlfie 0.1.0"},
        },
        "uninstall_hklm": {},
    }
    findings = sew.verify_install_findings({}, snapshot, r"C:\Programs\ZeAlfie")
    assert any(
        "could not be resolved" in f and '"com_rc": 1' in f for f in findings
    )
    assert json.dumps(snapshot)


def test_truncated_or_garbage_lnk_never_crashes(tmp_path: Path) -> None:
    path = _write_lnk(tmp_path / "broken.lnk", b"not a shell link")
    target, diag = sew._resolve_shortcut_target(path, _com_probe=_diag_fail)
    assert target is None
    assert diag["method_used"] == "none"
    assert "truncated shell link header" in diag["shllink_error"]


def test_parsed_wrong_target_fails_closed(tmp_path: Path) -> None:
    wrong = r"C:\Windows\System32\python.exe"
    path = _write_lnk(tmp_path / "ZeAlfie.lnk", build_lnk(wrong))
    target, diag = sew._resolve_shortcut_target(path, _com_probe=_diag_fail)
    assert target == wrong
    snapshot = {
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": target,
        "shortcut_target_probe": diag,
        "uninstall_hkcu": {"zealfie 0.1.0": {"display_name": "ZeAlfie 0.1.0"}},
        "uninstall_hklm": {},
    }
    findings = sew.verify_install_findings({}, snapshot, r"C:\Programs\ZeAlfie")
    assert any(
        "is not the installed windowed launcher" in f and "python.exe" in f
        for f in findings
    )


def test_capture_snapshot_records_probe_and_keeps_injectable_seam(
    tmp_path: Path,
) -> None:
    lnk = tmp_path / "ZeAlfie.lnk"
    lnk.write_bytes(b"placeholder")

    def resolver(path: str):
        return _GUI, {
            "method_used": "test_resolver",
            "shllink_error": None,
            "com_rc": None,
            "com_stderr_tail": None,
            "powershell_found": None,
            "com_error": None,
        }

    snapshot = sew.capture_snapshot(
        _registry=lambda hive, subkey: {},
        _registry_subkeys=lambda hive, subkey: [],
        _which=lambda exe: None,
        _py_0p=lambda exe: None,
        _isfile=lambda path: True,
        _exists=lambda path: False,
        _env=lambda name: {
            "APPDATA": str(tmp_path),
            "LOCALAPPDATA": str(tmp_path),
        }.get(name),
        _shortcut_resolver=resolver,
    )
    assert snapshot["start_menu_shortcut_target"] == _GUI
    assert snapshot["shortcut_target_probe"]["method_used"] == "test_resolver"
    assert json.dumps(snapshot)


# ---------------------------------------------------------------------------
# No x64 regression
# ---------------------------------------------------------------------------

def test_correct_target_without_probe_key_is_clean() -> None:
    snapshot = {
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": _GUI,
        "uninstall_hkcu": {"zealfie 0.1.0": {"display_name": "ZeAlfie 0.1.0"}},
        "uninstall_hklm": {},
    }
    assert sew.verify_install_findings(
        {}, snapshot, r"C:\Users\u\AppData\Local\Programs\ZeAlfie"
    ) == []


# ---------------------------------------------------------------------------
# CLI-layer evidence preservation (best effort, never masks the finding)
# ---------------------------------------------------------------------------

def _base_snapshot(lnk: Path, target, probe) -> dict:
    return {
        "schema": 1,
        "user_path": [],
        "machine_path": [],
        "py_launcher_path": None,
        "py_0p": None,
        "py_association_progid": None,
        "pythoncore_3_13_hkcu_install_path": None,
        "pythoncore_3_13_hklm_install_path": None,
        "uninstall_hkcu": {"zealfie 0.1.0": {"display_name": "ZeAlfie 0.1.0"}},
        "uninstall_hklm": {},
        "start_menu_shortcut": str(lnk),
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": target,
        "shortcut_target_probe": probe,
        "runtime_exists": False,
        "zealfie_appdata_exists": False,
    }


def test_cli_preserves_unresolved_shortcut_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    lnk = tmp_path / "ZeAlfie.lnk"
    lnk.write_bytes(build_idlist_only(_launcher_idlist("cmd.exe")))
    probe = {
        "method_used": "none",
        "shllink_error": "no local target path in .lnk",
        "com_rc": 1,
        "com_stderr_tail": "TimeoutExpired",
        "powershell_found": True,
        "com_error": "TimeoutExpired",
        "idlist_segments": ["appenv", "Scripts", "cmd.exe"],
    }
    snap = _base_snapshot(lnk, None, probe)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"snapshot": snap}), encoding="utf-8")
    monkeypatch.setenv("ZEALFIE_STAGE", str(tmp_path / "stage"))
    monkeypatch.setattr(sew, "capture_snapshot", lambda **kw: dict(snap))

    out = tmp_path / "audit.json"
    rc = sew.main(
        [
            "verify-install",
            "--baseline", str(baseline),
            "--install-root", r"C:\Programs\ZeAlfie",
            "--out", str(out),
        ]
    )
    assert rc == 1
    evidence = tmp_path / "stage" / "setup-output" / "shortcut-evidence"
    assert (evidence / "ZeAlfie.lnk").read_bytes() == lnk.read_bytes()
    note = json.loads((evidence / "shortcut-probe.json").read_text())
    assert note["command"] == "verify-install"
    assert note["probe"]["com_rc"] == 1
    assert note["probe"]["idlist_segments"][-1] == "cmd.exe"


def test_cli_evidence_copy_is_best_effort(tmp_path: Path, monkeypatch) -> None:
    """A copy failure must not raise or change the audit outcome."""
    snapshot = {
        "start_menu_shortcut_exists": True,
        "start_menu_shortcut_target": None,
        "start_menu_shortcut": str(tmp_path / "does-not-exist.lnk"),
        "shortcut_target_probe": {"method_used": "none"},
    }
    monkeypatch.setenv("ZEALFIE_STAGE", str(tmp_path / "stage"))
    sew._preserve_shortcut_evidence(snapshot, "verify-install")
    note = json.loads(
        (tmp_path / "stage" / "setup-output" / "shortcut-evidence"
         / "shortcut-probe.json").read_text()
    )
    assert note["probe"]["method_used"] == "none"
