"""pytest 共享夹具。"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def cyclic_db(tmp_path):
    """三表循环外键库（与内置样例一致）。"""
    path = tmp_path / "cyclic.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
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
        INSERT INTO dept VALUES (1,'Platform'),(2,'QA'),(3,'Sales');
        INSERT INTO employee VALUES (10,1,NULL),(11,1,10),(12,2,10);
        INSERT INTO project VALUES (100,10,1),(101,11,2),(102,12,3);
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def self_ref_db(tmp_path):
    """自引用（循环）外键库。"""
    path = tmp_path / "nodes.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE node (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES node(id),
            label TEXT NOT NULL
        );
        INSERT INTO node VALUES (1,NULL,'root'),(2,1,'a'),(3,2,'b'),(4,1,'c');
        """
    )
    conn.commit()
    conn.close()
    return path


UNIQUE_SQL = "INSERT INTO dept (id, name) VALUES (1, 'Platform');"
