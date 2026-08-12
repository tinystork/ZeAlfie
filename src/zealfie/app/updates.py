"""Read-only update detection for managed products (M1-2E LOT E.2).

Given active installed provenance (E.1) for a product, resolve the
requested source ref read-only and compare the resolved commit SHA with
the installed provenance commit SHA:

* same commit   → :attr:`UpdateStatus.UP_TO_DATE`
* different     → :attr:`UpdateStatus.UPDATE_AVAILABLE`
* no provenance → :attr:`UpdateStatus.PROVENANCE_UNKNOWN`
* resolve fails → :attr:`UpdateStatus.CHECK_FAILED`

Two further statuses exist for API/model and future-GUI completeness
(E.3 may add a real ``CHECKING`` state for startup background checks):

* :attr:`UpdateStatus.NOT_CHECKED` — a result that has not been computed.
* :attr:`UpdateStatus.CHECKING` — reserved for E.3; never produced here.

This module is **pure read-only**.  It never writes runtime state, the
:class:`~zealfie.runtime.provenance.ProductProvenanceStore`, the
``active.json`` pointer, the :class:`~zealfie.products.selection.SelectionStore`,
installs anything, launches anything, or applies any deployment.  The only
side effect is the injected *resolver* callable (which, in production,
performs a read-only network GET; in tests it is a pure fake).

Pure Python and Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from zealfie.runtime.provenance import ProductProvenance
from zealfie.sources import RemoteSource, SourceRefResolver, resolve_source


class UpdateStatus(StrEnum):
    """Stable statuses for a product's update check.

    Values are the exact strings a future GUI should render.  ``CHECKING``
    is reserved for E.3 (startup background checks) and is **never**
    produced by this module; it exists so state objects and UI code can
    model the full lifecycle today.
    """

    NOT_CHECKED = "NOT_CHECKED"
    CHECKING = "CHECKING"
    UP_TO_DATE = "UP_TO_DATE"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    CHECK_FAILED = "CHECK_FAILED"
    PROVENANCE_UNKNOWN = "PROVENANCE_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProductUpdateResult:
    """Immutable, read-only result of checking one product for updates.

    Field semantics:

    * ``status`` — the terminal check outcome.
    * ``installed_commit_sha`` — the installed provenance commit SHA
      (``None`` when provenance is unknown).
    * ``latest_commit_sha`` — the resolved commit SHA from the requested
      source ref (``None`` when resolution did not happen or failed).
    * ``source_owner`` / ``source_repo`` / ``requested_ref`` — where the
      installed source lives and the mutable ref that was requested.
    * ``version`` — the installed provenance version.
    * ``error`` — a human-readable reason, populated only for
      ``CHECK_FAILED``.
    """

    product_id: str
    status: UpdateStatus
    installed_commit_sha: str | None = None
    latest_commit_sha: str | None = None
    source_owner: str | None = None
    source_repo: str | None = None
    requested_ref: str | None = None
    version: str | None = None
    error: str | None = None

    @classmethod
    def not_checked(cls, product_id: str) -> "ProductUpdateResult":
        """Return a ``NOT_CHECKED`` result for *product_id*.

        Useful as a default/initial state for API consumers and the future
        GUI before any check has run.
        """
        return cls(product_id=product_id, status=UpdateStatus.NOT_CHECKED)


def check_product_update(
    product_id: str,
    provenance: ProductProvenance | None,
    *,
    resolver: SourceRefResolver,
) -> ProductUpdateResult:
    """Check *product_id* for an update, given its active provenance.

    This is the pure, dependency-injectable core of E.2.  It reads
    nothing from disk and writes nothing anywhere.  The *resolver* is
    the only side effect (injected; fakes in tests, GitHub in
    production).

    Outcome rules (in order):

    1. ``provenance is None`` → :attr:`UpdateStatus.PROVENANCE_UNKNOWN`
       (no resolver call, no invented SHA).
    2. The requested source is reconstructed from the provenance and
       resolved via :func:`zealfie.sources.resolve_source`.
    3. If resolution raises → :attr:`UpdateStatus.CHECK_FAILED` with the
       reason in ``error`` and **no** ``latest_commit_sha``.
    4. Resolved SHA == installed SHA → :attr:`UpdateStatus.UP_TO_DATE`.
    5. Resolved SHA != installed SHA → :attr:`UpdateStatus.UPDATE_AVAILABLE`
       carrying both SHAs.

    Never raises for missing provenance or resolver failure — those are
    *results*, not exceptions.
    """
    if provenance is None:
        return ProductUpdateResult(
            product_id=product_id,
            status=UpdateStatus.PROVENANCE_UNKNOWN,
        )

    try:
        source = RemoteSource(
            owner=provenance.source_owner,
            repo=provenance.source_repo,
            ref=provenance.requested_ref,
        )
        resolved = resolve_source(source, resolver=resolver)
    except Exception as exc:  # noqa: BLE001 - check boundary: never crash
        return ProductUpdateResult(
            product_id=product_id,
            status=UpdateStatus.CHECK_FAILED,
            installed_commit_sha=provenance.commit_sha,
            source_owner=provenance.source_owner,
            source_repo=provenance.source_repo,
            requested_ref=provenance.requested_ref,
            version=provenance.version,
            error=_error_message(exc),
        )

    if resolved.commit_sha == provenance.commit_sha:
        status = UpdateStatus.UP_TO_DATE
    else:
        status = UpdateStatus.UPDATE_AVAILABLE

    return ProductUpdateResult(
        product_id=product_id,
        status=status,
        installed_commit_sha=provenance.commit_sha,
        latest_commit_sha=resolved.commit_sha,
        source_owner=provenance.source_owner,
        source_repo=provenance.source_repo,
        requested_ref=provenance.requested_ref,
        version=provenance.version,
    )


def _error_message(exc: Exception) -> str:
    """Return a stable, human-readable reason from an exception.

    Falls back to the exception type name when the message is empty.
    """
    message = str(exc).strip()
    return message or type(exc).__name__
