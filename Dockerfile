# 国内构建时可换基础镜像与 pip 源：
#   docker build --build-arg BASE_IMAGE=<ACR 加速地址>/library/python:3.12-slim \
#                --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_INDEX_URL=${PIP_INDEX_URL} PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY fos ./fos
RUN pip install .
COPY alembic.ini ./
COPY migrations ./migrations
COPY tenants ./tenants
COPY packs ./packs
RUN useradd --create-home --uid 10001 fos && mkdir -p /data/blobs && chown fos /data/blobs
ENV FOS_BLOB_DIR=/data/blobs
USER fos
EXPOSE 8000
# 启动前执行数据库迁移（可重复执行）
CMD ["sh", "-c", "fos init-db && exec uvicorn fos.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers"]
