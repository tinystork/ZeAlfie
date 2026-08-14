"""Per-product update policy configuration (M1-2F Phase 3 / Lot F.2).

Implements the minimal Channel / Follow / Pin model (audit Rules 11/13/24):

* ``channel`` — one of ``stable | beta | development``.  A channel is a
  *discovery policy*, never an installed identity.
* ``policy``  — one of ``follow | pin``.
* ``pin_sha`` — required (40-hex) when ``policy == pin``.

The resolved immutable SHA remains the **only** installed identity; the
channel/policy only decide how a future source ref is chosen and how an
update check resolves it.

A single central channel→ref mapping lives here (``DEFAULT_CHANNEL_REFS``)
so no product code hardcodes the mapping.  It is data-driven and can be
overridden per product via the optional ``source_ref`` field, or globally
by injecting a custom mapping into :func:`effective_ref`.

This module is pure Python and Qt-free.  It never downloads, builds,
installs, or mutates the runtime.  The persisted file is a per-product
user configuration, strictly separate from the packaged catalog
(``manifests/products.toml``) and the selection store.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

POLICY_SCHEMA_VERSION = 1

VALID_CHANNELS: tuple[str, ...] = ("stable", "beta", "development")
VALID_POLICIES: tuple[str, ...] = ("follow", "pin")

# Central channel→ref mapping — the single place where channels are mapped
# to discovery refs.  Data-driven: changing this mapping changes discovery
# without changing any user configuration or product code.
DEFAULT_CHANNEL_REFS: dict[str, str] = {
    "stable": "main",
    "beta": "beta",
    "development": "development",
}

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProductPolicyError(RuntimeError):
    """Base for product policy configuration errors."""


class CorruptProductPolicyError(ProductPolicyError):
    """Invalid or corrupt persisted product-policy TOML.

    Raised when the policy file is present but unreadable, contains
    invalid TOML, has an unsupported schema version, or holds a product
    policy that fails validation.  The persisted file is never silently
    overwritten on this error — it is a hard, fail-closed signal.
    """


# ---------------------------------------------------------------------------
# Product policy model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductPolicy:
    """Immutable, validated per-product update policy.

    Fail-closed validation (``__post_init__``):

    * ``channel`` must be one of ``stable | beta | development``.
    * ``policy`` must be one of ``follow | pin``.
    * ``policy == "pin"`` requires a valid 40-hex ``pin_sha``;
      a missing/invalid one raises.
    * ``policy != "pin"`` rejects any present ``pin_sha``.
    * ``source_ref`` (optional per-product channel→ref override) is only
      valid for ``follow``; it must not be present for ``pin``.
    """

    product_id: str
    channel: str = "stable"
    policy: str = "follow"
    pin_sha: str | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        pid = str(self.product_id or "").strip()
        if not pid:
            raise ValueError("product_id must not be empty")
        object.__setattr__(self, "product_id", pid)

        channel = str(self.channel or "").strip()
        if channel not in VALID_CHANNELS:
            raise ValueError(
                f"channel must be one of {VALID_CHANNELS}, got {self.channel!r}"
            )
        object.__setattr__(self, "channel", channel)

        policy = str(self.policy or "").strip()
        if policy not in VALID_POLICIES:
            raise ValueError(
                f"policy must be one of {VALID_POLICIES}, got {self.policy!r}"
            )
        object.__setattr__(self, "policy", policy)

        pin_sha = self.pin_sha
        if pin_sha is not None:
            pin_sha = str(pin_sha).strip().lower()

        if policy == "pin":
            if not pin_sha or not _SHA1_RE.match(pin_sha):
                raise ValueError(
                    "pin_sha is required (40-hex) when policy is 'pin'"
                )
            object.__setattr__(self, "pin_sha", pin_sha)
        else:
            if pin_sha:
                raise ValueError(
                    "pin_sha must not be present when policy is not 'pin'"
                )
            object.__setattr__(self, "pin_sha", None)

        source_ref = self.source_ref
        if source_ref is not None:
            source_ref = str(source_ref).strip()
            if not source_ref:
                raise ValueError("source_ref must not be empty when present")
            if policy == "pin":
                raise ValueError(
                    "source_ref must not be present when policy is 'pin'"
                )
        object.__setattr__(self, "source_ref", source_ref)


def default_product_policy(product_id: str) -> ProductPolicy:
    """Return the factory-default policy for *product_id*.

    ``channel=stable``, ``policy=follow`` — unchanged from today's
    behaviour.  The catalog's ``remote_source.ref`` remains the factory
    default ref for the ``stable`` channel (which maps to ``main``).
    """
    return ProductPolicy(product_id=product_id)


def effective_ref(
    policy: ProductPolicy,
    channel_refs: Mapping[str, str] = DEFAULT_CHANNEL_REFS,
) -> str:
    """Return the effectively requested ref for *policy*.

    * ``follow`` → ``channel_refs[channel]`` (or the per-product
      ``source_ref`` override when present).
    * ``pin``    → the pinned immutable SHA (``policy.pin_sha``).

    Raises :class:`ValueError` when the channel has no entry in
    *channel_refs* (fail closed on an incomplete mapping).
    """
    if policy.policy == "pin":
        return policy.pin_sha  # validated non-None 40-hex
    if policy.source_ref is not None:
        return policy.source_ref
    try:
        return channel_refs[policy.channel]
    except KeyError as exc:
        raise ValueError(
            f"no channel→ref mapping for channel {policy.channel!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------


def default_product_policy_path() -> Path:
    """Platform-appropriate default for the product-policy TOML file.

    * Linux:   ``$XDG_CONFIG_HOME/zealfie/product-policy.toml``
      (default ``~/.config/zealfie/product-policy.toml``)
    * macOS:   ``~/Library/Application Support/zealfie/product-policy.toml``
    * Windows: ``%APPDATA%/zealfie/product-policy.toml``
    """
    if sys.platform == "win32":
        base = os.environ.get(
            "APPDATA",
            str(Path.home() / "AppData" / "Roaming"),
        )
        return Path(base) / "zealfie" / "product-policy.toml"
    elif sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "zealfie"
            / "product-policy.toml"
        )
    else:
        base = os.environ.get(
            "XDG_CONFIG_HOME",
            str(Path.home() / ".config"),
        )
        return Path(base) / "zealfie" / "product-policy.toml"


# ---------------------------------------------------------------------------
# ProductPolicyStore
# ---------------------------------------------------------------------------


class ProductPolicyStore:
    """Persistent store for per-product update policy.

    Thread-unsafe by design — single-owner at the service layer, mirroring
    :class:`~zealfie.products.selection.SelectionStore`.

    Read paths are lenient for a *missing* file (→ factory defaults) and
    strict for an *invalid* file (→ :class:`CorruptProductPolicyError`).
    Write paths are atomic (temp file + fsync + ``os.replace``).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = (
            Path(path) if path is not None else default_product_policy_path()
        )
        self._policies: dict[str, ProductPolicy] = {}
        self._loaded: bool = False

    @property
    def path(self) -> Path:
        """The filesystem path of the persisted policy file."""
        return self._path

    def reload(self) -> dict[str, ProductPolicy]:
        """Reload the policy mapping from disk.

        * Missing file → empty mapping (factory defaults apply).
        * Corrupt file → :class:`CorruptProductPolicyError`.
        """
        self._policies = _load_from_file(self._path)
        self._loaded = True
        return self._policies

    def policy_for(self, product_id: str) -> ProductPolicy:
        """Return the policy for *product_id* (lazy-load from disk).

        Products with no configured entry get the factory-default policy
        (``stable`` / ``follow``).
        """
        if not self._loaded:
            self.reload()
        key = str(product_id or "").strip()
        return self._policies.get(key, default_product_policy(key))

    def set_policy(self, policy: ProductPolicy) -> ProductPolicy:
        """Persist *policy* for its product id (atomic write).

        Replaces any existing entry for the same product id.
        """
        if not self._loaded:
            self.reload()
        self._policies[policy.product_id] = policy
        _save_to_file(self._policies, self._path)
        return policy


