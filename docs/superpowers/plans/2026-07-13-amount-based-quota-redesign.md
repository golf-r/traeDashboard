# 金额限额重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 traeDashboard 从 Token 计量体系迁移到金额计量体系(per_account_quota 从 50_000_000 token 改为 120.0 元),只采集 Trae 内置模型,移除 included_model_names/model_aliases/display_weights。

**Architecture:** 原地重构现有模块(storage/collector/api/report/cli/config),保持模块边界。`model_usage` 表 schema 演进(新增 amount_total/amount_basic/amount_pay_go/currency,清空旧数据),token 字段保留供 tooltip。

**Tech Stack:** Python 3.10+, FastAPI, SQLite (WAL mode), vanilla JS (无构建步骤), PyYAML, pytest, httpx。

**Spec reference:** `docs/superpowers/specs/2026-07-13-amount-based-quota-redesign.md`

---

## File Structure

**Modify:**
- `src/trae_dashboard/config.py` — 移除 included_model_names/model_aliases/display_weights;per_account_quota 类型 int→float,默认值 120.0
- `src/trae_dashboard/storage.py` — schema 演进新增 amount 字段;移除 display_weights 参数;upsert_model_usage/get_model_usage_* 签名调整;prune 依据改为 amount_total;新增 get_total_amount
- `src/trae_dashboard/collector.py` — 移除 _canonical 映射;过滤改为 model_source == "Trae";采集 amount 字段
- `src/trae_dashboard/api.py` — /api/status 与 /api/accounts 主指标改为 amount_total;移除 included_model_names 传参
- `src/trae_dashboard/report.py` — ReportRow 改为金额字段;新增 _fmt_cny;HTML 表格/KPI 用金额
- `src/trae_dashboard/cli.py` — 移除 display_weights=cfg.display_weights 和 cfg.included_model_names 引用
- `config.example.yaml` — 删除 included_model_names/display_weights 块;per_account_quota 改 120.0
- `src/trae_dashboard/static/index.html` — 移除 Input/Output 双色配额条;新增单色 quota-fill;KPI/表格列改金额
- `src/trae_dashboard/static/app.js` — normalizeAccount/enrichStatus 改 amount_total;新增 formatCNY;CSV 列头改 Consumed(CNY)
- `src/trae_dashboard/static/style.css` — 调整 quota-fill 样式(单色)

**Modify tests:**
- `tests/test_config.py`, `tests/test_storage.py`, `tests/test_collector.py`, `tests/test_api.py`, `tests/test_report.py`, `tests/test_cli.py`

---

## Task 1: config.py — 移除白名单/别名/权重

**Files:**
- Modify: `src/trae_dashboard/config.py`
- Test: `tests/test_config.py`

**保持不变:** `Account`, `EmailConfig`, `app_id`/`app_secret`(实际值,从 env 读取), `fetch_interval_minutes`, `db_path`, `email` 段, `load_config` 的 credential 读取与 user-metrics 校验逻辑。

**移除:** `DEFAULT_INCLUDED_MODEL_NAMES` 常量, `_load_included_model_names`, `_load_model_aliases`, `_load_display_weights`, `Config.included_model_names`, `Config.model_aliases`, `Config.display_weights`。

**变更:** `per_account_quota: int = 50_000_000` → `per_account_quota: float = 120.0`。

- [ ] **Step 1: 写失败测试**

完整替换 `tests/test_config.py`:

```python
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
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError` 或 `TypeError`(因为 Config 仍含 included_model_names 字段且 per_account_quota 是 int)

- [ ] **Step 3: 编辑 config.py**

基于现有 `src/trae_dashboard/config.py` 做以下**精准修改**(保留 EmailConfig、app_id/app_secret、_load_email_config、user-metrics 校验逻辑):

**3a. 删除 `DEFAULT_INCLUDED_MODEL_NAMES` 常量**(原文件 21-44 行整块删除)。

**3b. 修改 `Config` dataclass**(原文件 74-105 行),删除 `included_model_names`/`model_aliases`/`display_weights` 三个字段,`per_account_quota` 类型与默认值变更:

```python
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
```

**3c. 删除三个 `_load_*` 函数**(`_load_included_model_names`、`_load_model_aliases`、`_load_display_weights`,原文件 108-185 行整块删除)。

**3d. 修改 `load_config` 函数**(原文件 188-250 行),移除对三个函数的调用,改 `per_account_quota` 解析:

```python
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
```

保留 `_load_email_config` 函数(原文件 253-294 行)**不动**。保留 `Account` 和 `EmailConfig` dataclass(原文件 47-71 行)**不动**。保留文件顶部的 `from dotenv import load_dotenv; load_dotenv()` 和 `import os`/`import yaml` 等导入**不动**。

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trae_dashboard/config.py tests/test_config.py
git commit -m "refactor(config): drop model whitelist/aliases/weights, switch quota to CNY"
```

---

## Task 2: storage.py — schema 演进 + 移除 display_weights

**Files:**
- Modify: `src/trae_dashboard/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: 写失败测试**

替换 `tests/test_storage.py` 中所有 `display_weights` / `included_model_names` 相关测试。新测试文件关键测试(完整替换文件):

```python
"""Tests for trae_dashboard.storage."""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from trae_dashboard.storage import Storage, ModelUsage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    s.init()
    return s


def test_init_creates_tables(storage: Storage):
    conn = storage.conn
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "accounts" in tables
    assert "model_usage" in tables
    assert "snapshots" in tables


def test_model_usage_has_amount_columns(storage: Storage):
    cols = {r["name"] for r in storage.conn.execute("PRAGMA table_info(model_usage)")}
    assert "amount_total" in cols
    assert "amount_basic" in cols
    assert "amount_pay_go" in cols
    assert "currency" in cols
    # token columns retained for tooltip
    assert "input_tokens" in cols
    assert "output_tokens" in cols


def test_upsert_and_read_model_usage(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=200,
        amount_total=12.34, amount_basic=10.0, amount_pay_go=2.34,
        currency="CNY",
    )
    rows = storage.get_model_usage_for_account("a@x.com", "2026-07-10")
    assert len(rows) == 1
    r = rows[0]
    assert r.model_name == "GLM-5.1"
    assert r.amount_total == pytest.approx(12.34)
    assert r.amount_basic == pytest.approx(10.0)
    assert r.amount_pay_go == pytest.approx(2.34)
    assert r.currency == "CNY"
    assert r.input_tokens == 100
    assert r.output_tokens == 200


def test_get_model_usage_by_account_returns_amount(storage: Storage):
    storage.upsert_account("a@x.com", "Alice")
    storage.upsert_account("b@x.com", "Bob")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        amount_total=50.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        amount_total=30.0,
    )
    rows = storage.get_model_usage_by_account("2026-07-10", "2026-08-09")
    assert len(rows) == 2
    # Sorted by amount_total DESC
    assert rows[0]["email"] == "a@x.com"
    assert rows[0]["amount_total"] == pytest.approx(50.0)
    assert rows[1]["email"] == "b@x.com"
    assert rows[1]["amount_total"] == pytest.approx(30.0)
    # Each row should include per-model details
    assert "models" in rows[0]
    assert rows[0]["models"][0]["amount_total"] == pytest.approx(50.0)


def test_get_total_amount(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_account("b@x.com", "B")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=50.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M2", model_source="Trae", amount_total=30.0,
    )
    total = storage.get_total_amount("2026-07-10")
    assert total == pytest.approx(80.0)


def test_get_total_amount_excludes_disabled_accounts(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_account("b@x.com", "B")
    # Disable b via direct SQL (upsert_account doesn't take enabled param in current code)
    storage.conn.execute("UPDATE accounts SET enabled=0 WHERE email='b@x.com'")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=50.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M2", model_source="Trae", amount_total=30.0,
    )
    total = storage.get_total_amount("2026-07-10")
    assert total == pytest.approx(50.0)


def test_prune_zero_data_accounts_uses_amount(storage: Storage):
    storage.upsert_account("keep@x.com", "Keep")
    storage.upsert_account("zero@x.com", "Zero")
    storage.upsert_model_usage(
        email="keep@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=10.0,
        input_tokens=0, output_tokens=0,  # token 为零但 amount>0,不应被删
    )
    storage.upsert_model_usage(
        email="zero@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=0.0,
        input_tokens=100, output_tokens=200,  # token 非零但 amount=0,应被删
    )
    deleted = storage.prune_zero_data_accounts()
    assert deleted == {"deleted_accounts": 1, "deleted_model_rows": 1}
    remaining = {a.email for a in storage.list_accounts()}
    assert remaining == {"keep@x.com"}


def test_migration_clears_old_data(tmp_path: Path):
    """Simulate legacy schema (no amount columns) and verify migration clears rows."""
    db_path = tmp_path / "legacy.db"
    # Create old-style table without amount columns
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE accounts (
          email TEXT PRIMARY KEY, display_name TEXT, enabled INTEGER DEFAULT 1
        );
        CREATE TABLE model_usage (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT NOT NULL,
          cycle_start TEXT NOT NULL,
          cycle_end TEXT NOT NULL,
          model_name TEXT NOT NULL,
          model_type TEXT,
          model_source TEXT,
          input_tokens INTEGER DEFAULT 0,
          output_tokens INTEGER DEFAULT 0,
          fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(email, cycle_start, model_name)
        );
        INSERT INTO accounts VALUES ('a@x.com', 'A', 1);
        INSERT INTO model_usage (email, cycle_start, cycle_end, model_name, model_source, input_tokens, output_tokens)
          VALUES ('a@x.com', '2026-07-10', '2026-08-09', 'OLD', 'Trae', 999, 999);
        """
    )
    conn.commit()
    conn.close()

    # Now open with Storage — triggers migration
    s = Storage(db_path)
    s.init()
    rows = s.conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
    assert rows == 0  # old data cleared
    # New columns exist
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(model_usage)")}
    assert "amount_total" in cols
    s.close()


def test_upsert_model_usage_replaces_on_conflict(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=10.0,
    )
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=20.0,
    )
    rows = storage.get_model_usage_for_account("a@x.com", "2026-07-10")
    assert len(rows) == 1
    assert rows[0].amount_total == pytest.approx(20.0)
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_storage.py -v`
Expected: FAIL with `TypeError` (display_weights 仍存在) 或 `AttributeError` (缺少 amount_total 参数)

