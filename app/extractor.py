"""快照一致、内存有界的 SQLite 切片抽取器。

算法要点
========
1. 单连接快照
   每个任务独占一个 ``mode=ro`` 只读连接，首个读语句即在 WAL 上固定读标记
   （read mark）。此后源库的并发增删改对本连接不可见，整份切片来自同一快照。

2. 临时表驱动的 BFS，进程内存有界
   遍历状态（frontier/seen/batch/cand）全部放在 SQLite 的*磁盘临时表*中
   （``PRAGMA temp_store=FILE``）。Python 每次只取出 ``batch_size`` 行处理，
   从不把全部选中 ID 或全部记录读进内存；数据拷贝同样按键集分页流式进行。

3. 外键声明驱动的精确闭包，不反向带入
   种子集合 = 指定退货单本身 + 该退货单的全部退货明细。明细的归属、以及
   之后每一层扩展，都严格按 ``PRAGMA foreign_key_list`` 读到的*单列外键
   声明*行进：外键可以引用父表 id，也可以引用父表的其它单列唯一键
   （退货业务号、客户业务码等）。匹配用 SQL 连接完成，父列固定写在等号
   左侧，因此比较遵循 SQLite 语义——按父列的类型亲和性与排序规则
   （如 NOCASE）判定，数字文本与大小写差异都能正确命中，且绝不修改
   原值、不引入无关行。只沿外键*正向*递归（退货单 -> 订单 -> 客户），
   不做反向 JOIN，因此同一订单的其它退货单、未被退货的订单行都不会
   进入切片。

4. 缺失引用即整体失败
   种子不存在、或任一非空外键按声明在快照中找不到目标行，立即抛出
   MissingReferenceError，删除临时残件，不产出切片文件。

5. 成环支持
   候选目标一律解析为父表整数 id 后入 seen 临时表去重，客户推荐关系
   成环（含 NOCASE 业务码互引）时自然收敛。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    DatabaseSchema,
    RETURN_LINE_TABLE,
    RETURN_ORDER_TABLE,
    TABLES,
    quote_ident,
)

ProgressFn = Callable[[dict[str, Any]], None]


class ExtractionError(Exception):
    """抽取过程中的可预期失败。"""


def _lit(value: str) -> str:
    """SQL 字符串字面量转义（表名来自 sqlite_master 元数据，非用户输入）。"""
    return "'" + value.replace("'", "''") + "'"


class MissingReferenceError(ExtractionError):
    """外键目标（或种子退货单）在快照中不存在。"""


@dataclass
class ExtractResult:
    output_path: Path
    row_counts: dict[str, int]
    total_rows: int


def run_extraction(
    source_db: str,
    schema: DatabaseSchema,
    return_order_id: int,
    output_path: Path,
    batch_size: int = 500,
    progress: ProgressFn | None = None,
    on_snapshot: Callable[[], None] | None = None,
) -> ExtractResult:
    """执行一次切片抽取。

    成功返回 ExtractResult；失败抛出异常并保证 output_path 不存在残件。
    on_snapshot 在快照读标记固定后回调（测试用于并发写入验证）。
    """
    tmp_path = output_path.with_name(output_path.name + ".partial")
    if tmp_path.exists():
        tmp_path.unlink()

    snapshot = _open_snapshot(source_db)
    dest: sqlite3.Connection | None = None
    try:
        snapshot.execute("BEGIN")
        # 显式触发一次读，固定 WAL 读标记
        snapshot.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if on_snapshot is not None:
            on_snapshot()

        _create_temp_tables(snapshot)
        _seed(snapshot, schema, return_order_id)

        def report(phase: str, **extra: Any) -> None:
            if progress is not None:
                progress({"phase": phase, **extra})

        report("traverse", message="正在递归遍历外键闭包")
        _traverse(snapshot, schema, batch_size, report)

        counts = {
            row[0]: row[1]
            for row in snapshot.execute(
                "SELECT table_name, count(*) FROM seen GROUP BY table_name"
            )
        }
        counts = {t: counts.get(t, 0) for t in TABLES}
        total = sum(counts.values())
        report(
            "traverse_done",
            row_counts=counts,
            total_rows=total,
            message=f"闭包遍历完成，共选中 {total} 行",
        )

        dest = sqlite3.connect(str(tmp_path))
        # 加载阶段关闭外键检查：循环外键（客户推荐环）无法按线性顺序满足
        dest.execute("PRAGMA foreign_keys = OFF")
        _build_destination_ddl(dest, schema)
        copied = _stream_copy(
            snapshot, dest, schema, batch_size, report
        )

        # 完整性闸门：全部通过才允许原子改名
        dest.execute("PRAGMA foreign_keys = ON")
        violations = dest.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ExtractionError(
                f"切片外键检查未通过，违例行 {len(violations)} 条，首条: "
                f"{violations[0]}"
            )
        integrity = dest.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ExtractionError(f"切片完整性检查未通过: {integrity}")
        dest.commit()
        dest.close()
        dest = None

        os.replace(tmp_path, output_path)
        report(
            "done",
            row_counts=copied,
            total_rows=sum(copied.values()),
            message="切片已生成",
        )
        return ExtractResult(
            output_path=output_path,
            row_counts=copied,
            total_rows=sum(copied.values()),
        )
    except Exception:
        if dest is not None:
            try:
                dest.close()
            except sqlite3.Error:
                pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        snapshot.close()


def _open_snapshot(source_db: str) -> sqlite3.Connection:
    """打开只读快照连接：mode=ro 禁止写源库，临时表落盘、关闭 mmap。"""
    conn = sqlite3.connect(
        f"file:{source_db}?mode=ro",
        uri=True,
        timeout=30,
        isolation_level=None,  # 自动提交模式，事务由我们显式控制
    )
    # 关键：临时表存磁盘而非内存，遍历百万级 ID 也不撑爆进程内存。
    # 不使用 query_only（它会连带禁止 TEMP 表写入）；源库只读由 URI
    # 的 mode=ro 在文件层强制，任何针对源库的写尝试都会直接报错。
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA mmap_size = 0")
    return conn


def _create_temp_tables(conn: sqlite3.Connection) -> None:
    # WITHOUT ROWID 的复合主键表，去重与查找都走主键索引（落盘）。
    # 候选目标在 SQL 连接阶段就解析成父表整数 id，因此临时表始终
    # 只存 (表名, 整数 id)，与父列是 id 还是业务键无关。
    conn.executescript(
        """
        CREATE TEMP TABLE seen (
            table_name TEXT NOT NULL,
            id         INTEGER NOT NULL,
            PRIMARY KEY (table_name, id)
        ) WITHOUT ROWID;
        CREATE TEMP TABLE frontier (
            table_name TEXT NOT NULL,
            id         INTEGER NOT NULL,
            PRIMARY KEY (table_name, id)
        ) WITHOUT ROWID;
        CREATE TEMP TABLE batch (
            table_name TEXT NOT NULL,
            id         INTEGER NOT NULL,
            PRIMARY KEY (table_name, id)
        ) WITHOUT ROWID;
        CREATE TEMP TABLE cand (
            table_name TEXT NOT NULL,
            id         INTEGER NOT NULL,
            PRIMARY KEY (table_name, id)
        ) WITHOUT ROWID;
        """
    )


def _seed(
    conn: sqlite3.Connection,
    schema: DatabaseSchema,
    return_order_id: int,
) -> None:
    """写入种子：指定退货单 + 它的全部退货明细。

    明细归属严格按“退货明细 -> 退货单”的外键声明判定：子列值与
    父列（id 或退货业务号等唯一键）匹配即属于该退货单。比较由
    SQL 连接完成，父列写在等号左侧，沿用父列的亲和性与排序规则。
    """
    ro = quote_ident(RETURN_ORDER_TABLE)
    rl = quote_ident(RETURN_LINE_TABLE)

    exists = conn.execute(
        f"SELECT 1 FROM {ro} WHERE id = ? LIMIT 1", (return_order_id,)
    ).fetchone()
    if exists is None:
        raise MissingReferenceError(
            f"种子退货单不存在: {RETURN_ORDER_TABLE}.id = {return_order_id}"
        )

    # 内省已校验：退货明细 -> 退货单 恰好一条单列外键
    fk = schema.return_lines_fk()
    child = quote_ident(fk.column)
    parent = quote_ident(fk.ref_column)

    conn.execute(
        "INSERT INTO seen(table_name, id) VALUES (?, ?)",
        (RETURN_ORDER_TABLE, return_order_id),
    )
    conn.execute(
        "INSERT INTO frontier(table_name, id) VALUES (?, ?)",
        (RETURN_ORDER_TABLE, return_order_id),
    )
    # 该退货单的*全部*明细作为种子；只按外键正向取，不碰其它退货单的明细
    line_rows = f"""
        SELECT {_lit(RETURN_LINE_TABLE)}, rl.id
        FROM {rl} AS rl
        JOIN {ro} AS ro ON ro.{parent} = rl.{child}
        WHERE ro.id = ?
    """
    conn.execute(
        f"INSERT INTO seen(table_name, id) {line_rows}",
        (return_order_id,),
    )
    conn.execute(
        f"INSERT INTO frontier(table_name, id) {line_rows}",
        (return_order_id,),
    )


def _traverse(
    conn: sqlite3.Connection,
    schema: DatabaseSchema,
    batch_size: int,
    report: ProgressFn,
) -> None:
    """基于临时表的分批 BFS：每批 Python 至多持有 batch_size 行。"""
    while True:
        conn.execute("DELETE FROM batch")
        conn.execute(
            "INSERT INTO batch SELECT table_name, id FROM frontier LIMIT ?",
            (batch_size,),
        )
        got = conn.execute("SELECT count(*) FROM batch").fetchone()[0]
        if got == 0:
            return
        # 从待处理队列移除本批
        conn.execute(
            """
            DELETE FROM frontier
            WHERE (table_name, id) IN (
                SELECT table_name, id FROM batch
            )
            """
        )

        conn.execute("DELETE FROM cand")
        # 本批涉及的表（至多六张），列表极小
        group_rows = conn.execute(
            "SELECT DISTINCT table_name FROM batch"
        ).fetchall()
        for (table_name,) in group_rows:
            t = quote_ident(table_name)
            for fk in schema.fks_from(table_name):
                col = quote_ident(fk.column)
                ref_t = quote_ident(fk.ref_table)
                ref_col = quote_ident(fk.ref_column)
                # 父列固定写在等号左侧：比较采用父列的排序规则（如
                # NOCASE）；类型亲和性按 SQLite 规则对两侧对称生效，
                # 数字文本（'700' 与 700）也能正确匹配。
                #
                # 1) 缺失检查（反连接）：任一非空外键在父表快照中
                #    找不到目标 -> 整次失败，不留残件
                missing = conn.execute(
                    f"""
                    SELECT src.{col}
                    FROM {t} AS src
                    WHERE src.{col} IS NOT NULL
                      AND src.id IN (
                          SELECT id FROM batch
                          WHERE table_name = {_lit(table_name)}
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM {ref_t} AS p
                          WHERE p.{ref_col} = src.{col}
                      )
                    LIMIT 1
                    """
                ).fetchone()
                if missing is not None:
                    raise MissingReferenceError(
                        f"缺失外键目标: {fk.ref_table}.{fk.ref_column} = "
                        f"{missing[0]}（{table_name}.{fk.column} 的引用"
                        f"在源库快照中不存在）"
                    )
                # 2) 正向收集：按外键声明连接父表，把目标解析为父表
                #    整数 id；DISTINCT 之外的重叠由 cand 主键去重
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO cand(table_name, id)
                    SELECT DISTINCT {_lit(fk.ref_table)}, p.id
                    FROM {t} AS src
                    JOIN {ref_t} AS p ON p.{ref_col} = src.{col}
                    WHERE src.{col} IS NOT NULL
                      AND src.id IN (
                          SELECT id FROM batch
                          WHERE table_name = {_lit(table_name)}
                      )
                    """
                )

        # 先入队（排除已在 seen/frontier 的），再登记 seen
        conn.execute(
            """
            INSERT OR IGNORE INTO frontier(table_name, id)
            SELECT c.table_name, c.id
            FROM cand c
            WHERE NOT EXISTS (
                SELECT 1 FROM seen s
                WHERE s.table_name = c.table_name AND s.id = c.id
            )
              AND NOT EXISTS (
                SELECT 1 FROM frontier f
                WHERE f.table_name = c.table_name AND f.id = c.id
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO seen(table_name, id)
            SELECT c.table_name, c.id
            FROM cand c
            WHERE NOT EXISTS (
                SELECT 1 FROM seen s
                WHERE s.table_name = c.table_name AND s.id = c.id
            )
            """
        )

        discovered = conn.execute(
            "SELECT count(*) FROM seen"
        ).fetchone()[0]
        queued = conn.execute(
            "SELECT count(*) FROM frontier"
        ).fetchone()[0]
        report(
            "traverse",
            discovered=discovered,
            queued=queued,
            batch_size=got,
        )


