"""Tests for M1-2H acceleration requirement contract.

Covers the frozen model validation (``zealfie.acceleration.models``)
and the strict, fail-closed parsing of the optional
``[products.acceleration]`` table in the product catalog.

Synthetic distribution names (``accelerated-lib``, ``fake-cuda``) are
used everywhere — ZeAlfie never selects a concrete accelerated
framework, so no real framework name appears in these tests as a
selection decision.
"""

from __future__ import annotations

import pytest
from packaging.specifiers import SpecifierSet

from zealfie.acceleration.models import (
    AcceleratedRequirement,
    AccelerationIncompatibility,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    KNOWN_BACKENDS,
    ProductAccelerationRequirements,
)
from zealfie.components.model import EntryPointContract
from zealfie.products.catalog import (
    InvalidCatalogError,
    ProductDescriptor,
    default_catalog,
    load_catalog_from_text,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _catalog_with_acceleration(acceleration_block: str) -> str:
    """Build a minimal valid catalog with one product and an acceleration
    table from *acceleration_block* (the TOML inside the table)."""
    return (
        "schema_version = 1\n\n"
        "[[products]]\n"
        'id = "zebench"\n'
        'display_name = "ZeBench"\n'
        'distribution_name = "ZeBench"\n'
        "[products.launch]\n"
        'entry_points = [{group = "gui_scripts", name = "zebench"}]\n'
        "\n"
        "[products.acceleration]\n"
        f"{acceleration_block}\n"
    )


VALID_ACCELERATION_BLOCK = """\
backend = "NVIDIA_CUDA"

[[products.acceleration.requirements]]
distribution = "Accelerated_Lib"
specifier = ">=1.0,<3"
extras = ["cuda12", "gui"]

[[products.acceleration.incompatibilities]]
distribution = "fake-cuda"
reason = "ships its own kernels"
"""


def _descriptor(acceleration=None):
    return ProductDescriptor(
        product_id="zebench",
        display_name="ZeBench",
        distribution_name="ZeBench",
        launch_entry_points=(EntryPointContract("gui_scripts", "zebench"),),
        acceleration=acceleration,
    )


# ===========================================================================
# AcceleratedRequirement model
# ===========================================================================


def test_requirement_canonicalizes_distribution():
    """Distribution names are canonicalized (PEP 503)."""
    req = AcceleratedRequirement(distribution="Accelerated_Lib")
    assert req.distribution == "accelerated-lib"


def test_requirement_defaults_specifier_none_extras_empty():
    """specifier defaults to None, extras to ()."""
    req = AcceleratedRequirement(distribution="accelerated-lib")
    assert req.specifier is None
    assert req.extras == ()


def test_requirement_valid_specifier_parses():
    """A valid specifier string is kept and parses as a SpecifierSet."""
    req = AcceleratedRequirement(
        distribution="accelerated-lib", specifier=">=1.0,<3"
    )
    assert req.specifier == ">=1.0,<3"
    assert SpecifierSet(req.specifier)


def test_requirement_rejects_invalid_specifier():
    """A specifier that does not parse is rejected fail-closed."""
    with pytest.raises(ValueError, match="invalid specifier"):
        AcceleratedRequirement(distribution="accelerated-lib", specifier="not-a-version")


def test_requirement_rejects_empty_specifier():
    """An empty specifier string is rejected (never silently "any")."""
    with pytest.raises(ValueError, match="specifier"):
        AcceleratedRequirement(distribution="accelerated-lib", specifier="  ")


def test_requirement_rejects_empty_distribution():
    """Empty distribution names are rejected."""
    with pytest.raises(ValueError, match="distribution"):
        AcceleratedRequirement(distribution="")


def test_requirement_extras_canonicalized_and_sorted():
    """Extras are canonicalized and sorted."""
    req = AcceleratedRequirement(
        distribution="accelerated-lib",
        extras=("Gui", "cuda12", "cuda12_x"),
    )
    assert req.extras == ("cuda12", "cuda12-x", "gui")


def test_requirement_rejects_duplicate_extras():
    """Duplicate extras (after canonicalization) are rejected."""
    with pytest.raises(ValueError, match="duplicate extra"):
        AcceleratedRequirement(
            distribution="accelerated-lib",
            extras=("Gui", "gui"),
        )


def test_requirement_is_frozen_and_hashable():
    """AcceleratedRequirement is a frozen, hashable value object."""
    req = AcceleratedRequirement(distribution="accelerated-lib")
    assert hash(req) == hash(
        AcceleratedRequirement(distribution="accelerated-lib")
    )
    with pytest.raises(Exception):
        req.distribution = "other"  # type: ignore


# ===========================================================================
# AccelerationIncompatibility model
# ===========================================================================


def test_incompatibility_canonicalizes_distribution():
    """Distribution names are canonicalized."""
    inc = AccelerationIncompatibility(
        distribution="Fake_CUDA", reason="conflicts at runtime"
    )
    assert inc.distribution == "fake-cuda"
    assert inc.reason == "conflicts at runtime"


def test_incompatibility_rejects_empty_reason():
    """Empty reasons are rejected."""
    with pytest.raises(ValueError, match="reason"):
        AccelerationIncompatibility(distribution="fake-cuda", reason="   ")


def test_incompatibility_rejects_empty_distribution():
    """Empty distribution names are rejected."""
    with pytest.raises(ValueError, match="distribution"):
        AccelerationIncompatibility(distribution="", reason="conflicts")


# ===========================================================================
# ProductAccelerationRequirements model
# ===========================================================================


def test_product_requirements_valid_minimal():
    """A minimal declaration (backend only) is valid."""
    par = ProductAccelerationRequirements(product_id="zebench", backend="NVIDIA_CUDA")
    assert par.product_id == "zebench"
    assert par.backend == "NVIDIA_CUDA"
    assert par.optional is True
    assert par.requirements == ()
    assert par.incompatibilities == ()
    assert par.source == "zealfie-catalog@1"


def test_product_requirements_full_declaration():
    """A full declaration keeps its parts in declaration order."""
    par = ProductAccelerationRequirements(
        product_id="zebench",
        backend="NVIDIA_CUDA",
        optional=False,
        requirements=(
            AcceleratedRequirement(distribution="accelerated-lib", specifier="==1.0.0"),
            AcceleratedRequirement(distribution="kernel-common"),
        ),
        incompatibilities=(
            AccelerationIncompatibility(distribution="fake-cuda", reason="conflicts"),
        ),
    )
    assert par.optional is False
    assert [r.distribution for r in par.requirements] == [
        "accelerated-lib",
        "kernel-common",
    ]
    assert [i.distribution for i in par.incompatibilities] == ["fake-cuda"]


def test_product_requirements_rejects_empty_product_id():
    """Empty product ids are rejected."""
    with pytest.raises(ValueError, match="product_id"):
        ProductAccelerationRequirements(product_id=" ", backend="NVIDIA_CUDA")


def test_product_requirements_rejects_unknown_backend():
    """Unknown backends are rejected fail-closed."""
    with pytest.raises(ValueError, match="unsupported acceleration backend"):
        ProductAccelerationRequirements(product_id="zebench", backend="AMD_ROCM")


def test_product_requirements_rejects_non_bool_optional():
    """optional must be an actual bool."""
    with pytest.raises(ValueError, match="optional"):
        ProductAccelerationRequirements(
            product_id="zebench", backend="NVIDIA_CUDA", optional="yes"
        )


def test_product_requirements_rejects_duplicate_requirement_distributions():
    """Two requirements on the same distribution are rejected."""
    with pytest.raises(ValueError, match="duplicate requirement"):
        ProductAccelerationRequirements(
            product_id="zebench",
            backend="NVIDIA_CUDA",
            requirements=(
                AcceleratedRequirement(distribution="Accelerated_Lib"),
                AcceleratedRequirement(distribution="accelerated-lib"),
            ),
        )


def test_product_requirements_rejects_duplicate_incompatibility_distributions():
    """Two incompatibilities on the same distribution are rejected."""
    with pytest.raises(ValueError, match="duplicate incompatibility"):
        ProductAccelerationRequirements(
            product_id="zebench",
            backend="NVIDIA_CUDA",
            incompatibilities=(
                AccelerationIncompatibility(distribution="fake-cuda", reason="a"),
                AccelerationIncompatibility(distribution="Fake_CUDA", reason="b"),
            ),
        )


def test_product_requirements_rejects_requirement_in_incompatibilities():
    """A distribution in both lists is rejected."""
    with pytest.raises(ValueError, match="both as requirement and as incompatibility"):
        ProductAccelerationRequirements(
            product_id="zebench",
            backend="NVIDIA_CUDA",
            requirements=(AcceleratedRequirement(distribution="accelerated-lib"),),
            incompatibilities=(
                AccelerationIncompatibility(distribution="accelerated-lib", reason="no"),
            ),
        )


def test_product_requirements_rejects_wrong_requirement_types():
    """Non-AcceleratedRequirement values in requirements are rejected."""
    with pytest.raises(ValueError, match="AcceleratedRequirement"):
        ProductAccelerationRequirements(
            product_id="zebench",
            backend="NVIDIA_CUDA",
            requirements=("accelerated-lib",),
        )


def test_product_requirements_rejects_wrong_incompatibility_types():
    """Non-AccelerationIncompatibility values in incompatibilities are rejected."""
    with pytest.raises(ValueError, match="AccelerationIncompatibility"):
        ProductAccelerationRequirements(
            product_id="zebench",
            backend="NVIDIA_CUDA",
            incompatibilities=("fake-cuda",),
        )


def test_known_backends_value():
    """KNOWN_BACKENDS currently contains exactly NVIDIA_CUDA."""
    assert KNOWN_BACKENDS == frozenset({"NVIDIA_CUDA"})


# ===========================================================================
# HardwareCompatibility model
# ===========================================================================


def test_hardware_compatibility_frozen():
    """HardwareCompatibility is frozen and hashable."""
    hc = HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="ok",
        products_concerned=("zebench",),
    )
    assert hash(hc) == hash(hc)
    with pytest.raises(Exception):
        hc.reason = "nope"  # type: ignore