- [ ] **Step 3: 编辑 storage.py**

基于现有 `src/trae_dashboard/storage.py` 做**精准修改**(保留 `delete_account`、`latest_snapshot`、`prune_orphan_model_usage`、`prune_old_snapshots`、`__enter__`/`__exit__`、连接参数 `isolation_level=None, check_same_thread=False` 等)。`prune_zero_data_accounts` 返回 **dict** `{"deleted_accounts": int, "deleted_model_rows": int}` 以保持 cli.py 契约不变。

**3a. 删除模块顶部的 `_model_filter_sql` 函数**(原文件 21-39 行整块删除)。

**3b. 修改 SCHEMA 常量**(原文件 41-74 行),在 model_usage 表的 `output_tokens` 行后新增 amount 列:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  email TEXT PRIMARY KEY,
  display_name TEXT,
  enabled INTEGER DEFAULT 1,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  start_time INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  request_meta TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON snapshots(fetched_at);
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  cycle_start TEXT NOT NULL,
  cycle_end TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_type TEXT,
  model_source TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  amount_total REAL DEFAULT 0,
  amount_basic REAL DEFAULT 0,
  amount_pay_go REAL DEFAULT 0,
  currency TEXT DEFAULT 'CNY',
  fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(email, cycle_start, model_name),
  FOREIGN KEY (email) REFERENCES accounts(email)
);
CREATE INDEX IF NOT EXISTS idx_model_usage_email ON model_usage(email);
CREATE INDEX IF NOT EXISTS idx_model_usage_cycle ON model_usage(cycle_start, cycle_end);
"""
```

**3c. 扩展 `ModelUsage` dataclass**(原文件 84-93 行),新增 amount 字段:

```python
@dataclass
class ModelUsage:
    email: str
    cycle_start: str
    cycle_end: str
    model_name: str
    model_type: str | None
    model_source: str | None
    input_tokens: int
    output_tokens: int
    amount_total: float
    amount_basic: float
    amount_pay_go: float
    currency: str
```

**3d. 修改 `Storage.__init__`**(原文件 96-129 行),移除 `display_weights` 参数与字段(保留连接参数与 PRAGMA):

```python
class Storage:
    def __init__(
        self,
        db_path: str | Path,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.row_factory = sqlite3.Row
```

**3e. 修改 `init` 方法**(原文件 131-132 行),新增迁移逻辑(检测到旧 schema 时先清空 model_usage 再加列):

```python
    def init(self) -> None:
        self.conn.executescript(SCHEMA)
        # Migration: add amount columns if missing (clear old data first
        # — old rows lack amount values and would skew totals).
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(model_usage)")}
        if "amount_total" not in cols:
            self.conn.execute("DELETE FROM model_usage")
            for col, decl in [
                ("amount_total", "REAL DEFAULT 0"),
                ("amount_basic", "REAL DEFAULT 0"),
                ("amount_pay_go", "REAL DEFAULT 0"),
                ("currency", "TEXT DEFAULT 'CNY'"),
            ]:
                self.conn.execute(f"ALTER TABLE model_usage ADD COLUMN {col} {decl}")
```

保留 `close`/`__enter__`/`__exit__`/`upsert_account`/`delete_account`/`list_accounts`/`save_snapshot`/`latest_snapshot` **不动**。

**3f. 修改 `upsert_model_usage`**(原文件 245-281 行),新增 amount 参数与列:

```python
    def upsert_model_usage(
        self,
        *,
        email: str,
        cycle_start: str,
        cycle_end: str,
        model_name: str,
        model_type: str | None,
        model_source: str | None,
        input_tokens: int,
        output_tokens: int,
        amount_total: float = 0.0,
        amount_basic: float = 0.0,
        amount_pay_go: float = 0.0,
        currency: str = "CNY",
    ) -> None:
        """Write one row per (email, cycle_start, model_name).

        UNIQUE on (email, cycle_start, model_name) means re-fetching the
        same cycle window **overwrites** the old row (no accumulation).
        """
        self.conn.execute(
            "INSERT INTO model_usage(email, cycle_start, cycle_end, model_name, "
            "model_type, model_source, input_tokens, output_tokens, "
            "amount_total, amount_basic, amount_pay_go, currency) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(email, cycle_start, model_name) DO UPDATE SET "
            "cycle_end=excluded.cycle_end, "
            "model_type=excluded.model_type, model_source=excluded.model_source, "
            "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
            "amount_total=excluded.amount_total, amount_basic=excluded.amount_basic, "
            "amount_pay_go=excluded.amount_pay_go, currency=excluded.currency, "
            "fetched_at=CURRENT_TIMESTAMP",
            (
                email, cycle_start, cycle_end, model_name, model_type, model_source,
                input_tokens, output_tokens,
                amount_total, amount_basic, amount_pay_go, currency,
            ),
        )
```

**3g. 修改 `get_model_usage_by_account`**(原文件 283-373 行),移除 `included_model_names` 参数和 display_weights CASE 表达式,改用 amount_total 聚合与排序,并附带 `models` 明细数组(供 tooltip):

```python
    def get_model_usage_by_account(
        self, cycle_start: str, cycle_end: str
    ) -> list[dict]:
        """Per-account totals for a cycle.

        `cycle_end` is accepted for caller context, but rows are matched by
        `cycle_start` only (a row's stored cycle_end is the fetch cutoff).

        Returns list of dicts:
          { email, display_name, amount_total, input_tokens, output_tokens,
            model_count, models: [...] }
        Accounts with no model_usage rows are included with zeros.
        Only enabled accounts are listed. Sorted by amount_total desc.
        """
        rows = self.conn.execute(
            "SELECT a.email, a.display_name, "
            "COALESCE(SUM(m.amount_total), 0) AS amount_total, "
            "COALESCE(SUM(m.input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(m.output_tokens), 0) AS output_tokens, "
            "COUNT(m.model_name) AS model_count "
            "FROM accounts a "
            "LEFT JOIN model_usage m "
            "  ON a.email = m.email "
            "  AND m.cycle_start = ? "
            "WHERE a.enabled = 1 "
            "GROUP BY a.email, a.display_name",
            (cycle_start,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            models = self.conn.execute(
                "SELECT model_name, model_type, model_source, input_tokens, output_tokens, "
                "amount_total, amount_basic, amount_pay_go, currency "
                "FROM model_usage WHERE email=? AND cycle_start=? "
                "ORDER BY amount_total DESC",
                (r["email"], cycle_start),
            ).fetchall()
            out.append({
                "email": r["email"],
                "display_name": r["display_name"],
                "amount_total": float(r["amount_total"] or 0),
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "model_count": r["model_count"],
                "models": [dict(m) for m in models],
            })
        out.sort(key=lambda r: (-r["amount_total"], r["email"]))
        return out
```

**3h. 修改 `get_model_usage_for_account`**(原文件 375-422 行),移除 `included_model_names` 参数和 display_weights 逻辑,返回含 amount 字段的 ModelUsage:

```python
    def get_model_usage_for_account(
        self, email: str, cycle_start: str
    ) -> list[ModelUsage]:
        """Per-model breakdown for one account in the given cycle.

        Returns one row per model, sorted by amount_total desc.
        """
        rows = self.conn.execute(
            "SELECT email, cycle_start, cycle_end, model_name, model_type, "
            "model_source, input_tokens, output_tokens, "
            "amount_total, amount_basic, amount_pay_go, currency "
            "FROM model_usage WHERE email=? AND cycle_start=? "
            "ORDER BY amount_total DESC, model_name",
            (email, cycle_start),
        ).fetchall()
        out: list[ModelUsage] = []
        for r in rows:
            out.append(
                ModelUsage(
                    r["email"], r["cycle_start"], r["cycle_end"],
                    r["model_name"], r["model_type"], r["model_source"],
                    int(r["input_tokens"] or 0), int(r["output_tokens"] or 0),
                    float(r["amount_total"] or 0), float(r["amount_basic"] or 0),
                    float(r["amount_pay_go"] or 0), r["currency"] or "CNY",
                )
            )
        return out
```

**3i. 修改 `prune_zero_data_accounts`**(原文件 428-467 行),判断依据从 `SUM(input_tokens + output_tokens) == 0` 改为 `SUM(amount_total) == 0`。**保持返回 dict 形状不变**(cli.py 依赖 `deleted_accounts`/`deleted_model_rows` 键):

```python
    def prune_zero_data_accounts(self) -> dict:
        """Delete accounts whose total amount_total is zero.

        Cascade-deletes their model_usage rows.
        Returns: {deleted_accounts, deleted_model_rows}.
        """
        rows = self.conn.execute(
            "SELECT a.email, "
            "COALESCE(SUM(m.amount_total), 0) AS total "
            "FROM accounts a LEFT JOIN model_usage m ON a.email = m.email "
            "GROUP BY a.email"
        ).fetchall()
        zero_emails = [r["email"] for r in rows if (r["total"] or 0) == 0]
        if not zero_emails:
            return {"deleted_accounts": 0, "deleted_model_rows": 0}

        placeholders = ",".join("?" * len(zero_emails))
        model_count = self.conn.execute(
            f"SELECT COUNT(*) FROM model_usage WHERE email IN ({placeholders})",
            zero_emails,
        ).fetchone()[0]

        self.conn.execute(
            f"DELETE FROM model_usage WHERE email IN ({placeholders})",
            zero_emails,
        )
        self.conn.execute(
            f"DELETE FROM accounts WHERE email IN ({placeholders})",
            zero_emails,
        )

        return {
            "deleted_accounts": len(zero_emails),
            "deleted_model_rows": model_count,
        }
```

**3j. 新增 `get_total_amount` 方法**(放在 `get_model_usage_by_account` 之后):

```python
    def get_total_amount(self, cycle_start: str) -> float:
        """当前周期所有启用账号的金额消耗总和."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(m.amount_total), 0) AS total "
            "FROM model_usage m JOIN accounts a ON a.email = m.email "
            "WHERE m.cycle_start=? AND a.enabled=1",
            (cycle_start,),
        ).fetchone()
        return float(row["total"] or 0)
