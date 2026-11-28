"""Device-mapper 快照合并的纯软件模拟与崩溃恢复工具。

本包只在内存中模拟 origin / COW 块映射，不打开、不读写任何真实块设备。
"""

from .bitmap import (
    BITMAP_MERGED,
    BITMAP_PENDING,
    LoadedBitmap,
    MergeBitmap,
)
from .model import BlockStore, Extent, ExtentMap
from .recovery import (
    ACTION_CONTINUE,
    ACTION_ROLLBACK,
    RecoveryPlan,
    RecoveryRange,
    apply_recovery_plan,
    build_recovery_plan,
    safe_source,
)
from .simulator import (
    POLICY_MERGE_FIRST,
    POLICY_WRITE_WINS,
    Conflict,
    Event,
    MergeSimulator,
    TimelineEntry,
)

__all__ = [
    "BlockStore",
    "Extent",
    "ExtentMap",
    "MergeBitmap",
    "LoadedBitmap",
    "BITMAP_PENDING",
    "BITMAP_MERGED",
    "RecoveryPlan",
    "RecoveryRange",
    "ACTION_CONTINUE",
    "ACTION_ROLLBACK",
    "build_recovery_plan",
    "apply_recovery_plan",
    "safe_source",
    "MergeSimulator",
    "Event",
    "Conflict",
    "TimelineEntry",
    "POLICY_MERGE_FIRST",
    "POLICY_WRITE_WINS",
]
