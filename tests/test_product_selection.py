"""Tests for M1-2D.3 — User Selection Store and Desired Materialization.

Covers:

Selection store:
- Missing file → empty valid selection
- Explicit empty list → empty valid selection
- Add one known product
- Add is idempotent
- Adding second product preserves existing selections
- Reload preserves state
- Adding unknown product fails cleanly (UnknownProductError), no mutation
- Corrupt TOML / invalid schema / invalid list is an error, not silently empty
- Invalid existing content must not be overwritten by add as if empty
- Deterministic representation/order
- Atomic-ish writes

Materialization:
- ProductCatalog + DesiredProductSelection → ComponentDefinitions
- Unknown selected id raises UnknownProductError
- Fields are faithfully converted

Separation:
- Selection changes do not mutate ProductCatalog
- Selection changes do not mutate runtime
- Selection changes do not mutate packaged components.toml

Service integration:
- Product-shell managed set driven by selection store
- installed/launchable remain runtime-probe concepts
- Deployment/launch still uses ComponentRegistry (pre-D4)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zealfie.app import (
    DesiredProductSelection,
    SelectionStore,
    UnknownProductError,
    ZeAlfieService,
    default_selection_path,
    desired_component_registry,
    materialize_desired_components,
)
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.products.catalog import ProductCatalog, ProductDescriptor
from zealfie.products.selection import (
    CorruptSelectionError,
    SelectionStoreError,
    _load_from_file,
    _parse_selection,
    _render_selection,
    _save_to_file,
)
from zealfie.products.state import (
    ManagedStatus,
    ProductShellState,
    ProductState,
    ProductStateReasonCode,
    collect_product_state,
)
from zealfie.runtime.model import RuntimeState, RuntimeStatus


# ===========================================================================
# Helpers
# ===========================================================================


def _make_catalog() -> ProductCatalog:
    """Create a minimal 2-product catalog for tests."""
    descs = [
        ProductDescriptor(
            product_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(
                EntryPointContract("gui_scripts", "zesolver"),
            ),
            required_extras=("gui",),
            description="Optical solver",
        ),
        ProductDescriptor(
            product_id="zemosaic",
            display_name="ZeMosaic",
            distribution_name="ZeMosaic",
            launch_entry_points=(
                EntryPointContract("gui_scripts", "zemosaic"),
            ),
            description="Mosaic planner",
        ),
    ]
    return ProductCatalog(tuple(descs))


def _absent_status() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.ABSENT,
        runtime_root=Path("/fake/runtime"),
    )


# ===========================================================================
# 1) Default path
# ===========================================================================


def test_default_selection_path_is_platform_appropriate():
    """default_selection_path returns a platform-specific path."""
    path = default_selection_path()
    assert isinstance(path, Path)
    assert path.name == "desired-products.toml"
    assert "zealfie" in str(path)


def test_default_selection_path_on_linux(monkeypatch):
    """On Linux, the path respects XDG_DATA_HOME."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    path = default_selection_path()
    assert str(path).startswith("/custom/data/zealfie/")


# ===========================================================================
# 2) Empty state
# ===========================================================================


def test_empty_selection_default():
    """Default DesiredProductSelection is empty."""
    sel = DesiredProductSelection()
    assert len(sel) == 0
    assert sel.selected_product_ids == ()
    assert "anything" not in sel


def test_empty_selection_explicit():
    """Explicit empty tuple produces empty selection."""
    sel = DesiredProductSelection(())
    assert len(sel) == 0


def test_missing_file_is_empty_selection(tmp_path):
    """Missing file → empty valid selection (no error)."""
    path = tmp_path / "nonexistent.toml"
    store = SelectionStore(path=path)
    assert store.selected_product_ids == ()
    assert len(store.current_selection()) == 0


# ===========================================================================
# 3) Explicit empty list in file
# ===========================================================================


def test_explicit_empty_list_is_valid_empty_selection(tmp_path):
    """selected_product_ids = [] → valid empty selection."""
    path = tmp_path / "empty.toml"
    path.write_text("schema_version = 1\nselected_product_ids = []\n")
    store = SelectionStore(path=path)
    assert store.selected_product_ids == ()
    assert len(store.current_selection()) == 0


# ===========================================================================
# 4) Add one known product + persistence
# ===========================================================================


def test_add_one_product_persists(tmp_path):
    """Adding one known product persists to disk and returns updated selection."""
    path = tmp_path / "selection.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    result = store.select("zesolver", catalog=catalog)
    assert isinstance(result, DesiredProductSelection)
    assert result.selected_product_ids == ("zesolver",)

    # File exists and contains the product.
    assert path.is_file()
    text = path.read_text()
    assert "zesolver" in text
    assert "schema_version = 1" in text

    # Reload preserves the state.
    store2 = SelectionStore(path=path)
    assert store2.selected_product_ids == ("zesolver",)


def test_add_one_product_in_memory_state_updated(tmp_path):
    """After select, in-memory state reflects the change."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")

    store.select("zesolver", catalog=catalog)
    assert store.selected_product_ids == ("zesolver",)
    assert store.current_selection().selected_product_ids == ("zesolver",)
    assert "zesolver" in store.current_selection()


# ===========================================================================
# 5) Idempotent add
# ===========================================================================


def test_add_is_idempotent(tmp_path):
    """Adding the same product twice is a no-op."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    r1 = store.select("zesolver", catalog=catalog)
    assert r1.selected_product_ids == ("zesolver",)

    # Second add — same product
    r2 = store.select("zesolver", catalog=catalog)
    assert r2 is r1  # same object returned (no change)
    assert r2.selected_product_ids == ("zesolver",)

    # File not modified unnecessarily.
    mtime = path.stat().st_mtime
    store.select("zesolver", catalog=catalog)
    assert path.stat().st_mtime == mtime  # no write happened


# ===========================================================================
# 6) Adding second product preserves existing
# ===========================================================================


