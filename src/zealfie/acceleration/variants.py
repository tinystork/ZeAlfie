"""Accelerated variant catalog (M1-2H).

Pure, frozen value objects describing concrete accelerated *variants*
of Python distributions, plus the fail-closed catalog that resolves a
declared ``(distribution, backend, platform)`` need to at most one
variant.

Architectural invariant — ZeAlfie NEVER selects a concrete accelerated
framework and NEVER guesses a variant.  Variants are declared artifacts
(exact distribution + version + backend + optional platform and
sha256); when no variant matches, lookup returns ``None``, and when
more than one matches, lookup fails closed with
:class:`AmbiguousVariantError` — an ambiguous match is a catalog
configuration error, never an excuse to pick arbitrarily.

This module is pure: no I/O, no network, no Qt, no mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from packaging.utils import canonicalize_name

from zealfie.acceleration.models import KNOWN_BACKENDS


class AmbiguousVariantError(RuntimeError):
    """Raised when more than one variant matches a lookup (fail-closed).

    ZeAlfie never resolves ambiguity by guessing: the catalog author
    must make the variant declaration unambiguous.
    """


def _variant_key(variant: "AcceleratedVariant") -> tuple[str, str, str | None]:
    """Return the unique ``(distribution, backend, platform)`` key.

    ``None`` platform counts as its own distinct key (a
    platform-independent variant and a platform-tagged variant of the
    same distribution are both allowed to exist).
    """
    return (variant.distribution, variant.backend, variant.platform)


@dataclass(frozen=True, slots=True)
class AcceleratedVariant:
    """One declared accelerated variant of a distribution.

    ``distribution`` is canonicalized (PEP 503) and is the linkage key
    together with ``backend``.  ``version`` is the exact variant
    version (never a range — variants are concrete artifacts).
    ``backend`` must be a member of :data:`KNOWN_BACKENDS`.
    ``platform`` is ``None`` for platform-independent variants or a
    platform tag such as ``"linux_x86_64"`` otherwise.  ``sha256`` is
    an optional integrity digest of the variant artifact.
    """

    distribution: str
    version: str
    backend: str
    platform: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.distribution, str) or not self.distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        object.__setattr__(
            self, "distribution", canonicalize_name(self.distribution.strip())
        )

        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        object.__setattr__(self, "version", self.version.strip())

        backend = self.backend
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend must be a non-empty string")
        backend = backend.strip()
        if backend not in KNOWN_BACKENDS:
            raise ValueError(f"unsupported acceleration backend {backend!r}")
        object.__setattr__(self, "backend", backend)

        platform = self.platform
        if platform is not None:
            if not isinstance(platform, str) or not platform.strip():
                raise ValueError("platform must be None or a non-empty string")
            object.__setattr__(self, "platform", platform.strip())

        sha256 = self.sha256
        if sha256 is not None:
            if not isinstance(sha256, str) or not sha256.strip():
                raise ValueError("sha256 must be None or a non-empty string")
            object.__setattr__(self, "sha256", sha256.strip())


@dataclass(frozen=True, slots=True)
class AcceleratedVariantCatalog:
    """Immutable catalog of declared accelerated variants.

    Duplicate variants — same ``(distribution, backend, platform)`` key,
    with ``None`` platform as its own distinct key — are rejected at
    construction.  Lookups are fail-closed and deterministic: zero
    matches return ``None``, exactly one match returns that variant,
    and more than one match raises :class:`AmbiguousVariantError`.
    """

    variants: tuple[AcceleratedVariant, ...]

    def __post_init__(self) -> None:
        variants = tuple(self.variants)
        seen: dict[tuple[str, str, str | None], AcceleratedVariant] = {}
        for variant in variants:
            if not isinstance(variant, AcceleratedVariant):
                raise ValueError(
                    "variants must contain AcceleratedVariant values, "
                    f"got {type(variant).__qualname__}"
                )
            key = _variant_key(variant)
            if key in seen:
                raise ValueError(
                    f"duplicate variant for distribution {key[0]!r} "
                    f"backend {key[1]!r} platform {key[2]!r}"
                )
            seen[key] = variant
        object.__setattr__(self, "variants", variants)

    def find_variant(
        self,
        distribution: str,
        backend: str,
        platform_tag: str,
    ) -> AcceleratedVariant | None:
        """Find the variant for a declared need, fail-closed.

        * zero matches → ``None``;
        * exactly one match → that variant;
        * more than one match → :class:`AmbiguousVariantError` (never
          pick arbitrarily).

        A variant matches when its canonicalized distribution equals
        *distribution* (canonicalized), its backend equals *backend*,
        and its platform is ``None`` (platform-independent, matches any
        platform) or exactly equals *platform_tag*.
        """
        if not isinstance(distribution, str) or not distribution.strip():
            raise ValueError("distribution must be a non-empty string")
        canon_distribution = canonicalize_name(distribution.strip())

        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend must be a non-empty string")
        backend = backend.strip()

        if not isinstance(platform_tag, str) or not platform_tag.strip():
            raise ValueError("platform_tag must be a non-empty string")
        platform_tag = platform_tag.strip()

        matches = [
            variant
            for variant in self.variants
            if variant.distribution == canon_distribution
            and variant.backend == backend
            and (variant.platform is None or variant.platform == platform_tag)
        ]
        if len(matches) > 1:
            versions = ", ".join(sorted(v.version for v in matches))
            raise AmbiguousVariantError(
                f"multiple accelerated variants match distribution "
                f"{canon_distribution!r} backend {backend!r} platform "
                f"{platform_tag!r}: versions {versions}"
            )
        if len(matches) == 1:
            return matches[0]
        return None


def default_variant_catalog() -> AcceleratedVariantCatalog:
    """Return the empty variant catalog (pure fail-closed default).

    Kept empty for hermetic unit tests.  The production default is the
    manifest-derived catalog
    :func:`~zealfie.acceleration.acquisition.default_manifest_variant_catalog`
    (ZA-M1-2J Phase D).
    """
    return AcceleratedVariantCatalog(variants=())
