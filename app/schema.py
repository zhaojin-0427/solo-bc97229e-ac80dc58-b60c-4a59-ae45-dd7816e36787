"""SQLite 源库结构内省。

约定（与题目一致）：
- 六张业务表：退货单、退货明细、订单、订单行、客户、商品
- 每张表以整数 id 为单列主键
- 外键均为单列引用：可引用父表的 id，也可引用父表的*其他单列唯一键*
  （如退货业务号 return_no、客户业务码 code）。匹配时遵循 SQLite 语义，
  即按父列的类型亲和性与排序规则比较（数字文本、大小写差异都能命中）
- 客户推荐关系等外键可成环
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
    """单列外键定义。

    ref_column 是父表中被引用的列：可能是 id（整数主键），也可能是
    父表的其它单列唯一键（业务号、业务码等）。内省阶段已校验该列
    在父表中确为单列唯一键（主键或单列 UNIQUE 索引）。
    """

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

    def return_lines_fk(self) -> ForeignKey:
        """退货明细 -> 退货单 的唯一外键（introspect 已校验恰好一条）。

        种子明细的选取完全依据这条声明：子列值匹配父列（可能是 id，
        也可能是退货业务号等唯一键）即属于该退货单。
        """
        fks = [
            fk
            for fk in self.tables[RETURN_LINE_TABLE].fks
            if fk.ref_table == RETURN_ORDER_TABLE
        ]
        if len(fks) != 1:  # introspect 已拦截，这里只是防御
            raise SchemaError(
                f"退货明细表必须恰好声明一条指向退货单表的单列外键，"
                f"实际 {len(fks)} 条"
            )
        return fks[0]


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

        _validate_foreign_keys(conn, tables)

        return DatabaseSchema(tables=tables, table_order=TABLES)
    finally:
        conn.close()


def _validate_foreign_keys(
    conn: sqlite3.Connection, tables: dict[str, TableSchema]
) -> None:
    """校验外键声明：目标在六表内、列存在、父列是单列唯一键。"""
    known = set(TABLES)
    for ts in tables.values():
        for fk in ts.fks:
            if fk.ref_table not in known:
                raise SchemaError(
                    f"表 {ts.name}.{fk.column} 引用了未知表 "
                    f"{fk.ref_table}，切片仅支持六表闭包"
                )
            parent = tables[fk.ref_table]
            if fk.column not in ts.columns:
                raise SchemaError(
                    f"表 {ts.name} 的外键列 {fk.column} 不存在"
                )
            if fk.ref_column not in parent.columns:
                raise SchemaError(
                    f"外键 {ts.name}.{fk.column} 引用了 "
                    f"{fk.ref_table}.{fk.ref_column}，但该列不存在"
                )
            if not _is_single_column_unique(
                conn, fk.ref_table, fk.ref_column
            ):
                raise SchemaError(
                    f"外键 {ts.name}.{fk.column} 引用 "
                    f"{fk.ref_table}.{fk.ref_column}，但父列不是单列唯一键"
                    f"（主键或单列 UNIQUE），无法作为外键目标"
                )

    # 退货明细必须恰好声明一条指向退货单的外键：种子明细靠它选取
    rl_fks = [
        fk
        for fk in tables[RETURN_LINE_TABLE].fks
        if fk.ref_table == RETURN_ORDER_TABLE
    ]
    if len(rl_fks) != 1:
        raise SchemaError(
            f"退货明细表必须恰好声明一条指向退货单表的单列外键，"
            f"实际 {len(rl_fks)} 条"
        )


def _is_single_column_unique(
    conn: sqlite3.Connection, table: str, column: str
) -> bool:
    """父列是否为单列唯一键：主键，或覆盖该单列的唯一索引。

    部分唯一索引（partial）与表达式索引不能作为外键目标，排除。
    """
    q = quote_ident(table)
    for r in conn.execute(f"PRAGMA table_info({q})").fetchall():
        if r["name"] == column and r["pk"]:
            return True
    for idx in conn.execute(f"PRAGMA index_list({q})").fetchall():
        if not idx["unique"] or idx["partial"]:
            continue
        cols = [
            r["name"]
            for r in conn.execute(
                f"PRAGMA index_info({quote_ident(idx['name'])})"
            ).fetchall()
        ]
        if cols == [column]:
            return True
    return False


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
    # PRAGMA foreign_key_list 按约束 id 分组；seq 不止 0 即复合外键
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        f"PRAGMA foreign_key_list({quote_ident(name)})"
    ).fetchall():
        grouped.setdefault(row["id"], []).append(row)
    for rows in grouped.values():
        if sorted(r["seq"] for r in rows) != [0]:
            raise SchemaError(
                f"表 {name} 存在复合外键，仅支持单列外键引用"
            )
        row = rows[0]
        # REFERENCES t 未列名时 to 为 NULL，表示引用父表主键（此处即 id）
        ref_column = row["to"] if row["to"] is not None else "id"
        fks.append(
            ForeignKey(
                table=name,
                column=row["from"],
                ref_table=row["table"],
                ref_column=ref_column,
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


class SchemaError(Exception):
    """源库结构不符合切片约定。"""