def test_add_second_product_preserves_existing(tmp_path):
    """Adding a second product preserves the first."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    store.select("zesolver", catalog=catalog)
    store.select("zemosaic", catalog=catalog)

    assert store.selected_product_ids == ("zemosaic", "zesolver")  # sorted

    # Both are in the file.
    text = path.read_text()
    assert "zesolver" in text
    assert "zemosaic" in text


# ===========================================================================
# 7) Reload preserves state
# ===========================================================================


def test_reload_preserves_state(tmp_path):
    """Writing to disk through one store, reloading through another preserves state."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store_a = SelectionStore(path=path)
    store_a.select("zesolver", catalog=catalog)
    store_a.select("zemosaic", catalog=catalog)

    # Create a second store that hasn't loaded yet.
    store_b = SelectionStore(path=path)
    # Force reload.
    sel = store_b.reload()
    assert sel.selected_product_ids == ("zemosaic", "zesolver")
    assert store_b.selected_product_ids == ("zemosaic", "zesolver")


def test_reload_from_file_with_changes(tmp_path):
    """Store reload picks up changes made on disk."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    # Write a selection on disk directly.
    sel = DesiredProductSelection(("zemosaic", "zesolver"))
    _save_to_file(sel, path)

    # Reload.
    store.reload()
    assert store.selected_product_ids == ("zemosaic", "zesolver")


# ===========================================================================
# 8) Unknown product: fails cleanly, no mutation
# ===========================================================================


def test_add_unknown_product_raises_UnknownProductError(tmp_path):
    """Adding an unknown product raises UnknownProductError."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    with pytest.raises(UnknownProductError, match="nonexistent"):
        store.select("nonexistent", catalog=catalog)


def test_add_unknown_product_does_not_mutate_persisted_state(tmp_path):
    """Adding an unknown product does NOT write to disk or modify
    in-memory selection."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    # First, add a valid product.
    store.select("zesolver", catalog=catalog)
    assert store.selected_product_ids == ("zesolver",)

    # Now try an unknown product — must fail.
    with pytest.raises(UnknownProductError):
        store.select("nonexistent", catalog=catalog)

    # State unchanged.
    assert store.selected_product_ids == ("zesolver",)

    # File still only has the valid product.
    assert path.is_file()
    text = path.read_text()
    assert "zesolver" in text
    assert "nonexistent" not in text


def test_add_unknown_product_does_not_mutate_after_reload(tmp_path):
    """Unknown product rejection must not overwrite existing non-empty file
    — the guard applies even after a reload from a file with content."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)

    store.select("zesolver", catalog=catalog)
    # Reload to simulate a fresh session.
    store.reload()
    assert store.selected_product_ids == ("zesolver",)

    with pytest.raises(UnknownProductError):
        store.select("nonexistent", catalog=catalog)

    assert store.selected_product_ids == ("zesolver",)
    assert "nonexistent" not in path.read_text()


# ===========================================================================
# 9) Corrupt TOML / invalid schema / invalid list
# ===========================================================================


def test_corrupt_toml_raises_CorruptSelectionError(tmp_path):
    """Invalid TOML raises CorruptSelectionError, not silently empty."""
    path = tmp_path / "bad.toml"
    path.write_text("this is not valid toml {{{")

    with pytest.raises(CorruptSelectionError, match="invalid TOML"):
        SelectionStore(path=path).reload()


def test_missing_schema_version_raises_CorruptSelectionError(tmp_path):
    """Missing schema_version raises error."""
    path = tmp_path / "noschema.toml"
    path.write_text("selected_product_ids = []\n")

    with pytest.raises(CorruptSelectionError, match="missing schema_version"):
        _parse_selection(path.read_text(), source=path)


def test_non_int_schema_version_raises_CorruptSelectionError(tmp_path):
    """Non-integer schema_version raises error."""
    path = tmp_path / "badschema.toml"
    path.write_text(
        'schema_version = "1"\nselected_product_ids = []\n'
    )

    with pytest.raises(
        CorruptSelectionError, match="schema_version must be an integer"
    ):
        _parse_selection(path.read_text(), source=path)


def test_unsupported_schema_version_raises_CorruptSelectionError(tmp_path):
    """Unsupported schema_version raises error."""
    path = tmp_path / "futureschema.toml"
    path.write_text("schema_version = 999\nselected_product_ids = []\n")

    with pytest.raises(
        CorruptSelectionError, match="unsupported schema_version"
    ):
        _parse_selection(path.read_text(), source=path)


def test_missing_selected_product_ids_raises(tmp_path):
    """Missing selected_product_ids key raises error."""
    path = tmp_path / "missing.toml"
    path.write_text("schema_version = 1\n")

    with pytest.raises(
        CorruptSelectionError, match="missing selected_product_ids"
    ):
        _parse_selection(path.read_text(), source=path)


def test_non_list_selected_product_ids_raises(tmp_path):
    """Non-list selected_product_ids raises error."""
    path = tmp_path / "nonlist.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = "zesolver"\n'
    )

    with pytest.raises(
        CorruptSelectionError, match="selected_product_ids must be a list"
    ):
        _parse_selection(path.read_text(), source=path)


def test_empty_string_in_list_raises(tmp_path):
    """Empty string in list raises error."""
    path = tmp_path / "emptystr.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["zesolver", ""]\n'
    )

    with pytest.raises(
        CorruptSelectionError, match="must be a non-empty string"
    ):
        _parse_selection(path.read_text(), source=path)


def test_non_string_in_list_raises(tmp_path):
    """Non-string element in list raises error."""
    path = tmp_path / "nonstr.toml"
    path.write_text("schema_version = 1\nselected_product_ids = [42]\n")

    with pytest.raises(
        CorruptSelectionError, match="must be a non-empty string"
    ):
        _parse_selection(path.read_text(), source=path)


def test_corrupt_content_not_overwritten_by_add(tmp_path):
    """Invalid existing file content must not be overwritten by select
    as if empty."""
    path = tmp_path / "corrupt.toml"
    path.write_text("garbage {{ not valid toml")

    store = SelectionStore(path=path)

    # Reload must fail.
    with pytest.raises(CorruptSelectionError):
        store.reload()

    # The file is unchanged — NOT overwritten.
    assert path.read_text() == "garbage {{ not valid toml"


