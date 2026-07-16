# 邮件发送功能增强 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add UI-managed recipients, UI-managed SMTP configuration, and `.eml` download to the existing email report feature.

**Architecture:** New `config_writer.py` module handles `config.yaml` / `.env` write-back with atomic rename + prepended managed-by-dashboard header comment. Backend `report.py` extracts a shared message builder, adds `build_eml`. API gains `PUT /api/report/recipients`, `PUT /api/report/smtp`, `POST /api/report/smtp/password`, `POST /api/report/eml`, and `smtp_password_set` on `GET /api/report/config`. Frontend report modal becomes a two-tab UI (Send / Mail Settings) with a separate password entry flow. `create_app` accepts a new `config_path` parameter; `_serve` passes it through.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, PyYAML (no new dependencies), pytest. Frontend: vanilla HTML/CSS/JS (no framework).

**Reference design:** `docs/plans/2026-07-16-email-report-enhancement-design.md`

---

## Task 1: `config_writer.save_recipients` — write recipients to `config.yaml`

**Files:**
- Create: `src/trae_dashboard/config_writer.py`
- Create: `tests/test_config_writer.py`

**Step 1: Write the failing test**

Add to `tests/test_config_writer.py`:

```python
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
```

**Step 2: Run the tests, expect failures (no module)**

Run: `python -m pytest tests/test_config_writer.py -v`
Expected: `ModuleNotFoundError: No module named 'trae_dashboard.config_writer'`

**Step 3: Create `config_writer.py` skeleton with `HEADER_COMMENT` and `save_recipients`**

Create `src/trae_dashboard/config_writer.py`:

```python
"""Write-back helpers for config.yaml and .env.

Mutates only the specific sections we expose via the UI. Other
keys/formatting/comments outside the touched sections are preserved
by PyYAML's safe_dump (subject to the known comment-loss caveat).

All file writes use a temp-file + os.replace atomic rename to avoid
half-written files.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

import yaml

HEADER_COMMENT = "# 本文件的 email.* 由 Trae Dashboard 管理,手动编辑可能被覆盖"
_EMAIL_KEY = "email"


def _load_yaml(path: Path) -> dict:
    """Load YAML, raising on parse failure. Empty file → {}."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # Strip managed-by-dashboard header line(s) before parsing so
    # PyYAML doesn't choke on a comment.
    lines = [
        ln for ln in text.splitlines() if ln.strip() != HEADER_COMMENT
    ]
    return yaml.safe_load("\n".join(lines)) or {}


def _ensure_header(text: str) -> str:
    if text.lstrip().startswith(HEADER_COMMENT):
        return text
    return HEADER_COMMENT + "\n" + text


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically via temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _dump_yaml(data: dict) -> str:
    """Dump dict to YAML preserving key order, with allow_unicode."""
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def save_recipients(config_path: Path, recipients: Iterable[str]) -> None:
    """Write `recipients` to config.yaml's email section, preserving others.

    Validates each address; raises ValueError on invalid input.
    """
    from .api import _EMAIL_RE  # reuse validator; re-import-safe
    # Re-define the validator locally to avoid an import cycle in tests
    # that import config_writer without the api module.
    import re as _re
    email_re = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    cleaned: list[str] = []
    for r in recipients:
        addr = (r or "").strip().lower()
        if not addr:
            continue
        if not email_re.match(addr):
            raise ValueError(f"invalid recipient email: {r!r}")
        cleaned.append(addr)
    # De-dup while preserving order
    seen: set[str] = set()
    deduped = [a for a in cleaned if not (a in seen or seen.add(a))]

    data = _load_yaml(config_path)
    data.setdefault(_EMAIL_KEY, {})
    data[_EMAIL_KEY]["recipients"] = deduped
    _atomic_write(config_path, _ensure_header(_dump_yaml(data)))
```

**Step 4: Run tests, expect pass for the four tests above**

Run: `python -m pytest tests/test_config_writer.py::test_save_recipients_writes_new_list tests/test_config_writer.py::test_save_recipients_preserves_other_email_fields tests/test_config_writer.py::test_save_recipients_does_not_duplicate_header tests/test_config_writer.py::test_save_recipients_creates_email_section_if_missing -v`
Expected: 4 passed.

**Step 5: Commit**

```bash
git add src/trae_dashboard/config_writer.py tests/test_config_writer.py
git commit -m "feat(config_writer): save_recipients with atomic write + header"
```

---

## Task 2: `save_email_config` — write SMTP fields

**Files:**
- Modify: `src/trae_dashboard/config_writer.py`
- Modify: `tests/test_config_writer.py`

**Step 1: Write the failing test**

Add to `tests/test_config_writer.py`:

```python
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
```

**Step 2: Run, expect failures (function missing)**

Run: `python -m pytest tests/test_config_writer.py -k save_email_config -v`
Expected: `AttributeError: module 'trae_dashboard.config_writer' has no attribute 'save_email_config'`

**Step 3: Implement `save_email_config`**

Add to `src/trae_dashboard/config_writer.py`:

```python
import re as _re

_VALID_EMAIL = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_TIME_RE = _re.compile(r"^\d{2}:\d{2}$")


def save_email_config(
    config_path: Path,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    from_addr: str,
    send_time: str,
) -> None:
    """Update SMTP fields in config.yaml. Preserves recipients, smtp_password_env, enabled."""
    if not (smtp_host or "").strip():
        raise ValueError("smtp_host must not be empty")
    if not isinstance(smtp_port, int) or not (1 <= smtp_port <= 65535):
        raise ValueError(f"smtp_port must be 1..65535, got {smtp_port!r}")
    if not _VALID_EMAIL.match((smtp_user or "").strip()):
        raise ValueError(f"invalid smtp_user: {smtp_user!r}")
    if not _VALID_EMAIL.match((from_addr or "").strip()):
        raise ValueError(f"invalid from_addr: {from_addr!r}")
    if not _TIME_RE.match(send_time or ""):
        raise ValueError(f"send_time must match HH:MM, got {send_time!r}")

    data = _load_yaml(config_path)
    email = data.setdefault(_EMAIL_KEY, {})
    email["smtp_host"] = smtp_host.strip()
    email["smtp_port"] = int(smtp_port)
    email["smtp_user"] = smtp_user.strip()
    email["from_addr"] = from_addr.strip()
    email["send_time"] = send_time.strip()
    _atomic_write(config_path, _ensure_header(_dump_yaml(data)))
```

