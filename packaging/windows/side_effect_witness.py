"""ZeAlfie Windows installer — non-invasive side-effect witness
(ZA-WIN-BOOT-02 closure; ZA-WIN-BOOT-03B no-provider semantics;
ZA-WIN-UNINSTALL-01 complete-uninstall semantics).
Baseline → post-install → post-uninstall DELTA audit of the machine-scope
footprint the installer is allowed to touch.  Since ZA-WIN-BOOT-03B the
substrate is a **python-build-standalone** runtime (plain extracted
files, no installer), so the installer's contract is:

* FORBIDDEN pollution AND FORBIDDEN provider state: user/machine PATH
  unchanged; ``py.exe`` unchanged (presence/path AND its ``py -0p``
  registration list); ``.py`` association unchanged; existing
  ``PythonCore\\3.13`` (HKCU + HKLM) unchanged; NO NEW and NO CHANGED
  CPython Apps&Features/Uninstall entry in either hive — the standalone
  substrate creates NO provider state of its own and must not touch any
  of the host's (baseline-delta, so a pre-existing host CPython is fine
  as long as it stays byte-identical).
* EXPECTED ZeAlfie shell: Start Menu shortcut targeting
  ``{app}\\appenv\\Scripts\\zealfie-gui.exe`` and a per-user ZeAlfie
  uninstall registration (no machine-scope ZeAlfie registration).
* UNINSTALL: everything installer-owned is removed WITH the installer —
  shortcut, per-user ZeAlfie registration, assets, the whole private
  runtime ``{app}\\python`` + ``{app}\\appenv`` (plain files with no
  external registration: nothing on the host references them, so nothing
  is preserved) AND the whole ZeAlfie-owned managed app-data tree
  ``%LOCALAPPDATA%\\zealfie`` (installed products' managed runtime
  slots/state/cache under ``runtime\\``, the install work/cache staging
  under ``work\\``, the persisted ``desired-products.toml`` selection and
  the internal mutation lock — all disposable ZeAlfie-owned state, no
  user-authored data).  Host Python/launcher/PATH/associations/registry
  unchanged.  Cleanup never broadens outside that owned namespace:
  never ``{localappdata}`` itself and never any non-ZeAlfie path (no
  Documents/Pictures/astronomy data) — nothing is deleted outside its
  documented footprint.

Design rules (mirror the other ``packaging/windows`` modules):

* **pure comparison core** — snapshot capture and delta comparison are
  separate functions with injectable seams (``_registry``,
  ``_registry_subkeys``, ``_which``, ``_py_0p``, ``_isfile``, ``_exists``,
  ``_env``, ``_shortcut_target``) so the logic is hermetically unit-testable
  on Linux without ``winreg``/Windows (``winreg`` is imported lazily inside
  the real readers only);
* **stdlib-only**; ``ntpath`` normalisation so Windows path comparisons are
  deterministic on every host;
* **fail closed** — findings exit non-zero with machine-readable JSON
  evidence plus clear stdout lines.
"""


from __future__ import annotations

import argparse
import json
import ntpath
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "SideEffectError",
    "capture_snapshot",
    "verify_install_findings",
    "verify_uninstall_findings",
    "write_audit",
]

PYTHONCORE_SUBKEY = 'Software\\Python\\PythonCore\\3.13\\InstallPath'
ENV_HKCU_SUBKEY = 'Environment'
ENV_HKLM_SUBKEY = 'SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment'
UNINSTALL_SUBKEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall'
ASSOC_PY_SUBKEY = 'Software\\Classes\\.py'
START_MENU_REL = os.path.join("Microsoft", "Windows", "Start Menu",
                              "Programs", "ZeAlfie.lnk")
# The ZeAlfie-owned managed app-data namespace under %LOCALAPPDATA%:
# runtime\ (managed product slots/state/cache), work\ (install
# staging/cache), desired-products.toml (persisted selection) and the
# internal .runtime.zealfie-mutation.lock — ALL disposable ZeAlfie-owned
# application state (no user-authored data).  Since ZA-WIN-UNINSTALL-01
# the uninstaller removes the WHOLE tree with the installer.
ZEALFIE_APPDATA_REL = os.path.join("zealfie")
RUNTIME_REL = os.path.join(ZEALFIE_APPDATA_REL, "runtime")


class SideEffectError(RuntimeError):
    """A side-effect audit failed (fail closed)."""


# ---------------------------------------------------------------------------
# Real (Windows) readers — winreg imported lazily so the module imports and
# tests cleanly on Linux.
# ---------------------------------------------------------------------------