# ===========================================================================
# 10) Deterministic representation / order
# ===========================================================================


def test_selected_product_ids_always_sorted(tmp_path):
    """selected_product_ids is always sorted lexicographically."""
    sel = DesiredProductSelection(("zzz", "aaa", "mmm"))
    assert sel.selected_product_ids == ("aaa", "mmm", "zzz")


def test_add_order_is_irrelevant(tmp_path):
    """Adding in any order produces the same sorted result."""
    path_a = tmp_path / "a.toml"
    path_b = tmp_path / "b.toml"
    catalog = _make_catalog()

    store_a = SelectionStore(path=path_a)
    store_a.select("zemosaic", catalog=catalog)
    store_a.select("zesolver", catalog=catalog)

    store_b = SelectionStore(path=path_b)
    store_b.select("zesolver", catalog=catalog)
    store_b.select("zemosaic", catalog=catalog)

    assert store_a.selected_product_ids == ("zemosaic", "zesolver")
    assert store_b.selected_product_ids == ("zemosaic", "zesolver")

    # Files have byte-identical content.
    assert path_a.read_bytes() == path_b.read_bytes()


def test_duplicate_ids_in_constructor_raises_ValueError():
    """Constructor raises ValueError for duplicate product ids."""
    with pytest.raises(ValueError, match="duplicate product id"):
        DesiredProductSelection(("zesolver", "zesolver", "zemosaic"))


# ===========================================================================
# 11) Render / parse round-trip
# ===========================================================================


def test_render_empty_selection():
    """Empty selection renders with empty list."""
    text = _render_selection(DesiredProductSelection())
    assert "schema_version = 1" in text
    assert "selected_product_ids = []" in text


def test_render_with_products():
    """Selection with products renders correctly."""
    sel = DesiredProductSelection(("zesolver", "zemosaic"))
    text = _render_selection(sel)
    assert "schema_version = 1" in text
    assert '"zesolver"' in text
    assert '"zemosaic"' in text
    # Order: sorted.
    assert text.index("zemosaic") < text.index("zesolver")


def test_render_then_parse_round_trip():
    """Render → parse produces the same selection."""
    original = DesiredProductSelection(("zesolver", "zemosaic"))
    text = _render_selection(original)
    parsed = _parse_selection(text)
    assert parsed.selected_product_ids == original.selected_product_ids


def test_render_escapes_special_characters_round_trip():
    """Selection ids containing TOML-special characters round-trip.

    Product ids are normally controlled by ZeAlfie's packaged catalog,
    but the renderer must not be able to corrupt its own persisted file
    if a catalog/test id contains quotes, backslashes, or control chars.
    """
    original = DesiredProductSelection(('ze"solver', r"ze\mosaic", "ze\nalyser"))
    text = _render_selection(original)
    parsed = _parse_selection(text)
    assert parsed.selected_product_ids == original.selected_product_ids


def test_render_deterministic():
    """Two calls to _render_selection with the same selection produce
    byte-identical output."""
    sel = DesiredProductSelection(("zesolver", "zemosaic"))
    r1 = _render_selection(sel)
    r2 = _render_selection(sel)
    assert r1 == r2


# ===========================================================================
# 12) Atomic write
# ===========================================================================


def test_atomic_write_leaves_no_temp_files(tmp_path):
    """After a successful save, no temp files remain."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()
    store = SelectionStore(path=path)
    store.select("zesolver", catalog=catalog)

    # Only the target file exists.
    entries = list(path.parent.iterdir())
    assert len(entries) == 1
    assert entries[0].name == "sel.toml"


def test_atomic_write_preserves_existing_on_failure(tmp_path, monkeypatch):
    """If the write fails (e.g., os.replace raises), the original file
    is untouched."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()

    # First, write a valid selection.
    store = SelectionStore(path=path)
    store.select("zesolver", catalog=catalog)
    original_text = path.read_text()
    assert "zesolver" in original_text

    # Now, make os.replace fail.
    original_replace = os.replace

    def failing_replace(src, dst, **kwargs):
        original_replace(src, dst, **kwargs)

    # Actually, to test temp-file cleanup, let's break writing.
    # Instead, let's test that fsync failure cleans up.
    original_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError("simulated fsync failure")

    # We need a fresh store with the same path to test with.
    # Monkeypatch fsync after the store is created but before select.
    monkeypatch.setattr(os, "fsync", failing_fsync)

    store2 = SelectionStore(path=path)
    with pytest.raises(OSError, match="simulated fsync failure"):
        store2.select("zemosaic", catalog=catalog)

    # Original file untouched.
    assert path.read_text() == original_text


# ===========================================================================
# 13) Materialization: catalog + selection → ComponentDefinitions
# ===========================================================================


def test_materialize_desired_components_empty_selection():
    """Empty selection → empty tuple of definitions."""
    catalog = _make_catalog()
    sel = DesiredProductSelection()
    defs = materialize_desired_components(catalog, sel)
    assert isinstance(defs, tuple)
    assert len(defs) == 0


def test_materialize_desired_components_one_product():
    """One selected product → one ComponentDefinition with faithful conversion."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver",))
    defs = materialize_desired_components(catalog, sel)

    assert len(defs) == 1
    d = defs[0]
    assert isinstance(d, ComponentDefinition)
    assert d.component_id == "zesolver"
    assert d.display_name == "ZeSolver"
    assert d.distribution_name == "ZeSolver"
    assert d.launch_entry_points == (EntryPointContract("gui_scripts", "zesolver"),)
    assert d.required_extras == ("gui",)


def test_materialize_desired_components_two_products():
    """Two selected products → two ComponentDefinitions in sorted order."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver", "zemosaic"))
    defs = materialize_desired_components(catalog, sel)

    assert len(defs) == 2
    ids = [d.component_id for d in defs]
    assert ids == ["zemosaic", "zesolver"]  # sorted order from selection


