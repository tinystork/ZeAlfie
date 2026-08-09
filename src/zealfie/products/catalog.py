"""Product catalog — immutable registry of known ZeSoftware products.

Loaded from the packaged ``manifests/products.toml`` resource.
This catalog answers "what products does ZeAlfie know about?".

It is NOT the deployment contract.  See :mod:`zealfie.components.registry`
for the component registry that drives deployment planning.
"""

from __future__ import annotations

import importlib.resources
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from zealfie.components.model import EntryPointContract

CATALOG_PACKAGE = "zealfie.manifests"
CATALOG_RESOURCE = "products.toml"
SUPPORTED_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownProductError(KeyError):
    """Raised when a requested product id is not in the catalog.

    Deliberately a distinct type from
    :class:`zealfie.components.registry.UnknownComponentError` so that
    callers can distinguish "not in catalog" from "not in deployment
    registry".
    """

    def __init__(self, product_id: str) -> None:
        self.product_id = str(product_id)
        super().__init__(self.product_id)


class CatalogError(RuntimeError):
    """Base class for product catalog loading errors."""


class InvalidCatalogError(CatalogError):
    """Raised when the product catalog TOML is malformed."""


class UnsupportedCatalogSchemaError(CatalogError):
    """Raised when the catalog uses an unsupported schema version."""


