"""FastAPI application factory for trae-dashboard.

Routes:
  GET    /api/health                  - liveness check
  GET    /api/status                  - cycle window + company-level consumption
  GET    /api/accounts                - per-account cycle totals + per-model breakdown
  POST   /api/accounts                - add a managed account (email + display_name)
  DELETE /api/accounts/{email}        - remove a managed account (cascades model_usage)
  GET    /api/accounts/{email}/history - per-model breakdown for one account
  POST   /api/refresh                 - trigger a synchronous fetch (no scheduler)
  GET    /favicon.ico                 - tiny inline 1x1 PNG (no 404 noise)

Static files (index.html + app.js + style.css) are mounted AFTER the API
routes so /api/* paths win the route match.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .client import (
    TraeAPIError,
    TraeAuthError,
    TraeHTTPError,
    TraeRetryExhaustedError,
)
from .config import Config
from .cycle import current_cycle_window, next_cycle_reset
from .scheduler import make_collector
from .storage import Storage


# Email validation lives in trae_dashboard.validation so the API layer
# and the config writer share one regex/normalizer. Otherwise the writer
# could reject an address the API accepted (or vice versa) and produce
# surprising 400/422 errors after a successful save.
from .validation import VALID_EMAIL as _EMAIL_RE, is_valid_email, normalize_email  # noqa: E402,F401


# 1x1 transparent PNG, base64-decoded. Returned for /favicon.ico so the
# browser stops logging 404s (and so StaticFiles doesn't have to chase a
# missing path through the catch-all mount).
_FAVICON_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def _parse_iso8601(ts: str) -> datetime | None:
    """Parse an ISO8601-ish timestamp string. Returns None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


class AccountIn(BaseModel):
    """Request body for POST /api/accounts.

    `email` is required; `display_name` is optional and falls back to the
    local-part of the email (e.g. "alice@x.com" -> "alice") if omitted.
    """
    email: str = Field(..., min_length=3, max_length=255)
    display_name: str | None = Field(default=None, max_length=64)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v.lower()

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ReportIn(BaseModel):
    """Request body for POST /api/report.

    All fields optional:
      - preview: if true, render but DO NOT send the email.
      - recipients: override the configured recipient list for THIS
        send only (does not write back to config.yaml).
    """

    preview: bool = False
    recipients: list[str] | None = None