# ===========================================================================
# Catalog parsing — valid acceleration table
# ===========================================================================


def test_valid_acceleration_table_parses():
    """A valid [products.acceleration] table parses into the descriptor."""
    catalog = load_catalog_from_text(
        _catalog_with_acceleration(VALID_ACCELERATION_BLOCK)
    )
    desc = catalog.get("zebench")
    acc = desc.acceleration
    assert acc is not None
    assert isinstance(acc, ProductAccelerationRequirements)
    assert acc.product_id == "zebench"
    assert acc.backend == "NVIDIA_CUDA"
    assert acc.optional is True
    assert acc.source == "zealfie-catalog@1"

    assert len(acc.requirements) == 1
    req = acc.requirements[0]
    assert req.distribution == "accelerated-lib"
    assert req.specifier == ">=1.0,<3"
    assert req.extras == ("cuda12", "gui")

    assert len(acc.incompatibilities) == 1
    inc = acc.incompatibilities[0]
    assert inc.distribution == "fake-cuda"
    assert inc.reason == "ships its own kernels"


def test_acceleration_optional_false_parses():
    """optional = false is parsed."""
    catalog = load_catalog_from_text(
        _catalog_with_acceleration(
            'backend = "NVIDIA_CUDA"\noptional = false\n'
        )
    )
    assert catalog.get("zebench").acceleration.optional is False


