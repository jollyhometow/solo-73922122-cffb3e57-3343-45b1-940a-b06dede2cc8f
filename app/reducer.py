"""行级 Delta Debugging 缩减器。

删除任何一行时，必须连带删除直接或间接引用它的全部行（外键闭包），
否则剩余数据无法满足外键约束。算法：

1. 标准 ddmin 状态机：把当前保留行切成 n 份，先尝试删除一个分块，
   再尝试删除其补集；任何候选删除后仍复现同一错误
   （错误码 + 完整错误文本一致）就采纳，否则细化粒度直到单元素；
2. 逐行（含闭包）终检：反复整轮扫描，直到删除任一保留行的闭包都无法复现，
   即达到 1-最小；
3. 闭包只在当前保留集合内扩张，保证候选集始终外键封闭。

其它任何错误（错误码不同、错误文本不同、成功、超时）都不算复现。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .errors import ScriptRejected
from .runner import (
    ScriptResult,
    TrialTimeout,
    delete_rows,
    run_on_copy,
)
from .validation import RowId, Schema


@dataclass
class ReduceStats:
    trials: int = 0
    accepted_steps: int = 0
    log: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(message)


@dataclass
class ReduceOutcome:
    kept_rows: set[RowId]
    baseline: ScriptResult
    final: ScriptResult
    stats: ReduceStats
    row_counts_before: dict[str, int]
    row_counts_after: dict[str, int]
    reduced_path: str


def _reproduces(candidate_path: str, script: str, work_dir: str,
                target: ScriptResult, stats: ReduceStats) -> bool:
    """在候选库上试跑，只有同一错误才算复现。"""
    if stats.trials >= config.MAX_TRIALS:
        raise ScriptRejected(
            f"试跑次数达到上限 {config.MAX_TRIALS}，停止缩减。"
        )
    stats.trials += 1
    try:
        result = run_on_copy(candidate_path, script, work_dir)
    except TrialTimeout as exc:
        stats.note(f"第 {stats.trials} 次试跑超时：{exc}")
        return False
    return result.same_error(target)


def reduce_database(canonical_path: str, script: str, work_dir: str,
                    schema: Schema, baseline: ScriptResult,
                    reduced_path: str) -> ReduceOutcome:
    stats = ReduceStats()
    kept: set[RowId] = set(schema.rows)
    before_counts = {
        table: sum(1 for (t, _pk) in kept if t == table) for table in schema.tables
    }
    stats.note(f"初始 {len(kept)} 行，目标错误 {baseline.error_name}: "
               f"{baseline.error_message}")

    def try_delete(roots: set[RowId], label: str) -> bool:
        """尝试删除 roots 的外键闭包并在新副本上验证。

        闭包只在当前保留集合内扩张：引用行若此前已被删除，无需再次处理。
        """
        doomed = schema.closure(roots) & kept
        if not doomed:
            return False
        candidate = kept - doomed
        candidate_path = f"{work_dir}/candidate.db"
        delete_rows(
            canonical_path, work_dir, candidate,
            schema.tables, schema.pk_column, candidate_path,
        )
        if _reproduces(candidate_path, script, work_dir, baseline, stats):
            kept.difference_update(doomed)
            stats.accepted_steps += 1
            stats.note(
                f"第 {stats.trials} 次试跑复现成功，{label}：删除 {len(doomed)} 行，"
                f"保留 {len(kept)} 行。"
            )
            return True
        stats.note(
            f"第 {stats.trials} 次试跑未复现同一错误，{label} 被拒绝（拟删 {len(doomed)} 行）。"
        )
        return False

    # ---- 阶段 1：标准 ddmin 状态机（保证终止，粒度细化到单个元素）----
    n = 2
    while len(kept) >= 2:
        ordered = sorted(kept)
        size = len(ordered)
        if n > size:
            break
        chunk_size = size // n
        if chunk_size == 0:
            break
        chunks: list[set[RowId]] = []
        for i in range(n):
            start = i * chunk_size
            end = size if i == n - 1 else start + chunk_size
            piece = set(ordered[start:end])
            if piece:
                chunks.append(piece)

        progressed = False
        # 1) 尝试删除单个分块
        for i, chunk in enumerate(chunks):
            if try_delete(chunk, f"ddmin n={n} 第{i + 1}块"):
                progressed = True
                n = 2  # 采纳后回到粗粒度
                break
        if progressed:
            continue

        # 2) 尝试删除“除一个分块外的全部内容”（补集，加速向小集合收敛）
        if n < size:
            for i, chunk in enumerate(chunks):
                complement = set(ordered) - chunk
                if complement and try_delete(
                    complement, f"ddmin n={n} 第{i + 1}块补集"
                ):
                    progressed = True
                    n = max(n - 1, 2)
                    break
        if progressed:
            continue

        # 3) 本粒度两个方向都失败：细化粒度
        if n >= size:
            break
        n = min(n * 2, size)

    # ---- 阶段 2：逐行（含闭包）1-最小性终检 ----
    # 反复做整轮扫描：每轮对每个当前保留行尝试删除其外键闭包，
    # 一轮中只采纳第一个可删集合后重扫，直到一整轮无任何采纳。
    changed = True
    while changed:
        changed = False
        for row in sorted(kept):
            if row not in kept:
                continue
            if try_delete({row}, "逐行终检"):
                changed = True
                break

    # ---- 产出最终缩减库并再次验证 ----
    delete_rows(
        canonical_path, work_dir, kept,
        schema.tables, schema.pk_column, reduced_path,
    )
    final = run_on_copy(reduced_path, script, work_dir)
    stats.trials += 1
    if final.ok or not final.same_error(baseline):
        raise RuntimeError("最终缩减库未能复现目标错误（内部错误）。")

    after_counts = {
        table: sum(1 for (t, _pk) in kept if t == table) for table in schema.tables
    }
    stats.note(f"完成：{before_counts} -> {after_counts}，共试跑 {stats.trials} 次。")
    return ReduceOutcome(
        kept_rows=kept,
        baseline=baseline,
        final=final,
        stats=stats,
        row_counts_before=before_counts,
        row_counts_after=after_counts,
        reduced_path=reduced_path,
    )