```

保留 `prune_orphan_model_usage` 和 `prune_old_snapshots` **不动**。

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trae_dashboard/storage.py tests/test_storage.py
git commit -m "refactor(storage): add amount columns, drop display_weights, prune by amount"
```

---

## Task 3: collector.py — model_source 过滤 + amount 采集

**Files:**
- Modify: `src/trae_dashboard/collector.py`
- Test: `tests/test_collector.py`

**保持不变:** `Collector.__init__` 签名 `(client, storage, config)`, `run_once` 的返回 dict 形状 `{"snapshots", "users", "snapshot_id", "cycle_start", "cycle_end"}` (api.py /api/refresh 依赖), `self._client.get_model_usage(emails=, start=, end=)` 调用方式, `save_snapshot` 调用, cycle window 计算。

**移除:** `self._canonical` 映射表。
**变更:** 过滤从 canonical 查表改为 `model_source == "Trae"`;新增 amount 字段采集与 upsert。

- [ ] **Step 1: 写失败测试**

完整替换 `tests/test_collector.py`:

```python
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
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_collector.py -v`
Expected: FAIL with `AttributeError` (Collector.__init__ 仍构建 _canonical,引用 config.included_model_names)

- [ ] **Step 3: 重写 collector.py**

`src/trae_dashboard/collector.py` 完整新内容(保留原返回形状与 client 调用方式):

```python
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
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_collector.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trae_dashboard/collector.py tests/test_collector.py
git commit -m "refactor(collector): filter by model_source, collect amount fields"
```

---

## Task 4: api.py — /api/status 与 /api/accounts 改金额

**Files:**
- Modify: `src/trae_dashboard/api.py`
- Test: `tests/test_api.py`

**保持不变:** 所有 email/report/eml/accounts CRUD 端点, `_parse_iso8601`, `AccountIn`/`ReportIn`/`RecipientsIn`/`SmtpIn`/`PasswordIn` 模型, `create_app` 签名, 静态文件挂载。

**变更:**
- `status()`: `total_consumed` 改为 `storage.get_total_amount(start_date)`;`accounts_with_data` 改为基于 `amount_total > 0`;`total_quota = per_account_quota * len(rows)`(per_account_quota 现在是 float 120.0)。
- `list_accounts()`: 移除 `cfg.included_model_names` 传参;`consumed` 改为 `amount_total`;`quota_pct` 基于 amount;`models` 数组项改为含 `amount_total`/`amount_basic`/`amount_pay_go`。
- `account_history()`: 移除 `cfg.included_model_names` 传参;返回行新增 amount 字段。

- [ ] **Step 1: 写失败测试**

完整替换 `tests/test_api.py`(保留 email 端点测试,仅修改 status/accounts/history 相关测试):

```python
"""Tests for trae_dashboard.api."""
from __future__ import annotations
import pytest
from datetime import datetime, timezone

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    from starlette.testclient import TestClient  # type: ignore

from trae_dashboard.api import create_app
from trae_dashboard.storage import Storage
from trae_dashboard.config import Config, Account


def _cfg(**kw) -> Config:
    defaults = dict(openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[])
    defaults.update(kw)
    return Config(**defaults)


def _seed_cycle(storage: Storage, email: str, *, amount_total=0.0, input_tokens=0, output_tokens=0, model_name="M"):
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    storage.upsert_account(email, email.split("@")[0])
    storage.upsert_model_usage(
        email=email, cycle_start=s_dt.date().isoformat(), cycle_end=e_dt.date().isoformat(),
        model_name=model_name, model_type="Chat", model_source="Trae",
        input_tokens=input_tokens, output_tokens=output_tokens,
        amount_total=amount_total,
    )


def test_api_accounts_returns_amount(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    _seed_cycle(s, "a@x.com", amount_total=50.0, input_tokens=10, output_tokens=20)
    cfg = _cfg(accounts=[Account("a@x.com", "A")], per_account_quota=120.0)
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts")
        assert r.status_code == 200
        data = r.json()
    assert len(data) == 1
    assert data[0]["email"] == "a@x.com"
    assert data[0]["amount_total"] == pytest.approx(50.0)
    assert data[0]["per_account_quota"] == 120.0
    assert data[0]["quota_used_pct"] == pytest.approx(41.67, abs=0.01)
    # per-model breakdown includes amount_total
    assert data[0]["models"][0]["amount_total"] == pytest.approx(50.0)


def test_api_account_history_returns_amount(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    _seed_cycle(s, "a@x.com", amount_total=30.0)
    cfg = _cfg(accounts=[Account("a@x.com")])
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts/a@x.com/history")
        assert r.status_code == 200
        items = r.json()
    assert len(items) == 1
    assert items[0]["amount_total"] == pytest.approx(30.0)


def test_api_health(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _cfg()
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_api_status_returns_amount_fields(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    _seed_cycle(s, "a@x.com", amount_total=50.0)
    cfg = _cfg(accounts=[Account("a@x.com")], per_account_quota=120.0)
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200, r.text
        body = r.json()
    for key in ("ok", "last_fetched_at", "seconds_since_fetch",
                "total_accounts", "accounts_with_data",
                "total_quota", "total_consumed", "total_remaining", "utilization_pct"):
        assert key in body
    assert "db_path" not in body
    assert body["total_accounts"] == 1
    assert body["accounts_with_data"] == 1
    assert body["total_consumed"] == pytest.approx(50.0)
    assert body["total_quota"] == pytest.approx(120.0)
    assert body["total_remaining"] == pytest.approx(70.0)
    assert body["utilization_pct"] == pytest.approx(41.67, abs=0.01)


def test_api_status_excludes_zero_amount_accounts(tmp_data_dir):
    """accounts_with_data excludes zero-amount accounts."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    _seed_cycle(s, "real@x.com", amount_total=50.0)
    _seed_cycle(s, "zero@x.com", amount_total=0.0, input_tokens=10, output_tokens=20)
    cfg = _cfg(accounts=[Account("real@x.com"), Account("zero@x.com")], per_account_quota=120.0)
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        body = r.json()
    assert body["total_accounts"] == 2
    assert body["accounts_with_data"] == 1
    assert body["total_consumed"] == pytest.approx(50.0)
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL with `TypeError` (get_model_usage_by_account 仍需 included_model_names 参数) 或 `AttributeError` (consumed 字段不存在)

- [ ] **Step 3: 编辑 api.py**

**3a. 修改 `status()` 函数**(原文件 208-283 行)。将 `total_consumed` 计算改为 `storage.get_total_amount()`,`accounts_with_data` 改为基于 amount_total:

```python
    @app.get("/api/status")
    def status():
        start_dt, _ = current_cycle_window()
        cycle_end_dt = next_cycle_reset()
        start_date = start_dt.date().isoformat()
        end_date = cycle_end_dt.date().isoformat()
        rows = storage.get_model_usage_by_account(start_date, end_date)
        total_consumed = storage.get_total_amount(start_date)
        accounts_with_data = sum(
            1 for r in rows if (r["amount_total"] or 0) > 0
        )
        per_account_quota = cfg.per_account_quota
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
```

**3b. 修改 `list_accounts()` 函数**(原文件 285-330 行)。移除 `cfg.included_model_names`,改用 amount_total:

```python
    @app.get("/api/accounts")
    def list_accounts():
        start_dt, end_dt = current_cycle_window()
        start_date = start_dt.date().isoformat()
        end_date = end_dt.date().isoformat()
        rows = storage.get_model_usage_by_account(start_date, end_date)
        per_q = cfg.per_account_quota
        result = []
        for r in rows:
            amount = float(r["amount_total"] or 0)
            quota_pct = round((amount / per_q) * 100, 2) if per_q > 0 else 0.0
            models_rows = storage.get_model_usage_for_account(r["email"], start_date)
            models = [
                {
                    "name": m.model_name,
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                    "amount_total": m.amount_total,
                    "amount_basic": m.amount_basic,
                    "amount_pay_go": m.amount_pay_go,
                }
                for m in models_rows
            ]
            result.append(
                {
                    "email": r["email"],
                    "display_name": r["display_name"],
                    "amount_total": amount,
                    "input_tokens": r["input_tokens"] or 0,
                    "output_tokens": r["output_tokens"] or 0,
                    "model_count": r["model_count"] or 0,
                    "per_account_quota": per_q,
                    "quota_used_pct": quota_pct,
                    "models": models,
                }
            )
        return result
```

**3c. 修改 `account_history()` 函数**(原文件 427-446 行)。移除 `cfg.included_model_names`,返回 amount 字段:

```python
    @app.get("/api/accounts/{email}/history")
    def account_history(email: str):
        start_dt, _ = current_cycle_window()
        cycle_start = start_dt.date().isoformat()
        rows = storage.get_model_usage_for_account(email, cycle_start)
        return [
            {
                "cycle_start": r.cycle_start,
                "cycle_end": r.cycle_end,
                "model_name": r.model_name,
                "model_type": r.model_type,
                "model_source": r.model_source,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "amount_total": r.amount_total,
                "amount_basic": r.amount_basic,
                "amount_pay_go": r.amount_pay_go,
                "currency": r.currency,
            }
            for r in rows
        ]
```

保留所有其他端点(email/report/eml/refresh/accounts CRUD)**不动**。

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trae_dashboard/api.py tests/test_api.py
git commit -m "refactor(api): switch status/accounts to amount-based metrics"
```

---

## Task 5: report.py — 金额邮件报告

**Files:**
- Modify: `src/trae_dashboard/report.py`
- Test: `tests/test_report.py`

**保持不变:** `send_email`, `_build_message`, `build_eml`, `run_report`(除 collect_report_rows 调用), `_esc`, `_quota_color`, EmailConfig 导入。

**变更:** `ReportRow` 改为金额字段;新增 `_fmt_cny`;`collect_report_rows` 用 amount;`render_html` 表格/KPI 用 ¥。

- [ ] **Step 1: 写失败测试**

完整替换 `tests/test_report.py`:

```python
"""Tests for the daily email report renderer + SMTP config parser."""
from __future__ import annotations

import email
import email.parser
import email.policy
from pathlib import Path

import pytest

from trae_dashboard.config import Config, _load_email_config
from trae_dashboard.report import (
    ReportRow,
    _esc,
    _fmt_cny,
    _quota_color,
    collect_report_rows,
    render_html,
)
from trae_dashboard.storage import Storage


def _seed_storage(storage: Storage) -> None:
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    cycle_start = s_dt.date().isoformat()
    cycle_end = e_dt.date().isoformat()
    storage.upsert_account("a@x.com", "Alice")
    storage.upsert_account("b@x.com", "Bob")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=32_000_000, output_tokens=180_000,
        amount_total=80.0, amount_basic=60.0, amount_pay_go=20.0,
    )
    storage.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="Doubao-Seed-Code", model_type="Chat", model_source="Trae",
        input_tokens=8_000_000, output_tokens=100_000,
        amount_total=20.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="GLM-5-Turbo", model_type="Chat", model_source="Trae",
        input_tokens=8_000_000, output_tokens=80_000,
        amount_total=30.0,
    )


def _make_config(per_account_quota: float = 120.0) -> Config:
    return Config(
        openapi_base="https://example", auth_endpoint="/token",
        app_id="x", app_secret="x", accounts=[],
        per_account_quota=per_account_quota,
    )


# ---------- config parsing (unchanged) ----------

class TestEmailConfigParsing:
    def test_missing_section_returns_disabled(self):
        cfg = _load_email_config({})
        assert cfg.enabled is False
        assert cfg.smtp_host == ""

    def test_disabled_section_is_ok(self):
        cfg = _load_email_config({"email": {"enabled": False}})
        assert cfg.enabled is False

    def test_enabled_requires_required_fields(self):
        with pytest.raises(RuntimeError, match="missing required field"):
            _load_email_config({"email": {"enabled": True, "smtp_user": "u@x.com",
                "from_addr": "u@x.com", "recipients": ["r@x.com"]}})

    def test_enabled_requires_recipients(self):
        with pytest.raises(RuntimeError, match="recipients.*empty"):
            _load_email_config({"email": {"enabled": True, "smtp_host": "smtp.x.com",
                "smtp_user": "u@x.com", "from_addr": "u@x.com", "recipients": []}})

    def test_full_config_parses(self):
        cfg = _load_email_config({"email": {"enabled": True, "smtp_host": "smtp.qq.com",
            "smtp_port": 465, "smtp_user": "me@qq.com", "smtp_password_env": "SMTP_PASSWORD",
            "from_addr": "me@qq.com", "recipients": ["a@x.com", "b@x.com"], "send_time": "09:00"}})
        assert cfg.enabled is True
        assert cfg.smtp_host == "smtp.qq.com"
        assert cfg.recipients == ["a@x.com", "b@x.com"]


# ---------- pure helpers ----------

class TestFmtCny:
    def test_basic(self):
        assert _fmt_cny(1234.5) == "¥ 1,234.50"

    def test_zero(self):
        assert _fmt_cny(0) == "¥ 0.00"

    def test_large(self):
        assert _fmt_cny(1234567.89) == "¥ 1,234,567.89"


class TestQuotaColor:
    def test_high(self):
        assert _quota_color(95.0) == "#dc2626"
    def test_mid(self):
        assert _quota_color(75.0) == "#d97706"
    def test_low(self):
        assert _quota_color(50.0) == "#16a34a"


class TestEsc:
    def test_html_chars(self):
        assert _esc('a<b>&c"d') == "a&lt;b&gt;&amp;c&quot;d"


# ---------- data collection ----------

class TestCollectReportRows:
    def test_collects_per_account_rows(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        assert len(rows) == 2
        # Sorted by amount_total desc — Alice (100) > Bob (30)
        assert rows[0].email == "a@x.com"
        assert rows[1].email == "b@x.com"

    def test_amount_total_aggregated(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        alice = next(r for r in rows if r.email == "a@x.com")
        # GLM 80 + Doubao 20 = 100
        assert alice.amount_total == pytest.approx(100.0)
        # Top model is GLM-5.1 (80 > 20)
        assert alice.top_model == "GLM-5.1"
        assert alice.top_model_amount == pytest.approx(80.0)

    def test_quota_pct(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config(per_account_quota=200.0)
        rows = collect_report_rows(s, cfg)
        alice = next(r for r in rows if r.email == "a@x.com")
        # 100 / 200 = 50.0%
        assert alice.quota_pct == pytest.approx(50.0)

    def test_empty_db_returns_empty(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        assert rows == []


# ---------- HTML rendering ----------

class TestRenderHtml:
    def _sample_rows(self) -> list[ReportRow]:
        return [
            ReportRow(display_name="Alice", email="a@x.com",
                amount_total=100.0, quota_pct=83.3,
                top_model="GLM-5.1", top_model_amount=80.0,
                input_tokens=32_000_000, output_tokens=180_000),
            ReportRow(display_name="Bob", email="b@x.com",
                amount_total=30.0, quota_pct=25.0,
                top_model="GLM-5-Turbo", top_model_amount=30.0,
                input_tokens=8_000_000, output_tokens=80_000),
        ]

    def test_contains_per_account_data(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        assert "Alice" in html
        assert "a@x.com" in html
        assert "GLM-5.1" in html

    def test_total_consumed_in_kpi_uses_cny(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        # Total = 100 + 30 = 130 -> "¥ 130.00"
        assert "¥ 130.00" in html

    def test_total_quota_uses_row_count(self):
        from datetime import datetime, timezone
        cfg = _make_config(per_account_quota=120.0)
        rows = [ReportRow(f"u{i}@x.com", f"u{i}@x.com", 0.0, 0.0, "—", 0.0, 0, 0)
                for i in range(13)]
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html(rows, cfg, start, now)
        # 13 × 120 = 1560 -> "¥ 1,560.00"
        assert "¥ 1,560.00" in html

    def test_empty_rows_renders_placeholder(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html([], cfg, start, now)
        assert "本周期暂无用量数据" in html

    def test_html_escapes_user_content(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        rows = [ReportRow("<script>alert(1)</script>", "x@x.com",
            1.0, 0.0, "M", 1.0, 0, 0)]
        html = render_html(rows, cfg, start, now)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


def test_build_eml_has_headers_and_html():
    from datetime import datetime, timezone
    from trae_dashboard.config import EmailConfig
    from trae_dashboard.report import build_eml
    cfg = EmailConfig(enabled=True, smtp_host="smtp.x.com", smtp_port=465,
        smtp_user="me@x.com", from_addr="me@x.com", recipients=["a@x.com", "b@x.com"])
    raw = build_eml(cfg, subject="[Trae Dashboard] 测试", html_body="<p>hello</p>")
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    assert msg["From"] == "me@x.com"
    assert msg["To"] == "a@x.com, b@x.com"
    assert msg["Subject"] == "[Trae Dashboard] 测试"
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_report.py -v`
Expected: FAIL with `ImportError` (无法导入 `_fmt_cny`) 或 `AttributeError` (ReportRow 无 amount_total 字段)

- [ ] **Step 3: 编辑 report.py**

**3a. 修改 `ReportRow` dataclass + 新增 `_fmt_cny`**(原文件 38-61 行),替换为:

```python
@dataclass
class ReportRow:
    """One row in the email table — amount-based, ready to render."""

    display_name: str
    email: str
    amount_total: float
    quota_pct: float
    top_model: str
    top_model_amount: float
    input_tokens: int
    output_tokens: int


def _fmt_cny(n: float) -> str:
    """Format a CNY amount: 1234.5 -> '¥ 1,234.50'."""
    return "¥ {:,.2f}".format(float(n or 0))
```

(删除原 `_fmt_tokens` 函数,保留 `_quota_color` 和 `_esc` 不动。)

**3b. 修改 `collect_report_rows`**(原文件 83-130 行),改用 amount:

```python
def collect_report_rows(storage: Storage, cfg: Config) -> list[ReportRow]:
    start_dt, _ = current_cycle_window()
    start_date = start_dt.date().isoformat()
    end_date = datetime.now(timezone.utc).date().isoformat()
    per_q = cfg.per_account_quota

    accounts = storage.get_model_usage_by_account(start_date, end_date)
    rows: list[ReportRow] = []
    for a in accounts:
        email = a["email"]
        amount = float(a["amount_total"] or 0)
        pct = round((amount / per_q) * 100, 1) if per_q > 0 else 0.0

        models = storage.get_model_usage_for_account(email, start_date)
        if models:
            top = max(models, key=lambda m: m.amount_total)
            top_name = top.model_name
            top_amount = top.amount_total
        else:
            top_name = "—"
            top_amount = 0.0

        rows.append(
            ReportRow(
                display_name=a["display_name"] or email.split("@")[0],
                email=email,
                amount_total=amount,
                quota_pct=pct,
                top_model=top_name,
                top_model_amount=top_amount,
                input_tokens=int(a["input_tokens"] or 0),
                output_tokens=int(a["output_tokens"] or 0),
            )
        )
    return rows
```

**3c. 修改 `render_html`**(原文件 133-251 行),KPI 与表格改用 `_fmt_cny`,移除 Input/Output 列改为 Token 辅助列:

```python
def render_html(
    rows: list[ReportRow],
    cfg: Config,
    cycle_start: datetime,
    now: datetime,
) -> str:
    total_consumed = sum(r.amount_total for r in rows)
    total_quota = cfg.per_account_quota * len(rows)
    total_pct = (
        round((total_consumed / total_quota) * 100, 1) if total_quota > 0 else 0.0
    )

    cycle_str = f"{cycle_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')}"

    table_rows_html = []
    for r in rows:
        color = _quota_color(r.quota_pct)
        table_rows_html.append(f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;">
            <div style="font-weight:600;color:#111827;">{_esc(r.display_name)}</div>
            <div style="font-size:12px;color:#6b7280;">{_esc(r.email)}</div>
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-variant-numeric:tabular-nums;color:#111827;">
            {_fmt_cny(r.amount_total)}
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-variant-numeric:tabular-nums;color:{color};font-weight:600;">
            {r.quota_pct:.1f}%
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#374151;">
            {_esc(r.top_model)}
            <span style="color:#9ca3af;">({_fmt_cny(r.top_model_amount)})</span>
          </td>
        </tr>""")

    rows_html = (
        "".join(table_rows_html)
        if table_rows_html
        else (
            '<tr><td colspan="4" style="padding:20px;text-align:center;color:#9ca3af;">'
            "本周期暂无用量数据</td></tr>"
        )
    )

    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Trae Dashboard 日报</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,'Microsoft YaHei','微软雅黑','PingFang SC','Hiragino Sans GB','Noto Sans CJK SC',sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="720" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.05);overflow:hidden;">
        <tr><td style="padding:24px 28px;background:#1e3a8a;">
          <div style="font-size:18px;font-weight:600;color:#ffffff;">Trae Dashboard · 周期消耗日报</div>
          <div style="font-size:13px;color:#bfdbfe;margin-top:4px;">{now.strftime('%Y-%m-%d %H:%M')} UTC · 计费周期 {cycle_str}</div>
        </td></tr>
        <tr><td style="padding:20px 28px 8px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="width:33%;padding-right:12px;">
                <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;">总消耗</div>
                <div style="font-size:22px;font-weight:600;color:#111827;margin-top:2px;">{_fmt_cny(total_consumed)}</div>
              </td>
              <td style="width:33%;padding-right:12px;">
                <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;">总配额</div>
                <div style="font-size:22px;font-weight:600;color:#111827;margin-top:2px;">{_fmt_cny(total_quota)}</div>
              </td>
              <td style="width:33%;">
                <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;">使用率</div>
                <div style="font-size:22px;font-weight:600;color:{_quota_color(total_pct)};margin-top:2px;">{total_pct:.1f}%</div>
              </td>
            </tr>
          </table>
        </td></tr>
        <tr><td style="padding:8px 20px 20px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
            <thead>
              <tr style="background:#f9fafb;">
                <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">账号</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">总消耗</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">配额占比</th>
                <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">Top 模型</th>
              </tr>
            </thead>
            <tbody>{rows_html}
            </tbody>
          </table>
        </td></tr>
        <tr><td style="padding:16px 28px;background:#f9fafb;border-top:1px solid #e5e7eb;">
          <div style="font-size:12px;color:#9ca3af;">
            本邮件由 Trae Dashboard 自动发送 · 数据采集自 Trae Enterprise OpenAPI
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
```

**3d. 修改 `run_report` 的返回 dict**(原文件 367-374 行),`total_consumed` 改为 amount sum:

```python
    return {
        "recipients": list(email_cfg.recipients),
        "recipient_count": len(email_cfg.recipients),
        "rows": len(rows),
        "total_consumed": round(sum(r.amount_total for r in rows), 2),
        "subject": subject,
        "html": html if not send else None,
    }
```

保留 `send_email`/`_build_message`/`build_eml` **不动**。

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_report.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trae_dashboard/report.py tests/test_report.py
git commit -m "refactor(report): switch to CNY amount-based email report"
```

---

## Task 6: config.example.yaml — 删除废弃配置

**Files:**
- Modify: `config.example.yaml`
- Test: 无(手动验证)

- [ ] **Step 1: 编辑 config.example.yaml**

**1a. 修改 `per_account_quota` 行**(原文件 20-22 行):

```yaml
# Per-account monthly quota in CNY (120 yuan default).
# Company total = per_account_quota * number of accounts.
# Only Trae built-in models (model_source == "Trae") are counted.
per_account_quota: 120.0
```

**1b. 删除 `included_model_names` 块**(原文件 24-48 行整块删除,含注释)。

**1c. 删除 `display_weights` 块**(原文件 50-57 行整块删除,含注释)。

保留 `openapi_base`/`auth_endpoint`/`app_id_env`/`app_secret_env`/`db_path`/`fetch_interval_minutes`/`accounts`/`email` 段**不动**。

- [ ] **Step 2: 验证 YAML 合法**

Run: `python -c "import yaml; yaml.safe_load(open('config.example.yaml', encoding='utf-8')); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 提交**

```bash
git add config.example.yaml
git commit -m "chore(config): drop whitelist/weights, set quota to 120.0 CNY"
```

---

## Task 7: cli.py — 移除 display_weights/included_model_names 引用

**Files:**
- Modify: `src/trae_dashboard/cli.py`
- Test: `tests/test_cli.py`

**保持不变:** 所有子命令定义(`init`/`fetch`/`serve`/`prune`/`report`), `_build_parser`, `main`, `_init`, `_serve`。

**变更:** `_fetch`/`_prune`/`_report` 中的 `Storage(cfg.db_path, display_weights=cfg.display_weights)` → `Storage(cfg.db_path)`;`_prune` dry-run 移除 `included_model_names` 传参并改用 amount;`_report` 的 `total_consumed` 打印改用 amount。

- [ ] **Step 1: 写失败测试**

完整替换 `tests/test_cli.py`(基于现有文件,移除 `included_model_names` 引用,改用 amount):

```python
"""Tests for trae_dashboard.cli subcommands."""
from __future__ import annotations
import pytest
import sqlite3
import subprocess
import sys
from pathlib import Path

from trae_dashboard.cli import main, _build_parser


def test_cli_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "init" in combined
    assert "fetch" in combined
    assert "serve" in combined


def test_parser_lists_subcommands():
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.cmd is None
    args = parser.parse_args(["init"])
    assert args.cmd == "init"
    args = parser.parse_args(["fetch", "--config", "x.yaml"])
    assert args.cmd == "fetch"
    assert args.config == "x.yaml"


def test_init_subcommand_writes_config(tmp_data_dir, monkeypatch):
    monkeypatch.chdir(tmp_data_dir)
    example = tmp_data_dir / "config.example.yaml"
    example.write_text("openapi_base: x\nauth_endpoint: /a\n", encoding="utf-8")
    target = tmp_data_dir / "config.yaml"
    main(["init", "--config", str(target)])
    assert target.exists()
    assert "openapi_base" in target.read_text(encoding="utf-8")


def test_init_subcommand_does_not_overwrite(tmp_data_dir, monkeypatch):
    monkeypatch.chdir(tmp_data_dir)
    example = tmp_data_dir / "config.example.yaml"
    example.write_text("openapi_base: from_example\n", encoding="utf-8")
    target = tmp_data_dir / "config.yaml"
    target.write_text("openapi_base: from_user\n", encoding="utf-8")
    main(["init", "--config", str(target)])
    assert "from_user" in target.read_text(encoding="utf-8")


def test_prune_command_removes_zero_amount_accounts(tmp_data_dir, monkeypatch):
    """prune removes accounts whose amount_total is zero."""
    monkeypatch.chdir(tmp_data_dir)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")

    cfg = tmp_data_dir / "config.yaml"
    cfg.write_text(
        "openapi_base: x\nauth_endpoint: /a\n"
        "app_id_env: TRAE_APP_ID\napp_secret_env: TRAE_APP_SECRET\n"
        "db_path: data/dashboard.db\n"
        "accounts:\n  - email: keep@x.com\n    display_name: Keep\n",
        encoding="utf-8",
    )

    from trae_dashboard.storage import Storage
    db_path = tmp_data_dir / "data" / "dashboard.db"
    s = Storage(db_path)
    s.init()
    s.upsert_account("keep@x.com", "Keep")
    s.upsert_account("zero1@x.com", "Zero1")
    s.upsert_account("zero2@x.com", "Zero2")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="keep@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        amount_total=10.0,
    )
    s.close()

    from io import StringIO
    from contextlib import redirect_stdout
    buf = StringIO()
    with redirect_stdout(buf):
        main(["prune", "--config", str(cfg), "--keep-snapshots", "5"])
    output = buf.getvalue()
    assert "deleted_accounts=2" in output
    assert "zero-data accounts: 2" in output

    s2 = Storage(db_path)
    emails = {a.email for a in s2.list_accounts()}
    assert emails == {"keep@x.com"}
    s2.close()


def test_prune_dry_run_does_not_delete(tmp_data_dir, monkeypatch):
    monkeypatch.chdir(tmp_data_dir)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")

    cfg = tmp_data_dir / "config.yaml"
    cfg.write_text(
        "openapi_base: x\nauth_endpoint: /a\n"
        "app_id_env: TRAE_APP_ID\napp_secret_env: TRAE_APP_SECRET\n"
        "db_path: data/dashboard.db\n"
        "accounts: []\n",
        encoding="utf-8",
    )

    from trae_dashboard.storage import Storage
    db_path = tmp_data_dir / "data" / "dashboard.db"
    s = Storage(db_path)
    s.init()
    s.upsert_account("zero@x.com", "Zero")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="zero@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        amount_total=0.0,
    )
    s.close()

    from io import StringIO
    from contextlib import redirect_stdout
    buf = StringIO()
    with redirect_stdout(buf):
        main(["prune", "--config", str(cfg), "--dry-run"])
    output = buf.getvalue()
    assert "[dry-run]" in output
    assert "zero-data accounts: 1" in output

    s2 = Storage(db_path)
    emails = {a.email for a in s2.list_accounts()}
    assert "zero@x.com" in emails
    s2.close()


def test_fetch_subcommand_writes_db(tmp_data_dir, monkeypatch):
    """fetch creates SQLite with snapshots and model_usage rows."""
    import httpx
    from trae_dashboard.client import TraeClient

    monkeypatch.chdir(tmp_data_dir)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "code": 0, "message": "ok", "request_id": "r",
                "data": {"items": [
                    {"email": "a@x.com", "model_usage": [
                        {"model_name": "GLM-5.1", "model_type": "Chat", "model_source": "Trae",
                         "usage": {"input_tokens": 9, "output_tokens": 18},
                         "amount": {"total_amount": 5.0}}
                    ]}
                ]},
            },
        )

    orig_init = TraeClient.__init__
    def patched_init(self, **kwargs):
        orig_init(self, **kwargs)
        mock = httpx.Client(transport=httpx.MockTransport(handler))
        self._client = mock
        if getattr(self, "_tokens", None) is not None:
            self._tokens._client = mock
    monkeypatch.setattr(TraeClient, "__init__", patched_init)

    target_cfg = tmp_data_dir / "config.yaml"
    target_cfg.write_text(
        "openapi_base: https://api.test\nauth_endpoint: /auth\n"
        "app_id_env: TRAE_APP_ID\napp_secret_env: TRAE_APP_SECRET\n"
        "db_path: data/dashboard.db\n"
        "accounts:\n  - email: a@x.com\n    display_name: A\n",
        encoding="utf-8",
    )

    main(["fetch", "--config", str(target_cfg)])

    db_path = tmp_data_dir / "data" / "dashboard.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        snap_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        model_count = conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
        account_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()
    assert snap_count >= 1
    assert model_count >= 1
    assert account_count >= 1
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL with `TypeError` (Storage 不接受 display_weights) 或 AttributeError (cfg.included_model_names 不存在)

- [ ] **Step 3: 编辑 cli.py**

**3a. 修改 `_fetch`**(原文件 119-127 行),移除 `display_weights`:

```python
def _fetch(config_path: Path) -> None:
    cfg = load_config(config_path)
    storage = Storage(cfg.db_path)
    storage.init()
    for a in cfg.accounts:
        storage.upsert_account(a.email, a.display_name)
    collector = make_collector(cfg, storage)
    result = collector.run_once()
    print(result)
