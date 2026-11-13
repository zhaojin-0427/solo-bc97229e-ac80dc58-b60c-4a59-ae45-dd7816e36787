"""gunicorn 入口（Docker 生产方式）。"""
from app import create_app

app = create_app()
