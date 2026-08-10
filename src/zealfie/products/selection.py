"""User selection of desired managed products (M1-2D.3).

Persists selected product ids in a simple deterministic TOML file
independent of the ProductCatalog and runtime.  Missing file is a
valid empty selection; invalid content is an error, never silently empty.

Does NOT download, build, install, activate, or mutate the runtime.
Does NOT mutate the packaged ``components.toml`` manifest.

Concepts preserved:
- **known**   — in the ProductCatalog
- **selected/managed** — in this store (what the user wants)
- **installed** — probed from the runtime
- **launchable** — probed from the runtime

Selection Order Contract
------------------------
``selected_product_ids`` is always stored and returned in **lexicographic
(sort()) order**.  This is the deterministic representation contract:
two logically-equivalent selections always produce the same serialized
file and the same in-memory tuple, regardless of the order in which
products were added.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from zealfie.components.model import ComponentDefinition
from zealfie.components.registry import ComponentRegistry
from zealfie.products.catalog import ProductCatalog, UnknownProductError

SELECTION_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SelectionStoreError(RuntimeError):
    """Base for selection store errors."""


class CorruptSelectionError(SelectionStoreError):
    """Invalid or corrupt persisted selection TOML.

    Raised when the selection file is present but unreadable, contains
    invalid TOML, is missing required fields, has an unsupported schema
    version, or has a malformed ``selected_product_ids`` list.

    The persisted file is never silently overwritten when this error is
    raised.
    """


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------


def default_selection_path() -> Path:
    """Platform-appropriate default for the desired-products file.

    * Linux:   ``$XDG_DATA_HOME/zealfie/desired-products.toml``
      (default ``~/.local/share/zealfie/desired-products.toml``)
    * macOS:   ``~/Library/Application Support/zealfie/desired-products.toml``
    * Windows: ``%LOCALAPPDATA%/zealfie/desired-products.toml``
    """
    if sys.platform == "win32":
        base = os.environ.get(
            "LOCALAPPDATA",
            str(Path.home() / "AppData" / "Local"),
        )
        return Path(base) / "zealfie" / "desired-products.toml"
    elif sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "zealfie"
            / "desired-products.toml"
        )
    else:
        base = os.environ.get(
            "XDG_DATA_HOME",
            str(Path.home() / ".local" / "share"),
        )
        return Path(base) / "zealfie" / "desired-products.toml"


# ---------------------------------------------------------------------------
# DesiredProductSelection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DesiredProductSelection:
    """Immutable snapshot of the user's desired product selection.

    ``selected_product_ids`` is always sorted lexicographically
    (deterministic representation contract).
    """

    selected_product_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids: list[str] = []
        seen: set[str] = set()
        for raw in self.selected_product_ids:
            pid = str(raw or "").strip()
            if not pid:
                raise ValueError(
                    "selected_product_ids must not contain empty values"
                )
            if pid in seen:
                raise ValueError(
                    f"duplicate product id in selection: {pid!r}"
                )
            seen.add(pid)
            ids.append(pid)
        ids.sort()
        object.__setattr__(self, "selected_product_ids", tuple(ids))

    def __len__(self) -> int:
        return len(self.selected_product_ids)

    def __contains__(self, product_id: str) -> bool:
        return str(product_id or "").strip() in frozenset(
            self.selected_product_ids
        )

    def with_product(
        self, product_id: str, *, catalog: ProductCatalog
    ) -> DesiredProductSelection:
        """Return a *new* selection with *product_id* added (idempotent).

        Raises :class:`UnknownProductError` if *product_id* is not in
        the catalog — the current selection and persisted file are never
        mutated on failure.  The catalog check happens *before* the
        idempotence shortcut so that an already-present product id that
        is unknown in the catalog always raises.
        """
        # Validate against catalog first — even if already in selection.
        catalog.get(product_id)  # raises UnknownProductError if unknown
        if product_id in self:
            return self
        new_ids = tuple(sorted([*self.selected_product_ids, product_id]))
        return DesiredProductSelection(new_ids)

    def without_product(self, product_id: str) -> DesiredProductSelection:
        """Return a *new* selection with *product_id* removed (idempotent).
        """
        if product_id not in self:
            return self
        new_ids = tuple(
            sorted(
                pid
                for pid in self.selected_product_ids
                if pid != product_id
            )
        )
        return DesiredProductSelection(new_ids)


# ---------------------------------------------------------------------------
# SelectionStore
# ---------------------------------------------------------------------------


class SelectionStore:
    """Persistent store for user product selection.

    Thread-unsafe by design — single-owner at the service layer.

    Parameters
    ----------
    path:
        Filesystem path for the persisted TOML file.  ``None`` uses the
        platform-appropriate default.  Inject a temporary path for tests.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = (
            Path(path) if path is not None else default_selection_path()
        )
        self._selection: DesiredProductSelection = DesiredProductSelection()
        self._loaded: bool = False

    # -- properties -------------------------------------------------------

    @property
    def selected_product_ids(self) -> tuple[str, ...]:
        """Return the currently selected product ids (lazy-load from disk)."""
        if not self._loaded:
            self.reload()
        return self._selection.selected_product_ids

    @property
    def path(self) -> Path:
        """The filesystem path of the persisted selection file."""
        return self._path

    # -- I/O ---------------------------------------------------------------

    def reload(self) -> DesiredProductSelection:
        """Reload the selection from disk.

        * Missing file → empty valid selection.
        * Corrupt file → :class:`CorruptSelectionError`.
        """
        self._selection = _load_from_file(self._path)
        self._loaded = True
        return self._selection

    # -- mutation ----------------------------------------------------------

    def select(
        self, product_id: str, *, catalog: ProductCatalog
    ) -> DesiredProductSelection:
        """Add a product to the selection and persist to disk atomically.

        **Low-level primitive.**  This method does **not** bootstrap the
        selection file from the legacy registry.  Callers that need the
        legacy-preserving guard should use
        :func:`bootstrap_selection_from_legacy_registry` first, or use
        :meth:`ZeAlfieService.select_product` which guarantees the
        bootstrap (D.4.0).

        * Idempotent — adding an already-selected product is a no-op
          (no file write).
        * Raises :class:`UnknownProductError` for unknown products
          without mutating persisted state or in-memory selection.
        """
        if not self._loaded:
            self.reload()

        # Validate product_id against catalog BEFORE computing with_product.
        # This ensures the persisted state is never mutated for unknown
        # products, even if another thread/code path changed the file.
        catalog.get(product_id)  # raises UnknownProductError

        new_selection = self._selection.with_product(
            product_id, catalog=catalog
        )
        if new_selection is self._selection:
            return self._selection  # already selected → no-op

        _save_to_file(new_selection, self._path)
        self._selection = new_selection
        return new_selection

    def current_selection(self) -> DesiredProductSelection:
        """Return the current in-memory selection (lazy-load from disk)."""
        if not self._loaded:
            self.reload()
        return self._selection