```

**3b. 修改 `_prune`**(原文件 153-210 行),移除 `display_weights` 和 `included_model_names`,dry-run 改用 amount:

```python
def _prune(config_path: Path, *, keep_snapshots: int, dry_run: bool) -> None:
    """Clean up the SQLite DB.

    Operations (in order):
      1. Delete accounts whose model_usage amount_total totals are zero.
      2. Delete orphan model_usage rows (email no longer in accounts).
      3. Keep only the most recent ``keep_snapshots`` snapshots.
    """
    cfg = load_config(config_path)
    storage = Storage(cfg.db_path)
    storage.init()
    try:
        if dry_run:
            from .cycle import current_cycle_window

            s_dt, e_dt = current_cycle_window()
            rows = storage.get_model_usage_by_account(
                s_dt.date().isoformat(),
                e_dt.date().isoformat(),
            )
            n_zero = sum(1 for r in rows if (r["amount_total"] or 0) == 0)
            n_snap = storage.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            n_orphan = storage.conn.execute(
                "SELECT COUNT(*) FROM model_usage m "
                "WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.email = m.email)"
            ).fetchone()[0]
            print(
                f"[dry-run] zero-data accounts: {n_zero}; "
                f"orphan model rows: {n_orphan}; "
                f"snapshots total: {n_snap} "
                f"(would trim to keep_last={keep_snapshots})"
            )
            return

        zero_stats = storage.prune_zero_data_accounts()
        orphan_deleted = storage.prune_orphan_model_usage()
        old_snapshots_deleted = storage.prune_old_snapshots(keep_last=keep_snapshots)

        print(
            f"deleted_accounts={zero_stats['deleted_accounts']} "
            f"deleted_model_rows={zero_stats['deleted_model_rows']} "
            f"orphan_model_deleted={orphan_deleted} "
            f"old_snapshots_deleted={old_snapshots_deleted}"
        )
        print(
            f"zero-data accounts: {zero_stats['deleted_accounts']}; "
            f"orphan model rows: {orphan_deleted}; "
            f"old snapshots trimmed: {old_snapshots_deleted} (keep_last={keep_snapshots})"
        )
    finally:
        storage.close()
```

**3c. 修改 `_report`**(原文件 213-262 行),移除 `display_weights`,`total_consumed` 打印改用 amount(无需 `:,` 千分位因为现在是 float):

```python
def _report(config_path: Path, *, dry_run: bool = False) -> None:
    """Render and (optionally) send the daily email report."""
    from .report import collect_report_rows, render_html
    from .cycle import current_cycle_window
    from datetime import datetime, timezone

    cfg = load_config(config_path)
    storage = Storage(cfg.db_path)
    storage.init()
    try:
        rows = collect_report_rows(storage, cfg)
        now = datetime.now(timezone.utc)
        start_dt, _ = current_cycle_window()
        html = render_html(rows, cfg, start_dt, now)
        subject = f"[Trae Dashboard] 周期消耗日报 {now.strftime('%Y-%m-%d')}"

        if dry_run:
            print(f"Subject: {subject}")
            print(
                f"Recipients: {cfg.email.recipients or '(none — email not configured)'}"
            )
            print(f"Rows: {len(rows)}")
            print(f"Total consumed: ¥ {sum(r.amount_total for r in rows):.2f}")
            print("-" * 60)
            print(html)
            return

        if not cfg.email.enabled:
            print(
                "email report is disabled in config.yaml "
                "(email.enabled: false). "
                "Run with --dry-run to preview, or set email.enabled: true."
            )
            raise SystemExit(1)

        from .report import run_report
        summary = run_report(storage, cfg)
        print(
            f"sent: recipients={summary['recipient_count']} "
            f"rows={summary['rows']} "
            f"total_consumed=¥ {summary['total_consumed']:.2f} "
            f"subject={summary['subject']!r}"
        )
    finally:
        storage.close()
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: 运行全量测试,确认无回归**

Run: `python -m pytest tests/ -v`
Expected: PASS(所有后端测试通过)

- [ ] **Step 6: 提交**

```bash
git add src/trae_dashboard/cli.py tests/test_cli.py
git commit -m "refactor(cli): drop display_weights/included_model_names, use amount"
```

---

## Task 8: 前端 — index.html + app.js + style.css 改金额

**Files:**
- Modify: `src/trae_dashboard/static/app.js`
- Modify: `src/trae_dashboard/static/index.html`
- Modify: `src/trae_dashboard/static/style.css`
- Test: 无 JS 测试框架,**手动验证**(浏览器打开 + 检查 console)

**保持不变:** 主题切换、toast、分页、搜索、批量选择、模态框、所有事件绑定函数签名、所有 CSS 变量名。

**核心原则(per project memory):**
- 前端展示消耗统一为 CNY,格式 `¥ X,XXX.XX`
- 配额条使用单色填充(无 Input/Output 分段)
- 消耗列 tooltip 显示 per-model 用量明细(保留 token 字段供 tooltip)
- Dashboard 顶部 3 个 KPI 卡片高度紧凑(~140-150px),≤2px hairline 顶部边框,≤24px soft-color 图标
- 表头排序箭头绝对定位(right:6px),可排序表头有 `padding-right:26px`

**变更概览:**
- `app.js`:新增 `formatCNY`;`normalizeAccount` 改为读取 `amount_total`(保留 `consumed`/`input_tokens`/`output_tokens` 供 tooltip);`enrichStatus` 用 `amount_total` 汇总;`consumedFmt` 改用 `formatCNY`;`fetchAccounts` 返回结构不变。
- `index.html`:KPI 副标题"周期累计 · Input + Output"改"周期累计 · 金额";删除配额条的 Input/Output 双段填充 DOM 与图例;表格列 `Input`/`Output`/`消耗` 改为 `消耗(¥)` 单列(保留 tooltip 内的 per-model 表);CSV 导出列头 `Input,Output,消耗` 改为 `消耗(CNY)`。
- `style.css`:删除 `.quota-bar__fill-input` / `.quota-bar__fill-output` 双段样式;`.quota-bar__fill` 改为单色背景(用现有 `--color-status-active`);删除 `.quota-bar__legend-swatch--in/out` 引用。

- [ ] **Step 1: 编辑 `src/trae_dashboard/static/app.js`**

**1a. 新增 `formatCNY` 函数**(放在 `formatTokens` 之后,即原文件 43 行之后):

```javascript
  /**
   * Format a CNY amount: 1234.5 -> "¥ 1,234.50".
   * Used for all amount-based displays (KPI / quota bar / table / CSV).
   */
  function formatCNY(n) {
    n = Number(n) || 0;
    return "¥ " + n.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
```

**1b. 修改 `consumedFmt`**(原文件 411-413 行),改用 `formatCNY`:

```javascript
  function consumedFmt(n) {
    return formatCNY(n || 0);
  }
```

**1c. 修改 `normalizeAccount`**(原文件 467-489 行),主指标改用 `amount_total`,保留 `consumed`/`input_tokens`/`output_tokens` 供 tooltip:

```javascript
  function normalizeAccount(row) {
    const input = Number(row.input_tokens != null ? row.input_tokens : row.total_in) || 0;
    const output = Number(row.output_tokens != null ? row.output_tokens : row.total_out) || 0;
    // amount_total is the authoritative CNY metric from the new API.
    // Fall back to 0 when the backend doesn't provide it (legacy rows).
    const amountTotal = Number(row.amount_total) || 0;
    return {
      email: row.email,
      display_name: row.display_name,
      // Primary metric (CNY) — used by KPI / quota bar / table.
      amount_total: amountTotal,
      // Token fields retained for the per-model tooltip breakdown only.
      consumed: Number(row.consumed != null ? row.consumed : input + output) || 0,
      input_tokens: input,
      output_tokens: output,
      active_days: Number(row.active_days) || 0,
      models: Array.isArray(row.models) ? row.models : [],
      model_count: Number(row.model_count) || 0,
      per_account_quota: Number(row.per_account_quota) || 0,
      quota_used_pct: Number(row.quota_used_pct) || 0,
    };
  }
```

**1d. 修改 `enrichStatus`**(原文件 511-541 行),改用 `amount_total` 汇总,quota 默认值改为 `120.0`:

```javascript
  function enrichStatus(status, accounts) {
    const accs = accounts || [];
    const sumAmount = accs.reduce((s, a) => s + (a.amount_total || 0), 0);
    const accountsWithData = accs.filter(
      (a) => (a.amount_total || 0) > 0
    ).length;

    const total_consumed = Number(status && status.total_consumed) || sumAmount;
    const per_account_quota = Number(status && status.per_account_quota) || 120.0;
    const total_quota = Number(status && status.total_quota) ||
      per_account_quota * Math.max(accountsWithData, accs.length);
    const total_remaining = Number(status && status.total_remaining) ||
      Math.max(0, total_quota - total_consumed);

    const utilization_pct = Number(status && status.utilization_pct) ||
      (total_quota > 0 ? (total_consumed / total_quota) * 100 : 0);

    return Object.assign({}, status || {}, {
      total_consumed: total_consumed,
      per_account_quota: per_account_quota,
      total_quota: total_quota,
      total_remaining: total_remaining,
      utilization_pct: utilization_pct,
      cycle_start: (status && status.cycle_start) || null,
      cycle_end: (status && status.cycle_end) || null,
      nextResetAt: (status && status.nextResetAt) || null,
    });
  }
```

**1e. 在 Public API 导出列表新增 `formatCNY`**(原文件 684-686 行附近,在 `formatTokens,` 之后加一行):

```javascript
    // formatting
    formatTokens,
    formatCNY,
    formatInt,
```

保留 `aggregateByDay`/`aggregateByModel`(tooltip 仍用 token 字段)**不动**。

- [ ] **Step 2: 编辑 `src/trae_dashboard/static/index.html`**

**2a. 删除配额条的 Input/Output 图例**(原文件 137-149 行整块删除)。`quota-bar__legend` 整个 `<span class="quota-bar__legend">…</span>` 块删除,只保留 `<span class="quota-bar__title-text">配额使用</span>`:

```html
            <div class="quota-bar__title">
              <span class="quota-bar__title-text">配额使用</span>
            </div>
```

**2b. 删除配额条的双段填充 DOM**(原文件 181-183 行):

```html
            <div class="quota-bar__fill" id="quota-fill" style="width: 0%;"></div>
```

(删除 `quota-fill-input` 与 `quota-fill-output` 两个子 `<div>`。)

**2c. 修改表格列头**(原文件 270-273 行),删除 `Input`/`Output` 两列,`消耗` 改为 `消耗(¥)`:

```html
                  <th class="sortable num" data-key="total"   data-type="num">消耗(¥)<span class="sort-arrow"></span></th>
                  <th class="sortable num" data-key="quota"   data-type="num">配额使用率<span class="sort-arrow"></span></th>
                  <th class="status-col">状态</th>
```

同时修改 `<colgroup>`(原文件 257-262 行),删除两列 input/output 的 `<col>`:

```html
              <colgroup>
                <col class="chk-col" style="width:36px" />
                <col class="num idx-col" style="width:48px" />
                <col style="width:200px" />
                <col style="width:200px" />
                <col class="num quota-col" style="width:150px" />
                <col class="status-col" style="width:80px" />
              </colgroup>
```

**2d. 修改 KPI 副标题文案**(原文件 874 行),`"· 周期累计 · Input + Output"` 改为 `"· 周期累计 · 金额"`:

```javascript
        setSub("consumed", "· 周期累计 · 金额");
```

**2e. 修改 `renderKpis` 中的 `consumed` 显示**(原文件 860 行),改用 `formatCNY`:

```javascript
      setKpi("consumed", App.formatCNY(consumed));
```

**2f. 修改 `paintQuotaBar` 函数**(原文件 923-1021 行)。删除 `inN`/`outN` 计算、`quotaFillInput`/`quotaFillOutput` 设置、`quotaInVal`/`quotaOutVal` 文本设置,金额改用 `formatCNY`:

```javascript
    function paintQuotaBar(enriched) {
      const quota = enriched.total_quota || 0;
      const consumed = enriched.total_consumed || 0;

      let pct = 0;
      if (quota > 0) {
        pct = (consumed / quota) * 100;
      }
      const barPct = Math.min(100, Math.max(0, pct));

      // Single-color fill (amount-based, no Input/Output split).
      quotaFill.style.width = barPct.toFixed(2) + "%";

      quotaTrack.setAttribute("aria-valuenow", String(Math.round(barPct)));
      quotaTrack.setAttribute("aria-valuemin", "0");
      quotaTrack.setAttribute("aria-valuemax", "100");
      if (quota > 0) {
        quotaTrack.setAttribute(
          "aria-valuetext",
          "已用 " + App.formatCNY(consumed) + " / 共 " + App.formatCNY(quota)
        );
      } else {
        quotaTrack.removeAttribute("aria-valuetext");
      }

      let bucket = "zero";
      if (consumed === 0 || quota === 0) bucket = "zero";
      else if (pct >= 90) bucket = "high";
      else if (pct > 70) bucket = "mid";
      else bucket = "low";
      quotaBar.setAttribute("data-util", bucket);
      quotaBar.setAttribute("data-over", pct > 100 ? "true" : "false");
      quotaPct.className = "quota-bar__metric-pct mono quota-bar__pct--" + bucket;

      const remain = Math.max(0, quota - consumed);
      quotaUsedEl.textContent = App.formatCNY(consumed);
      quotaTotalEl.textContent = quota > 0 ? App.formatCNY(quota) : "未配置";
      quotaRemainEl.textContent = quota > 0 ? App.formatCNY(remain) : "—";
      quotaPct.textContent = quota > 0 ? App.formatPercent(pct, 1) : "—";

      var remainMetric = quotaRemainEl.parentElement;
      remainMetric.classList.remove("quota-bar__metric--warn", "quota-bar__metric--critical");
      if (quota > 0) {
        const ratio = remain / quota;
        if (ratio < 0.1) remainMetric.classList.add("quota-bar__metric--critical");
        else if (ratio < 0.3) remainMetric.classList.add("quota-bar__metric--warn");
      }

      let thresholdState = "ok";
      let thresholdText = "阈值正常 · 80% 起提醒";
      if (quota > 0 && pct >= 100) {
        thresholdState = "alert";
        thresholdText = "已超额 · 立即暂停或扩容";
      } else if (quota > 0 && pct >= 90) {
        thresholdState = "alert";
        thresholdText = "已触达 90% · 立即告警";
      } else if (quota > 0 && pct >= 80) {
        thresholdState = "warn";
        thresholdText = "已达 80% · 通知到负责人";
      } else if (quota === 0 && consumed > 0) {
        thresholdState = "warn";
        thresholdText = "未配置月配额 · 仅展示消耗";
      }
      quotaThreshold.setAttribute("data-state", thresholdState);
      quotaThresholdText.textContent = thresholdText;
      var iconEl = document.getElementById("quota-threshold-icon");
      if (iconEl) {
        iconEl.innerHTML = thresholdState === "ok"
          ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
          : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
      }
    }
```

**2g. 删除对 `quotaFillInput`/`quotaFillOutput`/`quotaInVal`/`quotaOutVal` 的 DOM 引用**(原文件 701-702 行、707-708 行):

```javascript
    const quotaFill = document.getElementById("quota-fill");
    const quotaPct = document.getElementById("quota-pct");
    const quotaUsedEl = document.getElementById("quota-used");
    const quotaTotalEl = document.getElementById("quota-total");
    const quotaRemainEl = document.getElementById("quota-remain");
    const quotaThreshold = document.getElementById("quota-threshold");
    const quotaThresholdText = document.getElementById("quota-threshold-text");
    const resultCount = document.getElementById("result-count");
```

(删除 `quotaFillInput`/`quotaFillOutput`/`quotaInVal`/`quotaOutVal` 四行变量声明。)

**2h. 修改 `decorateRows` 函数**(原文件 1036-1071 行),`_total` 改为 `amount_total`,`_quotaPct` 默认值计算改用 amount,tooltip 文案改为金额:

```javascript
    function decorateRows(accounts) {
      return accounts.map((a, i) => {
        const amount = Number(a.amount_total) || 0;
        const inN = Number(a.input_tokens) || 0;
        const outN = Number(a.output_tokens) || 0;
        const name = a.display_name || (a.email || "").split("@")[0] || "(未命名)";
        const perQ = Number(a.per_account_quota) || 120.0;
        const quotaPct = Number(a.quota_used_pct) ||
          (perQ > 0 ? (amount / perQ) * 100 : 0);
        const models = Array.isArray(a.models) ? a.models : [];
        const tooltipLines = models.length
          ? models.map(function (m) {
              return m.name + ": " +
                App.formatCNY(m.amount_total) +
                " (in " + App.formatTokens(m.input_tokens) +
                " / out " + App.formatTokens(m.output_tokens) + ")";
            })
          : ["该账号本周期无模型用量"];
        return {
          ...a,
          _idx: i,
          _name: name,
          _input: inN,
          _output: outN,
          _total: amount,
          _quotaPct: quotaPct,
          _quotaOver: quotaPct > 100,
          _quotaBucket: quotaPct >= 90 ? "high" : quotaPct > 70 ? "mid" : "low",
          _tooltip: tooltipLines.join("\n"),
          _models: models,
        };
      });
    }
```

**2i. 修改 `renderTable` 的行 HTML**(原文件 1175-1222 行),删除 `Input`/`Output` 两列,`消耗` 列用 `formatCNY`:

```javascript
      tbody.innerHTML = pageRows.map(function (r) {
        const hue = App.hueFor(r.email);
        const color = "hsl(" + hue + " 60% 55%)";
        const initial = App.initial(r._name);
        const qPct = r._quotaPct;
        const qBarW = Math.max(0, Math.min(100, qPct));
        const qBucket = r._quotaBucket;
        var statusHtml;
        if (qPct > 100) {
          statusHtml = '<span class="status-chip status-chip--alert">已超额</span>';
        } else if (qPct >= 90) {
          statusHtml = '<span class="status-chip status-chip--alert">接近上限</span>';
        } else if (qPct >= 70) {
          statusHtml = '<span class="status-chip status-chip--warn">注意</span>';
        } else {
          statusHtml = '<span class="status-chip status-chip--ok" title="正常使用中"><span class="status-dot"></span>正常</span>';
        }
        return (
          '<tr class="account-row" data-email="' + escapeHtml(r.email) + '" data-name="' + escapeHtml(r._name) + '">' +
            '<td class="chk-col"><input type="checkbox" class="row-chk" aria-label="选择 ' + escapeHtml(r._name) + '" /></td>' +
            '<td class="num idx-col">' + (r._idx + 1) + '</td>' +
            '<td>' +
              '<div class="account-row__name">' +
                '<div class="avatar" style="background:' + color + '">' + escapeHtml(initial) + '</div>' +
                '<span class="account-row__name-text">' + escapeHtml(r._name) + '</span>' +
              '</div>' +
            '</td>' +
            '<td class="account-row__email mono" title="点击复制邮箱">' + escapeHtml(r.email) + '</td>' +
            '<td class="num cell-total" tabindex="0" role="button" aria-label="' + escapeHtml(r._name) + ' 的模型消耗明细" data-models="' + escapeHtml(JSON.stringify(r._models || [])) + '"><strong>' + App.formatCNY(r._total) + '</strong></td>' +
            '<td class="num quota-cell quota-cell--' + qBucket + (r._quotaOver ? ' quota-cell--over' : '') + '">' +
              '<div class="quota-mini-bar" role="progressbar" aria-valuenow="' + Math.round(qPct) + '" aria-valuemin="0" aria-valuemax="100">' +
                '<div class="quota-mini-bar__fill" style="width:' + qBarW.toFixed(1) + '%"></div>' +
              '</div>' +
              '<span class="quota-mini-pct mono">' + qPct.toFixed(1) + '%</span>' +
            '</td>' +
            '<td class="status-col-cell">' + statusHtml + '</td>' +
          '</tr>'
        );
      }).join("");
```