def test_absent_acceleration_is_none():
    """Products without the table get acceleration=None."""
    catalog = load_catalog_from_text(
        "schema_version = 1\n\n"
        "[[products]]\n"
        'id = "zebench"\n'
        'display_name = "ZeBench"\n'
        'distribution_name = "ZeBench"\n'
        "[products.launch]\n"
        'entry_points = [{group = "gui_scripts", name = "zebench"}]\n'
    )
    assert catalog.get("zebench").acceleration is None


def test_real_catalog_acceleration_contracts():
    """The packaged catalog declares acceleration tables for the products
    that ship an optional NVIDIA_CUDA backend: zemosaic (ZA-M1-2J Phase C)
    and zeseestarstacker (ZSSS M3, optional CuPy drizzle); every other
    product stays None."""
    catalog = default_catalog()
    declared = [
        desc.product_id for desc in catalog.list() if desc.acceleration is not None
    ]
    assert declared == ["zemosaic", "zeseestarstacker"]
    for desc in catalog.list():
        if desc.product_id in ("zemosaic", "zeseestarstacker"):
            continue
        assert desc.acceleration is None, desc.product_id


def test_acceleration_requirements_without_specifier_or_extras():
    """A requirement table with only distribution is valid."""
    catalog = load_catalog_from_text(
        _catalog_with_acceleration(
            'backend = "NVIDIA_CUDA"\n'
            "[[products.acceleration.requirements]]\n"
            'distribution = "accelerated-lib"\n'
        )
    )
    req = catalog.get("zebench").acceleration.requirements[0]
    assert req.specifier is None
    assert req.extras == ()


