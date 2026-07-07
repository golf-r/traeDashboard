"""Tests for trae_dashboard.config."""
from __future__ import annotations
import pytest

from trae_dashboard.config import Account, Config, load_config


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
    assert cfg.accounts[0].display_name == "A"
    assert cfg.accounts[1].display_name is None


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
    assert cfg.per_account_quota == 50_000_000


def test_load_config_missing_file_raises(tmp_data_dir):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_data_dir / "nope.yaml")


def test_load_config_with_quota(tmp_data_dir, monkeypatch):
    """Custom per_account_quota is loaded; default is 50_000_000."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
app_id_env: TRAE_APP_ID
app_secret_env: TRAE_APP_SECRET
per_account_quota: 12345678
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert cfg.per_account_quota == 12345678


def test_load_config_quota_default(tmp_data_dir, monkeypatch):
    """When per_account_quota is missing, default to 50_000_000."""
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
    assert cfg.per_account_quota == 50_000_000


def test_load_config_monthly_quota_backward_compat(tmp_data_dir, monkeypatch):
    """Backward compat: deprecated monthly_quota key is still honored."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
monthly_quota: 22222222
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert cfg.per_account_quota == 22_222_222


# ---------------------------------------------------------------------------
# included_model_names allowlist
# ---------------------------------------------------------------------------


def test_load_config_default_included_model_names(tmp_data_dir, monkeypatch):
    """When `included_model_names` is missing, the built-in default is used.

    Default contains the official model names from the design doc and does
    NOT contain deprecated "CUE".
    """
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")

    cfg = load_config(cfg_file)

    assert "GLM-5.1" in cfg.included_model_names
    assert "DeepSeek-V3.2" in cfg.included_model_names
    assert "CUE" not in cfg.included_model_names


def test_load_config_custom_included_model_names_strict_values(tmp_data_dir, monkeypatch):
    """Custom allowlist is loaded as a strict-match set (no case folding, dedup)."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
included_model_names:
  - glm-5.1
  - DeepSeek-V4-Pro
  - glm-5.1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")

    cfg = load_config(cfg_file)

    assert cfg.included_model_names == {"glm-5.1", "DeepSeek-V4-Pro"}
    assert "GLM-5.1" not in cfg.included_model_names


@pytest.mark.parametrize(
    "yaml_value",
    [
        "included_model_names: CUE",
        "included_model_names:\n  - ''",
        "included_model_names:\n  - 123",
    ],
)
def test_load_config_invalid_included_model_names_raises(
    tmp_data_dir, monkeypatch, yaml_value
):
    """Non-list values, non-string items, and empty strings all raise."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        f"""
openapi_base: x
auth_endpoint: /auth
{yaml_value}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")

    with pytest.raises(RuntimeError, match="included_model_names"):
        load_config(cfg_file)


def test_load_config_model_aliases_basic(tmp_data_dir, monkeypatch):
    """Aliases are loaded as canonical_name -> list[str]."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
included_model_names:
  - Doubao-Seed-Code
  - GLM-5.1
model_aliases:
  Doubao-Seed-Code:
    - Doubao_1_6
    - doubao_legacy
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert cfg.model_aliases == {
        "Doubao-Seed-Code": ["Doubao_1_6", "doubao_legacy"]
    }


def test_load_config_model_aliases_canonical_must_be_in_allowlist(
    tmp_data_dir, monkeypatch
):
    """Aliases can only point to names already in included_model_names."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
included_model_names:
  - GLM-5.1
model_aliases:
  NotInAllowlist:
    - Doubao_1_6
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    with pytest.raises(RuntimeError, match="model_aliases"):
        load_config(cfg_file)


def test_load_config_model_aliases_missing_defaults_to_empty(
    tmp_data_dir, monkeypatch
):
    """When `model_aliases` is missing, default to empty dict."""
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        """
openapi_base: x
auth_endpoint: /auth
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_APP_ID", "id")
    monkeypatch.setenv("TRAE_APP_SECRET", "sec")
    cfg = load_config(cfg_file)
    assert cfg.model_aliases == {}
