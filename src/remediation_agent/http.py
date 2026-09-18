"""Minimal HTTP client with retry, built on urllib so the package stays dependency-free.

Devin publishes no rate-limit headers and documents only that 429 can occur, so
retry policy here is conservative: exponential backoff, honour `Retry-After`
when present, and never retry a non-idempotent call that already succeeded.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})


class HttpError(RuntimeError):
    def __init__(self, status: int, method: str, url: str, body: str):
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        super().__init__(f"{method} {url} -> {status}: {body[:400]}")


class Http:
    def __init__(
        self,
        base: str,
        headers: dict[str, str],
        *,
        timeout: int = 60,
        max_attempts: int = 4,
    ) -> None:
        self.base = base.rstrip("/")
        self.headers = headers
        self.timeout = timeout
        self.max_attempts = max_attempts

    # -------------------------------------------------------------- internals

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        url = path if path.startswith("http") else f"{self.base}{path}"
        if not params:
            return url
        flat: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                flat.extend((key, str(v)) for v in value)
            elif isinstance(value, bool):
                flat.append((key, "true" if value else "false"))
            else:
                flat.append((key, str(value)))
        if not flat:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urllib.parse.urlencode(flat)}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        url = self._url(path, params)
        payload = json.dumps(body).encode() if body is not None else None
        retryable_method = method in IDEMPOTENT or payload is None

        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(url, method=method, data=payload,
                                         headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw)

            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                should_retry = (
                    exc.code in RETRY_STATUSES
                    and attempt < self.max_attempts
                    # A POST that failed with 429 never reached the handler, so it
                    # is safe to retry. A POST that 500s may have partially applied;
                    # do not retry those.
                    and (retryable_method or exc.code == 429)
                )
                if not should_retry:
                    raise HttpError(exc.code, method, url, text) from None

                delay = self._backoff(attempt, exc.headers.get("Retry-After"))
                log.warning("%s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                            method, url, exc.code, delay, attempt, self.max_attempts)
                time.sleep(delay)
                last = exc

            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self.max_attempts or not retryable_method:
                    raise
                delay = self._backoff(attempt, None)
                log.warning("%s %s -> %s, retrying in %.1fs", method, url, exc, delay)
                time.sleep(delay)
                last = exc

        raise RuntimeError(f"{method} {url} failed after {self.max_attempts} attempts") from last

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        # full jitter, capped
        return min(2.0 ** attempt, 30.0) * (0.5 + random.random() / 2)

    # -------------------------------------------------------------- verbs

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, params=params, body=body)

    def patch(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("PATCH", path, params=params, body=body)

    def delete(self, path: str, **params: Any) -> Any:
        return self.request("DELETE", path, params=params)

    def paginate(self, path: str, *, key: str = "items", **params: Any):
        """Iterate a Devin v3 cursor-paginated collection."""
        cursor = None
        while True:
            page = self.request("GET", path, params={**params, "after": cursor})
            if not page:
                return
            yield from page.get(key, [])
            if not page.get("has_next_page"):
                return
            cursor = page.get("end_cursor")
            if cursor is None:
                return
