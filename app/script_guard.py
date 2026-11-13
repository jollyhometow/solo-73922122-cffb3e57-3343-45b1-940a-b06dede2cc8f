"""SQL 脚本的静态安全检查。

运行期还会在每个连接上安装 authorizer 做强制拦截（见 runner.py），
这里的静态检查用于给出更友好的错误信息，并提前拒绝明显违规的脚本。
"""
from __future__ import annotations

import re

from . import config
from .errors import ScriptRejected

# 禁止出现的 SQL 关键字/内置函数。authorizer 是最终防线。
_BANNED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bATTACH\b", re.IGNORECASE), "禁止 ATTACH 附加任何数据库（包括内存库）。"),
    (re.compile(r"\bDETACH\b", re.IGNORECASE), "禁止 DETACH 操作。"),
    (
        re.compile(r"\bPRAGMA\s+(?!foreign_keys\b)", re.IGNORECASE),
        "仅允许 PRAGMA foreign_keys，禁止其它 PRAGMA。",
    ),
    (
        re.compile(r"\bload_extension\s*\(", re.IGNORECASE),
        "禁止 load_extension 加载扩展。",
    ),
    (
        re.compile(r"\bVACUUM\s+INTO\b", re.IGNORECASE),
        "禁止 VACUUM INTO 写外部文件。",
    ),
    (
        re.compile(r"\b(?:readfile|writefile)\s*\(", re.IGNORECASE),
        "禁止 readfile/writefile 访问外部文件。",
    ),
    (re.compile(r"\.import\b", re.IGNORECASE), "禁止 .import 等外部文件操作。"),
)


def parse_script(raw: bytes) -> str:
    if len(raw) == 0:
        raise ScriptRejected("SQL 脚本为空。")
    if len(raw) > config.MAX_SCRIPT_BYTES:
        raise ScriptRejected(
            f"脚本大小 {len(raw)} 字节，超过上限 {config.MAX_SCRIPT_BYTES} 字节。"
        )
    if b"\x00" in raw:
        raise ScriptRejected("脚本包含 NUL 字节。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScriptRejected(f"脚本必须是 UTF-8 文本：{exc}") from exc
    if not text.strip():
        raise ScriptRejected("SQL 脚本为空。")
    for pattern, message in _BANNED_PATTERNS:
        if pattern.search(text):
            raise ScriptRejected(message)
    return text
