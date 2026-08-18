"""Shared, canonical network transport helpers (ZA-M1-4 LOT C).

This package holds the single canonical opener, reason-code classifier,
bounded-retry policy, and proxy diagnostics used across every ZeAlfie
network acquisition path.

Fail-closed invariants (never relax these):

* TLS certificate + hostname verification is always ON.  There is no
  ``verify=False``, no ``check_hostname=False``, and no plain-HTTP
  fallback anywhere in this package.
* Retries cover only transient failures (5xx, DNS/connect/timeout/reset,
  transient OSError).  Permanent failures — 4xx, TLS validation errors,
  and integrity/sha256 mismatches — are raised immediately.
* Diagnostic text never includes secrets: no tokens, no ``Authorization``
  headers, and no proxy credentials.
"""

from __future__ import annotations

from .http import (
    NetworkReasonCode,
    build_default_opener,
    classify_exception,
    effective_proxy_summary,
    proxy_hint_for,
    retry_transient,
)

__all__ = [
    "NetworkReasonCode",
    "build_default_opener",
    "classify_exception",
    "effective_proxy_summary",
    "proxy_hint_for",
    "retry_transient",
]