Also clean up Task 1's `_EMAIL_RE` import that was added but never used — remove those dead lines:

```python
def save_recipients(config_path: Path, recipients: Iterable[str]) -> None:
    ...
    import re as _re
    email_re = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    ...
```

(That local import was a guard against a hypothetical cycle and is now replaced by the module-level `_VALID_EMAIL` constant. Keep the local import for now if you want zero coupling — but since `_VALID_EMAIL` lives in the same module, you can simply use `_VALID_EMAIL` inside `save_recipients` and drop the local re-import.)

Final shape of `save_recipients`:

```python
def save_recipients(config_path: Path, recipients: Iterable[str]) -> None:
    cleaned: list[str] = []
    for r in recipients:
        addr = (r or "").strip().lower()
        if not addr:
            continue
        if not _VALID_EMAIL.match(addr):
            raise ValueError(f"invalid recipient email: {r!r}")
        cleaned.append(addr)
    seen: set[str] = set()
    deduped = [a for a in cleaned if not (a in seen or seen.add(a))]

    data = _load_yaml(config_path)
    data.setdefault(_EMAIL_KEY, {})
    data[_EMAIL_KEY]["recipients"] = deduped
    _atomic_write(config_path, _ensure_header(_dump_yaml(data)))
```

**Step 4: Run, expect pass**

Run: `python -m pytest tests/test_config_writer.py -v`
Expected: 7 passed (4 from Task 1 + 3 new).

**Step 5: Commit**

```bash
git add src/trae_dashboard/config_writer.py tests/test_config_writer.py
git commit -m "feat(config_writer): save_email_config with field validation"
```

---

## Task 3: `save_env_var` — update `.env` atomically

**Files:**
- Modify: `src/trae_dashboard/config_writer.py`
- Modify: `tests/test_config_writer.py`

**Step 1: Write the failing test**

Add to `tests/test_config_writer.py`:

```python
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
```

**Step 2: Run, expect failure**

Run: `python -m pytest tests/test_config_writer.py -k save_env_var -v`
Expected: `AttributeError: ... has no attribute 'save_env_var'`

**Step 3: Implement `save_env_var`**

Add to `src/trae_dashboard/config_writer.py`:

```python
def _needs_quoting(value: str) -> bool:
    if value == "":
        return True
    for ch in value:
        if ch.isspace() or ch in '"\\':
            return True
    # Values containing '=' without quoting are ambiguous to parsers.
    if "=" in value:
        return True
    return False


def _format_env_line(key: str, value: str) -> str:
    if _needs_quoting(value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"\n'
    return f"{key}={value}\n"


def save_env_var(env_path: Path, key: str, value: str) -> None:
    """Set or replace `key=value` in an .env file, atomic write, quote when needed.

    Preserves all other lines verbatim (including comments and unrelated
    entries). If `key` exists, only that line is replaced. If not, the
    line is appended at the end.
    """
    if not key or not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError(f"invalid env var name: {key!r}")

    if env_path.exists():
        original = env_path.read_text(encoding="utf-8")
    else:
        original = ""

    new_line = _format_env_line(key, value)
    pattern = _re.compile(rf"^{_re.escape(key)}=.*$", _re.MULTILINE)
    if pattern.search(original):
        updated = pattern.sub(new_line.rstrip("\n"), original)
        # If original didn't end with newline, keep new line terminated.
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        sep = "" if (not original or original.endswith("\n")) else "\n"
        updated = original + sep + new_line

    _atomic_write(env_path, updated)
```

**Step 4: Run, expect pass**

Run: `python -m pytest tests/test_config_writer.py -v`
Expected: 11 passed (7 from prior tasks + 4 new).

**Step 5: Commit**

```bash
git add src/trae_dashboard/config_writer.py tests/test_config_writer.py
git commit -m "feat(config_writer): save_env_var with quoting + atomic write"
```

---

## Task 4: `report._build_message` + `report.build_eml`

**Files:**
- Modify: `src/trae_dashboard/report.py`
- Modify: `tests/test_report.py`

**Step 1: Write the failing test**

Add to `tests/test_report.py`:

```python
import email
import email.policy


def test_build_eml_has_headers_and_html():
    from datetime import datetime, timezone
    from trae_dashboard.config import EmailConfig
    from trae_dashboard.report import build_eml

    cfg = EmailConfig(
        enabled=True,
        smtp_host="smtp.x.com",
        smtp_port=465,
        smtp_user="me@x.com",
        from_addr="me@x.com",
        recipients=["a@x.com", "b@x.com"],
    )
    raw = build_eml(
        cfg,
        subject="[Trae Dashboard] 测试",
        html_body="<p>hello</p>",
    )
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    assert msg["From"] == "me@x.com"
    assert msg["To"] == "a@x.com, b@x.com"
    assert msg["Subject"] == "[Trae Dashboard] 测试"
    # HTML alternative present
    parts = list(msg.walk())
    html_part = next(p for p in parts if p.get_content_type() == "text/html")
    assert b"<p>hello</p>" in html_part.get_payload(decode=True)


def test_build_eml_empty_recipients_leaves_to_blank():
    from trae_dashboard.config import EmailConfig
    from trae_dashboard.report import build_eml

    cfg = EmailConfig(
        enabled=True, smtp_host="h", smtp_port=465, smtp_user="u@x.com",
        from_addr="u@x.com", recipients=[],
    )
    raw = build_eml(cfg, subject="s", html_body="<p>x</p>")
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    # No To header (or empty), must not raise.
    assert msg.get("To", "") == ""
```

**Step 2: Run, expect failure**

Run: `python -m pytest tests/test_report.py -k build_eml -v`
Expected: `AttributeError: module 'trae_dashboard.report' has no attribute 'build_eml'`

**Step 3: Refactor `report.py` to extract `_build_message` and add `build_eml`**

In `src/trae_dashboard/report.py`:

- Add import at the top:

```python
from email import policy as _email_policy
```

- Add a new helper after `send_email`:

```python
def _build_message(
    email_cfg: EmailConfig,
    subject: str,
    html_body: str,
) -> EmailMessage:
    """Construct the EmailMessage for both send and .eml export."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg.from_addr
    if email_cfg.recipients:
        msg["To"] = ", ".join(email_cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("本邮件为 HTML 格式,请使用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")
    return msg


def build_eml(
    email_cfg: EmailConfig,
    subject: str,
    html_body: str,
) -> bytes:
    """Serialize the report as an .eml byte string (RFC 822)."""
    return _build_message(email_cfg, subject, html_body).as_bytes(
        policy=_email_policy.SMTPUTF8,
    )
```

