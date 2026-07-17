# 架构图(原始 Mermaid 源)

下面是 README 里嵌入的架构图的 Mermaid 源,GitHub / VS Code 都能直接渲染。

## 主架构图

```mermaid
flowchart TB
    %% ====== 外部 ======
    User["🌐 用户浏览器"]
    SchedExt["⏰ 系统任务计划<br/>Windows Task Scheduler / cron"]
    TraeAPI["☁️ Trae Enterprise OpenAPI<br/>openapi.enterprise.trae.cn"]
    SMTP["📤 SMTP 服务器<br/>smtp.qq.com:465"]

    %% ====== CLI 入口 ======
    subgraph CLI["CLI 入口 (python -m trae_dashboard)"]
        direction LR
        CmdInit["init<br/>种子 config.yaml"]
        CmdFetch["fetch<br/>跑一次采集"]
        CmdServe["serve<br/>起 Web 服务"]
        CmdPrune["prune<br/>清理过期数据"]
        CmdReport["report<br/>发每日邮件"]
    end

    %% ====== 后端模块 ======
    subgraph Backend["后端 (src/trae_dashboard/)"]
        direction TB
        Config["config.py + config_writer.py + validation.py<br/><i>配置加载 / 校验 / 写回</i>"]
        Cycle["cycle.py<br/><i>周期窗口计算</i>"]
        Client["client.py + auth.py<br/><i>OpenAPI 调用 + Bearer Token</i>"]
        Collector["collector.py<br/><i>拉取 + 解析 + 持久化</i>"]
        Storage["storage.py<br/><i>SQLite CRUD + display_weights 加权读</i>"]
        Sched["scheduler.py<br/><i>APScheduler 后台拉取</i>"]
        Report["report.py<br/><i>HTML 渲染 + SMTP 发送 + .eml 导出</i>"]
        API["api.py<br/><i>FastAPI 路由 + 静态文件</i>"]
    end

    %% ====== 前端 ======
    subgraph Frontend["前端 (src/trae_dashboard/static/)"]
        direction LR
        UI["index.html<br/>app.js / style.css / tokens.css"]
    end

    %% ====== 持久化 ======
    subgraph Storage_["持久化"]
        direction LR
        DB[("SQLite<br/>data/dashboard.db")]
        Yaml[("config.yaml")]
        Env[(".env<br/>TRAE_APP_* + SMTP_PASSWORD")]
    end

    %% ====== 连线:用户交互 ======
    User -- HTTP --> API
    UI -- fetch --> API

    %% ====== 连线:CLI 触发 ======
    SchedExt -- 每日定时 --> CmdReport
    CmdInit -- 写 --> Yaml
    CmdServe --> API
    CmdFetch --> Collector
    CmdPrune --> Storage
    CmdReport --> Report

    %% ====== 连线:数据采集链 ======
    Collector --> Client
    Collector --> Storage
    Client -- HTTPS --> TraeAPI
    Sched -. 每小时 .-> Collector
    Cycle -. 周期窗口 .-> Collector
    Cycle -. 周期窗口 .-> Storage
    Cycle -. 周期窗口 .-> Report

    %% ====== 连线:API 服务 ======
    API --> Storage
    API --> Config
    API --> Report

    %% ====== 连线:配置写回 ======
    Config -- 读 --> Yaml
    Config -- 读 --> Env
    Config -- 写回 --> Yaml
    Config -- 写回 --> Env

    %% ====== 连线:邮件 ======
    Report -- SMTP_SSL --> SMTP
    Report --> Storage

    %% ====== 连线:持久化层 ======
    Storage -- 读写 --> DB

    %% 样式
    classDef ext fill:#e0e7ff,stroke:#4338ca,color:#1e1b4b
    classDef cli fill:#fef3c7,stroke:#b45309,color:#78350f
    classDef backend fill:#dcfce7,stroke:#15803d,color:#14532d
    classDef frontend fill:#fce7f3,stroke:#be185d,color:#831843
    classDef store fill:#f1f5f9,stroke:#475569,color:#0f172a

    class User,SchedExt,TraeAPI,SMTP ext
    class CmdInit,CmdFetch,CmdServe,CmdPrune,CmdReport cli
    class Config,Cycle,Client,Collector,Storage,Sched,Report,API backend
    class UI frontend
    class DB,Yaml,Env store
```

## 图例

| 箭头 | 含义 |
|---|---|
| 实线 `→` | 直接调用 / 同步数据流 |
| 虚线 `-.->` | 间接依赖 / 定时触发 |
| 蓝色 | 外部系统 / 服务 |
| 黄色 | CLI 命令 |
| 绿色 | 后端模块 |
| 粉色 | 前端 |
| 灰色 | 持久化存储 |

## 主要数据流(三个)

### 1. 拉数据 — 定时 / 手动

```
Trae OpenAPI  ─►  client.py  ─►  collector.py  ─►  storage.py  ─►  SQLite
                       ▲
                       │
              auth.py (Bearer Token 缓存)
                       ▲
                       │
            .env (TRAE_APP_ID + TRAE_APP_SECRET)
```

触发方式:
- `serve --with-scheduler` → APScheduler 每小时调一次 collector
- `python -m trae_dashboard fetch` → CLI 手动跑一次

### 2. 展示数据 — 浏览器拉

```
浏览器 ─► index.html (静态) ─► /api/* ─► storage.py ─► SQLite
                  ▲
                  │ 静态文件由 api.py 的 mount("/") 提供
                  │
            FastAPI 路由 (api.py)
                  │
        ┌─────────┼─────────┐
        │         │         │
     /status   /accounts  /report
```

读路径上 `storage.py` 应用 `display_weights` 加权(Doubao-Seed-Code × 0.5),对齐 Trae 官网展示口径。

### 3. 邮件报告

```
触发源 ─► report.py ─┬─► storage.py  (读当前周期)
                     │
                     ├─► build_eml   (返回 RFC822 .eml 给 API 下载)
                     ├─► SMTP_SSL    (实际发邮件到收件人)
                     │
                     └─► render_html (内部,拼 HTML 表格)
```

触发源:
- `python -m trae_dashboard report` CLI(被 Windows 任务计划 / cron 每天 09:00 调)
- Web 端 `POST /api/report`(弹窗点"确认发送")
- Web 端 `POST /api/report/eml` 或 `GET /api/report/eml`(下载 .eml 后用本地邮件客户端发)