def create_app(*, cfg: Config, storage: Storage) -> FastAPI:
    app = FastAPI(title="Trae Token Dashboard")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(content=_FAVICON_PNG, media_type="image/png")

    @app.get("/api/status")
    def status():
        """Dashboard health + cycle + company-level token usage.

        Shape:
          {
            "ok": true,
            "last_fetched_at": "2026-06-29T21:50:20" | null,
            "seconds_since_fetch": 123 | null,
            "cycle_start": "2026-06-10T00:00:00+00:00",
            "cycle_end":   "2026-06-29T...+00:00",
            "per_account_quota": 50_000_000,
            "total_quota":       700_000_000,    # per_account_quota * accounts_with_data
            "total_consumed":    369_225_307,    # sum(input+output) in cycle
            "total_remaining":   330_774_693,    # max(0, total_quota - total_consumed)
            "utilization_pct":   52.75,          # total_consumed / total_quota * 100
            "total_accounts":    14,
            "accounts_with_data":11,
            "db_path": "data\\dashboard.db"
          }
        """
        start_dt, _ = current_cycle_window()
        cycle_end_dt = next_cycle_reset()
        start_date = start_dt.date().isoformat()
        end_date = cycle_end_dt.date().isoformat()
        rows = storage.get_model_usage_by_account(
            start_date, end_date, cfg.included_model_names
        )
        total_consumed = sum(
            (r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in rows
        )
        accounts_with_data = sum(
            1 for r in rows
            if (r["input_tokens"] or 0) > 0 or (r["output_tokens"] or 0) > 0
        )
        per_account_quota = cfg.per_account_quota
        # Quota is bought per account, not per account-with-data. Even an
        # account that hasn't consumed anything this cycle still occupies
        # a quota slot. So total_quota = per_account_quota × all_accounts.
        # (Previously this used max(accounts_with_data, len(rows)) which
        #  always collapsed to len(rows) — a dead max — AND excluded
        #  zero-consumption accounts from the denominator, which inflated
        #  utilization_pct. The new formula matches the billing reality.)
        total_accounts_for_quota = len(rows)
        total_quota = per_account_quota * total_accounts_for_quota
        total_remaining = max(0, total_quota - total_consumed)
        utilization_pct = (
            round((total_consumed / total_quota) * 100, 2) if total_quota > 0 else 0.0
        )

        latest = storage.latest_snapshot()
        last_fetched_at = latest["fetched_at"] if latest else None
        seconds_since_fetch: int | None = None
        if last_fetched_at:
            parsed = _parse_iso8601(last_fetched_at)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - parsed
                seconds_since_fetch = max(0, int(delta.total_seconds()))

        return {
            "ok": True,
            "last_fetched_at": last_fetched_at,
            "seconds_since_fetch": seconds_since_fetch,
            "cycle_start": start_dt.isoformat(),
            "cycle_end": cycle_end_dt.isoformat(),
            "nextResetAt": cycle_end_dt.isoformat(),
            "per_account_quota": per_account_quota,
            "total_quota": total_quota,
            "total_consumed": total_consumed,
            "total_remaining": total_remaining,
            "utilization_pct": utilization_pct,
            "total_accounts": len(rows),
            "accounts_with_data": accounts_with_data,
        }

    @app.get("/api/accounts")
    def list_accounts():
        """Per-account cycle totals + per-account quota usage.

        Each entry includes a `models` list with the per-model input/output
        tokens for that account in the current cycle. The frontend uses it
        to render a hover tooltip on the consumed cell.
        """
        start_dt, end_dt = current_cycle_window()
        start_date = start_dt.date().isoformat()
        end_date = end_dt.date().isoformat()
        rows = storage.get_model_usage_by_account(
            start_date, end_date, cfg.included_model_names
        )
        per_q = cfg.per_account_quota
        result = []
        for r in rows:
            consumed = (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
            quota_pct = round((consumed / per_q) * 100, 2) if per_q > 0 else 0.0
            # Per-model breakdown for the tooltip
            models_rows = storage.get_model_usage_for_account(
                r["email"], start_date, cfg.included_model_names
            )
            models = [
                {
                    "name": m.model_name,
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                    "consumed": m.input_tokens + m.output_tokens,
                }
                for m in models_rows
            ]
            result.append(
                {
                    "email": r["email"],
                    "display_name": r["display_name"],
                    "consumed": consumed,
                    "input_tokens": r["input_tokens"] or 0,
                    "output_tokens": r["output_tokens"] or 0,
                    "model_count": r["model_count"] or 0,
                    "per_account_quota": per_q,
                    "quota_used_pct": quota_pct,
                    "models": models,
                }
            )
        return result

    @app.post("/api/accounts", status_code=201)
    def add_account(body: AccountIn):
        """Add a new managed account.

        The collector's run_once reads from `storage.list_accounts()` —
        not from cfg.accounts — so newly added accounts are picked up
        on the very next /api/refresh call, no restart required.

        Returns 409 if the email is already tracked.
        """
        email = body.email
        # Pre-check for clearer 409 vs. raw UNIQUE constraint error.
        existing = storage.conn.execute(
            "SELECT 1 FROM accounts WHERE email = ?", (email,)
        ).fetchone()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"账号已存在: {email}",
            )
        display_name = body.display_name or email.split("@", 1)[0]
        storage.upsert_account(email, display_name)
        return {
            "ok": True,
            "email": email,
            "display_name": display_name,
        }

    @app.delete("/api/accounts/{email}")
    def delete_account(email: str):
        """Remove a managed account and cascade its model_usage rows.

        Idempotent: returns ok=True even if the email was not tracked
        (the user might have double-clicked the delete button, or the
        account was already removed by `prune`). Snapshots are NOT
        deleted — they are the audit trail.

        The next /api/refresh call will skip this email because the
        collector reads from `storage.list_accounts()`.
        """
        email = email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise HTTPException(status_code=422, detail="邮箱格式不正确")
        deleted = storage.delete_account(email)
        return {
            "ok": True,
            "email": email,
            "deleted_rows": deleted,
        }

    @app.post("/api/refresh")
    def refresh():
        """Run a synchronous fetch cycle.

        This is the only path that hits the Trae API in server mode. The
        refresh button in the UI calls this and then reloads the data.
        Returns the collector summary so the UI can show feedback.
        """
        collector = make_collector(cfg, storage)
        try:
            result = collector.run_once()
        except TraeAuthError as e:
            # Bad credentials / expired app_secret — caller's fault, not upstream.
            raise HTTPException(status_code=401, detail=f"auth failed: {e}") from e
        except TraeHTTPError as e:
            # Non-retryable 4xx (e.g. 400 bad request) — caller's fault.
            raise HTTPException(status_code=400, detail=str(e)) from e
        except TraeRetryExhaustedError as e:
            # Upstream kept failing — 502 is the right bucket.
            raise HTTPException(status_code=502, detail=str(e)) from e
        except TraeAPIError as e:
            # Catch-all for any future TraeAPIError subclass.
            raise HTTPException(status_code=502, detail=str(e)) from e
        except Exception as e:
            # Unknown errors — surface as 500 so they show up in logs.
            raise HTTPException(
                status_code=500, detail=f"refresh failed: {e}"
            ) from e
        finally:
            # Always close the underlying httpx.Client — make_collector()
            # creates a fresh one per request, so leaking it would
            # accumulate open sockets / file descriptors over time.
            try:
                collector._client.close()
            except Exception:
                pass
        return {
            "ok": True,
            "snapshots": result.get("snapshots", 0),
            "users": result.get("users", 0),
            "snapshot_id": result.get("snapshot_id", 0),
            "cycle_start": result.get("cycle_start"),
            "cycle_end": result.get("cycle_end"),
        }

    @app.get("/api/accounts/{email}/history")
    def account_history(email: str):
        """Per-model breakdown for one account in the current cycle."""
        start_dt, _ = current_cycle_window()
        cycle_start = start_dt.date().isoformat()
        rows = storage.get_model_usage_for_account(
            email, cycle_start, cfg.included_model_names
        )
        return [
            {
                "cycle_start": r.cycle_start,
                "cycle_end": r.cycle_end,
                "model_name": r.model_name,
                "model_type": r.model_type,
                "model_source": r.model_source,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
            }
            for r in rows
        ]

    @app.post("/api/report")
    def send_report(body: ReportIn | None = None):
        """Trigger or preview the daily email report.

        Body (all optional):
          - preview: bool      — if true, render but DO NOT send.
          - recipients: list   — override recipients for THIS send only
                                  (does not write back to config.yaml).

        Returns the summary dict on success, or 400/500 with detail.
        """
        from .report import run_report

        payload = body or ReportIn()
        preview = bool(payload.preview)
        recipients_override = payload.recipients
        # Strip + lowercase + validate each override recipient.
        if recipients_override is not None:
            cleaned = []
            for r in recipients_override:
                addr = (r or "").strip().lower()
                if not _EMAIL_RE.match(addr):
                    raise HTTPException(
                        status_code=400,
                        detail=f"invalid recipient email: {r!r}",
                    )
                cleaned.append(addr)
            recipients_override = cleaned

        try:
            summary = run_report(
                storage,
                cfg,
                recipients_override=recipients_override,
                send=not preview,
            )
        except RuntimeError as e:
            # Config issues (email disabled, missing SMTP password, etc.)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            # SMTP errors, network errors, etc.
            raise HTTPException(
                status_code=502, detail=f"report failed: {e}"
            ) from e
        return {"ok": True, **summary}

    @app.get("/api/report/config")
    def get_report_config():
        """Return the email-report configuration (read-only, no secrets).

        Used by the frontend "发送报告" dialog to list default recipients
        and show whether SMTP is enabled. Never exposes the password.
        """
        return {
            "enabled": cfg.email.enabled,
            "smtp_host": cfg.email.smtp_host,
            "smtp_port": cfg.email.smtp_port,
            "smtp_user": cfg.email.smtp_user,
            "from_addr": cfg.email.from_addr,
            "recipients": list(cfg.email.recipients),
            "send_time": cfg.email.send_time,
        }

    # Static files (mounted last so /api/* is not shadowed)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
