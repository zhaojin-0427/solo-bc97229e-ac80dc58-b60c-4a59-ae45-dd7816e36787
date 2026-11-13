"""切片任务管理：状态持久化、后台线程执行、重启恢复。

失败语义（题目要求）
====================
* 缺失引用 / 结构错误 / 执行中断 -> 整次失败，不提供残件
* 成功状态与完整文件保持一致：
  - 重启时 running 的任务一律重置为 failed（中断任务不会“假成功”）
  - success 任务若文件丢失则降级为 failed；若残留 .partial 则清理
  - 目标文件只在全部检查通过后由 os.replace 原子改名产生
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extractor import ExtractionError, run_extraction
from .schema import TABLES, introspect


@dataclass
class Job:
    id: str
    return_order_id: int
    status: str                      # pending/running/success/failed
    created_at: float
    updated_at: float
    progress: dict[str, Any]
    error: str | None
    output_file: str | None
    row_counts: dict[str, int] | None


class JobManager:
    def __init__(
        self,
        data_dir: Path,
        source_db: str,
        batch_size: int = 500,
        max_workers: int = 2,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.slices_dir = self.data_dir / "slices"
        self.source_db = source_db
        self.batch_size = batch_size
        self.max_workers = max(1, max_workers)

        self.db_path = self.data_dir / "jobs.db"
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stop = False
        self._workers: list[threading.Thread] = []

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.slices_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 状态库
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id               TEXT PRIMARY KEY,
                    return_order_id  INTEGER NOT NULL,
                    status           TEXT NOT NULL,
                    created_at       REAL NOT NULL,
                    updated_at       REAL NOT NULL,
                    progress_json    TEXT NOT NULL,
                    error            TEXT,
                    output_file      TEXT,
                    row_counts_json  TEXT
                )
                """
            )

    # ------------------------------------------------------------------ 恢复
    def recover(self) -> None:
        """进程启动时调用：对齐任务状态与磁盘文件。"""
        self.init_db()
        now = time.time()
        with self._connect() as conn:
            # 任何中断在途任务都视为失败，绝不遗留 running 的“僵尸成功”
            conn.execute(
                "UPDATE jobs SET status = 'failed', "
                "error = '服务重启，任务中断，未产出切片', updated_at = ? "
                "WHERE status IN ('pending', 'running')",
                (now,),
            )
            for row in conn.execute("SELECT id, status FROM jobs").fetchall():
                self._reconcile_file(conn, row["id"], row["status"], now)
        # 清理孤儿残件（没有对应任务的 .partial）
        known = {f"slice-{j}.db" for j in self.list_job_ids()}
        for p in self.slices_dir.glob("*.partial"):
            p.unlink(missing_ok=True)
        for p in self.slices_dir.glob("slice-*.db"):
            if p.name not in known:
                p.unlink(missing_ok=True)
        self._start_workers()

    def _reconcile_file(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        status: str,
        now: float,
    ) -> None:
        target = self.slices_dir / f"slice-{job_id}.db"
        partial = self.slices_dir / f"slice-{job_id}.db.partial"
        if partial.exists():
            partial.unlink()
        if status == "success" and not target.exists():
            conn.execute(
                "UPDATE jobs SET status = 'failed', "
                "error = '切片文件缺失（成功状态与文件不一致）', "
                "updated_at = ? WHERE id = ?",
                (now, job_id),
            )
        if status != "success" and target.exists():
            target.unlink()

    # ------------------------------------------------------------------ 任务
    def create_job(self, return_order_id: int) -> Job:
        if not isinstance(return_order_id, int) or return_order_id <= 0:
            raise ValueError("退货单 ID 必须是正整数")
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        progress = {"phase": "queued", "message": "排队等待执行"}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id, return_order_id, status, created_at, "
                "updated_at, progress_json, error, output_file, "
                "row_counts_json) VALUES (?, ?, 'pending', ?, ?, ?, "
                "NULL, NULL, NULL)",
                (
                    job_id,
                    return_order_id,
                    now,
                    now,
                    json.dumps(progress, ensure_ascii=False),
                ),
            )
        with self._wake:
            self._wake.notify_all()
        return self.get_job(job_id)  # type: ignore[return-value]

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def list_job_ids(self) -> list[str]:
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute("SELECT id FROM jobs").fetchall()
            ]

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def slice_path(self, job_id: str) -> Path:
        return self.slices_dir / f"slice-{job_id}.db"

    def to_dict(self, job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "return_order_id": job.return_order_id,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "progress": job.progress,
            "error": job.error,
            "output_file": job.output_file,
            "row_counts": job.row_counts,
            "download": f"/api/jobs/{job.id}/download",
        }

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            return_order_id=row["return_order_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            progress=json.loads(row["progress_json"]),
            error=row["error"],
            output_file=row["output_file"],
            row_counts=(
                json.loads(row["row_counts_json"])
                if row["row_counts_json"]
                else None
            ),
        )

    def _update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        error: str | None = None,
        output_file: str | None = None,
        row_counts: dict[str, int] | None = None,
        clear_error: bool = False,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [time.time()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress_json = ?")
            params.append(json.dumps(progress, ensure_ascii=False))
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if clear_error:
            sets.append("error = NULL")
        if output_file is not None:
            sets.append("output_file = ?")
            params.append(output_file)
        if row_counts is not None:
            sets.append("row_counts_json = ?")
            params.append(json.dumps(row_counts))
        params.append(job_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params
            )

    # ------------------------------------------------------------------ 执行
    def _start_workers(self) -> None:
        for i in range(self.max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"slice-worker-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    def _worker_loop(self) -> None:
        while True:
            job_id = None
            with self._wake:
                while not self._stop:
                    job_id = self._claim_next()
                    if job_id:
                        break
                    self._wake.wait(timeout=5)
                if self._stop:
                    return
            try:
                self._run_job(job_id)
            except Exception as exc:  # pragma: no cover - 兜底
                self._update(
                    job_id,
                    status="failed",
                    error=f"未预期错误: {exc!r}",
                )
                self._fail_cleanup(job_id)

    def _claim_next(self) -> str | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'pending' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            )
            return row["id"]

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        assert job is not None
        last_push = 0.0

        def progress_cb(payload: dict[str, Any]) -> None:
            nonlocal last_push
            now = time.time()
            # 节流：遍历阶段每 0.25 秒最多落库一次，结束/拷贝批次立即落库
            if payload.get("phase") == "traverse" and now - last_push < 0.25:
                return
            last_push = now
            self._update(job_id, progress=payload)

        try:
            schema = introspect(self.source_db)
            output = self.slice_path(job_id)
            result = run_extraction(
                source_db=self.source_db,
                schema=schema,
                return_order_id=job.return_order_id,
                output_path=output,
                batch_size=self.batch_size,
                progress=progress_cb,
            )
            self._update(
                job_id,
                status="success",
                progress={
                    "phase": "done",
                    "message": "切片已生成并通过外键检查",
                },
                output_file=str(result.output_path),
                row_counts=result.row_counts,
                clear_error=True,
            )
        except ExtractionError as exc:
            self._fail(job_id, str(exc))
        except sqlite3.Error as exc:
            self._fail(job_id, f"源库/SQL 错误（整次失败，无残件）: {exc}")
        except Exception as exc:
            self._fail(job_id, f"执行中断: {exc!r}")

    def _fail(self, job_id: str, message: str) -> None:
        self._update(job_id, status="failed", error=message)
        self._fail_cleanup(job_id)

    def _fail_cleanup(self, job_id: str) -> None:
        self.slice_path(job_id).unlink(missing_ok=True)
        self.slices_dir.joinpath(
            f"slice-{job_id}.db.partial"
        ).unlink(missing_ok=True)

    def shutdown(self) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify_all()
        for t in self._workers:
            t.join(timeout=5)

    def source_overview(self) -> dict[str, Any]:
        """读取源库各表行数（页面展示用，快照外的普通只读查询）。"""
        from .schema import TABLE_LABELS

        counts: dict[str, Any] = {}
        try:
            conn = sqlite3.connect(
                f"file:{self.source_db}?mode=ro", uri=True, timeout=10
            )
            try:
                for table in TABLES:
                    counts[table] = {
                        "label": TABLE_LABELS[table],
                        "rows": conn.execute(
                            f'SELECT count(*) FROM "{table}"'
                        ).fetchone()[0],
                    }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return {"available": False, "error": str(exc), "tables": counts}
        return {"available": True, "tables": counts}