- Refactor `send_email` to use `_build_message` (delete the inline construction):

```python
def send_email(
    email_cfg: EmailConfig,
    subject: str,
    html_body: str,
) -> None:
    """Send the HTML email via SMTP_SSL.

    Password is read from the environment variable named by
    `email_cfg.smtp_password_env`. Raises RuntimeError if missing.
    """
    password = os.environ.get(email_cfg.smtp_password_env, "")
    if not password:
        raise RuntimeError(
            f"SMTP password not found in env var '{email_cfg.smtp_password_env}'. "
            "Set it in .env or export it before running."
        )

    msg = _build_message(email_cfg, subject, html_body)
    log.info(
        "sending email via %s:%d from %s to %s",
        email_cfg.smtp_host, email_cfg.smtp_port,
        email_cfg.from_addr, email_cfg.recipients,
    )
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        email_cfg.smtp_host, email_cfg.smtp_port, context=ctx, timeout=30
    ) as server:
        server.login(email_cfg.smtp_user, password)
        server.send_message(msg)
```

**Step 4: Run, expect pass**

Run: `python -m pytest tests/test_report.py -v`
Expected: all tests pass, including the 2 new `build_eml` tests.

**Step 5: Commit**

```bash
git add src/trae_dashboard/report.py tests/test_report.py
git commit -m "refactor(report): extract _build_message, add build_eml"
```

---

## Task 5: `create_app` accepts `config_path`; report endpoints

**Files:**
- Modify: `src/trae_dashboard/api.py`
- Modify: `src/trae_dashboard/cli.py`
- Modify: `tests/test_api.py`

**Step 1: Write the failing test**

Add to `tests/test_api.py` (top of file imports already present):

```python
def test_api_report_config_includes_smtp_password_set(tmp_data_dir, monkeypatch):
    """GET /api/report/config returns smtp_password_set, never the password itself."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
        email=__import__("trae_dashboard.config", fromlist=["EmailConfig"]).EmailConfig(
            enabled=True, smtp_host="smtp.x.com", smtp_port=465,
            smtp_user="u@x.com", from_addr="u@x.com",
            recipients=["r@x.com"], smtp_password_env="SMTP_PASSWORD_TEST_X",
        ),
    )
    monkeypatch.setenv("SMTP_PASSWORD_TEST_X", "secret-pw")
    app = create_app(cfg=cfg, storage=s, config_path=tmp_data_dir / "config.yaml")
    with TestClient(app) as client:
        r = client.get("/api/report/config")
        assert r.status_code == 200
        body = r.json()
        assert body["smtp_password_set"] is True
        assert "secret-pw" not in r.text


def test_api_put_recipients_round_trip(tmp_data_dir, monkeypatch):
    """PUT /api/report/recipients persists + in-memory update, rejects invalid."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  recipients: [old@x.com]\n",
        encoding="utf-8",
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    cfg.email.enabled = False
    cfg.email.recipients = ["old@x.com"]
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.put(
            "/api/report/recipients",
            json={"recipients": ["NEW@X.com", "  b@x.com  ", "bad", "b@x.com"]},
        )
        assert r.status_code == 400  # "bad" fails validation
        r = client.put(
            "/api/report/recipients",
            json={"recipients": ["NEW@X.com", "  b@x.com  ", "b@x.com"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Lowercased, trimmed, deduped, order preserved
        assert body["recipients"] == ["new@x.com", "b@x.com"]
        # In-memory updated
        r2 = client.get("/api/report/config")
        assert r2.json()["recipients"] == ["new@x.com", "b@x.com"]
        # On disk
        disk = cfg_file.read_text(encoding="utf-8")
        assert "new@x.com" in disk
        assert "b@x.com" in disk
        assert "old@x.com" not in disk


def test_api_put_smtp_persists_and_preserves_recipients(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  recipients: [keep@x.com]\n  smtp_password_env: SMTP_PASSWORD\n",
        encoding="utf-8",
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.put("/api/report/smtp", json={
            "smtp_host": "smtp.new.com", "smtp_port": 587,
            "smtp_user": "u@new.com", "from_addr": "u@new.com",
            "send_time": "08:30",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["smtp_host"] == "smtp.new.com"
        # Recipients preserved
        assert body["recipients"] == ["keep@x.com"]
        # On disk
        disk = cfg_file.read_text(encoding="utf-8")
        assert "smtp_host: smtp.new.com" in disk
        assert "smtp_port: 587" in disk
        assert "keep@x.com" in disk
        assert "smtp_password_env: SMTP_PASSWORD" in disk


def test_api_put_smtp_rejects_invalid(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    cfg = Config(openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[])
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.put("/api/report/smtp", json={
            "smtp_host": "", "smtp_port": 465, "smtp_user": "u@x.com",
            "from_addr": "u@x.com", "send_time": "09:00",
        })
        assert r.status_code == 400


def test_api_post_smtp_password_writes_env(tmp_data_dir, monkeypatch):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    env_file = tmp_data_dir / ".env"
    env_file.write_text("TRAE_APP_ID=abc\n", encoding="utf-8")
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  smtp_password_env: SMTP_PASSWORD\n",
        encoding="utf-8",
    )
    cfg = Config(openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[])
    cfg.email.smtp_password_env = "SMTP_PASSWORD"
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file, env_path=env_file)
    with TestClient(app) as client:
        r = client.post("/api/report/smtp/password", json={"password": "new-pw"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"smtp_password_set": True}
        assert "new-pw" not in r.text
        # .env has the value
        text = env_file.read_text(encoding="utf-8")
        assert "SMTP_PASSWORD=new-pw" in text
        # TRAE_APP_ID preserved
        assert "TRAE_APP_ID=abc" in text


def test_api_post_eml_returns_eml_bytes(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    s.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
        email=__import__("trae_dashboard.config", fromlist=["EmailConfig"]).EmailConfig(
            enabled=False,  # .eml export must NOT require enabled
            smtp_host="smtp.x.com", smtp_port=465,
            smtp_user="u@x.com", from_addr="u@x.com",
            recipients=[],
        ),
        included_model_names={"GLM-5.1"},
    )
    s.upsert_model_usage(
        email="a@x.com", cycle_start="2026-06-10", cycle_end="2026-07-06",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=50,
    )
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.post(
            "/api/report/eml",
            json={"recipients": ["dest@x.com"]},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("message/rfc822")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert ".eml" in cd
        # Parseable
        import email, email.policy
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(r.content)
        assert msg["From"] == "u@x.com"
        assert msg["To"] == "dest@x.com"
```