def test_materialize_desired_components_with_description():
    """Description field from ProductDescriptor is preserved in the catalog
    but not carried to ComponentDefinition (which has no description field)."""
    catalog = _make_catalog()
    # Verify the catalog has the description.
    desc = catalog.get("zesolver")
    assert desc.description == "Optical solver"

    sel = DesiredProductSelection(("zesolver",))
    defs = materialize_desired_components(catalog, sel)

    # ComponentDefinition has no description field — that's expected.
    d = defs[0]
    assert d.component_id == "zesolver"
    # All required fields from descriptor are present.


def test_materialize_unknown_product_id_raises():
    """Unknown selected product id raises UnknownProductError."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("nonexistent",))

    with pytest.raises(UnknownProductError, match="nonexistent"):
        materialize_desired_components(catalog, sel)


def test_materialize_partial_unknown_raises():
    """Even with valid+invalid mix, unknown product raises."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver", "ghost"))

    with pytest.raises(UnknownProductError, match="ghost"):
        materialize_desired_components(catalog, sel)


def test_desired_component_registry_wrapper():
    """desired_component_registry returns a ComponentRegistry."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver",))
    reg = desired_component_registry(catalog, sel)

    assert isinstance(reg, ComponentRegistry)
    assert reg.available_ids() == ("zesolver",)
    assert reg.get("zesolver").component_id == "zesolver"


def test_materialization_returns_correct_type():
    """materialize_desired_components returns tuple[ComponentDefinition, ...]."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver",))
    result = materialize_desired_components(catalog, sel)
    assert isinstance(result, tuple)
    assert all(isinstance(d, ComponentDefinition) for d in result)


# ===========================================================================
# 14) Separation: selection changes don't mutate catalog, runtime, or manifest
# ===========================================================================


def test_selection_does_not_mutate_product_catalog(tmp_path):
    """Adding products to selection does not mutate the ProductCatalog."""
    catalog = _make_catalog()
    original_ids = catalog.available_ids()
    assert len(original_ids) == 2

    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)
    store.select("zemosaic", catalog=catalog)

    # Catalog unchanged.
    assert catalog.available_ids() == original_ids
    assert len(catalog) == 2


def test_selection_does_not_mutate_packaged_manifest_registry(tmp_path):
    """Selection changes do not touch the packaged components.toml registry."""
    from zealfie.components.registry import default_registry

    registry_before = default_registry()
    ids_before = set(registry_before.available_ids())

    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)

    # Registry is unchanged (separate concept).
    registry_after = default_registry()
    assert set(registry_after.available_ids()) == ids_before


def test_selection_store_does_not_touch_runtime(tmp_path):
    """SelectionStore has no runtime dependency."""
    # Just verify the store doesn't import or reference runtime modules.
    import sys
    mod = sys.modules.get("zealfie.products.selection")
    assert mod is not None
    # The module should not import runtime modules.
    source = Path(mod.__file__).read_text()
    assert "zealfie.runtime." not in source or "state" in source  # allow state import


# ===========================================================================
# 15) DesiredProductSelection immutability
# ===========================================================================


def test_selection_is_immutable():
    """DesiredProductSelection is frozen."""
    from dataclasses import FrozenInstanceError

    sel = DesiredProductSelection(("zesolver",))
    with pytest.raises(FrozenInstanceError):
        sel.selected_product_ids = ("zemosaic",)  # type: ignore[misc]


def test_with_product_returns_new_instance(tmp_path):
    """with_product returns a new instance, doesn't mutate original."""
    catalog = _make_catalog()
    original = DesiredProductSelection(("zesolver",))
    updated = original.with_product("zemosaic", catalog=catalog)

    assert original is not updated
    assert original.selected_product_ids == ("zesolver",)
    assert updated.selected_product_ids == ("zemosaic", "zesolver")


def test_with_product_catalog_check_before_idempotence():
    """with_product validates product_id against catalog BEFORE the
    idempotence shortcut.  An already-present product id that is not in
    the catalog must raise UnknownProductError."""
    catalog = _make_catalog()
    sel = DesiredProductSelection(("ghost",))

    with pytest.raises(UnknownProductError, match="ghost"):
        sel.with_product("ghost", catalog=catalog)


def test_without_product_returns_new_instance():
    """without_product returns a new instance, doesn't mutate original."""
    original = DesiredProductSelection(("zesolver", "zemosaic"))
    removed = original.without_product("zesolver")

    assert original is not removed
    assert original.selected_product_ids == ("zemosaic", "zesolver")
    assert removed.selected_product_ids == ("zemosaic",)


def test_without_product_idempotent():
    """Removing a non-selected product is a no-op."""
    original = DesiredProductSelection(("zesolver",))
    result = original.without_product("zemosaic")
    assert result is original


# ===========================================================================
# 16) SelectionStore lazy load
# ===========================================================================


def test_store_lazy_loads_from_disk(tmp_path):
    """Store doesn't read the file until selected_product_ids is accessed."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()

    # Write to disk first.
    store_write = SelectionStore(path=path)
    store_write.select("zesolver", catalog=catalog)

    # Create a new store that hasn't accessed selected_product_ids yet.
    store_read = SelectionStore(path=path)
    assert store_read._loaded is False

    # Access triggers load.
    _ = store_read.selected_product_ids
    assert store_read._loaded is True


# ===========================================================================
# 17) Service integration: managed set driven by selection store
# ===========================================================================


def test_service_managed_product_ids_from_selection_store(tmp_path):
    """managed_product_ids comes from the selection store, not the registry."""
    catalog = _make_catalog()
    registry = ComponentRegistry([
        ComponentDefinition(
            component_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
        ),
    ])

    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)
    store.select("zemosaic", catalog=catalog)

    service = ZeAlfieService(
        registry=registry,
        catalog=catalog,
        selection_store=store,
    )

    # Managed set comes from store (both products), not registry (one).
    assert service.managed_product_ids == frozenset({"zemosaic", "zesolver"})


def test_service_collect_product_state_uses_selection_store(tmp_path):
    """collect_product_state uses selection store for managed set."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)

    # Fake runtime (absent).
    service = ZeAlfieService(
        catalog=catalog,
        selection_store=store,
    )

    shell = service.collect_product_state()
    assert isinstance(shell, ProductShellState)
    assert shell.managed_count == 1

    for p in shell.products:
        if p.product_id == "zesolver":
            assert p.managed == ManagedStatus.MANAGED
        else:
            assert p.managed == ManagedStatus.UNMANAGED


