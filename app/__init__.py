"""售后退货单 SQLite 切片服务。

模块划分：
- schema.py:    源库结构内省（六表、整数主键、单列外键）
- extractor.py: 快照内 BFS 闭包遍历 + 流式拷贝（核心算法）
- jobs.py:      任务状态库、后台执行、崩溃恢复
- demo.py:      示例源库构造（含客户推荐环）与持续写入模拟器
- web.py:       Flask 路由与 JSON API
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .jobs import JobManager


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SOURCE_DB"] = os.environ.get(
        "SLICE_SOURCE_DB", "/data/source.db"
    )
    app.config["DATA_DIR"] = os.environ.get("SLICE_DATA_DIR", "/data")
    app.config["BATCH_SIZE"] = int(os.environ.get("SLICE_BATCH_SIZE", "500"))
    app.config["MAX_WORKERS"] = int(os.environ.get("SLICE_MAX_WORKERS", "2"))
    if config:
        app.config.update(config)

    data_dir = Path(app.config["DATA_DIR"])
    slices_dir = data_dir / "slices"
    data_dir.mkdir(parents=True, exist_ok=True)
    slices_dir.mkdir(parents=True, exist_ok=True)

    manager = JobManager(
        data_dir=data_dir,
        source_db=app.config["SOURCE_DB"],
        batch_size=app.config["BATCH_SIZE"],
        max_workers=app.config["MAX_WORKERS"],
    )
    # 启动恢复：中断任务置失败、清残件、成功状态与文件对齐
    manager.recover()
    app.extensions["job_manager"] = manager

    from .web import bp

    app.register_blueprint(bp)
    return app
