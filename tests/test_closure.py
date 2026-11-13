"""核心语义测试：精确闭包、循环引用、不反向带入、完整性。"""
from __future__ import annotations

import sqlite3

from app.extractor import run_extraction
from app.schema import TABLES


def _open(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _ids(conn, table):
    return {r[0] for r in conn.execute(f'SELECT id FROM "{table}"')}


def test_exact_closure_for_return_order_1(source_db, schema, out_path):
    """退货单 1 的切片恰好是种子 + 正向外键闭包，不多不少。"""
    result = run_extraction(
        str(source_db), schema, 1, out_path, batch_size=2
    )

    with _open(out_path) as conn:
        # 外键检查必须通过（含客户环）
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        assert _ids(conn, "return_orders") == {1}          # 不带兄弟退货单 2/3
        assert _ids(conn, "return_lines") == {1}           # 只带退货单 1 的明细
        assert _ids(conn, "orders") == {1}
        assert _ids(conn, "order_lines") == {1}            # 不带未退货的行 2/3
        assert _ids(conn, "products") == {1}
        assert _ids(conn, "customers") == {1, 2}           # 客户环 1<->2

    expected = {
        "return_orders": 1,
        "return_lines": 1,
        "orders": 1,
        "order_lines": 1,
        "customers": 2,
        "products": 1,
    }
    assert result.row_counts == expected
    assert result.total_rows == 7


def test_cyclic_self_reference_included(source_db, schema, out_path):
    """退货单 3 -> 客户 2 -> 客户 1，NOCASE 业务码两环全部保留且外键检查通过。"""
    run_extraction(str(source_db), schema, 3, out_path)
    with _open(out_path) as conn:
        assert _ids(conn, "customers") == {1, 2}
        rows = {
            r["id"]: r["referred_by_code"]
            for r in conn.execute("SELECT id, referred_by_code FROM customers")
        }
        # 环结构原值保留：大小写差异不改写（'beta' / 'ALPHA'）
        assert rows == {1: "beta", 2: "ALPHA"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_self_loop_customer_3(source_db, schema, tmp_path):
    """构造客户自环场景：c3.referred_by_code = 'gamma'（NOCASE 匹配 'Gamma'）。"""
    # 让一张退货单走到客户 3：临时挂一条订单/退货单
    conn = sqlite3.connect(str(source_db))
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute(
            "INSERT INTO orders(id, customer_id, order_no, status) "
            "VALUES (10, 3, 'SO-X', 'paid')"
        )
        conn.execute(
            "INSERT INTO return_orders(id, return_no, order_id, "
            "customer_id, reason, status) "
            "VALUES (10, '710', 10, 3, 'x', 'approved')"
        )
        conn.execute(
            "INSERT INTO return_lines(id, return_order_id, order_line_id, "
            "product_id, quantity) VALUES (10, 710, 1, 1, 1)"
        )
    conn.close()

    out = tmp_path / "s3.db"
    run_extraction(str(source_db), schema, 10, out)
    with _open(out) as c:
        # 自环客户 3 必在；return_line 复用 order_line 1，会沿其订单 1
        # 正向带入客户 1、2（两环），这是外键闭包的正确结果
        assert {3, 1, 2} <= _ids(c, "customers")
        assert c.execute(
            "SELECT referred_by_code FROM customers WHERE id = 3"
        ).fetchone()[0] == "gamma"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_no_reverse_pull_of_sibling_returns(source_db, schema, out_path):
    """同订单的兄弟退货单及其明细绝不能进入切片。"""
    run_extraction(str(source_db), schema, 1, out_path)
    with _open(out_path) as conn:
        assert _ids(conn, "return_orders") == {1}
        assert _ids(conn, "return_lines") == {1}
        # 订单行 2 仅被兄弟退货单 2 引用，不能反向带入
        assert 2 not in _ids(conn, "order_lines")


def test_schema_preserved_verbatim(source_db, schema, out_path):
    """字段、主键、外键定义必须原样保留（CREATE 语句一致）。"""
    run_extraction(str(source_db), schema, 1, out_path)
    src = _open(source_db)
    dst = _open(out_path)
    for table in TABLES:
        s_sql = src.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
        d_sql = dst.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
        assert s_sql == d_sql
        # 列定义一致
        s_cols = [(r[1], r[2], r[3], r[5]) for r in src.execute(
            f'PRAGMA table_info("{table}")'
        )]
        d_cols = [(r[1], r[2], r[3], r[5]) for r in dst.execute(
            f'PRAGMA table_info("{table}")'
        )]
        assert s_cols == d_cols
        # 外键定义一致
        assert src.execute(
            f'PRAGMA foreign_key_list("{table}")'
        ).fetchall() == dst.execute(
            f'PRAGMA foreign_key_list("{table}")'
        ).fetchall()
    src.close()
    dst.close()


def test_original_values_preserved(source_db, schema, out_path):
    run_extraction(str(source_db), schema, 1, out_path)
    with _open(out_path) as conn:
        row = conn.execute(
            "SELECT * FROM return_orders WHERE id = 1"
        ).fetchone()
        assert row["reason"] == "耳机质量问题"
        assert row["status"] == "approved"
