# 数据模型与前端重构:从 Token 限额到金额限额

**日期**: 2026-07-13
**状态**: 已批准
**背景**: Trae 企业版计费模式变更,每人每月限额从 Token 改为 120 元金额消耗。接口响应新增 amount 字段。参考: https://docs.trae.cn/enterprise_get-model-usage-for-users

## 目标

将整个 traeDashboard 从 Token 计量体系迁移到金额计量体系:
- 主指标: Token → 金额(¥)
- 配额: 50000000 token/人 → 120.0 元/人/周期月
- 数据采集: 只采集 Trae 内置模型(model_source == "Trae")
- 移除: included_model_names 白名单、model_aliases 别名、display_weights 权重

## 非目标

- 不改变认证流程(app_id/app_secret/oauth)
- 不改变周期窗口计算逻辑(每月 10 号重置)
- 不改变邮件发送机制(SMTP)
- 不改变 CLI 子命令结构

## 架构决策

**原地重构(方案 A)**: 直接修改现有代码,`model_usage` 表 schema 演进(新增 amount 字段 + 清空旧数据),移除 display_weights/included_model_names 逻辑。保持现有模块边界(storage/collector/api/report 各司其职)。

## §1 数据模型与 Schema

### model_usage 表 schema 演进

```sql
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  cycle_start TEXT NOT NULL,
  cycle_end TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_type TEXT,
  model_source TEXT,              -- 保留:用于 "Trae" 过滤
  input_tokens INTEGER DEFAULT 0,  -- 保留:tooltip 辅助
  output_tokens INTEGER DEFAULT 0, -- 保留:tooltip 辅助
  -- 新增金额字段
  amount_total REAL DEFAULT 0,     -- total_amount,主消耗指标
  amount_basic REAL DEFAULT 0,     -- basic_amount,基础会话额度
  amount_pay_go REAL DEFAULT 0,    -- pay_go_amount,按量计费
  currency TEXT DEFAULT 'CNY',
  fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(email, cycle_start, model_name),
  FOREIGN KEY (email) REFERENCES accounts(email)
);
```

### 关键决策

- **保留 token 字段**: tooltip 仍需要 per-model token 明细
- **amount 拆三列**: basic/pay_go/total 都存,方便后续分析
- **currency 存但固定显示 ¥**: 字段保留以防未来需要
- **清空策略**: 升级时 `DELETE FROM model_usage;`(保留表结构和 accounts/snapshots)
- **storage.py 的 display_weights 机制整体移除**

### Config 变更

```python
per_account_quota: float = 120.0  # 类型 int→float,语义从 token→元
# 移除: included_model_names, model_aliases, display_weights
```

### collector 过滤逻辑变更

```python
# 旧:canonical = self._canonical.get(raw_name.lower()); if canonical is None: continue
# 新:if mu.get("model_source") != "Trae": continue
```

## §2 采集层(collector + client)

### client.py

无需改动。接口路径、请求体、认证逻辑不变,响应里多了 amount 字段,client 原样返回 JSON。

### collector.py 变更

```python
class Collector:
    def __init__(self, *, client, storage, config):
        self._client = client
        self._storage = storage
        self._config = config
        # 移除: self._canonical 映射表

    def run_once(self) -> dict:
        emails = [a.email for a in self._storage.list_accounts()]
        # ... cycle window 计算不变 ...

        for item in items:
            email = item.get("email")
            if not email:
                continue
            for mu in item.get("model_usage", []):
                if mu.get("model_source") != "Trae":
                    continue

                raw_name = mu.get("model_name") or "unknown"
                u = mu.get("usage", {}) or {}
                amt = mu.get("amount", {}) or {}

                self._storage.upsert_model_usage(
                    email=email,
                    cycle_start=start_date,
                    cycle_end=end_date,
                    model_name=raw_name,
                    model_type=mu.get("model_type"),
                    model_source=mu.get("model_source"),
                    input_tokens=int(u.get("input_tokens", 0)),
                    output_tokens=int(u.get("output_tokens", 0)),
                    amount_total=float(amt.get("total_amount", 0)),
                    amount_basic=float(amt.get("basic_amount", 0)),
                    amount_pay_go=float(amt.get("pay_go_amount", 0)),
                    currency=amt.get("currency", "CNY"),
                )
```

### 关键变更点

1. **移除 canonical 映射**: 不再做模型名重命名,直接用 API 返回的 model_name
2. **过滤条件从白名单改为 model_source**: `if mu.get("model_source") != "Trae": continue`
3. **amount 字段采集**: 从 `mu["amount"]` 提取 total/basic/pay_go/currency
4. **model_name 大小写**: API 返回什么就存什么,暂不做额外归一化

## §3 存储层(storage.py)

### Storage.__init__ 变更

移除 `display_weights` 参数和 `self.display_weights` 字典。

### upsert_model_usage 签名扩展

新增参数: `amount_total=0.0, amount_basic=0.0, amount_pay_go=0.0, currency="CNY"`

### get_model_usage_by_account 变更

```python
def get_model_usage_by_account(self, cycle_start, cycle_end) -> list[dict]:
    """Per-account totals. 返回:
      { email, display_name, amount_total, input_tokens, output_tokens, model_count }
    排序: 按 amount_total DESC
    移除: included_model_names 参数, weight CASE 表达式
    """
```

### get_model_usage_for_account 变更

移除 `included_model_names` 参数,返回 ModelUsage(含 amount_total 等)。

