"""边界场景：WAL 规范化、双向循环、试跑上限保护。"""
from __future__ import annotations

import sqlite3

import pytest

from app import config
from app.errors import ScriptRejected
from app.reducer import reduce_database
from app.runner import confirm_baseline, make_canonical_copy, run_on_copy
from app.validation import validate_database


def test_wal_database_is_canonicalized(tmp_path):
    """WAL 模式上传库被规范为 DELETE 模式，且不带侧车文件。"""
    src = tmp_path / "wal.db"
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (
            id INTEGER PRIMARY KEY, a_id INTEGER NOT NULL REFERENCES a(id)
        );
        INSERT INTO a VALUES (1), (2);
        INSERT INTO b VALUES (10, 1), (11, 2);
        """
    )
    conn.commit()
    conn.close()
    # WAL 模式持久化在数据库文件头中（正常关闭会清掉侧车文件）
    check = sqlite3.connect(str(src))
    assert check.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    check.close()

    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(src), str(canonical))
    assert not (tmp_path / "canon.db-wal").exists()
    assert not (tmp_path / "canon.db-shm").exists()
    c = sqlite3.connect(str(canonical))
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert c.execute("SELECT COUNT(*) FROM b").fetchone()[0] == 2
    c.close()


def test_mutual_two_table_cycle_accepted(tmp_path):
    """两张表互相引用（真正的循环外键）可以通过校验并缩减。"""
    path = tmp_path / "mutual.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE husband (
            id INTEGER PRIMARY KEY,
            wife_id INTEGER REFERENCES wife(id)
        );
        CREATE TABLE wife (
            id INTEGER PRIMARY KEY,
            husband_id INTEGER REFERENCES husband(id)
        );
        INSERT INTO husband VALUES (1, 1), (2, NULL);
        INSERT INTO wife VALUES (1, 1), (2, NULL);
        """
    )
    conn.commit()
    conn.close()

    schema = validate_database(str(path))
    assert len(schema.tables) == 2
    assert len(schema.fk_edges) == 2

    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(path), str(canonical))
    script = "INSERT INTO husband (id, wife_id) VALUES (1, 1);"
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    assert baseline.error_name == "SQLITE_CONSTRAINT_PRIMARYKEY"

    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline,
        str(tmp_path / "red.db"),
    )
    assert outcome.final.same_error(baseline)
    # 双向循环：删 husband 1 的闭包必须带上 wife 1
    doomed = schema.closure({("husband", 1)})
    assert ("wife", 1) in doomed


def test_max_trials_guard(monkeypatch, tmp_path, cyclic_db):
    """达到试跑上限时明确报错而不是无限运行。"""
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(cyclic_db), str(canonical))
    schema = validate_database(str(cyclic_db))
    script = "INSERT INTO dept (id, name) VALUES (1, 'Platform');"
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))

    monkeypatch.setattr(config, "MAX_TRIALS", 2)
    with pytest.raises(ScriptRejected, match="试跑次数达到上限"):
        reduce_database(
            str(canonical), script, str(tmp_path), schema, baseline,
            str(tmp_path / "red.db"),
        )


def test_brute_force_irreducibility_commit_then_fail(tmp_path):
    """对先提交后失败场景做暴力枚举：每个保留行的闭包删除都不能复现。"""
    from app.runner import delete_rows

    path = tmp_path / "nodes.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE node (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES node(id),
            label TEXT NOT NULL
        );
        INSERT INTO node VALUES
            (1, NULL, 'root'), (2, 1, 'a'), (3, 2, 'b'), (4, 1, 'c');
        """
    )
    conn.commit()
    conn.close()

    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(path), str(canonical))
    schema = validate_database(str(path))
    script = (
        "BEGIN; DELETE FROM node WHERE id = 3; COMMIT; "
        "INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);"
    )
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline,
        str(tmp_path / "red.db"),
    )

    for row in sorted(outcome.kept_rows):
        doomed = schema.closure({row}) & outcome.kept_rows
        candidate = outcome.kept_rows - doomed
        cp = tmp_path / f"bf_{row[1]}.db"
        delete_rows(
            str(canonical), str(tmp_path), candidate,
            schema.tables, schema.pk_column, str(cp),
        )
        result = run_on_copy(str(cp), script, str(tmp_path))
        assert not result.same_error(baseline)


def test_remainder_foreign_keys_valid_under_cycle(tmp_path):
    """循环外键库缩减后，剩余行之间的引用全部有效。"""
    path = tmp_path / "cyc.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER);
        CREATE TABLE b (
            id INTEGER PRIMARY KEY,
            a_id INTEGER NOT NULL REFERENCES a(id)
        );
        """
    )
    conn.execute("INSERT INTO a VALUES (1, NULL), (2, NULL)")
    conn.execute("INSERT INTO b VALUES (10, 1), (11, 2)")
    conn.execute("UPDATE a SET b_id=10 WHERE id=1")
    conn.execute("UPDATE a SET b_id=11 WHERE id=2")
    conn.commit()
    conn.close()

    # a.b_id 没有声明 REFERENCES，不满足“外键”要求，补声明后重建
    conn = sqlite3.connect(str(path))
    conn.close()
    path2 = tmp_path / "cyc2.db"
    conn = sqlite3.connect(str(path2))
    conn.executescript(
        """
        CREATE TABLE a (
            id INTEGER PRIMARY KEY, b_id INTEGER REFERENCES b(id)
        );
        CREATE TABLE b (
            id INTEGER PRIMARY KEY, a_id INTEGER NOT NULL REFERENCES a(id)
        );
        INSERT INTO a VALUES (1, 10), (2, 11);
        INSERT INTO b VALUES (10, 1), (11, 2);
        """
    )
    conn.commit()
    conn.close()

    schema = validate_database(str(path2))
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(path2), str(canonical))
    script = "INSERT INTO a (id, b_id) VALUES (1, 10);"
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline,
        str(tmp_path / "red.db"),
    )
    check = sqlite3.connect(str(tmp_path / "red.db"))
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    check.close()
    assert outcome.final.same_error(baseline)
