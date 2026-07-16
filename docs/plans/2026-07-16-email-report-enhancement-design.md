# 邮件发送功能增强 — 设计

日期: 2026-07-16
状态: 已批准,待实施

## 背景与动机

当前的邮件报告功能(`trae-dashboard report` + `POST /api/report`)在用户面存在三个短板:

1. **收件人难以配置**。收件人列表写在 `config.yaml` 的 `email.recipients`,UI 弹窗里只能勾选「本次发送哪些人」,无法增删,需要手编配置文件。
2. **邮件内容不可改**。报告是固定模板,只读预览,无法在发送前微调主题、正文。
3. **SMTP 配置缺 UI**。`smtp_host` / `smtp_port` / `smtp_user` / `from_addr` 都在 `config.yaml`,密码存在 `.env`,改起来都不顺手。

## 目标

1. **收件人可配置** — UI 增删改 → 写回 `config.yaml`,并同步内存,避免改完需重启。
2. **SMTP 配置可配置** — UI 编辑 `smtp_host` / `smtp_port` / `smtp_user` / `from_addr` / `send_time` / `enabled` → 写回 `config.yaml`;密码通过单独入口写 `.env`。
3. **下载 `.eml`** — 把当前报告导出为标准 `.eml` 文件,`To` 预填已选收件人,用户在自己邮件客户端里修改后发送。
4. 保留现有的「应用内 SMTP 发送」能力。

## 明确不做(YAGNI)

- 不做 `.eml` 上传回传再发送(修改在客户端完成)。
- 不做富文本/模板编辑器。
- 不改 SMTP 认证/发送机制本身。
- 不暴露 `smtp_password_env` 变量名到 UI。
- 不从 UI 关闭 `email.enabled`(防误关)。

## 组件与数据流

### A. `config_writer.py`(新模块)

负责把 `config.yaml` 和 `.env` 的受控修改落盘。PyYAML `safe_dump` 会丢失注释,这是已知取舍(用户确认)。

- **`save_recipients(config_path: Path, recipients: list[str]) -> None`**
  - `yaml.safe_load` 读入,设置 `data["email"]["recipients"]`。
  - 写回时**保留** `email` 下其它字段(`smtp_host` / `smtp_port` / `smtp_user` / `from_addr` / `smtp_password_env` / `send_time` / `enabled`)。

- **`save_email_config(config_path: Path, *, smtp_host, smtp_port, smtp_user, from_addr, send_time) -> None`**
  - 校验:`smtp_host` 非空;`1 ≤ smtp_port ≤ 65535`;`smtp_user` / `from_addr` 通过 `_EMAIL_RE`;`send_time` 匹配 `^\d{2}:\d{2}$`。
  - 写回时保留 `recipients` / `smtp_password_env` / `enabled`。

- **`save_env_var(env_path: Path, key: str, value: str) -> None`**
  - 改 `.env` 中某个变量:行内替换同名 `KEY=...`;无则追加;含空格/`=` 等特殊字符时加双引号。
  - 用「临时文件 + `os.replace`」原子写。

- **公共前置检查**:`safe_load` 验证文件可解析;`data["email"]` 缺失则初始化为 `{"enabled": False}`。解析失败 → 抛 `RuntimeError`,不写。

- **写回头部注释**:`save_recipients` / `save_email_config` 完成后,若文件首行不是 `# 本文件的 email.* 由 Trae Dashboard 管理,手动编辑可能被覆盖`,则 prepend 上去(避免重复加)。

### B. `report.py` 扩展

- 提取 `_build_message(email_cfg, subject, html_body) -> EmailMessage`,`send_email` 和 `build_eml` 都调它,消除重复。
- `build_eml(email_cfg, subject, html_body) -> bytes`:`msg.as_bytes(policy=email.policy.SMTPUTF8)`。
- `run_report(...)` 接口不变。

### C. API 端点

| 端点 | 方法 | 作用 |
| --- | --- | --- |
| `/api/report/config` | GET | 现有基础上加 `smtp_password_set: bool`(读 `.env`),不返回密码 |
| `/api/report/recipients` | PUT | body `{recipients: [...]}` 整表覆盖;写回 `config.yaml` + 内存 |
| `/api/report/smtp` | PUT | body `{smtp_host, smtp_port, smtp_user, from_addr, send_time}`;写回 `config.yaml` + 内存 |
| `/api/report/smtp/password` | POST | body `{password}`;写到 `.env` 的 `SMTP_PASSWORD`(变量名取 `cfg.email.smtp_password_env`) |
| `/api/report/eml` | POST | 用当前勾选收件人构造 `.eml`,返回 `message/rfc822` 文件 |
| `/api/report` | POST | 现有,不变 |

**端点通用规则**:
- 收件人/SMTP 字段校验失败 → `400`。
- 写回失败 → `500`(内存回滚)。
- `.eml` 导出时若 `recipients` 为空,`To` 留空(用户客户端补),不报错;若 `email.enabled=false` 也允许(纯导出)。
- `.eml` 返回头:`Content-Type: message/rfc822`,`Content-Disposition: attachment; filename="trae-report-YYYY-MM-DD.eml"`。