# ---------------------------------------------------------------------------
# Bootstrap from legacy registry (D.4.0)
# ---------------------------------------------------------------------------


def bootstrap_selection_from_legacy_registry(
    store: SelectionStore,
    catalog: ProductCatalog,
    legacy_registry: ComponentRegistry,
) -> DesiredProductSelection:
    """One-shot bootstrap of a :class:`DesiredProductSelection` from the
    legacy ``ComponentRegistry`` (packaged ``components.toml``).

    **Bootstrap rule (idempotent):**

    * If ``store.path`` does **not** exist:
        - Derive initial product ids from ``legacy_registry.available_ids()``.
        - Validate every id against *catalog* — unknown legacy ids raise
          :class:`UnknownProductError` without writing the selection file.
        - Persist a deterministic ``DesiredProductSelection`` to *store*.
        - Return the newly persisted selection.
    * If ``store.path`` **does** exist (including an explicit empty
      selection): do nothing — the file is authoritative.  Return the
      currently loaded selection from *store*.

    **Idempotent / one-shot:** once the file exists, subsequent calls
    are no-ops.  The legacy registry is never consulted after the first
    bootstrap.

    **No runtime/deployment/network:** pure file-initialisation from
    local registries.  Does not touch the runtime, deployment, launch,
    or acquisition layers.

    Parameters
    ----------
    store:
        The :class:`SelectionStore` whose backing file is checked.
    catalog:
        The :class:`ProductCatalog` used to validate legacy ids.
    legacy_registry:
        The :class:`ComponentRegistry` representing the pre-D.3 managed
        set (packaged ``components.toml``).

    Returns
    -------
    DesiredProductSelection
        The authoritative selection — either freshly bootstrapped or
        loaded from the existing file.

    Raises
    ------
    UnknownProductError
        If any legacy component id is not in the product catalog.
        The selection file is never written on this error.
    """
    if store.path.exists():
        # File exists (including explicit empty) — authoritative, do not
        # overwrite with any derivation from a potentially-changed legacy
        # registry.
        return store.current_selection()

    legacy_ids = legacy_registry.available_ids()
    # Validate every legacy id against the product catalog first.
    for pid in legacy_ids:
        catalog.get(pid)  # raises UnknownProductError

    selection = DesiredProductSelection(legacy_ids)
    _save_to_file(selection, store.path)
    store.reload()
    return store.current_selection()



# ---------------------------------------------------------------------------
# Pure materialization
# ---------------------------------------------------------------------------


def materialize_desired_components(
    catalog: ProductCatalog,
    selection: DesiredProductSelection,
) -> tuple[ComponentDefinition, ...]:
    """Convert a product catalog + user selection into a tuple of
    :class:`ComponentDefinition` objects.

    Every selected product id is converted faithfully from its
    :class:`~zealfie.products.catalog.ProductDescriptor`:

    * ``product_id`` → ``component_id``
    * ``display_name`` → ``display_name``
    * ``distribution_name`` → ``distribution_name``
    * ``launch_entry_points`` → ``launch_entry_points``
    * ``required_extras`` → ``required_extras``

    Raises :class:`UnknownProductError` if any selected product id is
    not in the catalog.  No network calls, no wheel builds, no runtime
    mutation, and no mutation of the packaged ``components.toml``.

    Returns definitions in the same sorted order as
    ``selection.selected_product_ids``.
    """
    definitions: list[ComponentDefinition] = []
    for product_id in selection.selected_product_ids:
        desc = catalog.get(product_id)  # raises UnknownProductError
        definitions.append(
            ComponentDefinition(
                component_id=desc.product_id,
                display_name=desc.display_name,
                distribution_name=desc.distribution_name,
                launch_entry_points=desc.launch_entry_points,
                required_extras=desc.required_extras,
            )
        )
    return tuple(definitions)


