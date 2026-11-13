"""数据库结构校验测试。"""
from __future__ import annotations

import sqlite3

import pytest

from app.errors import ValidationError
from app.validation import validate_database


def test_valid_cyclic_schema(cyclic_db):
    schema = validate_database(str(cyclic_db))
    assert schema.tables == ["dept", "employee", "project"]
    assert schema.pk_column == {"dept": "id", "employee": "id", "project": "id"}
    assert len(schema.fk_edges) == 4
    assert len(schema.rows) == 9
    # project 100 引用 employee 10 与 dept 1，删除 10 必须连带删除 100
    assert ("project", 100) in schema.reverse[("employee", 10)]
    assert ("project", 100) in schema.reverse[("dept", 1)]
    # 自引用 employee(11)->employee(10)
    assert ("employee", 11) in schema.reverse[("employee", 10)]


def test_closure_transitive(cyclic_db):
    schema = validate_database(str(cyclic_db))
    # 删除 dept 1 -> project 100/101? 101 属于 dept 2，不受影响；
    # employee 10/11 属于 dept 1，再连带它们负责的 project
    doomed = schema.closure({("dept", 1)})
    assert ("dept", 1) in doomed
    assert ("employee", 10) in doomed and ("employee", 11) in doomed
    assert ("project", 100) in doomed
    # dept 2、3 及其专属数据保留
    assert ("dept", 2) not in doomed and ("dept", 3) not in doomed


def test_reject_too_many_tables(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    for i in range(4):
        conn.execute(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(ValidationError, match="超过上限 3 张"):
        validate_database(str(path))


def test_reject_trigger(tmp_path, cyclic_db):
    conn = sqlite3.connect(str(cyclic_db))
    conn.execute(
        "CREATE TRIGGER trg AFTER INSERT ON dept BEGIN SELECT 1; END;"
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError, match="触发器"):
        validate_database(str(cyclic_db))


def test_reject_view_and_virtual_table(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE VIEW v AS SELECT * FROM a")
    conn.close()
    with pytest.raises(ValidationError, match="视图"):
        validate_database(str(path))

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE VIRTUAL TABLE f USING fts5(body)")
    conn.close()
    with pytest.raises(ValidationError, match="虚拟表"):
        validate_database(str(path))


def test_reject_composite_primary_key(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE a (x INTEGER, y INTEGER, PRIMARY KEY(x, y))")
    conn.close()
    with pytest.raises(ValidationError, match="主键不是单列"):
        validate_database(str(path))


def test_reject_without_rowid(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE a (id INTEGER PRIMARY KEY) WITHOUT ROWID"
    )
    conn.close()
    with pytest.raises(ValidationError, match="WITHOUT ROWID"):
        validate_database(str(path))


def test_reject_non_integer_fk(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id TEXT REFERENCES a(id));
        """
    )
    conn.close()
    with pytest.raises(ValidationError, match="必须是 INTEGER"):
        validate_database(str(path))


def test_reject_cascade_action(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (
            id INTEGER PRIMARY KEY,
            a_id INTEGER REFERENCES a(id) ON DELETE CASCADE
        );
        """
    )
    conn.close()
    with pytest.raises(ValidationError, match="ON DELETE"):
        validate_database(str(path))


def test_reject_fk_violation(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));
        INSERT INTO b VALUES (1, 99);
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError, match="外键违规"):
        validate_database(str(path))


def test_reject_no_foreign_key(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(ValidationError, match="至少需要包含一个外键"):
        validate_database(str(path))


def test_reject_row_limit(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));
        INSERT INTO a VALUES (1);
        """
    )
    conn.executemany("INSERT INTO b VALUES (?, 1)", [(i,) for i in range(60)])
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError, match="60 行"):
        validate_database(str(path))


def test_reject_bad_sqlite_file(tmp_path):
    path = tmp_path / "x.db"
    path.write_bytes(b"not a database at all" * 10)
    with pytest.raises(ValidationError, match="无法作为 SQLite"):
        validate_database(str(path))