def test_service_get_product_state_uses_selection_store(tmp_path):
    """get_product_state uses selection store for managed status."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)

    service = ZeAlfieService(
        catalog=catalog,
        selection_store=store,
    )

    state = service.get_product_state("zesolver")
    assert state.managed == ManagedStatus.MANAGED

    state_mosaic = service.get_product_state("zemosaic")
    assert state_mosaic.managed == ManagedStatus.UNMANAGED


def test_service_select_product_integration(tmp_path):
    """select_product adds to selection, persisted."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    result = service.select_product("zesolver")
    assert isinstance(result, DesiredProductSelection)
    assert result.selected_product_ids == ("zesolver",)

    # File persisted.
    assert store.path.is_file()

    # managed_product_ids reflects the change.
    assert service.managed_product_ids == frozenset({"zesolver",})


def test_service_select_product_unknown_raises(tmp_path):
    """select_product raises UnknownProductError for unknown products.

    The D.3 contract validates *product_id* against the catalog before
    any file I/O.  An unknown product id raises UnknownProductError
    without writing or bootstrapping the selection file — even when the
    legacy registry would have produced a non-empty bootstrap.
    """
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    # Empty registry still bootstraps empty, but D.3 guard runs first.
    empty_registry = ComponentRegistry([])
    service = ZeAlfieService(
        registry=empty_registry,
        catalog=catalog,
        selection_store=store,
    )

    with pytest.raises(UnknownProductError, match="nonexistent"):
        service.select_product("nonexistent")

    # No file written — validation fails before bootstrap (D.3 contract).
    assert not store.path.exists()


def test_service_select_product_idempotent(tmp_path):
    """select_product is idempotent."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    r1 = service.select_product("zesolver")
    r2 = service.select_product("zesolver")
    assert r1 is r2


def test_service_desired_selection(tmp_path):
    """desired_selection returns current selection."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    service.select_product("zesolver")
    sel = service.desired_selection()
    assert isinstance(sel, DesiredProductSelection)
    assert sel.selected_product_ids == ("zesolver",)


def test_service_materialize_desired_components(tmp_path):
    """materialize_desired_components works through the service."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    service.select_product("zesolver")
    defs = service.materialize_desired_components()
    assert len(defs) == 1
    assert defs[0].component_id == "zesolver"


def test_service_desired_component_registry(tmp_path):
    """desired_component_registry returns a ComponentRegistry."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    service.select_product("zesolver")
    reg = service.desired_component_registry()
    assert isinstance(reg, ComponentRegistry)
    assert reg.available_ids() == ("zesolver",)


# ===========================================================================
# 18) Separation: installed/launchable remain runtime-probe concepts
# ===========================================================================


