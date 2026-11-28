"""快照合并模拟器：事件循环、块版本计算、冲突检测与崩溃恢复。"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .bitmap import (
    BITMAP_MERGED,
    BITMAP_PENDING,
    LoadedBitmap,
    MergeBitmap,
)
from .model import BlockStore, Extent, ExtentMap
from .recovery import (
    ACTION_CONTINUE,
    SOURCE_COW,
    SOURCE_ORIGIN,
    apply_recovery_plan,
    build_recovery_plan,
)

POLICY_MERGE_FIRST = "merge-first"
POLICY_WRITE_WINS = "write-wins"

EVENT_WRITE_ORIGIN = "write-origin"
EVENT_WRITE_COW = "write-cow"
EVENT_READ = "read"
EVENT_MERGE = "merge"
EVENT_FLUSH = "flush"
EVENT_CRASH = "crash"


@dataclass
class Event:
    """一个输入事件。

    写事件的 ``blocks`` 是块号列表；merge 事件可带 ``extent_ids``，缺省
    表示按映射顺序尝试合并所有未完成 extent。
    """

    kind: str
    blocks: Optional[List[int]] = None
    extent_ids: Optional[List[int]] = None
    policy: Optional[str] = None
    note: str = ""


@dataclass
class Conflict:
    """合并与 origin 写入在同一块/extent 上并发的记录。"""

    time: int
    extent_id: int
    block: int
    policy: str
    resolution: str
    origin_version: int
    cow_version: int


@dataclass
class TimelineEntry:
    """每个事件结束后一个时间点的完整可观察状态。"""

    time: int
    kind: str
    note: str
    block_views: Dict[int, Dict[str, Any]]
    bitmap: Dict[int, int]
    persisted_bitmap: Dict[int, int]
    alive: bool


class MergeSimulator:
    """按事件驱动 origin/COW 映射、合并位图和块版本。"""

    def __init__(self,
                 extent_map: ExtentMap,
                 blocks: Optional[BlockStore] = None,
                 policy: str = POLICY_MERGE_FIRST,
                 seed_origin: bool = True) -> None:
        if policy not in (POLICY_MERGE_FIRST, POLICY_WRITE_WINS):
            raise ValueError("未知冲突策略 %s" % policy)
        self.extent_map = extent_map
        self.blocks = blocks if blocks is not None else BlockStore()
        self.policy = policy
        self.bitmap = MergeBitmap()
        self.time = 0
        self.alive = True
        self.conflicts: List[Conflict] = []
        self.timeline: List[TimelineEntry] = []
        self._persisted: Dict[int, int] = {}
        self._origin_write_while_pending: set = set()
        self.last_flush_bytes: Optional[bytes] = None

        for extent in extent_map.all():
            self.bitmap.mark(extent.extent_id, BITMAP_PENDING)
            for offset in range(extent.length):
                cow_block = extent.cow_start + offset
                if seed_origin and self.blocks.cow_version(cow_block) == 0:
                    self.blocks.write_cow(cow_block, 0)
        self._persisted = self.bitmap.states_copy()

    # -- 基础查询 -------------------------------------------------------

    def _extent_state(self, extent_id: int) -> Optional[int]:
        return self.bitmap.state(extent_id)

    def block_source(self, block: int) -> str:
        """返回某块当前对上层可见的读取源，不存在映射时走 origin。"""
        extent = self.extent_map.find(block)
        if extent is None:
            return SOURCE_ORIGIN
        state = self._extent_state(extent.extent_id)
        if state == BITMAP_MERGED:
            return SOURCE_ORIGIN
        return SOURCE_COW

    def safe_read_sources(self) -> Dict[int, str]:
        """所有被映射块的安全读取源清单。"""
        result: Dict[int, str] = {}
        for extent in self.extent_map.all():
            source = (SOURCE_ORIGIN if self._extent_state(extent.extent_id)
                      == BITMAP_MERGED else SOURCE_COW)
            for offset in range(extent.length):
                result[extent.origin_start + offset] = source
        return result

    def read(self, block: int) -> Dict[str, Any]:
        self._require_alive()
        extent = self.extent_map.find(block)
        if extent is None or self._extent_state(extent.extent_id) == BITMAP_MERGED:
            return {
                "block": block,
                "source": SOURCE_ORIGIN,
                "version": self.blocks.origin_version(block),
            }
        cow_block = extent.cow_block_for(block)
        return {
            "block": block,
            "source": SOURCE_COW,
            "version": self.blocks.cow_version(cow_block),
        }

    # -- 写事件 ---------------------------------------------------------

    def write_origin(self, blocks: Sequence[int],
                     policy: Optional[str] = None) -> List[Conflict]:
        self._require_alive()
        chosen = policy or self.policy
        produced: List[Conflict] = []
        for block in blocks:
            extent = self.extent_map.find(block)
            if extent is not None and \
                    self._extent_state(extent.extent_id) == BITMAP_PENDING:
                cow_block = extent.cow_block_for(block)
                if chosen == POLICY_MERGE_FIRST:
                    self._merge_extent(extent.extent_id)
                    version = self.blocks.write_origin(block, self.time)
                    resolution = "merge-then-write-origin"
                else:
                    version = self.blocks.write_origin(block, self.time)
                    self._origin_write_while_pending.add(block)
                    resolution = "origin-write-preserved-skip-cow-copy"
                conflict = Conflict(
                    time=self.time,
                    extent_id=extent.extent_id,
                    block=block,
                    policy=chosen,
                    resolution=resolution,
                    origin_version=version,
                    cow_version=self.blocks.cow_version(cow_block),
                )
                produced.append(conflict)
                self.conflicts.append(conflict)
            else:
                self.blocks.write_origin(block, self.time)
        return produced

    def write_cow(self, blocks: Sequence[int]) -> None:
        self._require_alive()
        for block in blocks:
            extent = self.extent_map.find(block)
            if extent is None:
                raise ValueError("块 %d 不在任何 COW extent 内" % block)
            if self._extent_state(extent.extent_id) == BITMAP_MERGED:
                raise ValueError(
                    "extent %d 已合并，COW 重定向已关闭" % extent.extent_id
                )
            self.blocks.write_cow(extent.cow_block_for(block), self.time)

    # -- 合并、flush、崩溃 ----------------------------------------------

    def merge_extent(self, extent_id: int) -> None:
        self._require_alive()
        self._merge_extent(extent_id)

    def _merge_extent(self, extent_id: int) -> None:
        extent = self.extent_map.get(extent_id)
        if self._extent_state(extent_id) == BITMAP_MERGED:
            return
        for offset in range(extent.length):
            origin_block = extent.origin_start + offset
            cow_block = extent.cow_start + offset
            if origin_block in self._origin_write_while_pending:
                # 旧 COW 不允许覆盖合并期间 origin 上的新写。
                continue
            self.blocks.copy_cow_to_origin(cow_block, origin_block, self.time)
        self.bitmap.mark(extent_id, BITMAP_MERGED)

    def merge_pending(self) -> None:
        self._require_alive()
        for extent in self.extent_map.all():
            if self._extent_state(extent.extent_id) == BITMAP_PENDING:
                self._merge_extent(extent.extent_id)

    def flush(self) -> bytes:
        self._require_alive()
        self.bitmap.bump_generation()
        self._persisted = self.bitmap.states_copy()
        self.last_flush_bytes = self.bitmap.to_bytes()
        return self.last_flush_bytes

    def crash(self) -> bytes:
        """模拟崩溃：进程失活，返回最后一次 flush 持久化的位图字节。"""
        self.alive = False
        if self.last_flush_bytes is None:
            persisted = MergeBitmap(generation=self.bitmap.generation)
            for extent_id, state in self._persisted.items():
                persisted.mark(extent_id, state)
            self.last_flush_bytes = persisted.to_bytes()
        return self.last_flush_bytes

    def recover(self, raw_bitmap: Optional[bytes] = None,
                mode: str = ACTION_CONTINUE):
        """用持久化位图恢复，返回 (LoadedBitmap, RecoveryPlan)。"""
        if raw_bitmap is None:
            raw_bitmap = self.bitmap.to_bytes()
        loaded = MergeBitmap.from_bytes(raw_bitmap, self.extent_map.ids())
        plan = build_recovery_plan(loaded, self.extent_map, mode)
        apply_recovery_plan(
            plan, loaded, self.extent_map, self.blocks, self.bitmap
        )
        self.alive = True
        self._persisted = self.bitmap.states_copy()
        return loaded, plan

    # -- 事件循环 -------------------------------------------------------

    def run(self, events: Sequence[Any]) -> List[TimelineEntry]:
        for event in events:
            if isinstance(event, Event):
                current = event
            else:
                current = Event(**event)
            self.time += 1
            kind = current.kind
            if kind == EVENT_WRITE_ORIGIN:
                self.write_origin(current.blocks or [], current.policy)
            elif kind == EVENT_WRITE_COW:
                self.write_cow(current.blocks or [])
            elif kind == EVENT_READ:
                for block in current.blocks or []:
                    self.read(block)
            elif kind == EVENT_MERGE:
                if current.extent_ids:
                    for extent_id in current.extent_ids:
                        self._merge_extent(extent_id)
                else:
                    self.merge_pending()
            elif kind == EVENT_FLUSH:
                self.flush()
            elif kind == EVENT_CRASH:
                self.crash()
            else:
                raise ValueError("未知事件类型 %s" % kind)
            self.timeline.append(self._snapshot(kind, current.note))
        return self.timeline

    def conflict_extents(self) -> List[int]:
        return sorted({item.extent_id for item in self.conflicts})

    # -- 内部工具 -------------------------------------------------------

    def _require_alive(self) -> None:
        if not self.alive:
            raise RuntimeError("模拟器已崩溃，需要先 recover()")

    def _snapshot(self, kind: str, note: str) -> TimelineEntry:
        views: Dict[int, Dict[str, Any]] = {}
        for extent in self.extent_map.all():
            merged = self._extent_state(extent.extent_id) == BITMAP_MERGED
            for offset in range(extent.length):
                block = extent.origin_start + offset
                cow_block = extent.cow_start + offset
                if merged:
                    views[block] = {
                        "source": SOURCE_ORIGIN,
                        "version": self.blocks.origin_version(block),
                        "extent_id": extent.extent_id,
                    }
                else:
                    views[block] = {
                        "source": SOURCE_COW,
                        "version": self.blocks.cow_version(cow_block),
                        "extent_id": extent.extent_id,
                    }
        return TimelineEntry(
            time=self.time,
            kind=kind,
            note=note,
            block_views=views,
            bitmap=self.bitmap.states_copy(),
            persisted_bitmap=dict(self._persisted),
            alive=self.alive,
        )