**Step 2: Run, expect failure (no `config_path` arg)**

Run: `python -m pytest tests/test_api.py -k "report_config or put_recipients or put_smtp or smtp_password or post_eml" -v`
Expected: `TypeError: create_app() got an unexpected keyword argument 'config_path'`

**Step 3: Update `create_app` signature and add endpoints**

In `src/trae_dashboard/api.py`:

- Add at the top of the file (after existing imports):

```python
from pathlib import Path as _Path
from .config_writer import save_recipients, save_email_config, save_env_var
```

- Change the signature:

```python
def create_app(
    *,
    cfg: Config,
    storage: Storage,
    config_path: _Path | None = None,
    env_path: _Path | None = None,
) -> FastAPI:
```

- Add a helper near the top of `create_app`:

```python
    app = FastAPI(title="Trae Token Dashboard")

    # Default env path = config_path's sibling .env (mirrors `.env` usage).
    if env_path is None and config_path is not None:
        env_path = config_path.parent / ".env"

    def _smtp_password_set() -> bool:
        if env_path is None or not env_path.exists():
            return False
        from dotenv import dotenv_values
        return bool(dotenv_values(env_path).get(cfg.email.smtp_password_env))
```

- Update `GET /api/report/config`:

```python
    @app.get("/api/report/config")
    def get_report_config():
        return {
            "enabled": cfg.email.enabled,
            "smtp_host": cfg.email.smtp_host,
            "smtp_port": cfg.email.smtp_port,
            "smtp_user": cfg.email.smtp_user,
            "from_addr": cfg.email.from_addr,
            "recipients": list(cfg.email.recipients),
            "send_time": cfg.email.send_time,
            "smtp_password_set": _smtp_password_set(),
        }
```

- Add Pydantic models near `ReportIn`:

```python
class RecipientsIn(BaseModel):
    recipients: list[str]


class SmtpIn(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_user: str
    from_addr: str
    send_time: str


class PasswordIn(BaseModel):
    password: str
```

- Add the four new endpoints right after `get_report_config`:

```python
    @app.put("/api/report/recipients")
    def put_recipients(body: RecipientsIn):
        if config_path is None:
            raise HTTPException(status_code=500, detail="config_path not wired")
        # Validate
        cleaned: list[str] = []
        for r in body.recipients:
            addr = (r or "").strip().lower()
            if not addr:
                continue
            if not _EMAIL_RE.match(addr):
                raise HTTPException(
                    status_code=400, detail=f"invalid recipient email: {r!r}"
                )
            cleaned.append(addr)
        seen: set[str] = set()
        deduped = [a for a in cleaned if not (a in seen or seen.add(a))]
        # Persist (may raise ValueError on YAML issues → 500)
        try:
            save_recipients(config_path, deduped)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"save_recipients failed: {e}") from e
        # Sync in-memory (dataclasses are mutable)
        cfg.email.recipients = deduped
        return {"recipients": cfg.email.recipients}

    @app.put("/api/report/smtp")
    def put_smtp(body: SmtpIn):
        if config_path is None:
            raise HTTPException(status_code=500, detail="config_path not wired")
        try:
            save_email_config(
                config_path,
                smtp_host=body.smtp_host,
                smtp_port=body.smtp_port,
                smtp_user=body.smtp_user,
                from_addr=body.from_addr,
                send_time=body.send_time,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"save_email_config failed: {e}") from e
        # Sync in-memory
        cfg.email.smtp_host = body.smtp_host.strip()
        cfg.email.smtp_port = int(body.smtp_port)
        cfg.email.smtp_user = body.smtp_user.strip()
        cfg.email.from_addr = body.from_addr.strip()
        cfg.email.send_time = body.send_time.strip()
        return {
            "smtp_host": cfg.email.smtp_host,
            "smtp_port": cfg.email.smtp_port,
            "smtp_user": cfg.email.smtp_user,
            "from_addr": cfg.email.from_addr,
            "send_time": cfg.email.send_time,
            "recipients": list(cfg.email.recipients),
        }

    @app.post("/api/report/smtp/password")
    def post_smtp_password(body: PasswordIn):
        if env_path is None:
            raise HTTPException(status_code=500, detail="env_path not wired")
        try:
            save_env_var(env_path, cfg.email.smtp_password_env, body.password)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"save_env_var failed: {e}") from e
        return {"smtp_password_set": True}

    @app.post("/api/report/eml")
    def post_eml(body: ReportIn | None = None):
        from .report import build_eml
        payload = body or ReportIn()
        recipients_override = payload.recipients
        if recipients_override is not None:
            cleaned: list[str] = []
            for r in recipients_override:
                addr = (r or "").strip().lower()
                if not addr:
                    continue
                if not _EMAIL_RE.match(addr):
                    raise HTTPException(
                        status_code=400, detail=f"invalid recipient email: {r!r}"
                    )
                cleaned.append(addr)
            recipients_override = cleaned
        # Build a temporary EmailConfig with the override (empty allowed)
        from dataclasses import replace as _dc_replace
        email_cfg = cfg.email
        if recipients_override is not None:
            email_cfg = _dc_replace(email_cfg, recipients=list(recipients_override))
        # Render the report with the overridden recipients so subject/to match.
        from .report import run_report
        try:
            summary = run_report(
                storage, cfg,
                recipients_override=recipients_override,
                send=False,  # NEVER send
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"render failed: {e}") from e
        raw = build_eml(email_cfg, summary["subject"], summary["html"])
        filename = f'trae-report-{datetime.now(timezone.utc).strftime("%Y-%m-%d")}.eml'
        return Response(
            content=raw,
            media_type="message/rfc822",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
```

In `src/trae_dashboard/cli.py`, update `_serve` to pass `config_path` and `env_path`:

```python
def _serve(config_path: Path, host: str, port: int, *, scheduler: bool = False) -> None:
    ...
    app = create_app(
        cfg=cfg, storage=storage, config_path=config_path, env_path=config_path.parent / ".env",
    )
```