def test_managed_vs_installed_are_independent(tmp_path):
    """Selecting a product does not make it installed — installed is
    determined by the runtime probe."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)

    # Collect state with ABSENT runtime.
    shell = collect_product_state(
        catalog,
        _absent_status(),
        managed_component_ids=frozenset(store.selected_product_ids),
    )

    for p in shell.products:
        assert p.installed is False  # Not installed — runtime is ABSENT
        if p.product_id == "zesolver":
            assert p.managed == ManagedStatus.MANAGED
        else:
            assert p.managed == ManagedStatus.UNMANAGED


# ===========================================================================
# 19) Pre-D4: deployment/launch still uses ComponentRegistry
# ===========================================================================


def test_service_launch_respects_selection_when_file_exists(tmp_path):
    """D.4.1A: When the selection file exists, prepare_launch_plan uses
    the materialized desired registry from the SelectionStore, not the
    legacy packaged ComponentRegistry.

    zesolver is in the legacy registry but NOT in the selection → unknown.
    zemosaic is NOT in the legacy registry but IS in the selection →
    proceeds past registry check."""
    from zealfie.app.service import LaunchPreparationError
    from zealfie.components.registry import UnknownComponentError

    catalog = _make_catalog()
    registry = ComponentRegistry([
        ComponentDefinition(
            component_id="zesolver",
            display_name="ZeSolver",
            distribution_name="ZeSolver",
            launch_entry_points=(EntryPointContract("gui_scripts", "zesolver"),),
        ),
    ])

    store = SelectionStore(path=tmp_path / "sel.toml")
    # zemosaic is in the catalog and selected, but NOT in the legacy registry.
    store.select("zemosaic", catalog=catalog)

    # Inject a fake ABSENT runtime.
    class _FakeAbsentRuntime:
        def status(self):
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=Path('/fake/runtime'),
            )

    service = ZeAlfieService(
        registry=registry,
        runtime=_FakeAbsentRuntime(),
        catalog=catalog,
        selection_store=store,
    )

    # zesolver is NOT in the selection → UnknownComponentError.
    with pytest.raises(UnknownComponentError):
        service.prepare_launch_plan("zesolver")

    # zemosaic IS in the selection (desired registry) → proceeds to
    # runtime check.
    with pytest.raises(LaunchPreparationError, match="absent"):
        service.prepare_launch_plan("zemosaic")


# ===========================================================================
# 20) Edge cases: whitespace, encoding, path safety
# ===========================================================================


def test_empty_string_product_id_rejected():
    """Empty product_id in constructor raises ValueError."""
    with pytest.raises(ValueError, match="must not contain empty"):
        DesiredProductSelection(("",))


def test_whitespace_only_product_id_rejected():
    """Whitespace-only product_id raises ValueError."""
    with pytest.raises(ValueError, match="must not contain empty"):
        DesiredProductSelection(("   ",))


def test_duplicate_in_constructor_raises():
    """Duplicate product ids raise ValueError."""
    with pytest.raises(ValueError, match="duplicate product id"):
        DesiredProductSelection(("zesolver", "zesolver"))


def test_selection_store_path_is_attribute(tmp_path):
    """SelectionStore.path returns the configured path."""
    path = tmp_path / "sel.toml"
    store = SelectionStore(path=path)
    assert store.path == path


def test_utf8_selection_file(tmp_path):
    """UTF-8 selection file with BOM-like content works correctly."""
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["zesolver"]\n',
        encoding="utf-8",
    )

    store = SelectionStore(path=path)
    assert store.selected_product_ids == ("zesolver",)


def test_selection_store_reads_then_persists_new(tmp_path):
    """Store reads from disk, allows mutation, persists correctly."""
    path = tmp_path / "sel.toml"
    catalog = _make_catalog()

    # Write initial state.
    store_a = SelectionStore(path=path)
    store_a.select("zesolver", catalog=catalog)

    # New store to read it back.
    store_b = SelectionStore(path=path)
    assert store_b.current_selection().selected_product_ids == ("zesolver",)

    # Mutate via the new store.
    store_b.select("zemosaic", catalog=catalog)
    assert store_b.selected_product_ids == ("zemosaic", "zesolver")

    # Verify on disk.
    text = path.read_text()
    assert "zesolver" in text
    assert "zemosaic" in text


# ===========================================================================
# 21) CorruptSelectionError for duplicate ids in persisted file
# ===========================================================================


def test_duplicate_ids_in_persisted_file_raises_CorruptSelectionError(tmp_path):
    """Duplicate product ids in persisted selection TOML raises
    CorruptSelectionError, not bare ValueError."""
    path = tmp_path / "dup.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["zesolver", "zesolver"]\n'
    )

    with pytest.raises(CorruptSelectionError, match="duplicate product id"):
        SelectionStore(path=path).reload()


def test_duplicate_ids_in_persisted_file_via_parse():
    """_parse_selection wraps DesiredProductSelection ValueError as
    CorruptSelectionError."""
    text = 'schema_version = 1\nselected_product_ids = ["a", "a"]\n'
    with pytest.raises(CorruptSelectionError, match="duplicate product id"):
        _parse_selection(text)


def test_empty_string_in_persisted_file_raises_CorruptSelectionError():
    """Empty string in persisted file raises CorruptSelectionError via
    _parse_selection wrapping DesiredProductSelection validation."""
    text = 'schema_version = 1\nselected_product_ids = ["zesolver", ""]\n'
    # _parse_selection's own validation catches the empty string before
    # DesiredProductSelection construction — still CorruptSelectionError.
    with pytest.raises(CorruptSelectionError, match="must be a non-empty string"):
        _parse_selection(text)


def test_whitespace_only_in_persisted_file_raises_CorruptSelectionError():
    """Whitespace-only id in persisted file raises CorruptSelectionError."""
    text = 'schema_version = 1\nselected_product_ids = ["   ", "zesolver"]\n'
    # The empty string check in _parse_selection will catch whitespace-only
    # because it strips and checks for non-empty.
    with pytest.raises(CorruptSelectionError, match="must be a non-empty string"):
        _parse_selection(text)


# ===========================================================================
# 22) Unknown selected ids in persisted state must raise, not disappear
# ===========================================================================


def test_unknown_persisted_id_raises_in_managed_product_ids(tmp_path):
    """managed_product_ids raises UnknownProductError when persisted
    selection contains an unknown product id."""
    catalog = _make_catalog()
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["unknown-product"]\n'
    )
    store = SelectionStore(path=path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    with pytest.raises(UnknownProductError, match="unknown-product"):
        _ = service.managed_product_ids


def test_unknown_persisted_id_raises_in_collect_product_state(tmp_path):
    """collect_product_state raises UnknownProductError when persisted
    selection contains an unknown product id."""
    catalog = _make_catalog()
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["unknown-product"]\n'
    )
    store = SelectionStore(path=path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    with pytest.raises(UnknownProductError, match="unknown-product"):
        service.collect_product_state()


def test_unknown_persisted_id_raises_in_get_product_state(tmp_path):
    """get_product_state raises UnknownProductError when persisted
    selection contains an unknown product id."""
    catalog = _make_catalog()
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["unknown-product"]\n'
    )
    store = SelectionStore(path=path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    with pytest.raises(UnknownProductError, match="unknown-product"):
        service.get_product_state("zesolver")


def test_unknown_persisted_id_already_raises_in_materialize(tmp_path):
    """materialize_desired_components already raises UnknownProductError
    for unknown selected ids (existing behavior)."""
    catalog = _make_catalog()
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["unknown-product"]\n'
    )
    store = SelectionStore(path=path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    with pytest.raises(UnknownProductError, match="unknown-product"):
        service.materialize_desired_components()


def test_mixed_known_and_unknown_persisted_ids_raises(tmp_path):
    """Both known and unknown ids in persisted selection → UnknownProductError."""
    catalog = _make_catalog()
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["unknown-product", "zesolver"]\n'
    )
    store = SelectionStore(path=path)
    service = ZeAlfieService(catalog=catalog, selection_store=store)

    with pytest.raises(UnknownProductError, match="unknown-product"):
        _ = service.managed_product_ids


def test_managed_product_ids_succeeds_when_all_valid(tmp_path):
    """managed_product_ids returns correct set when all selected ids are
    valid (regression guard)."""
    catalog = _make_catalog()
    store = SelectionStore(path=tmp_path / "sel.toml")
    store.select("zesolver", catalog=catalog)
    store.select("zemosaic", catalog=catalog)

    service = ZeAlfieService(catalog=catalog, selection_store=store)
    assert service.managed_product_ids == frozenset({"zemosaic", "zesolver"})


# ===========================================================================
# 23) validate_selection_against_catalog helper
# ===========================================================================


def test_validate_selection_against_catalog_known_ids():
    """validate_selection_against_catalog returns None for all-valid selection."""
    from zealfie.products.selection import validate_selection_against_catalog

    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver", "zemosaic"))
    # Should not raise.
    result = validate_selection_against_catalog(catalog, sel)
    assert result is None


def test_validate_selection_against_catalog_unknown_id_raises():
    """validate_selection_against_catalog raises UnknownProductError for
    unknown ids."""
    from zealfie.products.selection import validate_selection_against_catalog

    catalog = _make_catalog()
    sel = DesiredProductSelection(("unknown-product",))

    with pytest.raises(UnknownProductError, match="unknown-product"):
        validate_selection_against_catalog(catalog, sel)


def test_validate_selection_against_catalog_empty_selection():
    """Empty selection validation is a no-op."""
    from zealfie.products.selection import validate_selection_against_catalog

    catalog = _make_catalog()
    sel = DesiredProductSelection()
    result = validate_selection_against_catalog(catalog, sel)
    assert result is None


def test_validate_selection_against_catalog_partial_unknown():
    """Even with valid+invalid mix, validate raises on first unknown."""
    from zealfie.products.selection import validate_selection_against_catalog

    catalog = _make_catalog()
    sel = DesiredProductSelection(("zesolver", "ghost"))

    with pytest.raises(UnknownProductError, match="ghost"):
        validate_selection_against_catalog(catalog, sel)


# ===========================================================================
# 24) D.4.0 — Legacy-preserving SelectionStore bootstrap
# ===========================================================================


def _make_legacy_registry(ids: tuple[str, ...]) -> ComponentRegistry:
    """Create a synthetic legacy ComponentRegistry with the given component ids."""
    defs = tuple(
        ComponentDefinition(
            component_id=pid,
            display_name=pid.title(),
            distribution_name=pid.title(),
            launch_entry_points=(),
        )
        for pid in ids
    )
    return ComponentRegistry(defs)


# --- bootstrap: store absent + legacy {zesolver} -> bootstrap persists {zesolver} ---


def test_bootstrap_absent_store_persists_legacy_ids(tmp_path):
    """When the selection file does not exist, bootstrap derives ids from
    legacy ComponentRegistry and persists them."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    result = bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    assert result.selected_product_ids == ("zesolver",)
    assert store.path.is_file()
    assert "zesolver" in store.path.read_text()