def _build_destination_ddl(
    dest: sqlite3.Connection, schema: DatabaseSchema
) -> None:
    """原样保留六表的 CREATE 语句（字段、主键、唯一键、外键）与索引。"""
    script_lines: list[str] = ["PRAGMA journal_mode = DELETE;"]
    for name in schema.table_order:
        ts = schema.tables[name]
        # sqlite_master 中的 SQL 不含结尾分号，统一补上再拼接
        script_lines.append(ts.create_sql.rstrip().rstrip(";") + ";")
        for index_sql in ts.indexes:
            script_lines.append(index_sql.rstrip().rstrip(";") + ";")
    dest.executescript("\n".join(script_lines))
    dest.execute("BEGIN")


def _stream_copy(
    snapshot: sqlite3.Connection,
    dest: sqlite3.Connection,
    schema: DatabaseSchema,
    batch_size: int,
    report: ProgressFn,
) -> dict[str, int]:
    """按键集分页把 seen 集合中的行流式拷入切片，进程内存有界。

    按原列顺序、原值 INSERT：业务号、业务码等字段的存储类型与
    大小写都与源库一致，不做任何归一化。
    """
    copied_counts: dict[str, int] = {}
    for table in schema.table_order:
        ts = schema.tables[table]
        t = quote_ident(table)
        cols = [quote_ident(c) for c in ts.columns]
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in ts.columns)
        insert_sql = (
            f"INSERT INTO {t} ({col_list}) VALUES ({placeholders})"
        )
        id_index = ts.columns.index("id")

        max_id = 0
        table_copied = 0
        while True:
            rows = snapshot.execute(
                f"""
                SELECT {col_list}
                FROM {t}
                WHERE id > ?
                  AND id IN (
                      SELECT id FROM seen WHERE table_name = ?
                  )
                ORDER BY id
                LIMIT ?
                """,
                (max_id, table, batch_size),
            ).fetchmany(batch_size)
            if not rows:
                break
            dest.executemany(insert_sql, rows)
            table_copied += len(rows)
            max_id = rows[-1][id_index]
            report(
                "copy",
                table=table,
                table_rows=table_copied,
                message=f"正在拷贝 {table}（已拷贝 {table_copied} 行）",
            )
            if len(rows) < batch_size:
                break
        copied_counts[table] = table_copied
    return copied_counts