# ---------------------------------------------------------------------------
# Private I/O helpers
# ---------------------------------------------------------------------------


def _load_from_file(path: Path) -> dict[str, ProductPolicy]:
    """Load the policy mapping from the TOML file at *path*.

    Missing file → empty mapping.  Any present-but-invalid file raises
    :class:`CorruptProductPolicyError` — never silently empty.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise CorruptProductPolicyError(
            f"cannot read product-policy file {path}: {exc}"
        ) from exc
    return _parse(text, source=path)


def _parse(
    text: str, source: object = "<string>"
) -> dict[str, ProductPolicy]:
    """Parse a policy mapping from TOML text.

    Raises :class:`CorruptProductPolicyError` for any structural or
    semantic problem.
    """
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CorruptProductPolicyError(
            f"invalid TOML in product-policy file {source}: {exc}"
        ) from exc

    schema_version = payload.get("schema_version")
    if schema_version is None:
        raise CorruptProductPolicyError(
            f"product-policy file {source}: missing schema_version"
        )
    if not isinstance(schema_version, int):
        raise CorruptProductPolicyError(
            f"product-policy file {source}: schema_version must be an integer"
        )
    if schema_version != POLICY_SCHEMA_VERSION:
        raise CorruptProductPolicyError(
            f"product-policy file {source}: unsupported schema_version "
            f"{schema_version} (expected {POLICY_SCHEMA_VERSION})"
        )

    products = payload.get("products")
    if products is None:
        raise CorruptProductPolicyError(
            f"product-policy file {source}: missing [products] table"
        )
    if not isinstance(products, dict):
        raise CorruptProductPolicyError(
            f"product-policy file {source}: products must be a table"
        )

    result: dict[str, ProductPolicy] = {}
    for product_id, raw in products.items():
        if not isinstance(product_id, str) or not product_id.strip():
            raise CorruptProductPolicyError(
                f"product-policy file {source}: empty product id"
            )
        if not isinstance(raw, dict):
            raise CorruptProductPolicyError(
                f"product-policy file {source}: "
                f"[products.{product_id}] must be a table"
            )
        try:
            result[product_id] = ProductPolicy(product_id=product_id, **raw)
        except (TypeError, ValueError) as exc:
            raise CorruptProductPolicyError(
                f"product-policy file {source}: "
                f"[products.{product_id}] invalid: {exc}"
            ) from exc
    return result


def _save_to_file(policies: dict[str, ProductPolicy], path: Path) -> None:
    """Persist the policy mapping to disk atomically.

    1. Write to a temp sibling file.
    2. Flush + fsync.
    3. ``os.replace`` (atomic on same filesystem).

    On any failure the temp file is cleaned up and the original file
    (if any) is left untouched.
    """
    text = _render(policies)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".product-policy-",
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


def _render(policies: dict[str, ProductPolicy]) -> str:
    """Render the policy mapping to deterministic TOML text."""
    lines: list[str] = [
        f"schema_version = {POLICY_SCHEMA_VERSION}",
        "",
    ]
    for product_id in sorted(policies):
        policy = policies[product_id]
        lines.append(f"[products.{_toml_str(product_id)}]")
        lines.append(f"channel = {_toml_str(policy.channel)}")
        lines.append(f"policy = {_toml_str(policy.policy)}")
        if policy.pin_sha is not None:
            lines.append(f"pin_sha = {_toml_str(policy.pin_sha)}")
        if policy.source_ref is not None:
            lines.append(f"source_ref = {_toml_str(policy.source_ref)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _toml_str(value: str) -> str:
    """Render *value* as a TOML basic string.

    JSON string escaping is a safe subset for TOML basic strings.
    """
    return json.dumps(value)
