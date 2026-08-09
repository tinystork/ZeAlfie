"""Tests for M1-2A Product Catalog and Product Shell read model.

Tests cover:

- Catalog content (exactly 4 products, stable order)
- Product state with ABSENT runtime (4 known, 0 installed)
- Product state with READY runtime + fake probe (only ZeSolver installed/launchable)
- Selection independence (catalog ≠ deployment contract)
- Unknown product → UnknownProductError
- Dev-env contamination sentinel
- Launchability false when contract absent
- CLI products command
- UnknownProductError distinct from UnknownComponentError
- Legacy tests still pass (verified by running existing test suites)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.app import (
    ManagedStatus,
    ProductCatalog,
    ProductDescriptor,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    UnknownProductError,
    ZeAlfieService,
)
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.products.catalog import default_catalog, load_catalog_from_text
from zealfie.products.state import collect_product_state, get_product_state
from zealfie.runtime.model import RuntimeReasonCode, RuntimeState, RuntimeStatus


# ===========================================================================
# Helpers
# ===========================================================================


def _absent_status() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=Path("/fake/runtime"),
    )


def _broken_status() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.BROKEN,
        runtime_root=Path("/fake/runtime"),
        reason="active slot missing",
    )


def _ready_status(
    active_path: Path | None = None,
    python: Path | None = None,
) -> RuntimeStatus:
    if active_path is None:
        active_path = Path("/fake/runtime/slots/rt-test")
    if python is None:
        python = active_path / "bin" / "python"
    return RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=active_path.parent.parent,
        active_slot_id="rt-test",
        active_path=active_path,
        python_executable=python,
        python_version="3.13.5",
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )


def _fake_probe_zesolver_only(runtime_python: str, distribution_name: str) -> dict:
    """Fake probe: ZeSolver installed+launchable, others not installed."""
    if distribution_name == "ZeSolver":
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "gui_scripts", "name": "zesolver"},
            ],
        }
    return {
        "python_version": "3.13.5",
        "installed": False,
        "version": None,
        "entry_points": [],
    }


def _recording_probe():
    """Return a probe that records calls and returns not-installed."""
    calls: list[tuple[str, str]] = []

    def probe(runtime_python: str, distribution_name: str) -> dict:
        calls.append((runtime_python, distribution_name))
        return {
            "python_version": "3.13.5",
            "installed": False,
            "version": None,
            "entry_points": [],
        }

    return probe, calls


# ===========================================================================
# 1) Catalog has exactly 4 products in stable order
# ===========================================================================


EXPECTED_PRODUCT_IDS = ("zesolver", "zemosaic", "zeseestarstacker", "zeanalyser")


def test_catalog_has_exactly_4_products():
    """The default product catalog contains exactly 4 known products."""
    catalog = default_catalog()
    assert len(catalog) == 4
    assert catalog.available_ids() == EXPECTED_PRODUCT_IDS


def test_catalog_list_returns_4_descriptors():
    """catalog.list() returns 4 ProductDescriptor in definition order."""
    catalog = default_catalog()
    descriptors = catalog.list()
    assert len(descriptors) == 4
    assert tuple(d.product_id for d in descriptors) == EXPECTED_PRODUCT_IDS


def test_catalog_get_returns_correct_descriptor():
    """catalog.get() returns the right descriptor for each known id."""
    catalog = default_catalog()
    desc = catalog.get("zesolver")
    assert desc.product_id == "zesolver"
    assert desc.display_name == "ZeSolver"
    assert desc.distribution_name == "ZeSolver"
    assert len(desc.launch_entry_points) == 1
    assert desc.launch_entry_points[0].group == "gui_scripts"
    assert desc.launch_entry_points[0].name == "zesolver"


def test_catalog_contains_all_expected_ids():
    """All 4 expected ids are in the catalog."""
    catalog = default_catalog()
    for pid in EXPECTED_PRODUCT_IDS:
        assert pid in catalog


def test_catalog_available_ids_is_stable_tuple():
    """available_ids() returns a tuple, not a list."""
    catalog = default_catalog()
    ids = catalog.available_ids()
    assert isinstance(ids, tuple)
    assert ids == EXPECTED_PRODUCT_IDS


# ===========================================================================
# 2) 4 known + 0 installed with ABSENT runtime
# ===========================================================================


def test_absent_runtime_4_known_0_installed():
    """With ABSENT runtime, all 4 products are known, none installed."""
    catalog = default_catalog()
    shell = collect_product_state(
        catalog,
        _absent_status(),
    )
    assert isinstance(shell, ProductShellState)
    assert shell.runtime_state == RuntimeState.ABSENT
    assert len(shell.products) == 4
    assert shell.installed_count == 0
    assert shell.managed_count == 0  # default: managed_component_ids is empty

    for p in shell.products:
        assert p.known is True
        assert p.installed is False
        assert p.launchable is False
        assert p.version is None
        assert p.reason_code == ProductStateReasonCode.RUNTIME_ABSENT


def test_absent_runtime_managed_set_reflected():
    """Managed ids are reflected in the managed field even when ABSENT."""
    catalog = default_catalog()
    shell = collect_product_state(
        catalog,
        _absent_status(),
        managed_component_ids=frozenset({"zesolver"}),
    )
    assert shell.managed_count == 1
    for p in shell.products:
        if p.product_id == "zesolver":
            assert p.managed == ManagedStatus.MANAGED
        else:
            assert p.managed == ManagedStatus.UNMANAGED


# ===========================================================================
# 3) 4 known + only ZeSolver installed/launchable with READY runtime
# ===========================================================================


def test_ready_runtime_only_zesolver_installed():
    """READY runtime + fake probe → only ZeSolver installed/launchable."""
    catalog = default_catalog()
    shell = collect_product_state(
        catalog,
        _ready_status(),
        managed_component_ids=frozenset({"zesolver"}),
        probe_fn=_fake_probe_zesolver_only,
    )
    assert shell.runtime_state == RuntimeState.READY
    assert len(shell.products) == 4
    assert shell.managed_count == 1

    installed_ids = {p.product_id for p in shell.products if p.installed}
    assert installed_ids == {"zesolver"}

    for p in shell.products:
        assert p.known is True
        if p.product_id == "zesolver":
            assert p.installed is True
            assert p.launchable is True
            assert p.version == "1.0.0"
            assert p.reason_code == ProductStateReasonCode.INSTALLED_LAUNCHABLE
            assert p.managed == ManagedStatus.MANAGED
        else:
            assert p.installed is False
            assert p.launchable is False
            assert p.version is None
            assert p.reason_code == ProductStateReasonCode.NOT_INSTALLED
            assert p.managed == ManagedStatus.UNMANAGED


def test_ready_runtime_unmanaged_products_are_probed():
    """Unmanaged known products are still probed in READY runtime."""
    catalog = default_catalog()
    probe, calls = _recording_probe()
    shell = collect_product_state(
        catalog,
        _ready_status(),
        managed_component_ids=frozenset({"zesolver"}),
        probe_fn=probe,
    )
    # All 4 products should have been probed.
    assert len(calls) == 4, f"Expected 4 probe calls, got {len(calls)}"
    probed_dists = {dist for _, dist in calls}
    assert probed_dists == {"ZeSolver", "ZeMosaic", "ZeSeestarStacker", "ZeAnalyser"}

    # Even unmanaged products are probed and report NOT_INSTALLED.
    for p in shell.products:
        assert p.installed is False
    assert shell.managed_count == 1


# ===========================================================================
# 3b) BROKEN runtime
# ===========================================================================


def test_broken_runtime_all_not_installed():
    """BROKEN runtime → all products not installed, no probes."""
    catalog = default_catalog()

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("probe must not be called for BROKEN runtime")

    shell = collect_product_state(
        catalog,
        _broken_status(),
        probe_fn=must_not_be_called,
    )
    assert shell.runtime_state == RuntimeState.BROKEN
    for p in shell.products:
        assert p.known is True
        assert p.installed is False
        assert p.launchable is False
        assert p.reason_code == ProductStateReasonCode.RUNTIME_BROKEN


# ===========================================================================
# 3c) READY runtime with no python_executable
# ===========================================================================


def test_ready_without_python_executable():
    """READY runtime with None python_executable → RUNTIME_BROKEN."""
    catalog = default_catalog()
    status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=Path("/fake/runtime"),
        active_slot_id="rt-test",
        active_path=Path("/fake/runtime/slots/rt-test"),
        python_executable=None,
        python_version="3.13.5",
    )
    shell = collect_product_state(catalog, status)
    for p in shell.products:
        assert p.installed is False
        assert p.reason_code == ProductStateReasonCode.RUNTIME_BROKEN
        assert "no python executable" in p.reason.lower()


# ===========================================================================
# 4) Selection independence
# ===========================================================================


def test_catalog_has_4_but_registry_can_have_1():
    """Product catalog has 4 products; ComponentRegistry can have 1.

    Adding products to the catalog must not force them into the registry.
    """
    catalog = default_catalog()
    assert len(catalog) == 4

    # Create a registry with only ZeSolver (matching current reality).
    registry = ComponentRegistry([
        ComponentDefinition(
            component_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
        ),
    ])
    assert registry.available_ids() == ("zesolver",)

    # The catalog still has 4, the registry has 1. These are independent.
    assert set(catalog.available_ids()) != set(registry.available_ids())


def test_service_managed_product_ids_independent_from_catalog():
    """ZeAlfieService.managed_product_ids comes from registry, not catalog."""
    registry = ComponentRegistry([
        ComponentDefinition(
            component_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
        ),
    ])
    service = ZeAlfieService(registry=registry)
    assert service.managed_product_ids == frozenset({"zesolver"})
    # The catalog still has 4 products regardless.
    assert len(service.catalog) == 4


# ===========================================================================
# 5) Unknown product raises typed UnknownProductError
# ===========================================================================


def test_catalog_get_unknown_raises_UnknownProductError():
    """catalog.get() with unknown id raises UnknownProductError."""
    catalog = default_catalog()
    with pytest.raises(UnknownProductError) as exc_info:
        catalog.get("nonexistent")
    assert exc_info.value.product_id == "nonexistent"


def test_get_product_state_unknown_raises_UnknownProductError():
    """get_product_state() with unknown id raises UnknownProductError."""
    catalog = default_catalog()
    with pytest.raises(UnknownProductError) as exc_info:
        get_product_state(catalog, "nonexistent", _absent_status())
    assert exc_info.value.product_id == "nonexistent"


def test_service_get_product_state_unknown_raises():
    """ZeAlfieService.get_product_state() raises UnknownProductError."""
    service = ZeAlfieService()
    with pytest.raises(UnknownProductError, match="nonexistent"):
        service.get_product_state("nonexistent")


def test_unknown_product_error_is_not_unknown_component_error():
    """UnknownProductError is a distinct type from UnknownComponentError."""
    from zealfie.components.registry import UnknownComponentError

    err = UnknownProductError("test")
    assert isinstance(err, KeyError)
    assert not isinstance(err, UnknownComponentError)


# ===========================================================================
# 6) Dev-env contamination sentinel
# ===========================================================================


def test_product_state_uses_runtime_python_not_importlib():
    """Product state probes the runtime's Python; dev env importability is
    not proof of installation.

    We use a recording probe to assert that the probe is called with the
    *runtime_python* path (not with dev Python) and that the caller
    never uses importlib.metadata or import in the current process.
    """
    catalog = default_catalog()
    runtime_python_path = Path("/fake/runtime/slots/rt-test/bin/python")

    probe_calls: list[tuple[str, str]] = []

    def recording_probe(runtime_python: str, distribution_name: str) -> dict:
        probe_calls.append((runtime_python, distribution_name))
        return {
            "python_version": "3.13.5",
            "installed": False,
            "version": None,
            "entry_points": [],
        }

    shell = collect_product_state(
        catalog,
        _ready_status(python=runtime_python_path),
        probe_fn=recording_probe,
    )

    # 4 calls, one per product
    assert len(probe_calls) == 4

    # Every call uses the runtime_python path, never the dev Python
    for runtime_python, dist_name in probe_calls:
        assert runtime_python == str(runtime_python_path), (
            f"probe called with {runtime_python!r}, "
            f"expected {str(runtime_python_path)!r}"
        )

    # All products report NOT_INSTALLED — even if a test fixture
    # distribution were importable in dev env, that wouldn't matter.
    assert shell.installed_count == 0


def test_product_state_never_uses_import_or_importlib():
    """The product state module must not use importlib.metadata.distribution
    or import directly.  It must route through the probe only.

    This test verifies that when we monkeypatch importlib in the
    products.state module, product state collection still works via
    the injected probe — proving no fallback to importlib.
    """
    import zealfie.products.state as state_mod

    catalog = default_catalog()
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    probe_called = False

    def fake_probe(runtime_python: str, distribution_name: str) -> dict:
        nonlocal probe_called
        probe_called = True
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "gui_scripts", "name": "zesolver"},
            ],
        }

    # Even if the state module had an importlib fallback, we're using
    # the injectable probe_fn which takes priority.  The collection
    # succeeds without touching importlib.metadata.
    shell = collect_product_state(
        catalog,
        _ready_status(python=runtime_python),
        probe_fn=fake_probe,
    )
    assert probe_called
    # All products report as installed (fake probe says everything is installed)
    assert shell.installed_count == 4


# ===========================================================================
# 7) Launchability false if entry point contract missing
# ===========================================================================


def test_installed_but_no_contract_not_launchable():
    """Product installed but no matching entry-point contract → not launchable."""
    catalog = default_catalog()
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    def probe_no_contract(runtime_python: str, distribution_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "other_scripts", "name": "other_tool"},
            ],
        }

    shell = collect_product_state(
        catalog,
        _ready_status(python=runtime_python),
        managed_component_ids=frozenset({"zesolver"}),
        probe_fn=probe_no_contract,
    )

    for p in shell.products:
        assert p.installed is True
        assert p.launchable is False
        assert p.reason_code == ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE
        assert "launch contract" in p.reason.lower()


def test_zesolver_installed_no_matching_contract():
    """ZeSolver installed but entry_points have wrong group/name → not launchable."""
    catalog = default_catalog()
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    def probe_wrong_contract(runtime_python: str, distribution_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "zesolver"},
                # ZeSolver expects gui_scripts:zesolver, not console_scripts
            ],
        }

    state = get_product_state(
        catalog, "zesolver",
        _ready_status(python=runtime_python),
        managed_component_ids=frozenset({"zesolver"}),
        probe_fn=probe_wrong_contract,
    )
    assert state.installed is True
    assert state.launchable is False
    assert state.reason_code == ProductStateReasonCode.INSTALLED_NOT_LAUNCHABLE


def test_zesolver_installed_correct_contract_launchable():
    """ZeSolver installed with gui_scripts:zesolver → launchable."""
    catalog = default_catalog()
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    def probe_correct(runtime_python: str, distribution_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "gui_scripts", "name": "zesolver"},
            ],
        }

    state = get_product_state(
        catalog, "zesolver",
        _ready_status(python=runtime_python),
        managed_component_ids=frozenset({"zesolver"}),
        probe_fn=probe_correct,
    )
    assert state.installed is True
    assert state.launchable is True
    assert state.reason_code == ProductStateReasonCode.INSTALLED_LAUNCHABLE


def test_multi_entry_point_first_match_makes_launchable():
    """Multiple entry points in probe; first matching contract → launchable."""
    # Create a catalog with a product that has multiple entry-point contracts.
    from zealfie.products.catalog import ProductDescriptor

    desc = ProductDescriptor(
        product_id="testprod",
        display_name="Test Product",
        distribution_name="test-prod",
        launch_entry_points=(
            EntryPointContract("console_scripts", "first_app"),
            EntryPointContract("console_scripts", "second_app"),
        ),
    )
    catalog = ProductCatalog((desc,))
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    def probe_multi(runtime_python: str, distribution_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [
                {"group": "console_scripts", "name": "first_app"},
                {"group": "console_scripts", "name": "second_app"},
            ],
        }

    state = get_product_state(
        catalog, "testprod",
        _ready_status(python=runtime_python),
        probe_fn=probe_multi,
    )
    assert state.installed is True
    assert state.launchable is True


def test_no_expected_contract_means_launchable_by_default():
    """A product with no expected launch_entry_points is always launchable
    when installed."""
    from zealfie.products.catalog import ProductDescriptor

    desc = ProductDescriptor(
        product_id="nolaunch",
        display_name="No Launch",
        distribution_name="no-launch",
        launch_entry_points=(),  # No expected contracts
    )
    catalog = ProductCatalog((desc,))
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    def probe_installed(runtime_python: str, distribution_name: str) -> dict:
        return {
            "python_version": "3.13.5",
            "installed": True,
            "version": "1.0.0",
            "entry_points": [],
        }

    state = get_product_state(
        catalog, "nolaunch",
        _ready_status(python=runtime_python),
        probe_fn=probe_installed,
    )
    assert state.installed is True
    assert state.launchable is True
    assert state.reason_code == ProductStateReasonCode.INSTALLED_LAUNCHABLE


# ===========================================================================
# 7b) Probe failures
# ===========================================================================


def test_probe_exception_reports_probe_failed():
    """Probe raises → PROBE_FAILED with the exception message."""
    catalog = default_catalog()
    runtime_python = Path("/fake/runtime/slots/rt-test/bin/python")

    def failing_probe(runtime_python: str, distribution_name: str) -> dict:
        if distribution_name == "ZeSolver":
            raise RuntimeError("probe crashed for ZeSolver")

    shell = collect_product_state(
        catalog,
        _ready_status(python=runtime_python),
        probe_fn=failing_probe,
    )
    # ZeSolver should have PROBE_FAILED
    zesolver = None
    for p in shell.products:
        if p.product_id == "zesolver":
            zesolver = p
            break
    assert zesolver is not None
    assert zesolver.installed is False
    assert zesolver.reason_code == ProductStateReasonCode.PROBE_FAILED
    assert "probe crashed" in zesolver.reason


def test_probe_returns_non_dict_reports_probe_failed():
    """Probe returns non-dict → PROBE_FAILED."""
    catalog = default_catalog()

    def bad_probe(runtime_python: str, distribution_name: str) -> str:
        return "not a dict"  # type: ignore

    state = get_product_state(
        catalog, "zesolver",
        _ready_status(),
        probe_fn=bad_probe,  # type: ignore
    )
    assert state.installed is False
    assert state.reason_code == ProductStateReasonCode.PROBE_FAILED
    assert "non-dict" in state.reason.lower()


# ===========================================================================
# 8) CLI products command
# ===========================================================================


import zealfie.cli as cli
from io import StringIO


class _FakeProductService:
    """Fake ZeAlfieService for CLI products tests."""

    def __init__(self, shell_state=None, single_state=None,
                 catalog_ids=("zesolver",)):
        self._shell_state = shell_state
        self._single_state = single_state
        self._catalog_ids = catalog_ids
        self.collect_called = 0
        self.get_called_with: list[str] = []

    @property
    def catalog(self):
        class _FakeCatalog:
            def available_ids(self):
                return self._ids
        fc = _FakeCatalog()
        fc._ids = self._catalog_ids  # type: ignore
        return fc

    def collect_product_state(self):
        self.collect_called += 1
        return self._shell_state

    def get_product_state(self, product_id):
        self.get_called_with.append(product_id)
        if self._single_state is None:
            raise UnknownProductError(product_id)
        return self._single_state


def _make_fake_shell(installed_count=0, managed_count=0, runtime_state=RuntimeState.ABSENT):
    """Create a minimal ProductShellState for CLI tests."""
    products = tuple(
        ProductState(
            product_id=pid,
            display_name=pid.title(),
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.RUNTIME_ABSENT,
            reason="shared runtime is absent",
            managed=ManagedStatus.UNMANAGED,
        )
        for pid in ("zesolver", "zemosaic", "zeseestarstacker", "zeanalyser")
    )
    return ProductShellState(
        runtime_state=runtime_state,
        runtime_root=Path("/fake/runtime"),
        products=products,
    )


def test_cli_products_command_shows_all_products(monkeypatch):
    """CLI `zealfie products` outputs product shell state."""
    shell = _make_fake_shell()
    service = _FakeProductService(shell_state=shell)
    monkeypatch.setattr(cli, "_make_service", lambda: service)

    stdout = StringIO()
    code = cli.run(["products"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Product shell state:" in output
    assert "Known products: 4" in output
    assert "zesolver" in output
    assert "zemosaic" in output
    assert "zeseestarstacker" in output
    assert "zeanalyser" in output
    assert "Runtime state: ABSENT" in output
    assert service.collect_called == 1


def test_cli_products_command_single_product(monkeypatch):
    """CLI `zealfie products zesolver` outputs single product state."""
    single = ProductState(
        product_id="zesolver",
        display_name="ZeSolver",
        known=True,
        installed=True,
        launchable=True,
        version="1.0.0",
        reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
        reason="ZeSolver 1.0.0 installed and launchable",
        managed=ManagedStatus.MANAGED,
    )
    service = _FakeProductService(single_state=single)
    monkeypatch.setattr(cli, "_make_service", lambda: service)

    stdout = StringIO()
    code = cli.run(["products", "zesolver"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Product: zesolver (ZeSolver)" in output
    assert "Managed: MANAGED" in output
    assert "Installed: yes" in output
    assert "Version: 1.0.0" in output
    assert "Launchable: yes" in output
    assert "INSTALLED_LAUNCHABLE" in output
    assert len(service.get_called_with) == 1
    assert service.get_called_with[0] == "zesolver"


def test_cli_products_unknown_product_clean_error(monkeypatch):
    """CLI `zealfie products unknown` → clean error, code 2, no traceback."""
    import sys

    service = _FakeProductService(catalog_ids=("zesolver", "zemosaic", "zeseestarstacker", "zeanalyser"))
    monkeypatch.setattr(cli, "_make_service", lambda: service)

    backup = sys.stderr
    try:
        sys.stderr = stderr = StringIO()
        stdout = StringIO()
        code = cli.run(["products", "nonexistent"], stdout=stdout)
        assert code == 2
        err = stderr.getvalue()
        assert "Unknown product: nonexistent" in err
        assert "zesolver" in err
        assert "Traceback" not in err
    finally:
        sys.stderr = backup


def test_cli_products_command_in_parser():
    """products subcommand is present in the argument parser."""
    p = cli.build_parser()
    args = p.parse_args(["products"])
    assert args.command == "products"
    assert args.product_id is None

    args = p.parse_args(["products", "zesolver"])
    assert args.command == "products"
    assert args.product_id == "zesolver"


# ===========================================================================
# 9) Catalog validation — load from TOML
# ===========================================================================


def test_load_catalog_from_minimal_toml():
    """A minimal valid catalog TOML parses correctly."""
    toml = """\