(Keep all other cli.py changes minimal; `_report` already loads its own `cfg` and doesn't need updates.)

**Step 4: Run, expect pass**

Run: `python -m pytest tests/test_api.py -k "report_config or put_recipients or put_smtp or smtp_password or post_eml" -v`
Expected: 6 new tests pass.

Also run the whole API suite to confirm no regressions:

Run: `python -m pytest tests/test_api.py -v`
Expected: all pass (existing 615-line suite + 6 new = 6 new tests added).

**Step 5: Commit**

```bash
git add src/trae_dashboard/api.py src/trae_dashboard/cli.py tests/test_api.py
git commit -m "feat(api): recipients/smtp write-back + .eml export endpoints"
```

---

## Task 6: Frontend — add Mail Settings tab structure to the report modal

**Files:**
- Modify: `src/trae_dashboard/static/index.html` (modal markup, lines ~405-455)
- Modify: `src/trae_dashboard/static/style.css` (new tab styles)
- Modify: `src/trae_dashboard/static/index.html` (script at bottom — JS for tabs)

**Step 1: No new test (visual/JS). Verify existing dashboard loads + report modal still opens.**

Run: `python -m pytest tests/test_e2e.py -v`
Expected: pass (smoke check the server boots and the index page is served).

Also: manually verify `python -m trae_dashboard serve --config /tmp/cfg.yaml` boots without import errors.

**Step 2: Add tab markup to the existing modal**

In `src/trae_dashboard/static/index.html`, replace the report modal markup (lines 405-455) with:

```html
  <!-- ===================== Send Report Modal ===================== -->
  <div class="modal-overlay" id="report-modal" hidden role="dialog" aria-modal="true" aria-labelledby="report-modal-title">
    <div class="modal modal--report">
      <div class="modal__head">
        <h3 class="modal__title" id="report-modal-title">
          发送邮件报告
          <small>预览报告内容 · 确认收件人后发送</small>
        </h3>
        <button class="modal__close" type="button" id="report-modal-close" aria-label="关闭">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>

      <div class="tabs" role="tablist" aria-label="邮件报告选项卡">
        <button class="tab is-active" type="button" role="tab" id="tab-send" aria-controls="tab-panel-send" aria-selected="true">发送</button>
        <button class="tab" type="button" role="tab" id="tab-settings" aria-controls="tab-panel-settings" aria-selected="false">邮件设置</button>
      </div>

      <div class="modal__body">
        <div class="report-modal__status-banner" id="report-modal-status" hidden></div>

        <!-- Tab: Send -->
        <div class="tab-panel" id="tab-panel-send" role="tabpanel" aria-labelledby="tab-send">
          <div class="report-modal__recipients">
            <div class="report-modal__recipients-head">
              <h4>收件人</h4>
              <span class="report-modal__recipients-meta" id="report-recipients-meta">— 位收件人 · 已选 — 位</span>
            </div>
            <div class="report-modal__recipients-list" id="report-recipients-list">
              <span class="report-modal__preview-loading">加载中…</span>
            </div>
          </div>

          <div class="report-modal__preview-wrap">
            <div class="report-modal__preview-head">
              <span>报告预览(与实际邮件内容一致)</span>
              <span>主题: <code id="report-preview-subject">—</code></span>
            </div>
            <div class="report-modal__preview-loading" id="report-preview-loading">正在生成预览…</div>
            <div class="report-modal__preview-error" id="report-preview-error" hidden></div>
            <iframe class="report-modal__preview-iframe" id="report-preview-iframe" hidden title="报告预览"></iframe>
          </div>
        </div>

        <!-- Tab: Mail Settings -->
        <div class="tab-panel" id="tab-panel-settings" role="tabpanel" aria-labelledby="tab-settings" hidden>
          <!-- Recipients management -->
          <section class="settings-section">
            <h4>收件人管理</h4>
            <p class="settings-hint">修改后点「保存收件人」才写回 <code>config.yaml</code>。</p>
            <ul class="settings-recipient-list" id="settings-recipient-list"></ul>
            <div class="settings-recipient-add">
              <input type="email" id="settings-recipient-input" placeholder="user@company.com" autocomplete="off" />
              <button class="btn btn--ghost" type="button" id="settings-recipient-add-btn">添加</button>
            </div>
            <button class="btn btn--primary" type="button" id="settings-recipient-save">保存收件人</button>
          </section>

          <!-- SMTP config -->
          <section class="settings-section">
            <h4>SMTP 服务器</h4>
            <div class="settings-grid">
              <label>主机 <input type="text" id="settings-smtp-host" autocomplete="off" /></label>
              <label>端口 <input type="number" id="settings-smtp-port" min="1" max="65535" autocomplete="off" /></label>
              <label>登录用户 <input type="text" id="settings-smtp-user" autocomplete="off" /></label>
              <label>发件邮箱 <input type="email" id="settings-smtp-from" autocomplete="off" /></label>
              <label>每日发送时间 <input type="text" id="settings-smtp-send-time" placeholder="HH:MM" autocomplete="off" /></label>
            </div>
            <button class="btn btn--primary" type="button" id="settings-smtp-save">保存 SMTP 设置</button>
            <div class="settings-enabled-row">
              邮件报告启用状态:
              <label class="settings-toggle">
                <input type="checkbox" id="settings-enabled-toggle" disabled />
                <span id="settings-enabled-label">—</span>
              </label>
              <span class="settings-hint">(只读,关闭需手改 <code>config.yaml</code>)</span>
            </div>
          </section>

          <!-- SMTP password -->
          <section class="settings-section">
            <h4>SMTP 密码</h4>
            <p class="settings-hint">状态: <strong id="settings-password-status">—</strong></p>
            <div class="settings-password-row" id="settings-password-row">
              <button class="btn btn--ghost" type="button" id="settings-password-edit-btn">修改密码</button>
              <span class="settings-password-form" hidden>
                <input type="password" id="settings-password-input" placeholder="新密码" autocomplete="new-password" />
                <button class="btn btn--primary" type="button" id="settings-password-save-btn">保存</button>
                <button class="btn btn--ghost" type="button" id="settings-password-cancel-btn">取消</button>
              </span>
            </div>
          </section>
        </div>

        <div class="report-modal__footer">
          <span class="report-modal__footer-hint" id="report-footer-hint">改动仅对本次发送生效,不会写回 config.yaml</span>
          <div class="report-modal__footer-actions">
            <button class="btn btn--ghost" type="button" id="report-modal-eml">下载 .eml</button>
            <button class="btn btn--ghost" type="button" id="report-modal-cancel">取消</button>
            <button class="btn btn--primary" type="button" id="report-modal-send" disabled>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="width:16px;height:16px;">
                <line x1="22" y1="2" x2="11" y2="13"/>
                <polygon points="22,2 15,22 11,13 2,9"/>
              </svg>
              <span class="btn__label">确认发送</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
```

**Step 3: Add tab + settings CSS to `style.css`**

Append to `src/trae_dashboard/static/style.css`:

```css
/* ===================== Report modal: tabs + settings ===================== */
.tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid #e5e7eb;
  padding: 0 24px;
  background: #f9fafb;
}
.tabs .tab {
  background: none;
  border: none;
  padding: 12px 18px;
  font: inherit;
  color: #6b7280;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tabs .tab:hover { color: #111827; }
.tabs .tab.is-active {
  color: #1e3a8a;
  border-bottom-color: #1e3a8a;
  font-weight: 600;
}

.tab-panel { display: block; }
.tab-panel[hidden] { display: none; }

.settings-section {
  padding: 16px 0;
  border-bottom: 1px solid #f3f4f6;
}
.settings-section:last-child { border-bottom: none; }
.settings-section h4 {
  margin: 0 0 8px;
  font-size: 14px;
  color: #111827;
}
.settings-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: #6b7280;
}
.settings-recipient-list {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.settings-recipient-list li {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  background: #eef2ff;
  color: #1e3a8a;
  border-radius: 4px;
  font-size: 13px;
}
.settings-recipient-list button {
  background: none;
  border: none;
  color: #6b7280;
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
}
.settings-recipient-list button:hover { color: #dc2626; }
.settings-recipient-add {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.settings-recipient-add input { flex: 1; }
.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  margin-bottom: 12px;
}
.settings-grid label {
  display: flex;
  flex-direction: column;
  font-size: 12px;
  color: #6b7280;
  gap: 4px;
}
.settings-grid input {
  padding: 6px 8px;
  border: 1px solid #d1d5db;
  border-radius: 4px;
  font: inherit;
}
.settings-enabled-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #374151;
}
.settings-enabled-row input[disabled] { cursor: not-allowed; }
.settings-password-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.settings-password-form {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.settings-password-form input { width: 200px; }
```

**Step 4: Wire tab switching + settings behavior in the inline `<script>`**

Locate the existing `// ---- Send report button (opens the confirm modal) ----` block in the inline `<script>` of `index.html`. Make the following changes:

a) Add tab switching handlers right after the existing `openReportModal` function definition:

```javascript
    // ----- Tab switching -----
    var tabSend = document.getElementById("tab-send");
    var tabSettings = document.getElementById("tab-settings");
    var tabPanelSend = document.getElementById("tab-panel-send");
    var tabPanelSettings = document.getElementById("tab-panel-settings");
    function activateTab(name) {
      var isSend = name === "send";
      tabSend.classList.toggle("is-active", isSend);
      tabSettings.classList.toggle("is-active", !isSend);
      tabSend.setAttribute("aria-selected", isSend ? "true" : "false");
      tabSettings.setAttribute("aria-selected", isSend ? "false" : "true");
      tabPanelSend.hidden = !isSend;
      tabPanelSettings.hidden = isSend;
    }
    if (tabSend) tabSend.addEventListener("click", function () { activateTab("send"); });
    if (tabSettings) tabSettings.addEventListener("click", function () { activateTab("settings"); });
```

b) In `openReportModal`, after the existing `reportModal.hidden = false;` line, call `activateTab("send")` and call a new `loadMailSettings()` to populate the form fields from the current config.

c) Append a new block below the existing `reportModalSendBtn` click handler:

```javascript
    // ----- Tab2: Mail Settings -----
    var settingsRecipientList = document.getElementById("settings-recipient-list");
    var settingsRecipientInput = document.getElementById("settings-recipient-input");
    var settingsRecipientAddBtn = document.getElementById("settings-recipient-add-btn");
    var settingsRecipientSaveBtn = document.getElementById("settings-recipient-save");
    var settingsSmtpHost = document.getElementById("settings-smtp-host");
    var settingsSmtpPort = document.getElementById("settings-smtp-port");
    var settingsSmtpUser = document.getElementById("settings-smtp-user");
    var settingsSmtpFrom = document.getElementById("settings-smtp-from");
    var settingsSmtpSendTime = document.getElementById("settings-smtp-send-time");
    var settingsSmtpSaveBtn = document.getElementById("settings-smtp-save");
    var settingsEnabledToggle = document.getElementById("settings-enabled-toggle");
    var settingsEnabledLabel = document.getElementById("settings-enabled-label");
    var settingsPasswordStatus = document.getElementById("settings-password-status");
    var settingsPasswordEditBtn = document.getElementById("settings-password-edit-btn");
    var settingsPasswordRow = document.getElementById("settings-password-row");
    var settingsPasswordForm = settingsPasswordRow
      ? settingsPasswordRow.querySelector(".settings-password-form")
      : null;
    var settingsPasswordInput = document.getElementById("settings-password-input");
    var settingsPasswordSaveBtn = document.getElementById("settings-password-save-btn");
    var settingsPasswordCancelBtn = document.getElementById("settings-password-cancel-btn");
    var settingsDraft = { recipients: [] };  // local working copy

    function renderSettingsRecipients() {
      settingsRecipientList.innerHTML = "";
      if (!settingsDraft.recipients.length) {
        var li = document.createElement("li");
        li.textContent = "(暂无收件人)";
        li.style.background = "transparent";
        li.style.color = "#9ca3af";
        settingsRecipientList.appendChild(li);
        return;
      }
      settingsDraft.recipients.forEach(function (addr, idx) {
        var li = document.createElement("li");
        li.textContent = addr + " ";
        var btn = document.createElement("button");
        btn.type = "button";
        btn.setAttribute("aria-label", "删除 " + addr);
        btn.textContent = "×";
        btn.addEventListener("click", function () {
          settingsDraft.recipients.splice(idx, 1);
          renderSettingsRecipients();
        });
        li.appendChild(btn);
        settingsRecipientList.appendChild(li);
      });
    }
    if (settingsRecipientAddBtn) {
      settingsRecipientAddBtn.addEventListener("click", function () {
        var v = (settingsRecipientInput.value || "").trim().toLowerCase();
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
          App.showToast("邮箱格式不正确", { variant: "error" });
          return;
        }
        if (settingsDraft.recipients.indexOf(v) !== -1) {
          App.showToast("收件人已存在", { variant: "warn" });
          return;
        }
        settingsDraft.recipients.push(v);
        settingsRecipientInput.value = "";
        renderSettingsRecipients();
      });
    }
    if (settingsRecipientSaveBtn) {
      settingsRecipientSaveBtn.addEventListener("click", async function () {
        try {
          var result = await App.apiPut("/api/report/recipients", {
            recipients: settingsDraft.recipients,
          });
          // Update reportState.config to keep Send tab in sync
          reportState.config.recipients = result.recipients;
          reportState.selected = new Set(result.recipients);
          renderReportRecipients();
          App.showToast("收件人已保存 (" + result.recipients.length + ")", { variant: "success" });
        } catch (e) {
          App.showToast("保存失败: " + (e.message || e), { variant: "error" });
        }
      });
    }
    if (settingsSmtpSaveBtn) {
      settingsSmtpSaveBtn.addEventListener("click", async function () {
        var body = {
          smtp_host: settingsSmtpHost.value,
          smtp_port: parseInt(settingsSmtpPort.value, 10),
          smtp_user: settingsSmtpUser.value,
          from_addr: settingsSmtpFrom.value,
          send_time: settingsSmtpSendTime.value,
        };
        try {
          await App.apiPut("/api/report/smtp", body);
          App.showToast("SMTP 设置已保存", { variant: "success" });
          // Refresh reportState.config so enabled flag updates if relevant
          var cfg = await App.apiGet("/api/report/config");
          reportState.config = cfg;
          applySettingsStatus(cfg);
        } catch (e) {
          App.showToast("保存失败: " + (e.message || e), { variant: "error" });
        }
      });
    }
    function applySettingsStatus(cfg) {
      settingsSmtpHost.value = cfg.smtp_host || "";
      settingsSmtpPort.value = cfg.smtp_port || 465;
      settingsSmtpUser.value = cfg.smtp_user || "";
      settingsSmtpFrom.value = cfg.from_addr || "";
      settingsSmtpSendTime.value = cfg.send_time || "09:00";
      settingsDraft.recipients = (cfg.recipients || []).slice();
      renderSettingsRecipients();
      settingsEnabledToggle.checked = !!cfg.enabled;
      settingsEnabledLabel.textContent = cfg.enabled ? "已启用" : "未启用";
      settingsEnabledToggle.disabled = !!cfg.enabled;  // only allow enabling
      settingsPasswordStatus.textContent = cfg.smtp_password_set ? "已设置" : "未设置";
      settingsPasswordStatus.style.color = cfg.smtp_password_set ? "#16a34a" : "#9ca3af";
    }
    if (settingsPasswordEditBtn) {
      settingsPasswordEditBtn.addEventListener("click", function () {
        settingsPasswordForm.hidden = false;
        settingsPasswordEditBtn.hidden = true;
        settingsPasswordInput.value = "";
        setTimeout(function () { settingsPasswordInput.focus(); }, 0);
      });
    }
    if (settingsPasswordCancelBtn) {
      settingsPasswordCancelBtn.addEventListener("click", function () {
        settingsPasswordForm.hidden = true;
        settingsPasswordEditBtn.hidden = false;
        settingsPasswordInput.value = "";
      });
    }
    if (settingsPasswordSaveBtn) {
      settingsPasswordSaveBtn.addEventListener("click", async function () {
        var pw = settingsPasswordInput.value || "";
        if (!pw) {
          App.showToast("密码不能为空", { variant: "error" });
          return;
        }
        try {
          await App.apiPost("/api/report/smtp/password", { password: pw });
          App.showToast("密码已保存", { variant: "success" });
          settingsPasswordInput.value = "";  // never persist in DOM
          settingsPasswordForm.hidden = true;
          settingsPasswordEditBtn.hidden = false;
          // Update status only
          var cfg = await App.apiGet("/api/report/config");
          applySettingsStatus(cfg);
        } catch (e) {
          App.showToast("保存失败: " + (e.message || e), { variant: "error" });
        }
      });
    }
    // Populate on modal open
    async function loadMailSettings() {
      try {
        var cfg = await App.apiGet("/api/report/config");
        applySettingsStatus(cfg);
      } catch (e) {
        // Surface a hint in the status banner; the Send tab will already
        // have shown its own error.
      }
    }
```

