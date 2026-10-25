"""Fail-closed PE architecture audit for the installed ZeAlfie payload.

The Inno Setup bootstrap/uninstaller is deliberately outside this audit: Inno
Setup 6.7.3's Setup program is x86 and runs under Windows ARM emulation.  The
claim proven here is narrower and useful: the installed private CPython,
application venv and Qt/PySide native payload are all the architecture selected
by the committed reproducibility profile, with no opposite-architecture PE
residue in those two trees.
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
}
_EXPECTED = {"AMD64": 0x8664, "ARM64": 0xAA64}
_PE_SUFFIXES = frozenset({".exe", ".dll", ".pyd"})


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


def audit_payload(install_root: Path, expected_machine: str) -> dict:
    """Require every audited image to match AMD64 or ARM64 exactly."""
    expected_name = str(expected_machine).strip().upper()
    expected_value = _EXPECTED.get(expected_name)
    if expected_value is None:
        raise PeAuditError(
            f"unsupported expected PE machine {expected_machine!r}; "
            f"expected one of {sorted(_EXPECTED)}"
        )
    root = Path(install_root).resolve()
    required = (
        root / "python" / "python.exe",
        root / "python" / "pythonw.exe",
        root / "appenv" / "Scripts" / "python.exe",
        root / "appenv" / "Scripts" / "pythonw.exe",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise PeAuditError("required native payload image(s) missing: " + ", ".join(missing))

    counts: dict[str, int] = {}
    mismatches: list[dict] = []
    images = list(iter_payload_images(root))
    if not images:
        raise PeAuditError("no PE images found in installed Python/appenv payload")
    for path in images:
        machine = pe_machine(path)
        name = _MACHINE_NAMES.get(machine, f"UNKNOWN_0x{machine:04X}")
        counts[name] = counts.get(name, 0) + 1
        if machine != expected_value:
            mismatches.append({
                "path": str(path.relative_to(root)),
                "machine": name,
                "machine_value": f"0x{machine:04X}",
            })
    result = {
        "schema": "zealfie-windows-native-payload-audit-v1",
        "scope": ["python", "appenv"],
        "inno_setup_loader_in_scope": False,
        "expected_machine": expected_name,
        "images_checked": len(images),
        "machine_counts": counts,
        "mismatches": mismatches,
        "status": "PASS" if not mismatches else "FAIL",
    }
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
