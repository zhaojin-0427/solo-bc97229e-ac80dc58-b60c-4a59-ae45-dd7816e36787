"""单列外键引用非 id 唯一键的回归测试。

对应售后现场的两类故障：
- 退货单 id=1、业务号 700，明细引用业务号 -> 旧实现成功导出零条明细（漏导）
- 客户业务码按 NOCASE 互引成环 -> 旧实现把合法引用误报缺失（抽取失败）

匹配必须遵循 SQLite 语义：按父列的类型亲和性与排序规则比较，
数字文本与大小写差异都能命中；不改原值、不加入无关行；
真正缺失的引用仍整次失败、无残件。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.extractor import MissingReferenceError, run_extraction
from app.schema import SchemaError, introspect


def _build_db(
    path: Path,
    *,
    rno_type: str = "INTEGER",
    rno_value: object = 700,
    rno_unique: bool = True,
    line_type: str = "INTEGER",
    line_value: object = 700,
    line_fk: str = "REFERENCES return_orders(return_no)",
    lines_ddl: str | None = None,
    extra_sql: str = "",
    check: bool = True,
) -> Path:
    """构造最小六表源库：退货单 id=1、业务号 700，明细 1 条引用业务号。

    客户表固定为 NOCASE 业务码两环：1('Alpha') <-> 2('BETA')。
    """
    if lines_ddl is None:
        lines_ddl = f"""
        CREATE TABLE return_lines (
            id INTEGER PRIMARY KEY,
            return_order_id {line_type} NOT NULL {line_fk},
            order_line_id INTEGER NOT NULL REFERENCES order_lines(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL
        );
        """
    rno_unique_ddl = "UNIQUE" if rno_unique else ""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            f"""
            PRAGMA journal_mode = WAL;
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                referred_by_code TEXT REFERENCES customers(code)
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, sku TEXT NOT NULL,
                name TEXT NOT NULL, price_cents INTEGER NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                order_no TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE order_lines (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL
            );
            CREATE TABLE return_orders (
                id INTEGER PRIMARY KEY,
                return_no {rno_type} NOT NULL {rno_unique_ddl},
                order_id INTEGER NOT NULL REFERENCES orders(id),
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                reason TEXT NOT NULL, status TEXT NOT NULL
            );
            {lines_ddl}
            {extra_sql}
            """
        )
        conn.executemany(
            "INSERT INTO customers(id, code, name, referred_by_code) "
            "VALUES (?, ?, ?, ?)",
            [(1, "Alpha", "张三", "beta"), (2, "BETA", "李四", "ALPHA")],
        )
        conn.execute(
            "INSERT INTO products(id, sku, name, price_cents) "
            "VALUES (1, 'SKU-A', '耳机', 29900)"
        )
        conn.execute(
            "INSERT INTO orders(id, customer_id, order_no, status) "
            "VALUES (1, 1, 'SO-1', 'paid')"
        )
        conn.execute(
            "INSERT INTO order_lines(id, order_id, product_id, quantity) "
            "VALUES (1, 1, 1, 1)"
        )
        conn.execute(
            "INSERT INTO return_orders(id, return_no, order_id, "
            "customer_id, reason, status) "
            "VALUES (1, ?, 1, 1, '质量问题', 'approved')",
            (rno_value,),
        )
        conn.execute(
            "INSERT INTO return_lines(id, return_order_id, order_line_id, "
            "product_id, quantity) VALUES (1, ?, 1, 1, 1)",
            (line_value,),
        )
        if check:
            violations = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            assert violations == [], f"测试库自身外键违例: {violations}"
        conn.commit()
    finally:
        conn.close()
    return path


def _ids(conn: sqlite3.Connection, table: str) -> set:
    return {r[0] for r in conn.execute(f'SELECT id FROM "{table}"')}


def test_seed_lines_via_business_key(tmp_path):
    """退货单 id=1、业务号 700，明细引用业务号：必须导出该单全部明细。

    （回归：旧实现按 id=1 匹配明细外键值 700，成功导出零条明细。）
    """
    db = _build_db(tmp_path / "biz.db")
    # 兄弟退货单（业务号 701）及其明细：不得进入退货单 1 的切片
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO return_orders(id, return_no, order_id, "
            "customer_id, reason, status) "
            "VALUES (2, 701, 1, 1, '误购', 'draft')"
        )
        conn.execute(
            "INSERT INTO return_lines(id, return_order_id, order_line_id, "
            "product_id, quantity) VALUES (2, 701, 1, 1, 1)"
        )
    conn.close()

    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    result = run_extraction(str(db), schema, 1, out)

    with sqlite3.connect(str(out)) as dst:
        assert dst.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _ids(dst, "return_orders") == {1}
        assert _ids(dst, "return_lines") == {1}   # 明细不再漏导
        assert _ids(dst, "orders") == {1}
        assert _ids(dst, "order_lines") == {1}
        assert _ids(dst, "customers") == {1, 2}   # NOCASE 环
        assert _ids(dst, "products") == {1}
    assert result.row_counts["return_lines"] == 1
    assert result.total_rows == 7


def test_numeric_text_child_matches_integer_parent(tmp_path):
    """子列 TEXT '700'、父列 INTEGER 700：按父列亲和性匹配，原值不改。"""
    db = _build_db(tmp_path / "t.db", line_type="TEXT", line_value="700")
    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)

    with sqlite3.connect(str(out)) as dst:
        assert dst.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _ids(dst, "return_lines") == {1}
        # 原值与存储类型都保留：仍是文本 '700'，不归一化成整数
        row = dst.execute(
            "SELECT return_order_id, typeof(return_order_id) "
            "FROM return_lines"
        ).fetchone()
        assert row == ("700", "text")


def test_integer_child_matches_numeric_text_parent(tmp_path):
    """父列 TEXT '700'、子列 INTEGER 700：数字文本同样命中。"""
    db = _build_db(
        tmp_path / "t.db", rno_type="TEXT", rno_value="700"
    )
    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)

    with sqlite3.connect(str(out)) as dst:
        assert dst.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _ids(dst, "return_lines") == {1}
        row = dst.execute(
            "SELECT return_no, typeof(return_no) FROM return_orders"
        ).fetchone()
        assert row == ("700", "text")


def test_nocase_business_code_cycle(tmp_path):
    """客户业务码 NOCASE 互引成环：闭包收敛、检查通过、大小写原值保留。

    （回归：旧实现把文本业务码当整数 id 比对，合法引用被误报缺失。）
    """
    db = _build_db(tmp_path / "c.db")
    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)

    with sqlite3.connect(str(out)) as dst:
        assert dst.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = {
            r[0]: (r[1], r[2])
            for r in dst.execute(
                "SELECT id, code, referred_by_code FROM customers"
            )
        }
        # 环两端都在，且大小写原值保留（'Alpha'/'beta'、'BETA'/'ALPHA'）
        assert rows == {1: ("Alpha", "beta"), 2: ("BETA", "ALPHA")}


def test_missing_business_code_target_fails(tmp_path):
    """业务码引用真正缺失：整次失败、无残件。"""
    db = _build_db(tmp_path / "m.db")
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = OFF")  # 故意写入脏数据
    with conn:
        conn.execute(
            "UPDATE customers SET referred_by_code = 'NOPE' WHERE id = 1"
        )
    conn.close()

    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    with pytest.raises(MissingReferenceError, match="customers.code"):
        run_extraction(str(db), schema, 1, out)
    assert not out.exists()
    assert not out.with_name(out.name + ".partial").exists()


def test_orphan_line_of_unknown_return_no_ignored(tmp_path):
    """指向不存在业务号的明细不属于退货单 1 的闭包：不报错、不带入。"""
    db = _build_db(tmp_path / "o.db")
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = OFF")  # 脏数据：与本次抽取无关
    with conn:
        conn.execute(
            "INSERT INTO return_lines(id, return_order_id, order_line_id, "
            "product_id, quantity) VALUES (99, 999, 1, 1, 1)"
        )
    conn.close()

    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)
    with sqlite3.connect(str(out)) as dst:
        assert _ids(dst, "return_lines") == {1}   # 99 号明细不进入切片


def test_fk_to_non_unique_column_rejected(tmp_path):
    """外键目标不是单列唯一键 -> 结构校验拒绝。"""
    db = _build_db(
        tmp_path / "n.db",
        line_fk="REFERENCES return_orders(reason)",
        check=False,  # 父键不唯一时 fk_check 自身会报 mismatch
    )
    with pytest.raises(SchemaError, match="唯一"):
        introspect(str(db))


def test_fk_to_unique_index_column_accepted(tmp_path):
    """父列唯一性来自独立 UNIQUE INDEX（非列级约束）也可作外键目标。"""
    db = _build_db(
        tmp_path / "u.db",
        rno_unique=False,
        extra_sql=(
            "CREATE UNIQUE INDEX idx_ro_rno ON return_orders(return_no);"
        ),
    )
    schema = introspect(str(db))
    assert schema.return_lines_fk().ref_column == "return_no"
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)
    with sqlite3.connect(str(out)) as dst:
        assert _ids(dst, "return_lines") == {1}
        assert dst.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fk_without_column_list_uses_parent_pk(tmp_path):
    """REFERENCES return_orders（不带列名）-> 引用父表主键 id。"""
    db = _build_db(
        tmp_path / "p.db",
        line_fk="REFERENCES return_orders",
        line_value=1,
    )
    schema = introspect(str(db))
    fk = schema.return_lines_fk()
    assert fk.ref_table == "return_orders"
    assert fk.ref_column == "id"
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)
    with sqlite3.connect(str(out)) as dst:
        assert _ids(dst, "return_lines") == {1}


def test_composite_fk_rejected(tmp_path):
    """复合外键仍被明确拒绝。"""
    lines_ddl = """
        CREATE TABLE return_lines (
            id INTEGER PRIMARY KEY,
            return_order_id INTEGER NOT NULL,
            order_line_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            FOREIGN KEY (return_order_id, order_line_id)
                REFERENCES return_orders(return_no, order_id)
        );
    """
    db = _build_db(tmp_path / "x.db", lines_ddl=lines_ddl, check=False)
    with pytest.raises(SchemaError, match="复合外键"):
        introspect(str(db))


def test_slice_preserves_business_key_constraints(tmp_path):
    """切片保留唯一键、COLLATE 与外键声明（约束不丢、不改写）。"""
    db = _build_db(tmp_path / "k.db")
    schema = introspect(str(db))
    out = tmp_path / "slice.db"
    run_extraction(str(db), schema, 1, out)

    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(out))
    try:
        for table in ("customers", "return_orders", "return_lines"):
            s_sql = src.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            d_sql = dst.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            assert s_sql == d_sql
        # 外键声明逐条一致（含 REFERENCES return_orders(return_no)）
        assert src.execute(
            'PRAGMA foreign_key_list("return_lines")'
        ).fetchall() == dst.execute(
            'PRAGMA foreign_key_list("return_lines")'
        ).fetchall()
    finally:
        src.close()
        dst.close()
