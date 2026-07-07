"""Tests for trae_dashboard.auth."""
from __future__ import annotations
import time
import pytest
import httpx

from trae_dashboard.auth import TokenManager, AuthError


class FakeTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(200, json={"access_token": "tok_1", "expires_in": 3600})
        return httpx.Response(200, json={"access_token": "tok_2", "expires_in": 3600})


def test_token_caches_and_refreshes_on_expiry():
    ft = FakeTransport()
    client = httpx.Client(transport=ft, base_url="https://x")
    tm = TokenManager(
        client=client,
        auth_url="https://x/auth",
        app_id="id",
        app_secret="sec",
        ttl_skew=300,
    )
    t1 = tm.get_token()
    assert t1 == "tok_1"
    t2 = tm.get_token()  # cached
    assert t2 == "tok_1"
    assert ft.calls == 1
    # Force expire
    tm._expires_at = time.time() - 1
    t3 = tm.get_token()
    assert t3 == "tok_2"
    assert ft.calls == 2


def test_token_auth_error_raises():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tm = TokenManager(
        client=client,
        auth_url="https://x/auth",
        app_id="id",
        app_secret="sec",
    )
    with pytest.raises(AuthError):
        tm.get_token()


def test_token_no_token_in_response_raises():
    def handler(request):
        return httpx.Response(200, json={"error": "no token"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tm = TokenManager(
        client=client,
        auth_url="https://x/auth",
        app_id="id",
        app_secret="sec",
    )
    with pytest.raises(AuthError):
        tm.get_token()


def test_token_ttl_skew_triggers_early_refresh():
    """If token is within skew window, it should be refreshed."""
    ft_calls = {"n": 0}

    def handler(request):
        ft_calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"tok_{ft_calls['n']}", "expires_in": 100})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tm = TokenManager(
        client=client,
        auth_url="https://x/auth",
        app_id="id",
        app_secret="sec",
        ttl_skew=200,  # > expires_in => first call won't cache
    )
    t1 = tm.get_token()
    # With skew 200 and expiry 100, expires_at is now+100, but check is now < expires_at - 200
    # = now < now - 100, which is false; so it refreshes again
    t2 = tm.get_token()
    assert t1 == "tok_1"
    assert t2 == "tok_2"
    assert ft_calls["n"] == 2
