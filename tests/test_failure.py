"""失败语义：缺失引用、种子不存在 -> 整次失败、无残件。"""
from __future__ import annotations

import pytest

from app.extractor import MissingReferenceError, run_extraction
from app.schema import introspect


def test_seed_return_order_missing(source_db, schema, out_path):
    with pytest.raises(MissingReferenceError, match="种子退货单不存在"):
        run_extraction(str(source_db), schema, 404, out_path)
    assert not out_path.exists()
    assert not out_path.with_name(out_path.name + ".partial").exists()


def test_missing_fk_reference_fails_atomically(orphan_db, out_path):
    """断引用源库：抽取退货单 1 的过程中遇到缺失目标，整体失败、无残件。"""
    schema = introspect(str(orphan_db))
    # 退货单 99 自身断引用；另构造一个让闭包遍历阶段才发现断引用的场景
    with pytest.raises(MissingReferenceError):
        run_extraction(str(orphan_db), schema, 99, out_path)
    assert not out_path.exists()
    assert not out_path.with_name(out_path.name + ".partial").exists()


def test_missing_fk_discovered_during_traversal(tmp_path, out_path):
    """先建合法库，再在非种子路径上制造断引用，验证遍历阶段的缺失检查。"""
    from app.demo import build_demo_db
    import sqlite3

    db = build_demo_db(tmp_path / "src2.db")
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = OFF")  # 故意写入脏数据
    with conn:
        # 退货单 1 指向不存在的客户 777
        conn.execute(
            "UPDATE return_orders SET customer_id = 777 WHERE id = 1"
        )
    conn.close()

    schema = introspect(str(db))
    with pytest.raises(MissingReferenceError, match="customers.id = 777"):
        run_extraction(str(db), schema, 1, out_path)
    assert not out_path.exists()


def test_bad_schema_rejected(tmp_path):
    """缺少必需表时结构校验失败。"""
    import sqlite3

    db = tmp_path / "bad.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    conn.close()
    from app.schema import SchemaError

    with pytest.raises(SchemaError):
        introspect(str(db))
