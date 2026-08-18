# traeDashboard Docker 一键部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 traeDashboard 改造成 Docker 项目,支持本机 Docker Desktop 一键部署(`docker compose up -d --build`),访问 `http://127.0.0.1:8888`,容器内 `serve --with-scheduler` 自动采集,config.yaml/.env 可写挂载、SQLite 数据卷持久化。

**Architecture:** 新增 `Dockerfile`(`python:3.11-slim` + `pip install .`)与 `docker-compose.yml`(端口 `127.0.0.1:8888:8765` 仅本机访问;config.yaml/.env bind mount 可写;`trae-dashboard-data` named volume 持久化)。修复 `pyproject.toml` 缺少 `package-data` 的隐患(wheel 不打前端文件,Docker 里 `pip install .` 后页面会打不开)。

**Tech Stack:** Docker, Docker Compose, Python 3.11, setuptools package-data, FastAPI(现状,不改后端逻辑)。

**Spec reference:** `docs/superpowers/specs/2026-08-13-docker-deployment-design.md`

---

## File Structure

**Create:**
- `Dockerfile` — 镜像定义 + 内联 entrypoint CMD(fetch 一次 → serve --with-scheduler)
- `docker-compose.yml` — 一键编排(端口/卷/环境/重启/健康检查)
- `.dockerignore` — 减小构建上下文,排除密钥/数据/测试等
- `tests/test_packaging.py` — 验证 wheel 内包含 static 前端文件

**Modify:**
- `pyproject.toml` — 新增 `[tool.setuptools.package-data] trae_dashboard = ["static/*"]`
- `README.md` — 末尾追加「Docker 一键部署」章节

---

## Task 1: pyproject.toml — 修复 package-data(Docker 依赖此修复)

**Files:**
- Modify: `pyproject.toml`(在 `[tool.setuptools.packages.find]` 段之后追加)
- Test: `tests/test_packaging.py`(新建)

