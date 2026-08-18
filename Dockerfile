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
