"""Bearer token manager with TTL cache and refresh on expiry."""
from __future__ import annotations
import threading
import time
import httpx


class AuthError(RuntimeError):
    """Raised when authentication fails."""


class TokenManager:
    """Caches an access_token and refreshes it before expiry.

    ttl_skew: refresh the token this many seconds before its reported expiry
              (default 300s = 5 min) to avoid races.

    Thread safety: a `threading.Lock` guards `_refresh` so concurrent
    first-time callers (FastAPI sync routes + scheduler background thread)
    don't all POST to the auth endpoint at once. The lock is held only
    during the refresh, not during the cached return.
    """

    def __init__(
        self,
        *,
        client: httpx.Client,
        auth_url: str,
        app_id: str,
        app_secret: str,
        ttl_skew: int = 300,
    ) -> None:
        self._client = client
        self._auth_url = auth_url
        self._app_id = app_id
        self._app_secret = app_secret
        self._ttl_skew = ttl_skew
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - self._ttl_skew:
            return self._token
        # Serialize refreshes across threads: only one caller POSTs to
        # the auth endpoint; the rest wait and then reuse the freshly
        # cached token (which is what the inner check sees on retry).
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - self._ttl_skew:
                return self._token
            return self._refresh()

    def invalidate(self) -> None:
        """Force the next get_token() call to re-authenticate."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _refresh(self) -> str:
        try:
            resp = self._client.post(
                self._auth_url,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
        except httpx.HTTPError as e:
            raise AuthError(f"auth transport error: {e}") from e
        if resp.status_code != 200:
            raise AuthError(f"auth failed: http {resp.status_code}: {resp.text}")
        try:
            body = resp.json()
        except Exception as e:
            raise AuthError(f"auth response not json: {e}") from e
        # Trae API wraps everything under `data` and uses `expire` (not `expires_in`).
        # Some older endpoints may return token at the top level; handle both.
        data = body.get("data", body) if isinstance(body, dict) else {}
        token = (
            data.get("access_token")
            or data.get("token")
            or body.get("access_token")
            or body.get("token")
        )
        if not token:
            raise AuthError(f"no token in response: {body}")
        expires_in = int(
            data.get("expire")
            or data.get("expires_in")
            or body.get("expire")
            or body.get("expires_in")
            or 7200
        )
        self._token = token
        self._expires_at = time.time() + expires_in
        return token