In `openReportModal`, **after** the existing `reportModal.hidden = false;` line, add:

```javascript
      activateTab("send");
      loadMailSettings();
```

d) In `closeReportModal`, also hide the password form and re-show the edit button:

```javascript
    function closeReportModal() {
      if (!reportModal) return;
      reportModal.hidden = true;
      reportPreviewIframe.srcdoc = "";
      reportState.previewHtml = "";
      reportState.selected = new Set();
      reportState.config = null;
      setReportStatus("");
      if (settingsPasswordForm) settingsPasswordForm.hidden = true;
      if (settingsPasswordEditBtn) settingsPasswordEditBtn.hidden = false;
      if (lastReportFocus && typeof lastReportFocus.focus === "function") {
        try { lastReportFocus.focus(); } catch (_) {}
      }
    }
```

e) Add the "下载 .eml" button handler, and a new helper that triggers the download:

```javascript
    var reportModalEmlBtn = document.getElementById("report-modal-eml");
    if (reportModalEmlBtn) {
      reportModalEmlBtn.addEventListener("click", async function () {
        var selected = Array.from(reportState.selected);
        if (!selected.length) {
          setReportStatus("请至少选择一位收件人", "error");
          return;
        }
        try {
          var blob = await App.apiPostBlob("/api/report/eml", {
            recipients: selected,
          });
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = "trae-report-" +
            new Date().toISOString().slice(0, 10) + ".eml";
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        } catch (e) {
          setReportStatus("下载失败: " + (e.message || e), "error");
        }
      });
    }
```

**Step 5: Add the `apiPut` / `apiPostBlob` helpers to `app.js` (or to the inline script if `App.apiPost` lives there)**

Look at `src/trae_dashboard/static/app.js` (referenced as `app.js?v=3` in the index). Add a thin wrapper:

```javascript
App.apiPut = function (path, body) {
  return fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then(function (r) {
    if (!r.ok) {
      return r.json().then(function (e) {
        throw new Error(e.detail || ("HTTP " + r.status));
      }).catch(function () { throw new Error("HTTP " + r.status); });
    }
    return r.json();
  });
};

App.apiPostBlob = function (path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then(function (r) {
    if (!r.ok) {
      return r.json().then(function (e) {
        throw new Error(e.detail || ("HTTP " + r.status));
      }).catch(function () { throw new Error("HTTP " + r.status); });
    }
    return r.blob();
  });
};
```