def desired_component_registry(
    catalog: ProductCatalog,
    selection: DesiredProductSelection,
) -> ComponentRegistry:
    """Materialize a :class:`ComponentRegistry` from catalog + selection.

    Convenience wrapper around :func:`materialize_desired_components`.
    """
    return ComponentRegistry(
        materialize_desired_components(catalog, selection)
    )


# ---------------------------------------------------------------------------
# Private I/O helpers
# ---------------------------------------------------------------------------


def _load_from_file(path: Path) -> DesiredProductSelection:
    """Load the selection from the TOML file at *path*.

    Missing file → empty selection.
    """
    if not path.exists():
        return DesiredProductSelection()
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise CorruptSelectionError(
            f"cannot read selection file {path}: {exc}"
        ) from exc
    return _parse_selection(text, source=path)


def _parse_selection(
    text: str, source: object = "<string>"
) -> DesiredProductSelection:
    """Parse a selection from TOML text.

    Raises :class:`CorruptSelectionError` for any structural or semantic
    problem — never returns a silently-empty selection for invalid content.
    """
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CorruptSelectionError(
            f"invalid TOML in selection file {source}: {exc}"
        ) from exc

    # -- schema_version --------------------------------------------------
    schema_version = payload.get("schema_version")
    if schema_version is None:
        raise CorruptSelectionError(
            f"selection file {source}: missing schema_version"
        )
    if not isinstance(schema_version, int):
        raise CorruptSelectionError(
            f"selection file {source}: schema_version must be an integer"
        )
    if schema_version != SELECTION_SCHEMA_VERSION:
        raise CorruptSelectionError(
            f"selection file {source}: unsupported schema_version "
            f"{schema_version} (expected {SELECTION_SCHEMA_VERSION})"
        )

    # -- selected_product_ids --------------------------------------------
    ids = payload.get("selected_product_ids")
    if ids is None:
        raise CorruptSelectionError(
            f"selection file {source}: missing selected_product_ids"
        )
    if not isinstance(ids, list):
        raise CorruptSelectionError(
            f"selection file {source}: selected_product_ids must be a list"
        )

    validated: list[str] = []
    for i, raw in enumerate(ids):
        if not isinstance(raw, str) or not str(raw).strip():
            raise CorruptSelectionError(
                f"selection file {source}: selected_product_ids[{i}] "
                f"must be a non-empty string"
            )
        validated.append(str(raw).strip())

    try:
        return DesiredProductSelection(tuple(validated))
    except ValueError as exc:
        raise CorruptSelectionError(
            f"selection file {source}: {exc}"
        ) from exc


def validate_selection_against_catalog(
    catalog: ProductCatalog,
    selection: DesiredProductSelection,
) -> None:
    """Raise :class:`UnknownProductError` if any selected product id is
    not in *catalog*.

    This is a pure validation helper that callers should invoke before
    using a loaded selection in a context that requires catalog-valid ids,
    such as managed-product determination, product-state collection, or
    desired-component materialization.

    Raises
    ------
    UnknownProductError
        If any ``product_id`` in *selection* is not in *catalog*.
    """
    for product_id in selection.selected_product_ids:
        catalog.get(product_id)  # raises UnknownProductError


def _save_to_file(
    selection: DesiredProductSelection, path: Path
) -> None:
    """Persist a selection to disk atomically.

    1. Write to a temp sibling file.
    2. Flush + fsync.
    3. ``os.replace`` (atomic on same filesystem).

    On any failure the temp file is cleaned up and the original file
    (if any) is left untouched.
    """
    import tempfile

    text = _render_selection(selection)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".desired-products-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _render_selection(selection: DesiredProductSelection) -> str:
    """Render a :class:`DesiredProductSelection` to deterministic TOML text.

    Output contract: ``selected_product_ids`` is always sorted
    lexicographically, and integer fields are emitted without quotes
    with trailing newline.  Two equivalent selections always produce
    byte-identical output.
    """
    lines: list[str] = [
        f"schema_version = {SELECTION_SCHEMA_VERSION}",
    ]
    if selection.selected_product_ids:
        lines.append("selected_product_ids = [")
        for pid in selection.selected_product_ids:
            lines.append(f"    {_toml_str(pid)},")
        lines.append("]")
    else:
        lines.append("selected_product_ids = []")
    return "\n".join(lines) + "\n"


def _toml_str(value: str) -> str:
    """Render *value* as a TOML basic string.

    JSON string escaping is a safe subset for TOML basic strings and
    prevents selected ids containing quotes, backslashes, or control
    characters from corrupting the persisted file.
    """
    return json.dumps(value)
