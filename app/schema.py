"""SQLite 源库结构内省。

约定（与题目一致）：
- 六张业务表：退货单、退货明细、订单、订单行、客户、商品
- 每张表以整数 id 为单列主键
- 外键均为单列引用（客户推荐关系可成环）
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# 六表的规范顺序（也是拷贝顺序，父表在前）
TABLES: tuple[str, ...] = (
    "customers",
    "products",
    "orders",
    "order_lines",
    "return_orders",
    "return_lines",
)

# 中文展示名
TABLE_LABELS: dict[str, str] = {
    "return_orders": "退货单",
    "return_lines": "退货明细",
    "orders": "订单",
    "order_lines": "订单行",
    "customers": "客户",
    "products": "商品",
}

RETURN_ORDER_TABLE = "return_orders"
RETURN_LINE_TABLE = "return_lines"


@dataclass(frozen=True)
class ForeignKey:
    """单列外键定义。"""

    table: str
    column: str
    ref_table: str
    ref_column: str


@dataclass
class TableSchema:
    name: str
    create_sql: str                      # sqlite_master 中的原始 CREATE 语句
    columns: list[str]                    # pragma table_info 的列顺序（含 id）
    fks: list[ForeignKey] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)  # 原始 CREATE INDEX 语句


@dataclass
class DatabaseSchema:
    tables: dict[str, TableSchema]
    table_order: tuple[str, ...]

    def fks_from(self, table: str) -> list[ForeignKey]:
        return self.tables[table].fks


def quote_ident(name: str) -> str:
    """双引号包裹标识符。标识符来自 sqlite_master 元数据，非用户输入。"""
    return '"' + name.replace('"', '""') + '"'


def introspect(source_db: str) -> DatabaseSchema:
    """打开源库做结构内省；结构不符合约定时抛出 SchemaError。"""
    conn = sqlite3.connect(
        f"file:{source_db}?mode=ro", uri=True, timeout=30
    )
    try:
        conn.row_factory = sqlite3.Row
        existing = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = [t for t in TABLES if t not in existing]
        if missing:
            raise SchemaError(f"源库缺少必需的表: {', '.join(missing)}")

        tables: dict[str, TableSchema] = {}
        for name in TABLES:
            tables[name] = _read_table(conn, name)

        # 外键引用的目标表必须也在六表集合内
        known = set(TABLES)
        for ts in tables.values():
            for fk in ts.fks:
                if fk.ref_table not in known:
                    raise SchemaError(
                        f"表 {ts.name}.{fk.column} 引用了未知表 "
                        f"{fk.ref_table}，切片仅支持六表闭包"
                    )

        return DatabaseSchema(tables=tables, table_order=TABLES)
    finally:
        conn.close()


def _read_table(conn: sqlite3.Connection, name: str) -> TableSchema:
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()[0]

    info = conn.execute(
        f"PRAGMA table_info({quote_ident(name)})"
    ).fetchall()
    columns = [r["name"] for r in info]

    # 主键校验：必须是单列整数主键 id
    pk_cols = [r["name"] for r in info if r["pk"]]
    if pk_cols != ["id"]:
        raise SchemaError(
            f"表 {name} 的主键必须是单列 id，实际主键列: {pk_cols}"
        )
    id_row = next(r for r in info if r["name"] == "id")
    # INTEGER PRIMARY KEY 是 rowid 别名；类型名大小写不敏感
    if (id_row["type"] or "").upper() != "INTEGER":
        raise SchemaError(f"表 {name}.id 必须是 INTEGER PRIMARY KEY")

    fks: list[ForeignKey] = []
    for row in conn.execute(
        f"PRAGMA foreign_key_list({quote_ident(name)})"
    ).fetchall():
        if row["seq"] != 0 or row["from"] != _single_from(conn, name, row):
            # 复合外键直接拒绝
            raise SchemaError(f"表 {name} 存在复合外键，仅支持单列外键引用")
        fks.append(
            ForeignKey(
                table=name,
                column=row["from"],
                ref_table=row["table"],
                ref_column=row["to"],
            )
        )

    indexes = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (name,),
        ).fetchall()
    ]

    return TableSchema(
        name=name,
        create_sql=create_sql,
        columns=columns,
        fks=fks,
        indexes=indexes,
    )


def _single_from(
    conn: sqlite3.Connection, name: str, first_row: sqlite3.Row
) -> str:
    """确认该外键约束只有一列（seq 只出现 0）。"""
    rows = conn.execute(
        f"PRAGMA foreign_key_list({quote_ident(name)})"
    ).fetchall()
    constraint_id = first_row["id"]
    seqs = [r["seq"] for r in rows if r["id"] == constraint_id]
    if seqs != [0]:
        raise SchemaError(f"表 {name} 存在复合外键，仅支持单列外键引用")
    return first_row["from"]


class SchemaError(Exception):
    """源库结构不符合切片约定。"""