# ---------------------------------------------------------------------------
# Product descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductDescriptor:
    """Immutable description of a known ZeSoftware product.

    Mirrors :class:`~zealfie.components.model.ComponentDefinition` in
    shape but belongs to the product catalog, not the deployment
    contract.
    """

    product_id: str
    display_name: str
    distribution_name: str
    launch_entry_points: tuple[EntryPointContract, ...]
    required_extras: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        for field_name in ("product_id", "display_name", "distribution_name"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
        # Validate description: optional, but if provided must be a str.
        desc = str(getattr(self, "description") or "")
        object.__setattr__(self, "description", desc)
        # Validate entry points.
        entry_points = tuple(self.launch_entry_points)
        object.__setattr__(self, "launch_entry_points", entry_points)
        # Validate extras.
        extras: list[str] = []
        seen_extras: set[str] = set()
        for raw_extra in self.required_extras:
            extra = canonicalize_name(str(raw_extra or "").strip())
            if not extra:
                raise ValueError("required_extras must not contain empty values")
            if extra in seen_extras:
                raise ValueError(f"duplicate required extra: {extra}")
            seen_extras.add(extra)
            extras.append(extra)
        object.__setattr__(self, "required_extras", tuple(extras))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductCatalog:
    """Immutable registry of known ZeSoftware products.

    Loaded from the packaged ``products.toml`` resource.
    """

    _descriptors: tuple[ProductDescriptor, ...]
    _by_id: dict[str, ProductDescriptor]

    def __init__(self, descriptors: tuple[ProductDescriptor, ...]) -> None:
        by_id: dict[str, ProductDescriptor] = {}
        for desc in descriptors:
            if desc.product_id in by_id:
                raise InvalidCatalogError(
                    f"duplicate product id: {desc.product_id}"
                )
            by_id[desc.product_id] = desc
        # Use __setattr__ because this is a frozen dataclass.
        object.__setattr__(self, "_descriptors", descriptors)
        object.__setattr__(self, "_by_id", by_id)

    def list(self) -> tuple[ProductDescriptor, ...]:
        """Return all known products in definition order."""
        return self._descriptors

    def get(self, product_id: str) -> ProductDescriptor:
        """Return the descriptor for *product_id*.

        Raises :class:`UnknownProductError` if not found.
        """
        key = str(product_id or "").strip()
        try:
            return self._by_id[key]
        except KeyError as exc:
            raise UnknownProductError(key) from exc

    def available_ids(self) -> tuple[str, ...]:
        """Return all known product ids in definition order."""
        return tuple(desc.product_id for desc in self._descriptors)

    def __len__(self) -> int:
        return len(self._descriptors)

    def __contains__(self, product_id: str) -> bool:
        return str(product_id or "").strip() in self._by_id


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def default_catalog() -> ProductCatalog:
    """Load the default product catalog from the packaged resource."""
    try:
        resource = importlib.resources.files(CATALOG_PACKAGE).joinpath(CATALOG_RESOURCE)
        return load_catalog_from_text(resource.read_text(encoding="utf-8"))
    except CatalogError:
        raise
    except Exception as exc:
        raise InvalidCatalogError(
            f"catalog resource could not be read: {exc}"
        ) from exc


def load_catalog_from_text(text: str) -> ProductCatalog:
    """Parse a product catalog from TOML text."""
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidCatalogError(f"catalog TOML is invalid: {exc}") from exc
    return _catalog_from_payload(payload)


def load_catalog_from_file(path: str | Path) -> ProductCatalog:
    """Parse a product catalog from a TOML file path."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        raise InvalidCatalogError(
            f"catalog file could not be read: {exc}"
        ) from exc
    return load_catalog_from_text(text)


def _catalog_from_payload(payload: dict[str, Any]) -> ProductCatalog:
    schema_version = payload.get("schema_version")
    if schema_version is None:
        raise InvalidCatalogError("schema_version is required")
    if not isinstance(schema_version, int):
        raise InvalidCatalogError("schema_version must be an integer")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedCatalogSchemaError(
            f"unsupported schema_version: {schema_version}"
        )

    products = payload.get("products")
    if not isinstance(products, list):
        raise InvalidCatalogError("products must be a list")
    if not products:
        raise InvalidCatalogError("products must not be empty")

    descriptors: list[ProductDescriptor] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(products):
        if not isinstance(raw, dict):
            raise InvalidCatalogError(f"products[{idx}] must be a table")
        desc = _product_from_payload(raw, idx)
        if desc.product_id in seen_ids:
            raise InvalidCatalogError(
                f"duplicate product id: {desc.product_id}"
            )
        seen_ids.add(desc.product_id)
        descriptors.append(desc)

    return ProductCatalog(tuple(descriptors))


def _product_from_payload(
    raw: dict[str, Any],
    index: int,
) -> ProductDescriptor:
    label_prefix = f"products[{index}]"
    product_id = _required_str(raw, "id", f"{label_prefix}.id")
    display_name = _required_str(
        raw, "display_name", f"{label_prefix}.display_name"
    )
    distribution_name = _required_str(
        raw, "distribution_name", f"{label_prefix}.distribution_name"
    )

    # --- optional description ---
    description = raw.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise InvalidCatalogError(
            f"{label_prefix}.description must be a string"
        )
    description = description.strip()

    # --- launch entry points ---
    launch = raw.get("launch")
    if not isinstance(launch, dict):
        raise InvalidCatalogError(f"{label_prefix}.launch must be a table")
    entry_points = launch.get("entry_points")
    if not isinstance(entry_points, list):
        raise InvalidCatalogError(
            f"{label_prefix}.launch.entry_points must be a list"
        )
    if not entry_points:
        raise InvalidCatalogError(
            f"{label_prefix}.launch.entry_points must not be empty"
        )

    contracts: list[EntryPointContract] = []
    seen_contracts: set[EntryPointContract] = set()
    for ep_idx, raw_ep in enumerate(entry_points):
        if not isinstance(raw_ep, dict):
            raise InvalidCatalogError(
                f"{label_prefix}.launch.entry_points[{ep_idx}] must be a table"
            )
        contract = EntryPointContract(
            group=_required_str(
                raw_ep, "group",
                f"{label_prefix}.launch.entry_points[{ep_idx}].group",
            ),
            name=_required_str(
                raw_ep, "name",
                f"{label_prefix}.launch.entry_points[{ep_idx}].name",
            ),
        )
        if contract in seen_contracts:
            raise InvalidCatalogError(
                f"duplicate entry point contract: "
                f"{contract.group}:{contract.name}"
            )
        seen_contracts.add(contract)
        contracts.append(contract)

    # --- required_extras ---
    required_extras: tuple[str, ...] = ()
    raw_extras = raw.get("required_extras")
    if raw_extras is not None:
        if not isinstance(raw_extras, list):
            raise InvalidCatalogError(
                f"{label_prefix}.required_extras must be a list of strings"
            )
        canonicals: list[str] = []
        seen: set[str] = set()
        for ei, raw_extra in enumerate(raw_extras):
            if not isinstance(raw_extra, str):
                raise InvalidCatalogError(
                    f"{label_prefix}.required_extras[{ei}] must be a string"
                )
            extra = raw_extra.strip()
            if not extra:
                raise InvalidCatalogError(
                    f"{label_prefix}.required_extras[{ei}] must not be empty"
                )
            canon = canonicalize_name(extra)
            if canon in seen:
                raise InvalidCatalogError(
                    f"{label_prefix}.required_extras: duplicate extra {extra!r}"
                )
            seen.add(canon)
            canonicals.append(canon)
        required_extras = tuple(canonicals)

    return ProductDescriptor(
        product_id=product_id,
        display_name=display_name,
        distribution_name=distribution_name,
        launch_entry_points=tuple(contracts),
        required_extras=required_extras,
        description=description,
    )


def _required_str(
    payload: dict[str, Any],
    key: str,
    label: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise InvalidCatalogError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise InvalidCatalogError(f"{label} must not be empty")
    return value
