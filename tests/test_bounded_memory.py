"""有界内存测试：超大闭包下进程内存不随选中 ID 数量线性增长。

遍历状态全部存放于 SQLite 磁盘临时表，Python 侧每批只保留 BATCH_SIZE 行。
用 tracemalloc 对比“小闭包”和“大闭包”两次抽取的 Python 峰值增量。
"""
from __future__ import annotations

import sqlite3
import tracemalloc

from app.extractor import run_extraction
from app.schema import introspect


def _build_large_db(path: str, n_customers: int, n_lines: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                referred_by INTEGER REFERENCES customers(id)
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, sku TEXT NOT NULL,
                name TEXT NOT NULL, price_cents INTEGER NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL
                REFERENCES customers(id), order_no TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE order_lines (
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL
                REFERENCES orders(id), product_id INTEGER NOT NULL
                REFERENCES products(id), quantity INTEGER NOT NULL
            );
            CREATE TABLE return_orders (
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL
                REFERENCES orders(id), customer_id INTEGER NOT NULL
                REFERENCES customers(id), reason TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE return_lines (
                id INTEGER PRIMARY KEY, return_order_id INTEGER NOT NULL
                REFERENCES return_orders(id), order_line_id INTEGER NOT NULL
                REFERENCES order_lines(id), product_id INTEGER NOT NULL
                REFERENCES products(id), quantity INTEGER NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO customers(id, name, referred_by) VALUES (?, ?, ?)",
            # 首尾成环的推荐链：1->2->...->n->1
            [(i, f"c{i}", i - 1 if i > 1 else n_customers)
             for i in range(1, n_customers + 1)],
        )
        conn.executemany(
            "INSERT INTO products(id, sku, name, price_cents) "
            "VALUES (?, ?, ?, ?)",
            [(i, f"SKU-{i}", f"p{i}", 1000) for i in range(1, n_lines + 1)],
        )
        # 每张订单一个客户，退货单 1 的明细引用所有订单行 -> 闭包覆盖全量
        conn.executemany(
            "INSERT INTO orders(id, customer_id, order_no, status) "
            "VALUES (?, ?, ?, 'paid')",
            [(i, i, f"SO-{i}") for i in range(1, n_lines + 1)],
        )
        conn.executemany(
            "INSERT INTO order_lines(id, order_id, product_id, quantity) "
            "VALUES (?, ?, ?, 1)",
            [(i, i, i) for i in range(1, n_lines + 1)],
        )
        conn.execute(
            "INSERT INTO return_orders(id, order_id, customer_id, reason, "
            "status) VALUES (1, 1, 1, 'bulk', 'approved')"
        )
        conn.executemany(
            "INSERT INTO return_lines(id, return_order_id, order_line_id, "
            "product_id, quantity) VALUES (?, 1, ?, ?, 1)",
            [(i, i, i) for i in range(1, n_lines + 1)],
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()
    finally:
        conn.close()


def test_memory_does_not_grow_with_closure_size(tmp_path):
    small = tmp_path / "small.db"
    large = tmp_path / "large.db"
    # 小库：1000 个客户/行；大库：40 倍节点（含 4 万客户长环）
    _build_large_db(str(small), 1000, 1000)
    _build_large_db(str(large), 40000, 10000)

    def measure(db_path: str) -> tuple[int, int]:
        schema = introspect(db_path)
        out = tmp_path / f"out-{db_path.split('/')[-1]}"
        tracemalloc.start()
        result = run_extraction(
            db_path, schema, 1, out, batch_size=200
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak, result.total_rows

    peak_small, rows_small = measure(str(small))
    peak_large, rows_large = measure(str(large))

    assert rows_large > rows_small * 10
    # 选中记录扩大 10 倍以上，Python 侧峰值增量不得超过 8 MB
    # （遍历状态在磁盘临时表，进程只持有当前批次）
    assert peak_large - peak_small < 8 * 1024 * 1024, (
        peak_small,
        peak_large,
    )

    # 大切片本身完整且通过外键检查
    conn = sqlite3.connect(str(tmp_path / "out-large.db"))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT count(*) FROM return_lines"
        ).fetchone()[0] == 10000
        assert conn.execute(
            "SELECT count(*) FROM customers"
        ).fetchone()[0] == 40000
    finally:
        conn.close()