# ===========================================================================
# Catalog parsing — malformed acceleration tables fail closed
# ===========================================================================


def test_acceleration_not_a_table_rejected():
    """acceleration as a non-table is rejected."""
    toml = (
        "schema_version = 1\n\n"
        "[[products]]\n"
        'id = "zebench"\n'
        'display_name = "ZeBench"\n'
        'distribution_name = "ZeBench"\n'
        'acceleration = "yes please"\n'
        "[products.launch]\n"
        'entry_points = [{group = "gui_scripts", name = "zebench"}]\n'
    )
    with pytest.raises(InvalidCatalogError, match="acceleration must be a table"):
        load_catalog_from_text(toml)


def test_unknown_key_in_acceleration_rejected():
    """Unknown keys inside the acceleration table are rejected."""
    with pytest.raises(InvalidCatalogError, match="unknown key"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\nframeworks = ["tensorflow"]\n'
            )
        )


def test_unknown_key_in_requirement_rejected():
    """Unknown keys inside a requirement table are rejected."""
    with pytest.raises(InvalidCatalogError, match="unknown key"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
                'platform = "linux"\n'
            )
        )


def test_unknown_key_in_incompatibility_rejected():
    """Unknown keys inside an incompatibility table are rejected."""
    with pytest.raises(InvalidCatalogError, match="unknown key"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.incompatibilities]]\n"
                'distribution = "fake-cuda"\n'
                'reason = "conflicts"\n'
                'severity = "high"\n'
            )
        )


def test_missing_backend_rejected():
    """The backend key is required inside the acceleration table."""
    with pytest.raises(InvalidCatalogError, match="acceleration.backend"):
        load_catalog_from_text(
            _catalog_with_acceleration('optional = true\n')
        )


def test_unknown_backend_rejected():
    """A backend outside KNOWN_BACKENDS is rejected."""
    with pytest.raises(InvalidCatalogError, match="unsupported acceleration backend"):
        load_catalog_from_text(
            _catalog_with_acceleration('backend = "AMD_ROCM"\n')
        )


def test_invalid_specifier_rejected():
    """A specifier that does not parse as a PEP 440 SpecifierSet is rejected."""
    with pytest.raises(InvalidCatalogError, match="specifier"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
                'specifier = ">>1.0"\n'
            )
        )


def test_duplicate_requirement_rejected():
    """Two requirement tables on the same (canonicalized) distribution
    are rejected."""
    with pytest.raises(InvalidCatalogError, match="duplicate requirement"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "Accelerated_Lib"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
            )
        )


