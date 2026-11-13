"""上传 -> 校验 -> 确认错误 -> 缩减 的完整流水线。"""
from __future__ import annotations

from pathlib import Path

from .jobs import Job
from .reducer import reduce_database
from .runner import confirm_baseline, make_canonical_copy
from .script_guard import parse_script
from .validation import validate_database


def run_pipeline(job: Job, uploaded_db: str, raw_script: bytes) -> None:
    job.status = "running"
    job.message = "正在校验数据库结构与外键…"

    script = parse_script(raw_script)
    schema = validate_database(uploaded_db)

    job_dir = Path(job.dir)
    canonical = job_dir / "canonical.db"
    make_canonical_copy(uploaded_db, str(canonical))

    job.message = "正在独立副本上确认原始错误并校验确定性…"
    baseline = confirm_baseline(str(canonical), script, str(job_dir))
    if baseline.ok:
        # 原脚本成功 -> 无需缩减
        job.status = "done"
        job.message = "原脚本执行成功，没有产生错误，因此无需缩减。"
        job.result = {
            "kind": "success_no_reduction",
            "baseline": baseline.to_dict(),
            "row_counts_before": {
                table: sum(1 for (t, _pk) in schema.rows if t == table)
                for table in schema.tables
            },
            "row_counts_after": {
                table: sum(1 for (t, _pk) in schema.rows if t == table)
                for table in schema.tables
            },
            "tables": schema.tables,
            "trials": 0,
            "download_available": False,
        }
        return

    job.message = "已复现原始错误，开始 Delta Debugging 缩减…"
    reduced = job_dir / "reduced.db"
    outcome = reduce_database(
        str(canonical), script, str(job_dir), schema, baseline, str(reduced)
    )

    job.status = "done"
    job.message = "缩减完成：删除任一行及其外键闭包后都无法再复现同一错误。"
    job.result = {
        "kind": "reduced",
        "baseline": outcome.baseline.to_dict(),
        "final": outcome.final.to_dict(),
        "row_counts_before": outcome.row_counts_before,
        "row_counts_after": outcome.row_counts_after,
        "tables": schema.tables,
        "trials": outcome.stats.trials,
        "accepted_steps": outcome.stats.accepted_steps,
        "kept_rows": [
            {"table": table, "pk": pk} for table, pk in sorted(outcome.kept_rows)
        ],
        "log": outcome.stats.log,
        "download_available": True,
    }