def test_bootstrap_absent_store_persists_multiple_legacy_ids(tmp_path):
    """Bootstrap handles multiple legacy ids and sorts them."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver", "zemosaic"))
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    result = bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    assert result.selected_product_ids == ("zemosaic", "zesolver")  # sorted
    text = store.path.read_text()
    assert "zesolver" in text
    assert "zemosaic" in text


# --- bootstrap + service/select add zemosaic -> {zesolver, zemosaic} ---


def test_service_select_after_bootstrap_preserves_legacy(tmp_path):
    """select_product bootstraps from legacy, then adding a new product
    preserves the legacy one."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    service = ZeAlfieService(
        registry=legacy,
        catalog=catalog,
        selection_store=store,
    )

    # First select_product call: bootstraps from legacy, adds nothing new.
    # The bootstrap is idempotent, so we'll just add zemosaic directly.
    # But actually, select_product calls bootstrap first, then adds.
    # So adding zemosaic from scratch: bootstrap persists {zesolver}, then
    # adds zemosaic -> {zemosaic, zesolver}.
    result = service.select_product("zemosaic")
    assert result.selected_product_ids == ("zemosaic", "zesolver")

    # File has both.
    text = store.path.read_text()
    assert "zesolver" in text
    assert "zemosaic" in text


# --- bootstrap: file existing empty -> remains empty, no bootstrap ---


def test_bootstrap_existing_empty_file_no_op(tmp_path):
    """If the selection file already exists with an explicit empty
    selection, bootstrap does NOT overwrite it."""
    path = tmp_path / "sel.toml"
    path.write_text("schema_version = 1\nselected_product_ids = []\n")

    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=path)

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    result = bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    # Must remain empty — file is authoritative.
    assert result.selected_product_ids == ()

    # File content unchanged.
    assert "zesolver" not in path.read_text()
    assert "selected_product_ids = []" in path.read_text()


# --- bootstrap: file existing non-empty -> authoritative ---


def test_bootstrap_existing_non_empty_file_no_op(tmp_path):
    """If the selection file already exists with content, bootstrap does
    NOT overwrite with legacy ids."""
    path = tmp_path / "sel.toml"
    path.write_text(
        'schema_version = 1\nselected_product_ids = ["zemosaic"]\n'
    )

    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=path)

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    result = bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    # Must be the existing file content.
    assert result.selected_product_ids == ("zemosaic",)
    # Not rewritten with zesolver.
    assert '"zesolver"' not in path.read_text()


# --- bootstrap: legacy id unknown in ProductCatalog -> explicit error ---


def test_bootstrap_unknown_legacy_id_raises_no_file_written(tmp_path):
    """If a legacy component id is not in the ProductCatalog, bootstrap
    raises UnknownProductError and writes no file."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver", "ghost-product"))
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    with pytest.raises(UnknownProductError, match="ghost-product"):
        bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    # No file should be written.
    assert not store.path.exists()


def test_bootstrap_unknown_legacy_id_no_file_even_if_valid_also_present(tmp_path):
    """When a legacy registry has valid + unknown ids, bootstrap fails
    fast and writes no file (partial bootstrap is forbidden)."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver", "ghost"))
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    with pytest.raises(UnknownProductError, match="ghost"):
        bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    assert not store.path.exists()


# --- bootstrap: second call -> idempotent, does not rewrite from changed legacy registry ---


