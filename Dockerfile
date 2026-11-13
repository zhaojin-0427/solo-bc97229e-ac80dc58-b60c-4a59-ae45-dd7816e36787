FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SLICE_DATA_DIR=/data \
    SLICE_SOURCE_DB=/data/source.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x scripts/entrypoint.sh && mkdir -p /data/slices

# 非 root 运行；TMPDIR 指向可写目录，磁盘临时表（BFS 状态）落此处
ENV TMPDIR=/tmp
RUN useradd --create-home --uid 10001 appuser \
  && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/source', timeout=4).status==200 else 1)"

ENTRYPOINT ["scripts/entrypoint.sh"]
