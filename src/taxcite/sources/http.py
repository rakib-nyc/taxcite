"""A polite HTTP client for government sources.

Government publishers give this data away for free; the least TaxCite can do is not
hammer them. Every request carries a User-Agent naming the repository, requests are
rate-limited, failures back off exponentially, and every response is cached on disk so
that a rebuild re-downloads nothing it already has.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import shutil
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

import httpx

from taxcite.config import (
    HTTP_BACKOFF_BASE_SECONDS,
    HTTP_BACKOFF_MAX_SECONDS,
    HTTP_CACHE_TTL_SECONDS,
    HTTP_MAX_RETRIES,
    HTTP_RATE_LIMIT_RETRIES,
    HTTP_RETRY_AFTER_MAX_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MAX_REQUESTS_PER_SECOND,
    USER_AGENT,
    cache_dir,
)
from taxcite.errors import OfflineError, SourceNotFoundError, SourceUnavailableError

logger: Final = logging.getLogger("taxcite.http")

#: When set, every outbound request raises instead of being sent. This is a
#: process-wide latch rather than a per-client flag on purpose: "--offline" is a
#: promise a practitioner may be relying on under I.R.C. § 7216 and ordinary
#: confidentiality duties, and a promise kept by every caller remembering to pass a
#: flag is not a promise. Nothing turns it back off.
_NETWORK_SEALED = False


def seal_network() -> None:
    """Forbid every outbound request for the rest of the process.

    Irreversible by design. Call it once, early, when the user has asked for offline
    operation; after that no code path — including one added later — can reach the
    network without the failure being loud.
    """
    global _NETWORK_SEALED
    _NETWORK_SEALED = True
    logger.info("network access sealed for this process")


def network_is_sealed() -> bool:
    """Return ``True`` if outbound requests are forbidden."""
    return _NETWORK_SEALED


RETRY_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Rate limiting. Distinguished from the other retryable statuses because the right
#: response is different: wait as long as the server asks, try once or twice, and then
#: stop rather than spending a quota the rest of the run still needs.
RATE_LIMIT_STATUS: Final = 429

#: Statuses that mean the resource is genuinely absent, not that the source is down.
NOT_FOUND_STATUS: Final = frozenset({404, 410})


class RateLimiter:
    """A minimum-interval rate limiter shared by every request on a client."""

    def __init__(self, requests_per_second: float = MAX_REQUESTS_PER_SECOND) -> None:
        """Create a limiter allowing at most ``requests_per_second`` requests."""
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        """Block until the next request may be sent."""
        if self._interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last = time.monotonic()


class HttpClient:
    """An httpx client with retries, rate limiting, and an on-disk response cache."""

    def __init__(
        self,
        *,
        offline: bool = False,
        cache_root: Path | None = None,
        requests_per_second: float = MAX_REQUESTS_PER_SECOND,
        user_agent: str = USER_AGENT,
    ) -> None:
        """Create a client. In ``offline`` mode only cached responses are served."""
        self.offline = offline
        self._cache_root = cache_root
        self._limiter = RateLimiter(requests_per_second)
        self._user_agent = user_agent
        self._client: httpx.Client | None = None

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> Self:
        """Enter a context manager that closes the underlying connection pool."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying connection pool."""
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        """Return the lazily created httpx client."""
        if self._client is None:
            self._client = httpx.Client(
                timeout=HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={
                    "User-Agent": self._user_agent,
                    # The eCFR API answers 406 unless the request accepts a compressed
                    # response, and compressing is the polite thing to do regardless.
                    "Accept-Encoding": "gzip, deflate",
                },
            )
        return self._client

    # -- cache -------------------------------------------------------------------

    def _cache_dir(self) -> Path:
        if self._cache_root is not None:
            self._cache_root.mkdir(parents=True, exist_ok=True)
            return self._cache_root
        return cache_dir()

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        root = self._cache_dir()
        return root / f"{digest}.body", root / f"{digest}.meta.json"

    def _read_cache(self, url: str, ttl: float) -> bytes | None:
        body, meta = self._cache_paths(url)
        if not body.exists() or not meta.exists():
            return None
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not self.offline and time.time() - float(info.get("fetched_at", 0)) > ttl:
            return None
        logger.debug("cache hit: %s", url)
        return body.read_bytes()

    def _write_cache(self, url: str, content: bytes) -> None:
        body, meta = self._cache_paths(url)
        body.write_bytes(content)
        meta.write_text(
            json.dumps({"url": url, "fetched_at": time.time(), "bytes": len(content)}),
            encoding="utf-8",
        )

    # -- requests ----------------------------------------------------------------

    def get_bytes(
        self,
        url: str,
        *,
        use_cache: bool = True,
        ttl: float = HTTP_CACHE_TTL_SECONDS,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Fetch ``url`` and return the body, serving from the disk cache when possible.

        Raises:
            OfflineError: in offline mode with nothing cached.
            SourceUnavailableError: if the source cannot be reached after retries.
        """
        full_url = str(httpx.URL(url, params=params)) if params else url
        if use_cache:
            cached = self._read_cache(full_url, ttl)
            if cached is not None:
                return cached
        if self.offline:
            raise OfflineError(
                f"offline: {full_url} is not in the local cache",
                hint="Re-run without --offline, or run: taxcite build-index",
            )
        content = self._request(full_url, headers=headers)
        if use_cache:
            self._write_cache(full_url, content)
        return content

    def get_text(self, url: str, **kwargs: Any) -> str:
        """Fetch ``url`` and decode the body as UTF-8."""
        return self.get_bytes(url, **kwargs).decode("utf-8", errors="replace")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """Fetch ``url`` and parse the body as JSON.

        Raises:
            SourceUnavailableError: if the body is not valid JSON.
        """
        raw = self.get_bytes(url, **kwargs)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceUnavailableError(f"{url} did not return valid JSON: {exc}") from exc

    def _request(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        """Perform a rate-limited GET with exponential backoff.

        Raises:
            OfflineError: if network access has been sealed for this process.
            SourceUnavailableError: if the source cannot be reached after retries.
        """
        if _NETWORK_SEALED:
            raise OfflineError(
                f"offline mode is in force; refusing to request {url}",
                hint="Re-run without --offline to allow network access.",
            )
        last_error: str = "unknown error"
        throttled = 0
        for attempt in range(HTTP_MAX_RETRIES):
            self._limiter.wait()
            try:
                response = self.client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                logger.warning("request failed (%s): %s", url, exc)
            else:
                if response.status_code < 400:
                    return response.content
                last_error = f"HTTP {response.status_code}"
                if response.status_code in NOT_FOUND_STATUS:
                    raise SourceNotFoundError(f"{url} returned {last_error}")
                if response.status_code not in RETRY_STATUS:
                    raise SourceUnavailableError(f"{url} returned {last_error}")
                if response.status_code == RATE_LIMIT_STATUS:
                    throttled += 1
                    if throttled > HTTP_RATE_LIMIT_RETRIES:
                        raise SourceUnavailableError(
                            f"{url} is rate limiting this client ({last_error})",
                            hint=(
                                "Wait a few minutes, or set COURTLISTENER_TOKEN for a larger quota."
                            ),
                        )
                    logger.warning("rate limited by %s", url)
                    self._sleep_retry_after(response, attempt)
                    continue
                logger.warning("retryable %s from %s", last_error, url)
            self._sleep_backoff(attempt)
        raise SourceUnavailableError(
            f"{url} could not be fetched after {HTTP_MAX_RETRIES} attempts: {last_error}"
        )

    def _sleep_retry_after(self, response: httpx.Response, attempt: int) -> None:
        """Wait as long as a throttling server asks, within reason."""
        raw = response.headers.get("Retry-After", "")
        try:
            asked = float(raw)
        except ValueError:
            self._sleep_backoff(attempt)
            return
        time.sleep(min(max(asked, 0.0), HTTP_RETRY_AFTER_MAX_SECONDS))

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        """Sleep for an exponentially growing, jittered interval."""
        delay = min(HTTP_BACKOFF_BASE_SECONDS * 2**attempt, HTTP_BACKOFF_MAX_SECONDS)
        # Jitter, not cryptography: spread retries so parallel clients do not sync up.
        time.sleep(delay * (0.5 + random.random() / 2))

    def download(self, url: str, destination: Path, *, force: bool = False) -> Path:
        """Stream ``url`` to ``destination``, skipping the download if it already exists.

        Raises:
            OfflineError: in offline mode when the file is not already present.
            SourceUnavailableError: if the download fails after retries.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not force:
            logger.info("already downloaded: %s", destination)
            return destination
        if _NETWORK_SEALED:
            raise OfflineError(
                f"offline mode is in force; refusing to download {url}",
                hint="Re-run without --offline to allow network access.",
            )
        if self.offline:
            raise OfflineError(f"offline: {destination} has not been downloaded")
        temporary = destination.with_suffix(destination.suffix + ".part")
        last_error = "unknown error"
        for attempt in range(HTTP_MAX_RETRIES):
            self._limiter.wait()
            try:
                with (
                    self.client.stream("GET", url) as response,
                    temporary.open("wb") as handle,
                ):
                    if response.status_code >= 400:
                        last_error = f"HTTP {response.status_code}"
                        if response.status_code not in RETRY_STATUS:
                            raise SourceUnavailableError(f"{url} returned {last_error}")
                        raise httpx.HTTPError(last_error)
                    for chunk in response.iter_bytes(chunk_size=1 << 20):
                        handle.write(chunk)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                logger.warning("download failed (%s): %s", url, exc)
                self._sleep_backoff(attempt)
                continue
            shutil.move(str(temporary), str(destination))
            return destination
        temporary.unlink(missing_ok=True)
        raise SourceUnavailableError(f"could not download {url}: {last_error}")
