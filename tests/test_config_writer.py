"""Tests for trae_dashboard.config_writer (config.yaml + .env write-back)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from trae_dashboard.config_writer import (
    HEADER_COMMENT,
    save_recipients,
    save_email_config,
    save_env_var,
)


CONFIG_WITH_EMAIL = """\
openapi_base: https://api.example.com
auth_endpoint: /auth/token
accounts:
  - email: a@x.com
# comment to preserve
email:
  enabled: true
  smtp_host: smtp.qq.com
  smtp_port: 465
  smtp_user: me@qq.com
  smtp_password_env: SMTP_PASSWORD
  from_addr: me@qq.com
  recipients:
    - old@x.com
  send_time: "09:00"
"""


def test_save_recipients_writes_new_list(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_WITH_EMAIL, encoding="utf-8")
    save_recipients(p, ["new1@x.com", "new2@x.com"])
    text = p.read_text(encoding="utf-8")
    # Header comment was prepended
    assert text.startswith(HEADER_COMMENT)
    # New recipients present
    assert "new1@x.com" in text
    assert "new2@x.com" in text
    # Old recipients gone
    assert "old@x.com" not in text


def test_save_recipients_preserves_other_email_fields(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_WITH_EMAIL, encoding="utf-8")
    save_recipients(p, ["new@x.com"])
    text = p.read_text(encoding="utf-8")
    # Other email.* fields untouched
    assert "smtp_host: smtp.qq.com" in text
    assert "smtp_port: 465" in text
    assert "from_addr: me@qq.com" in text
    assert 'send_time: "09:00"' in text
    assert "smtp_password_env: SMTP_PASSWORD" in text


def test_save_recipients_does_not_duplicate_header(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(HEADER_COMMENT + "\n" + CONFIG_WITH_EMAIL, encoding="utf-8")
    save_recipients(p, ["x@x.com"])
    text = p.read_text(encoding="utf-8")
    # Only one header
    assert text.count(HEADER_COMMENT) == 1


def test_save_recipients_creates_email_section_if_missing(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "openapi_base: x\nauth_endpoint: y\n", encoding="utf-8"
    )
    save_recipients(p, ["a@x.com"])
    text = p.read_text(encoding="utf-8")
    assert "recipients:" in text
    assert "a@x.com" in text
    assert "email:" in text


def test_save_email_config_writes_smtp_fields(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_WITH_EMAIL, encoding="utf-8")
    save_email_config(
        p,
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user="new@gmail.com",
        from_addr="new@gmail.com",
        send_time="08:30",
    )
    text = p.read_text(encoding="utf-8")
    assert "smtp_host: smtp.gmail.com" in text
    assert "smtp_port: 587" in text
    assert "smtp_user: new@gmail.com" in text
    assert "from_addr: new@gmail.com" in text
    assert 'send_time: "08:30"' in text


def test_save_email_config_preserves_recipients_and_password_env(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_WITH_EMAIL, encoding="utf-8")
    save_email_config(
        p,
        smtp_host="h", smtp_port=465, smtp_user="u@x.com",
        from_addr="u@x.com", send_time="09:00",
    )
    text = p.read_text(encoding="utf-8")
    assert "old@x.com" in text
    assert "smtp_password_env: SMTP_PASSWORD" in text
    assert "enabled: true" in text


def test_save_email_config_rejects_invalid_input(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_WITH_EMAIL, encoding="utf-8")
    with pytest.raises(ValueError):
        save_email_config(
            p, smtp_host="", smtp_port=465, smtp_user="u@x.com",
            from_addr="u@x.com", send_time="09:00",
        )
    with pytest.raises(ValueError):
        save_email_config(
            p, smtp_host="h", smtp_port=99999, smtp_user="u@x.com",
            from_addr="u@x.com", send_time="09:00",
        )
    with pytest.raises(ValueError):
        save_email_config(
            p, smtp_host="h", smtp_port=465, smtp_user="not-email",
            from_addr="u@x.com", send_time="09:00",
        )
    with pytest.raises(ValueError):
        save_email_config(
            p, smtp_host="h", smtp_port=465, smtp_user="u@x.com",
            from_addr="u@x.com", send_time="bad",
        )


def test_save_env_var_replaces_existing(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "TRAE_APP_ID=abc\nSMTP_PASSWORD=oldpw\n# trailing comment\n",
        encoding="utf-8",
    )
    save_env_var(p, "SMTP_PASSWORD", "newpw")
    text = p.read_text(encoding="utf-8")
    assert "SMTP_PASSWORD=newpw" in text
    assert "oldpw" not in text
    # Other vars preserved
    assert "TRAE_APP_ID=abc" in text
    # Comment lines preserved (best-effort: line beginning with # is kept)


def test_save_env_var_appends_when_missing(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("TRAE_APP_ID=abc\n", encoding="utf-8")
    save_env_var(p, "SMTP_PASSWORD", "newpw")
    text = p.read_text(encoding="utf-8")
    assert "TRAE_APP_ID=abc" in text
    assert "SMTP_PASSWORD=newpw" in text


def test_save_env_var_creates_file_if_missing(tmp_path: Path):
    p = tmp_path / ".env"
    save_env_var(p, "SMTP_PASSWORD", "newpw")
    assert p.exists()
    assert "SMTP_PASSWORD=newpw" in p.read_text(encoding="utf-8")


def test_save_env_var_quotes_value_with_special_chars(tmp_path: Path):
    p = tmp_path / ".env"
    save_env_var(p, "SMTP_PASSWORD", "abc def=ghi")
    text = p.read_text(encoding="utf-8")
    # Value contains a space and an '=' — must be quoted.
    assert '"abc def=ghi"' in text
    # Re-parse round-trip: env value matches exactly.
    from dotenv import dotenv_values
    parsed = dotenv_values(p)
    assert parsed["SMTP_PASSWORD"] == "abc def=ghi"
