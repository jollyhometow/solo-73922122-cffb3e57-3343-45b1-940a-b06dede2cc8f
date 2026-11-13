"""运行期配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# 业务约束（题目硬性限制）
MAX_TABLES = 3
MAX_TOTAL_ROWS = 60

# 上传/脚本限制
MAX_DB_BYTES = _int("MAX_DB_BYTES", 20 * 1024 * 1024)
MAX_SCRIPT_BYTES = _int("MAX_SCRIPT_BYTES", 256 * 1024)

# 缩减过程限制
MAX_TRIALS = _int("MAX_TRIALS", 800)
TRIAL_TIMEOUT_SECONDS = _int("TRIAL_TIMEOUT_SECONDS", 15)

# 任务工作目录与保留时间
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
JOB_TTL_SECONDS = _int("JOB_TTL_SECONDS", 3600)
MAX_CONCURRENT_JOBS = _int("MAX_CONCURRENT_JOBS", 2)

# SQLite 官方保留前缀，用户表不允许使用
RESERVED_PREFIX = "sqlite_"
