"""崩溃恢复器：根据持久化位图生成继续合并或回滚的范围与顺序。"""

from dataclasses import dataclass, field
from typing import List, Optional

from .bitmap import (
    BITMAP_MERGED,
    BITMAP_PENDING,
    LoadedBitmap,
)
from .model import BlockStore, ExtentMap

ACTION_CONTINUE = "continue-merge"
ACTION_ROLLBACK = "rollback"

SOURCE_ORIGIN = "origin"
SOURCE_COW = "cow"
SOURCE_QUARANTINE = "quarantine"


@dataclass
class RecoveryRange:
    """一个可执行的恢复步骤，extent 按顺序排列。"""

    order: int
    extent_id: int
    action: str
    from_store: str
    to_store: str
    reason: str


@dataclass
class RecoveryPlan:
    """恢复计划：可执行范围、隔离清单与已完成（幂等跳过）的范围。"""

    mode: str
    ranges: List[RecoveryRange] = field(default_factory=list)
    isolated: List[int] = field(default_factory=list)
    already_done: List[int] = field(default_factory=list)
    irreversible_merged: List[int] = field(default_factory=list)
    performed: List[RecoveryRange] = field(default_factory=list)

    def ordered_extent_ids(self) -> List[int]:
        return [item.extent_id for item in self.ranges]


def safe_source(extent_id: int, loaded: LoadedBitmap) -> str:
    """返回某 extent 当前的安全读取源。

    隔离 extent 不可自动选择 origin 或 COW，必须先人工核查，因此返回
    ``quarantine`` 而不是猜测一个映射。
    """
    if loaded.is_isolated(extent_id):
        return SOURCE_QUARANTINE
    state = loaded.states.get(extent_id)
    if state == BITMAP_MERGED:
        return SOURCE_ORIGIN
    if state == BITMAP_PENDING:
        return SOURCE_COW
    return SOURCE_QUARANTINE


def build_recovery_plan(loaded: LoadedBitmap, extent_map: ExtentMap,
                        mode: str = ACTION_CONTINUE) -> RecoveryPlan:
    """按确定性顺序（extent id 升序）构造恢复计划。

    continue-merge：PENDING 继续把 COW 拷到 origin；MERGED 幂等跳过。
    rollback：PENDING 解除 COW 映射、恢复 origin 直写；已经拷到 origin 的
    MERGED 范围无法撤销，单独列出供调用方决策。
    隔离范围永远不进入执行队列。
    """
    if mode not in (ACTION_CONTINUE, ACTION_ROLLBACK):
        raise ValueError("未知恢复模式 %s" % mode)

    plan = RecoveryPlan(mode=mode)
    plan.isolated = sorted(
        item.extent_id for item in loaded.isolated
        if item.extent_id in extent_map.ids()
    )
    order = 0
    for extent_id in extent_map.ids():
        if loaded.is_isolated(extent_id):
            continue
        state = loaded.states.get(extent_id)
        if mode == ACTION_CONTINUE:
            if state == BITMAP_PENDING:
                plan.ranges.append(
                    RecoveryRange(
                        order=order,
                        extent_id=extent_id,
                        action=ACTION_CONTINUE,
                        from_store=SOURCE_COW,
                        to_store=SOURCE_ORIGIN,
                        reason="bitmap-pending",
                    )
                )
                order += 1
            elif state == BITMAP_MERGED:
                plan.already_done.append(extent_id)
        else:
            if state == BITMAP_PENDING:
                plan.ranges.append(
                    RecoveryRange(
                        order=order,
                        extent_id=extent_id,
                        action=ACTION_ROLLBACK,
                        from_store=SOURCE_COW,
                        to_store=SOURCE_ORIGIN,
                        reason="discard-cow-mapping",
                    )
                )
                order += 1
            elif state == BITMAP_MERGED:
                plan.irreversible_merged.append(extent_id)
                plan.already_done.append(extent_id)
    return plan


def apply_recovery_plan(plan: RecoveryPlan, loaded: LoadedBitmap,
                        extent_map: ExtentMap, blocks: BlockStore,
                        bitmap: "MergeBitmapLike",
                        generation: Optional[int] = None) -> RecoveryPlan:
    """执行恢复计划并原位更新位图；重复执行时已完成范围自动跳过。

    隔离范围保持不动，调用方必须先修复位图或人工接管。
    """
    from .bitmap import BITMAP_MERGED, BITMAP_PENDING, MergeBitmap

    gen = bitmap.generation if generation is None else generation
    for step in plan.ranges:
        current = loaded.states.get(step.extent_id)
        if plan.mode == ACTION_CONTINUE:
            if current == BITMAP_MERGED:
                continue
            extent = extent_map.get(step.extent_id)
            for offset in range(extent.length):
                origin_block = extent.origin_start + offset
                cow_block = extent.cow_start + offset
                blocks.copy_cow_to_origin(cow_block, origin_block, gen)
            bitmap.mark(step.extent_id, BITMAP_MERGED)
            loaded.states[step.extent_id] = BITMAP_MERGED
            plan.performed.append(step)
        else:
            if current is None:
                continue
            loaded.states.pop(step.extent_id, None)
            bitmap.remove(step.extent_id)
            plan.performed.append(step)

    for extent_id in plan.already_done:
        if plan.mode == ACTION_CONTINUE:
            bitmap.mark(extent_id, BITMAP_MERGED)
            loaded.states[extent_id] = BITMAP_MERGED
    return plan
