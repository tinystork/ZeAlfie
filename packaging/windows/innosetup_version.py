"""Pure Inno Setup version-pin logic (ZA-WIN-BOOT-02, rework-1).

``ISCC.exe /?`` prints ``Inno Setup 6 Command-Line Compiler`` — the patch
level is NOT in the banner, so the banner must never be used to pin the
toolchain.  The authoritative version lives in the compiler's PE version
resource (``FileVersion``/``ProductVersion``, e.g. ``6.7.3`` or
``6.7.3.0``).  This module normalises and verifies those raw values against
the pinned version in ``packaging/windows/installer/innosetup.toml``.

Design rules (mirror ``provision.py`` / ``wheelhouse_lock.py``):

* **pure** — no I/O beyond reading the toolchain pin record;
* **stdlib-only**, no ZeAlfie import, no third-party import;
* **hermetically testable on Linux** — no Windows, no PowerShell, no Inno;
* **fail closed** — a missing/garbage raw version or a mismatch with the
  pinned version raises :class:`InnoVersionError`; the CI job never
  compiles against an unverified compiler.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

__all__ = [
    "InnoVersionError",
    "normalize_innosetup_version",
    "pinned_innosetup_version",
    "verify_innosetup_version",
]

#: Leading X.Y.Z — e.g. "6.7.3", "6.7.3.0", "6.7.3 (whatever)".
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class InnoVersionError(RuntimeError):
    """The compiler version cannot be established or does not match the pin."""


def normalize_innosetup_version(raw: str | None) -> str:
    """Extract the leading ``X.Y.Z`` from a raw FileVersion/ProductVersion.

    Returns ``""`` when no ``X.Y.Z`` is present (``None``, empty, garbage,
    whitespace/CRLF around a valid value are all handled — the regex scans
    the raw string and ``6.7.3.0`` normalises to ``6.7.3``).
    """
    if raw is None:
        return ""
    match = _VERSION_RE.search(str(raw))
    if match is None:
        return ""
    return match.group(0)


def pinned_innosetup_version(path: str | Path | None = None) -> str:
    """Return the pinned Inno Setup version from the toolchain record.

    Default path: ``packaging/windows/installer/innosetup.toml`` (this
    module's parent directory).  Fail closed: a missing record or an empty
    ``[innosetup].version`` raises :class:`InnoVersionError`.
    """
    record_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent / "installer" / "innosetup.toml"
    )
    try:
        with open(record_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise InnoVersionError(
            f"Inno Setup toolchain record not found: {record_path}"
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InnoVersionError(
            f"Inno Setup toolchain record unreadable/invalid: "
            f"{record_path}: {exc}"
        ) from exc
    section = data.get("innosetup")
    if not isinstance(section, dict):
        raise InnoVersionError(
            f"Inno Setup toolchain record missing [innosetup]: {record_path}"
        )
    version = section.get("version")
    if not isinstance(version, str) or not version.strip():
        raise InnoVersionError(
            f"Inno Setup toolchain record has no [innosetup].version: "
            f"{record_path}"
        )
    normalized = normalize_innosetup_version(version)
    if not normalized:
        raise InnoVersionError(
            f"Inno Setup toolchain record [innosetup].version is not an "
            f"X.Y.Z version: {version!r}"
        )
    return normalized


def verify_innosetup_version(
    raw_file_version: str | None,
    raw_product_version: str | None,
    pinned: str,
) -> tuple[str, str]:
    """Verify raw PE version-resource values against the pinned version.

    Fail-closed rules:

    * ``FileVersion`` is tried first, then ``ProductVersion``;
    * a candidate whose normalised form is ``""`` is skipped (absent);
    * a non-empty normalised candidate that differs from the pinned version
      raises :class:`InnoVersionError` (a present-but-wrong compiler is
      rejected, never silently downgraded to the next candidate);
    * if no candidate yields a non-empty normalised value,
      :class:`InnoVersionError` is raised (an unverifiable compiler is
      rejected).

    On success returns ``(normalized, evidence)`` with evidence like
    ``FileVersion='6.7.3'``.
    """
    pinned_norm = normalize_innosetup_version(pinned)
    if not pinned_norm:
        raise InnoVersionError(
            f"pinned Inno Setup version is not an X.Y.Z version: {pinned!r}"
        )
    candidates = (
        ("FileVersion", raw_file_version),
        ("ProductVersion", raw_product_version),
    )
    for label, raw in candidates:
        normalized = normalize_innosetup_version(raw)
        if not normalized:
            continue
        if normalized != pinned_norm:
            raise InnoVersionError(
                f"Inno Setup compiler {label} version {normalized!r} does "
                f"not match the pinned version {pinned_norm!r} "
                "(packaging/windows/installer/innosetup.toml)"
            )
        return normalized, f"{label}='{normalized}'"
    raise InnoVersionError(
        "cannot verify the Inno Setup compiler version: both "
        "FileVersion and ProductVersion are absent/unreadable — refusing "
        "to compile against an unverified compiler"
    )
