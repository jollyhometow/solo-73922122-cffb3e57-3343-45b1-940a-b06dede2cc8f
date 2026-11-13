"""内置样例：循环外键、先提交后失败。

样例文件同时通过 /static 目录提供下载，便于用户直接体验。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CYCLIC_SQL = """\
-- 三表循环外键：dept <-> employee <-> project
-- 原始库中所有外键均有效。下面的语句必然触发唯一约束错误。
INSERT INTO dept (id, name) VALUES (1, 'Platform');
"""

COMMIT_THEN_FAIL_SQL = """\
-- 先提交修改，再触发 NOT NULL 约束错误。
-- 删除的是叶子节点（无子节点引用），COMMIT 必然成功；
-- 随后的 INSERT 因 label 为 NULL 失败。
-- 每个试跑使用独立副本，已提交的 DELETE 不会污染原件或后续试跑。
BEGIN;
DELETE FROM node WHERE id = 3;
COMMIT;
INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);
"""


def build_cyclic(path: str | Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE dept (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE employee (
                id INTEGER PRIMARY KEY,
                dept_id INTEGER NOT NULL REFERENCES dept(id),
                mentor_id INTEGER REFERENCES employee(id)
            );
            CREATE TABLE project (
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES employee(id),
                dept_id INTEGER NOT NULL REFERENCES dept(id)
            );
            """
        )
        conn.executescript(
            """
            INSERT INTO dept (id, name) VALUES
                (1, 'Platform'), (2, 'QA'), (3, 'Sales');
            INSERT INTO employee (id, dept_id, mentor_id) VALUES
                (10, 1, NULL), (11, 1, 10), (12, 2, 10);
            -- 循环外键：项目属于部门，又由部门内员工负责
            INSERT INTO project (id, owner_id, dept_id) VALUES
                (100, 10, 1), (101, 11, 2), (102, 12, 3);
            """
        )
        conn.commit()
    finally:
        conn.close()


def build_commit_then_fail(path: str | Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE node (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES node(id),
                label TEXT NOT NULL
            );
            """
        )
        conn.executescript(
            """
            INSERT INTO node (id, parent_id, label) VALUES
                (1, NULL, 'root'),
                (2, 1, 'child-a'),
                (3, 2, 'child-b');
            """
        )
        conn.commit()
    finally:
        conn.close()


SAMPLES = {
    "cyclic": {
        "title": "循环外键（三表）+ 唯一约束错误",
        "db_builder": build_cyclic,
        "script": CYCLIC_SQL,
        "db_filename": "sample_cyclic.db",
        "sql_filename": "sample_cyclic.sql",
    },
    "commit_then_fail": {
        "title": "先提交后失败（自引用循环外键）",
        "db_builder": build_commit_then_fail,
        "script": COMMIT_THEN_FAIL_SQL,
        "db_filename": "sample_commit_then_fail.db",
        "sql_filename": "sample_commit_then_fail.sql",
    },
}


def ensure_samples(static_dir: Path) -> dict[str, dict[str, str]]:
    """在静态目录中生成样例文件，返回下载路径映射。"""
    static_dir.mkdir(parents=True, exist_ok=True)
    info: dict[str, dict[str, str]] = {}
    for key, spec in SAMPLES.items():
        db_path = static_dir / spec["db_filename"]
        sql_path = static_dir / spec["sql_filename"]
        if not db_path.exists():
            spec["db_builder"](db_path)
        if not sql_path.exists():
            sql_path.write_text(spec["script"], encoding="utf-8")
        info[key] = {
            "title": spec["title"],
            "db_url": f"/static/{spec['db_filename']}",
            "sql_url": f"/static/{spec['sql_filename']}",
        }
    return info
