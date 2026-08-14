"""Phase 2A — compatibility metadata parser/evaluator tests.

These tests build minimal *fixture wheels* (valid ZIP archives with a
``.dist-info/METADATA`` and an embedded ``zesoftware_interop.json`` package
data file) without executing any product code.  Product names such as
``ZeSolver``/``ZeMosaic`` appear here only as fixture data strings, exactly
as permitted by the mission; the production evaluator stays generic.

Product-name literals in fixture data are intentional and do not leak into
``src/zealfie/compatibility``.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from zealfie.compatibility import (
    CompatibilityVerdict,
    InteropParseStatus,
    evaluate_wheels,
    scan_wheel_interop,
)

# ---------------------------------------------------------------------------
# Fixture-wheel helpers
# ---------------------------------------------------------------------------

_ALL_CAPS = ["near_solve", "blind_solve", "wcs_write", "gpu", "cancel"]


def _provider_json(**overrides: object) -> dict:
    data = {
        "schema": "zesoftware.interop.v1",
        "product_id": "zesolver",
        "distribution_name": "ZeSolver",
        "provides": [
            {
                "api_module": "zesolver.api.v1",
                "api_version": "1.0",
                "capabilities": list(_ALL_CAPS),
            }
        ],
        "consumes": [],
    }
    data.update(overrides)
    return data


def _consumer_json(**overrides: object) -> dict:
    data = {
        "schema": "zesoftware.interop.v1",
        "product_id": "zemosaic",
        "distribution_name": "ZeMosaic",
        "provides": [],
        "consumes": [
            {
                "provider_product_id": "zesolver",
                "provider_distribution_name": "ZeSolver",
                "optional": True,
                "api_module": "zesolver.api.v1",
                "api_version": ">=1,<2",
                "required_capabilities": ["wcs_write"],
                "any_of_capabilities": [
                    {
                        "id": "solve_backend",
                        "capabilities": ["near_solve", "blind_solve"],
                        "required": True,
                    }
                ],
                "optional_capabilities": ["cancel", "gpu"],
            }
        ],
    }
    data.update(overrides)
    return data


def _wheel(
    tmp_path: Path,
    *,
    name: str,
    version: str,
    top_level: str,
    interop_text: str | None = None,
    extra_members: dict[str, str] | None = None,
) -> Path:
    """Build a minimal, valid wheel ZIP without executing any product code."""
    dist_info = f"{name.replace(' ', '_').lower()}-{version}.dist-info"
    wheel_name = f"{name.replace(' ', '_').lower()}-{version}-py3-none-any.whl"
    wheel_path = Path(tmp_path) / wheel_name
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if interop_text is not None:
            zf.writestr(f"{top_level}/zesoftware_interop.json", interop_text)
        for member, content in (extra_members or {}).items():
            zf.writestr(member, content)
    return wheel_path


def _provider_wheel(tmp_path: Path, *, interop: dict | None = None, **kw: object) -> Path:
    if interop is None:
        interop = _provider_json()
    text = json.dumps(interop)
    name = str(kw.pop("name", "ZeSolver"))
    version = str(kw.pop("version", "1.1.0"))
    top_level = str(kw.pop("top_level", "zesolver"))
    return _wheel(tmp_path, name=name, version=version, top_level=top_level, interop_text=text)


def _consumer_wheel(tmp_path: Path, *, interop: dict | None = None, **kw: object) -> Path:
    if interop is None:
        interop = _consumer_json()
    text = json.dumps(interop)
    name = str(kw.pop("name", "ZeMosaic"))
    version = str(kw.pop("version", "4.6.0"))
    top_level = str(kw.pop("top_level", "zemosaic"))
    return _wheel(tmp_path, name=name, version=version, top_level=top_level, interop_text=text)


def _provides(capabilities: list[str], api_module: str = "zesolver.api.v1", api_version: str = "1.0") -> list:
    return [{"api_module": api_module, "api_version": api_version, "capabilities": capabilities}]


def _codes(report) -> set[str]:
    return {f.code for f in report.findings}


# ---------------------------------------------------------------------------
# 1. Compatible pair passes
# ---------------------------------------------------------------------------


def test_compatible_pair_passes(tmp_path: Path) -> None:
    provider = _provider_wheel(tmp_path)
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.COMPATIBLE
    assert report.blocked is False
    assert all(not f.blocking for f in report.findings)


# ---------------------------------------------------------------------------
# 2. Provider API 2.x fails for consumer >=1,<2
# ---------------------------------------------------------------------------


def test_provider_api_2x_is_incompatible(tmp_path: Path) -> None:
    provider = _provider_wheel(
        tmp_path, interop=_provider_json(provides=_provides(_ALL_CAPS, api_version="2.0"))
    )
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.INCOMPATIBLE
    assert report.blocked is True
    assert "API_VERSION_MISMATCH" in _codes(report)


# ---------------------------------------------------------------------------
# 3. API module mismatch fails
# ---------------------------------------------------------------------------


def test_api_module_mismatch_fails(tmp_path: Path) -> None:
    provider = _provider_wheel(
        tmp_path, interop=_provider_json(provides=_provides(_ALL_CAPS, api_module="zesolver.api.v2"))
    )
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.INCOMPATIBLE
    assert "API_MODULE_MISMATCH" in _codes(report)


# ---------------------------------------------------------------------------
# 4. Missing required wcs_write fails
# ---------------------------------------------------------------------------


def test_missing_required_capability_fails(tmp_path: Path) -> None:
    caps = [c for c in _ALL_CAPS if c != "wcs_write"]
    provider = _provider_wheel(tmp_path, interop=_provider_json(provides=_provides(caps)))
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.INCOMPATIBLE
    assert "MISSING_REQUIRED_CAPABILITY" in _codes(report)


# ---------------------------------------------------------------------------
# 5. Missing any-of solve backend (near_solve / blind_solve) fails
# ---------------------------------------------------------------------------


def test_missing_any_of_capability_fails(tmp_path: Path) -> None:
    caps = ["wcs_write", "gpu", "cancel"]  # no near_solve, no blind_solve
    provider = _provider_wheel(tmp_path, interop=_provider_json(provides=_provides(caps)))
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.INCOMPATIBLE
    assert "MISSING_ANY_OF_CAPABILITY" in _codes(report)


# ---------------------------------------------------------------------------
# 6. Missing optional capability (gpu or cancel) passes with degraded diagnostic
# ---------------------------------------------------------------------------


def test_missing_optional_capability_degrades(tmp_path: Path) -> None:
    caps = [c for c in _ALL_CAPS if c != "gpu"]
    provider = _provider_wheel(tmp_path, interop=_provider_json(provides=_provides(caps)))
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED
    assert report.blocked is False
    assert "MISSING_OPTIONAL_CAPABILITY" in _codes(report)


# ---------------------------------------------------------------------------
# 7. Optional provider absent passes with degraded diagnostic
# ---------------------------------------------------------------------------


def test_optional_provider_absent_degrades(tmp_path: Path) -> None:
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([consumer])
    assert report.verdict is CompatibilityVerdict.COMPATIBLE_WITH_DEGRADED
    assert report.blocked is False
    assert "OPTIONAL_PROVIDER_ABSENT" in _codes(report)


# ---------------------------------------------------------------------------
# 8. Mandatory provider absent fails
# ---------------------------------------------------------------------------


def test_mandatory_provider_absent_fails(tmp_path: Path) -> None:
    interop = _consumer_json()
    interop["consumes"][0]["optional"] = False
    consumer = _consumer_wheel(tmp_path, interop=interop)
    report = evaluate_wheels([consumer])
    assert report.verdict is CompatibilityVerdict.INCOMPATIBLE
    assert "MANDATORY_PROVIDER_ABSENT" in _codes(report)


# ---------------------------------------------------------------------------
# 9. Provider present metadata-unavailable referenced by a consumer fails
# ---------------------------------------------------------------------------


def test_provider_metadata_unavailable_referenced_blocks(tmp_path: Path) -> None:
    # Provider wheel carries valid METADATA Name but NO interop declaration.
    provider = _wheel(
        tmp_path, name="ZeSolver", version="1.1.0", top_level="zesolver"
    )
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.METADATA_UNAVAILABLE
    assert report.blocked is True
    assert "PROVIDER_METADATA_UNAVAILABLE" in _codes(report)


# ---------------------------------------------------------------------------
# 10. Provider present metadata-unavailable but unreferenced does not block
# ---------------------------------------------------------------------------


def test_provider_metadata_unavailable_unreferenced_non_blocking(tmp_path: Path) -> None:
    provider = _wheel(
        tmp_path, name="ZeSolver", version="1.1.0", top_level="zesolver"
    )
    report = evaluate_wheels([provider])
    assert report.verdict is CompatibilityVerdict.COMPATIBLE
    assert report.blocked is False
    assert "METADATA_UNAVAILABLE_UNREFERENCED" in _codes(report)
    unreferenced = [f for f in report.findings if f.code == "METADATA_UNAVAILABLE_UNREFERENCED"]
    assert unreferenced and all(not f.blocking for f in unreferenced)


# ---------------------------------------------------------------------------
# 11. Duplicate capability IDs invalid / fail closed when relevant
# ---------------------------------------------------------------------------


def test_duplicate_capability_ids_fail_closed(tmp_path: Path) -> None:
    provider = _provider_wheel(
        tmp_path,
        interop=_provider_json(provides=_provides(["near_solve", "near_solve", "wcs_write"])),
    )
    consumer = _consumer_wheel(tmp_path)
    scanned = scan_wheel_interop(provider)
    assert scanned.status is InteropParseStatus.INVALID
    assert scanned.reason_code == "DUPLICATE_CAPABILITY"
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.METADATA_UNAVAILABLE
    assert report.blocked is True
    assert "PROVIDER_METADATA_UNAVAILABLE" in _codes(report)


# ---------------------------------------------------------------------------
# 12. Duplicate interop files invalid / fail closed when relevant
# ---------------------------------------------------------------------------


def test_duplicate_interop_files_fail_closed(tmp_path: Path) -> None:
    provider = _wheel(
        tmp_path,
        name="ZeSolver",
        version="1.1.0",
        top_level="zesolver",
        interop_text=json.dumps(_provider_json()),
        extra_members={"zepkg/zesoftware_interop.json": json.dumps(_provider_json())},
    )
    consumer = _consumer_wheel(tmp_path)
    scanned = scan_wheel_interop(provider)
    assert scanned.status is InteropParseStatus.INVALID
    assert scanned.reason_code == "DUPLICATE_INTEROP_FILE"
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.METADATA_UNAVAILABLE
    assert report.blocked is True


# ---------------------------------------------------------------------------
# 13. Unknown schema invalid / fail closed when relevant
# ---------------------------------------------------------------------------


def test_unknown_schema_fail_closed(tmp_path: Path) -> None:
    provider = _provider_wheel(
        tmp_path, interop=_provider_json(schema="zesoftware.interop.v2")
    )
    consumer = _consumer_wheel(tmp_path)
    scanned = scan_wheel_interop(provider)
    assert scanned.status is InteropParseStatus.INVALID
    assert scanned.reason_code == "UNKNOWN_SCHEMA"
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.METADATA_UNAVAILABLE
    assert report.blocked is True


# ---------------------------------------------------------------------------
# 14. Distribution-name mismatch invalid / fail closed when relevant
# ---------------------------------------------------------------------------


def test_distribution_name_mismatch_fail_closed(tmp_path: Path) -> None:
    provider = _provider_wheel(
        tmp_path, interop=_provider_json(distribution_name="ZeOther")
    )
    consumer = _consumer_wheel(tmp_path)
    scanned = scan_wheel_interop(provider)
    assert scanned.status is InteropParseStatus.INVALID
    assert scanned.reason_code == "DISTRIBUTION_NAME_MISMATCH"
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.METADATA_UNAVAILABLE
    assert report.blocked is True


# ---------------------------------------------------------------------------
# 15. No product-specific tokens in the production compatibility module
# ---------------------------------------------------------------------------


def test_no_product_tokens_in_compatibility_module() -> None:
    pkg_dir = (
        Path(__file__).resolve().parents[1] / "src" / "zealfie" / "compatibility"
    )
    assert pkg_dir.is_dir()
    for path in sorted(pkg_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in ("zesolver", "zemosaic"):
            assert token not in lowered, f"{path.name} contains forbidden token {token!r}"


# ---------------------------------------------------------------------------
# 16. Duplicate api_module provides: a later fully-satisfying entry wins over
#     an earlier incompatible entry.
# ---------------------------------------------------------------------------


def test_duplicate_api_module_satisfying_entry_wins_over_incompatible(
    tmp_path: Path,
) -> None:
    provides = [
        {"api_module": "zesolver.api.v1", "api_version": "2.0", "capabilities": list(_ALL_CAPS)},
        {"api_module": "zesolver.api.v1", "api_version": "1.0", "capabilities": list(_ALL_CAPS)},
    ]
    provider = _provider_wheel(tmp_path, interop=_provider_json(provides=provides))
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.COMPATIBLE
    assert report.blocked is False
    assert all(not f.blocking for f in report.findings)


# ---------------------------------------------------------------------------
# 17. Duplicate api_module provides: a fully-satisfying entry also wins over
#     an earlier degraded (optional-capability-missing) entry.
# ---------------------------------------------------------------------------


def test_duplicate_api_module_satisfying_entry_wins_over_degraded(
    tmp_path: Path,
) -> None:
    degraded_caps = [c for c in _ALL_CAPS if c != "gpu"]
    provides = [
        {"api_module": "zesolver.api.v1", "api_version": "1.0", "capabilities": degraded_caps},
        {"api_module": "zesolver.api.v1", "api_version": "1.0", "capabilities": list(_ALL_CAPS)},
    ]
    provider = _provider_wheel(tmp_path, interop=_provider_json(provides=provides))
    consumer = _consumer_wheel(tmp_path)
    report = evaluate_wheels([provider, consumer])
    assert report.verdict is CompatibilityVerdict.COMPATIBLE
    assert report.blocked is False
    assert all(not f.blocking for f in report.findings)
    assert "MISSING_OPTIONAL_CAPABILITY" not in _codes(report)
