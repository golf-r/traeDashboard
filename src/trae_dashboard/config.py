"""Configuration loader for trae-dashboard.

Reads config.yaml + environment variables (.env supported via python-dotenv).
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env once at import time; safe to call multiple times.
load_dotenv()


@dataclass
class Account:
    email: str
    display_name: str | None = None


@dataclass
class EmailConfig:
    """SMTP email-report configuration.

    `smtp_password_env` names the environment variable that holds the
    SMTP password (never put the password itself in config.yaml).
    `recipients` is the list of To: addresses. `send_time` is purely
    informational — actual scheduling is done by Windows Task Scheduler
    or cron, not by this process.
    """

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password_env: str = "SMTP_PASSWORD"
    from_addr: str = ""
    recipients: list[str] = field(default_factory=list)
    send_time: str = "09:00"


@dataclass
class Config:
    openapi_base: str
    auth_endpoint: str
    app_id: str
    app_secret: str
    accounts: list[Account] = field(default_factory=list)
    db_path: str = "data/dashboard.db"
    fetch_interval_minutes: int = 60
    # Per-account monthly quota in CNY (amount-based, not token-based).
    # Company total = per_account_quota * number of accounts.
    per_account_quota: float = 120.0
    # Optional daily email report (SMTP). Disabled by default.
    email: EmailConfig = field(default_factory=EmailConfig)


def load_config(path: str | Path) -> Config:
    """Load configuration from a YAML file + env vars.

    Required YAML keys: openapi_base, auth_endpoint.
    Required env vars (defaults to TRAE_APP_ID / TRAE_APP_SECRET):
      - <app_id_env>
      - <app_secret_env>

    Raises FileNotFoundError if the config file does not exist.
    Raises RuntimeError if credentials are missing.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "openapi_base" not in data or "auth_endpoint" not in data:
        raise RuntimeError(
            "config.yaml is missing required keys: openapi_base, auth_endpoint"
        )
    app_id_env = data.get("app_id_env", "TRAE_APP_ID")
    app_secret_env = data.get("app_secret_env", "TRAE_APP_SECRET")
    app_id = os.environ.get(app_id_env, "")
    app_secret = os.environ.get(app_secret_env, "")
    if not app_id or not app_secret:
        raise RuntimeError(
            f"Missing {app_id_env} or {app_secret_env} in environment. "
            f"Set them in .env or export them before running."
        )
    accounts = [
        Account(email=a["email"], display_name=a.get("display_name"))
        for a in data.get("accounts", [])
    ]
    # T3: whitelist enforcement — only /user-model-usage data endpoint is
    # allowed. Reject any config that still references the deprecated
    # user-metrics endpoint in any string field.
    for key, val in data.items():
        if isinstance(val, str) and "user-metrics" in val:
            raise ValueError(
                f"Only /user-model-usage data endpoint is allowed; "
                f"user-metrics is deprecated (found in config key '{key}')"
            )
    return Config(
        openapi_base=data["openapi_base"],
        auth_endpoint=data["auth_endpoint"],
        app_id=app_id,
        app_secret=app_secret,
        accounts=accounts,
        db_path=data.get("db_path", "data/dashboard.db"),
        fetch_interval_minutes=int(data.get("fetch_interval_minutes", 60)),
        per_account_quota=float(data.get("per_account_quota", 120.0)),
        email=_load_email_config(data),
    )


def _load_email_config(data: dict) -> EmailConfig:
    """Parse the optional `email` section from raw YAML data.

    Missing section → disabled EmailConfig. Present but not a mapping
    or missing required fields (smtp_host / smtp_user / from_addr /
    recipients) when enabled → RuntimeError.
    """
    raw = data.get("email")
    if raw is None:
        return EmailConfig()
    if not isinstance(raw, dict):
        raise RuntimeError("email config must be a YAML mapping")
    enabled = bool(raw.get("enabled", False))
    cfg = EmailConfig(
        enabled=enabled,
        smtp_host=str(raw.get("smtp_host", "")).strip(),
        smtp_port=int(raw.get("smtp_port", 465)),
        smtp_user=str(raw.get("smtp_user", "")).strip(),
        smtp_password_env=str(raw.get("smtp_password_env", "SMTP_PASSWORD")).strip(),
        from_addr=str(raw.get("from_addr", "")).strip(),
        recipients=[
            str(r).strip() for r in raw.get("recipients", []) if str(r).strip()
        ],
        send_time=str(raw.get("send_time", "09:00")).strip(),
    )
    if enabled:
        missing = [
            k
            for k, v in {
                "smtp_host": cfg.smtp_host,
                "smtp_user": cfg.smtp_user,
                "from_addr": cfg.from_addr,
            }.items()
            if not v
        ]
        if missing:
            raise RuntimeError(
                f"email config is enabled but missing required field(s): {missing}"
            )
        if not cfg.recipients:
            raise RuntimeError("email config is enabled but 'recipients' list is empty")
    return cfg
