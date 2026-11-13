"""重启恢复与失败原子性（JobManager 层）。"""
from __future__ import annotations

import time
from pathlib import Path

from app.demo import build_demo_db
from app.jobs import JobManager


def _new_manager(tmp_path: Path, source: Path) -> JobManager:
    return JobManager(
        data_dir=tmp_path / "data",
        source_db=str(source),
        batch_size=10,
        max_workers=1,
    )


def _wait_status(manager: JobManager, job_id: str, expected: set[str]) -> dict:
    for _ in range(200):
        job = manager.get_job(job_id)
        if job.status in expected:
            return job
        time.sleep(0.05)
    raise AssertionError(
        f"任务未在预期时间内到达 {expected}，当前 {manager.get_job(job_id)}"
    )


def test_full_job_lifecycle_and_restart_success(tmp_path):
    source = build_demo_db(tmp_path / "source.db")
    manager = _new_manager(tmp_path, source)
    manager.recover()
    job = manager.create_job(1)
    done = _wait_status(manager, job.id, {"success", "failed"})
    assert done.status == "success"
    slice_file = manager.slice_path(job.id)
    assert slice_file.exists()
    assert done.row_counts["return_orders"] == 1
    manager.shutdown()

    # 重启：成功状态与完整文件保持一致
    manager2 = _new_manager(tmp_path, source)
    manager2.recover()
    again = manager2.get_job(job.id)
    assert again.status == "success"
    assert slice_file.exists()
    manager2.shutdown()


def test_interrupted_job_marked_failed_without_residual(tmp_path):
    source = build_demo_db(tmp_path / "source.db")
    manager = _new_manager(tmp_path, source)
    # 不启动 worker，仅初始化状态库并手工构造“执行中进程被杀死”的现场
    manager.init_db()
    import json
    import time as _time
    import uuid

    job_id = uuid.uuid4().hex[:16]
    now = _time.time()
    with manager._connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id, return_order_id, status, created_at, "
            "updated_at, progress_json, error, output_file, "
            "row_counts_json) VALUES (?, 1, 'running', ?, ?, ?, "
            "NULL, NULL, NULL)",
            (job_id, now, now, json.dumps({"phase": "copy"})),
        )
    partial = manager.slices_dir / f"slice-{job_id}.db.partial"
    partial.write_bytes(b"not a real sqlite db")

    manager2 = _new_manager(tmp_path, source)
    manager2.recover()
    recovered = manager2.get_job(job_id)
    assert recovered.status == "failed"
    assert "中断" in (recovered.error or "")
    assert not partial.exists()
    assert not manager2.slice_path(job_id).exists()
    manager2.shutdown()


def test_success_with_missing_file_downgraded(tmp_path):
    source = build_demo_db(tmp_path / "source.db")
    manager = _new_manager(tmp_path, source)
    manager.recover()
    job = manager.create_job(1)
    _wait_status(manager, job.id, {"success"})
    target = manager.slice_path(job.id)
    target.unlink()  # 模拟文件丢失
    manager.shutdown()

    manager2 = _new_manager(tmp_path, source)
    manager2.recover()
    job2 = manager2.get_job(job.id)
    assert job2.status == "failed"
    assert "文件缺失" in (job2.error or "")
    manager2.shutdown()


def test_bad_input_rejected(tmp_path):
    source = build_demo_db(tmp_path / "source.db")
    manager = _new_manager(tmp_path, source)
    manager.recover()
    for bad in (0, -1, "x"):
        try:
            manager.create_job(bad)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"非法输入未被拒绝: {bad}")
    manager.shutdown()