def _real_registry(hive: str, subkey: str) -> dict[str, str] | None:
    """Return {value_name: string data} of a registry key, or None if absent.

    *hive* is ``"HKCU"`` or ``"HKLM"`` (HKLM is read through the 64-bit
    view).  Only REG_SZ / REG_EXPAND_SZ values are captured.  The real
    readers require Windows (winreg); off-Windows they fail closed with a
    clear error.
    """
    try:
        import winreg
    except ImportError as exc:
        raise SideEffectError("side-effect witness requires Windows "
                              "(winreg is unavailable on this host)") from exc

    hive_map = {
        "HKCU": (winreg.HKEY_CURRENT_USER, 0),
        "HKLM": (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    }
    if hive not in hive_map:
        raise SideEffectError(f"unknown hive: {hive!r}")
    root, extra_flags = hive_map[hive]
    try:
        key = winreg.OpenKey(root, subkey, 0,
                             winreg.KEY_READ | extra_flags)
    except OSError:
        return None
    try:
        result: dict[str, str] = {}
        index = 0
        while True:
            try:
                name, value, typ = winreg.EnumValue(key, index)
            except OSError:
                break
            if typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                result[str(name)] = str(value)
            index += 1
        return result
    finally:
        key.Close()


def _real_registry_subkeys(hive: str, subkey: str) -> list[str]:
    """Return the subkey names of a registry key ([] when absent)."""
    try:
        import winreg
    except ImportError as exc:
        raise SideEffectError("side-effect witness requires Windows "
                              "(winreg is unavailable on this host)") from exc

    hive_map = {
        "HKCU": (winreg.HKEY_CURRENT_USER, 0),
        "HKLM": (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    }
    root, extra_flags = hive_map[hive]
    try:
        key = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | extra_flags)
    except OSError:
        return []
    try:
        names: list[str] = []
        index = 0
        while True:
            try:
                names.append(winreg.EnumKey(key, index))
            except OSError:
                break
            index += 1
        return names
    finally:
        key.Close()


def _real_which(exe: str) -> str | None:
    return shutil.which(exe)


def _real_py_0p(py_exe: str | None) -> list[str] | None:
    """Return the ``py -0p`` registration lines (or None when py.exe is
    absent/unusable).  ``py -0p`` lists every registered Python; the
    standalone substrate must never add a registration, so the list must
    be byte-identical to the baseline."""
    if not py_exe or sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            [py_exe, "-0p"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return lines or None


def _real_isfile(path: str) -> bool:
    return os.path.isfile(path)


def _real_exists(path: str) -> bool:
    return os.path.exists(path)


def _real_env(name: str) -> str | None:
    return os.environ.get(name)


def _real_shortcut_target(path: str) -> str | None:
    """Resolve a .lnk target via the WScript.Shell COM object (Windows).

    Kept as a thin compatibility wrapper; :func:`_probe_shortcut_target_com`
    carries the diagnostics used by the witness snapshot.
    """
    target, _diag = _probe_shortcut_target_com(path)
    return target


def _probe_shortcut_target_com(path: str) -> tuple[str | None, dict]:
    """Secondary resolver: WScript.Shell COM via powershell.exe.

    Never the primary method: the deterministic ``.lnk`` parser runs first and
    a silent COM failure can no longer make a valid shortcut look unresolvable.
    Returns ``(target, diagnostics)``; diagnostics are always JSON-safe.
    """
    diag = {
        "com_rc": None,
        "com_stderr_tail": None,
        "powershell_found": False,
        "com_error": None,
    }
    if sys.platform != "win32":
        diag["com_error"] = "not running on Windows"
        return None, diag
    powershell = shutil.which("powershell.exe")
    diag["powershell_found"] = bool(powershell)
    if not powershell:
        diag["com_error"] = "powershell.exe not found"
        return None, diag
    escaped = str(path).replace("'", "''")
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
        + escaped + "'); $s.TargetPath"
    )
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diag["com_error"] = f"{type(exc).__name__}: {exc}"
        return None, diag
    diag["com_rc"] = int(proc.returncode)
    if proc.returncode != 0:
        diag["com_stderr_tail"] = (proc.stderr or "").strip()[-400:]
        return None, diag
    target = proc.stdout.strip()
    return (target or None), diag


# ---------------------------------------------------------------------------
# Deterministic MS-SHLLINK reader (stdlib only, hermetic, no COM)
# ---------------------------------------------------------------------------

_SHELL_LINK_HEADER_SIZE = 0x4C
_SHELL_LINK_CLSID = bytes.fromhex("0114020000000000c000000000000046")
_HAS_LINK_TARGET_ID_LIST = 0x00000001
_HAS_LINK_INFO = 0x00000002
_VOLUME_ID_AND_LOCAL_BASE_PATH = 0x00000001


class ShellLinkError(ValueError):
    """The bytes are not a well-formed MS-SHLLINK structure."""


def _u16(data: bytes, offset: int, label: str) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ShellLinkError(f"truncated {label} at 0x{offset:X}")
    return int.from_bytes(data[offset:offset + 2], "little")


def _u32(data: bytes, offset: int, label: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ShellLinkError(f"truncated {label} at 0x{offset:X}")
    return int.from_bytes(data[offset:offset + 4], "little")


def _ansi_string(data: bytes, offset: int, label: str, *, allow_zero: bool = False) -> str:
    if offset < 0 or (offset == 0 and not allow_zero) or offset >= len(data):
        raise ShellLinkError(f"invalid {label} offset 0x{offset:X}")
    end = data.find(b"\x00", offset)
    if end < 0:
        raise ShellLinkError(f"unterminated {label} at 0x{offset:X}")
    # LocalBasePath is ANSI; latin-1 is byte-preserving and deterministic.
    return data[offset:end].decode("latin-1")


def _unicode_string(data: bytes, offset: int, label: str, *, allow_zero: bool = False) -> str:
    if offset < 0 or (offset == 0 and not allow_zero) or offset >= len(data) or offset % 2:
        raise ShellLinkError(f"invalid {label} offset 0x{offset:X}")
    end = offset
    while True:
        if end + 2 > len(data):
            raise ShellLinkError(f"unterminated {label} at 0x{offset:X}")
        if data[end:end + 2] == b"\x00\x00":
            break
        end += 2
    return data[offset:end].decode("utf-16-le")


def _decode_idlist_item_name(item_data: bytes) -> str | None:
    """Decode the NAME of a file-system ItemID (0x30-0x3F), else ``None``.

    Layout (MS-SHLLINK / libfwsi file entry): ClassTypeIndicator (1),
    Unknown (1), FileSize (4), DOS timestamp (4), FileAttributeFlags (2),
    PrimaryName (ANSI or UTF-16LE when the 0x04 bit is set), NUL-terminated.
    """
    if not item_data:
        raise ShellLinkError("empty ItemID")
    indicator = item_data[0]
    if not 0x30 <= indicator <= 0x3F:
        return None  # root/volume/network/URI/control-panel: safely ignored
    if len(item_data) < 12:
        raise ShellLinkError("truncated file entry ItemID")
    name_data = item_data[12:]
    if not name_data:
        raise ShellLinkError("truncated file entry ItemID")
    if indicator & 0x04:
        name = _unicode_string(name_data, 0, "IDList unicode name", allow_zero=True)
    else:
        name = _ansi_string(name_data, 0, "IDList ANSI name", allow_zero=True)
    return name or None


def _walk_idlist(idlist: bytes) -> list[str]:
    """Return the file-system NAME segments of a LinkTargetIDList."""
    segments: list[str] = []
    offset = 0
    while True:
        if offset + 2 > len(idlist):
            raise ShellLinkError("missing LinkTargetIDList terminator")
        size = _u16(idlist, offset, "ItemID size")
        if size == 0:
            return segments
        if size < 2:
            raise ShellLinkError(f"invalid ItemID size {size}")
        if offset + size > len(idlist):
            raise ShellLinkError("ItemID overruns LinkTargetIDList")
        item_data = idlist[offset + 2:offset + size]
        offset += size
        name = _decode_idlist_item_name(item_data)
        if name:
            segments.append(name)


def parse_shllink(data: bytes) -> tuple[str | None, list[str]]:
    """Parse a ``.lnk`` into ``(link_info_target, idlist_segments)``.

    ``link_info_target`` is the authoritative local target carried by LinkInfo
    (ANSI or Unicode). ``idlist_segments`` is the best-effort sequence of
    file-system names decoded from LinkTargetIDList (POSITIVE evidence only:
    the caller must match its trailing segments against the expected target).
    Malformed/truncated input raises :class:`ShellLinkError`.
    """
    if len(data) < _SHELL_LINK_HEADER_SIZE:
        raise ShellLinkError("truncated shell link header")
    if _u32(data, 0, "HeaderSize") != _SHELL_LINK_HEADER_SIZE:
        raise ShellLinkError("unexpected shell link HeaderSize")
    if data[4:20] != _SHELL_LINK_CLSID:
        raise ShellLinkError("unexpected shell link CLSID")
    flags = _u32(data, 0x14, "LinkFlags")
    pos = _SHELL_LINK_HEADER_SIZE
    idlist_segments: list[str] = []
    if flags & _HAS_LINK_TARGET_ID_LIST:
        id_list_size = _u16(data, pos, "LinkTargetIDList size")
        pos += 2
        if id_list_size < 2:
            raise ShellLinkError("empty LinkTargetIDList")
        if pos + id_list_size > len(data):
            raise ShellLinkError("truncated LinkTargetIDList")
        idlist_segments = _walk_idlist(data[pos:pos + id_list_size])
        pos += id_list_size
    if not flags & _HAS_LINK_INFO:
        return None, idlist_segments
    link_info_size = _u32(data, pos, "LinkInfoSize")
    if link_info_size < 0x1C:
        raise ShellLinkError("LinkInfo smaller than its fixed header")
    if pos + link_info_size > len(data):
        raise ShellLinkError("truncated LinkInfo")
    info = data[pos:pos + link_info_size]
    header_size = _u32(info, 4, "LinkInfoHeaderSize")
    if header_size < 0x1C or header_size > link_info_size:
        raise ShellLinkError("invalid LinkInfoHeaderSize")
    info_flags = _u32(info, 8, "LinkInfoFlags")
    if not info_flags & _VOLUME_ID_AND_LOCAL_BASE_PATH:
        return None, idlist_segments
    local_ansi = _u32(info, 16, "LocalBasePathOffset")
    suffix_ansi = _u32(info, 24, "CommonPathSuffixOffset")
    if header_size >= 0x24:
        local_unicode = _u32(info, 28, "LocalBasePathOffsetUnicode")
        suffix_unicode = _u32(info, 32, "CommonPathSuffixOffsetUnicode")
        if local_unicode:
            base = _unicode_string(info, local_unicode, "LocalBasePathUnicode")
            suffix = (
                _unicode_string(
                    info, suffix_unicode, "CommonPathSuffixUnicode"
                )
                if suffix_unicode
                else ""
            )
            target = base + suffix
            return (target or None), idlist_segments
    if not local_ansi:
        return None, idlist_segments
    base = _ansi_string(info, local_ansi, "LocalBasePath")
    suffix = (
        _ansi_string(info, suffix_ansi, "CommonPathSuffix")
        if suffix_ansi
        else ""
    )
    target = base + suffix
    return (target or None), idlist_segments


def parse_shllink_target(data: bytes) -> str | None:
    """Return the authoritative LinkInfo target path of a real ``.lnk``.

    Compatibility wrapper around :func:`parse_shllink`; IDList-only links have
    no authoritative target and return ``None`` here.
    """
    return parse_shllink(data)[0]


#: Trailing segments that MUST match for IDList-derived positive evidence.
_IDLIST_LAUNCHER_TAIL = ("appenv", "scripts", "zealfie-gui.exe")


def _segments_match_launcher(segments: list[str], expected_target: str | None) -> bool:
    """True only when IDList segments END WITH the expected launcher path."""
    if not expected_target or len(segments) < len(_IDLIST_LAUNCHER_TAIL):
        return False
    expected = [
        part for part in ntpath.normpath(str(expected_target)).split("\\") if part
    ]
    if len(expected) < len(_IDLIST_LAUNCHER_TAIL):
        return False
    expected_tail = [ntpath.normcase(p) for p in expected[-len(_IDLIST_LAUNCHER_TAIL):]]
    if expected_tail != list(_IDLIST_LAUNCHER_TAIL):
        return False
    observed_tail = [
        ntpath.normcase(s) for s in segments[-len(_IDLIST_LAUNCHER_TAIL):]
    ]
    return observed_tail == expected_tail


def _resolve_shortcut_target(
    path: str,
    *,
    expected_target: str | None = None,
    _parser=parse_shllink,
    _com_probe=_probe_shortcut_target_com,
) -> tuple[str | None, dict]:
    """Resolve a ``.lnk`` target deterministically, then by COM as fallback.

    Order: (1) authoritative ``LinkInfo`` target, (2) ``LinkTargetIDList``
    POSITIVE evidence when its trailing segments match *expected_target*,
    (3) PowerShell COM, (4) fail closed.

    Returns ``(target, diagnostics)``. Diagnostics always contain
    ``method_used`` (``shllink_parser`` / ``shllink_idlist`` /
    ``powershell_com`` / ``none``), ``shllink_error``, ``com_rc``,
    ``com_stderr_tail``, ``powershell_found`` and ``idlist_segments`` so a
    failure can never be silent.
    """
    diag = {
        "method_used": None,
        "shllink_error": None,
        "com_rc": None,
        "com_stderr_tail": None,
        "powershell_found": None,
        "com_error": None,
        "idlist_segments": None,
    }
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        target, idlist_segments = _parser(raw)
        diag["idlist_segments"] = list(idlist_segments)
        if target:
            diag["method_used"] = "shllink_parser"
            return target, diag
        diag["shllink_error"] = "no local target path in .lnk"
        if _segments_match_launcher(idlist_segments, expected_target):
            # POSITIVE IDList evidence, not an authoritative resolved path.
            diag["method_used"] = "shllink_idlist"
            diag["idlist_matched_expected"] = True
            return str(expected_target), diag
    except ShellLinkError as exc:
        diag["shllink_error"] = str(exc)
    except OSError as exc:
        diag["shllink_error"] = f"cannot read .lnk: {exc}"
    target, com_diag = _com_probe(path)
    diag.update(com_diag)
    if target:
        diag["method_used"] = "powershell_com"
        return target, diag
    diag["method_used"] = "none"
    return None, diag


# ---------------------------------------------------------------------------
# Pure helpers (ntpath-normalised so behaviour is identical on every host)
# ---------------------------------------------------------------------------


def _win_norm(path: str | None) -> str:
    if path is None:
        return ""
    return ntpath.normcase(ntpath.normpath(str(path)))


def _path_list(raw: str | None) -> list[str]:
    """Split a registry PATH value into normalised, non-empty entries."""
    if raw is None:
        return []
    raw = os.path.expandvars(raw)
    return [ntpath.normcase(p.strip()) for p in raw.split(";") if p.strip()]


def _uninstall_scan(registry, subkeys, hive: str) -> dict:
    """Relevant Uninstall entries (Python/ZeAlfie) of one hive.

    Returns {display_name_lower: {"display_name", "install_location",
    "uninstall_string"}} — the values needed for scope/path assertions.
    """
    result: dict[str, dict] = {}
    for child in subkeys(hive, UNINSTALL_SUBKEY):
        key_values = registry(hive, UNINSTALL_SUBKEY + "\\" + child) or {}
        display = str(key_values.get("DisplayName", "")).strip()
        lowered = display.lower()
        if "python" not in lowered and "zealfie" not in lowered:
            continue
        result[lowered] = {
            "display_name": display,
            "install_location": key_values.get("InstallLocation"),
            "uninstall_string": key_values.get("UninstallString"),
        }
    return result


def _start_menu_lnk(env) -> str | None:
    appdata = env("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, START_MENU_REL)


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------


def capture_snapshot(
    *,
    expected_shortcut_target: str | None = None,
    _registry=_real_registry,
    _registry_subkeys=_real_registry_subkeys,
    _which=_real_which,
    _py_0p=_real_py_0p,
    _isfile=_real_isfile,
    _exists=_real_exists,
    _env=_real_env,
    _shortcut_target=None,
    _shortcut_resolver=None,
) -> dict:
    """Capture the machine-scope footprint relevant to the installer.

    ``_shortcut_target`` (legacy seam) takes a path and returns a target;
    ``_shortcut_resolver`` takes a path and returns ``(target, diagnostics)``.
    The default resolver parses the ``.lnk`` deterministically first (LinkInfo,
    then positive LinkTargetIDList evidence against
    ``expected_shortcut_target``), and only then falls back to the PowerShell
    COM probe.
    """
    hkcu_env = _registry("HKCU", ENV_HKCU_SUBKEY) or {}
    hklm_env = _registry("HKLM", ENV_HKLM_SUBKEY) or {}
    py_assoc = _registry("HKCU", ASSOC_PY_SUBKEY) or {}
    core_hkcu = _registry("HKCU", PYTHONCORE_SUBKEY) or {}
    core_hklm = _registry("HKLM", PYTHONCORE_SUBKEY) or {}

    lnk = _start_menu_lnk(_env)
    local = _env("LOCALAPPDATA")
    appdata_root = os.path.join(local, ZEALFIE_APPDATA_REL) if local else None
    runtime = os.path.join(appdata_root, "runtime") if appdata_root else None
    py_launcher_path = _which("py.exe")

    lnk_exists = bool(lnk and _isfile(lnk))
    shortcut_target = None
    if lnk_exists:
        if _shortcut_resolver is not None:
            shortcut_target, shortcut_probe = _shortcut_resolver(lnk)
        elif _shortcut_target is not None:
            shortcut_target = _shortcut_target(lnk)
            shortcut_probe = {
                "method_used": "injected_shortcut_target",
                "shllink_error": None,
                "com_rc": None,
                "com_stderr_tail": None,
                "powershell_found": None,
                "com_error": None,
                "idlist_segments": None,
            }
        else:
            shortcut_target, shortcut_probe = _resolve_shortcut_target(
                lnk, expected_target=expected_shortcut_target
            )
    else:
        shortcut_probe = {
            "method_used": "no_shortcut",
            "shllink_error": None,
            "com_rc": None,
            "com_stderr_tail": None,
            "powershell_found": None,
            "com_error": None,
            "idlist_segments": None,
        }

    return {
        "schema": 1,
        "user_path": _path_list(hkcu_env.get("Path")),
        "machine_path": _path_list(hklm_env.get("Path")),
        "py_launcher_path": py_launcher_path,
        "py_0p": _py_0p(py_launcher_path),
        "py_association_progid": py_assoc.get(""),
        "pythoncore_3_13_hkcu_install_path": core_hkcu.get(""),
        "pythoncore_3_13_hklm_install_path": core_hklm.get(""),
        "uninstall_hkcu": _uninstall_scan(_registry, _registry_subkeys, "HKCU"),
        "uninstall_hklm": _uninstall_scan(_registry, _registry_subkeys, "HKLM"),
        "start_menu_shortcut": lnk,
        "start_menu_shortcut_exists": lnk_exists,
        "start_menu_shortcut_target": shortcut_target,
        "shortcut_target_probe": shortcut_probe,
        "runtime_exists": bool(runtime and _exists(runtime)),
        "zealfie_appdata_exists": bool(appdata_root and _exists(appdata_root)),
    }


def _zealfie_entries(uninstall: dict) -> dict:
    return {k: v for k, v in uninstall.items() if "zealfie" in k}


def _cpython_entries(uninstall: dict) -> dict:
    return {k: v for k, v in uninstall.items() if "python" in k}


# ---------------------------------------------------------------------------
# Delta comparison (pure)
# ---------------------------------------------------------------------------


def _host_unchanged_findings(baseline: dict, snapshot: dict) -> list[str]:
    """Host-scope deltas that are FORBIDDEN on both install and uninstall.

    The standalone substrate (python-build-standalone, plain files) must
    leave the host's Python footprint byte-identical: user/machine PATH,
    ``py.exe`` (presence/path and its ``py -0p`` registration list), the
    ``.py`` association, existing ``PythonCore\\3.13`` values (HKCU + HKLM)
    and every CPython Apps&Features/Uninstall entry (HKCU + HKLM).  A
    pre-existing host CPython is fine — it just must not change.
    """
    findings: list[str] = []
    if baseline.get("user_path") != snapshot.get("user_path"):
        findings.append("forbidden: user PATH changed by the installer")
    if baseline.get("machine_path") != snapshot.get("machine_path"):
        findings.append("forbidden: machine PATH changed by the installer")
    base_py = baseline.get("py_launcher_path")
    snap_py = snapshot.get("py_launcher_path")
    if _win_norm(base_py) != _win_norm(snap_py):
        findings.append(
            f"forbidden: py.exe launcher changed: {base_py!r} -> {snap_py!r}"
        )
    if baseline.get("py_0p") != snapshot.get("py_0p"):
        findings.append(
            "forbidden: py launcher registration (py -0p) changed by the "
            "installer"
        )
    base_assoc = baseline.get("py_association_progid")
    snap_assoc = snapshot.get("py_association_progid")
    if base_assoc != snap_assoc:
        findings.append(
            f"forbidden: .py file association changed: {base_assoc!r} -> "
            f"{snap_assoc!r}"
        )
    base_core_hkcu = baseline.get("pythoncore_3_13_hkcu_install_path")
    snap_core_hkcu = snapshot.get("pythoncore_3_13_hkcu_install_path")
    if base_core_hkcu != snap_core_hkcu:
        findings.append(
            "forbidden: user-scoped PythonCore 3.13 registration changed "
            f"({base_core_hkcu!r} -> {snap_core_hkcu!r}) — the standalone "
            "substrate must create NO PythonCore state"
        )
    base_core_hklm = baseline.get("pythoncore_3_13_hklm_install_path")
    snap_core_hklm = snapshot.get("pythoncore_3_13_hklm_install_path")
    if base_core_hklm != snap_core_hklm:
        findings.append(
            "forbidden: machine-scope PythonCore 3.13 registration changed "
            f"({base_core_hklm!r} -> {snap_core_hklm!r}) — the standalone "
            "substrate must create NO PythonCore state"
        )
    base_hkcu_cpython = _cpython_entries(baseline.get("uninstall_hkcu", {}))
    snap_hkcu_cpython = _cpython_entries(snapshot.get("uninstall_hkcu", {}))
    if base_hkcu_cpython != snap_hkcu_cpython:
        findings.append(
            "forbidden: per-user CPython Apps&Features registration changed "
            "(new, removed or altered entry) — the standalone substrate "
            "must create NO provider uninstall state"
        )
    base_hklm_cpython = _cpython_entries(baseline.get("uninstall_hklm", {}))
    snap_hklm_cpython = _cpython_entries(snapshot.get("uninstall_hklm", {}))
    if base_hklm_cpython != snap_hklm_cpython:
        findings.append(
            "forbidden: machine-scope CPython Apps&Features registration "
            "changed (new, removed or altered entry) — the standalone "
            "substrate must create NO provider uninstall state"
        )
    return findings


def verify_install_findings(
    baseline: dict, snapshot: dict, install_root: str
) -> list[str]:
    """Findings after install: forbidden host/provider deltas + shell state."""
    findings: list[str] = []
    private_python = ntpath.join(str(install_root), "python")
    appenv_gui = ntpath.join(
        str(install_root), "appenv", "Scripts", "zealfie-gui.exe"
    )

    # A. Host/provider state must be byte-identical (NO new provider state).
    findings.extend(_host_unchanged_findings(baseline, snapshot))

    # B. EXPECTED ZeAlfie shell
    if not snapshot.get("start_menu_shortcut_exists"):
        findings.append("expected ZeAlfie Start Menu shortcut is missing")
    elif not snapshot.get("start_menu_shortcut_target"):
        probe = json.dumps(snapshot.get("shortcut_target_probe"), sort_keys=True)
        findings.append(
            "Start Menu shortcut target could not be resolved "
            f"(expected {appenv_gui!r}); probe={probe}"
        )
    elif _win_norm(snapshot.get("start_menu_shortcut_target")) != _win_norm(
        appenv_gui
    ):
        probe = json.dumps(snapshot.get("shortcut_target_probe"), sort_keys=True)
        findings.append(
            "Start Menu shortcut target is not the installed windowed "
            f"launcher: {snapshot.get('start_menu_shortcut_target')!r} != "
            f"{appenv_gui!r}; probe={probe}"
        )
    hkcu_zealfie = _zealfie_entries(snapshot.get("uninstall_hkcu", {}))
    if not hkcu_zealfie:
        findings.append("expected per-user ZeAlfie uninstall registration "
                        "is missing")
    hklm_zealfie = _zealfie_entries(snapshot.get("uninstall_hklm", {}))
    if hklm_zealfie:
        findings.append(
            "forbidden: machine-scope ZeAlfie registration present: "
            + ", ".join(sorted(hklm_zealfie))
        )

    return findings


def verify_uninstall_findings(
    baseline: dict, snapshot: dict, install_root: str
) -> list[str]:
    """Findings after uninstall: owned state removed, host state untouched.

    Pure: everything observable (shortcut, registrations, asset/private-
    python/appenv existence, runtime state) must already be IN the snapshot
    dict (the CLI enriches the captured snapshot before comparing).
    """
    findings: list[str] = []

    # A. Host/provider state still byte-identical after uninstall.
    findings.extend(_host_unchanged_findings(baseline, snapshot))

    if snapshot.get("start_menu_shortcut_exists"):
        findings.append(
            "ZeAlfie Start Menu shortcut still present after uninstall"
        )
    if _zealfie_entries(snapshot.get("uninstall_hkcu", {})):
        findings.append(
            "per-user ZeAlfie uninstall registration still present after "
            "uninstall"
        )
    if _zealfie_entries(snapshot.get("uninstall_hklm", {})):
        findings.append(
            "machine-scope ZeAlfie registration still present after "
            "uninstall"
        )
    if snapshot.get("owned_assets"):
        findings.append(
            "installer-owned asset still present after uninstall"
        )
    # The private standalone runtime is installer-owned plain files with NO
    # external registration — it is removed WITH the installer.
    if snapshot.get("private_python_exists"):
        findings.append(
            "private runtime still present after uninstall "
            f"({ntpath.join(str(install_root), 'python', 'python.exe')!r}) — "
            "the python-build-standalone substrate is installer-owned and "
            "must be removed with the installer"
        )
    if snapshot.get("appenv_exists"):
        findings.append(
            "application environment still present after uninstall "
            f"({ntpath.join(str(install_root), 'appenv')!r}) — the appenv is "
            "installer-owned and must be removed with the installer"
        )
    # The whole %LOCALAPPDATA%\zealfie managed app-data tree (managed
    # runtime slots/state/cache, work staging/cache, the persisted
    # desired-products.toml selection and the mutation lock) is
    # ZeAlfie-owned disposable product/runtime state — since
    # ZA-WIN-UNINSTALL-01 the uninstaller removes it WITH the installer
    # (never {localappdata} itself, never any non-ZeAlfie path).
    if snapshot.get("zealfie_appdata_exists"):
        findings.append(
            "%LOCALAPPDATA%\\zealfie still present after uninstall — the "
            "whole ZeAlfie-owned managed app-data tree (managed runtime, "
            "install work, desired-products selection, mutation lock) is "
            "disposable product/runtime state and must be removed with "
            "the installer"
        )
    return findings


def write_audit(out_path: str, audit: dict) -> None:
    """Write a machine-readable audit JSON (with trailing newline)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, indent=2) + "\n")


def _preserve_shortcut_evidence(snapshot: dict, command: str) -> None:
    """Best-effort copy of an UNRESOLVED ``.lnk`` and its probe for the artifact.

    CLI-layer only (never part of the pure comparison core). A failure to copy
    is swallowed so it can never mask the original finding.
    """
    stage = os.environ.get("ZEALFIE_STAGE")
    if not stage:
        return
    if not snapshot.get("start_menu_shortcut_exists"):
        return
    if snapshot.get("start_menu_shortcut_target"):
        return
    lnk = snapshot.get("start_menu_shortcut")
    try:
        outdir = Path(stage) / "setup-output" / "shortcut-evidence"
        outdir.mkdir(parents=True, exist_ok=True)
        if lnk and os.path.isfile(lnk):
            shutil.copy2(lnk, outdir / "ZeAlfie.lnk")
        note = {
            "command": command,
            "shortcut": lnk,
            "probe": snapshot.get("shortcut_target_probe"),
        }
        (outdir / "shortcut-probe.json").write_text(
            json.dumps(note, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — evidence capture must never mask findings
        return


def _emit(audit: dict, label: str) -> int:
    print(f"[side-effect] {label}: {audit['status']}")
    for finding in audit.get("findings", []):
        print(f"[side-effect]   FINDING: {finding}")
    if audit["status"] == "ok":
        print(f"[side-effect] {label} PASS (no findings)")
    return 0 if audit["status"] == "ok" else 1


def _audit(audit: dict) -> dict:
    audit["status"] = "ok" if not audit.get("findings") else "findings"
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python packaging/windows/side_effect_witness.py",
        description="ZeAlfie installer side-effect witness "
                    "(baseline/verify-install/verify-uninstall)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_base = sub.add_parser("baseline", help="capture the machine-scope baseline")
    p_base.add_argument("--out", type=Path, required=True)

    p_inst = sub.add_parser("verify-install",
                            help="delta-audit the installed machine")
    p_inst.add_argument("--baseline", type=Path, required=True)
    p_inst.add_argument("--install-root", type=Path, required=True)
    p_inst.add_argument("--out", type=Path, required=True)

    p_un = sub.add_parser("verify-uninstall",
                          help="delta-audit the machine after uninstall")
    p_un.add_argument("--baseline", type=Path, required=True)
    p_un.add_argument("--install-root", type=Path, required=True)
    p_un.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "baseline":
        snapshot = capture_snapshot()
        snapshot["owned_assets"] = None  # unknown before install
        write_audit(args.out, {"command": "baseline", "status": "ok",
                               "snapshot": snapshot})
        print(f"[side-effect] baseline captured: {args.out}")
        return 0

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    baseline_snap = baseline.get("snapshot", baseline)
    install_root = str(args.install_root)
    appenv_gui = ntpath.join(
        install_root, "appenv", "Scripts", "zealfie-gui.exe"
    )
    snapshot = capture_snapshot(expected_shortcut_target=appenv_gui)
    # Enrich the captured snapshot with installer-relative facts the
    # pure comparison needs (assets + preserved private python).
    owned_ico = ntpath.join(install_root, "assets", "zealfie.ico")
    snapshot["owned_assets"] = _real_isfile(owned_ico)
    snapshot["private_python_exists"] = _real_isfile(
        ntpath.join(install_root, "python", "python.exe")
    )
    snapshot["appenv_exists"] = _real_isfile(
        ntpath.join(install_root, "appenv", "Scripts", "python.exe")
    )

    if args.command == "verify-install":
        findings = verify_install_findings(
            baseline_snap, snapshot, install_root
        )
        if not snapshot.get("start_menu_shortcut_target"):
            _preserve_shortcut_evidence(snapshot, "verify-install")
        audit = _audit({"command": "verify-install", "findings": findings,
                        "install_root": install_root, "snapshot": snapshot})
        write_audit(args.out, audit)
        return _emit(audit, "install side-effect audit")

    findings = verify_uninstall_findings(
        baseline_snap, snapshot, install_root
    )
    audit = _audit({"command": "verify-uninstall", "findings": findings,
                    "install_root": install_root, "snapshot": snapshot})
    write_audit(args.out, audit)
    return _emit(audit, "uninstall side-effect audit")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail closed with evidence
        print(f"[side-effect] FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)
