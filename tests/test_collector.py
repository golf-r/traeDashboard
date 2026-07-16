"""Tests for trae_dashboard.collector."""

from __future__ import annotations
import json
import httpx

from trae_dashboard.collector import Collector
from trae_dashboard.config import Config, Account


def _client_with_mock(items):
    """Build an httpx.MockTransport that returns given items."""

    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "ok",
                "request_id": "r",
                "data": {"items": items},
            },
        )

    return httpx.MockTransport(handler)


def test_collector_writes_snapshot_and_model_usage(tmp_data_dir):
    """Collector writes one row per (email, model) with the cycle total — no per-day split."""
    items = [
        {
            "email": "a@x.com",
            "model_usage": [
                {
                    "model_name": "M1",
                    "model_type": "Chat",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 700, "output_tokens": 1400},
                }
            ],
        }
    ]
    from trae_dashboard.client import TraeClient
    from trae_dashboard.storage import Storage

    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="i",
        app_secret="s",
        transport=_client_with_mock(items),
    )
    db = tmp_data_dir / "test.db"
    storage = Storage(db)
    storage.init()
    storage.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com", "A")],
        included_model_names={"M1"},
    )
    c = Collector(
        client=client,
        storage=storage,
        config=cfg,
    )
    summary = c.run_once()
    assert summary["snapshots"] == 1
    cycle_start = summary["cycle_start"]
    rows = storage.get_model_usage_for_account(
        "a@x.com", cycle_start, cfg.included_model_names
    )
    # Exactly one model row with EXACT totals (no per-day distribution).
    assert len(rows) == 1
    assert rows[0].model_name == "M1"
    assert rows[0].input_tokens == 700
    assert rows[0].output_tokens == 1400
    client.close()


def test_collector_only_persists_included_model_names(tmp_data_dir):
    """Collector persists allowlisted models and skips all non-allowlisted models."""
    items = [
        {
            "email": "a@x.com",
            "model_usage": [
                {
                    "model_name": "CUE",
                    "model_type": "CUE",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 1000, "output_tokens": 2000},
                },
                {
                    "model_name": "AllowedModel",
                    "model_type": "Chat",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
                {
                    "model_name": "OtherModel",
                    "model_type": "Chat",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 30, "output_tokens": 40},
                },
            ],
        }
    ]
    from trae_dashboard.client import TraeClient
    from trae_dashboard.storage import Storage

    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="i",
        app_secret="s",
        transport=_client_with_mock(items),
    )
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    storage.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com", "A")],
        included_model_names={"AllowedModel"},
    )
    collector = Collector(client=client, storage=storage, config=cfg)

    summary = collector.run_once()
    rows = storage.get_model_usage_for_account(
        "a@x.com", summary["cycle_start"], cfg.included_model_names
    )

    assert [row.model_name for row in rows] == ["AllowedModel"]
    assert rows[0].input_tokens == 10
    assert rows[0].output_tokens == 20
    client.close()


def test_collector_skips_cue_model_usage(tmp_data_dir):
    """Backward-compat coverage: CUE rows are still skipped under the allowlist.

    Now expressed via `included_model_names` (CUE is not in the official list),
    but the assertion is the same as before: only the allowlisted model lands.
    """
    items = [
        {
            "email": "a@x.com",
            "model_usage": [
                {
                    "model_name": "CUE",
                    "model_type": "CUE",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 1000, "output_tokens": 2000},
                },
                {
                    "model_name": "ChatModel",
                    "model_type": "Chat",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            ],
        }
    ]
    from trae_dashboard.client import TraeClient
    from trae_dashboard.storage import Storage

    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="i",
        app_secret="s",
        transport=_client_with_mock(items),
    )
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    storage.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com", "A")],
        included_model_names={"ChatModel"},
    )
    collector = Collector(client=client, storage=storage, config=cfg)

    summary = collector.run_once()
    rows = storage.get_model_usage_for_account(
        "a@x.com", summary["cycle_start"], cfg.included_model_names
    )

    assert [row.model_name for row in rows] == ["ChatModel"]
    assert rows[0].input_tokens == 10
    assert rows[0].output_tokens == 20
    client.close()


def test_collector_summarizes_users(tmp_data_dir):
    items = [
        {"email": "a@x.com", "model_usage": []},
        {"email": "b@x.com", "model_usage": []},
    ]
    from trae_dashboard.client import TraeClient
    from trae_dashboard.storage import Storage

    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="i",
        app_secret="s",
        transport=_client_with_mock(items),
    )
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    storage.upsert_account("a@x.com")
    storage.upsert_account("b@x.com")
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com"), Account("b@x.com")],
    )
    c = Collector(
        client=client,
        storage=storage,
        config=cfg,
    )
    summary = c.run_once()
    assert summary["snapshots"] == 1
    assert summary["users"] == 2
    client.close()


