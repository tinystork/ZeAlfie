"""ZeAlfie Windows installer — non-invasive side-effect witness (ZA-WIN-BOOT-02, closure).

Baseline → post-install → post-uninstall DELTA audit of the machine-scope
footprint the installer is allowed to touch.  The installer's contract:

* FORBIDDEN pollution: user/machine PATH unchanged, no new ``py.exe``
  launcher, no new/changed ``.py`` file association, no machine-scope
  Python/ZeAlfie registration.
* EXPECTED provider state (python.org per-user installer, documented
  platform-provider limitation): a USER-scoped ``PythonCore\\3.13``
  registration resolving to ``{app}\\python`` and a user-scoped CPython
  Apps&Features/Uninstall entry pointing into that private install.
* EXPECTED ZeAlfie shell: Start Menu shortcut targeting
  ``{app}\\appenv\\Scripts\\zealfie-gui.exe`` and a per-user ZeAlfie
  uninstall registration.
* UNINSTALL: shortcut + installer-owned registration/assets removed; the
  nested private CPython/provider state intentionally preserved;
  ``%LOCALAPPDATA%\\zealfie\\runtime`` never touched.

Baseline-delta only: nothing here asserts "Python must not exist" (the
runner may already have Python) — only that the installer changed nothing
outside its documented footprint.

Design rules (mirror the other ``packaging/windows`` modules):

* **pure comparison core** — snapshot capture and delta comparison are
  separate functions with injectable seams (``_registry``,
  ``_registry_subkeys``, ``_which``, ``_isfile``, ``_exists``, ``_env``,
  ``_shortcut_target``) so the logic is hermetically unit-testable on
  Linux without ``winreg``/Windows (``winreg`` is imported lazily inside
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
RUNTIME_REL = os.path.join("zealfie", "runtime")


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


def _real_isfile(path: str) -> bool:
    return os.path.isfile(path)


def _real_exists(path: str) -> bool:
    return os.path.exists(path)


def _real_env(name: str) -> str | None:
    return os.environ.get(name)


def _real_shortcut_target(path: str) -> str | None:
    """Resolve a .lnk target via the WScript.Shell COM object (Windows)."""
    if sys.platform != "win32":
        return None
    escaped = str(path).replace("'", "''")
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
        + escaped + "'); $s.TargetPath"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    target = proc.stdout.strip()
    return target or None


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
    _registry=_real_registry,
    _registry_subkeys=_real_registry_subkeys,
    _which=_real_which,
    _isfile=_real_isfile,
    _exists=_real_exists,
    _env=_real_env,
    _shortcut_target=_real_shortcut_target,
) -> dict:
    """Capture the machine-scope footprint relevant to the installer."""
    hkcu_env = _registry("HKCU", ENV_HKCU_SUBKEY) or {}
    hklm_env = _registry("HKLM", ENV_HKLM_SUBKEY) or {}
    py_assoc = _registry("HKCU", ASSOC_PY_SUBKEY) or {}
    core_hkcu = _registry("HKCU", PYTHONCORE_SUBKEY) or {}
    core_hklm = _registry("HKLM", PYTHONCORE_SUBKEY) or {}

    lnk = _start_menu_lnk(_env)
    local = _env("LOCALAPPDATA")
    runtime = os.path.join(local, RUNTIME_REL) if local else None

    return {
        "schema": 1,
        "user_path": _path_list(hkcu_env.get("Path")),
        "machine_path": _path_list(hklm_env.get("Path")),
        "py_launcher_path": _which("py.exe"),
        "py_association_progid": py_assoc.get(""),
        "pythoncore_3_13_hkcu_install_path": core_hkcu.get(""),
        "pythoncore_3_13_hklm_install_path": core_hklm.get(""),
        "uninstall_hkcu": _uninstall_scan(_registry, _registry_subkeys, "HKCU"),
        "uninstall_hklm": _uninstall_scan(_registry, _registry_subkeys, "HKLM"),
        "start_menu_shortcut": lnk,
        "start_menu_shortcut_exists": bool(lnk and _isfile(lnk)),
        "start_menu_shortcut_target": (
            _shortcut_target(lnk) if lnk and _isfile(lnk) else None
        ),
        "runtime_exists": bool(runtime and _exists(runtime)),
    }


def _entry_under(entry: dict | None, root: str) -> bool:
    """True when a uninstall entry's location/string points under *root*."""
    if not entry:
        return False
    want = _win_norm(root)
    for key in ("install_location", "uninstall_string"):
        value = entry.get(key)
        if value and _win_norm(value).startswith(want):
            return True
    return False


def _zealfie_entries(uninstall: dict) -> dict:
    return {k: v for k, v in uninstall.items() if "zealfie" in k}


def _cpython_entries(uninstall: dict) -> dict:
    return {k: v for k, v in uninstall.items() if "python" in k}


# ---------------------------------------------------------------------------
# Delta comparison (pure)
# ---------------------------------------------------------------------------