### ModelUsage dataclass 扩展

新增字段: `amount_total: float, amount_basic: float, amount_pay_go: float, currency: str`

### prune_zero_data_accounts 变更

判断零数据的依据从 `SUM(input_tokens + output_tokens) == 0` 改为 `SUM(amount_total) == 0`。

### 迁移策略(在 init() 内)

```python
def init(self) -> None:
    self.conn.executescript(SCHEMA)
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

### 新增方法 get_total_amount

```python
def get_total_amount(self, cycle_start: str, cycle_end: str) -> float:
    """当前周期所有启用账号的金额消耗总和."""
    row = self.conn.execute(
        "SELECT COALESCE(SUM(m.amount_total), 0) AS total "
        "FROM model_usage m JOIN accounts a ON a.email = m.email "
        "WHERE m.cycle_start = ? AND a.enabled = 1",
        (cycle_start,),
    ).fetchone()
    return float(row["total"] or 0)
```

## §4 API 层(api.py)

### GET /api/accounts 变更

- 主指标从 token 改为 `amount_total`
- `quota_pct` 基于 `amount_total / per_account_quota`
- `models` 数组新增 `consumed`(=amount_total)、`amount_basic`、`amount_pay_go`
- 移除: included_model_names 参数

### GET /api/status 变更

- `total_consumed` 改为 `storage.get_total_amount()`
- `total_quota = per_account_quota * accounts_count`
- `total_remaining = max(0, total_quota - total_consumed)`
- `utilization_pct` 基于金额

### 其他端点

- `GET /api/accounts/{email}/history` — 返回行带 amount_total
- `GET /api/accounts/{email}/detail` — 同上
- `POST /api/refresh` — 无需改动

## §5 配置层(config.py + config.example.yaml)

### Config dataclass 变更

```python
@dataclass
class Config:
    # ... 不变字段 ...
    per_account_quota: float = 120.0  # int→float,语义从 token→元
    # 移除: included_model_names, model_aliases, display_weights
```

### 移除

- `DEFAULT_INCLUDED_MODEL_NAMES` 常量
- `_load_included_model_names()` 函数
- `_load_model_aliases()` 函数
- `_load_display_weights()` 函数

### load_config() 简化

不再读取 `included_model_names` / `model_aliases` / `display_weights` 键。

### config.example.yaml 变更

删除 `included_model_names`、`model_aliases`、`display_weights` 配置块,新增:

```yaml
per_account_quota: 120.0  # 元/人/周期月(10号重置), 仅统计 Trae 内置模型
```

## §6 邮件报告(report.py)

### ReportRow dataclass 变更

```python
@dataclass
class ReportRow:
    display_name: str
    email: str
    amount_total: float          # 主指标:金额
    quota_pct: float             # 金额占比
    top_model: str
    top_model_amount: float      # top 模型金额
    input_tokens: int            # 保留辅助
    output_tokens: int           # 保留辅助
```

### collect_report_rows 变更

- `amount = float(a["amount_total"] or 0.0)`
- `pct = round((amount / per_q) * 100, 1)`
- `top = max(models, key=lambda m: m.amount_total)`

### render_html 变更

- `total_consumed = sum(r.amount_total for r in rows)`
- `total_quota = cfg.per_account_quota * len(rows)`
- 新增 `_fmt_cny(n)` → `¥ 1,234.56`
- 表格列: 总消耗改为 ¥,配额占比改为金额 pct,Top 模型显示 ¥ amount
- KPI 区块: 总消耗/总配额用 `_fmt_cny()`

## §7 前端(index.html + app.js + style.css)

### KPI 三卡片

- 「已消耗」「剩余」从 Token 改为 ¥
- 「总账号数」不变

### 配额条

- 总量/已用/剩余全部用 ¥
- **移除 Input/Output 分段**(金额无法按 token 类型拆分)
- 改为单色渐变填充

### 账号表格

- 移除 Input/Output/消耗(token) 三列
- 新增「消耗(¥)」列
- 列数从 9→6

### Tooltip

- per-model 明细改为「模型 | 金额(¥) | Token」三列
- 金额为主、Token 辅助

### app.js 关键变更

- `normalizeAccount()`: 主字段从 `consumed`(token) 改为 `amount_total`
- `enrichStatus()`: total_consumed/total_quota/total_remaining 全部基于金额
- 新增 `formatCNY(n)` → `¥ 1,234.56`
- 配额条: 移除 `quota-fill-input` / `quota-fill-output` 双色,改为单色 `quota-fill`
- CSV 导出: 列头从 `Input,Output,Consumed` 改为 `Consumed(CNY)`

### 状态徽章

逻辑不变(基于配额百分比),只是百分比来源从 token 改为金额。

## 错误处理

- API 响应缺失 amount 字段时,默认 0.0(不崩溃)
- amount 字段类型异常时,float() 转换失败记录 warning,跳过该条
- 迁移时如果 model_usage 表不存在,正常走 CREATE TABLE 路径

## 测试策略

- 更新现有 storage 测试: 验证 amount 字段读写、迁移逻辑、prune 逻辑
- 更新现有 collector 测试: 验证 model_source 过滤、amount 提取
- 更新现有 api 测试: 验证 /api/accounts 和 /api/status 返回金额字段
- 更新现有 report 测试: 验证 HTML 输出含 ¥ 格式
- 新增迁移测试: 模拟旧 schema → 新 schema 升级路径