**Step 6: Run the full pytest suite (catch any regression in existing tests)**

Run: `python -m pytest tests/ -v --ignore=tests/test_e2e.py`
Expected: all pass. (`test_e2e.py` is excluded because it depends on a live server with seeded data.)

**Step 7: Smoke test the running app**

In a separate shell:

```bash
python -m trae_dashboard serve --config /path/to/config.yaml --port 8765
```

Open `http://127.0.0.1:8765`, click the 发送报告 button, verify:
- Two tabs render (发送 / 邮件设置).
- Switching to 邮件设置 shows recipients, SMTP form, password status, enabled toggle.
- Saving recipients updates the Send-tab chips on switch back.
- Saving SMTP fields persists (restart server, re-open modal, fields populated).
- Modifying the password shows "已设置" after save; password is not echoed anywhere.
- Clicking 下载 .eml downloads a `.eml` file that opens in Outlook/Foxmail with the chosen recipients in To.

**Step 8: Commit**

```bash
git add src/trae_dashboard/static/index.html src/trae_dashboard/static/style.css src/trae_dashboard/static/app.js
git commit -m "feat(ui): report modal — recipients/SMTP settings tab + .eml download"
```

---

## Task 7: End-to-end integration test

**Files:**
- Create: `tests/test_email_enhancement_e2e.py` (or extend `tests/test_e2e.py` if that already covers the report flow)

**Step 1: Write a focused e2e test exercising the full chain**

```python
"""End-to-end: UI-driven config write + .eml export + SMTP send paths."""
from __future__ import annotations

import email
import email.policy
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    from starlette.testclient import TestClient  # type: ignore

from trae_dashboard.api import create_app
from trae_dashboard.config import Config, EmailConfig
from trae_dashboard.storage import Storage


def test_email_enhancement_full_flow(tmp_data_dir: Path):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com", cycle_start="2026-06-10", cycle_end="2026-07-06",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=1000, output_tokens=500,
    )

    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  recipients: []\n",
        encoding="utf-8",
    )
    env_file = tmp_data_dir / ".env"
    env_file.write_text("", encoding="utf-8")

    cfg = Config(
        openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[],
        email=EmailConfig(enabled=False, smtp_password_env="SMTP_PASSWORD_E2E"),
        included_model_names={"GLM-5.1"},
    )
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file, env_path=env_file)
    with TestClient(app) as c:
        # 1. Configure recipients
        r = c.put("/api/report/recipients", json={"recipients": ["r1@x.com", "r2@x.com"]})
        assert r.status_code == 200
        assert r.json()["recipients"] == ["r1@x.com", "r2@x.com"]
        # 2. Configure SMTP
        r = c.put("/api/report/smtp", json={
            "smtp_host": "smtp.x.com", "smtp_port": 465,
            "smtp_user": "u@x.com", "from_addr": "u@x.com", "send_time": "08:00",
        })
        assert r.status_code == 200
        # 3. Set password
        r = c.post("/api/report/smtp/password", json={"password": "supersecret"})
        assert r.status_code == 200
        assert r.json() == {"smtp_password_set": True}
        assert "supersecret" not in r.text
        # 4. Config now reports everything
        r = c.get("/api/report/config")
        assert r.status_code == 200
        body = r.json()
        assert body["smtp_password_set"] is True
        assert body["recipients"] == ["r1@x.com", "r2@x.com"]
        # 5. Download .eml (works even with email.enabled=false)
        r = c.post("/api/report/eml", json={"recipients": ["r1@x.com"]})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("message/rfc822")
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(r.content)
        assert msg["From"] == "u@x.com"
        assert msg["To"] == "r1@x.com"
        # 6. .env still contains the password
        env_text = env_file.read_text(encoding="utf-8")
        assert "SMTP_PASSWORD_E2E=supersecret" in env_text
```

**Step 2: Run, expect pass**

Run: `python -m pytest tests/test_email_enhancement_e2e.py -v`
Expected: 1 passed.

**Step 3: Commit**

```bash
git add tests/test_email_enhancement_e2e.py
git commit -m "test: end-to-end for email enhancement flow"
```

---

## Task 8: README + .env.example update

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

**Step 1: Update `.env.example` with a note about the dashboard managing it**

Append (if not already present):

```bash
# The dashboard's "邮件设置" tab can write SMTP_PASSWORD here.
# Manual edits are fine; the dashboard will overwrite the SMTP_PASSWORD line.
```

**Step 2: Update `README.md` "邮件报告部署" section**

In the existing README, after the description of the email section, add a short paragraph:

```markdown
### 在 UI 里修改收件人 / SMTP 配置

「发送报告」弹窗里有两个 Tab:

- **发送**: 选收件人 + 预览 + 「下载 .eml」/「确认发送」。
- **邮件设置**: 增删收件人并保存; 编辑 SMTP 主机/端口/用户/发件邮箱/发送时间; 单独入口修改 SMTP 密码(写到 `.env`); 启用开关为只读(关闭需手改 `config.yaml`)。

所有修改都立即生效(无需重启服务)。
```

**Step 3: Run the test suite once more for final confidence**

Run: `python -m pytest tests/ -v --ignore=tests/test_e2e.py`
Expected: all pass.

**Step 4: Commit**

```bash
git add README.md .env.example
git commit -m "docs: README + .env.example for email settings UI"
```

---

## Done

All 8 tasks complete. Final summary of new files:
- `src/trae_dashboard/config_writer.py`
- `tests/test_config_writer.py`
- `tests/test_email_enhancement_e2e.py`

Modified files:
- `src/trae_dashboard/report.py` (extracted `_build_message`, added `build_eml`)
- `src/trae_dashboard/api.py` (4 new endpoints, `create_app` gains `config_path`/`env_path`, `GET /api/report/config` gains `smtp_password_set`)
- `src/trae_dashboard/cli.py` (`_serve` passes paths)
- `src/trae_dashboard/static/index.html` (two-tab modal, settings UI, .eml download button, JS handlers)
- `src/trae_dashboard/static/style.css` (tab + settings styles)
- `src/trae_dashboard/static/app.js` (`apiPut`, `apiPostBlob` helpers)
- `README.md`, `.env.example`
- `tests/test_report.py`, `tests/test_api.py` (extended)