def verify_install_findings(
    baseline: dict, snapshot: dict, install_root: str
) -> list[str]:
    """Findings after install: forbidden pollution + expected state."""
    findings: list[str] = []
    private_python = ntpath.join(str(install_root), "python")
    appenv_gui = ntpath.join(
        str(install_root), "appenv", "Scripts", "zealfie-gui.exe"
    )

    # A. FORBIDDEN pollution (baseline deltas only)
    if baseline.get("user_path") != snapshot.get("user_path"):
        findings.append("forbidden: user PATH changed by the installer")
    if baseline.get("machine_path") != snapshot.get("machine_path"):
        findings.append("forbidden: machine PATH changed by the installer")
    base_py = baseline.get("py_launcher_path")
    snap_py = snapshot.get("py_launcher_path")
    if base_py is None and snap_py is not None:
        findings.append(
            "forbidden: new py.exe launcher appeared: " + str(snap_py)
        )
    elif base_py is not None and _win_norm(base_py) != _win_norm(snap_py):
        findings.append(
            f"forbidden: py.exe launcher changed: {base_py!r} -> {snap_py!r}"
        )
    base_assoc = baseline.get("py_association_progid")
    snap_assoc = snapshot.get("py_association_progid")
    if base_assoc is None and snap_assoc is not None:
        findings.append(
            "forbidden: new .py file association appeared: "
            + repr(snap_assoc)
        )
    elif base_assoc is not None and snap_assoc != base_assoc:
        findings.append(
            f"forbidden: .py file association changed: {base_assoc!r} -> "
            f"{snap_assoc!r}"
        )

    # B. EXPECTED provider state (user-scoped CPython pointing at {app}\python)
    core_hkcu = snapshot.get("pythoncore_3_13_hkcu_install_path")
    if core_hkcu is None:
        findings.append(
            "expected user-scoped PythonCore 3.13 registration is missing"
        )
    elif _win_norm(core_hkcu) != _win_norm(private_python):
        findings.append(
            "PythonCore 3.13 InstallPath does not resolve to the private "
            f"install: {core_hkcu!r} != {private_python!r}"
        )
    base_core_hklm = baseline.get("pythoncore_3_13_hklm_install_path")
    snap_core_hklm = snapshot.get("pythoncore_3_13_hklm_install_path")
    if base_core_hklm is None and snap_core_hklm is not None:
        findings.append(
            "forbidden: NEW machine-scope PythonCore 3.13 registration: "
            + repr(snap_core_hklm)
        )
    hkcu_cpython = _cpython_entries(snapshot.get("uninstall_hkcu", {}))
    if not any(
        _entry_under(entry, private_python) for entry in hkcu_cpython.values()
    ):
        findings.append(
            "expected per-user CPython Apps&Features entry pointing into "
            "the private install is missing"
        )
    hklm_cpython = _cpython_entries(snapshot.get("uninstall_hklm", {}))
    machine_pollution = [
        name for name, entry in hklm_cpython.items()
        if _entry_under(entry, private_python)
    ]
    if machine_pollution:
        findings.append(
            "forbidden: machine-scope CPython registration points into the "
            "private install: " + ", ".join(sorted(machine_pollution))
        )

    # C. EXPECTED ZeAlfie shell
    if not snapshot.get("start_menu_shortcut_exists"):
        findings.append("expected ZeAlfie Start Menu shortcut is missing")
    elif _win_norm(snapshot.get("start_menu_shortcut_target")) != _win_norm(
        appenv_gui
    ):
        findings.append(
            "Start Menu shortcut target is not the installed windowed "
            f"launcher: {snapshot.get('start_menu_shortcut_target')!r} != "
            f"{appenv_gui!r}"
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
    """Findings after uninstall: owned state removed, provider state kept.

    Pure: everything observable (shortcut, registrations, asset/private-
    python existence, runtime state) must already be IN the snapshot dict
    (the CLI enriches the captured snapshot before comparing).
    """
    findings: list[str] = []
    private_python_exe = ntpath.join(
        str(install_root), "python", "python.exe"
    )

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
    if not snapshot.get("private_python_exists"):
        findings.append(
            f"private CPython unexpectedly removed by the uninstaller "
            f"(documented platform-provider state {private_python_exe!r} "
            f"must be preserved)"
        )
    if baseline.get("runtime_exists") != snapshot.get("runtime_exists"):
        findings.append(
            "%LOCALAPPDATA%\\zealfie\\runtime existence changed "
            f"({baseline.get('runtime_exists')} -> "
            f"{snapshot.get('runtime_exists')}) — it must never be touched"
        )
    return findings


def write_audit(out_path: str, audit: dict) -> None:
    """Write a machine-readable audit JSON (with trailing newline)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, indent=2) + "\n")


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
    snapshot = capture_snapshot()
    install_root = str(args.install_root)
    # Enrich the captured snapshot with installer-relative facts the
    # pure comparison needs (assets + preserved private python).
    owned_ico = ntpath.join(install_root, "assets", "zealfie.ico")
    snapshot["owned_assets"] = _real_isfile(owned_ico)
    snapshot["private_python_exists"] = _real_isfile(
        ntpath.join(install_root, "python", "python.exe")
    )

    if args.command == "verify-install":
        findings = verify_install_findings(
            baseline_snap, snapshot, install_root
        )
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
