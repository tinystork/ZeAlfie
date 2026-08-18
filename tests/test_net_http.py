"""Tests for ZA-M1-4 LOT C — shared network transport helpers.

Hermetic: no real network, no real proxies, no real subprocess.  Tests
cover the reason-code classifier, the canonical opener, bounded
transient-only retry, proxy diagnostics, and the fail-closed
TLS / integrity / credential-leak invariants.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.request
from urllib.error import HTTPError, URLError

import pytest

from zealfie.acceleration import Sha256Mismatch
from zealfie.net import (
    NetworkReasonCode,
    build_default_opener,
    classify_exception,
    effective_proxy_summary,
    proxy_hint_for,
    retry_transient,
)
from zealfie.sources import SourceResolutionError
from zealfie.sources.github import GitHubSourceRefResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raises_once(exc: BaseException):
    """Return a function that records each call then raises *exc*."""
    calls: list[int] = []

    def fn():
        calls.append(1)
        raise exc

    return fn, calls


class _RaisingHandler(urllib.request.BaseHandler):
    """A handler whose https_open raises a canned error."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def https_open(self, req):
        raise self._error


def _failing_opener(error: BaseException) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    opener.add_handler(_RaisingHandler(error))
    return opener


# ---------------------------------------------------------------------------
# 1. Proxy-aware opener + proxy summary
# ---------------------------------------------------------------------------


def test_build_default_opener_includes_proxy_and_https_handlers(monkeypatch):
    """With a proxy env var set, the canonical opener includes a
    ProxyHandler (proxy-aware) and an HTTPSHandler (TLS-verified)."""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.example:8080")
    opener = build_default_opener(timeout=30)
    assert any(
        isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers
    )
    assert any(
        isinstance(h, urllib.request.HTTPSHandler) for h in opener.handlers
    )