**背景:** 当前 `pyproject.toml` 没有声明 package-data,`pip install .` 构建的 wheel 不含 `src/trae_dashboard/static/`。本地源码跑没问题(PYTHONPATH 直接指向 src),但 Docker 镜像靠 `pip install .` 装包,前端文件缺失会导致页面 404。此任务用测试锁定"wheel 必须包含 static 文件"。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_packaging.py`(完整内容):

```python
"""Packaging tests: the wheel must ship static assets.

Docker relies on ``pip install .``, so the frontend (static/index.html,
app.js, style.css) must be inside the built wheel. This guards against
accidentally dropping the package-data declaration.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _build_wheel(dist: Path) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(dist),
        ],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted(dist.glob("*.whl"))
    assert wheels, "no wheel was built"
    return wheels[0]


def test_wheel_ships_static_frontend(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path / "dist")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    assert any(n.endswith("trae_dashboard/static/index.html") for n in names)
    assert any(n.endswith("trae_dashboard/static/app.js") for n in names)
    assert any(n.endswith("trae_dashboard/static/style.css") for n in names)
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `python -m pytest tests/test_packaging.py -v`
Expected: FAIL — `assert any(...)` 报错,说明 wheel 里**没有** `trae_dashboard/static/index.html`。
(若提示缺 `wheel` 包:先 `pip install wheel` 再跑。)

- [ ] **Step 3: 修改 pyproject.toml**

在 `[tool.setuptools.packages.find]` 段(第 32-33 行)之后追加:

```toml
[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
trae_dashboard = ["static/*"]
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `python -m pytest tests/test_packaging.py -v`
Expected: PASS(1 passed)。wheel 内已包含三个前端文件。

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml tests/test_packaging.py
git commit -m "fix(packaging): ship static assets in wheel via package-data"
```

---

## Task 2: 新增 .dockerignore

**Files:**
- Create: `.dockerignore`

**背景:** Docker 构建上下文默认是整个项目。排除密钥(`config.yaml`/`.env`)、运行时数据(`data/`)、测试/文档、构建产物,既减小上下文、也避免把密钥烧进镜像层。

- [ ] **Step 1: 写 .dockerignore**

创建 `.dockerignore`(完整内容):

```
# VCS
.git
.gitignore

# 本地密钥/配置(由 compose bind mount 提供,不烧进镜像)
config.yaml
.env
*.env

# 运行时数据(SQLite 走 named volume)
data

# 测试与文档(镜像不需要)
tests
docs

# Python 构建产物
.venv
venv
__pycache__
*.py[cod]
*.egg-info
build
dist
.pytest_cache
.coverage
*.log

# IDE / agent 状态
.idea
.vscode
.agents
.claude
.trae
.superpowers
```

- [ ] **Step 2: 验证文件就位**

Run: `git status --short`
Expected: `.dockerignore` 显示为 untracked 新文件。
(真正的"上下文减小"验证在 Task 6 的 `docker build` 输出中可见。)

- [ ] **Step 3: 提交**

```bash
git add .dockerignore
git commit -m "chore(docker): add .dockerignore to slim build context"
```

---

## Task 3: 新增 Dockerfile

**Files:**
- Create: `Dockerfile`

**背景:** 镜像 `python:3.11-slim`(匹配 `requires-python = ">=3.11"`)。`pip install .` 安装已打好的 wheel(依赖 Task 1 的 package-data 修复)。非 root 用户运行,`/app/data` 归属该用户以写 named volume。CMD 内联 entrypoint:先尽力 fetch 一次,再 `serve --with-scheduler`。

- [ ] **Step 1: 检查 Docker 是否可用**

Run: `docker --version; docker compose version`
Expected: 两行均打印版本号。
若提示 `command not found`,**不要跳过**本任务(文件照写),把 Task 3 Step 3 / Task 6 的 docker 命令记为"用户在本机 Docker Desktop 上手动验证"。

- [ ] **Step 2: 写 Dockerfile**

创建 `Dockerfile`(完整内容):

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 1) 先装依赖(源码变更可复用这一层缓存)
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# 2) 非 root 运行用户;/app/data 是 SQLite 数据卷(named volume)
RUN useradd --create-home --uid 1000 trae \
    && mkdir -p /app/data \
    && chown -R trae:trae /app
USER trae

EXPOSE 8765

# 3) 启动时先拉一次数据(尽力而为,失败只打日志交给调度器重试),
#    再以 --with-scheduler 启动 Web 服务。
#    容器内绑 0.0.0.0;外部访问由 compose 收敛到 127.0.0.1:8888。
CMD ["sh", "-c", "trae-dashboard fetch --config /app/config.yaml || echo '[docker] initial fetch failed (will retry on scheduler interval)'; exec trae-dashboard serve --config /app/config.yaml --host 0.0.0.0 --port 8765 --with-scheduler"]
```

- [ ] **Step 3: 构建镜像**

Run: `docker build -t trae-dashboard:latest .`
Expected: 构建成功,最后输出 `Successfully tagged trae-dashboard:latest`。构建输出中 `.dockerignore` 应使 context 很小(如 `Transferring context: xxMB`)。
(若 Docker 不可用,此步留给用户手动执行。)

- [ ] **Step 4: 冒烟验证镜像内 CLI**

Run: `docker run --rm trae-dashboard:latest trae-dashboard --help`
Expected: 打印 CLI 帮助(init/fetch/serve/prune/report 子命令),退出码 0。

- [ ] **Step 5: 提交**

```bash
git add Dockerfile
git commit -m "feat(docker): add Dockerfile for containerized deployment"
```

---

## Task 4: 新增 docker-compose.yml

**Files:**
- Create: `docker-compose.yml`

**背景:** 一键编排。端口 `127.0.0.1:8888:8765` 仅本机访问;config.yaml/.env bind mount **可写**(面板「账号管理 / 邮件设置」写回功能照常生效);`trae-dashboard-data` named volume 持久化;`TZ=Asia/Shanghai`;`restart: unless-stopped`;python urllib 健康检查 `GET /api/health`。

- [ ] **Step 1: 写 docker-compose.yml**

创建 `docker-compose.yml`(完整内容):

```yaml
services:
  trae-dashboard:
    build: .
    image: trae-dashboard:latest
    container_name: trae-dashboard
    ports:
      - "127.0.0.1:8888:8765"      # 仅本机可访问 → http://127.0.0.1:8888
    volumes:
      - ./config.yaml:/app/config.yaml   # 可写,面板可管理
      - ./.env:/app/.env                 # 可写,SMTP 密码保存
      - trae-dashboard-data:/app/data    # SQLite 持久化
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=5)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  trae-dashboard-data:
```

- [ ] **Step 2: 校验 compose 语法**

Run: `docker compose config --quiet`
Expected: 无输出、退出码 0(compose 文件合法)。
若提示 `command not found`:同样记为手动验证。

- [ ] **Step 3: 提交**

```bash
git add docker-compose.yml
git commit -m "feat(docker): add docker-compose one-click deployment"
```

---

## Task 5: README 追加 Docker 章节

**Files:**
- Modify: `README.md`(文件末尾追加)

- [ ] **Step 1: 追加章节**

在 `README.md` **末尾**追加以下内容(用 `<<'EOF'` heredoc 或 Write/Edit 追加;注意章节内部自带 fenced code block,是文档内容的一部分):

````markdown
## Docker 一键部署

> 需要本机装有 Docker Desktop(Windows/macOS)或 Docker Engine(Linux)。

**前置:** 项目根目录先备好 `config.yaml` 和 `.env`(见「快速开始」第 2/3 步;没有的话先
`cp config.example.yaml config.yaml`、`cp .env.example .env` 再填好)。

```bash
# 一键构建并启动 → http://127.0.0.1:8888
docker compose up -d --build

docker compose logs -f     # 看日志
docker compose down        # 停止(数据保留在 named volume 里)
docker compose up -d       # 再次启动
```

**行为说明:**

- 容器启动时先自动拉一次数据(`fetch`),之后按 `fetch_interval_minutes` 后台定时采集(`--with-scheduler`)。
- `config.yaml` / `.env` 直接挂载自宿主机,**可写** —— 面板里的「账号管理 / 邮件设置」写回功能在容器内照常生效。
- SQLite 数据存在 named volume `trae-dashboard-data` 里,`docker compose down` 不丢。
- 端口映射 `127.0.0.1:8888:8765`:仅本机可访问(容器内绑 0.0.0.0,外部由 compose 收敛到本机)。
- 邮件日报**不在**容器内调度,保持外部 cron / 手动触发(与源码部署一致)。

> Linux 主机小提示:若面板写回配置时遇到权限问题(挂载文件属主是 root),执行
> `sudo chown 1000:1000 config.yaml .env` 后重启容器即可(Docker Desktop 一般无此问题)。
````

- [ ] **Step 2: 验证追加成功**

Run: `Select-String -Path README.md -Pattern "Docker 一键部署"`
Expected: 命中新增章节标题行。

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs(readme): add docker one-click deployment section"
```

---

## Task 6: 全量验证(后端回归 + 容器端到端)

**Files:** 无改动,纯验证。若 Docker 不可用,容器相关步骤由用户在本机手动执行。

- [ ] **Step 1: 后端测试回归**

Run: `python -m pytest tests/ -q`
Expected: 全部 PASS(原 135 + 新增 1 个 packaging 测试 = 136)。

- [ ] **Step 2: 重新构建镜像(确认最新代码)**

Run: `docker build -t trae-dashboard:latest .`
Expected: `Successfully tagged trae-dashboard:latest`。

- [ ] **Step 3: 一键启动**

Run: `docker compose up -d --build`
Expected: 容器 `trae-dashboard` 创建并进入 running 状态。

- [ ] **Step 4: 健康检查**

Run: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8888/api/health`
Expected: `200`。

- [ ] **Step 5: 验证前端文件已入镜像(package-data 修复生效)**

Run: `curl -s http://127.0.0.1:8888/ | Select-String -Pattern "trae-dashboard"`
Expected: 返回 HTML 且含 `trae-dashboard`(title/元素),说明 index.html 被 StaticFiles 正常提供,前端文件确实在 wheel 里。

- [ ] **Step 6: 验证调度器启动**

Run: `docker compose logs --tail 30 trae-dashboard`
Expected: 日志含 `Application startup complete`、`Uvicorn running on http://0.0.0.0:8765`,以及启动 fetch 记录或 `[docker] initial fetch failed` 提示;随后有调度器日志(`collector run:` 或 `collector run failed`)。

- [ ] **Step 7: 验证数据持久化**

Run:
```powershell
docker compose restart
Start-Sleep -Seconds 5
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8888/api/health
docker compose exec trae-dashboard sh -c "ls -la /app/data"
```
Expected: 健康检查仍 `200`;`/app/data/dashboard.db` 存在且非空(重启后数据保留)。

- [ ] **Step 8: 收尾**

Run: `docker compose down`
Expected: 容器停止,`trae-dashboard-data` 卷保留(数据未丢)。告知用户以后 `docker compose up -d` 即可再次启动。

---

## Self-Review Notes

- Spec §需求 1-6 全部有对应任务:Web+采集(CMD --with-scheduler,Task 3)、本机 Docker Desktop(Task 4 端口绑定)、8888(Task 4)、config/.env 可写挂载(Task 4)、数据卷持久化(Task 4 + Task 6 Step 7)。
- 无占位符;每个代码/文档步骤都给出完整内容。
- 类型/命令一致性:`trae-dashboard fetch/serve`、端口 8765(容器内)/8888(宿主)、`trae-dashboard-data` 卷名在 Task 3/4/6 保持一致。
- 明确不做:邮件日报容器内调度(README 章节注明)、HTTPS/反向代理/对外暴露、config 环境变量化(均在 spec Out-of-scope)。
