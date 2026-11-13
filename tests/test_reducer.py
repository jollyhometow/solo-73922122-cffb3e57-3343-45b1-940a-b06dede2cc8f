"""缩减器正确性测试。"""
from __future__ import annotations

import sqlite3

from app.reducer import reduce_database
from app.runner import (
    confirm_baseline,
    make_canonical_copy,
    run_on_copy,
)
from app.validation import validate_database


def _prepare(tmp_path, source_db):
    canonical = tmp_path / "canonical.db"
    make_canonical_copy(str(source_db), str(canonical))
    return canonical


def _assert_irreducible(outcome, canonical, script, tmp_path, schema):
    """题目硬性终态：删除任一保留行及其外键闭包后都无法复现同一错误。"""
    from app.runner import delete_rows

    target = outcome.baseline
    for row in sorted(outcome.kept_rows):
        doomed = schema.closure({row})
        candidate = outcome.kept_rows - doomed
        cand_path = tmp_path / f"irre_{row[0]}_{row[1]}.db"
        delete_rows(
            str(canonical), str(tmp_path), candidate,
            schema.tables, schema.pk_column, str(cand_path),
        )
        result = run_on_copy(str(cand_path), script, str(tmp_path))
        assert not result.same_error(target), (
            f"删除 {row} 及其闭包后仍能复现，缩减结果不是 1-最小的"
        )


def _assert_fk_valid(db_path, schema):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # 表结构保持不变
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == set(schema.tables)
    conn.close()


def test_reduce_unique_error_cyclic_db(tmp_path, cyclic_db):
    canonical = _prepare(tmp_path, cyclic_db)
    schema = validate_database(str(cyclic_db))
    script = "INSERT INTO dept (id, name) VALUES (1, 'Platform');"
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    assert not baseline.ok
    assert baseline.error_name == "SQLITE_CONSTRAINT_PRIMARYKEY"

    reduced = tmp_path / "reduced.db"
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline, str(reduced)
    )

    # 最终库仍复现完全相同的错误码与完整文本
    assert outcome.final.same_error(baseline)
    assert outcome.final.error_message == baseline.error_message
    # 至少删掉了一些行
    assert len(outcome.kept_rows) < len(schema.rows)
    # 1-最小性
    _assert_irreducible(outcome, canonical, script, tmp_path, schema)
    # 结构与外键有效
    _assert_fk_valid(reduced, schema)


def test_reduce_commit_then_fail_self_ref(tmp_path, self_ref_db):
    canonical = _prepare(tmp_path, self_ref_db)
    schema = validate_database(str(self_ref_db))
    script = (
        "BEGIN; DELETE FROM node WHERE id = 3; COMMIT; "
        "INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);"
    )
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    assert baseline.error_name == "SQLITE_CONSTRAINT_NOTNULL"

    reduced = tmp_path / "reduced.db"
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline, str(reduced)
    )
    assert outcome.final.same_error(baseline)
    _assert_irreducible(outcome, canonical, script, tmp_path, schema)
    _assert_fk_valid(reduced, schema)

    # 循环自引用闭包：保留 parent 链时，子节点也必须保留
    conn = sqlite3.connect(str(reduced))
    kept = dict(conn.execute("SELECT id, parent_id FROM node").fetchall())
    conn.close()
    for node_id, parent_id in kept.items():
        if parent_id is not None:
            assert parent_id in kept, f"节点 {node_id} 的父节点 {parent_id} 被误删"


def test_different_error_does_not_count(tmp_path, cyclic_db):
    """复现判定严格：错误码或文本不同一律拒绝缩减步骤。"""
    canonical = _prepare(tmp_path, cyclic_db)
    schema = validate_database(str(cyclic_db))
    # 目标错误：主键冲突（dept.id=1 已存在）
    target_script = "INSERT INTO dept (id, name) VALUES (1, 'X');"
    baseline = confirm_baseline(str(canonical), target_script, str(tmp_path))

    # 构造一个返回“其它错误”的试跑结果（外键错误，码与文本均不同）
    other = run_on_copy(
        str(canonical),
        "INSERT INTO employee (id, dept_id) VALUES (99, 999);",
        str(tmp_path),
    )
    assert not other.ok
    assert not other.same_error(baseline)

    # 不同错误文本（同码不同消息）也不算复现
    from app.runner import ScriptResult

    variant = ScriptResult(
        ok=False, error_code=baseline.error_code,
        error_name=baseline.error_name,
        error_message="UNIQUE constraint failed: dept.name",
    )
    assert not variant.same_error(baseline)


def test_reduction_to_empty_when_error_structure_independent(tmp_path):
    """不依赖数据的错误（如 NOT NULL 列直接插 NULL）允许缩减到 0 行，空库仍复现。"""
    db = tmp_path / "emptyish.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER NOT NULL REFERENCES a(id));
        INSERT INTO a VALUES (1, 'x'), (2, 'y');
        INSERT INTO b VALUES (10, 1), (11, 2);
        """
    )
    conn.commit()
    conn.close()

    canonical = _prepare(tmp_path, db)
    schema = validate_database(str(db))
    script = "INSERT INTO a (id, name) VALUES (100, NULL);"
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    assert baseline.error_name == "SQLITE_CONSTRAINT_NOTNULL"

    reduced = tmp_path / "reduced.db"
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline, str(reduced)
    )
    assert outcome.kept_rows == set()
    assert outcome.final.same_error(baseline)
    _assert_fk_valid(reduced, schema)
    conn = sqlite3.connect(str(reduced))
    assert conn.execute("SELECT COUNT(*) FROM a").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM b").fetchone()[0] == 0
    conn.close()


def test_remaining_row_values_unchanged(tmp_path, cyclic_db):
    """保留行的值必须与原件逐字节一致。"""
    canonical = _prepare(tmp_path, cyclic_db)
    schema = validate_database(str(cyclic_db))
    script = "INSERT INTO dept (id, name) VALUES (1, 'Platform');"
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    reduced = tmp_path / "reduced.db"
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline, str(reduced)
    )

    old = sqlite3.connect(str(canonical))
    new = sqlite3.connect(str(reduced))
    for table in schema.tables:
        old_rows = old.execute(f'SELECT * FROM "{table}"').fetchall()
        new_rows = {r[0]: r for r in new.execute(f'SELECT * FROM "{table}"').fetchall()}
        for row in old_rows:
            if (table, row[0]) in outcome.kept_rows:
                assert tuple(new_rows[row[0]]) == tuple(row)
    old.close()
    new.close()


def test_canonical_never_mutated(tmp_path, cyclic_db):
    """所有试跑结束后规范原件保持原样。"""
    canonical = _prepare(tmp_path, cyclic_db)
    schema = validate_database(str(cyclic_db))
    before = {
        t: sqlite3.connect(str(canonical)).execute(
            f'SELECT COUNT(*) FROM "{t}"'
        ).fetchone()[0]
        for t in schema.tables
    }
    script = (
        "BEGIN; UPDATE dept SET name='HACKED' WHERE id=1; COMMIT; "
        "INSERT INTO dept (id, name) VALUES (1, 'x');"
    )
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline,
        str(tmp_path / "reduced.db"),
    )
    conn = sqlite3.connect(str(canonical))
    assert conn.execute("SELECT name FROM dept WHERE id=1").fetchone()[0] == "Platform"
    for t in schema.tables:
        assert conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == before[t]
    conn.close()
    assert not list(tmp_path.glob("trial_*"))
    assert not list(tmp_path.glob("*wal"))