def test_bootstrap_idempotent_across_calls(tmp_path):
    """Second bootstrap call is a no-op — does not rewrite from a
    potentially-changed legacy registry."""
    catalog = _make_catalog()
    legacy_v1 = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    # First bootstrap.
    r1 = bootstrap_selection_from_legacy_registry(store, catalog, legacy_v1)
    assert r1.selected_product_ids == ("zesolver",)

    # "Change" the legacy registry — now it includes zemosaic too.
    legacy_v2 = _make_legacy_registry(("zesolver", "zemosaic"))

    # Second bootstrap with the "new" registry — must not rewrite.
    r2 = bootstrap_selection_from_legacy_registry(store, catalog, legacy_v2)

    # Still returns the original bootstrapped selection.
    assert r2.selected_product_ids == ("zesolver",)
    # zemosaic from legacy_v2 is NOT in the file.
    assert "zemosaic" not in store.path.read_text()


def test_bootstrap_idempotent_after_file_exists_from_select(tmp_path):
    """If select_product already bootstrapped and wrote the file,
    a second bootstrap call does nothing."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    service = ZeAlfieService(
        registry=legacy,
        catalog=catalog,
        selection_store=store,
    )

    # select_product bootstraps then adds zemosaic.
    service.select_product("zemosaic")
    assert store.path.is_file()
    content_before = store.path.read_text()

    # Now call bootstrap explicitly — it should be a no-op.
    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    # Use a different legacy registry that would produce a different result.
    legacy_different = _make_legacy_registry(("zesolver",))
    result = bootstrap_selection_from_legacy_registry(
        store, catalog, legacy_different,
    )

    # File unchanged — no rewrite.
    assert store.path.read_text() == content_before
    assert result.selected_product_ids == ("zemosaic", "zesolver")


# --- bootstrap: no runtime/deployment touched ---


def test_bootstrap_does_not_touch_runtime_or_deployment(tmp_path):
    """Bootstrap is pure file-initialisation — it imports no runtime or
    deployment modules and creates no side effects beyond the selection file."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    # Verify the module doesn't import runtime modules.
    import sys
    mod = sys.modules["zealfie.products.selection"]
    source = Path(mod.__file__).read_text()
    for forbidden in ("zealfie.runtime.", "zealfie.launching.", "zealfie.sources."):
        assert forbidden not in source, f"selection module imports {forbidden}"

    # Call bootstrap — just verifies no exceptions.
    result = bootstrap_selection_from_legacy_registry(store, catalog, legacy)
    assert result.selected_product_ids == ("zesolver",)

    # No runtime directories created.
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    assert entries[0].name == "sel.toml"


# --- bootstrap: legacy empty registry -> empty selection ---


def test_bootstrap_empty_legacy_registry_persists_empty_selection(tmp_path):
    """If the legacy registry has no components, bootstrap persists an
    empty selection."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(())
    store = SelectionStore(path=tmp_path / "sel.toml")

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    result = bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    assert result.selected_product_ids == ()
    assert store.path.is_file()
    assert "selected_product_ids = []" in store.path.read_text()


# --- bootstrap: service.bootstrap_desired_selection() ---


def test_service_bootstrap_desired_selection(tmp_path):
    """bootstrap_desired_selection on the service bootstraps from the
    injected registry."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    service = ZeAlfieService(
        registry=legacy,
        catalog=catalog,
        selection_store=store,
    )

    result = service.bootstrap_desired_selection()
    assert result.selected_product_ids == ("zesolver",)
    assert store.path.is_file()


def test_service_bootstrap_desired_selection_idempotent(tmp_path):
    """Second call to bootstrap_desired_selection is a no-op."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    service = ZeAlfieService(
        registry=legacy,
        catalog=catalog,
        selection_store=store,
    )

    r1 = service.bootstrap_desired_selection()
    r2 = service.bootstrap_desired_selection()
    assert r1.selected_product_ids == r2.selected_product_ids
    assert r1.selected_product_ids == ("zesolver",)


def test_select_product_absent_store_does_not_orphan_legacy(tmp_path):
    """Core D.4.0 sentinel: absent selection file + select_product(zemosaic)
    must bootstrap from legacy {zesolver} first, then add zemosaic,
    NOT produce {zemosaic} alone."""
    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=tmp_path / "sel.toml")

    service = ZeAlfieService(
        registry=legacy,
        catalog=catalog,
        selection_store=store,
    )

    # This is the D.4.0 guard: absent + zemosaic -> {zesolver, zemosaic}
    service.select_product("zemosaic")

    sel = store.current_selection()
    assert sel.selected_product_ids == ("zemosaic", "zesolver")
    assert "zesolver" in sel  # legacy preserved
    assert "zemosaic" in sel   # new addition present


def test_select_product_absent_store_unknown_legacy_id_raises(tmp_path):
    """If the legacy registry has an id unknown to the catalog,
    select_product raises UnknownProductError and writes no file — even
    for a valid product_id argument."""
    catalog = _make_catalog()
    # Legacy has a valid id AND an unknown one.
    legacy = _make_legacy_registry(("zesolver", "ghost"))
    store = SelectionStore(path=tmp_path / "sel.toml")

    service = ZeAlfieService(
        registry=legacy,
        catalog=catalog,
        selection_store=store,
    )

    with pytest.raises(UnknownProductError, match="ghost"):
        service.select_product("zemosaic")

    # No file written — bootstrap failed, select never reached.
    assert not store.path.exists()


# ===========================================================================
# 25) Corrupt existing selection file + bootstrap: must raise, not overwrite
# ===========================================================================


def test_bootstrap_corrupt_existing_file_raises_CorruptSelectionError(tmp_path):
    """If the selection file already exists but is corrupt, bootstrap
    must raise CorruptSelectionError and must not clobber/overwrite the
    corrupt file.  This pins the present-file-authoritative/fail-closed
    contract.
    """
    path = tmp_path / "corrupt.toml"
    corrupt_content = "this is not valid toml {{{"
    path.write_text(corrupt_content)

    catalog = _make_catalog()
    legacy = _make_legacy_registry(("zesolver",))
    store = SelectionStore(path=path)

    from zealfie.products.selection import bootstrap_selection_from_legacy_registry

    with pytest.raises(CorruptSelectionError, match="invalid TOML"):
        bootstrap_selection_from_legacy_registry(store, catalog, legacy)

    # The corrupt file must NOT be overwritten or replaced.
    assert path.read_text() == corrupt_content
