"""Tests for the M1-2H accelerated variant catalog.

Covers the frozen :class:`AcceleratedVariant` model, the fail-closed
:class:`AcceleratedVariantCatalog` (duplicate keys rejected, ambiguous
matches raise :class:`AmbiguousVariantError`, empty catalog resolves to
``None``) and the empty :func:`default_variant_catalog` default.

Synthetic distribution names (``accelerated-lib``, ``fake-cuda``) are
used everywhere — ZeAlfie never selects a concrete accelerated
framework.
"""

from __future__ import annotations

import pytest

from zealfie.acceleration import (
    AcceleratedVariant,
    AcceleratedVariantCatalog,
    AmbiguousVariantError,
    default_variant_catalog,
)


def variant(
    distribution: str,
    version: str = "1.0.0",
    backend: str = "NVIDIA_CUDA",
    platform: str | None = None,
    sha256: str | None = None,
) -> AcceleratedVariant:
    return AcceleratedVariant(
        distribution=distribution,
        version=version,
        backend=backend,
        platform=platform,
        sha256=sha256,
    )


# ===========================================================================
# AcceleratedVariant model
# ===========================================================================


def test_variant_canonicalizes_distribution():
    """Distribution names are canonicalized (PEP 503)."""
    v = variant("Accelerated_Lib")
    assert v.distribution == "accelerated-lib"


def test_variant_defaults_platform_and_sha256_to_none():
    """platform and sha256 default to None."""
    v = variant("accelerated-lib")
    assert v.platform is None
    assert v.sha256 is None


def test_variant_is_frozen_and_hashable():
    """AcceleratedVariant is a frozen, hashable value object."""
    v = variant("accelerated-lib")
    assert hash(v) == hash(variant("accelerated-lib"))
    with pytest.raises(Exception):
        v.version = "2.0.0"  # type: ignore


def test_variant_rejects_empty_distribution():
    """Empty distribution names are rejected."""
    with pytest.raises(ValueError, match="distribution"):
        variant("  ")


def test_variant_rejects_empty_version():
    """Empty versions are rejected — variants are exact artifacts."""
    with pytest.raises(ValueError, match="version"):
        variant("accelerated-lib", version="")


def test_variant_rejects_unknown_backend():
    """Backends outside KNOWN_BACKENDS are rejected fail-closed."""
    with pytest.raises(ValueError, match="unsupported acceleration backend"):
        variant("accelerated-lib", backend="AMD_ROCM")


def test_variant_rejects_empty_backend():
    """Empty backends are rejected."""
    with pytest.raises(ValueError, match="backend"):
        variant("accelerated-lib", backend="  ")


def test_variant_rejects_empty_platform():
    """An empty platform string is rejected (use None instead)."""
    with pytest.raises(ValueError, match="platform"):
        variant("accelerated-lib", platform=" ")


def test_variant_rejects_empty_sha256():
    """An empty sha256 string is rejected (use None instead)."""
    with pytest.raises(ValueError, match="sha256"):
        variant("accelerated-lib", sha256="  ")


# ===========================================================================
# AcceleratedVariantCatalog model
# ===========================================================================


def test_catalog_rejects_duplicate_key():
    """Two variants with the same (distribution, backend, platform) key
    are rejected."""
    with pytest.raises(ValueError, match="duplicate variant"):
        AcceleratedVariantCatalog(
            variants=(
                variant("accelerated-lib", version="1.0.0"),
                variant("accelerated-lib", version="2.0.0"),
            )
        )


def test_catalog_duplicate_key_detected_after_canonicalization():
    """Duplicate keys are detected after distribution canonicalization."""
    with pytest.raises(ValueError, match="duplicate variant"):
        AcceleratedVariantCatalog(
            variants=(
                variant("Accelerated_Lib"),
                variant("accelerated-lib"),
            )
        )


