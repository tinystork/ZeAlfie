"""Architecture sentinels for the ZeAlfie ↔ ZeSeestarStacker (ZSSS) integration.

These tests guard the *generic-chain* boundary, not ZSSS behaviour.  They
exist so a future change cannot quietly reintroduce a product-specific
shortcut that would violate the ZeSoftware interoperability contract
(``ZESOFTWARE_INTEROPERABILITY_RULES.md``):

* Rule 1  — no component depends on ZeAlfie (and ZeAlfie must not become a
  runtime dependency of a managed product).
* Rule 2  — inter-project integration uses public APIs only; no sibling
  checkout, no ``sys.path`` hacks, no private module imports.
* Rule 22 — distribution format does not redefine public behaviour;
  launch happens only through the public entry point.

The assertions are deliberately *structural and cheap*: they scan the
ZeAlfie source tree for the forbidden ``seestar`` package namespace and for
product-specific deployer modules, and they pin the catalog's declared
public contract.  They do **not** reach into the ZSSS repository (a
sibling-checkout assumption would itself violate Rule 2).  The reverse
direction — "ZSSS must not import zealfie" — is a property of the ZSSS
repository and is guarded here only indirectly, via the catalog contract
(no ``zealfie`` extra, no private entry point).
"""

from __future__ import annotations

import re
from pathlib import Path

from zealfie.components.model import EntryPointContract
from zealfie.products.catalog import default_catalog
from zealfie.products.policy import default_product_policy, effective_ref
from zealfie.sources import RemoteSource, resolve_source

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _PROJECT_ROOT / "src" / "zealfie"

# ``import seestar`` / ``from seestar`` / ``import seestar.x`` / ``from
# seestar.x`` — the ZSSS package namespace is private to ZSSS and must never
# be imported by ZeAlfie product code.
_SEESTAR_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+seestar(?:\s+import|\s*\.)|import\s+seestar(?:\s+|$|\.))"
)


def _zealfie_py_files() -> tuple[Path, ...]:
    """Return every ``.py`` file under the packaged ZeAlfie source tree."""
    return tuple(sorted(p for p in _SRC_ROOT.rglob("*.py") if p.is_file()))


def test_zealfie_source_never_imports_seestar() -> None:
    """ZeAlfie product code never imports the ``seestar`` package namespace.

    Rule 2: ZeAlfie must reach ZSSS only through its public entry point
    (``gui_scripts:zeseestarstacker``), never through ZSSS implementation
    modules.  Any ``import seestar`` / ``from seestar`` line in ZeAlfie
    source is a contract violation.
    """
    offenders: list[str] = []
    for py_file in _zealfie_py_files():
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SEESTAR_IMPORT_RE.match(line):
                offenders.append(f"{py_file.relative_to(_PROJECT_ROOT)}:{lineno}")
    assert not offenders, (
        "ZeAlfie source imports the private 'seestar' namespace (Rule 2):\n"
        + "\n".join(offenders)
    )


def test_no_zsss_specific_deployer_or_builder_module() -> None:
    """No product-specific ZSSS deployer/resolver/builder/installer exists.

    The generic chain (discover → resolve → acquire → build → validate →
    install → activate → launch → provenance) must stay product-agnostic.
    A module file named after ``seestar`` / ``zeseestarstacker`` would be a
    product-specific shortcut and a regression of the generic architecture.
    """
    offenders = [
        str(p.relative_to(_PROJECT_ROOT))
        for p in _zealfie_py_files()
        if "seestar" in p.name.lower() or "zeseestarstacker" in p.name.lower()
    ]
    assert not offenders, (
        "product-specific ZSSS module detected (breaks generic chain):\n"
        + "\n".join(offenders)
    )


def test_zeseestarstacker_launch_contract_is_public_gui_script_only() -> None:
    """ZSSS is launched only through the public ``gui_scripts:zeseestarstacker``.

    The catalog must declare exactly one launch entry point, in the
    ``gui_scripts`` group (the public GUI entry point), with no
    ``required_extras`` that could pull in ZeAlfie or a private module.
    """
    desc = default_catalog().get("zeseestarstacker")
    assert desc.distribution_name == "ZeSeestarStacker"
    assert desc.launch_entry_points == (
        EntryPointContract(group="gui_scripts", name="zeseestarstacker"),
    )
    # No required extras: ZSSS must not depend on ZeAlfie or any
    # ZeAlfie-managed extra to launch (Rule 1).
    assert desc.required_extras == ()


def test_zeseestarstacker_declares_no_zealfie_dependency() -> None:
    """ZSSS's catalog contract declares no ZeAlfie dependency (Rule 1)."""
    desc = default_catalog().get("zeseestarstacker")
    assert "zealfie" not in desc.required_extras
    # The remote source is the public tinystork/zeseestarstacker repo —
    # never a ZeAlfie-internal path or a sibling checkout.
    assert isinstance(desc.remote_source, RemoteSource)
    assert desc.remote_source.owner == "tinystork"
    assert desc.remote_source.repo == "zeseestarstacker"


def test_zeseestarstacker_stable_channel_resolves_to_immutable_sha() -> None:
    """Stable channel → requested_ref ``main`` → immutable SHA (Rule 12).

    The default policy (``stable``/``follow``) must map to the requested
    ref ``main``, and :func:`resolve_source` must hand the resolver that
    exact mutable ref and return an immutable 40-hex commit SHA.  This is
    the provenance boundary the acquisition/build steps consume.
    """
    desc = default_catalog().get("zeseestarstacker")
    policy = default_product_policy("zeseestarstacker")
    assert policy.channel == "stable"
    assert policy.policy == "follow"

    # stable -> main (per-product channel authority, not the global default).
    requested_ref = effective_ref(policy, channel_refs=desc.channel_ref_map)
    assert requested_ref == "main"
    assert desc.channel_ref("stable") == "main"

    # Resolve the mutable ref to an immutable SHA; the resolver must be
    # given "main" (never a guessed SHA) and the result is a 40-hex SHA.
    calls: list[tuple[str, str, str]] = []
    fake_sha = "a" * 40

    def _resolver(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        return fake_sha

    resolved = resolve_source(desc.remote_source, resolver=_resolver)
    assert calls == [("tinystork", "zeseestarstacker", "main")]
    assert resolved.source.ref == "main"
    assert resolved.commit_sha == fake_sha
    assert len(resolved.commit_sha) == 40