def test_build_default_opener_always_has_https_handler(monkeypatch):
    """Even with no proxy env, the canonical opener keeps an HTTPSHandler."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    opener = build_default_opener(timeout=30)
    assert any(
        isinstance(h, urllib.request.HTTPSHandler) for h in opener.handlers
    )


def test_effective_proxy_summary_reports_without_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@host:8080")
    summary = effective_proxy_summary()
    assert summary is not None
    assert "host:8080" in summary
    assert "secret" not in summary
    assert "user" not in summary


def test_effective_proxy_summary_none_without_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    assert effective_proxy_summary() is None


# ---------------------------------------------------------------------------
# 2. Classifier
# ---------------------------------------------------------------------------


def test_classify_dns_failure():
    code, _ = classify_exception(socket.gaierror("Name or service not known"))
    assert code is NetworkReasonCode.DNS_FAILURE


def test_classify_connect_timeout():
    code, _ = classify_exception(TimeoutError("timed out"))
    assert code is NetworkReasonCode.CONNECT_TIMEOUT


def test_classify_connect_refused():
    code, _ = classify_exception(ConnectionRefusedError("refused"))
    assert code is NetworkReasonCode.CONNECT_REFUSED


def test_classify_connect_reset():
    code, _ = classify_exception(ConnectionResetError("reset"))
    assert code is NetworkReasonCode.CONNECT_RESET


def test_classify_tls_cert_invalid():
    code, _ = classify_exception(
        ssl.SSLCertVerificationError(20, "unable to get local issuer certificate")
    )
    assert code is NetworkReasonCode.TLS_CERT_INVALID
    code, _ = classify_exception(ssl.SSLError("tls failure"))
    assert code is NetworkReasonCode.TLS_CERT_INVALID


def test_classify_proxy_auth_407():
    code, _ = classify_exception(
        HTTPError("http://x", 407, "Proxy Authentication Required", {}, None)
    )
    assert code is NetworkReasonCode.PROXY_AUTH_REQUIRED


def test_classify_http_error_carries_status():
    code, msg = classify_exception(
        HTTPError("http://x", 404, "Not Found", {}, None)
    )
    assert code is NetworkReasonCode.HTTP_ERROR
    assert "404" in msg


def test_classify_urlerror_delegates_to_reason():
    code, _ = classify_exception(URLError(socket.timeout("timed out")))
    assert code is NetworkReasonCode.CONNECT_TIMEOUT


def test_classify_offline_oserror():
    code, _ = classify_exception(OSError(101, "Network is unreachable"))
    assert code is NetworkReasonCode.OFFLINE


def test_classify_unknown_for_non_network():
    code, _ = classify_exception(ValueError("not a network error"))
    assert code is NetworkReasonCode.UNKNOWN


def test_classify_messages_never_include_url():
    """HTTPError messages must not echo the request URL (which may carry
    query strings)."""
    _, msg = classify_exception(
        HTTPError("http://x/path?token=abc123", 403, "Forbidden", {}, None)
    )
    assert "token" not in msg
    assert "http://x" not in msg


# ---------------------------------------------------------------------------
# 3. Retry policy (transient-only)
# ---------------------------------------------------------------------------


def test_retry_transient_urlerror_timeout():
    fn, calls = _raises_once(URLError(socket.timeout("timed out")))
    with pytest.raises(URLError):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 3


def test_retry_transient_5xx():
    fn, calls = _raises_once(
        HTTPError("http://x", 503, "Service Unavailable", {}, None)
    )
    with pytest.raises(HTTPError):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 3


def test_retry_transient_connection_refused():
    fn, calls = _raises_once(ConnectionRefusedError("refused"))
    with pytest.raises(ConnectionRefusedError):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 3


def test_no_retry_4xx():
    fn, calls = _raises_once(HTTPError("http://x", 404, "Not Found", {}, None))
    with pytest.raises(HTTPError):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 1


def test_no_retry_tls_cert_verification():
    fn, calls = _raises_once(
        ssl.SSLCertVerificationError(20, "unable to get local issuer certificate")
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 1


def test_no_retry_value_error():
    fn, calls = _raises_once(ValueError("validation failed"))
    with pytest.raises(ValueError):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 1


def test_no_retry_integrity_mismatch():
    """Sha256Mismatch (integrity) is never retried — fail-closed."""
    fn, calls = _raises_once(Sha256Mismatch("sha256 mismatch"))
    with pytest.raises(Sha256Mismatch):
        retry_transient(fn, retries=2, delay=0)
    assert len(calls) == 1
    # The classifier also never maps integrity to a network code.
    code, _ = classify_exception(Sha256Mismatch("sha256 mismatch"))
    assert code is NetworkReasonCode.UNKNOWN


def test_retry_preserves_original_exception():
    original = URLError(socket.timeout("timed out"))
    fn, _ = _raises_once(original)
    with pytest.raises(URLError) as exc_info:
        retry_transient(fn, retries=2, delay=0)
    assert exc_info.value is original


def test_retry_zero_retries_runs_once():
    fn, calls = _raises_once(URLError(socket.timeout("timed out")))
    with pytest.raises(URLError):
        retry_transient(fn, retries=0, delay=0)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 4. Credential / proxy leak (integration via resolver)
# ---------------------------------------------------------------------------


def test_proxy_credentials_never_leak(monkeypatch):
    """A 407 proxy failure must not leak proxy or token secrets in the
    raised error message, reason code, or proxy hint."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxyuser:proxysecret@proxy.example:8080")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecrettoken")

    opener = _failing_opener(
        HTTPError(
            "https://api.github.com/repos/o/r/commits/main",
            407,
            "Proxy Authentication Required",
            {},
            None,
        )
    )
    resolver = GitHubSourceRefResolver(_opener=opener, retries=0)
    with pytest.raises(SourceResolutionError) as exc_info:
        resolver("o", "r", "main")

    exc = exc_info.value
    text = str(exc)
    assert "proxysecret" not in text
    assert "proxyuser" not in text
    assert "ghp_supersecrettoken" not in text

    assert exc.reason_code is NetworkReasonCode.PROXY_AUTH_REQUIRED
    hint = exc.proxy_hint or ""
    assert "proxysecret" not in hint
    assert "proxyuser" not in hint
    assert "ghp_supersecrettoken" not in hint
    # The sanitised proxy target is still surfaced (useful, non-secret).
    assert "proxy.example:8080" in hint


def test_proxy_hint_for_non_proxy_codes_is_none():
    assert proxy_hint_for(NetworkReasonCode.DNS_FAILURE) is None
    assert proxy_hint_for(NetworkReasonCode.TLS_CERT_INVALID) is None


def test_proxy_hint_for_proxy_codes(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.example:8080")
    hint = proxy_hint_for(NetworkReasonCode.PROXY_AUTH_REQUIRED)
    assert hint is not None
    assert "secret" not in hint
    assert "proxy.example:8080" in hint
