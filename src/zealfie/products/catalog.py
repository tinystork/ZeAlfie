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

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from zealfie.acceleration.models import (
    AcceleratedRequirement,
    AccelerationIncompatibility,
    KNOWN_BACKENDS,
    ProductAccelerationRequirements,
)
from zealfie.components.model import EntryPointContract
from zealfie.products.policy import VALID_CHANNELS
from zealfie.sources import InvalidRemoteSourceError, RemoteSource

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
    # M1-2D.1: Optional remote source metadata.
    # None for products without a declared remote repository.
    remote_source: RemoteSource | None = None
    # M1-2F Phase 5: product-specific channel -> ref mapping.
    #
    # Declares which discovery channels a product actually exposes and which
    # mutable ref each one resolves to.  This is the per-product authority:
    # ``DEFAULT_CHANNEL_REFS`` in :mod:`zealfie.products.policy` remains a
    # default mapper only and never grants a channel to a product that does
    # not declare it here.
    #
    # Immutable tuple of ``(channel, ref)`` pairs in declaration order.  An
    # empty tuple means "no channels" (product has no remote source, or the
    # descriptor is constructed without channel metadata).
    channel_refs: tuple[tuple[str, str], ...] = ()
    # M1-2H: Optional structured accelerated requirements.
    #
    # None for products that declare no acceleration needs.  When present,
    # the declaration is fully validated (known backend, canonicalized
    # distributions, parseable specifiers, no self-conflicts).  ZeAlfie
    # never selects a concrete accelerated framework — this only records
    # what the product declares.
    acceleration: ProductAccelerationRequirements | None = None

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


        # Validate remote_source: must be None or a RemoteSource instance.
        rs = self.remote_source
        if rs is not None and not isinstance(rs, RemoteSource):
            raise ValueError(
                f"remote_source must be None or a RemoteSource instance, "
                f"got {type(rs).__qualname__}"
            )

        # Validate channel_refs.  Backward-compatible fallback: a product
        # with a remote source but no explicit channels exposes exactly the
        # ``stable`` channel pointing at ``remote_source.ref`` — and nothing
        # else.  This deliberately does NOT inherit beta/development from
        # DEFAULT_CHANNEL_REFS.
        channel_refs = tuple(self.channel_refs)
        if not channel_refs and rs is not None:
            channel_refs = (("stable", rs.ref),)
        if channel_refs and rs is None:
            raise ValueError("channel_refs requires remote_source")

        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw_channel, raw_ref in channel_refs:
            if not isinstance(raw_channel, str):
                raise ValueError("channel_refs channels must be strings")
            channel = raw_channel.strip()
            if not channel:
                raise ValueError("channel_refs channels must not be empty")
            if channel not in VALID_CHANNELS:
                raise ValueError(
                    f"channel_refs channel {channel!r} is not a known channel "
                    f"(expected one of {VALID_CHANNELS})"
                )
            if not isinstance(raw_ref, str):
                raise ValueError(
                    f"channel_refs ref for {channel!r} must be a string"
                )
            ref = raw_ref.strip()
            if not ref:
                raise ValueError(
                    f"channel_refs ref for {channel!r} must not be empty"
                )
            if channel in seen:
                raise ValueError(f"duplicate channel in channel_refs: {channel!r}")
            seen.add(channel)
            normalized.append((channel, ref))
        object.__setattr__(self, "channel_refs", tuple(normalized))

        # Validate acceleration (M1-2H): must be None or a validated
        # ProductAccelerationRequirements instance.
        acc = self.acceleration
        if acc is not None and not isinstance(acc, ProductAccelerationRequirements):
            raise ValueError(
                "acceleration must be None or a ProductAccelerationRequirements "
                f"instance, got {type(acc).__qualname__}"
            )

    @property
    def channel_ref_map(self) -> dict[str, str]:
        """Return ``channel_refs`` as a plain ``{channel: ref}`` mapping."""
        return dict(self.channel_refs)

    @property
    def available_channels(self) -> tuple[str, ...]:
        """Return the product's declared channels in declaration order."""
        return tuple(channel for channel, _ in self.channel_refs)

    def channel_ref(self, channel: str) -> str | None:
        """Return the ref for a declared *channel*, or ``None`` if undeclared."""
        key = str(channel or "").strip()
        for ch, ref in self.channel_refs:
            if ch == key:
                return ref
        return None


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

    # --- optional remote_source (M1-2D.1) ---
    remote_source = _parse_optional_remote_source(raw, label_prefix)

    # --- optional channels (M1-2F Phase 5) ---
    channel_refs = _parse_optional_channels(raw, label_prefix, remote_source)

    # --- optional acceleration requirements (M1-2H) ---
    # Purely additive and backward compatible: schema_version stays at 1
    # because an older ZeAlfie simply ignores the table and keeps treating
    # the product as having no acceleration requirements.
    acceleration = _parse_optional_acceleration(raw, label_prefix, product_id)

    return ProductDescriptor(
        product_id=product_id,
        display_name=display_name,
        distribution_name=distribution_name,
        launch_entry_points=tuple(contracts),
        required_extras=required_extras,
        description=description,
        remote_source=remote_source,
        channel_refs=channel_refs,
        acceleration=acceleration,
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


def _parse_optional_remote_source(
    raw: dict[str, Any],
    label_prefix: str,
) -> RemoteSource | None:
    """Parse an optional ``[products.remote_source]`` table.

    Returns ``None`` if the key is absent — remote source metadata is
    purely additive and does not affect existing catalog semantics.
    """
    raw_rs = raw.get("remote_source")
    if raw_rs is None:
        return None
    if not isinstance(raw_rs, dict):
        raise InvalidCatalogError(
            f"{label_prefix}.remote_source must be a table"
        )
    try:
        return RemoteSource(
            owner=_required_str(raw_rs, "owner", f"{label_prefix}.remote_source.owner"),
            repo=_required_str(raw_rs, "repo", f"{label_prefix}.remote_source.repo"),
            ref=_required_str(raw_rs, "ref", f"{label_prefix}.remote_source.ref"),
        )
    except InvalidRemoteSourceError as exc:
        raise InvalidCatalogError(f"{label_prefix}.remote_source: {exc}") from exc


def _parse_optional_channels(
    raw: dict[str, Any],
    label_prefix: str,
    remote_source: RemoteSource | None,
) -> tuple[tuple[str, str], ...]:
    """Parse an optional ``[products.channels]`` table.

    Returns ``()`` when absent — the per-product fallback (``stable =
    remote_source.ref`` only) is applied later in
    :class:`ProductDescriptor`.

    Fail-closed rules:

    * ``channels`` must be a table of non-empty-string → non-empty-string.
    * channel names must be known policy channels (``stable``, ``beta``,
      ``development``).
    * duplicate channel names are rejected.
    * a channel table without ``remote_source`` is rejected — a product
      cannot declare discoverable channels without a remote repository.
    """
    raw_channels = raw.get("channels")
    if raw_channels is None:
        return ()
    if not isinstance(raw_channels, dict):
        raise InvalidCatalogError(f"{label_prefix}.channels must be a table")
    if remote_source is None:
        raise InvalidCatalogError(
            f"{label_prefix}.channels requires a remote_source table"
        )

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_channel, raw_ref in raw_channels.items():
        if not isinstance(raw_channel, str) or not raw_channel.strip():
            raise InvalidCatalogError(
                f"{label_prefix}.channels keys must be non-empty strings"
            )
        channel = raw_channel.strip()
        if channel not in VALID_CHANNELS:
            raise InvalidCatalogError(
                f"{label_prefix}.channels.{channel!r} is not a known channel "
                f"(expected one of {VALID_CHANNELS})"
            )
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            raise InvalidCatalogError(
                f"{label_prefix}.channels.{channel} must be a non-empty string"
            )
        if channel in seen:
            raise InvalidCatalogError(
                f"{label_prefix}.channels: duplicate channel {channel!r}"
            )
        seen.add(channel)
        result.append((channel, raw_ref.strip()))
    return tuple(result)


def _reject_unknown_keys(
    table: dict[str, Any],
    known_keys: set[str],
    label: str,
) -> None:
    """Fail closed on any key outside *known_keys* in a catalog table."""
    unknown = sorted(set(table) - known_keys)
    if unknown:
        raise InvalidCatalogError(
            f"{label} contains unknown key(s): "
            + ", ".join(repr(key) for key in unknown)
        )


def _parse_optional_acceleration(
    raw: dict[str, Any],
    label_prefix: str,
    product_id: str,
) -> ProductAccelerationRequirements | None:
    """Parse an optional ``[products.acceleration]`` table (M1-2H).

    Returns ``None`` when the key is absent.  Fail-closed and strict
    when present:

    * only the keys ``backend``, ``optional``, ``requirements`` and
      ``incompatibilities`` are allowed — unknown keys anywhere inside
      the acceleration tables are rejected;
    * ``backend`` is required and must be in
      :data:`zealfie.acceleration.models.KNOWN_BACKENDS`;
    * requirement ``specifier`` values must parse as PEP 440
      :class:`~packaging.specifiers.SpecifierSet`;
    * duplicate requirements, duplicate incompatibilities and
      distributions declared both ways are rejected.

    ZeAlfie never selects a concrete accelerated framework here — this
    only records what the product declares as distribution names.
    """
    raw_acc = raw.get("acceleration")
    if raw_acc is None:
        return None
    label = f"{label_prefix}.acceleration"
    if not isinstance(raw_acc, dict):
        raise InvalidCatalogError(f"{label} must be a table")

    _reject_unknown_keys(
        raw_acc,
        {"backend", "optional", "requirements", "incompatibilities"},
        label,
    )

    backend = _required_str(raw_acc, "backend", f"{label}.backend")
    if backend not in KNOWN_BACKENDS:
        raise InvalidCatalogError(
            f"{label}.backend: unsupported acceleration backend {backend!r}"
        )

    optional = raw_acc.get("optional", True)
    if not isinstance(optional, bool):
        raise InvalidCatalogError(f"{label}.optional must be a bool")

    try:
        requirements = _parse_acceleration_requirements(raw_acc, label)
        incompatibilities = _parse_acceleration_incompatibilities(raw_acc, label)
        return ProductAccelerationRequirements(
            product_id=product_id,
            backend=backend,
            optional=optional,
            requirements=requirements,
            incompatibilities=incompatibilities,
        )
    except InvalidCatalogError:
        raise
    except ValueError as exc:
        raise InvalidCatalogError(f"{label}: {exc}") from exc


def _parse_acceleration_requirements(
    raw_acc: dict[str, Any],
    label: str,
) -> tuple[AcceleratedRequirement, ...]:
    """Parse ``acceleration.requirements`` array of tables."""
    raw_reqs = raw_acc.get("requirements")
    if raw_reqs is None:
        return ()
    if not isinstance(raw_reqs, list):
        raise InvalidCatalogError(
            f"{label}.requirements must be an array of tables"
        )

    parsed: list[AcceleratedRequirement] = []
    for idx, raw_req in enumerate(raw_reqs):
        req_label = f"{label}.requirements[{idx}]"
        if not isinstance(raw_req, dict):
            raise InvalidCatalogError(f"{req_label} must be a table")
        _reject_unknown_keys(
            raw_req, {"distribution", "specifier", "extras"}, req_label
        )

        distribution = _required_str(
            raw_req, "distribution", f"{req_label}.distribution"
        )

        specifier = raw_req.get("specifier")
        if specifier is not None:
            if not isinstance(specifier, str):
                raise InvalidCatalogError(
                    f"{req_label}.specifier must be a string"
                )
            specifier = specifier.strip()
            if not specifier:
                raise InvalidCatalogError(
                    f"{req_label}.specifier must not be empty"
                )
            try:
                SpecifierSet(specifier)
            except InvalidSpecifier as exc:
                raise InvalidCatalogError(
                    f"{req_label}.specifier is not a valid PEP 440 "
                    f"specifier: {exc}"
                ) from exc

        extras: list[str] = []
        raw_extras = raw_req.get("extras")
        if raw_extras is not None:
            if not isinstance(raw_extras, list):
                raise InvalidCatalogError(
                    f"{req_label}.extras must be an array of strings"
                )
            for extra_idx, raw_extra in enumerate(raw_extras):
                if not isinstance(raw_extra, str):
                    raise InvalidCatalogError(
                        f"{req_label}.extras[{extra_idx}] must be a string"
                    )
                extra = raw_extra.strip()
                if not extra:
                    raise InvalidCatalogError(
                        f"{req_label}.extras[{extra_idx}] must not be empty"
                    )
                extras.append(canonicalize_name(extra))

        parsed.append(
            AcceleratedRequirement(
                distribution=distribution,
                specifier=specifier,
                extras=tuple(extras),
            )
        )
    return tuple(parsed)


def _parse_acceleration_incompatibilities(
    raw_acc: dict[str, Any],
    label: str,
) -> tuple[AccelerationIncompatibility, ...]:
    """Parse ``acceleration.incompatibilities`` array of tables."""
    raw_incs = raw_acc.get("incompatibilities")
    if raw_incs is None:
        return ()
    if not isinstance(raw_incs, list):
        raise InvalidCatalogError(
            f"{label}.incompatibilities must be an array of tables"
        )

    parsed: list[AccelerationIncompatibility] = []
    for idx, raw_inc in enumerate(raw_incs):
        inc_label = f"{label}.incompatibilities[{idx}]"
        if not isinstance(raw_inc, dict):
            raise InvalidCatalogError(f"{inc_label} must be a table")
        _reject_unknown_keys(
            raw_inc, {"distribution", "reason"}, inc_label
        )

        distribution = _required_str(
            raw_inc, "distribution", f"{inc_label}.distribution"
        )
        reason = _required_str(raw_inc, "reason", f"{inc_label}.reason")
        parsed.append(
            AccelerationIncompatibility(distribution=distribution, reason=reason)
        )
    return tuple(parsed)