**接线变更**:
- `create_app(*, cfg, storage, config_path)` 增 `config_path` 参数。
- `_serve(config_path, host, port, ...)` 透传 `config_path`。

### D. 前端(index.html 发送报告 modal)

**布局**: 双 Tab(「发送」/ 「邮件设置」),共用 footer 按钮区。

**Tab1 发送**(现有内容改造):
- 收件人勾选 chip(只显示「已配置」,只读 — 修改在 Tab2)。
- 预览 iframe + 主题。
- Footer:「下载 .eml」/「取消」/「确认发送」。

**Tab2 邮件设置**(新增):
- **收件人管理区**: chip 列表(每项带「删除」) + 底部「邮箱地址」输入框 + 「添加」按钮(暂存到 UI,未点「保存收件人」前不写盘)。「保存收件人」→ `PUT /api/report/recipients`,成功 toast 刷新。
- **SMTP 配置区**: 表单(host / port / user / from_addr / send_time),每个字段都做前端格式校验。「保存 SMTP 设置」→ `PUT /api/report/smtp`。
- **SMTP 密码区**: 状态行(「已设置」/「未设置」读 `smtp_password_set`);「修改密码」按钮 → 弹小输入框(modal 内 inline,非新 modal),提交 `POST /api/report/smtp/password`,成功后关闭小框并刷新状态行。密码字段不留痕,关闭即清空。
- **`email.enabled` 开关**: 单选 toggle,只允许从 `false → true`,`true` 时显示为「已启用」且禁用(灰色);从 UI 不能关闭。

## 错误处理

- 写回失败:内存回滚 + `500`,前端 toast。
- `.env` 写失败:`500`,前端 toast。
- 解析失败:拒绝写,`500`。
- `.eml` 导出:不依赖 `email.enabled`(纯导出);空收件人允许。
- 现有 SMTP 发送错误处理路径不变(`502`)。

## 测试

### `tests/test_config_writer.py`(新)
- `save_recipients`: 写回后能被 `load_config` 读出,中文/特殊字符邮箱,空列表也能写。
- `save_recipients`: 写回不破坏 `email` 下其它字段(`smtp_host` 等保留)。
- `save_email_config`: 写回不破坏 `recipients` / `smtp_password_env` / `enabled`。
- `save_email_config`: 非法 host / port / time 抛错。
- `save_env_var`: 替换已有值、追加新值、含 `=` 或空格的值加引号、其他变量不被破坏、文件不存在时创建。
- 原子写:用 mock fs 验证临时文件 + `os.replace` 路径(不严格要求 mock,看实际可行)。

### `tests/test_report.py`(扩展)
- `build_eml` 产出可被 `email.parser.BytesParser` 解析;`To` / `Subject` / `Date` 头正确;存在 `text/html` alternative;`From` 与 `cfg.from_addr` 一致。
- `send_email` 重构后,`smtplib.SMTP_SSL.login`/`send_message` 调用参数与之前一致。

### API 集成测试(`tests/test_api.py` 或扩展现有)
- `PUT /api/report/recipients`:合法列表 200 并被下次 `GET /api/report/config` 读到;非法邮箱 400;重复邮箱去重;大小写归一化。
- `PUT /api/report/smtp`:合法字段 200;非法 host/port/time 400;写回后 `GET /api/report/config` 读到新值;同时 `recipients` 不丢。
- `POST /api/report/smtp/password`:写后 `smtp_password_set=true`;响应不返回密码。
- `POST /api/report/eml`:返回 `Content-Type: message/rfc822` 与正确 `Content-Disposition`;body 可被 `email.parser.BytesParser` 解析;`To` 与传入 `recipients` 一致。
- `GET /api/report/config` 增加 `smtp_password_set`,不返回密码。

## 兼容性 / 迁移

- 现有 `config.yaml` 不变;`save_recipients` 首次触发时 prepend 头部注释,其它内容原样保留(除 `email` 段外字段顺序和注释会丢 — 用户已知情)。
- 现有 `POST /api/report`(发送)和 `GET /api/report/config` 形态保持兼容,仅 `GET` 多一个字段。
- CLI `trae-dashboard report` 不变(仍用 `cfg.email.recipients` 发送)。

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| PyYAML 重写丢注释 | 顶部加管理注释;`config.example.yaml` 始终是完整文档源 |
| 写盘过程中崩溃 | 临时文件 + `os.replace` 原子写 |
| 内存与磁盘不一致 | 写盘前修改内存;写盘失败回滚内存 |
| 密码误写/泄露 | 密码不返回 API、UI 不持久化、`.env` 改写后只回 `smtp_password_set: true` |
| 并发写 | UI 是单用户;后端无并发写请求(单进程),`config_writer` 不加锁;若未来加多用户需重新审视 |
