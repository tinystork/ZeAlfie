"""Canonical network transport helpers (ZA-M1-4 LOT C).

This module is the single place that decides *how* ZeAlfie talks to the
network, so every acquisition path (GitHub resolver/fetcher, pip
dependency download, accelerated artifact download, and later the
self-update path in LOT D) shares the same fail-closed posture:

* **Proxy-aware** — the canonical opener installs a default
  :class:`urllib.request.ProxyHandler`, so ``HTTP_PROXY``/``HTTPS_PROXY``/
  ``NO_PROXY`` environment variables are respected exactly as stdlib
  urllib would.
* **TLS verified** — the canonical opener uses the default SSL context;
  certificate + hostname verification is ON and can never be disabled
  from here (no ``ssl._create_unverified_context``, no
  ``check_hostname=False``, no ``verify=False``).
* **Transient-only retry** — :func:`retry_transient` retries only
  transient failures and never permanent ones (4xx, TLS validation,
  integrity/sha256 mismatches, validation errors).
* **No secret leakage** — every diagnostic string returned here is built
  from reason codes and sanitised proxy targets only; tokens,
  ``Authorization`` headers, and proxy credentials are never emitted.

Pure stdlib.  No third-party dependencies.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from enum import StrEnum
from typing import Callable, TypeVar

__all__ = [
    "NetworkReasonCode",
    "build_default_opener",
    "classify_exception",
    "effective_proxy_summary",
    "proxy_hint_for",
    "retry_transient",
]


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


class NetworkReasonCode(StrEnum):
    """Structured, machine-readable network failure reason codes.

    ``HTTP_ERROR`` deliberately carries only the *class* of HTTP failure;
    the numeric status is surfaced separately (in the message and, where
    available, on the caught :class:`urllib.error.HTTPError`).
    """

    DNS_FAILURE = "DNS_FAILURE"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    CONNECT_REFUSED = "CONNECT_REFUSED"
    CONNECT_RESET = "CONNECT_RESET"
    PROXY_AUTH_REQUIRED = "PROXY_AUTH_REQUIRED"
    PROXY_CONNECT_FAILED = "PROXY_CONNECT_FAILED"
    TLS_CERT_INVALID = "TLS_CERT_INVALID"
    HTTP_ERROR = "HTTP_ERROR"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_exception(
    exc: BaseException,
    *,
    http_status: int | None = None,
) -> tuple[NetworkReasonCode, str]:
    """Map a stdlib network exception to ``(reason_code, message)``.

    The returned *message* is short and human-safe: it never contains a
    URL, an ``Authorization`` header, a token, or proxy credentials.

    Mapping:

    * ``socket.gaierror`` → :attr:`NetworkReasonCode.DNS_FAILURE`
    * ``TimeoutError`` / ``socket.timeout`` →
      :attr:`NetworkReasonCode.CONNECT_TIMEOUT`
    * ``ConnectionRefusedError`` →
      :attr:`NetworkReasonCode.CONNECT_REFUSED`
    * ``ConnectionResetError`` →
      :attr:`NetworkReasonCode.CONNECT_RESET`
    * ``ssl.SSLCertVerificationError`` / ``ssl.SSLError`` →
      :attr:`NetworkReasonCode.TLS_CERT_INVALID`
    * ``urllib.error.HTTPError`` with code 407 →
      :attr:`NetworkReasonCode.PROXY_AUTH_REQUIRED`; any other code →
      :attr:`NetworkReasonCode.HTTP_ERROR`
    * ``urllib.error.URLError`` → delegate to its ``.reason`` recursively
    * ``http.client.HTTPException`` →
      :attr:`NetworkReasonCode.HTTP_ERROR`
    * ``OSError`` with no more-specific match →
      :attr:`NetworkReasonCode.OFFLINE`
    * anything else → :attr:`NetworkReasonCode.UNKNOWN`

    *http_status* overrides the numeric status when the exception does not
    carry one itself (e.g. a bare status surfaced from a subprocess).
    """
    # HTTPError subclasses URLError/OSError — check it first so a 407 is
    # never misclassified as OFFLINE.
    if isinstance(exc, urllib.error.HTTPError):
        status = http_status if http_status is not None else exc.code
        if status == 407:
            return (
                NetworkReasonCode.PROXY_AUTH_REQUIRED,
                "proxy authentication required (HTTP 407)",
            )
        return NetworkReasonCode.HTTP_ERROR, f"HTTP {status} error"

    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        # ``reason`` is normally the underlying exception (gaierror,
        # SSLError, ConnectionRefusedError, ...) but can be a bare string.
        if isinstance(reason, BaseException) and reason is not exc:
            return classify_exception(reason, http_status=http_status)
        return NetworkReasonCode.UNKNOWN, "unknown network error"

    # TLS validation failures must remain fail-closed and distinct.
    if isinstance(exc, ssl.SSLCertVerificationError):
        return (
            NetworkReasonCode.TLS_CERT_INVALID,
            "TLS certificate verification failed",
        )
    if isinstance(exc, ssl.SSLError):
        return NetworkReasonCode.TLS_CERT_INVALID, "TLS/SSL error"

    if isinstance(exc, socket.gaierror):
        return NetworkReasonCode.DNS_FAILURE, "DNS resolution failed"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return NetworkReasonCode.CONNECT_TIMEOUT, "connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return NetworkReasonCode.CONNECT_REFUSED, "connection refused"
    if isinstance(exc, ConnectionResetError):
        return NetworkReasonCode.CONNECT_RESET, "connection reset"

    if isinstance(exc, http.client.HTTPException):
        return NetworkReasonCode.HTTP_ERROR, "HTTP protocol error"

    if isinstance(exc, OSError):
        return NetworkReasonCode.OFFLINE, "network unreachable"

    return NetworkReasonCode.UNKNOWN, "unknown network error"


# ---------------------------------------------------------------------------
# Canonical opener
# ---------------------------------------------------------------------------


def build_default_opener(*, timeout: float) -> urllib.request.OpenerDirector:
    """Build the single canonical urllib opener.

    Explicitly installs a default :class:`~urllib.request.ProxyHandler`
    (so environment proxies are respected) and an
    :class:`~urllib.request.HTTPSHandler` with the **default** SSL
    context (TLS certificate + hostname verification ON).

    *timeout* is the per-request timeout consumers apply at
    ``opener.open(req, timeout=timeout)``; the opener itself does not
    bake in a default timeout because urllib applies timeouts per call.

    This is the one canonical opener.  Callers must never bypass it by
    constructing an opener with an unverified SSL context.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(),
    )


