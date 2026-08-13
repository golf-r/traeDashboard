"""Tests for trae_dashboard.config."""
from __future__ import annotations
import pytest

from trae_dashboard.config import Account, Config, EmailConfig, load_config


def test_load_minimal_config(tmp_data_dir, monkeypatch):
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: https://api.example.com
auth_endpoint: /auth/token
app_id_env: TRAE_APP_ID
app_secret_env: TRAE_APP_SECRET
accounts:
  - email: a@x.com
    display_name: A
  - email: b@x.com
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")
    cfg = load_config(cfg_file)
    assert isinstance(cfg, Config)
    assert cfg.openapi_base == "https://api.example.com"
    assert cfg.app_id == "test_id"
    assert cfg.app_secret == "test_secret"
    assert len(cfg.accounts) == 2
    assert isinstance(cfg.accounts[0], Account)
    assert cfg.accounts[0].email == "a@x.com"


def test_load_config_missing_app_creds_raises(tmp_data_dir, monkeypatch):
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    monkeypatch.delenv("TRAE_APP_ID", raising=False)
    monkeypatch.delenv("TRAE_APP_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="TRAE_APP_ID"):
        load_config(cfg_file)


def test_load_config_defaults(tmp_data_dir, monkeypatch):
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
app_id_env: TRAE_APP_ID
app_secret_env: TRAE_APP_SECRET
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert cfg.db_path == "data/dashboard.db"
    assert cfg.fetch_interval_minutes == 60
    # NEW: quota is now 120.0 CNY (float), not 50M tokens (int)
    assert cfg.per_account_quota == 120.0
    assert isinstance(cfg.per_account_quota, float)


def test_load_config_missing_file_raises(tmp_data_dir):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_data_dir / "nope.yaml")


def test_load_config_with_quota_override(tmp_data_dir, monkeypatch):
    """Custom per_account_quota (float, in CNY) is loaded."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
app_id_env: TRAE_APP_ID
app_secret_env: TRAE_APP_SECRET
per_account_quota: 200.5
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert cfg.per_account_quota == 200.5


def test_config_no_longer_has_whitelist_fields():
    """Config dataclass must not expose included_model_names/model_aliases/display_weights."""
    cfg = Config(
        openapi_base="x", auth_endpoint="/a",
        app_id="id", app_secret="sec",
    )
    assert not hasattr(cfg, "included_model_names")
    assert not hasattr(cfg, "model_aliases")
    assert not hasattr(cfg, "display_weights")


def test_load_config_ignores_legacy_whitelist_keys(tmp_data_dir, monkeypatch):
    """Old configs that still contain included_model_names/display_weights/model_aliases
    should load without error (the keys are simply ignored, not stored)."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
included_model_names:
  - GLM-5.1
display_weights:
  GLM-5.1: 1.0
model_aliases:
  GLM-5.1: glm
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert not hasattr(cfg, "included_model_names")
    assert not hasattr(cfg, "model_aliases")
    assert not hasattr(cfg, "display_weights")


def test_email_section_loaded(tmp_data_dir, monkeypatch):
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
email:
  enabled: true
  smtp_host: smtp.qq.com
  smtp_port: 465
  smtp_user: x@y.com
  from_addr: x@y.com
  recipients:
    - r@z.com
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert isinstance(cfg.email, EmailConfig)
    assert cfg.email.enabled is True
    assert cfg.email.smtp_host == "smtp.qq.com"
    assert cfg.email.recipients == ["r@z.com"]


def test_load_config_rejects_user_metrics_endpoint(tmp_data_dir, monkeypatch):
    """user-metrics endpoint is deprecated; only /user-model-usage is allowed."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: https://api.trae.cn/user-metrics
auth_endpoint: /auth
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    with pytest.raises(ValueError, match="user-metrics"):
        load_config(cfg_file)
