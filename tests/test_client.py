"""Tests for trae_dashboard.client."""
from __future__ import annotations
import json
import httpx
import pytest

from trae_dashboard.client import (
    TraeClient,
    TraeAPIError,
    TraeAuthError,
    TraeHTTPError,
    TraeRetryExhaustedError,
)


def test_get_model_usage_success_with_transport():
    """TraeClient calls the API via httpx (injected via MockTransport)."""

    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(200, json={
            "code": 0, "message": "ok", "request_id": "r",
            "data": {"items": [
                {"email": "a@x.com", "model_usage": [
                    {"model_name": "M", "model_type": "Chat", "model_source": "Trae",
                     "usage": {"input_tokens": 1, "output_tokens": 2}}
                ]}
            ]},
        })

    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    result = client.get_model_usage(emails=["a@x.com"], start=1, end=2)
    assert result["data"]["items"][0]["email"] == "a@x.com"
    client.close()


def test_get_model_usage_429_retries_with_backoff(monkeypatch):
    """429 should be retried with exponential backoff."""
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        # data calls are calls 2, 3, ... ; first 2 data calls return 429
        if calls["n"] in (2, 3):
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"code": 0, "message": "ok", "request_id": "r", "data": {"items": []}})

    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    result = client.get_model_usage(emails=["a"], start=1, end=2)
    assert result["data"]["items"] == []
    # 2 data-call 429s + 1 success
    assert calls["n"] == 4  # auth + 2x429 + success
    # Sleeps should follow 2^attempt pattern: 1, 2 after each 429
    assert sleeps == [1, 2]
    client.close()


def test_get_model_usage_500_retries_with_backoff(monkeypatch):
    """500 should be retried with exponential backoff."""
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        if calls["n"] == 2:  # first data call (auth is call 1) returns 500
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"code": 0, "message": "ok", "request_id": "r", "data": {"items": []}})

    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    result = client.get_model_usage(emails=["a"], start=1, end=2)
    assert result["data"]["items"] == []
    assert calls["n"] == 3  # auth + 500 + success
    assert sleeps == [1]
    client.close()


def test_get_model_usage_401_refreshes_token(monkeypatch):
    """A 401 response should force the token to refresh and retry."""
    calls = {"n": 0, "auth_calls": 0}

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            calls["auth_calls"] += 1
            return httpx.Response(200, json={"access_token": f"T{calls['auth_calls']}", "expires_in": 3600})
        if calls["n"] == 2:  # first data call returns 401
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"code": 0, "message": "ok", "request_id": "r", "data": {"items": []}})

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    result = client.get_model_usage(emails=["a"], start=1, end=2)
    assert result["data"]["items"] == []
    assert calls["auth_calls"] == 2  # re-authenticated exactly once
    client.close()


def test_get_model_usage_exhausts_retries(monkeypatch):
    """After MAX_RETRIES retryable failures, should raise TraeAPIError."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TraeAPIError):
        client.get_model_usage(emails=["a"], start=1, end=2)
    client.close()


def test_get_model_usage_non_2xx_raises(monkeypatch):
    """Non-retryable HTTP status (e.g. 400) raises TraeAPIError."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(400, text="bad request")

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TraeAPIError):
        client.get_model_usage(emails=["a"], start=1, end=2)
    client.close()


# ---------------------------------------------------------------------------
# T3 — Single data endpoint enforcement
# ---------------------------------------------------------------------------


def test_only_user_model_usage_endpoint_exists():
    """Grep the source for any /statistics/ path to ensure we only use one.

    The dashboard MUST only call /openapi/v1/statistics/user-model-usage.
    No other /openapi/v1/statistics/* path should appear in production code.
    """
    import re
    from pathlib import Path

    src_dir = Path(__file__).resolve().parent.parent / "src" / "trae_dashboard"
    pattern = re.compile(r"/openapi/v\d+/statistics/(\w[\w-]*)")
    found: set[str] = set()
    for f in src_dir.glob("*.py"):
        for m in pattern.finditer(f.read_text(encoding="utf-8")):
            found.add(m.group(1))
    assert found == {"user-model-usage"}, (
        f"unexpected endpoints used in production code: {found}"
    )