def test_requirement_also_in_incompatibilities_rejected():
    """A distribution in both requirements and incompatibilities is rejected."""
    with pytest.raises(InvalidCatalogError, match="both as requirement"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
                "[[products.acceleration.incompatibilities]]\n"
                'distribution = "accelerated-lib"\n'
                'reason = "self conflict"\n'
            )
        )


def test_empty_incompatibility_reason_rejected():
    """An empty incompatibility reason is rejected."""
    with pytest.raises(InvalidCatalogError, match="reason"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.incompatibilities]]\n"
                'distribution = "fake-cuda"\n'
                'reason = ""\n'
            )
        )


def test_requirements_wrong_type_rejected():
    """requirements as a non-list is rejected."""
    with pytest.raises(InvalidCatalogError, match="array of tables"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\nrequirements = "accelerated-lib"\n'
            )
        )


def test_requirement_not_a_table_rejected():
    """A requirement entry that is not a table is rejected.

    TOML arrays of tables can only hold tables, so a non-table element
    is expressed as an inline array of strings.
    """
    with pytest.raises(InvalidCatalogError, match="must be a table"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                'requirements = ["accelerated-lib"]\n'
            )
        )


def test_requirement_distribution_wrong_type_rejected():
    """A non-string distribution is rejected."""
    with pytest.raises(InvalidCatalogError, match="distribution"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                "distribution = 42\n"
            )
        )


def test_requirement_specifier_wrong_type_rejected():
    """A non-string specifier is rejected."""
    with pytest.raises(InvalidCatalogError, match="specifier"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
                "specifier = 42\n"
            )
        )


def test_requirement_extras_wrong_type_rejected():
    """extras as a non-list is rejected."""
    with pytest.raises(InvalidCatalogError, match="extras"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
                'extras = "gui"\n'
            )
        )


def test_requirement_empty_extra_rejected():
    """Empty extras entries are rejected."""
    with pytest.raises(InvalidCatalogError, match="extras"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.requirements]]\n"
                'distribution = "accelerated-lib"\n'
                'extras = ["gui", ""]\n'
            )
        )


def test_optional_wrong_type_rejected():
    """optional as a non-bool is rejected."""
    with pytest.raises(InvalidCatalogError, match="optional"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\noptional = "yes"\n'
            )
        )


def test_incompatibility_missing_distribution_rejected():
    """Incompatibility tables require distribution."""
    with pytest.raises(InvalidCatalogError, match="distribution"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.incompatibilities]]\n"
                'reason = "conflicts"\n'
            )
        )


def test_incompatibility_missing_reason_rejected():
    """Incompatibility tables require reason."""
    with pytest.raises(InvalidCatalogError, match="reason"):
        load_catalog_from_text(
            _catalog_with_acceleration(
                'backend = "NVIDIA_CUDA"\n'
                "[[products.acceleration.incompatibilities]]\n"
                'distribution = "fake-cuda"\n'
            )
        )


# ===========================================================================
# ProductDescriptor integration
# ===========================================================================


def test_descriptor_acceleration_defaults_to_none():
    """The new field defaults to None and is the last field."""
    desc = _descriptor()
    assert desc.acceleration is None


def test_descriptor_accepts_acceleration_instance():
    """A validated ProductAccelerationRequirements instance is accepted."""
    acc = ProductAccelerationRequirements(product_id="zebench", backend="NVIDIA_CUDA")
    desc = _descriptor(acceleration=acc)
    assert desc.acceleration is acc


def test_descriptor_rejects_non_acceleration_object():
    """Garbage values for acceleration are rejected fail-closed."""
    with pytest.raises(ValueError, match="acceleration"):
        _descriptor(acceleration="nvidia-cuda")  # type: ignore


def test_descriptor_positional_construction_still_works():
    """Existing positional constructions keep working (new field is last
    with a default)."""
    desc = ProductDescriptor(
        "zebench",
        "ZeBench",
        "ZeBench",
        (EntryPointContract("gui_scripts", "zebench"),),
    )
    assert desc.acceleration is None
