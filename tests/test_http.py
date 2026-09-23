"""The HTTP client's manners: rate limits, retries, and the offline seal.

These matter because the sources are public services run by a government office and a
non-profit. A client that retries hard against a throttled endpoint is not merely
impolite — it spends a quota the rest of the run still needs, and turns one limit into
a report full of spurious outages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from taxcite.errors import SourceNotFoundError, SourceUnavailableError
from taxcite.sources import http as http_module
from taxcite.sources.http import HttpClient

#: The real request loop, captured at import time — before the autouse guard in
#: conftest replaces it. This module is the one place that legitimately puts it back,
#: because the loop *is* what is under test; no socket is opened, since every test
#: stubs the transport underneath it.
_REAL_REQUEST = HttpClient._request


class FakeResponse:
    """Just enough of an httpx response for the retry loop."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        """Hold a status code and any headers the loop reads."""
        self.status_code = status
        self.headers = headers or {}
        self.content = b"ok"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HttpClient:
    """A client with waiting stubbed out, so the tests do not actually sleep."""
    monkeypatch.setenv("TAXCITE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(HttpClient, "_request", _REAL_REQUEST)
    # The offline seal is process-wide and deliberately irreversible, so an earlier
    # test that sealed it would otherwise decide the outcome of every test here.
    # monkeypatch puts it back at teardown, so the latch's own guarantee is untouched.
    monkeypatch.setattr(http_module, "_NETWORK_SEALED", False)
    made = HttpClient()
    monkeypatch.setattr(made._limiter, "wait", lambda: None)
    return made


def respond(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]
) -> list[str]:
    """Serve ``responses`` in order and return the log of requests made."""
    calls: list[str] = []
    queue = list(responses)

    def get(url: str, headers: Any = None) -> FakeResponse:
        del headers
        calls.append(url)
        return queue.pop(0) if queue else responses[-1]

    monkeypatch.setattr(client.client, "get", get)
    monkeypatch.setattr(HttpClient, "_sleep_backoff", staticmethod(lambda _a: None))
    monkeypatch.setattr(http_module.time, "sleep", lambda _s: None)
    return calls


def test_a_successful_request_is_made_once(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = respond(client, monkeypatch, [FakeResponse(200)])
    assert client.get_bytes("https://example.gov/a") == b"ok"
    assert len(calls) == 1


def test_a_missing_document_is_not_retried(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 is an answer, not a failure: the thing asked for does not exist."""
    calls = respond(client, monkeypatch, [FakeResponse(404)])
    with pytest.raises(SourceNotFoundError):
        client.get_bytes("https://example.gov/missing")
    assert len(calls) == 1


def test_a_server_error_is_retried(client: HttpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = respond(client, monkeypatch, [FakeResponse(503), FakeResponse(503), FakeResponse(200)])
    assert client.get_bytes("https://example.gov/flaky") == b"ok"
    assert len(calls) == 3


def test_rate_limiting_gives_up_quickly(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 is a quota. Retrying five times spends what the run still needs."""
    calls = respond(client, monkeypatch, [FakeResponse(429)] * 10)
    with pytest.raises(SourceUnavailableError, match="rate limiting"):
        client.get_bytes("https://example.gov/throttled")
    assert len(calls) <= 3


def test_rate_limiting_still_recovers_if_the_server_relents(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = respond(client, monkeypatch, [FakeResponse(429), FakeResponse(200)])
    assert client.get_bytes("https://example.gov/throttled") == b"ok"
    assert len(calls) == 2


def test_retry_after_is_honoured(client: HttpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a server says how long to wait, wait that long rather than guessing."""
    slept: list[float] = []
    respond(client, monkeypatch, [FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200)])
    monkeypatch.setattr(http_module.time, "sleep", slept.append)
    assert client.get_bytes("https://example.gov/throttled") == b"ok"
    assert slept == [7.0]


def test_an_unreasonable_retry_after_is_capped(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server asking for an hour is telling us to come back later, not to block."""
    from taxcite.config import HTTP_RETRY_AFTER_MAX_SECONDS

    slept: list[float] = []
    respond(client, monkeypatch, [FakeResponse(429, {"Retry-After": "3600"}), FakeResponse(200)])
    monkeypatch.setattr(http_module.time, "sleep", slept.append)
    client.get_bytes("https://example.gov/throttled")
    assert slept == [HTTP_RETRY_AFTER_MAX_SECONDS]


def test_a_retry_after_that_is_a_date_falls_back_to_backoff(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header may carry an HTTP date; that must not raise."""
    respond(
        client,
        monkeypatch,
        [FakeResponse(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), FakeResponse(200)],
    )
    assert client.get_bytes("https://example.gov/throttled") == b"ok"


def test_a_transport_error_is_retried(client: HttpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def get(url: str, headers: Any = None) -> FakeResponse:
        del headers
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectError("no route")
        return FakeResponse(200)

    monkeypatch.setattr(client.client, "get", get)
    monkeypatch.setattr(HttpClient, "_sleep_backoff", staticmethod(lambda _a: None))
    assert client.get_bytes("https://example.gov/down") == b"ok"
    assert len(calls) == 3
