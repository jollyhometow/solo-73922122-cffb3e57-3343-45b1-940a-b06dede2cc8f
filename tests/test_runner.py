"""脚本守卫、试跑隔离与基线确认测试。"""
from __future__ import annotations

import sqlite3

import pytest

from app.errors import ScriptRejected
from app.runner import (
    ScriptResult,
    confirm_baseline,
    make_canonical_copy,
    run_on_copy,
)
from app.script_guard import parse_script


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH '/tmp/x.db' AS x; SELECT 1;",
        "DETACH x;",
        "PRAGMA journal_mode=DELETE;",
        "SELECT load_extension('x.so');",
        "VACUUM INTO '/tmp/out.db';",
        "SELECT readfile('/etc/passwd');",
        "SELECT writefile('/tmp/x', 'y');",
    ],
)
def test_guard_rejects_file_access(sql):
    with pytest.raises(ScriptRejected):
        parse_script(sql.encode())


def test_guard_allows_foreign_keys_pragma():
    text = parse_script(b"PRAGMA foreign_keys=ON; SELECT 1;")
    assert "SELECT 1" in text


def test_guard_rejects_empty_and_oversize():
    with pytest.raises(ScriptRejected):
        parse_script(b"")
    with pytest.raises(ScriptRejected):
        parse_script(b"   \n  ")
    with pytest.raises(ScriptRejected):
        parse_script(b"SELECT 1;\x00")


def test_authorizer_blocks_attach_at_runtime(tmp_path, cyclic_db):
    # 绕过静态检查，验证 authorizer 是最终防线
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(cyclic_db), str(canonical))
    result = run_on_copy(
        str(canonical), "ATTACH '/tmp/evil.db' AS evil;", str(tmp_path)
    )
    assert not result.ok
    assert result.error_name == "SQLITE_AUTH"
    assert "not authorized" in (result.error_message or "")


def test_authorizer_blocks_pragma_at_runtime(tmp_path, cyclic_db):
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(cyclic_db), str(canonical))
    result = run_on_copy(
        str(canonical), "PRAGMA trusted_schema=ON;", str(tmp_path)
    )
    assert not result.ok and result.error_name == "SQLITE_AUTH"


def test_trial_copy_isolation_commit_then_fail(tmp_path, self_ref_db):
    """先 COMMIT 再失败：修改只落在临时副本，原件与后续试跑不受影响。"""
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(self_ref_db), str(canonical))
    original_count = sqlite3.connect(str(canonical)).execute(
        "SELECT COUNT(*) FROM node"
    ).fetchone()[0]

    script = (
        "BEGIN; DELETE FROM node WHERE id=3; COMMIT; "
        "INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);"
    )
    first = run_on_copy(str(canonical), script, str(tmp_path))
    second = run_on_copy(str(canonical), script, str(tmp_path))

    assert not first.ok and first.error_name == "SQLITE_CONSTRAINT_NOTNULL"
    assert first.same_error(second)
    # 原件未被污染
    conn = sqlite3.connect(str(canonical))
    assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == original_count
    assert conn.execute("SELECT label FROM node WHERE id=3").fetchone()[0] == "b"
    conn.close()
    # 临时副本与侧车文件已清理
    leftovers = list(tmp_path.glob("trial_*"))
    assert leftovers == []


def test_foreign_keys_enforced_in_trial(tmp_path, cyclic_db):
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(cyclic_db), str(canonical))
    result = run_on_copy(
        str(canonical),
        "INSERT INTO employee (id, dept_id) VALUES (99, 999);",
        str(tmp_path),
    )
    assert not result.ok
    assert result.error_name == "SQLITE_CONSTRAINT_FOREIGNKEY"
    assert result.error_code == 787
    assert result.error_message == "FOREIGN KEY constraint failed"


def test_confirm_baseline_rejects_inconsistent_runs(monkeypatch, tmp_path, cyclic_db):
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(cyclic_db), str(canonical))
    calls = {"n": 0}
    from app import runner

    def fake_run(path, script, work_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            return ScriptResult(
                ok=False, error_code=1555,
                error_name="SQLITE_CONSTRAINT_PRIMARYKEY",
                error_message="UNIQUE constraint failed: dept.id",
            )
        return ScriptResult(ok=True)

    monkeypatch.setattr(runner, "run_on_copy", fake_run)
    with pytest.raises(ScriptRejected, match="确定性"):
        runner.confirm_baseline(str(canonical), "SELECT 1;", str(tmp_path))


def test_confirm_baseline_success_returns_ok(tmp_path, cyclic_db):
    canonical = tmp_path / "canon.db"
    make_canonical_copy(str(cyclic_db), str(canonical))
    result = confirm_baseline(str(canonical), "SELECT 1;", str(tmp_path))
    assert result.ok
