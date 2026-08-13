"""Collector: pulls model usage from Trae API and persists to SQLite.

The Trae API returns totals for the requested cycle window without a
per-day breakdown. We store ONE row per (email, cycle_start, model_name)
in the ``model_usage`` table — no daily distribution, no rounding. The
UNIQUE constraint on (email, cycle_start, model_name) means re-fetching
the same cycle window overwrites the prior totals (no accumulation).

Only models with model_source == "Trae" are persisted (Trae built-in
models). Third-party models are skipped at the collector boundary so
storage / API / report layers never see them.
"""

from __future__ import annotations
import json
import logging

from .client import TraeClient
from .storage import Storage
from .config import Config
from .cycle import current_cycle_window

log = logging.getLogger(__name__)


class Collector:
    def __init__(
        self,
        *,
        client: TraeClient,
        storage: Storage,
        config: Config,
    ) -> None:
        self._client = client
        self._storage = storage
        self._config = config

    def run_once(self) -> dict:
        """Run one fetch + persist cycle.

        Returns: {snapshots, users, snapshot_id, cycle_start, cycle_end}.
        """
        emails = [a.email for a in self._storage.list_accounts()]
        start_dt, end_dt = current_cycle_window()
        start_unix = int(start_dt.timestamp())
        end_unix = int(end_dt.timestamp())
        start_date = start_dt.date().isoformat()
        end_date = end_dt.date().isoformat()

        if not emails:
            return {
                "snapshots": 0,
                "users": 0,
                "snapshot_id": 0,
                "cycle_start": start_date,
                "cycle_end": end_date,
            }

        result = self._client.get_model_usage(
            emails=emails, start=start_unix, end=end_unix
        )
        snap_id = self._storage.save_snapshot(
            start_time=start_unix,
            end_time=end_unix,
            payload_json=json.dumps(result, ensure_ascii=False),
            request_meta=f"cycle {start_date}..{end_date}",
        )

        items = result.get("data", {}).get("items", [])

        # One row per (email, model_name) — totals for the cycle window.
        # The UNIQUE(email, cycle_start, model_name) constraint handles
        # the case where a fetch is re-run: the latest numbers win.
        for item in items:
            email = item.get("email")
            if not email:
                continue
            for mu in item.get("model_usage", []):
                # Only persist Trae built-in models.
                if mu.get("model_source") != "Trae":
                    continue
                raw_name = mu.get("model_name") or "unknown"
                u = mu.get("usage", {}) or {}
                amt = mu.get("amount", {}) or {}
                try:
                    amount_total = float(amt.get("total_amount", 0) or 0)
                    amount_basic = float(amt.get("basic_amount", 0) or 0)
                    amount_pay_go = float(amt.get("pay_go_amount", 0) or 0)
                except (TypeError, ValueError) as e:
                    log.warning("Bad amount for %s/%s: %s", email, raw_name, e)
                    continue
                currency = amt.get("currency", "CNY") or "CNY"
                self._storage.upsert_model_usage(
                    email=email,
                    cycle_start=start_date,
                    cycle_end=end_date,
                    model_name=raw_name,
                    model_type=mu.get("model_type"),
                    model_source=mu.get("model_source"),
                    input_tokens=int(u.get("input_tokens", 0) or 0),
                    output_tokens=int(u.get("output_tokens", 0) or 0),
                    amount_total=amount_total,
                    amount_basic=amount_basic,
                    amount_pay_go=amount_pay_go,
                    currency=currency,
                )

        return {
            "snapshots": 1,
            "users": len(items),
            "snapshot_id": snap_id,
            "cycle_start": start_date,
            "cycle_end": end_date,
        }
