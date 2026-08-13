"""Tests for trae_dashboard.collector."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trae_dashboard.collector import Collector
from trae_dashboard.config import Config, Account
from trae_dashboard.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alice")
    return s


@pytest.fixture
def config() -> Config:
    return Config(
        openapi_base="https://api.test",
        auth_endpoint="/auth",
        app_id="test_id",
        app_secret="test_secret",
        per_account_quota=120.0,
        accounts=[Account(email="a@x.com", display_name="Alice")],
    )


def _api_response(items):
    return {"code": 0, "message": "ok", "request_id": "r", "data": {"items": items}}


def test_collector_filters_non_trae_models(storage: Storage, config: Config):
    """Models with model_source != 'Trae' should be skipped."""
    client = MagicMock()
    client.get_model_usage.return_value = _api_response([
        {"email": "a@x.com", "model_usage": [
            {"model_name": "GLM-5.1", "model_type": "Chat", "model_source": "Trae",
             "usage": {"input_tokens": 10, "output_tokens": 20},
             "amount": {"total_amount": 5.0, "basic_amount": 4.0, "pay_go_amount": 1.0, "currency": "CNY"}},
            {"model_name": "External-Model", "model_type": "Chat", "model_source": "ThirdParty",
             "usage": {"input_tokens": 999, "output_tokens": 999},
             "amount": {"total_amount": 999.0}},
        ]}
    ])

    collector = Collector(client=client, storage=storage, config=config)
    result = collector.run_once()
    assert result["snapshots"] == 1
    assert result["users"] == 1

    rows = storage.get_model_usage_for_account("a@x.com", result["cycle_start"])
    assert len(rows) == 1
    assert rows[0].model_name == "GLM-5.1"
    assert rows[0].amount_total == pytest.approx(5.0)
    assert rows[0].amount_basic == pytest.approx(4.0)
    assert rows[0].amount_pay_go == pytest.approx(1.0)
    assert rows[0].currency == "CNY"


def test_collector_handles_missing_amount_field(storage: Storage, config: Config):
    """API response missing 'amount' should default to 0.0, not crash."""
    client = MagicMock()
    client.get_model_usage.return_value = _api_response([
        {"email": "a@x.com", "model_usage": [
            {"model_name": "GLM-5.1", "model_type": "Chat", "model_source": "Trae",
             "usage": {"input_tokens": 10, "output_tokens": 20}},
        ]}
    ])

    collector = Collector(client=client, storage=storage, config=config)
    collector.run_once()

    from trae_dashboard.cycle import current_cycle_window
    s_dt, _ = current_cycle_window()
    rows = storage.get_model_usage_for_account("a@x.com", s_dt.date().isoformat())
    assert len(rows) == 1
    assert rows[0].amount_total == 0.0
    assert rows[0].currency == "CNY"


def test_collector_handles_multiple_accounts(storage: Storage, config: Config):
    storage.upsert_account("b@x.com", "Bob")
    client = MagicMock()
    client.get_model_usage.return_value = _api_response([
        {"email": "a@x.com", "model_usage": [
            {"model_name": "M1", "model_source": "Trae",
             "usage": {"input_tokens": 1, "output_tokens": 2},
             "amount": {"total_amount": 10.0}},
        ]},
        {"email": "b@x.com", "model_usage": [
            {"model_name": "M2", "model_source": "Trae",
             "usage": {"input_tokens": 3, "output_tokens": 4},
             "amount": {"total_amount": 20.0}},
        ]},
    ])

    collector = Collector(client=client, storage=storage, config=config)
    collector.run_once()

    from trae_dashboard.cycle import current_cycle_window
    s_dt, _ = current_cycle_window()
    a_rows = storage.get_model_usage_for_account("a@x.com", s_dt.date().isoformat())
    b_rows = storage.get_model_usage_for_account("b@x.com", s_dt.date().isoformat())
    assert len(a_rows) == 1 and a_rows[0].amount_total == 10.0
    assert len(b_rows) == 1 and b_rows[0].amount_total == 20.0