def test_collector_handles_no_accounts(tmp_data_dir):
    from trae_dashboard.client import TraeClient
    from trae_dashboard.storage import Storage

    client = TraeClient(
        base_url="https://api",
        auth_url="/auth",
        app_id="i",
        app_secret="s",
        transport=_client_with_mock([]),
    )
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[],
    )
    c = Collector(
        client=client,
        storage=storage,
        config=cfg,
    )
    summary = c.run_once()
    assert summary["snapshots"] == 0
    assert summary["users"] == 0
    client.close()


# ---------------------------------------------------------------------------
# Cycle-mode (T4): collector uses cycle_window instead of lookback_days
# ---------------------------------------------------------------------------


def _capturing_client(items):
    """Build a client that records (start, end) of every model-usage call."""
    import httpx
    from trae_dashboard.client import TraeClient

    captured: list[tuple[int, int]] = []

    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        # body: { start_time, end_time, emails }
        try:
            body = json.loads(request.content.decode("utf-8"))
            captured.append((body.get("start_time"), body.get("end_time")))
        except Exception:
            pass
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "ok",
                "request_id": "r",
                "data": {"items": items},
            },
        )

    return (
        TraeClient(
            base_url="https://api",
            auth_url="/auth",
            app_id="i",
            app_secret="s",
            transport=httpx.MockTransport(handler),
        ),
        captured,
    )


def test_collector_uses_cycle_window(tmp_data_dir, monkeypatch):
    """Collector calls API with start/end matching the current cycle window."""
    from datetime import datetime, timezone
    from trae_dashboard.storage import Storage
    from trae_dashboard.cycle import current_cycle_window

    items = [
        {
            "email": "a@x.com",
            "model_usage": [
                {
                    "model_name": "M1",
                    "model_type": "Chat",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 100, "output_tokens": 200},
                }
            ],
        }
    ]
    client, captured = _capturing_client(items)
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    storage.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com", "A")],
        included_model_names={"M1"},
    )
    # Pin `now` to a known date so the cycle window is deterministic.
    # mid-month: 2026-06-29 → start = 2026-06-10
    c = Collector(
        client=client,
        storage=storage,
        config=cfg,
    )
    # Inject `now` via cycle_window
    fixed_now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
    s_dt, e_dt = current_cycle_window(fixed_now)
    expected_start = int(s_dt.timestamp())
    # Collector.run_once() always calls current_cycle_window() with no arg,
    # so monkeypatch the imported reference inside the collector module
    # to return our pinned window.
    import trae_dashboard.collector as collector_mod
    monkeypatch.setattr(
        collector_mod, "current_cycle_window",
        lambda *a, **kw: (s_dt, e_dt),
    )

    summary = c.run_once()
    assert summary["snapshots"] == 1
    # The captured (start, end) for the only API call must equal the cycle
    # window unix ints (allow a tiny slack for time-of-call drift).
    assert len(captured) == 1
    s_captured, e_captured = captured[0]
    # The captured "now" may drift a few seconds past fixed_now; only check
    # the *start* is exactly aligned to the 10th 00:00 UTC, and end >= start.
    assert s_captured == expected_start
    assert e_captured >= s_captured
    client.close()


def test_collector_saves_cycle_in_metadata(tmp_data_dir):
    """The snapshot's request_meta contains the cycle dates (start..end)."""
    from trae_dashboard.storage import Storage

    items = [
        {
            "email": "a@x.com",
            "model_usage": [
                {
                    "model_name": "M1",
                    "model_type": "Chat",
                    "model_source": "Trae",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
            ],
        }
    ]
    client, _ = _capturing_client(items)
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    storage.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com", "A")],
        included_model_names={"M1"},
    )
    c = Collector(
        client=client,
        storage=storage,
        config=cfg,
    )
    summary = c.run_once()
    # snapshot row in storage
    row = storage.conn.execute(
        "SELECT request_meta FROM snapshots WHERE id=?", (summary["snapshot_id"],)
    ).fetchone()
    assert row is not None
    assert "cycle" in (row["request_meta"] or "").lower()
    # The return summary also carries cycle_start/cycle_end
    assert "cycle_start" in summary
    assert "cycle_end" in summary
    client.close()
