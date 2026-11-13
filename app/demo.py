"""本地示例源库与持续修改模拟器。

示例库（WAL 模式，结构与生产源库一致）：
- 客户表含循环推荐关系：c1 -> c2 -> c1（两环）、c3 -> c3（自环）
- 同一订单挂两张退货单，用于证明不会反向带入兄弟退货单
- 订单含未退货的订单行，用于证明不反向带入其它明细

写入器每个 tick 在单个事务里做 插入 -> 修改 -> 删除，
用于演示抽取期间源库持续增删改而切片仍来自同一快照。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    referred_by INTEGER REFERENCES customers(id)
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_no TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE order_lines (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL
);
CREATE TABLE return_orders (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE return_lines (
    id INTEGER PRIMARY KEY,
    return_order_id INTEGER NOT NULL REFERENCES return_orders(id),
    order_line_id INTEGER NOT NULL REFERENCES order_lines(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_order_lines_order ON order_lines(order_id);
CREATE INDEX idx_return_orders_order ON return_orders(order_id);
CREATE INDEX idx_return_lines_return ON return_lines(return_order_id);
"""


def build_demo_db(path: str | Path, *, with_orphan: bool = False) -> Path:
    """创建示例源库。with_orphan=True 时植入一条断引用（失败演示用）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(str(path))
    try:
        # 建表阶段先关外键：客户推荐关系含前向引用与环，无法按插入顺序满足
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO customers(id, name, referred_by) VALUES (?, ?, ?)",
            [
                (1, "张三", 2),
                (2, "李四", 1),   # 1 <-> 2 两环
                (3, "王五", 3),   # 自环
                (4, "赵六", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO products(id, sku, name, price_cents) "
            "VALUES (?, ?, ?, ?)",
            [
                (1, "SKU-A", "蓝牙耳机", 29900),
                (2, "SKU-B", "机械键盘", 45900),
                (3, "SKU-C", "显示器", 129900),
            ],
        )
        conn.executemany(
            "INSERT INTO orders(id, customer_id, order_no, status) "
            "VALUES (?, ?, ?, ?)",
            [
                (1, 1, "SO-1001", "paid"),
                (2, 2, "SO-1002", "paid"),
            ],
        )
        # 订单 1 三行，其中第 3 行永不退货（验证不反向带入明细）
        conn.executemany(
            "INSERT INTO order_lines(id, order_id, product_id, quantity) "
            "VALUES (?, ?, ?, ?)",
            [
                (1, 1, 1, 1),
                (2, 1, 2, 2),
                (3, 1, 3, 1),
                (4, 2, 3, 1),
            ],
        )
        # 同订单两张退货单：抽取退货单 1 时不得带入退货单 2
        conn.executemany(
            "INSERT INTO return_orders(id, order_id, customer_id, reason, "
            "status) VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 1, "耳机质量问题", "approved"),
                (2, 1, 1, "键盘误购", "received"),
                (3, 2, 2, "显示器坏点", "approved"),
            ],
        )
        conn.executemany(
            "INSERT INTO return_lines(id, return_order_id, order_line_id, "
            "product_id, quantity) VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 1, 1, 1),
                (2, 2, 2, 2, 1),
                (3, 3, 4, 3, 1),
            ],
        )
        if with_orphan:
            # 断引用：指向不存在的退货单 -> 触发整次失败、无残件
            conn.execute(
                "INSERT INTO return_lines(id, return_order_id, "
                "order_line_id, product_id, quantity) "
                "VALUES (99, 9999, 1, 1, 1)"
            )
        else:
            # 正常示例库必须自身满足全部外键（含环）
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"示例库外键违例: {violations}")
        conn.commit()
    finally:
        conn.close()
    return path


class DemoWriter:
    """后台持续写入器：每个 tick 一个事务，覆盖增/改/删三类操作。"""

    def __init__(self, source_db: str, interval: float = 2.0) -> None:
        self.source_db = source_db
        self.interval = interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.ticks = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="demo-writer", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def tick_once(self) -> int:
        """执行一次写入事务，返回当前 tick 序号。"""
        conn = sqlite3.connect(self.source_db, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            with conn:  # 单事务：增、改、删原子可见
                self.ticks += 1
                n = self.ticks
                rid = 9000 + n
                conn.execute(
                    "INSERT INTO return_orders(id, order_id, customer_id, "
                    "reason, status) VALUES (?, 1, 1, ?, 'draft')",
                    (rid, f"演示写入 tick {n}"),
                )
                conn.execute(
                    "UPDATE return_orders SET status = ? WHERE id = ?",
                    (f"updated-{n % 5}", rid),
                )
                # 上一轮的演示单据删掉，保持源库体量稳定
                conn.execute(
                    "DELETE FROM return_orders WHERE id = ?", (rid - 1,)
                )
        finally:
            conn.close()
        return self.ticks

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except sqlite3.Error:
                # 源库被替换等瞬时错误：等待下轮重试，不杀线程
                pass
            self._stop.wait(self.interval)