def test_catalog_none_platform_is_distinct_key():
    """A platform-independent variant and a platform-tagged variant of
    the same distribution can coexist."""
    catalog = AcceleratedVariantCatalog(
        variants=(
            variant("accelerated-lib", version="1.0.0", platform=None),
            variant("accelerated-lib", version="1.0.0", platform="linux_x86_64"),
        )
    )
    assert len(catalog.variants) == 2


def test_catalog_rejects_non_variant_values():
    """Garbage entries are rejected fail-closed."""
    with pytest.raises(ValueError, match="AcceleratedVariant"):
        AcceleratedVariantCatalog(variants=("accelerated-lib",))  # type: ignore


# ===========================================================================
# find_variant lookups
# ===========================================================================


def test_find_variant_exact_match():
    """An exact (distribution, backend, platform) match is returned."""
    expected = variant("accelerated-lib", version="1.0.0", platform="linux_x86_64")
    catalog = AcceleratedVariantCatalog(variants=(expected,))
    found = catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64")
    assert found is expected


def test_find_variant_platform_independent_matches_any_platform():
    """A platform=None variant matches any platform tag."""
    expected = variant("accelerated-lib")
    catalog = AcceleratedVariantCatalog(variants=(expected,))
    found = catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64")
    assert found is expected


def test_find_variant_platform_mismatch_returns_none():
    """A variant tagged for another platform does not match."""
    catalog = AcceleratedVariantCatalog(
        variants=(variant("accelerated-lib", platform="windows_x86_64"),)
    )
    found = catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64")
    assert found is None


def test_find_variant_unknown_distribution_returns_none():
    """A distribution absent from the catalog resolves to None."""
    catalog = AcceleratedVariantCatalog(variants=(variant("other-lib"),))
    found = catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64")
    assert found is None


def test_find_variant_wrong_backend_returns_none():
    """A backend mismatch resolves to None (never guessed)."""
    catalog = AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),))
    found = catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64")
    assert found is not None
    assert (
        catalog.find_variant("accelerated-lib", "OTHER_BACKEND", "linux_x86_64")
        is None
    )


def test_find_variant_ambiguous_raises():
    """A platform-independent variant plus a platform-tagged variant of
    the same distribution raises AmbiguousVariantError — never picked
    arbitrarily."""
    catalog = AcceleratedVariantCatalog(
        variants=(
            variant("accelerated-lib", version="1.0.0", platform=None),
            variant("accelerated-lib", version="2.0.0", platform="linux_x86_64"),
        )
    )
    with pytest.raises(AmbiguousVariantError, match="multiple accelerated variants"):
        catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64")


def test_find_variant_canonicalizes_query_distribution():
    """The query distribution is canonicalized before matching."""
    expected = variant("accelerated-lib")
    catalog = AcceleratedVariantCatalog(variants=(expected,))
    assert catalog.find_variant("Accelerated_Lib", "NVIDIA_CUDA", "linux_x86_64") is expected


def test_find_variant_rejects_empty_distribution():
    """Empty query distribution is rejected fail-closed."""
    catalog = AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),))
    with pytest.raises(ValueError, match="distribution"):
        catalog.find_variant(" ", "NVIDIA_CUDA", "linux_x86_64")


def test_find_variant_rejects_empty_platform_tag():
    """Empty platform tags are rejected fail-closed."""
    catalog = AcceleratedVariantCatalog(variants=(variant("accelerated-lib"),))
    with pytest.raises(ValueError, match="platform_tag"):
        catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "  ")


# ===========================================================================
# default_variant_catalog
# ===========================================================================


def test_default_variant_catalog_is_empty():
    """The fail-closed default is the empty catalog."""
    catalog = default_variant_catalog()
    assert isinstance(catalog, AcceleratedVariantCatalog)
    assert catalog.variants == ()
    assert catalog.find_variant("accelerated-lib", "NVIDIA_CUDA", "linux_x86_64") is None