# ---------------------------------------------------------------------------
# Bounded transient retry
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def _is_transient_exception(exc: BaseException) -> bool:
    """Return True iff *exc* is a transient network failure.

    Transient: HTTP 5xx, connect timeout, connection refused/reset, and
    other transient ``URLError``/``OSError``/``socket.timeout``.

    Never transient (fail-closed): TLS validation errors, 4xx HTTP, and
    anything else (including ``ValueError`` and integrity/sha256
    mismatches, which are not network errors at all).
    """
    # TLS failures are permanent — never retried, even though SSLError
    # is technically an OSError subclass.
    if isinstance(exc, ssl.SSLError):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code is not None and exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException) and reason is not exc:
            return _is_transient_exception(reason)
        return False
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        return True
    return False


def retry_transient(
    fn: Callable[[], _T],
    *,
    retries: int = 2,
    delay: float = 0.5,
    is_transient: Callable[[BaseException], bool] | None = None,
) -> _T:
    """Call *fn*, retrying only transient failures up to *retries* times.

    * ``fn`` is invoked ``retries + 1`` times at most.
    * A failure is retried only when it is transient (5xx, DNS/connect
      timeout/refused/reset, transient ``URLError``/``OSError``,
      ``socket.timeout``).  Permanent failures — 4xx, TLS validation
      errors, integrity/sha256 mismatches, ``ValueError``/validation
      errors — are re-raised immediately with zero retries.
    * *is_transient* overrides the default classifier; it receives the
      caught exception and returns a bool.
    * On exhaustion the **original** exception is re-raised.

    Synchronous and stdlib-only; *delay* is a fixed sleep between retries.
    """
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - transient policy below
            last_exc = exc
            if attempt >= retries:
                break
            transient = (
                _is_transient_exception(exc)
                if is_transient is None
                else bool(is_transient(exc))
            )
            if not transient:
                raise
            if delay > 0:
                time.sleep(delay)
    assert last_exc is not None  # loop always runs at least once
    raise last_exc


# ---------------------------------------------------------------------------
# Proxy diagnostics (never leak credentials)
# ---------------------------------------------------------------------------


def _first_env(*names: str) -> str | None:
    """Return the first non-empty environment value among *names*."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _strip_proxy_credentials(value: str) -> str:
    """Return *value* with any ``user:pass@`` userinfo removed."""
    value = value.strip()
    if "@" not in value:
        return value
    before, _, after = value.rpartition("@")
    if "://" in before:
        scheme, _, _rest = before.partition("://")
        return f"{scheme}://{after}"
    return after


def effective_proxy_summary() -> str | None:
    """Return a short informational proxy summary, or ``None``.

    Derived from ``HTTPS_PROXY``/``HTTP_PROXY``/``ALL_PROXY`` (each
    case-insensitively).  Any ``user:pass@`` credentials are stripped,
    so the result is safe to log or show to a user.
    """
    https_proxy = _first_env("HTTPS_PROXY", "https_proxy")
    http_proxy = _first_env("HTTP_PROXY", "http_proxy")
    all_proxy = _first_env("ALL_PROXY", "all_proxy")
    if https_proxy:
        return (
            "https proxy from HTTPS_PROXY "
            f"({_strip_proxy_credentials(https_proxy)})"
        )
    if http_proxy:
        return (
            "http proxy from HTTP_PROXY "
            f"({_strip_proxy_credentials(http_proxy)})"
        )
    if all_proxy:
        return f"proxy from ALL_PROXY ({_strip_proxy_credentials(all_proxy)})"
    return None


def proxy_hint_for(reason_code: NetworkReasonCode) -> str | None:
    """Return a short human-safe proxy diagnostic for PROXY_* codes.

    Returns ``None`` for non-proxy reason codes.  Never includes proxy
    credentials.
    """
    if reason_code is NetworkReasonCode.PROXY_AUTH_REQUIRED:
        base = (
            "proxy authentication required; ZeAlfie does not support "
            "authenticated proxies"
        )
        summary = effective_proxy_summary()
        return f"{base} ({summary})" if summary else base
    if reason_code is NetworkReasonCode.PROXY_CONNECT_FAILED:
        base = "proxy connection failed"
        summary = effective_proxy_summary()
        return f"{base} ({summary})" if summary else base
    return None
