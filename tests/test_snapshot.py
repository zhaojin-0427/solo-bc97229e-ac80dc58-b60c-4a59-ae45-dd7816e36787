"""并发与一致性：抽取期间持续增删改，切片仍来自同一快照。"""
from __future__ import annotations

import sqlite3
import threading

from app.demo import DemoWriter
from app.extractor import run_extraction


def test_snapshot_isolation_under_concurrent_writes(
    source_db, schema, out_path
):
    """快照固定后写入器疯狂增删改，切片内容与快照时刻完全一致。"""
    writer = DemoWriter(str(source_db), interval=0.05)
    fired = threading.Event()

    def after_snapshot():
        # 快照读标记固定后才启动写入，保证写入都发生在遍历/拷贝期间
        writer.start()
        for _ in range(20):
            writer.tick_once()
        fired.set()

    result = run_extraction(
        str(source_db),
        schema,
        1,
        out_path,
        batch_size=1,  # 极小批次，尽量拉长遍历窗口
        on_snapshot=after_snapshot,
    )
    fired.wait(5)
    writer.stop()

    # 源库已被写入器改动（出现 900x 演示单据或状态变化）
    live = sqlite3.connect(str(source_db))
    demo_rows = live.execute(
        "SELECT count(*) FROM return_orders WHERE id >= 9000"
    ).fetchone()[0]
    live.close()
    assert demo_rows >= 1

    # 切片只含快照时刻的退货单 1，看不到任何演示单据
    dst = sqlite3.connect(str(out_path))
    try:
        assert dst.execute(
            "PRAGMA foreign_key_check"
        ).fetchall() == []
        ids = {
            r[0]
            for r in dst.execute("SELECT id FROM return_orders")
        }
        assert ids == {1}
        assert result.row_counts["return_orders"] == 1
        # 快照时刻客户只有 1..4，切片不允许出现后来的任何变化
        assert dst.execute(
            "SELECT count(*) FROM return_orders WHERE id >= 9000"
        ).fetchone()[0] == 0
    finally:
        dst.close()


def test_source_wal_mode_supported(source_db):
    conn = sqlite3.connect(str(source_db))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_repeated_extractions_identical(source_db, schema, tmp_path):
    """同一源库、同一退货单的两次切片逐字节结构一致、行数一致。"""
    out1 = tmp_path / "a.db"
    out2 = tmp_path / "b.db"
    r1 = run_extraction(str(source_db), schema, 1, out1)
    r2 = run_extraction(str(source_db), schema, 1, out2)
    assert r1.row_counts == r2.row_counts

    def dump(path):
        c = sqlite3.connect(str(path))
        lines = []
        for (sql,) in c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "ORDER BY tbl_name, type"
        ):
            lines.append(sql)
        for table in (
            "return_orders",
            "return_lines",
            "orders",
            "order_lines",
            "customers",
            "products",
        ):
            rows = c.execute(
                f'SELECT * FROM "{table}" ORDER BY id'
            ).fetchall()
            lines.append((table, rows))
        c.close()
        return lines

    assert dump(out1) == dump(out2)
