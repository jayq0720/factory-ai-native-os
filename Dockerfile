FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY fos ./fos
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
COPY tenants ./tenants
COPY packs ./packs
RUN useradd --create-home --uid 10001 fos && mkdir -p /data/blobs && chown fos /data/blobs
ENV FOS_BLOB_DIR=/data/blobs
USER fos
EXPOSE 8000
CMD ["uvicorn", "fos.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
