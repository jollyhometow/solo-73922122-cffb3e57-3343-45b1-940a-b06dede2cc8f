"""数据库结构校验与行级依赖图。

题目约束：
* 至多 3 张普通表，无触发器；
* 每张表有且仅有一个单列整数主键；
* 所有外键均为单列整数外键，目标列也是单列整数主键；
* 全部表总行数不超过 60；
* 允许循环外键；
* 上传库中的现有数据必须已经满足外键约束。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import config
from .errors import ValidationError

RowId = tuple[str, int]
# 外键边：(来源表, 来源列, 目标表, 目标列)
FkEdge = tuple[str, str, str, str]


@dataclass
class Schema:
    tables: list[str]
    pk_column: dict[str, str]
    fk_edges: list[FkEdge]
    rows: set[RowId]
    # 行级依赖：source_row 引用 target_row，删除 target 必须连带删除 source
    row_edges: set[tuple[RowId, RowId]] = field(default_factory=set)
    reverse: dict[RowId, set[RowId]] = field(default_factory=dict)

    def closure(self, roots: set[RowId]) -> set[RowId]:
        """返回 roots 及其全部（直接/间接）引用者。"""
        deleted: set[RowId] = set()
        stack = list(roots)
        while stack:
            node = stack.pop()
            if node in deleted:
                continue
            deleted.add(node)
            stack.extend(self.reverse.get(node, ()))
        return deleted


def _open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def validate_database(db_path: str) -> Schema:
    """校验上传库，返回可供缩减器使用的 Schema。"""
    try:
        return _validate(db_path)
    except sqlite3.DatabaseError as exc:
        raise ValidationError(f"无法作为 SQLite 数据库打开：{exc}") from exc


def _validate(db_path: str) -> Schema:
    conn = _open_ro(db_path)
    try:
        _ensure_no_attached(conn)
        tables = _load_tables(conn)
        if not tables:
            raise ValidationError("数据库中没有任何用户表。")
        if len(tables) > config.MAX_TABLES:
            raise ValidationError(
                f"普通表数量为 {len(tables)}，超过上限 {config.MAX_TABLES} 张。"
            )
        _ensure_no_triggers(conn)

        pk_column: dict[str, str] = {}
        fk_edges: list[FkEdge] = []
        total_rows = 0
        for table in tables:
            pk = _check_primary_key(conn, table)
            pk_column[table] = pk
            _check_not_without_rowid(conn, table)
            fk_edges.extend(_check_foreign_keys(conn, table, tables, pk_column))
            total_rows += _count_rows(conn, table)

        if not fk_edges:
            raise ValidationError("数据库至少需要包含一个外键关系。")
        if total_rows > config.MAX_TOTAL_ROWS:
            raise ValidationError(
                f"全部表总行数为 {total_rows}，超过上限 {config.MAX_TOTAL_ROWS} 行。"
            )

        _check_foreign_key_integrity(conn)

        rows: set[RowId] = set()
        for table in tables:
            for (pk_value,) in conn.execute(
                f'SELECT "{pk_column[table]}" FROM "{table}" '
                f'ORDER BY "{pk_column[table]}"'
            ):
                _check_pk_value(table, pk_value)
                rows.add((table, int(pk_value)))

        row_edges = _build_row_edges(conn, tables, pk_column, fk_edges)
    finally:
        conn.close()

    reverse: dict[RowId, set[RowId]] = {}
    for source, target in row_edges:
        reverse.setdefault(target, set()).add(source)

    return Schema(
        tables=tables,
        pk_column=pk_column,
        fk_edges=fk_edges,
        rows=rows,
        row_edges=row_edges,
        reverse=reverse,
    )


def _ensure_no_attached(conn: sqlite3.Connection) -> None:
    attached = [
        r[1]
        for r in conn.execute("PRAGMA database_list")
        if r[1] not in ("main", "temp")
    ]
    if attached:
        raise ValidationError(f"数据库带有附加库，不允许：{attached}")


def _load_tables(conn: sqlite3.Connection) -> list[str]:
    tables: list[str] = []
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'view') ORDER BY name"
    ):
        if name.startswith(config.RESERVED_PREFIX):
            continue
        if sql is None:
            raise ValidationError(
                f"表 {name} 是虚拟表（sqlite_master.sql 为空），只允许普通表。"
            )
        stripped = sql.lstrip().upper()
        if stripped.startswith("CREATE VIRTUAL TABLE"):
            raise ValidationError(f"表 {name} 是虚拟表，只允许普通表。")
        if stripped.startswith("CREATE VIEW") or stripped.startswith("CREATE TEMP VIEW"):
            raise ValidationError(f"存在视图 {name}，只允许普通表。")
        tables.append(name)
    return tables


def _ensure_no_triggers(conn: sqlite3.Connection) -> None:
    triggers = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    ]
    if triggers:
        raise ValidationError(f"数据库包含触发器，不允许：{triggers}")


def _check_primary_key(conn: sqlite3.Connection, table: str) -> str:
    cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk_cols = [c for c in cols if c["pk"] > 0]
    if len(pk_cols) != 1:
        raise ValidationError(
            f"表 {table} 的主键不是单列（主键列数={len(pk_cols)}）。"
        )
    pk = pk_cols[0]
    declared = (pk["type"] or "").upper().strip()
    # INTEGER PRIMARY KEY 是 rowid 的别名；只接受精确 INTEGER 声明，
    # 排除 INT / BIGINT 等“类型亲和”整数（题目要求整数主键）。
    if declared != "INTEGER":
        raise ValidationError(
            f"表 {table} 的主键列 {pk['name']} 声明类型为 "
            f"'{pk['type']}'，必须是 INTEGER。"
        )
    return pk["name"]


def _check_not_without_rowid(conn: sqlite3.Connection, table: str) -> None:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    if re.search(r"\bwithout\s+rowid\b", sql, re.IGNORECASE):
        raise ValidationError(f"表 {table} 是 WITHOUT ROWID 表，不允许。")


def _check_foreign_keys(
    conn: sqlite3.Connection,
    table: str,
    tables: list[str],
    pk_column: dict[str, str],
) -> list[FkEdge]:
    edges: list[FkEdge] = []
    fk_list = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    by_id: dict[int, list[sqlite3.Row]] = {}
    for fk in fk_list:
        by_id.setdefault(fk["id"], []).append(fk)
    for fk_id, cols in by_id.items():
        if len(cols) != 1 or cols[0]["seq"] != 0:
            raise ValidationError(
                f"表 {table} 的外键 #{fk_id} 不是单列外键。"
            )
        fk = cols[0]
        ref_table = fk["table"]
        if ref_table not in tables:
            raise ValidationError(
                f"表 {table} 的外键指向库外表 {ref_table}，不允许。"
            )
        if ref_table not in pk_column:
            # 外键表排在当前表之后时，目标列也必须是其主键，先记录列名稍后复核
            ref_pk = _check_primary_key(conn, ref_table)
        else:
            ref_pk = pk_column[ref_table]
        if fk["to"] != ref_pk:
            raise ValidationError(
                f"表 {table}.{fk['from']} 的外键目标列是 {ref_table}.{fk['to']}，"
                f"必须引用目标表单列整数主键 {ref_table}.{ref_pk}。"
            )
        source_col = _column_type(conn, table, fk["from"])
        if source_col.upper().strip() != "INTEGER":
            raise ValidationError(
                f"外键列 {table}.{fk['from']} 类型为 '{source_col}'，必须是 INTEGER。"
            )
        if fk["on_update"] not in ("NO ACTION", "RESTRICT") or fk["on_delete"] not in (
            "NO ACTION",
            "RESTRICT",
        ):
            raise ValidationError(
                f"外键 {table}.{fk['from']} -> {ref_table}.{ref_pk} "
                f"使用了 ON DELETE/UPDATE 动作 "
                f"({fk['on_delete']}/{fk['on_update']})，仅允许 NO ACTION/RESTRICT。"
            )
        edges.append((table, fk["from"], ref_table, fk["to"]))
    return edges


def _column_type(conn: sqlite3.Connection, table: str, column: str) -> str:
    for col in conn.execute(f'PRAGMA table_info("{table}")'):
        if col["name"] == column:
            return col["type"] or ""
    raise ValidationError(f"表 {table} 中找不到列 {column}。")


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _check_foreign_key_integrity(conn: sqlite3.Connection) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        detail = ", ".join(
            f"{r['table']} 行 rowid={r['rowid']} -> {r['parent']}" for r in violations
        )
        raise ValidationError(f"上传库存在外键违规：{detail}")


def _check_pk_value(table: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(
            f"表 {table} 存在非整数主键值 {value!r}，题目要求单列整数主键。"
        )


def _build_row_edges(
    conn: sqlite3.Connection,
    tables: list[str],
    pk_column: dict[str, str],
    fk_edges: list[FkEdge],
) -> set[tuple[RowId, RowId]]:
    edges: set[tuple[RowId, RowId]] = set()
    for src_table, src_col, dst_table, dst_col in fk_edges:
        query = (
            f'SELECT "{pk_column[src_table]}", "{src_col}" FROM "{src_table}" '
            f'WHERE "{src_col}" IS NOT NULL'
        )
        for src_pk, fk_value in conn.execute(query):
            if not isinstance(fk_value, int) or isinstance(fk_value, bool):
                raise ValidationError(
                    f"表 {src_table} 行主键 {src_pk} 的外键列 {src_col} "
                    f"含非整数数据 {fk_value!r}。"
                )
            source = (src_table, int(src_pk))
            target = (dst_table, int(fk_value))
            if source == target and src_table == dst_table:
                continue  # 自引用不产生删除依赖
            edges.add((source, target))
    return edges
