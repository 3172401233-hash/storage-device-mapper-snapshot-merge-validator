"""块存储与 extent 映射的内存模型。"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Block:
    """一个逻辑块在某个时间点的版本视图。"""

    address: int
    store: str
    version: int
    source: str


@dataclass(frozen=True)
class Extent:
    """一段连续的块范围。

    ``origin_start`` 是 origin 上的起始块号，``cow_start`` 是 COW 设备上
    重定向块的起始块号，长度由 ``length`` 给出。
    """

    extent_id: int
    origin_start: int
    cow_start: int
    length: int

    def contains(self, block: int) -> bool:
        return self.origin_start <= block < self.origin_start + self.length

    def cow_block_for(self, block: int) -> int:
        return self.cow_start + (block - self.origin_start)


class BlockStore:
    """保存 origin / COW 两侧的块版本号与写入代数。

    版本号在每次写入时单调递增，用于判断某个块内容的新旧；本模拟器不
    保存真实数据，只保存版本号与最近一次写入的代数。
    """

    def __init__(self) -> None:
        self._origin: Dict[int, int] = {}
        self._cow: Dict[int, int] = {}
        self._origin_generation: Dict[int, int] = {}
        self._cow_generation: Dict[int, int] = {}
        self._next_version = 1

    def _bump(self) -> int:
        version = self._next_version
        self._next_version += 1
        return version

    def write_origin(self, block: int, generation: int) -> int:
        version = self._bump()
        self._origin[block] = version
        self._origin_generation[block] = generation
        return version

    def write_cow(self, block: int, generation: int) -> int:
        version = self._bump()
        self._cow[block] = version
        self._cow_generation[block] = generation
        return version

    def copy_cow_to_origin(self, cow_block: int, origin_block: int,
                           generation: int) -> int:
        """把 COW 块版本搬到 origin，模拟一次块拷贝，仍生成新版本号。"""
        if cow_block not in self._cow:
            raise KeyError("cow block %d 从未写入" % cow_block)
        version = self._bump()
        self._origin[origin_block] = version
        self._origin_generation[origin_block] = generation
        return version

    def origin_version(self, block: int) -> int:
        return self._origin.get(block, 0)

    def cow_version(self, block: int) -> int:
        return self._cow.get(block, 0)

    def origin_generation(self, block: int) -> Optional[int]:
        return self._origin_generation.get(block)

    def cow_generation(self, block: int) -> Optional[int]:
        return self._cow_generation.get(block)

    def snapshot(self) -> Dict[str, Dict[int, int]]:
        return {
            "origin": dict(self._origin),
            "cow": dict(self._cow),
        }


class ExtentMap:
    """origin 块到 COW extent 的映射表。"""

    def __init__(self, extents: List[Extent]) -> None:
        checked = sorted(extents, key=lambda item: item.origin_start)
        for extent in checked:
            if extent.length <= 0 or extent.origin_start < 0 or extent.cow_start < 0:
                raise ValueError("extent %d 参数非法" % extent.extent_id)
        for previous, current in zip(checked, checked[1:]):
            if previous.origin_start + previous.length > current.origin_start:
                raise ValueError("extent 范围发生重叠")
        self._extents = checked
        self._by_id = {extent.extent_id: extent for extent in checked}

    @staticmethod
    def build(origin_starts: List[int], cow_starts: List[int],
              length: int) -> "ExtentMap":
        if len(origin_starts) != len(cow_starts):
            raise ValueError("origin/cow 起始块数量不一致")
        extents = [
            Extent(index, origin_start, cow_start, length)
            for index, (origin_start, cow_start)
            in enumerate(zip(origin_starts, cow_starts))
        ]
        return ExtentMap(extents)

    def all(self) -> List[Extent]:
        return list(self._extents)

    def ids(self) -> List[int]:
        return [extent.extent_id for extent in self._extents]

    def get(self, extent_id: int) -> Extent:
        return self._by_id[extent_id]

    def find(self, block: int) -> Optional[Extent]:
        for extent in self._extents:
            if extent.contains(block):
                return extent
        return None

    def __len__(self) -> int:
        return len(self._extents)
