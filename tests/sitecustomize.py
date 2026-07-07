"""Test-only sitecustomize for e2e CLI subprocesses.

Install via PYTHONPATH=tests so that subprocesses running `python -m
trae_dashboard ...` load this module on startup. When the env var
TRAE_E2E_TRANSPORT=1 is set, the TraeClient class is monkey-patched so
its httpx transport returns a canned /user-model-usage response — no
network access required, and no production code is modified.

Activated only when both conditions hold:
  - env var TRAE_E2E_TRANSPORT=1
  - the trae_dashboard package is importable

This file is auto-loaded by Python only if it's on sys.path. Tests set
PYTHONPATH=tests to make that happen.
"""
from __future__ import annotations
import os
import sys

if os.environ.get("TRAE_E2E_TRANSPORT") != "1":
    sys.exit(0)

try:
    import httpx  # noqa: F401
    from trae_dashboard.client import TraeClient
except Exception:
    sys.exit(0)


_CANNED_ITEMS = [
    {
        "email": "user01@company.com",
        "model_usage": [
            {
                "model_name": "GLM-5.1",
                "model_type": "Chat",
                "model_source": "Trae",
                "usage": {"input_tokens": 100, "output_tokens": 200},
            },
            {
                "model_name": "DeepSeek-V4-Pro",
                "model_type": "Chat",
                "model_source": "Trae",
                "usage": {"input_tokens": 50, "output_tokens": 80},
            },
        ],
    },
    {
        "email": "user02@company.com",
        "model_usage": [
            {
                "model_name": "GLM-5.1",
                "model_type": "Chat",
                "model_source": "Trae",
                "usage": {"input_tokens": 30, "output_tokens": 40},
            },
        ],
    },
]


def _handler(request):
    if "/auth" in str(request.url):
        return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    return httpx.Response(
        200,
        json={
            "code": 0,
            "message": "ok",
            "request_id": "e2e-mock",
            "data": {"items": list(_CANNED_ITEMS)},
        },
    )


_orig_init = TraeClient.__init__


def _patched_init(self, **kwargs):
    _orig_init(self, **kwargs)
    # Replace BOTH the top-level client and the token manager's client with
    # one that uses the canned mock transport. The original __init__ stored
    # self._client into TokenManager, so the auth call would otherwise hit
    # the real network.
    mock = httpx.Client(transport=httpx.MockTransport(_handler))
    self._client = mock
    if getattr(self, "_tokens", None) is not None:
        self._tokens._client = mock


TraeClient.__init__ = _patched_init  # type: ignore[assignment]
