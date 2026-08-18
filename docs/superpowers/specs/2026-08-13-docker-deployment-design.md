# traeDashboard Docker 一键部署 Design

> **日期:** 2026-08-13
> **状态:** 已批准设计,待实现

## 背景

traeDashboard 目前以 Python 源码方式运行(Windows/macOS/Linux + 手动建 venv 安装依赖),配置用 `config.yaml` + `.env`,数据存本地 SQLite(`data/dashboard.db`),自动采集靠 `serve --with-scheduler`,邮件日报靠外部 Windows 任务计划 / cron 触发。

目标:将项目 Docker 化,支持**一键部署**(`docker compose up -d --build`),同时保持现有配置/数据管理方式不变。

## 需求(已与用户确认)

1. **部署范围:** Web + 采集 —— 容器内 `serve --with-scheduler` 自动采集;邮件日报保持外部 cron / 手动触发(不在本次范围内)。
2. **部署环境:** 本机 Docker Desktop。
3. **访问范围:** 仅本机访问 —— 宿主机端口绑定 `127.0.0.1`。
4. **访问端口:** 宿主 `8888`(映射到容器内部 `8765`,保持应用默认端口不动)。
5. **配置方式:** `config.yaml` / `.env` 继续放在宿主机项目根目录,通过 bind mount 挂进容器(**可写** —— 面板「账号管理 / 邮件设置」写回功能必须继续生效)。
6. **数据持久化:** SQLite 数据用 named volume 持久化,`docker compose down` 不丢数据。

## 架构

```
宿主机(本机 Docker Desktop)
├── config.yaml          ← bind mount → /app/config.yaml   (可写,面板可管理)
├── .env                 ← bind mount → /app/.env           (可写,SMTP 密码)
└── data/                ← named volume → /app/data         (SQLite 持久化)
        │
        ▼  docker compose up -d --build
container: trae-dashboard (python:3.11-slim)
  CMD:
    1. 尽力 fetch 一次(失败只打日志,交给调度器重试)
    2. exec serve --config /app/config.yaml --host 0.0.0.0 --port 8765 --with-scheduler
       (容器内绑 0.0.0.0;外部访问由 compose 映射收敛到 127.0.0.1:8888)

> 注意:config.yaml / .env 是宿主机 bind mount,全新机器**必须先**在项目根 `cp` 生成后再 `docker compose up`(见「使用方式」)。不在容器内做 auto-init —— bind mount 对不存在的路径会创建空目录,容器内 init 无法可靠兜底。
```

## 交付文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `Dockerfile` | 新增 | `python:3.11-slim`;`pip install .` 安装;非 root 用户;内联 entrypoint CMD;`EXPOSE 8765` |
| `docker-compose.yml` | 新增 | 端口映射、卷挂载、`TZ=Asia/Shanghai`、`restart: unless-stopped`、healthcheck |
| `.dockerignore` | 新增 | 排除 `.git`/`data`/`config.yaml`/`.env`/`tests`/`docs`/`.venv` 等 |
| `pyproject.toml` | 修改 | 加 `[tool.setuptools.package-data] trae_dashboard = ["static/*"]`(修复 wheel 打包缺前端文件隐患) |
| `README.md` | 修改 | 追加「Docker 一键部署」章节 |

## Dockerfile(设计要点)

- 基础镜像 `python:3.11-slim`(匹配 `requires-python = ">=3.11"`)。
- 先 `COPY pyproject.toml` + `COPY src` 再 `pip install --no-cache-dir .`,利用层缓存。
- 创建非 root 用户(`uid 1000`),`/app/data` 归属该用户(数据卷可写)。
- `ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Shanghai`。
- CMD 内联 entrypoint(见上文「架构」),保证 `docker run` 单独用也能跑。

## docker-compose.yml(设计要点)

```yaml
services:
  trae-dashboard:
    build: .
    image: trae-dashboard:latest
    container_name: trae-dashboard
    ports:
      - "127.0.0.1:8888:8765"      # 仅本机可访问,访问 http://127.0.0.1:8888
    volumes:
      - ./config.yaml:/app/config.yaml   # 可写
      - ./.env:/app/.env                 # 可写
      - trae-dashboard-data:/app/data    # 持久化
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=5).status==200 else sys.exit(1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
volumes:
  trae-dashboard-data:
```

## 使用方式

```bash
# 首次(项目根已有 config.yaml + .env)
docker compose up -d --build     # 构建并启动 → http://127.0.0.1:8888
docker compose logs -f           # 看日志
docker compose down              # 停止(数据保留在卷里)
docker compose up -d             # 再次启动
```

全新机器:`cp config.example.yaml config.yaml` + `cp .env.example .env` 填好后执行上面的命令。

## 测试验证

1. `docker build -t trae-dashboard:latest .` 成功;镜像内 `trae-dashboard --help` 正常。
2. `docker compose up -d` 后:
   - `curl http://127.0.0.1:8888/api/health` 返回 200;
   - `curl http://127.0.0.1:8888/` 返回 index.html(验证 `package-data` 修复生效,前端文件已入镜像);
   - `docker compose logs` 可见启动 fetch 记录与 `--with-scheduler` 调度器。
3. 后端 `python -m pytest tests/ -q` 全量通过(不改后端逻辑,无回归)。
4. 数据持久化:重启容器后数据仍在。

## 明确不做(Out of scope)

- 邮件日报的容器内调度(保持外部 cron / 手动触发,与现状一致)。
- 多节点 / 反向代理 / HTTPS / 对外暴露(仅本机访问)。
- config.py 改为环境变量驱动(方案 B 被否)。