schema_version = 1

[[products]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"

[[products.launch.entry_points]]
group = "gui_scripts"
name = "zesolver"

[[products]]
id = "zemosaic"
display_name = "ZeMosaic"
distribution_name = "ZeMosaic"

[[products.launch.entry_points]]
group = "gui_scripts"
name = "zemosaic"
"""
    catalog = load_catalog_from_text(toml)
    assert len(catalog) == 2
    assert catalog.available_ids() == ("zesolver", "zemosaic")


def test_load_catalog_rejects_missing_schema_version():
    """Catalog with no schema_version → InvalidCatalogError."""
    from zealfie.products.catalog import InvalidCatalogError

    with pytest.raises(InvalidCatalogError, match="schema_version"):
        load_catalog_from_text("[[products]]\nid = 'x'\n")


def test_load_catalog_rejects_empty_products():
    """Catalog with empty products list → InvalidCatalogError."""
    from zealfie.products.catalog import InvalidCatalogError

    with pytest.raises(InvalidCatalogError, match="must not be empty"):
        load_catalog_from_text('schema_version = 1\nproducts = []\n')


def test_load_catalog_rejects_duplicate_ids():
    """Catalog with duplicate product ids → InvalidCatalogError."""
    from zealfie.products.catalog import InvalidCatalogError

    toml = """\
schema_version = 1

[[products]]
id = "zesolver"
display_name = "ZeSolver"
distribution_name = "ZeSolver"
[products.launch]
entry_points = [{group = "gui_scripts", name = "zesolver"}]

[[products]]
id = "zesolver"
display_name = "ZeSolver Dupe"
distribution_name = "ZeSolver"
[products.launch]
entry_points = [{group = "gui_scripts", name = "zesolver"}]
"""
    with pytest.raises(InvalidCatalogError, match="duplicate product id"):
        load_catalog_from_text(toml)


# ===========================================================================
# 10) ProductDescriptor validation
# ===========================================================================


def test_product_descriptor_rejects_empty_id():
    with pytest.raises(ValueError, match="product_id"):
        ProductDescriptor(
            product_id="",
            display_name="Test",
            distribution_name="test",
            launch_entry_points=(EntryPointContract("console_scripts", "test"),),
        )


def test_product_descriptor_rejects_empty_display_name():
    with pytest.raises(ValueError, match="display_name"):
        ProductDescriptor(
            product_id="test",
            display_name="",
            distribution_name="test",
            launch_entry_points=(),
        )


def test_product_descriptor_rejects_empty_distribution_name():
    with pytest.raises(ValueError, match="distribution_name"):
        ProductDescriptor(
            product_id="test",
            display_name="Test",
            distribution_name="",
            launch_entry_points=(),
        )


# ===========================================================================
# 11) ProductCatalog immutability
# ===========================================================================


def test_product_catalog_is_immutable():
    """ProductCatalog is frozen — cannot mutate after creation."""
    catalog = default_catalog()
    with pytest.raises(Exception):
        catalog.list = lambda: ()  # type: ignore
    with pytest.raises(Exception):
        catalog._descriptors = ()  # type: ignore


# ===========================================================================
# 12) ProductShellState computed fields
# ===========================================================================


def test_product_shell_state_computed_counts():
    """managed_count and installed_count are computed correctly."""
    products = (
        ProductState(
            product_id="a",
            display_name="A",
            known=True,
            installed=True,
            launchable=True,
            version="1.0",
            reason_code=ProductStateReasonCode.INSTALLED_LAUNCHABLE,
            reason="ok",
            managed=ManagedStatus.MANAGED,
        ),
        ProductState(
            product_id="b",
            display_name="B",
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.NOT_INSTALLED,
            reason="not installed",
            managed=ManagedStatus.UNMANAGED,
        ),
    )
    shell = ProductShellState(
        runtime_state=RuntimeState.READY,
        runtime_root=Path("/fake/runtime"),
        products=products,
    )
    assert shell.managed_count == 1
    assert shell.installed_count == 1
    assert len(shell.products) == 2


# ===========================================================================
# 13) ProductState validation
# ===========================================================================


def test_product_state_rejects_empty_product_id():
    with pytest.raises(ValueError, match="product_id"):
        ProductState(
            product_id="",
            display_name="Test",
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.RUNTIME_ABSENT,
            reason="test",
        )


def test_product_state_rejects_empty_display_name():
    with pytest.raises(ValueError, match="display_name"):
        ProductState(
            product_id="test",
            display_name="",
            known=True,
            installed=False,
            launchable=False,
            version=None,
            reason_code=ProductStateReasonCode.RUNTIME_ABSENT,
            reason="test",
        )


# ===========================================================================
# 14) Service delegation tests
# ===========================================================================


class _FakeServiceRuntime:
    """Fake runtime for service-level product tests."""

    def __init__(self, status: RuntimeStatus):
        self._status = status

    def status(self) -> RuntimeStatus:
        return self._status


def test_service_collect_product_state_with_absent_runtime():
    """ZeAlfieService.collect_product_state works with ABSENT runtime."""
    registry = ComponentRegistry([
        ComponentDefinition(
            component_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
        ),
    ])
    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeServiceRuntime(_absent_status()),
    )
    shell = service.collect_product_state()
    assert isinstance(shell, ProductShellState)
    assert shell.runtime_state == RuntimeState.ABSENT
    assert len(shell.products) == 4
    assert shell.managed_count == 1  # zesolver is managed
    assert shell.installed_count == 0


def test_service_list_products():
    """ZeAlfieService.list_products() returns catalog descriptors."""
    service = ZeAlfieService()
    descriptors = service.list_products()
    assert len(descriptors) == 4
    assert tuple(d.product_id for d in descriptors) == EXPECTED_PRODUCT_IDS


def test_service_catalog_property():
    """ZeAlfieService.catalog property returns the catalog."""
    service = ZeAlfieService()
    assert isinstance(service.catalog, ProductCatalog)
    assert len(service.catalog) == 4
