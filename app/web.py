"""Flask 路由：任务提交、进度轮询、行数展示、切片下载、演示写入控制。"""
from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)

from .demo import DemoWriter

bp = Blueprint("main", __name__)


def _manager():
    return current_app.extensions["job_manager"]


def _writer() -> DemoWriter:
    writer = current_app.extensions.get("demo_writer")
    if writer is None:
        writer = DemoWriter(
            current_app.config["SOURCE_DB"],
            interval=float(current_app.config.get("DEMO_INTERVAL", 2.0)),
        )
        current_app.extensions["demo_writer"] = writer
    return writer


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/source")
def source_overview():
    return jsonify(_manager().source_overview())


@bp.post("/api/jobs")
def create_job():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("return_order_id")
    try:
        return_order_id = int(raw)
        if return_order_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return (
            jsonify({"error": "退货单 ID 必须是正整数"}),
            400,
        )
    job = _manager().create_job(return_order_id)
    return jsonify(_manager().to_dict(job)), 201


@bp.get("/api/jobs")
def list_jobs():
    jobs = _manager().list_jobs()
    return jsonify([_manager().to_dict(j) for j in jobs])


@bp.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = _manager().get_job(job_id)
    if job is None:
        abort(404, description="任务不存在")
    return jsonify(_manager().to_dict(job))


@bp.get("/api/jobs/<job_id>/download")
def download(job_id: str):
    manager = _manager()
    job = manager.get_job(job_id)
    if job is None:
        abort(404, description="任务不存在")
    if job.status != "success":
        abort(409, description="任务尚未成功，无切片可下载")
    path = manager.slice_path(job_id)
    if not path.exists():
        # 成功状态与文件不一致：降级并拒绝下载
        manager._update(
            job_id, status="failed", error="切片文件缺失，状态已纠正为失败"
        )
        abort(410, description="切片文件缺失，任务已标记失败")
    return send_file(
        path,
        as_attachment=True,
        download_name=f"return-slice-{job.return_order_id}-{job_id}.db",
        mimetype="application/x-sqlite3",
    )


@bp.post("/api/demo/start")
def demo_start():
    writer = _writer()
    writer.start()
    return jsonify({"running": True, "ticks": writer.ticks})


@bp.post("/api/demo/stop")
def demo_stop():
    writer = _writer()
    writer.stop()
    return jsonify({"running": False, "ticks": writer.ticks})


@bp.post("/api/demo/tick")
def demo_tick():
    writer = _writer()
    n = writer.tick_once()
    return jsonify({"running": writer.running, "ticks": n})


@bp.get("/api/demo/status")
def demo_status():
    writer = _writer()
    return jsonify({"running": writer.running, "ticks": writer.ticks})


@bp.errorhandler(404)
@bp.errorhandler(409)
@bp.errorhandler(410)
def _err(exc):
    return jsonify({"error": exc.description}), exc.code
