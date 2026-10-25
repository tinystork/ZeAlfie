"""Fail-closed PE architecture audit for the installed ZeAlfie payload.

The Inno Setup bootstrap/uninstaller is deliberately outside this audit: Inno
Setup 6.7.3's Setup program is x86 and runs under Windows ARM emulation.

Claim proven here (three tiers, fail-closed):

1. HARD requirement — every RUNTIME-CRITICAL image (the private CPython
   executables/DLL and the Qt/PySide native libraries ZeAlfie actually loads)
   must match the selected architecture exactly. ``IMAGE_FILE_MACHINE_ARM64EC``
   (0x01C4, ARM64-compatible hybrid) is accepted ONLY when the expected target
   is ARM64, is counted separately, and is never accepted for AMD64.
2. EXPLICIT allowlist — a narrow, evidence-documented set of NON-runtime
   multi-architecture images ships inside universal wheels or the immutable
   python-build-standalone pin: pip's vendored distlib launcher stubs,
   setuptools' launcher stubs and the upstream x64 ``vcruntime140_1.dll``. Each
   allowed image is reported in ``allowed_non_native`` with its path, machine
   and reason, and counted. There are no tree-wide wildcards and site-packages
   is never blanket-ignored.
3. Anything else that is not native remains a hard failure with the same
   precise diagnostic style.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

_MACHINE_NAMES = {
    0x014C: "I386",
    0x8664: "AMD64",
    0xAA64: "ARM64",
    0x01C4: "ARM64EC",
}
_EXPECTED = {"AMD64": 0x8664, "ARM64": 0xAA64}
_PE_SUFFIXES = frozenset({".exe", ".dll", ".pyd"})

#: Hybrid ARM64EC images run natively on ARM64 only.
_ARM64_COMPATIBLE = frozenset({"ARM64", "ARM64EC"})

#: Tier 1: images whose architecture is a hard, unconditional requirement.
_RUNTIME_IMAGES = (
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
)
_RUNTIME_SET = frozenset(_RUNTIME_IMAGES)

#: Tier 2: exact-path allowances. The machine must be in the listed set.
_ALLOW_EXACT = {
    "python/vcruntime140_1.dll": (
        frozenset({"AMD64"}),
        "upstream python-build-standalone x64 exception-handling runtime; "
        "immutable hash-verified substrate pin, not loaded by ARM64 CPython",
    ),
}

#: Tier 2: exact directories + exact launcher filenames (no tree wildcards).
_DISTLIB_STUBS = frozenset(
    {"t32.exe", "t64.exe", "t64-arm.exe", "w32.exe", "w64.exe", "w64-arm.exe"}
)
_SETUPTOOLS_STUBS = frozenset(
    {
        "cli.exe",
        "cli-32.exe",
        "cli-64.exe",
        "cli-arm64.exe",
        "gui.exe",
        "gui-32.exe",
        "gui-64.exe",
        "gui-arm64.exe",
    }
)
_DISTLIB_REASON = (
    "pip vendored distlib multi-architecture console/window launcher stub; "
    "shipped inside the universal pip wheel and never imported by ZeAlfie"
)
_SETUPTOOLS_REASON = (
    "setuptools multi-architecture CLI/GUI launcher stub; shipped inside the "
    "universal setuptools wheel and never imported by ZeAlfie"
)
_ALLOW_STUB_DIRS = {
    "python/Lib/site-packages/pip/_vendor/distlib": (_DISTLIB_STUBS, _DISTLIB_REASON),
    "appenv/Lib/site-packages/pip/_vendor/distlib": (_DISTLIB_STUBS, _DISTLIB_REASON),
    "python/Lib/site-packages/setuptools": (_SETUPTOOLS_STUBS, _SETUPTOOLS_REASON),
    "appenv/Lib/site-packages/setuptools": (_SETUPTOOLS_STUBS, _SETUPTOOLS_REASON),
}


class PeAuditError(RuntimeError):
    """The installed payload cannot support the requested native claim."""


def pe_machine(path: Path) -> int:
    """Read and return IMAGE_FILE_HEADER.Machine from *path*."""
    try:
        with path.open("rb") as fh:
            if fh.read(2) != b"MZ":
                raise PeAuditError(f"not a PE image (missing MZ): {path}")
            fh.seek(0x3C)
            raw = fh.read(4)
            if len(raw) != 4:
                raise PeAuditError(f"truncated DOS header: {path}")
            pe_offset = struct.unpack("<I", raw)[0]
            fh.seek(pe_offset)
            if fh.read(4) != b"PE\0\0":
                raise PeAuditError(f"invalid PE signature: {path}")
            raw = fh.read(2)
            if len(raw) != 2:
                raise PeAuditError(f"truncated COFF header: {path}")
            return struct.unpack("<H", raw)[0]
    except OSError as exc:
        raise PeAuditError(f"cannot inspect PE image {path}: {exc}") from exc


def iter_payload_images(install_root: Path):
    """Yield every architecture-sensitive PE under private Python + appenv."""
    for subtree in (install_root / "python", install_root / "appenv"):
        if not subtree.is_dir():
            raise PeAuditError(f"installed payload subtree missing: {subtree}")
        for path in sorted(subtree.rglob("*")):
            if path.is_file() and path.suffix.lower() in _PE_SUFFIXES:
                yield path


def _is_native(machine_name: str, expected_name: str) -> bool:
    """True when *machine_name* satisfies the *expected_name* native claim."""
    if machine_name == expected_name:
        return True
    return expected_name == "ARM64" and machine_name in _ARM64_COMPATIBLE


def _allowance(relative_path: str, machine_name: str) -> str | None:
    """Return the documented reason when this image is explicitly allowed."""
    exact = _ALLOW_EXACT.get(relative_path)
    if exact is not None:
        machines, reason = exact
        return reason if machine_name in machines else None
    parent, _, filename = relative_path.rpartition("/")
    rule = _ALLOW_STUB_DIRS.get(parent)
    if rule is not None:
        filenames, reason = rule
        if filename in filenames:
            return reason
    return None


def audit_payload(install_root: Path, expected_machine: str) -> dict:
    """Audit the installed payload against the selected native architecture.

    Raises :class:`PeAuditError` for A cross-architecture runtime-critical image,
    a cross-architecture image outside the explicit allowlist, an allowlisted
    image with an unreadable/unknown machine, or any missing required image.
    """
    expected_name = str(expected_machine).strip().upper()
    expected_value = _EXPECTED.get(expected_name)
    if expected_value is None:
        raise PeAuditError(
            f"unsupported expected PE machine {expected_machine!r}; "
            f"expected one of {sorted(_EXPECTED)}"
        )
    root = Path(install_root).resolve()
    required = tuple(root / relative for relative in _RUNTIME_IMAGES)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise PeAuditError("required native payload image(s) missing: " + ", ".join(missing))

    counts: dict[str, int] = {}
    runtime_checked: list[str] = []
    runtime_mismatches: list[dict] = []
    allowed: list[dict] = []
    mismatches: list[dict] = []
    images = list(iter_payload_images(root))
    if not images:
        raise PeAuditError("no PE images found in installed Python/appenv payload")
    for path in images:
        relative = path.relative_to(root).as_posix()
        machine = pe_machine(path)
        name = _MACHINE_NAMES.get(machine, f"UNKNOWN_0x{machine:04X}")
        counts[name] = counts.get(name, 0) + 1
        record = {
            "path": relative,
            "machine": name,
            "machine_value": f"0x{machine:04X}",
        }
        if relative in _RUNTIME_SET:
            runtime_checked.append(relative)
            if not _is_native(name, expected_name):
                runtime_mismatches.append(record)
            continue
        if _is_native(name, expected_name):
            continue
        reason = _allowance(relative, name)
        if reason is not None and not name.startswith("UNKNOWN_"):
            record["reason"] = reason
            allowed.append(record)
            continue
        mismatches.append(record)

    result = {
        "schema": "zealfie-windows-native-payload-audit-v2",
        "scope": ["python", "appenv"],
        "inno_setup_loader_in_scope": False,
        "expected_machine": expected_name,
        "arm64ec_is_native_compatible": expected_name == "ARM64",
        "images_checked": len(images),
        "runtime_images_checked": len(runtime_checked),
        "runtime_images": sorted(runtime_checked),
        "machine_counts": counts,
        "arm64ec_count": counts.get("ARM64EC", 0),
        "allowed_non_native": allowed,
        "allowed_count": len(allowed),
        "runtime_mismatches": runtime_mismatches,
        "mismatches": mismatches,
        "status": (
            "PASS" if not runtime_mismatches and not mismatches else "FAIL"
        ),
    }
    if runtime_mismatches:
        sample = "; ".join(
            f"{item['path']}={item['machine']}" for item in runtime_mismatches[:12]
        )
        raise PeAuditError(
            f"runtime-critical native payload image(s) are not {expected_name}: "
            f"{sample}"
        )
    if mismatches:
        sample = "; ".join(
            f"{item['path']}={item['machine']}" for item in mismatches[:12]
        )
        raise PeAuditError(
            f"native payload contains {len(mismatches)} non-{expected_name} "
            f"PE image(s): {sample}"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit installed Windows PE payload architecture")
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--expected-machine", required=True, choices=sorted(_EXPECTED))
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit_payload(args.install_root, args.expected_machine)
    except PeAuditError as exc:
        print(f"[pe-audit] FAIL: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
