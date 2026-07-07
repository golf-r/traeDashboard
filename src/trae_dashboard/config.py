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


# Built-in default: official model allowlist. Configurable in config.yaml via
# the `included_model_names` key. Strict API-name matching (no case folding,
# no alias normalization).
DEFAULT_INCLUDED_MODEL_NAMES: frozenset[str] = frozenset(
    {
        "GLM-5.1",
        "GLM-5-Turbo",
        "DeepSeek-V4-Pro",
        "Doubao-Seed-Code",
        "GLM-5V-Turbo",
        "Doubao-Seed-2.1-Pro",
        "Doubao-Seed-2.1-Turbo",
        "Doubao-Seed-2.0-Code",
        "GLM-5.2",
        "GLM-5",
        "GLM-4.7",
        "MiniMax-M3",
        "MiniMax-M2.7",
        "Qwen3.7-Plus",
        "Qwen3-Coder-Next",
        "Kimi-K2.7-Code",
        "Kimi-K2.6",
        "Kimi-K2.5",
        "DeepSeek-V4-Flash",
        "DeepSeek-V3.2",
    }
)


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
    # Per-account monthly token quota. Company total = per_account_quota * accounts.
    per_account_quota: int = 50_000_000
    # Strict-match allowlist of official model names. Rows whose API
    # model_name is not in this set are not persisted (collector) and not
    # returned by storage reads.
    included_model_names: set[str] = field(
        default_factory=lambda: set(DEFAULT_INCLUDED_MODEL_NAMES)
    )
    # Map canonical model name -> list of API-side aliases that should be
    # stored under the canonical name. Used when the Trae UI displays a
    # name different from what the OpenAPI returns (e.g. the official UI
    # shows "Doubao-Seed-Code" while the API returns "Doubao_1_6").
    # Matching is case-insensitive.
    model_aliases: dict[str, list[str]] = field(default_factory=dict)
    # Display-time weights keyed by canonical model_name. Applied on read
    # in storage so DB rows keep the raw API values (audit trail intact).
    # Default matches the Trae admin UI's display for Doubao-Seed-Code
    # (which shows 0.5x of the API value for the same cycle window).
    display_weights: dict[str, float] = field(
        default_factory=lambda: {"Doubao-Seed-Code": 0.5}
    )
    # Optional daily email report (SMTP). Disabled by default.
    email: EmailConfig = field(default_factory=EmailConfig)


def _load_included_model_names(data: dict) -> set[str]:
    """Parse and validate `included_model_names` from raw YAML data.

    - Missing key → default to built-in official list.
    - Present but not a list → RuntimeError.
    - Item not a string, or empty/whitespace-only → RuntimeError.
    - Duplicates collapse into a set; surrounding whitespace is stripped.
    """
    raw = data.get("included_model_names")
    if raw is None:
        return set(DEFAULT_INCLUDED_MODEL_NAMES)
    if not isinstance(raw, list):
        raise RuntimeError(
            "included_model_names must be a YAML list of non-empty strings"
        )
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(
                "included_model_names must be a YAML list of non-empty strings"
            )
        names.add(item.strip())
    return names


def _load_model_aliases(data: dict, allowlist: set[str]) -> dict[str, list[str]]:
    """Parse `model_aliases` from raw YAML data.

    Schema: a dict mapping canonical name -> list of API-side names.
    Each canonical name must already be in the allowlist; each alias is
    validated to be a non-empty string. Aliases themselves are NOT added
    to the allowlist (they are only entry points for renaming).
    """
    raw = data.get("model_aliases")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError("model_aliases must be a YAML mapping")
    out: dict[str, list[str]] = {}
    for canonical, aliases in raw.items():
        if canonical not in allowlist:
            raise RuntimeError(
                f"model_aliases: canonical '{canonical}' is not in "
                f"included_model_names"
            )
        if not isinstance(aliases, list) or not all(
            isinstance(a, str) and a.strip() for a in aliases
        ):
            raise RuntimeError(
                f"model_aliases['{canonical}'] must be a list of non-empty strings"
            )
        out[canonical] = [a.strip() for a in aliases]
    return out


def _load_display_weights(data: dict) -> dict[str, float]:
    """Parse optional `display_weights` mapping from YAML.

    Schema: dict mapping canonical model_name -> float weight.
    Each key must already be in `included_model_names` (validated by the
    caller's allowlist, but here we only check shape). Weight must be
    a positive number; 1.0 means "no adjustment".
    """
    raw = data.get("display_weights")
    if raw is None:
        return {"Doubao-Seed-Code": 0.5}
    if not isinstance(raw, dict):
        raise RuntimeError("display_weights must be a YAML mapping of {model_name: weight}")
    out: dict[str, float] = {}
    for name, w in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("display_weights keys must be non-empty strings")
        if not isinstance(w, (int, float)) or w <= 0:
            raise RuntimeError(
                f"display_weights['{name}'] must be a positive number, got {w!r}"
            )
        out[name.strip()] = float(w)
    return out


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
    # Parse allowlist once and reuse for both fields — avoids duplicate
    # work AND keeps the two in sync if validation ever grows side effects.
    included = _load_included_model_names(data)
    return Config(
        openapi_base=data["openapi_base"],
        auth_endpoint=data["auth_endpoint"],
        app_id=app_id,
        app_secret=app_secret,
        accounts=accounts,
        db_path=data.get("db_path", "data/dashboard.db"),
        fetch_interval_minutes=int(data.get("fetch_interval_minutes", 60)),
        per_account_quota=int(
            data.get(
                "per_account_quota",
                data.get("monthly_quota", 50_000_000),  # backward-compat
            )
        ),
        included_model_names=included,
        model_aliases=_load_model_aliases(data, included),
        display_weights=_load_display_weights(data),
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