注意:`colspan` 从 `9` 改为 `7`(因删除两列),原文件 1122 行、1162 行、1320 行的三处空状态 HTML 均需把 `colspan="9"` 改为 `colspan="7"`。

**2j. 修改 `showTooltip` 函数**(原文件 582-624 行),列头与单元格改用金额 + token 辅助:

```javascript
    function showTooltip(target, modelsJson) {
      let models = [];
      try { models = JSON.parse(modelsJson || "[]"); } catch (_) { models = []; }
      let html;
      if (!models.length) {
        html = '<div class="tooltip__empty">该账号本周期无模型用量</div>';
      } else {
        html = '<div class="tooltip__title">模型消耗明细</div>' +
          '<div class="tooltip__table">' +
            '<div class="tooltip__table-head">' +
              '<span>模型</span>' +
              '<span>金额</span>' +
              '<span>Tokens (in/out)</span>' +
            '</div>';
        models.forEach(function (m) {
          html +=
            '<span class="tooltip__model">' + escapeHtml(m.name || "?") + '</span>' +
            '<span class="tooltip__num tooltip__num--total">' + App.formatCNY(m.amount_total) + '</span>' +
            '<span class="tooltip__num">' +
              App.formatTokens(m.input_tokens) + ' / ' + App.formatTokens(m.output_tokens) +
            '</span>';
        });
        html += "</div>";
      }
      tooltipEl.innerHTML = html;
      tooltipEl.hidden = false;
      requestAnimationFrame(function () {
        const r = target.getBoundingClientRect();
        const tw = tooltipEl.offsetWidth;
        const th = tooltipEl.offsetHeight;
        let x = r.left + (r.width / 2) - (tw / 2);
        let y = r.bottom + 8;
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        if (x < 8) x = 8;
        if (x + tw > vw - 8) x = vw - tw - 8;
        if (y + th > vh - 8) y = r.top - th - 8;
        tooltipEl.style.left = x + "px";
        tooltipEl.style.top = y + "px";
        tooltipEl.classList.add("is-visible");
      });
    }
```

**2k. 修改 CSV 导出列头与列索引**(原文件 1527 行 batch-export、1556 行 quick-export)。两处均把 `["账号","邮箱","Input","Output","消耗","配额使用率","状态"]` 改为 `["账号","邮箱","消耗(CNY)","配额使用率","状态"]`,并把列索引从 `cells[4]/cells[5]/cells[6]/cells[7]/cells[8]` 改为 `cells[4]/cells[5]/cells[6]`(因表格删了两列,新列顺序为:chk(0)/idx(1)/name(2)/email(3)/total(4)/quota(5)/status(6)):

batch-export(原文件 1524-1549 行):

```javascript
    document.getElementById("batch-export").addEventListener("click", function () {
      var checked = document.querySelectorAll("#tbody .row-chk:checked");
      if (!checked.length) return;
      var rows = [["账号","邮箱","消耗(CNY)","配额使用率","状态"]];
      checked.forEach(function (cb) {
        var tr = cb.closest("tr");
        if (!tr) return;
        var cells = tr.querySelectorAll("td");
        // 列顺序:chk(0)/idx(1)/name(2)/email(3)/total(4)/quota(5)/status(6)
        rows.push([
          tr.getAttribute("data-name") || "",
          tr.getAttribute("data-email") || "",
          cells[4] ? cells[4].textContent.trim() : "",
          cells[5] ? cells[5].textContent.trim() : "",
          cells[6] ? cells[6].textContent.trim() : "",
        ]);
      });
      var csv = rows.map(function (r) { return r.map(function (c) { return '"' + c.replace(/"/g, '""') + '"'; }).join(","); }).join("\n");
      var blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url; a.download = "trae-accounts.csv"; a.click();
      URL.revokeObjectURL(url);
      App.showToast("已导出 " + checked.length + " 个账号", { variant: "success" });
    });
```

quick-export(原文件 1554-1577 行):

```javascript
    var btnQuickExport = document.getElementById("btn-quick-export");
    if (btnQuickExport) {
      btnQuickExport.addEventListener("click", function () {
        var allRows = [["账号","邮箱","消耗(CNY)","配额使用率","状态"]];
        document.querySelectorAll("#tbody .account-row").forEach(function (tr) {
          var cells = tr.querySelectorAll("td");
          allRows.push([
            tr.getAttribute("data-name") || "",
            tr.getAttribute("data-email") || "",
            cells[4] ? cells[4].textContent.trim() : "",
            cells[5] ? cells[5].textContent.trim() : "",
            cells[6] ? cells[6].textContent.trim() : "",
          ]);
        });
        var csv = allRows.map(function (r) { return r.map(function (c) { return '"' + String(c).replace(/"/g, '""') + '"'; }).join(","); }).join("\n");
        var blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url; a.download = "trae-accounts-all.csv"; a.click();
        URL.revokeObjectURL(url);
        App.showToast("已导出当前页 " + (allRows.length - 1) + " 个账号", { variant: "success" });
      });
    }
```

注意:原 quick-export 第 1575 行 `allRows.length - 1` 缺括号是个已存在 bug,顺便修复为 `(allRows.length - 1)`。

**2l. 修改 report modal toast 文案**(原文件 2283-2289 行),`total_consumed` 显示用 `formatCNY`:

```javascript
          var totalConsumed =
            result && typeof result.total_consumed === "number"
              ? App.formatCNY(result.total_consumed)
              : null;
          var detail =
            totalConsumed !== null
              ? "（" + count + " 位收件人，总消耗 " + totalConsumed + "）"
              : "（" + count + " 位收件人）";
          App.showToast("已发送邮件报告" + detail, { variant: "success" });
```

**2m. 修改帮助说明文案**(原文件 380-386 行附近),把"input + output token 之和"改为"金额消耗之和",把"每账号的 consumed / 50M × 100%"改为"每账号的 amount_total / 120.0 × 100%":

```html
          <dd>本周期所有账号的金额消耗总和。颜色按用量档位:&lt;70% 绿,70-90% 橙,≥90% 红,&gt;100% 红色闪烁。</dd>
```

```html
          <dd>每账号的 amount_total / 120.0 × 100%。&lt;70% 正常,70-90% 注意,90-100% 接近上限,&gt;100% 已超额。</dd>
```

- [ ] **Step 3: 编辑 `src/trae_dashboard/static/style.css`**

**3a. 修改 `.quota-bar__fill` 与删除双段填充**(原文件 716-736 行),改为单色:

```css
.quota-bar__fill {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  background: var(--color-status-active);
  border-radius: var(--radius-pill);
  transition: width var(--transition-slow);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}
```

(删除 `.quota-bar__fill-input` 与 `.quota-bar__fill-output` 两个规则块。)

**3b. 删除 Input/Output 图例色块规则**(原文件 574-575 行):

```css
/* .quota-bar__legend-swatch--in / --out removed: legend DOM deleted in Task 8.2a */
```

(保留两行注释或直接删除,只要规则被移除即可。)

**3c. 修改 `.cell-in` / `.cell-out` 规则**(原文件 1292-1293 行),因表格列已删除,这两个规则无用,可删除或保留(不影响)。建议保留以兼容可能的 stale 模板:

```css
/* .cell-in / .cell-out retained for legacy templates; table no longer renders these columns. */
```

- [ ] **Step 4: 手动验证**

启动后端:`python -m trae_dashboard serve`(若需 scheduler 则 `--with-scheduler`)。

浏览器打开 `http://localhost:8765/`,验证:

1. **KPI 卡片**:总账号数 / 已消耗(`¥ X,XXX.XX`)/ 剩余(`¥ X,XXX.XX`)显示正确。
2. **配额条**:单色填充(绿/橙/红按 bucket),无 Input/Output 分段;总量/已用/剩余三段文案均为 `¥` 格式;无图例 swatch。
3. **表格**:列头为 `#` / 账号 / 邮箱 / 消耗(¥) / 配额使用率 / 状态(6 列,无 Input/Output 列);消耗单元格显示 `¥ X,XXX.XX`;hover 消耗单元格出现 tooltip,显示每模型的金额 + token。
4. **CSV 导出**:点击"导出"按钮,下载文件用 Excel 打开,列头为 `账号,邮箱,消耗(CNY),配额使用率,状态`。
5. **Console**:无 JS 报错(F12 查看)。
6. **搜索/排序/分页/批量选择**:功能正常。
7. **主题切换**:light/dark 切换正常,单色配额条颜色随主题变化。

- [ ] **Step 5: 运行全量后端测试,确认无回归**

Run: `python -m pytest tests/ -v`
Expected: PASS(前端改动不影响后端测试)

- [ ] **Step 6: 提交**

```bash
git add src/trae_dashboard/static/app.js src/trae_dashboard/static/index.html src/trae_dashboard/static/style.css
git commit -m "refactor(frontend): switch to CNY amount-based display, single-color quota bar"
```