def test_client_endpoint_is_user_model_usage():
    """The client's configured endpoint must be /user-model-usage."""
    from trae_dashboard.client import DEFAULT_ENDPOINT

    assert DEFAULT_ENDPOINT.endswith("/user-model-usage"), (
        f"DEFAULT_ENDPOINT changed: {DEFAULT_ENDPOINT}"
    )
    assert "/statistics/" in DEFAULT_ENDPOINT


def test_client_get_model_usage_asserts_endpoint_path(monkeypatch):
    """get_model_usage should refuse to run if the configured endpoint is not the allowed one.

    Construct a client pointing at a non-allowed endpoint and assert that
    the call raises AssertionError before any HTTP work happens.
    """
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        endpoint="/openapi/v1/statistics/user-metrics",  # wrong endpoint
    )
    with pytest.raises(AssertionError):
        client.get_model_usage(emails=["a"], start=1, end=2)
    client.close()


def test_load_config_rejects_user_metrics_endpoint(tmp_data_dir, monkeypatch):
    """load_config should refuse a config that points data endpoint at user-metrics."""
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: https://api\n"
        "auth_endpoint: /openapi/v1/auth/access_token\n"
        "data_endpoint: /openapi/v1/statistics/user-metrics\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="user-metrics"):
        from trae_dashboard.config import load_config

        load_config(cfg_file)


# ---------------------------------------------------------------------------
# Regression: 401 must terminate after MAX_RETRIES, not loop forever.
# This is a regression test for the 401 infinite-loop bug (the old code
# called `continue` without consuming `attempt`, so a permanently-401
# upstream would never exit the retry loop).
# ---------------------------------------------------------------------------


def test_get_model_usage_persistent_401_eventually_raises(monkeypatch):
    """A persistent 401 (e.g. bad app_secret) must terminate, not loop."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            # Auth endpoint itself returns 200, but the data endpoint
            # always returns 401 — simulates an invalid/expired token
            # that re-auth does NOT fix.
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TraeAuthError):
        client.get_model_usage(emails=["a"], start=1, end=2)
    # The loop must terminate. Total calls = auth(1) + MAX_RETRIES(5) data calls.
    # The exact number is an implementation detail; the contract is "it exits".
    assert calls["n"] <= 50, f"client appears to be looping forever ({calls['n']} calls)"
    client.close()


def test_get_model_usage_401_then_success_still_works(monkeypatch):
    """The fix must not break the legitimate case: 401 → refresh → success."""
    calls = {"n": 0, "auth_calls": 0}

    def handler(request):
        calls["n"] += 1
        if "/auth" in str(request.url):
            calls["auth_calls"] += 1
            return httpx.Response(200, json={"access_token": f"T{calls['auth_calls']}", "expires_in": 3600})
        if calls["n"] == 2:  # first data call returns 401
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"code": 0, "message": "ok", "request_id": "r", "data": {"items": []}})

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    result = client.get_model_usage(emails=["a"], start=1, end=2)
    assert result["data"]["items"] == []
    assert calls["auth_calls"] == 2  # initial + one refresh
    client.close()


def test_retry_exhausted_raises_specific_subclass(monkeypatch):
    """5xx exhaustion raises TraeRetryExhaustedError (subclass of TraeAPIError)."""
    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    # Both the specific subclass and the base class should catch it.
    with pytest.raises(TraeRetryExhaustedError):
        client.get_model_usage(emails=["a"], start=1, end=2)
    client.close()


def test_non_retryable_4xx_raises_http_subclass(monkeypatch):
    """Non-retryable 4xx (e.g. 400) raises TraeHTTPError (subclass of TraeAPIError)."""
    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(400, text="bad request")

    monkeypatch.setattr("time.sleep", lambda s: None)
    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="id",
        app_secret="sec",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TraeHTTPError):
        client.get_model_usage(emails=["a"], start=1, end=2)
    client.close()
