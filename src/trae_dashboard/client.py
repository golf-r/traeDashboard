"""Trae Enterprise OpenAPI client with retry support.

Calls POST /openapi/v1/statistics/user-model-usage with bearer-token auth.
The client retries retryable status codes (429, 5xx) with exponential
backoff and force-refreshes on 401. Only the user-model-usage endpoint is
permitted.
"""
from __future__ import annotations
import time
from typing import Any
import httpx

from .auth import TokenManager, AuthError


RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
DEFAULT_ENDPOINT = "/openapi/v1/statistics/user-model-usage"

# Only this data endpoint is allowed. Any other /statistics/* path is
# considered deprecated (e.g. /user-metrics was removed).
ALLOWED_DATA_ENDPOINTS = {"/openapi/v1/statistics/user-model-usage"}


class TraeAPIError(RuntimeError):
    """Raised when the Trae API returns a non-retryable error or retries are exhausted.

    The `kind` attribute lets callers branch on the failure category
    without parsing the message:
      - "auth"      — authentication failed (bad credentials, expired app_secret).
      - "http"      — non-retryable HTTP status (4xx other than 401/429).
      - "retryable" — retried MAX_RETRIES times and still failing (5xx/429/network).
      - "endpoint"  — the configured endpoint is not the allowed one (AssertionError path).
    """


class TraeAuthError(TraeAPIError):
    """Authentication failed (bad credentials, expired app_secret, etc.)."""


class TraeHTTPError(TraeAPIError):
    """Non-retryable HTTP error (4xx other than 401/429)."""


class TraeRetryExhaustedError(TraeAPIError):
    """Retried MAX_RETRIES times and still failing (5xx/429/network)."""


# Back-compat: callers that `except TraeAPIError` keep working, and any
# code that branched on the message can keep using the base class.
TraeAPIError.kind = "base"


class TraeClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_url: str,
        app_id: str,
        app_secret: str,
        transport: httpx.BaseTransport | None = None,
        ttl_skew: int = 300,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._endpoint = endpoint
        # auth_url can be absolute or relative to base
        if auth_url.startswith("http://") or auth_url.startswith("https://"):
            self._auth_url = auth_url
        else:
            self._auth_url = self._base + auth_url
        kwargs: dict = {"timeout": 30.0}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(**kwargs)
        self._tokens = TokenManager(
            client=self._client,
            auth_url=self._auth_url,
            app_id=app_id,
            app_secret=app_secret,
            ttl_skew=ttl_skew,
        )

    def get_model_usage(self, *, emails: list[str], start: int, end: int) -> dict[str, Any]:
        # Enforce that we only call the allowed data endpoint. Any other
        # /statistics/* path is rejected before any HTTP work.
        if "/user-model-usage" not in self._endpoint:
            raise AssertionError(
                f"unexpected endpoint: {self._endpoint}. "
                f"Only {ALLOWED_DATA_ENDPOINTS} is allowed."
            )
        body = {"start_time": start, "end_time": end, "emails": emails}
        url = self._base + self._endpoint
        last_exc: Exception | None = None
        auth_failures = 0
        for attempt in range(MAX_RETRIES):
            try:
                token = self._tokens.get_token()
                resp = self._client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 401:
                    # Force refresh on the next iteration. Counts as an
                    # attempt: a bad app_secret would otherwise loop here
                    # forever (we'd never consume `attempt` and never
                    # reach the MAX_RETRIES exit).
                    auth_failures += 1
                    self._tokens.invalidate()
                    if attempt + 1 >= MAX_RETRIES:
                        raise TraeAuthError(
                            f"auth kept failing (401) after {MAX_RETRIES} attempts"
                        )
                    continue
                if resp.status_code in RETRYABLE_STATUSES:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code != 200:
                    raise TraeHTTPError(
                        f"http {resp.status_code}: {resp.text[:200]}"
                    )
                return resp.json()
            except AuthError as e:
                raise TraeAuthError(f"auth failed: {e}") from e
            except httpx.HTTPError as e:
                last_exc = e
                time.sleep(2 ** attempt)
        raise TraeRetryExhaustedError(
            f"exhausted {MAX_RETRIES} retries: {last_exc or 'no response'}"
        )

    def close(self) -> None:
        self._client.close()
