#!/bin/sh
# 容器入口：数据目录为空时自动构建示例源库（含客户推荐环），再启动 Web
set -eu

DATA_DIR="${SLICE_DATA_DIR:-/data}"
SOURCE_DB="${SLICE_SOURCE_DB:-/data/source.db}"
mkdir -p "$DATA_DIR/slices"

if [ ! -f "$SOURCE_DB" ]; then
  echo "[entrypoint] 未发现源库，构建示例源库: $SOURCE_DB"
  python -I scripts/build_demo_db.py "$SOURCE_DB"
else
  echo "[entrypoint] 使用已有源库: $SOURCE_DB"
fi

echo "[entrypoint] 启动 gunicorn (0.0.0.0:8000)"
exec gunicorn --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-1}" \
  --threads "${GUNICORN_THREADS:-4}" --timeout 120 wsgi:app
